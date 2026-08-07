"""LIVE_ORDER_ENABLED 킬스위치 + 현금초과 사전 차단 테스트.

검증 목표:
  - LIVE_ORDER_ENABLED=false 이면 매수/매도/취소/해외주문이 KIS 실주문 API(requests.post)를
    절대 호출하지 않고 dry-run 응답을 반환한다.
  - 현금초과(미수) 주문은 제출 전에 차단된다(신용·미수 미사용).
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import api.kis_api as kmod  # noqa: E402
from api.kis_api import KISApi as KisAPI  # noqa: E402
from config import Config  # noqa: E402


class _PostForbidden(Exception):
    pass


class LiveOrderSwitchTest(unittest.TestCase):

    def setUp(self):
        self._orig_live = Config.LIVE_ORDER_ENABLED
        self._orig_post = kmod.requests.post
        self.post_calls = []

        def _boom(*a, **k):
            self.post_calls.append((a, k))
            raise _PostForbidden("실주문 API가 호출되면 안 됨")

        kmod.requests.post = _boom
        # __init__(네트워크 토큰 발급) 우회
        self.api = object.__new__(KisAPI)

    def tearDown(self):
        Config.LIVE_ORDER_ENABLED = self._orig_live
        kmod.requests.post = self._orig_post

    # ── 킬스위치: false → 실주문 미호출 ──────────────────────────
    def test_kill_switch_blocks_domestic_buy_sell(self):
        Config.LIVE_ORDER_ENABLED = False
        for fn, args in (
            (self.api.buy,  ("005930", 10, 50000)),
            (self.api.sell, ("005930", 10, 50000)),
        ):
            r = fn(*args)
            self.assertEqual(r.get("rt_cd"), "9")
            self.assertTrue(r.get("_dry_run"))
            self.assertTrue(r.get("_live_disabled"))
        self.assertEqual(self.post_calls, [], "실주문 requests.post 호출됨")

    def test_kill_switch_blocks_cancel(self):
        Config.LIVE_ORDER_ENABLED = False
        r = self.api.cancel_order("0000117057", "005930", 10, 50000)
        self.assertTrue(r.get("_dry_run"))
        self.assertEqual(self.post_calls, [])

    def test_kill_switch_blocks_us_orders(self):
        Config.LIVE_ORDER_ENABLED = False
        r1 = self.api.buy_us("TSLA", 1, 250.0)
        r2 = self.api.sell_us("TSLA", 1, 250.0)
        self.assertTrue(r1.get("_dry_run"))
        self.assertTrue(r2.get("_dry_run"))
        self.assertEqual(self.post_calls, [])

    # ── BUY 최종검증(nrcvb 기반, ord_psbl_cash [현금초과 차단] 대체) ────────
    def test_nrcvb_guard_blocks_over_qty(self):
        """요청수량 > nrcvb_buy_qty → 차단(_cash_guard). ord_psbl_cash 미사용."""
        self.api.get_kr_available_amounts = lambda *a, **k: {
            "ok": True, "amount": 9_999_999.0, "qty": 1, "ord_psbl_cash": 6098.0}
        r = self.api._reject_if_nrcvb_insufficient("005930", 10, 50000, "00")
        self.assertIsNotNone(r)
        self.assertEqual(r.get("rt_cd"), "9")
        self.assertTrue(r.get("_cash_guard"))

    def test_nrcvb_guard_allows_within(self):
        """qty<=nrcvb_buy_qty 이고 금액<=nrcvb_buy_amt → 통과(None)."""
        self.api.get_kr_available_amounts = lambda *a, **k: {
            "ok": True, "amount": 10_000_000.0, "qty": 100, "ord_psbl_cash": 6098.0}
        self.assertIsNone(
            self.api._reject_if_nrcvb_insufficient("005930", 10, 50000, "00"))

    def test_get_orderable_cash_uses_ord_psbl_cash(self):
        self.api._get_cash_from_psbl_api = lambda: 777_777
        self.assertEqual(self.api.get_orderable_cash(), 777_777)


if __name__ == "__main__":
    unittest.main()
