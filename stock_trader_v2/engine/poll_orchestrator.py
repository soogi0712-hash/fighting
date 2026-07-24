"""
engine/poll_orchestrator.py — 미확정 주문 폴링 오케스트레이터 (GAP2)

역할:
  PendingRegistry + FillSource + OrderStateSource 를 조합해,
  불변 이벤트(PollResult)를 생성한다.
  apply_buy/apply_sell·pnl·취소 발주는 하지 않는다(이벤트만 반환).

GAP2 stock_trader/strategies/poll_orchestrator.py 에서 이식.
import 경로만 수정. 로직 무변경.

오류 정책(확장):
  - fill_source.get_fills 실패 → 해당 그룹 skip, PollResult.fill_errors 기록
  - order_state_source.get_states 실패 → 해당 시장 상태 skip
  - 오류만으로 pending 삭제 금지, 체결 간주 금지
  - timeout(15초)만으로 CANCEL 단정 금지 → CancelRequest 이벤트만 방출
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from engine.pending_orders import (
    PendingRegistry, CANCELED, REJECTED, FILLED, CANCEL_REQUESTED
)

_LOG = logging.getLogger("poll_orchestrator")

OPEN    = "OPEN"
PARTIAL = "PARTIAL"

RECONCILE_BROKER_FILLED_REGISTRY_INCOMPLETE = "BROKER_FILLED_REGISTRY_INCOMPLETE"


# ══════════════════════════════════════════════════════════════
# OrderState / OrderStateSource
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class OrderState:
    order_no:      str
    status:        str            # OPEN|PARTIAL|FILLED|CANCELED|REJECTED
    filled_qty:    int | None
    remaining_qty: int | None
    updated_at:    str
    raw_status:    str = ""


class OrderStateSource:
    def get_states(self, market: str, order_nos: tuple) -> dict:
        raise NotImplementedError


class NoOrderStateSource(OrderStateSource):
    """취소/거부 확정 소스 미확보 시 사용. 빈 상태 반환."""
    def get_states(self, market, order_nos):
        return {}


class MockOrderStateSource(OrderStateSource):
    def __init__(self):
        self._states: dict[str, OrderState] = {}
        self._fail_markets: set[str] = set()
        self.calls: list[tuple] = []

    def set(self, state: OrderState) -> "MockOrderStateSource":
        self._states[state.order_no] = state
        return self

    def fail_on(self, market: str) -> "MockOrderStateSource":
        self._fail_markets.add(market)
        return self

    def get_states(self, market: str, order_nos: tuple) -> dict:
        self.calls.append((market, tuple(order_nos)))
        if market in self._fail_markets:
            raise RuntimeError(f"OrderStateSource 실패(mock): market={market}")
        return {o: self._states[o] for o in order_nos if o in self._states}


# ══════════════════════════════════════════════════════════════
# 이벤트 4종 + PollResult
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AppliedFillEvent:
    """실제 체결 델타 → 포지션·손익 변경용."""
    order_no:       str
    market:         str
    code:           str
    name:           str
    side:           str
    level:          int
    applied_qty:    int
    price:          float
    remaining_qty:  int
    using_compound: float
    is_full:        bool
    became_filled:  bool


@dataclass(frozen=True)
class OrderStatusEvent:
    """브로커 확정 종결 통지(CANCELED/REJECTED, 최초 terminal 전이에서만)."""
    order_no:   str
    market:     str
    code:       str
    side:       str
    status:     str
    raw_status: str
    updated_at: str


@dataclass(frozen=True)
class CancelRequest:
    """timeout → 취소 요청(아직 terminal 아님). 최초 request_cancel에서만."""
    order_no:      str
    market:        str
    code:          str
    side:          str
    remaining_qty: int
    age_sec:       float
    reason:        str = "timeout"


@dataclass(frozen=True)
class ReconcileIssue:
    """브로커 FILLED인데 registry.applied_qty < req_qty인 불일치."""
    order_no:                str
    code:                    str
    market:                  str
    side:                    str
    broker_status:           str
    broker_filled_qty:       int | None
    broker_remaining_qty:    int | None
    registry_applied_qty:    int
    registry_remaining_qty:  int
    raw_status:              str
    updated_at:              str
    reason: str = RECONCILE_BROKER_FILLED_REGISTRY_INCOMPLETE


@dataclass
class PollResult:
    fills:            tuple = ()
    statuses:         tuple = ()
    cancel_requests:  tuple = ()
    reconcile_issues: tuple = ()
    fill_errors:      tuple = ()   # (market, code, side, exc_str)


# ══════════════════════════════════════════════════════════════
# poll_pending_fills
# ══════════════════════════════════════════════════════════════
def poll_pending_fills(fill_source, order_state_source, registry: PendingRegistry,
                       now: float, timeout_sec: float,
                       logger=None) -> PollResult:
    """
    한 주기 폴링. 실행 순서:
      1) all_open() 스냅샷
      2) open 없으면 빈 PollResult 즉시 반환
      3) (market,code,side) 그룹당 fills 1회 조회 → Fill.order_no 매칭 → apply_delta
      4) 시장별 OrderStateSource.get_states 조회
      5) 공식 CANCELED/REJECTED 만 mark_terminal
      6) mark_terminal True(최초 전이)에서만 OrderStatusEvent
      7) broker FILLED & registry 미완결 → ReconcileIssue
      8) 그 후에도 open인 주문만 timeout 검사
      9) request_cancel True에서만 CancelRequest
     10) purge_terminal 호출 안 함

    오류 정책:
      - fill_source.get_fills 실패 → 그룹 skip, fill_errors 기록. poll 전체 중단 금지.
      - order_state_source.get_states 실패 → 해당 시장 skip. poll 전체 중단 금지.
      - 오류만으로 pending 삭제 금지.
    """
    log = logger or _LOG

    open_orders = registry.all_open()
    if not open_orders:
        return PollResult()

    fills_ev:     list = []
    statuses_ev:  list = []
    cancels_ev:   list = []
    reconcile_ev: list = []
    fill_errors:  list = []

    # 3) (market, code, side) 그룹당 get_fills 1회
    group_keys = list(dict.fromkeys((po.market, po.code, po.side) for po in open_orders))
    for (market, code, side) in group_keys:
        try:
            fills = fill_source.get_fills(market, code, side)
        except Exception as ex:
            log.warning("fill_source.get_fills 실패 market=%s code=%s side=%s: %r",
                        market, code, side, ex)
            fill_errors.append((market, code, side, str(ex)))
            continue   # 이 그룹 skip. pending 삭제 금지.
        for f in (fills or []):
            po = registry.get(f.order_no)
            if po is None:
                continue
            res = registry.apply_delta(f.order_no, f.qty)
            if res.applied_delta > 0:
                fills_ev.append(AppliedFillEvent(
                    order_no=po.order_no, market=po.market, code=po.code,
                    name=po.name, side=po.side, level=po.level,
                    applied_qty=res.applied_delta, price=f.price,
                    remaining_qty=res.remaining_qty,
                    using_compound=po.using_compound, is_full=po.is_full,
                    became_filled=res.became_filled,
                ))

    # 4~7) 시장별 상태 조회
    by_market: dict = {}
    for po in open_orders:
        by_market.setdefault(po.market, []).append(po)

    for market, orders in by_market.items():
        order_nos = tuple(po.order_no for po in orders)
        try:
            states = order_state_source.get_states(market, order_nos)
        except Exception as ex:
            log.warning("order_state_source.get_states 실패 market=%s: %r", market, ex)
            continue
        for po in orders:
            st = states.get(po.order_no)
            if st is None:
                continue
            cur = registry.get(po.order_no)
            if cur is None or cur.is_terminal():
                continue
            if st.status in (CANCELED, REJECTED):
                if registry.mark_terminal(po.order_no, st.status):
                    statuses_ev.append(OrderStatusEvent(
                        order_no=po.order_no, market=po.market, code=po.code,
                        side=po.side, status=st.status,
                        raw_status=st.raw_status, updated_at=st.updated_at,
                    ))
            elif st.status == FILLED:
                if cur.applied_qty < cur.req_qty:
                    reconcile_ev.append(ReconcileIssue(
                        order_no=po.order_no, code=po.code, market=po.market,
                        side=po.side, broker_status=st.status,
                        broker_filled_qty=st.filled_qty,
                        broker_remaining_qty=st.remaining_qty,
                        registry_applied_qty=cur.applied_qty,
                        registry_remaining_qty=cur.remaining_qty(),
                        raw_status=st.raw_status, updated_at=st.updated_at,
                    ))

    # 8~9) 그 후에도 open인 주문만 timeout 검사
    for po in open_orders:
        cur = registry.get(po.order_no)
        if cur is None or cur.is_terminal():
            continue
        if cur.status == CANCEL_REQUESTED:
            continue   # 이미 취소 요청됨
        age = now - cur.accepted_ts
        if age >= timeout_sec:
            if registry.request_cancel(po.order_no):
                cancels_ev.append(CancelRequest(
                    order_no=po.order_no, market=po.market, code=po.code,
                    side=po.side, remaining_qty=cur.remaining_qty(),
                    age_sec=age, reason="timeout",
                ))

    return PollResult(
        fills=tuple(fills_ev),
        statuses=tuple(statuses_ev),
        cancel_requests=tuple(cancels_ev),
        reconcile_issues=tuple(reconcile_ev),
        fill_errors=tuple(fill_errors),
    )
