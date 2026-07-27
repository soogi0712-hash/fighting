"""P0-1 ORD_UNPR=0 수정 — select_sell_price 단위 테스트."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from strategies.order_pricing import (  # noqa: E402
    select_sell_price, ORD_LIMIT, ORD_MARKET, ORD_PRE, ORD_POST,
)


class OrderPricingTest(unittest.TestCase):
    def test_regular_limit_uses_decision_price(self):
        # 정규장 지정가(00): decision price 전달 → 0 아님
        self.assertEqual(select_sell_price(ORD_LIMIT, 55000, 54900), 55000)

    def test_regular_limit_falls_back_to_cur_price(self):
        # decision price 없으면 현재가로 폴백(>0)
        self.assertEqual(select_sell_price(ORD_LIMIT, 0, 54900), 54900)

    def test_regular_limit_never_zero(self):
        # 지정가는 절대 0이 되지 않음 (ORD_UNPR=0 차단 방지)
        self.assertGreater(select_sell_price(ORD_LIMIT, 0, 54900), 0)

    def test_market_is_zero(self):
        self.assertEqual(select_sell_price(ORD_MARKET, 55000, 54900), 0)

    def test_post_market_is_zero(self):
        self.assertEqual(select_sell_price(ORD_POST, 55000, 54900), 0)

    def test_pre_market_requires_price(self):
        self.assertEqual(select_sell_price(ORD_PRE, 55000, 54900), 55000)
        self.assertEqual(select_sell_price(ORD_PRE, 0, 54900), 54900)

    def test_both_zero_stays_zero_only_for_market(self):
        # 지정가인데 가격 정보가 전혀 없으면 0 (→ kis_api 가 차단, 안전)
        self.assertEqual(select_sell_price(ORD_LIMIT, 0, 0), 0)
        # 시장가는 원래 0
        self.assertEqual(select_sell_price(ORD_MARKET, 0, 0), 0)


if __name__ == "__main__":
    unittest.main()
