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

    # ── 현금초과 사전 차단 ───────────────────────────────────────
    def test_cash_guard_blocks_over_cash_buy_before_submit(self):
        Config.LIVE_ORDER_ENABLED = True   # 킬스위치는 통과시키고 현금가드만 검증
        self.api._get_cash_from_psbl_api = lambda: 100_000   # 주문가능현금 10만
        # 주문금액 = 10주 × 50,000 × (1+수수료) ≈ 500,075원 > 100,000
        r = self.api._order("005930", "BUY", 10, 50000)
        self.assertEqual(r.get("rt_cd"), "9")
        self.assertTrue(r.get("_cash_guard"))
        self.assertEqual(self.post_calls, [], "현금초과인데 주문 제출됨")

    def test_cash_guard_allows_within_cash(self):
        self.api._get_cash_from_psbl_api = lambda: 10_000_000  # 충분
        # 현금 충분 → 차단 없음(None)
        self.assertIsNone(
            self.api._reject_if_cash_exceeded("005930", 10, 50000))

    def test_get_orderable_cash_uses_ord_psbl_cash(self):
        self.api._get_cash_from_psbl_api = lambda: 777_777
        self.assertEqual(self.api.get_orderable_cash(), 777_777)


if __name__ == "__main__":
    unittest.main()
