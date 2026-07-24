"""
pending_orders.py — 미확정 주문(pending) 메모리 레지스트리 (갭2 단계 1)

목적:
  주문 '접수(rt_cd==0)'와 '실제 체결'을 분리하기 위한, 미확정 주문의
  휘발성 작업상태를 담는 **메모리 전용** 캐시.

원칙 (설계 확정 사항):
  - KIS 체결 상태가 항상 최종 진실(SoT). 이 레지스트리는 그 진실을 향한
    메모리 캐시일 뿐이다. → 디스크 저장 없음, KIS API 호출 없음.
  - apply 는 반드시 '체결 델타'로만 누적한다(applied_qty 증가).
  - order_no 를 멱등키로 사용한다. 같은 order_no 재등록은 기존 주문을
    덮어쓰거나 초기화하지 않는다(멱등).
  - 시간은 clock(now_fn) 주입으로 테스트 가능하게 한다.
  - 최종 설계상 루프 선두 poll + 전용 poll 잡이 APScheduler 스레드풀에서
    동시 실행될 수 있으므로 최소한의 Lock 으로 변이를 보호한다.
    (재진입 없음이 검증되어 RLock 대신 plain Lock 사용.)
  - apply_delta() 는 불변 FillApplyResult 를 반환한다(내부 객체 미유출).

이 파일은 단계 1 범위: 순수 자료구조/상태머신만. StrategyManager·app.py·
스케줄러·기존 주문/체결/apply 흐름은 건드리지 않는다.
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass


# ── 상태 상수 ──────────────────────────────────────────────────
ACCEPTED         = "ACCEPTED"          # 접수(주문 수락), 아직 체결 확인 전
UNFILLED         = "UNFILLED"          # 조회 결과 미체결(대기)
PARTIAL          = "PARTIAL"           # 부분체결
CANCEL_REQUESTED = "CANCEL_REQUESTED"  # 취소 요청됨(예: timeout). ★비-terminal, open 유지
FILLED           = "FILLED"            # 전량체결 (terminal)
REJECTED         = "REJECTED"          # 거부 (terminal)
CANCELED         = "CANCELED"          # 취소 확정 (terminal)

# ★ timeout 은 더 이상 terminal 상태가 아니다. timeout 판단 결과는
#   CANCEL_REQUESTED(비-terminal) 전환으로 표현하고, 브로커가 확인하기 전까지
#   주문은 open 을 유지하며 추가 체결을 계속 반영할 수 있다.
#   최종 terminal 은 브로커 확인 3종(FILLED/CANCELED/REJECTED)만.
STATUSES = frozenset({
    ACCEPTED, UNFILLED, PARTIAL, CANCEL_REQUESTED, FILLED, REJECTED, CANCELED,
})
TERMINAL_STATUSES = frozenset({FILLED, CANCELED, REJECTED})

SIDES = frozenset({"BUY", "SELL"})


@dataclass
class PendingOrder:
    order_no:       str
    market:         str
    code:           str
    name:           str
    side:           str          # "BUY" | "SELL"
    level:          int
    req_qty:        int
    req_price:      float
    applied_qty:       int   = 0
    status:            str   = ACCEPTED
    accepted_ts:       float = 0.0
    last_check_ts:     float = 0.0
    cancel_requested_ts: float = 0.0   # 최초 취소요청 시각(0=미요청). accepted_ts 와 별개.
    is_full:           bool  = False
    using_compound:    float = 0.0

    def remaining_qty(self) -> int:
        """미반영 잔량 (req_qty - applied_qty), 0 하한."""
        return max(0, self.req_qty - self.applied_qty)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class FillApplyResult:
    """
    apply_delta() 반환용 **불변 스냅샷**.
    caller(poll_pending_fills)가 apply_buy/apply_sell 을 구동하는 데 필요한
    '이번 호출의 실제 반영 결과'를 담는다. (레지스트리 내부 객체를 유출하지 않음)

    - applied_delta:  이번 호출에서 실제 흡수된 수량(0이면 no-op)
    - requested_delta:요청된 델타(감사/로그용)
    - clamped:        applied_delta < requested_delta (잔량 한도로 잘렸는가)
    - applied_qty:    반영 후 누적 체결수량
    - remaining_qty:  반영 후 잔량
    - status:         반영 후 상태
    - is_terminal:    반영 후 terminal 여부
    - became_filled:  '이번 호출'로 FILLED 로 전환됐는가(전량 완료 호출에서만 True)
    """
    order_no:        str
    code:            str
    side:            str
    applied_delta:   int
    requested_delta: int
    clamped:         bool
    applied_qty:     int
    remaining_qty:   int
    status:          str
    is_terminal:     bool
    became_filled:   bool


class PendingRegistry:
    """
    미확정 주문 메모리 레지스트리 (order_no 키).

    now_fn: 현재시각(epoch seconds) 반환 콜러블. 미지정 시 time.time.
            테스트에서 결정적 clock 을 주입한다.
    """

    def __init__(self, now_fn=None):
        self._now = now_fn if callable(now_fn) else time.time
        self._orders: dict[str, PendingOrder] = {}
        # 재진입(락 보유 중 다른 락 메서드 호출) 없음이 검증됨 → plain Lock 으로 충분.
        self._lock = threading.Lock()

    # ── 등록 ────────────────────────────────────────────────
    def register(self, order_no, market, code, name, side, level,
                 req_qty, req_price, is_full=False, using_compound=0.0) -> PendingOrder:
        """
        신규 주문 등록. 같은 order_no 가 이미 있으면 **기존 주문을 그대로
        반환**(멱등) — 덮어쓰거나 초기화하지 않는다.
        """
        if not order_no:
            raise ValueError("order_no 필수")
        _side = str(side).upper()
        if _side not in SIDES:
            raise ValueError(f"잘못된 side: {side!r} (BUY|SELL)")
        try:
            _req_qty = int(req_qty)
        except (TypeError, ValueError):
            raise ValueError(f"req_qty 정수 아님: {req_qty!r}")
        if _req_qty <= 0:
            raise ValueError(f"req_qty 는 양수여야 함: {req_qty!r}")
        if req_price is None or float(req_price) < 0:
            raise ValueError(f"req_price 는 0 이상이어야 함: {req_price!r}")

        with self._lock:
            existing = self._orders.get(order_no)
            if existing is not None:
                return existing   # ★ 멱등: 기존 유지
            ts = self._now()
            po = PendingOrder(
                order_no=str(order_no), market=str(market), code=str(code),
                name=str(name), side=_side, level=level,
                req_qty=_req_qty, req_price=float(req_price),
                applied_qty=0, status=ACCEPTED,
                accepted_ts=ts, last_check_ts=ts,
                is_full=bool(is_full), using_compound=float(using_compound or 0.0),
            )
            self._orders[order_no] = po
            return po

    # ── 조회 ────────────────────────────────────────────────
    def get(self, order_no) -> PendingOrder | None:
        with self._lock:
            return self._orders.get(order_no)

    def has_open(self, code, side=None) -> bool:
        """
        해당 code 에 비종결(non-terminal) 주문이 있으면 True.
        side 지정 시 그 방향만, None 이면 양방향.
        """
        _side = str(side).upper() if side is not None else None
        if _side is not None and _side not in SIDES:
            raise ValueError(f"잘못된 side: {side!r}")
        with self._lock:
            for po in self._orders.values():
                if po.code == code and not po.is_terminal():
                    if _side is None or po.side == _side:
                        return True
        return False

    def all_open(self) -> list[PendingOrder]:
        """비종결 주문 목록."""
        with self._lock:
            return [po for po in self._orders.values() if not po.is_terminal()]

    def remaining_qty(self, order_no) -> int:
        with self._lock:
            po = self._orders.get(order_no)
            if po is None:
                raise KeyError(order_no)
            return po.remaining_qty()

    # ── 상태/체결 갱신 ──────────────────────────────────────
    def update_status(self, order_no, status) -> PendingOrder:
        if status not in STATUSES:
            raise ValueError(f"잘못된 status: {status!r}")
        with self._lock:
            po = self._orders.get(order_no)
            if po is None:
                raise KeyError(order_no)
            po.status = status
            po.last_check_ts = self._now()
            return po

    def request_cancel(self, order_no) -> bool:
        """
        취소 필요(예: timeout) 표시 → CANCEL_REQUESTED(비-terminal) 전환.

        반환:
          - True  : '이번 호출'로 최초 CANCEL_REQUESTED 전환됨(=이때만 CancelRequest 생성).
          - False : 이미 CANCEL_REQUESTED 이거나 terminal → 멱등, 상태·시각 미변경.

        멱등 규칙:
          - 최초 전환 시에만 cancel_requested_ts 를 기록(clock).
          - 재요청은 status/cancel_requested_ts/accepted_ts 를 초기화하지 않는다.
          - accepted_ts(주문 접수시각)와 last_check_ts 는 절대 덮어쓰지 않는다.
        """
        with self._lock:
            po = self._orders.get(order_no)
            if po is None:
                raise KeyError(order_no)
            if po.is_terminal():
                return False                      # terminal 은 취소요청 무의미
            if po.status == CANCEL_REQUESTED:
                return False                      # 이미 요청됨 → 멱등, 미변경
            po.status = CANCEL_REQUESTED
            po.cancel_requested_ts = self._now()  # ★ 최초만 기록(accepted/last_check 미변경)
            return True

    def apply_delta(self, order_no, delta_qty) -> FillApplyResult:
        """
        체결 '델타'(누적값이 아닌 신규 체결분)를 applied_qty 에 누적하고
        **불변 FillApplyResult** 를 반환한다. Result 생성과 상태 변경은
        동일 Lock 임계구역에서 처리한다.

        동작 규칙:
          - delta_qty 음수 → ValueError.
          - 존재하지 않는 order_no → KeyError.
          - 이미 terminal(FILLED/CANCELED/REJECTED) → 추가 반영 없음:
            applied_delta=0, became_filled=False (예외 아님, no-op).
          - 요청 delta 가 remaining 보다 크면 remaining 까지만 반영 → clamped=True.
          - 반영 후 remaining==0 이 '이번 호출'로 발생하면 became_filled=True,
            상태 FILLED. 부분이면 PARTIAL.
          - delta_qty==0 → applied_delta=0, 상태·수량 무변화(no-op).
          - clamped == (applied_delta < requested_delta) 로 일관.
        """
        try:
            _dq = int(delta_qty)
        except (TypeError, ValueError):
            raise ValueError(f"delta_qty 정수 아님: {delta_qty!r}")
        if _dq < 0:
            raise ValueError(f"delta_qty 음수 불가: {delta_qty!r}")
        with self._lock:
            po = self._orders.get(order_no)
            if po is None:
                raise KeyError(order_no)

            # 이미 terminal → 추가 반영 금지(no-op Result)
            if po.is_terminal():
                return FillApplyResult(
                    order_no=po.order_no, code=po.code, side=po.side,
                    applied_delta=0, requested_delta=_dq,
                    clamped=(_dq > 0),                 # 요청분이 전혀 반영 안 됨
                    applied_qty=po.applied_qty, remaining_qty=po.remaining_qty(),
                    status=po.status, is_terminal=True, became_filled=False,
                )

            remaining_before = po.remaining_qty()
            applied = min(_dq, remaining_before)       # ★ 잔량까지만 반영(초과 clamp)
            became_filled = False
            if applied > 0:
                po.applied_qty += applied
                po.last_check_ts = self._now()
                if po.remaining_qty() == 0:
                    po.status = FILLED
                    became_filled = True
                elif po.status != CANCEL_REQUESTED:
                    # ★ CANCEL_REQUESTED 는 부분체결이 와도 유지(취소요청 마커 보존).
                    #   전량체결(remaining==0)일 때만 FILLED 로 전환.
                    po.status = PARTIAL
            # applied==0(delta 0) → 상태·수량 무변화

            return FillApplyResult(
                order_no=po.order_no, code=po.code, side=po.side,
                applied_delta=applied, requested_delta=_dq,
                clamped=(applied < _dq),
                applied_qty=po.applied_qty, remaining_qty=po.remaining_qty(),
                status=po.status, is_terminal=po.is_terminal(),
                became_filled=became_filled,
            )

    def mark_terminal(self, order_no, status) -> PendingOrder:
        """FILLED/CANCELED/REJECTED 중 하나로 종결 처리."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"terminal status 아님: {status!r}")
        return self.update_status(order_no, status)

    # ── 정리 ────────────────────────────────────────────────
    def remove(self, order_no) -> bool:
        with self._lock:
            return self._orders.pop(order_no, None) is not None

    def purge_terminal(self) -> int:
        """종결된 주문을 제거하고 제거 개수 반환."""
        with self._lock:
            dead = [k for k, po in self._orders.items() if po.is_terminal()]
            for k in dead:
                del self._orders[k]
            return len(dead)

    def clear(self) -> None:
        with self._lock:
            self._orders.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._orders)

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._orders)
