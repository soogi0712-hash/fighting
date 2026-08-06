"""US 체결 delta 누적 부킹 검증 (A) + 해외 주문가능 사전검증 (B).

핵심 원리:
  delta      = cumulative_filled_qty(lc.filled_qty) − applied_qty(영속)
  delta_avg  = (누적원가 − applied원가) / delta        ← 단계별 평균체결가
  - PARTIALLY_FILLED / FILLED 모두 신규 체결분(delta)만 반영
  - 중복 폴링·재시작 재조회·부분체결 후 취소에서도 이중부킹 없음
  - 반영 성공 후에만 applied 원자적 저장, 저장 실패 시 보상 되돌림
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from strategies.us_strategy_manager import USStrategyManager, USPosition  # noqa: E402


class FakePosMgr:
    def __init__(self):
        self.positions = {}

    def add(self, pos):
        self.positions[pos.symbol] = pos

    def remove(self, symbol):
        self.positions.pop(symbol, None)

    def update(self, symbol, qty, avg_price, level):
        p = self.positions.get(symbol)
        if p:
            p.qty = qty
            p.avg_price = avg_price
            p.current_level = level


class FakeAppliedStore:
    """인메모리 applied 워터마크 스토어 (set 실패 시뮬레이션 지원)."""
    def __init__(self):
        self.data = {}
        self.fail_set = False

    def get(self, oid):
        return self.data.get(oid, (0, 0.0))

    def set(self, oid, applied_qty, applied_cost):
        if self.fail_set:
            raise RuntimeError("applied 저장 실패(시뮬)")
        self.data[oid] = (int(applied_qty), float(applied_cost))

    def clear(self, oid):
        self.data.pop(oid, None)


class FakeLC:
    def __init__(self, oid, code, filled_qty, avg_fill_price,
                 side="BUY", order_qty=10, trade_id=""):
        self.order_lifecycle_id = oid
        self.code = code
        self.filled_qty = filled_qty
        self.avg_fill_price = avg_fill_price
        self.side = side
        self.order_qty = order_qty
        self.trade_id = trade_id


def make_us(applied_store=None):
    us = USStrategyManager.__new__(USStrategyManager)
    us.pos_mgr = FakePosMgr()
    us._us_pending_buy_meta = {}
    us._us_pending_sell_meta = {}
    us._us_fill_events = []
    us._us_applied_store = applied_store or FakeAppliedStore()
    us.pnl_guard = MagicMock()
    us.pnl_guard.status_dict.return_value = {
        "realized_pnl": 0, "peak_pnl": 0, "state": "NORMAL"}
    us.reentry = MagicMock()
    us.reentry.check.return_value = (False, {})
    us.api = MagicMock()
    us.api.get_usd_exchange_rate.return_value = 1000.0   # 환산 단순화
    for m in ("_us_apply_fill_delta", "_us_book_buy_delta", "_us_book_sell_delta",
              "_us_has_active_order", "_do_buy"):
        setattr(us, m, getattr(USStrategyManager, m).__get__(us))
    return us


class TestUSDeltaBooking(unittest.TestCase):

    # 1. 매수 10주: 누적 3→5→10에서 delta 3→2→5, 단계별 평균 100/110/120
    def test_01_buy_cumulative_delta(self):
        us = make_us()
        oid = "US_BUY_AAPL_1"
        us._us_pending_buy_meta[oid] = {
            "code": "AAPL", "name": "Apple", "excd": "NASD", "level": 1,
            "qty": 10, "trade_id": "t1"}
        # 누적 (수량, 평균): (3,100)→(5,104)→(10,112)
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 3)
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 5, 104.0, "BUY", 10))
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 5)
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 10, 112.0, "BUY", 10))
        pos = us.pos_mgr.positions["AAPL"]
        self.assertEqual(pos.qty, 10)
        self.assertAlmostEqual(pos.avg_price, 112.0)   # 누적 평균과 일치
        deltas = [(e["qty"], e["price"]) for e in us._us_fill_events]
        self.assertEqual(deltas, [(3, 100.0), (2, 110.0), (5, 120.0)])  # 단계별 delta_avg

    # 2. 매도 10주: 누적 3→5→10에서 포지션·PnL이 delta만큼 반영
    def test_02_sell_cumulative_delta(self):
        us = make_us()
        us.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 10, 100.0))
        oid = "US_SELL_AAPL_1"
        us._us_pending_sell_meta[oid] = {
            "code": "AAPL", "name": "Apple", "excd": "NASD",
            "reason": "익절", "qty": 10}
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 110.0, "SELL", 10))
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 7)
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 5, 110.0, "SELL", 10))
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 5)
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 10, 110.0, "SELL", 10))
        self.assertNotIn("AAPL", us.pos_mgr.positions)     # 전량 청산
        # delta PnL = (110-100)*qty*fx(1000): 3주→30000, 2주→20000, 5주→50000
        recorded = [c.args[0] for c in us.pnl_guard.record.call_args_list]
        self.assertEqual(recorded, [30000.0, 20000.0, 50000.0])
        us.reentry.record_sell.assert_called_once()        # 청산 시 1회

    # 3. 같은 PARTIALLY_FILLED 응답 반복 → 중복 반영 없음
    def test_03_duplicate_partial_no_double(self):
        us = make_us()
        oid = "US_BUY_AAPL_1"
        us._us_pending_buy_meta[oid] = {"code": "AAPL", "name": "Apple",
                                        "excd": "NASD", "level": 1, "qty": 10}
        lc = FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10)
        us._us_apply_fill_delta(lc)
        us._us_apply_fill_delta(lc)   # 동일 누적 → delta=0
        us._us_apply_fill_delta(lc)
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 3)
        self.assertEqual(len(us._us_fill_events), 1)

    # 4. 부분체결 후 취소 → 체결분만 유지
    def test_04_partial_then_cancel_keeps_filled(self):
        us = make_us()
        oid = "US_BUY_AAPL_1"
        us._us_pending_buy_meta[oid] = {"code": "AAPL", "name": "Apple",
                                        "excd": "NASD", "level": 1, "qty": 10}
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))
        # 취소는 apply_fill_delta 를 호출하지 않음 → 체결분 3주 유지
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 3)
        self.assertEqual(us._us_applied_store.get(oid), (3, 300.0))

    # 5. 부분체결 후 재시작·재조회 → 이중 반영 없음 (applied 영속)
    def test_05_restart_requery_no_double(self):
        store = FakeAppliedStore()
        us1 = make_us(store)
        oid = "US_BUY_AAPL_1"
        us1._us_pending_buy_meta[oid] = {"code": "AAPL", "name": "Apple",
                                         "excd": "NASD", "level": 1, "qty": 10}
        us1._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))
        self.assertEqual(store.get(oid), (3, 300.0))
        # 재시작: 새 매니저(같은 영속 스토어), 포지션은 잔고에서 복원(3주)
        us2 = make_us(store)
        us2.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 3, 100.0))
        us2._us_pending_buy_meta[oid] = {"code": "AAPL", "name": "Apple",
                                         "excd": "NASD", "level": 1, "qty": 10}
        us2._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))  # 재조회
        self.assertEqual(us2.pos_mgr.positions["AAPL"].qty, 3)   # 이중 반영 없음
        self.assertEqual(len(us2._us_fill_events), 0)

    # 6. 반영 상태 저장 실패 → 보상 되돌림(불일치 없음), 이후 재시도 시 1회만
    def test_06_save_failure_rollback_recovery(self):
        store = FakeAppliedStore()
        us = make_us(store)
        oid = "US_BUY_AAPL_1"
        us._us_pending_buy_meta[oid] = {"code": "AAPL", "name": "Apple",
                                        "excd": "NASD", "level": 1, "qty": 10}
        store.fail_set = True
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))
        # 저장 실패 → 방금 반영분 되돌림: 포지션 없음, applied 미기록, 이벤트 없음
        self.assertNotIn("AAPL", us.pos_mgr.positions)
        self.assertEqual(store.get(oid), (0, 0.0))
        self.assertEqual(len(us._us_fill_events), 0)
        # 복구: 저장 정상화 후 재시도 → 정확히 1회 반영
        store.fail_set = False
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 3)
        self.assertEqual(store.get(oid), (3, 300.0))
        self.assertEqual(len(us._us_fill_events), 1)

    # 7. 해외 주문가능 사전조회 실패(ok=False) → buy_us 미호출, BUY_BLOCKED
    def test_07_prevalidation_failure_blocks_order(self):
        us = make_us()
        us.api.get_us_available_amounts.return_value = {"ok": False}
        res = us._do_buy("AAPL", "Apple", "NASD", 100.0,
                         {"session": "정규장"}, {"buy_score": 1.0})
        self.assertEqual(res["action"], "BUY_BLOCKED")
        us.api.buy_us.assert_not_called()

    # 8. 국내 예수금 있어도 해외 주문가능금액 0 → 주문 미제출(BUY_BLOCKED)
    def test_08_zero_overseas_available_blocks(self):
        us = make_us()
        # 폴백 제거로 국내 예수금은 무관 — 해외 확정액이 0이면 차단
        us.api.get_us_available_amounts.return_value = {
            "ok": True, "usd": 0.0, "krw": 0.0}
        us.api.get_orderable_cash.return_value = 10_000_000  # 국내 예수금 존재
        res = us._do_buy("AAPL", "Apple", "NASD", 100.0,
                         {"session": "정규장"}, {"buy_score": 1.0})
        self.assertEqual(res["action"], "BUY_BLOCKED")
        us.api.buy_us.assert_not_called()

    # (참고) in-flight 중복 가드
    def test_09_inflight_guard(self):
        us = make_us()
        us._us_pending_buy_meta["b1"] = {"code": "AAPL"}
        self.assertTrue(us._us_has_active_order("AAPL", "BUY"))
        self.assertFalse(us._us_has_active_order("AAPL", "SELL"))
        self.assertFalse(us._us_has_active_order("TSLA", "BUY"))


if __name__ == "__main__":
    unittest.main()
