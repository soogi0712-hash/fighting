"""US 체결-기반 부킹 검증 (A: rt_cd=0 접수 아님, 실체결 후에만 부킹).

커버리지:
  01 매수: 접수 시점엔 포지션 없음 → FILLED 콜백에서만 생성, 평단은 실체결가
  02 매수 중복 체결(멱등): 동일 lc 콜백 2회 → meta pop 후 1회만 반영
  03 추가매수: 기존 포지션에 가중평균 병합
  04 매도 전량: FILLED 콜백에서 포지션 삭제 + 실현손익 1회 + 재진입 1회
  05 매도 부분: 수량만 감소, 재진입 미등록
  06 매도 중복 체결(멱등): 동일 lc 콜백 2회 → 1회만 반영
  07 in-flight 가드: 동일 종목·방향 미체결 주문 있으면 True
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from strategies.us_strategy_manager import USStrategyManager, USPosition  # noqa: E402


class FakePosMgr:
    """파일 저장 없는 경량 포지션 매니저."""
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


class FakeLC:
    def __init__(self, oid, code, filled_qty, avg_fill_price, trade_id=""):
        self.order_lifecycle_id = oid
        self.code = code
        self.filled_qty = filled_qty
        self.avg_fill_price = avg_fill_price
        self.trade_id = trade_id


def make_us():
    us = USStrategyManager.__new__(USStrategyManager)
    us.pos_mgr = FakePosMgr()
    us._us_pending_buy_meta = {}
    us._us_pending_sell_meta = {}
    us._us_fill_events = []
    us.pnl_guard = MagicMock()
    us.pnl_guard.status_dict.return_value = {
        "realized_pnl": 0, "peak_pnl": 0, "state": "NORMAL"}
    us.reentry = MagicMock()
    us.api = MagicMock()
    us.api.get_usd_exchange_rate.return_value = 1350.0
    for m in ("_us_handle_buy_filled", "_us_handle_sell_filled",
              "_us_has_active_order"):
        setattr(us, m, getattr(USStrategyManager, m).__get__(us))
    return us


class TestUSFillBooking(unittest.TestCase):

    def test_01_buy_booked_only_on_fill_with_fill_price(self):
        us = make_us()
        oid = "US_BUY_AAPL_1"
        # 접수: meta 등록 (아직 포지션 없음)
        us._us_pending_buy_meta[oid] = {
            "code": "AAPL", "name": "Apple", "qty": 10,
            "price": 100.0, "avg_price": 100.0,
            "excd": "NASD", "level": 1, "trade_id": "t1"}
        self.assertNotIn("AAPL", us.pos_mgr.positions)  # 접수만으로 부킹 안 됨
        # 체결(실체결가 101.5)
        us._us_handle_buy_filled(FakeLC(oid, "AAPL", 10, 101.5, "t1"))
        self.assertIn("AAPL", us.pos_mgr.positions)
        pos = us.pos_mgr.positions["AAPL"]
        self.assertEqual(pos.qty, 10)
        self.assertEqual(pos.avg_price, 101.5)   # cur_price(100) 아님, 실체결가
        self.assertEqual(pos.trade_id, "t1")
        self.assertEqual(len(us._us_fill_events), 1)
        self.assertEqual(us._us_fill_events[0]["side"], "BUY")
        self.assertEqual(us._us_fill_events[0]["price"], 101.5)

    def test_02_buy_fill_idempotent(self):
        us = make_us()
        oid = "US_BUY_AAPL_1"
        us._us_pending_buy_meta[oid] = {
            "code": "AAPL", "name": "Apple", "qty": 10, "price": 100.0,
            "avg_price": 100.0, "excd": "NASD", "level": 1, "trade_id": ""}
        lc = FakeLC(oid, "AAPL", 10, 101.5)
        us._us_handle_buy_filled(lc)
        us._us_handle_buy_filled(lc)   # meta 이미 pop → 중복 반영 없음
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 10)
        self.assertEqual(len(us._us_fill_events), 1)

    def test_03_add_buy_merges_weighted_avg(self):
        us = make_us()
        us.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 10, 100.0))
        oid = "US_BUY_AAPL_2"
        us._us_pending_buy_meta[oid] = {
            "code": "AAPL", "name": "Apple", "qty": 10, "price": 0.0,
            "avg_price": 0.0, "excd": "NASD", "level": 2, "trade_id": ""}
        us._us_handle_buy_filled(FakeLC(oid, "AAPL", 10, 120.0))
        pos = us.pos_mgr.positions["AAPL"]
        self.assertEqual(pos.qty, 20)
        self.assertAlmostEqual(pos.avg_price, 110.0)   # (100*10+120*10)/20
        self.assertEqual(pos.current_level, 2)

    def test_04_sell_full_books_pnl_and_reentry_once(self):
        us = make_us()
        us.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 10, 100.0))
        oid = "US_SELL_AAPL_1"
        us._us_pending_sell_meta[oid] = {
            "code": "AAPL", "name": "Apple", "qty": 10, "price": 0.0,
            "avg_price": 100.0, "reason": "익절", "is_full": True,
            "trade_id": ""}
        us._us_handle_sell_filled(FakeLC(oid, "AAPL", 10, 110.0))
        self.assertNotIn("AAPL", us.pos_mgr.positions)         # 전량 삭제
        us.pnl_guard.record.assert_called_once()
        self.assertAlmostEqual(
            us.pnl_guard.record.call_args[0][0], (110 - 100) * 10 * 1350.0)
        us.reentry.record_sell.assert_called_once()
        self.assertTrue(us._us_fill_events[0]["is_full"])

    def test_05_sell_partial_reduces_qty_no_reentry(self):
        us = make_us()
        us.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 10, 100.0))
        oid = "US_SELL_AAPL_2"
        us._us_pending_sell_meta[oid] = {
            "code": "AAPL", "name": "Apple", "qty": 4, "price": 0.0,
            "avg_price": 100.0, "reason": "부분익절", "is_full": False,
            "trade_id": ""}
        us._us_handle_sell_filled(FakeLC(oid, "AAPL", 4, 110.0))
        self.assertIn("AAPL", us.pos_mgr.positions)
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 6)
        us.reentry.record_sell.assert_not_called()            # 부분 → 재진입 미등록
        us.pnl_guard.record.assert_called_once()

    def test_06_sell_fill_idempotent(self):
        us = make_us()
        us.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 10, 100.0))
        oid = "US_SELL_AAPL_1"
        us._us_pending_sell_meta[oid] = {
            "code": "AAPL", "name": "Apple", "qty": 10, "price": 0.0,
            "avg_price": 100.0, "reason": "익절", "is_full": True,
            "trade_id": ""}
        lc = FakeLC(oid, "AAPL", 10, 110.0)
        us._us_handle_sell_filled(lc)
        us._us_handle_sell_filled(lc)   # meta pop → 2번째 스킵
        self.assertEqual(us.pnl_guard.record.call_count, 1)
        self.assertEqual(us.reentry.record_sell.call_count, 1)

    def test_07_inflight_guard(self):
        us = make_us()
        us._us_pending_buy_meta["b1"] = {"code": "AAPL"}
        us._us_pending_sell_meta["s1"] = {"code": "MSFT"}
        self.assertTrue(us._us_has_active_order("AAPL", "BUY"))
        self.assertTrue(us._us_has_active_order("AAPL"))            # 방향 무관
        self.assertFalse(us._us_has_active_order("AAPL", "SELL"))   # 방향 다름
        self.assertTrue(us._us_has_active_order("MSFT", "SELL"))
        self.assertFalse(us._us_has_active_order("TSLA", "BUY"))    # 미체결 없음


if __name__ == "__main__":
    unittest.main()
