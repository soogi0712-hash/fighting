"""국내 매수: KIS 현금 주문가능금액·수량(inquire-psbl-order, 미수 없는 현금 기준)
으로 최종수량 확정. 예수금은 교차검증/로그용이며 권위값이 아니다.

_kr_finalize_buy_qty:
  min(전략수량, KIS현금주문가능수량(nrcvb_buy_qty), floor(현금가능금액*0.98/주문가))
  조회실패/rt_cd오류/수량·금액 0 → (0, 사유) → 호출측 BUY_BLOCKED, api.buy 미호출.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from strategies.strategy_manager import StrategyManager   # noqa: E402
from utils.order_sizing import qty_from_cash              # noqa: E402


def make_kr(avail_returns):
    """_kr_finalize_buy_qty / has_active_buy 구동용 경량 매니저."""
    mgr = StrategyManager.__new__(StrategyManager)
    mgr.api = MagicMock()
    if isinstance(avail_returns, list):
        mgr.api.get_kr_available_amounts.side_effect = avail_returns
    else:
        mgr.api.get_kr_available_amounts.return_value = avail_returns
    mgr._pending_registry = None
    for m in ("_kr_finalize_buy_qty", "has_active_buy"):
        setattr(mgr, m, getattr(StrategyManager, m).__get__(mgr))
    return mgr


class TestKRFinalizeBuyQty(unittest.TestCase):

    def test_tiny_deposit_but_kis_cash_sufficient_orders(self):
        """예수금 6,098원이지만 KIS 종목별 현금 주문가능액이 충분하면 주문 성립.
        (예수금 선차단 제거 — deposit_cash 는 교차검증/로그용)."""
        mgr = make_kr({"ok": True, "amount": 1_000_000, "qty": 100,
                       "cash": 1_000_000})
        qty, reason = mgr._kr_finalize_buy_qty(
            "005930", 10000, 1.0, deposit_cash=6098)
        # min(floor(1e6/1e4)=100, nrcvb 100, floor(1e6*0.98/1e4)=98)=98
        self.assertEqual(qty, 98)
        self.assertEqual(reason, "OK")
        mgr.api.buy.assert_not_called()   # 확정 단계는 주문을 내지 않는다

    def test_kis_qty_zero_blocks(self):
        """종목별 현금 주문가능수량 0 → 0(BUY_BLOCKED), buy 미호출."""
        mgr = make_kr({"ok": True, "amount": 1_000_000, "qty": 0,
                       "cash": 1_000_000})
        qty, reason = mgr._kr_finalize_buy_qty("005930", 10000, 1.0)
        self.assertEqual(qty, 0)
        self.assertIn("미제출", reason)
        mgr.api.buy.assert_not_called()

    def test_ratio_30pct_applied_to_kis_cash(self):
        """전략비중 30%는 예수금이 아니라 KIS 현금 주문가능금액에 적용."""
        mgr = make_kr({"ok": True, "amount": 1_000_000, "qty": 999,
                       "cash": 1_000_000})
        qty, _ = mgr._kr_finalize_buy_qty("005930", 10000, 0.30)
        # ratio_cash=300,000 → min(floor(30e4/1e4)=30, 999,
        #                          floor(30e4*0.98/1e4)=29) = 29
        self.assertEqual(qty, 29)

    def test_lookup_failure_no_order(self):
        """조회 실패(ok=False) → 0, buy 미호출."""
        mgr = make_kr({"ok": False})
        qty, reason = mgr._kr_finalize_buy_qty("005930", 10000, 1.0)
        self.assertEqual(qty, 0)
        self.assertIn("미제출", reason)
        mgr.api.buy.assert_not_called()

    def test_fee_buffer_applied_once(self):
        """0.98 버퍼는 1회만 적용: floor(현금가능금액*ratio*0.98/주문가)."""
        # amount 500,000, ratio 1.0 → floor(490,000/10,000)=49
        mgr = make_kr({"ok": True, "amount": 500_000, "qty": 999,
                       "cash": 500_000})
        qty, _ = mgr._kr_finalize_buy_qty("005930", 10000, 1.0)
        self.assertEqual(qty, 49)
        self.assertEqual(qty, qty_from_cash(500_000, 10000))

    def test_inflight_reduces_available(self):
        """미체결 주문으로 KIS 현금 가능금액·수량 감소 → 재조회 시 축소 반영."""
        mgr = make_kr([
            {"ok": True, "amount": 1_000_000, "qty": 100, "cash": 1_000_000},
            {"ok": True, "amount": 200_000, "qty": 20, "cash": 200_000},  # 소진 후
        ])
        q1, _ = mgr._kr_finalize_buy_qty("005930", 10000, 1.0)
        q2, _ = mgr._kr_finalize_buy_qty("005930", 10000, 1.0)
        # 1차: min(100, 100, floor(980000/10000)=98)=98
        self.assertEqual(q1, 98)
        # 2차(미체결 소진 반영): min(20, 20, floor(196000/10000)=19)=19
        self.assertEqual(q2, 19)
        self.assertLess(q2, q1)

    def test_query_uses_actual_symbol_price_dvsn(self):
        """계좌·종목·실제주문가격·주문구분 기준 조회(예수금 나눗셈 아님)."""
        mgr = make_kr({"ok": True, "amount": 1_000_000, "qty": 100,
                       "cash": 1_000_000})
        mgr._kr_finalize_buy_qty("035720", 55000, 0.30, ord_dvsn="00")
        mgr.api.get_kr_available_amounts.assert_called_once_with(
            "035720", 55000, "00")


class TestKRInflightGuard(unittest.TestCase):

    def test_active_buy_blocks_duplicate(self):
        """동일 종목 미체결 매수 존재 → has_active_buy True(중복 신규매수 차단)."""
        mgr = make_kr({"ok": True, "amount": 1, "qty": 1, "cash": 1})
        reg = MagicMock()
        reg.has_active_order.return_value = True
        mgr._pending_registry = reg
        self.assertTrue(mgr.has_active_buy("005930", market="KR"))
        reg.has_active_order.return_value = False
        self.assertFalse(mgr.has_active_buy("005930", market="KR"))

    def test_no_registry_returns_false(self):
        mgr = make_kr({"ok": True, "amount": 1, "qty": 1, "cash": 1})
        mgr._pending_registry = None
        self.assertFalse(mgr.has_active_buy("005930"))


if __name__ == "__main__":
    unittest.main()
