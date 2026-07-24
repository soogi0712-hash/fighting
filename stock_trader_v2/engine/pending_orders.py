"""
engine/pending_orders.py — 미확정 주문(pending) 메모리 레지스트리 (GAP2)

목적:
  주문 '접수(rt_cd==0)'와 '실제 체결'을 분리하기 위한,
  미확정 주문의 작업상태를 담는 메모리 레지스트리.
  GAP2 stock_trader/strategies/pending_orders.py 에서 이식.
  import 경로만 수정. 로직 무변경.

원칙:
  - KIS 체결 상태가 최종 진실(SoT). 이 레지스트리는 캐시.
  - apply는 반드시 '체결 델타'로만 누적(applied_qty 증가).
  - order_no를 멱등키로 사용. 같은 order_no 재등록은 기존 유지.
  - 최종 terminal: FILLED / CANCELED / REJECTED 3종.
  - CANCEL_REQUESTED: 비-terminal, open 유지 (취소 확정 전까지).
  - atomic save_to / load_from 로 영속화 지원.
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass


# ── 상태 상수 ──────────────────────────────────────────────────
ACCEPTED         = "ACCEPTED"
UNFILLED         = "UNFILLED"
PARTIAL          = "PARTIAL"
CANCEL_REQUESTED = "CANCEL_REQUESTED"
FILLED           = "FILLED"
REJECTED         = "REJECTED"
CANCELED         = "CANCELED"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"   # 상태 불명(API 연속 실패 등)

STATUSES = frozenset({
    ACCEPTED, UNFILLED, PARTIAL, CANCEL_REQUESTED,
    FILLED, REJECTED, CANCELED, RECOVERY_REQUIRED,
})
TERMINAL_STATUSES = frozenset({FILLED, CANCELED, REJECTED})

SIDES = frozenset({"BUY", "SELL"})


@dataclass
class PendingOrder:
    order_no:            str
    market:              str
    code:                str
    name:                str
    side:                str          # "BUY" | "SELL"
    level:               int
    req_qty:             int
    req_price:           float
    applied_qty:         int   = 0
    status:              str   = ACCEPTED
    accepted_ts:         float = 0.0
    last_check_ts:       float = 0.0
    cancel_requested_ts: float = 0.0
    is_full:             bool  = False
    using_compound:      float = 0.0

    def remaining_qty(self) -> int:
        return max(0, self.req_qty - self.applied_qty)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class FillApplyResult:
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
    """

    def __init__(self, now_fn=None):
        self._now = now_fn if callable(now_fn) else time.time
        self._orders: dict[str, PendingOrder] = {}
        self._lock = threading.Lock()

    # ── 등록 ────────────────────────────────────────────────
    def register(self, order_no, market, code, name, side, level,
                 req_qty, req_price, is_full=False, using_compound=0.0) -> PendingOrder:
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
                return existing   # 멱등: 기존 유지
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
        취소 필요 표시 → CANCEL_REQUESTED(비-terminal) 전환.
        반환: True=이번 호출로 최초 전환, False=이미 요청됨 또는 terminal.
        """
        with self._lock:
            po = self._orders.get(order_no)
            if po is None:
                raise KeyError(order_no)
            if po.is_terminal():
                return False
            if po.status == CANCEL_REQUESTED:
                return False
            po.status = CANCEL_REQUESTED
            po.cancel_requested_ts = self._now()
            return True

    def mark_recovery(self, order_no) -> bool:
        """
        상태 불명(API 연속 실패 등) → RECOVERY_REQUIRED 전환.
        비-terminal 상태에서만 전환. terminal이면 False 반환.
        """
        with self._lock:
            po = self._orders.get(order_no)
            if po is None:
                raise KeyError(order_no)
            if po.is_terminal():
                return False
            if po.status == RECOVERY_REQUIRED:
                return False
            po.status = RECOVERY_REQUIRED
            po.last_check_ts = self._now()
            return True

    def apply_delta(self, order_no, delta_qty) -> FillApplyResult:
        """
        체결 '델타'(신규 체결분)를 applied_qty에 누적하고
        불변 FillApplyResult를 반환한다.
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
                return FillApplyResult(
                    order_no=po.order_no, code=po.code, side=po.side,
                    applied_delta=0, requested_delta=_dq,
                    clamped=(_dq > 0),
                    applied_qty=po.applied_qty, remaining_qty=po.remaining_qty(),
                    status=po.status, is_terminal=True, became_filled=False,
                )

            remaining_before = po.remaining_qty()
            applied = min(_dq, remaining_before)
            became_filled = False
            if applied > 0:
                po.applied_qty += applied
                po.last_check_ts = self._now()
                if po.remaining_qty() == 0:
                    po.status = FILLED
                    became_filled = True
                elif po.status not in (CANCEL_REQUESTED, RECOVERY_REQUIRED):
                    po.status = PARTIAL

            return FillApplyResult(
                order_no=po.order_no, code=po.code, side=po.side,
                applied_delta=applied, requested_delta=_dq,
                clamped=(applied < _dq),
                applied_qty=po.applied_qty, remaining_qty=po.remaining_qty(),
                status=po.status, is_terminal=po.is_terminal(),
                became_filled=became_filled,
            )

    def mark_terminal(self, order_no, status) -> bool:
        """
        FILLED/CANCELED/REJECTED로 원자적 종결 처리.
        반환: True=이번 호출로 최초 terminal 전이, False=이미 terminal.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"terminal status 아님: {status!r}")
        with self._lock:
            po = self._orders.get(order_no)
            if po is None:
                raise KeyError(order_no)
            if po.is_terminal():
                return False
            po.status = status
            po.last_check_ts = self._now()
            return True

    # ── 정리 ────────────────────────────────────────────────
    def remove(self, order_no) -> bool:
        with self._lock:
            return self._orders.pop(order_no, None) is not None

    def purge_terminal(self) -> int:
        with self._lock:
            dead = [k for k, po in self._orders.items() if po.is_terminal()]
            for k in dead:
                del self._orders[k]
            return len(dead)

    def clear(self) -> None:
        with self._lock:
            self._orders.clear()

    # ── 영속(재시작 복구용) ─────────────────────────────────
    def save_to(self, path) -> None:
        """전체 주문을 JSON으로 저장. atomic write (tmp → replace)."""
        import json, os
        with self._lock:
            data = {k: vars(v).copy() for k, v in self._orders.items()}
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)   # 원자적 교체

    def load_from(self, path) -> int:
        """JSON에서 주문 복원. 반환: 로드된 주문 수. 파일 없으면 0."""
        import json, os
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        n = 0
        with self._lock:
            for k, d in (raw or {}).items():
                try:
                    self._orders[k] = PendingOrder(**d)
                    n += 1
                except Exception:
                    continue
        return n

    def __len__(self) -> int:
        with self._lock:
            return len(self._orders)

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._orders)
