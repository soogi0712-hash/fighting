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
    동시 실행될 수 있으므로 최소한의 RLock 으로 변이를 보호한다.

이 파일은 단계 1 범위: 순수 자료구조/상태머신만. StrategyManager·app.py·
스케줄러·기존 주문/체결/apply 흐름은 건드리지 않는다.
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass


# ── 상태 상수 ──────────────────────────────────────────────────
ACCEPTED = "ACCEPTED"   # 접수(주문 수락), 아직 체결 확인 전
UNFILLED = "UNFILLED"   # 조회 결과 미체결(대기)
PARTIAL  = "PARTIAL"    # 부분체결
FILLED   = "FILLED"     # 전량체결 (terminal)
REJECTED = "REJECTED"   # 거부 (terminal)
CANCELED = "CANCELED"   # 취소 (terminal)
TIMEOUT  = "TIMEOUT"    # 시간초과 (terminal)

STATUSES = frozenset({ACCEPTED, UNFILLED, PARTIAL, FILLED, REJECTED, CANCELED, TIMEOUT})
TERMINAL_STATUSES = frozenset({FILLED, REJECTED, CANCELED, TIMEOUT})

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
    applied_qty:    int   = 0
    status:         str   = ACCEPTED
    accepted_ts:    float = 0.0
    last_check_ts:  float = 0.0
    is_full:        bool  = False
    using_compound: float = 0.0

    def remaining_qty(self) -> int:
        """미반영 잔량 (req_qty - applied_qty), 0 하한."""
        return max(0, self.req_qty - self.applied_qty)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class PendingRegistry:
    """
    미확정 주문 메모리 레지스트리 (order_no 키).

    now_fn: 현재시각(epoch seconds) 반환 콜러블. 미지정 시 time.time.
            테스트에서 결정적 clock 을 주입한다.
    """

    def __init__(self, now_fn=None):
        self._now = now_fn if callable(now_fn) else time.time
        self._orders: dict[str, PendingOrder] = {}
        self._lock = threading.RLock()

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

    def apply_delta(self, order_no, delta_qty) -> PendingOrder:
        """
        체결 '델타'를 applied_qty 에 누적. (누적값이 아니라 신규 체결분)
          - delta_qty 음수 금지.
          - applied_qty 는 req_qty 를 초과하지 않도록 clamp(초과분 무시).
          - 이미 terminal 인 주문에는 적용 불가(방어).
          - 적용 후 remaining==0 이면 FILLED, 그 외 applied>0 이면 PARTIAL 로 전환.
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
            if po.is_terminal():
                raise ValueError(
                    f"terminal 주문({po.status})에는 체결을 적용할 수 없음: {order_no}")
            new_applied = po.applied_qty + _dq
            if new_applied > po.req_qty:
                new_applied = po.req_qty     # ★ 초과 방지 clamp
            po.applied_qty = max(0, new_applied)
            po.last_check_ts = self._now()
            if po.remaining_qty() == 0:
                po.status = FILLED
            elif po.applied_qty > 0:
                po.status = PARTIAL
            return po

    def mark_terminal(self, order_no, status) -> PendingOrder:
        """FILLED/REJECTED/CANCELED/TIMEOUT 중 하나로 종결 처리."""
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
