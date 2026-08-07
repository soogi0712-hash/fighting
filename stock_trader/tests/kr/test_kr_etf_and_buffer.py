"""ETF 매수 게이트 + 전략비중/0.98 버퍼 이중적용 방지(수치) + 잔고캐시 무효화.

- req6/9: 최종 = min(전략수량, nrcvb_buy_qty, floor(ord_psbl_cash*비중*0.98/가)).
          비중과 0.98 이 각각 '정확히 1회'만 적용되는지 수치로 검증.
- req3:   ETF 도 주문 직전 종목·가격으로 get_kr_available_amounts 게이트 통과.
- req13:  invalidate_balance_cache 가 짧은 TTL 캐시를 비운다.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from strategies.strategy_manager import StrategyManager  # noqa: E402
from utils.order_sizing import qty_from_cash, kr_gate_from_api  # noqa: E402


def _mgr(avail):
    mgr = StrategyManager.__new__(StrategyManager)
    mgr.api = MagicMock()
    mgr.api.get_kr_available_amounts.return_value = avail
    mgr._pending_registry = None
    for m in ("_kr_finalize_buy_qty",):
        setattr(mgr, m, getattr(StrategyManager, m).__get__(mgr))
    return mgr


class TestRatioAppliedOnceNoBuffer(unittest.TestCase):
    """국내: 비중 1회 적용, 0.98 버퍼 미적용(수치)."""

    def test_pyramid_ratio_single_no_buffer(self):
        # nrcvb_buy_amt=1,000,000, 비중 0.30, 가격 10,000
        mgr = _mgr({"ok": True, "amount": 1_000_000, "qty": 999, "cash": 1_000_000})
        qty, _ = mgr._kr_finalize_buy_qty("005930", 10000, 0.30)
        # 비중 1회, 버퍼 미적용 → floor(1e6*0.30/1e4)=30
        self.assertEqual(qty, 30)
        self.assertNotEqual(qty, 29)   # 0.98 적용값(29)이 아님
        self.assertNotEqual(qty, 9)    # 비중 이중(0.09)도 아님

    def test_etf_strategy_qty_cap(self):
        """ETF: 전략수량(strategy_qty)이 더 작으면 그 값이 최종."""
        mgr = _mgr({"ok": True, "amount": 1_000_000, "qty": 999, "cash": 1_000_000})
        qty, _ = mgr._kr_finalize_buy_qty("069500", 10000, 0.30, strategy_qty=5)
        self.assertEqual(qty, 5)       # min(5, ratio 30, nrcvb 999)

    def test_ratio_qty_cap_when_strategy_large(self):
        """ETF: 전략수량이 크면 floor(nrcvb_buy_amt*비중/가)로 제한(버퍼 없음)."""
        mgr = _mgr({"ok": True, "amount": 1_000_000, "qty": 999, "cash": 1_000_000})
        qty, _ = mgr._kr_finalize_buy_qty("069500", 10000, 0.30, strategy_qty=100)
        self.assertEqual(qty, 30)      # min(100, 30, 999)

    def test_nrcvb_caps(self):
        """미수 없는 수량(nrcvb)이 가장 작으면 그 값이 최종(미수수량 미사용)."""
        mgr = _mgr({"ok": True, "amount": 1_000_000, "qty": 3, "cash": 1_000_000})
        qty, _ = mgr._kr_finalize_buy_qty("069500", 10000, 1.0, strategy_qty=100)
        self.assertEqual(qty, 3)

    def test_us_keeps_098_but_kr_does_not(self):
        """동일 입력에서 미국은 0.98 유지(49), 국내는 미적용(50)."""
        from utils.order_sizing import finalize_order_qty
        # 미국 경로(finalize_order_qty 기본 buffer=0.98): floor(5e5*0.98/1e4)=49
        us_qty = finalize_order_qty(999, 999, 500_000, 10000)
        self.assertEqual(us_qty, 49)
        # 국내 경로(_kr_finalize_buy_qty, 버퍼 미적용): floor(5e5/1e4)=50
        mgr = _mgr({"ok": True, "amount": 500_000, "qty": 999, "cash": 500_000})
        kr_qty = mgr._kr_finalize_buy_qty("005930", 10000, 1.0)[0]
        self.assertEqual(kr_qty, 50)
        self.assertNotEqual(kr_qty, us_qty)


class TestETFOrderableGate(unittest.TestCase):
    """kr_gate_from_api — ETF 주문 직전 KIS 게이트(개별주와 동일 규칙).
    app._kr_orderable_gate 는 StrategyManager 없을 때 이 함수를 그대로 호출한다."""

    def _api(self, avail):
        api = MagicMock()
        api.get_kr_available_amounts.return_value = avail
        return api

    def test_gate_blocks_on_zero_qty(self):
        api = self._api({"ok": True, "amount": 1_000_000, "qty": 0, "cash": 1_000_000})
        final, reason = kr_gate_from_api(api, "069500", 10000, 0.30, 10)
        self.assertEqual(final, 0)
        self.assertIn("미제출", reason)

    def test_gate_blocks_on_lookup_failure(self):
        api = self._api({"ok": False})
        final, reason = kr_gate_from_api(api, "069500", 10000, 0.30, 10)
        self.assertEqual(final, 0)
        self.assertIn("미제출", reason)

    def test_gate_returns_capped_qty(self):
        api = self._api({"ok": True, "amount": 1_000_000, "qty": 999, "cash": 1_000_000})
        final, reason = kr_gate_from_api(api, "069500", 10000, 0.30, 100)
        self.assertEqual(final, 30)     # floor(1e6*0.3/1e4)=30 (국내 버퍼 없음)
        self.assertEqual(reason, "OK")

    def test_gate_uses_actual_symbol_price(self):
        api = self._api({"ok": True, "amount": 500_000, "qty": 999, "cash": 500_000})
        final, reason = kr_gate_from_api(api, "069500", 10000, 1.0, 100)
        self.assertEqual(final, 50)     # floor(5e5/1e4)=50 (국내 버퍼 없음)
        api.get_kr_available_amounts.assert_called_once_with("069500", 10000, "00")

    def test_gate_no_order_call_when_blocked(self):
        """게이트가 0 을 반환하면 주문 함수는 호출되지 않는다(호출은 상위 책임)."""
        api = self._api({"ok": False})
        api.buy = MagicMock()
        final, _ = kr_gate_from_api(api, "069500", 10000, 0.30, 10)
        self.assertEqual(final, 0)
        api.buy.assert_not_called()


class TestBalanceCacheInvalidate(unittest.TestCase):
    """req13: 접수·체결·취소 후 무효화되도록 하는 API."""

    def test_invalidate_clears_short_cache(self):
        from api.kis_api import KISApi
        api = object.__new__(KISApi)
        api._balance_short_cache = {"cash": 1}
        api._balance_short_ts = 12345.0
        api.invalidate_balance_cache()
        self.assertIsNone(api._balance_short_cache)
        self.assertEqual(api._balance_short_ts, 0.0)


if __name__ == "__main__":
    unittest.main()
