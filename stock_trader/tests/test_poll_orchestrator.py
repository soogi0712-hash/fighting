"""
S3 검증: poll_pending_fills() + PollResult 4종
==============================================

MockFillSource + MockOrderStateSource 로 poll 을 단위 검증(네트워크 없음).
apply_buy/apply_sell·StrategyManager·KIS 무관.

규칙 검증:
  - CANCELED/REJECTED 최초 전이만 OrderStatusEvent
  - FILLED 상태만으로 terminal 처리 금지(became_filled 로만)
  - broker 수량으로 가짜 fill 금지 / open 소멸로 CANCELED 추론 금지
  - purge_terminal 미호출
  - poll 은 LIVE 미인지
  - 시장별(PendingOrder.market) 그룹화
  - OrderStateSource 시장별 예외 격리(fills 유지·타 시장 정상·timeout 계속)
"""
import os
import sys
import threading

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

import pytest
from ledger.fills import Fill, MockFillSource
from strategies.pending_orders import (
    PendingRegistry, CANCELED, REJECTED, FILLED, PARTIAL, CANCEL_REQUESTED,
)
from strategies.poll_orchestrator import (
    poll_pending_fills, PollResult, AppliedFillEvent, OrderStatusEvent,
    CancelRequest, ReconcileIssue, OrderState, MockOrderStateSource,
    OPEN,
)


def _reg(now_fn=None):
    return PendingRegistry(now_fn=now_fn)


def _reg_order(reg, order_no="O1", market="KR", code="005930", name="삼성",
               side="BUY", level=1, qty=100, price=70000):
    return reg.register(order_no, market, code, name, side, level, qty, price)


class _SpyFillSource(MockFillSource):
    def __init__(self):
        super().__init__()
        self.called = 0

    def get_fills(self, market, code, side, requested_qty=None,
                  requested_price=None, order_hint=None, ts=None):
        self.called += 1
        return super().get_fills(market, code, side, requested_qty,
                                 requested_price, order_hint, ts)


# ── 1. 빈/terminal-only registry → 소스 미호출, 빈 PollResult ──
def test_empty_registry_no_source_calls():
    reg = _reg()
    fs = _SpyFillSource()
    ss = MockOrderStateSource()
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    assert isinstance(r, PollResult)
    assert r == PollResult()
    assert fs.called == 0
    assert ss.calls == []

    # terminal 만 남은 경우도 동일
    _reg_order(reg, order_no="T", qty=10)
    reg.apply_delta("T", 10)            # FILLED(terminal)
    fs2 = _SpyFillSource(); ss2 = MockOrderStateSource()
    r2 = poll_pending_fills(fs2, ss2, reg, now=1000.0, timeout_sec=15)
    assert r2 == PollResult()
    assert fs2.called == 0 and ss2.calls == []


# ── 2. 전량체결 → AppliedFillEvent(became_filled), status 이벤트 없음 ──
def test_full_fill_emits_applied_fill_only():
    reg = _reg()
    _reg_order(reg, order_no="O1", qty=100, price=70000)
    fs = MockFillSource().add("KR", "005930", "BUY",
                              [Fill(order_no="O1", qty=100, price=70050)])
    ss = MockOrderStateSource()
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    assert len(r.fills) == 1
    ev = r.fills[0]
    assert isinstance(ev, AppliedFillEvent)
    assert ev.applied_qty == 100 and ev.price == 70050
    assert ev.became_filled is True and ev.remaining_qty == 0
    assert ev.name == "삼성" and ev.level == 1
    assert r.statuses == ()            # FILLED 는 status 이벤트로 안 만듦
    assert reg.get("O1").status == FILLED


# ── 3. 부분체결 → became_filled=False, 주문 open 유지 ──────────
def test_partial_fill_keeps_open():
    reg = _reg()
    _reg_order(reg, order_no="O1", qty=100)
    fs = MockFillSource().add("KR", "005930", "BUY",
                              [Fill(order_no="O1", qty=40, price=70000)])
    ss = MockOrderStateSource()
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    assert len(r.fills) == 1
    assert r.fills[0].became_filled is False
    assert r.fills[0].remaining_qty == 60
    assert reg.get("O1").status == PARTIAL


# ── 4. 공식 CANCELED → mark_terminal True → OrderStatusEvent 1건 ──
def test_official_canceled_emits_status_event():
    reg = _reg()
    _reg_order(reg, order_no="O1", qty=100)
    fs = MockFillSource()          # 체결 없음
    ss = MockOrderStateSource().set(
        OrderState(order_no="O1", status=CANCELED, filled_qty=0,
                   remaining_qty=100, updated_at="0900", raw_status="취소"))
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    assert len(r.statuses) == 1
    ev = r.statuses[0]
    assert isinstance(ev, OrderStatusEvent)
    assert ev.status == CANCELED and ev.raw_status == "취소"
    assert reg.get("O1").status == CANCELED


# ── 5. CANCELED 재폴링 → 중복 OrderStatusEvent 없음 ───────────
def test_canceled_second_poll_no_duplicate():
    reg = _reg()
    _reg_order(reg, order_no="O1", qty=100)
    fs = MockFillSource()
    ss = MockOrderStateSource().set(
        OrderState(order_no="O1", status=CANCELED, filled_qty=0,
                   remaining_qty=100, updated_at="0900", raw_status="취소"))
    r1 = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    assert len(r1.statuses) == 1
    # 두 번째 폴: 이미 terminal → all_open 에서 빠짐 → 아무 이벤트 없음
    r2 = poll_pending_fills(fs, ss, reg, now=1001.0, timeout_sec=15)
    assert r2 == PollResult()


# ── 6. 공식 REJECTED → OrderStatusEvent(REJECTED) ─────────────
def test_official_rejected_emits_status_event():
    reg = _reg()
    _reg_order(reg, order_no="O1", qty=100)
    fs = MockFillSource()
    ss = MockOrderStateSource().set(
        OrderState(order_no="O1", status=REJECTED, filled_qty=0,
                   remaining_qty=100, updated_at="0901", raw_status="거부"))
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    assert len(r.statuses) == 1
    assert r.statuses[0].status == REJECTED
    assert reg.get("O1").status == REJECTED


# ── 7. broker FILLED & registry 미완결 → ReconcileIssue, terminal/apply 없음 ──
def test_broker_filled_registry_incomplete_reconcile():
    reg = _reg()
    _reg_order(reg, order_no="O1", qty=100)
    reg.apply_delta("O1", 40)          # registry 40 만 반영(미완결)
    fs = MockFillSource()              # 이번 폴엔 새 체결 없음
    ss = MockOrderStateSource().set(
        OrderState(order_no="O1", status=FILLED, filled_qty=100,
                   remaining_qty=0, updated_at="0902", raw_status="체결"))
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    assert r.fills == ()               # 가짜 fill 없음
    assert len(r.reconcile_issues) == 1
    iss = r.reconcile_issues[0]
    assert isinstance(iss, ReconcileIssue)
    assert iss.reason == "BROKER_FILLED_REGISTRY_INCOMPLETE"
    assert iss.broker_filled_qty == 100 and iss.registry_applied_qty == 40
    assert iss.registry_remaining_qty == 60
    # terminal 처리 안 됨 → 여전히 open(PARTIAL)
    assert reg.get("O1").status == PARTIAL
    assert reg.get("O1").is_terminal() is False


# ── 8. fills 먼저 반영 후 CANCELED → 체결분 유지 + terminal ───
def test_fill_then_cancel_keeps_fill_and_terminates():
    reg = _reg()
    _reg_order(reg, order_no="O1", qty=100)
    fs = MockFillSource().add("KR", "005930", "BUY",
                              [Fill(order_no="O1", qty=30, price=70000)])
    ss = MockOrderStateSource().set(
        OrderState(order_no="O1", status=CANCELED, filled_qty=30,
                   remaining_qty=70, updated_at="0903", raw_status="취소"))
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    assert len(r.fills) == 1 and r.fills[0].applied_qty == 30
    assert len(r.statuses) == 1 and r.statuses[0].status == CANCELED
    po = reg.get("O1")
    assert po.applied_qty == 30        # 체결분 유지
    assert po.status == CANCELED       # terminal


# ── 9. timeout → open 주문만 request_cancel True → CancelRequest ──
def test_timeout_emits_cancel_request_once():
    fake = {"t": 1000.0}
    reg = _reg(now_fn=lambda: fake["t"])
    _reg_order(reg, order_no="O1", qty=100)        # accepted_ts=1000
    fs = MockFillSource()
    ss = MockOrderStateSource()                    # 상태 없음
    # 아직 timeout 전
    r0 = poll_pending_fills(fs, ss, reg, now=1010.0, timeout_sec=15)
    assert r0.cancel_requests == ()
    # timeout 도달
    r1 = poll_pending_fills(fs, ss, reg, now=1016.0, timeout_sec=15)
    assert len(r1.cancel_requests) == 1
    cr = r1.cancel_requests[0]
    assert isinstance(cr, CancelRequest)
    assert cr.reason == "timeout" and cr.remaining_qty == 100
    assert reg.get("O1").status == CANCEL_REQUESTED
    # 재폴: 이미 CANCEL_REQUESTED → 중복 CancelRequest 없음
    r2 = poll_pending_fills(fs, ss, reg, now=1030.0, timeout_sec=15)
    assert r2.cancel_requests == ()


# ── 10. 시장별 그룹화: KR·US 각 market 으로 get_states 호출 ────
def test_market_grouping_get_states_called_per_market():
    reg = _reg()
    _reg_order(reg, order_no="K1", market="KR", code="005930", side="BUY")
    _reg_order(reg, order_no="U1", market="US", code="AAPL", side="BUY")
    fs = MockFillSource()
    ss = MockOrderStateSource()
    poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    markets = {m for (m, _nos) in ss.calls}
    assert markets == {"KR", "US"}
    # 각 호출의 order_nos 가 해당 시장 주문만 포함
    for m, nos in ss.calls:
        if m == "KR":
            assert nos == ("K1",)
        elif m == "US":
            assert nos == ("U1",)


# ── 11. 상태 미제공(order_no 누락) → CANCELED 추론 금지 ───────
def test_missing_state_no_inference():
    reg = _reg()
    _reg_order(reg, order_no="O1", qty=100)
    fs = MockFillSource()
    ss = MockOrderStateSource()        # O1 상태 미설정 → get_states 빈 dict
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    assert r.statuses == ()
    assert reg.get("O1").is_terminal() is False   # 소멸/추론으로 CANCELED 안 함


# ── 12. OrderStateSource 시장 예외 격리 ──────────────────────
def test_state_source_error_isolation():
    """
    KR 상태조회 예외 → 예외로 poll 종료 안 함. 이미 적용된 fill 유지,
    US 상태 정상 처리, KR·US 모두 timeout 검사 계속.
    """
    fake = {"t": 2000.0}
    reg = _reg(now_fn=lambda: fake["t"])
    _reg_order(reg, order_no="K1", market="KR", code="005930", side="BUY", qty=100)
    _reg_order(reg, order_no="U1", market="US", code="AAPL", side="BUY", qty=50)
    # KR 은 부분체결 발생
    fs = MockFillSource().add("KR", "005930", "BUY",
                              [Fill(order_no="K1", qty=40, price=70000)])
    # KR 상태조회는 예외, US 는 CANCELED 정상
    ss = MockOrderStateSource().fail_on("KR").set(
        OrderState(order_no="U1", status=CANCELED, filled_qty=0,
                   remaining_qty=50, updated_at="0900", raw_status="취소"))

    # timeout 도 걸리도록 now 를 크게(accepted_ts=2000, +100 >= 15)
    r = poll_pending_fills(fs, ss, reg, now=2100.0, timeout_sec=15)

    # 1) 이미 적용된 KR fill 유지
    assert any(e.order_no == "K1" and e.applied_qty == 40 for e in r.fills)
    assert reg.get("K1").applied_qty == 40
    # 2) US 상태 정상 처리(CANCELED)
    assert any(e.order_no == "U1" and e.status == CANCELED for e in r.statuses)
    assert reg.get("U1").status == CANCELED
    # 3) timeout 계속: KR(K1)은 상태처리 건너뛰었어도 timeout 으로 CancelRequest.
    #    US(U1)은 이번 폴에서 CANCELED terminal 이 되었으므로 timeout 대상 아님.
    cr_orders = {c.order_no for c in r.cancel_requests}
    assert "K1" in cr_orders
    assert "U1" not in cr_orders
    assert reg.get("K1").status == CANCEL_REQUESTED


# ── 13. purge_terminal 미호출 → terminal 주문 잔존 ────────────
def test_no_purge_terminal():
    reg = _reg()
    _reg_order(reg, order_no="O1", qty=100)
    fs = MockFillSource()
    ss = MockOrderStateSource().set(
        OrderState(order_no="O1", status=CANCELED, filled_qty=0,
                   remaining_qty=100, updated_at="0900", raw_status="취소"))
    poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    # terminal 이 되었지만 레지스트리에서 제거되지 않음(purge 미호출)
    assert reg.get("O1") is not None
    assert reg.get("O1").status == CANCELED
    assert len(reg) == 1


# ══════════════════════════════════════════════════════════════
# Fill 기준 귀속 (동일 종목 다중 주문 정확성 + 그룹당 1회 호출)
# ══════════════════════════════════════════════════════════════

# ── 14. 동일 종목 다중 주문 → Fill.order_no 로 정확 귀속 ───────
def test_same_code_multiple_orders_attributed_by_fill_order_no():
    reg = _reg()
    # 같은 종목·같은 side 에 두 주문(피라미딩 Early + add)
    _reg_order(reg, order_no="O1", market="KR", code="005930", side="BUY",
               level=1, qty=100)
    _reg_order(reg, order_no="O2", market="KR", code="005930", side="BUY",
               level=2, qty=50)
    # 한 그룹(KR,005930,BUY) 조회에서 두 주문의 Fill 이 함께 반환됨
    fs = MockFillSource().add("KR", "005930", "BUY", [
        Fill(order_no="O1", qty=100, price=70000),
        Fill(order_no="O2", qty=50,  price=71000),
    ])
    ss = MockOrderStateSource()
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)

    assert len(r.fills) == 2
    by_no = {e.order_no: e for e in r.fills}
    # O1 은 O1 델타만, O2 는 O2 델타만 (오귀속 없음)
    assert by_no["O1"].applied_qty == 100 and by_no["O1"].price == 70000
    assert by_no["O1"].level == 1
    assert by_no["O2"].applied_qty == 50 and by_no["O2"].price == 71000
    assert by_no["O2"].level == 2
    assert reg.get("O1").applied_qty == 100 and reg.get("O1").status == FILLED
    assert reg.get("O2").applied_qty == 50 and reg.get("O2").status == FILLED


# ── 15. Registry 에 없는 order_no 는 무시 ─────────────────────
def test_unknown_fill_order_no_ignored():
    reg = _reg()
    _reg_order(reg, order_no="O1", code="005930", side="BUY", qty=100)
    # 그룹조회가 O1 + (레지스트리에 없는) GHOST 를 반환
    fs = MockFillSource().add("KR", "005930", "BUY", [
        Fill(order_no="O1",    qty=40, price=70000),
        Fill(order_no="GHOST", qty=99, price=70000),
    ])
    ss = MockOrderStateSource()
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    # GHOST 는 무시, O1 만 반영
    assert len(r.fills) == 1
    assert r.fills[0].order_no == "O1" and r.fills[0].applied_qty == 40
    assert reg.get("GHOST") is None


# ── 16. 그룹당 get_fills 1회 (동일 code+side 다중 주문) ───────
def test_get_fills_called_once_per_group():
    reg = _reg()
    _reg_order(reg, order_no="O1", market="KR", code="005930", side="BUY", qty=100)
    _reg_order(reg, order_no="O2", market="KR", code="005930", side="BUY", qty=50)
    _reg_order(reg, order_no="O3", market="KR", code="000660", side="BUY", qty=10)
    fs = _SpyFillSource()
    ss = MockOrderStateSource()
    poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    # (KR,005930,BUY) 1회 + (KR,000660,BUY) 1회 = 2회 (주문 3개인데 호출 2회)
    assert fs.called == 2


# ── 17. 같은 종목 매수·매도 주문은 서로 다른 그룹 ─────────────
def test_buy_and_sell_same_code_are_distinct_groups():
    reg = _reg()
    _reg_order(reg, order_no="B", code="005930", side="BUY", qty=100)
    _reg_order(reg, order_no="S", code="005930", side="SELL", qty=100)
    fs = _SpyFillSource()
    fs.add("KR", "005930", "BUY",  [Fill(order_no="B", qty=100, price=70000)])
    fs.add("KR", "005930", "SELL", [Fill(order_no="S", qty=100, price=71000)])
    ss = MockOrderStateSource()
    r = poll_pending_fills(fs, ss, reg, now=1000.0, timeout_sec=15)
    assert fs.called == 2                        # BUY 그룹 + SELL 그룹
    sides = {e.order_no: e.side for e in r.fills}
    assert sides == {"B": "BUY", "S": "SELL"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
