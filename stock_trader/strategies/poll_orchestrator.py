"""
poll_orchestrator.py — 미확정 주문 폴링 오케스트레이터 (갭2 S3)

역할:
  PendingRegistry(미확정 주문 상태) + FillSource(체결 델타·가격) +
  OrderStateSource(생애주기 상태)를 조합해, StrategyManager 가 소비할
  불변 이벤트(PollResult)를 생성한다. apply_buy/apply_sell·pnl·취소 발주는
  하지 않는다(이벤트만 반환).

원칙(설계 확정):
  - KIS 체결/상태가 최종 진실. Registry 는 캐시. poll 은 조인 계층.
  - poll 은 Registry/FillSource/OrderStateSource 의 '공개 표면'만 사용한다.
    (Registry 내부 _orders/_lock 미접근.)
  - poll 자체는 LIVE_ORDER_ENABLED 를 모른다. 네트워크 게이트는 각 Kis
    구현체(KisFillSource / KisOrderStateSource) 내부 책임.
  - FILLED 는 AppliedFillEvent.became_filled 로만 처리(상태소스로 terminal 금지).
  - CANCELED/REJECTED 는 '공식 상태 + 최초 terminal 전이'에서만 이벤트.
  - open 목록 소멸/브로커 수량으로 CANCELED 추론·가짜 fill 금지.
  - purge_terminal 호출 안 함(정리는 상위 오케스트레이터 책임).

이 파일은 ledger/fills.py 와 strategies/pending_orders.py 를 '읽기 import'만
하며 수정하지 않는다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

# 읽기 import — Registry 상태 상수(수정 없음)
from strategies.pending_orders import CANCELED, REJECTED, FILLED

_LOG = logging.getLogger("poll_orchestrator")

# OrderStateSource 가 돌려주는 정규화 status 어휘(참고용). CANCELED/REJECTED/FILLED
# 는 Registry 어휘를 재사용. OPEN/PARTIAL 은 poll 이 별도 조치하지 않으므로 문자열만.
OPEN    = "OPEN"
PARTIAL = "PARTIAL"

RECONCILE_BROKER_FILLED_REGISTRY_INCOMPLETE = "BROKER_FILLED_REGISTRY_INCOMPLETE"


# ══════════════════════════════════════════════════════════════
# OrderState / OrderStateSource (S2.5 확정 인터페이스 + Mock)
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class OrderState:
    """
    브로커 주문 생애주기 상태 스냅샷(가격 없음).
    filled_qty/remaining_qty 는 브로커 미신뢰/미제공 시 None(시장별 응답차 흡수).
    """
    order_no:      str
    status:        str            # OPEN|PARTIAL|FILLED|CANCELED|REJECTED (정규화)
    filled_qty:    int | None
    remaining_qty: int | None
    updated_at:    str
    raw_status:    str = ""


class OrderStateSource:
    def get_states(self, market: str, order_nos: tuple) -> dict:
        """order_no → OrderState. 조회할 order_no 를 명시적으로 받는다."""
        raise NotImplementedError


class MockOrderStateSource(OrderStateSource):
    """테스트용. 순수 메모리, 네트워크 없음. 특정 market 예외 주입 가능."""
    def __init__(self):
        self._states: dict[str, OrderState] = {}
        self._fail_markets: set[str] = set()
        self.calls: list[tuple] = []   # (market, order_nos) 호출 기록(검증용)

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
# 이벤트 4종 (전부 불변) + PollResult
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AppliedFillEvent:
    """실제 체결 델타 → 포지션·손익 변경용. 값은 PendingOrder/FillApplyResult/Fill 에서만."""
    order_no:       str
    market:         str
    code:           str
    name:           str      # PendingOrder.name
    side:           str
    level:          int      # PendingOrder.level
    applied_qty:    int      # FillApplyResult.applied_delta (실제 반영 델타)
    price:          float    # Fill.price (이번 delta 평균 체결가)
    remaining_qty:  int      # FillApplyResult.remaining_qty
    using_compound: float    # PendingOrder.using_compound
    is_full:        bool     # PendingOrder.is_full (주문 의도; 완료는 became_filled)
    became_filled:  bool     # FillApplyResult.became_filled


@dataclass(frozen=True)
class OrderStatusEvent:
    """브로커 확정 종결 통지(CANCELED/REJECTED, 최초 terminal 전이에서만)."""
    order_no:    str
    market:      str
    code:        str
    side:        str
    status:      str     # CANCELED | REJECTED
    raw_status:  str
    updated_at:  str


@dataclass(frozen=True)
class CancelRequest:
    """timeout → 취소 요청(아직 terminal 아님). 최초 request_cancel 에서만."""
    order_no:      str
    market:        str
    code:          str
    side:          str
    remaining_qty: int
    age_sec:       float
    reason:        str = "timeout"


@dataclass(frozen=True)
class ReconcileIssue:
    """브로커 status=FILLED 인데 registry.applied_qty < req_qty 인 불일치."""
    order_no:              str
    code:                  str
    market:                str
    side:                  str
    broker_status:         str
    broker_filled_qty:     int | None
    broker_remaining_qty:  int | None
    registry_applied_qty:  int
    registry_remaining_qty: int
    raw_status:            str
    updated_at:            str
    reason:                str = RECONCILE_BROKER_FILLED_REGISTRY_INCOMPLETE


@dataclass(frozen=True)
class PollResult:
    fills:            tuple = ()
    statuses:         tuple = ()
    cancel_requests:  tuple = ()
    reconcile_issues: tuple = ()


# ══════════════════════════════════════════════════════════════
# poll_pending_fills
# ══════════════════════════════════════════════════════════════
def poll_pending_fills(fill_source, order_state_source, registry,
                       now: float, timeout_sec: float,
                       logger=None) -> PollResult:
    """
    한 주기 폴링. 실행 순서(확정):
      1) all_open() 스냅샷
      2) open 없으면 빈 PollResult 즉시 반환(FillSource/OrderStateSource 미호출)
      3) (market,code,side) 그룹당 fills 1회 조회 → Fill.order_no 로 주문 매칭
         → apply_delta → AppliedFillEvent (Registry 에 없는 order_no 무시)
      4) 시장별 OrderStateSource.get_states 조회
      5) 공식 CANCELED/REJECTED 만 mark_terminal
      6) mark_terminal True(최초 전이)에서만 OrderStatusEvent
      7) broker FILLED & registry.applied_qty<req_qty → ReconcileIssue(terminal/가짜fill 금지)
      8) 그 후에도 open 인 주문만 timeout 검사
      9) request_cancel True 에서만 CancelRequest
     10) purge_terminal 호출 안 함
    """
    log = logger or _LOG

    # 1~2) 스냅샷 + 빈 검사
    open_orders = registry.all_open()
    if not open_orders:
        return PollResult()

    fills_ev:     list = []
    statuses_ev:  list = []
    cancels_ev:   list = []
    reconcile_ev: list = []

    # 3) (market, code, side) 그룹당 get_fills 1회 → Fill 기준 귀속
    #    KisFillSource.get_fills 는 order_hint 를 쓰지 않고 code+side 전체의 Fill 을
    #    각자 odno 로 키잉해 반환한다. 따라서 폴링 주문(po)에 강제 적용하지 않고,
    #    반환된 Fill.order_no 로 PendingOrder 를 찾아 그 주문에 적용한다.
    #    → 동일 종목 다중 주문 정확 귀속 + tracker key(odno)와 registry key 일치
    #    → 그룹당 1회 호출로 계좌 전체조회 중복 감소.
    group_keys = list(dict.fromkeys((po.market, po.code, po.side) for po in open_orders))
    for (market, code, side) in group_keys:
        try:
            fills = fill_source.get_fills(market, code, side)
        except Exception as ex:   # 한 그룹의 체결조회 실패가 poll 전체를 죽이지 않음
            log.warning("fill_source.get_fills 실패 market=%s code=%s side=%s: %r",
                        market, code, side, ex)
            continue
        for f in (fills or []):
            po = registry.get(f.order_no)      # ★ Fill 기준 매칭
            if po is None:
                continue                        # Registry 에 없는 order_no 무시
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

    # 4) 시장별 그룹화
    by_market: dict = {}
    for po in open_orders:
        by_market.setdefault(po.market, []).append(po)

    # 4~7) 시장별 상태 조회 및 적용
    for market, orders in by_market.items():
        order_nos = tuple(po.order_no for po in orders)
        try:
            states = order_state_source.get_states(market, order_nos)
        except Exception as ex:
            # 오류 정책: 이미 적용된 fills 유지, 해당 시장 상태처리만 건너뜀,
            #           timeout 은 계속(아래 8단계). 오류는 숨기지 않고 로깅.
            log.warning("order_state_source.get_states 실패 market=%s: %r", market, ex)
            continue
        for po in orders:
            st = states.get(po.order_no)
            if st is None:
                continue   # 상태 미제공 → 소멸/추론 금지(아무 것도 안 함)
            cur = registry.get(po.order_no)
            if cur is None or cur.is_terminal():
                continue
            if st.status in (CANCELED, REJECTED):
                # 5~6) 공식 상태 + 최초 terminal 전이에서만 이벤트
                if registry.mark_terminal(po.order_no, st.status):
                    statuses_ev.append(OrderStatusEvent(
                        order_no=po.order_no, market=po.market, code=po.code,
                        side=po.side, status=st.status,
                        raw_status=st.raw_status, updated_at=st.updated_at,
                    ))
            elif st.status == FILLED:
                # 7) FILLED 는 상태소스만으로 terminal 처리 금지.
                #    apply_delta 로 이미 채워졌으면 cur.is_terminal()==True 라 위에서 skip.
                #    여기 도달 = registry 미완결 → ReconcileIssue.
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
            # OPEN/PARTIAL 등 → 조치 없음

    # 8~9) 그 후에도 open 인 주문만 timeout 검사
    for po in open_orders:
        cur = registry.get(po.order_no)
        if cur is None or cur.is_terminal():
            continue
        age = now - cur.accepted_ts
        if age >= timeout_sec:
            if registry.request_cancel(po.order_no):   # 최초 전환에서만 True
                cancels_ev.append(CancelRequest(
                    order_no=po.order_no, market=po.market, code=po.code,
                    side=po.side, remaining_qty=cur.remaining_qty(),
                    age_sec=age, reason="timeout",
                ))

    # 10) purge_terminal 호출 안 함
    return PollResult(
        fills=tuple(fills_ev),
        statuses=tuple(statuses_ev),
        cancel_requests=tuple(cancels_ev),
        reconcile_issues=tuple(reconcile_ev),
    )
