"""utils.order_sizing — KIS 현금 주문가능금액·수량 기반 최종수량 확정 규칙.

최종수량 = min(전략수량, KIS현금주문가능수량, floor(현금가능금액*0.98/주문가)).
예수금 단독 나눗셈이 아니라 세 상한의 최솟값을 쓴다.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from utils.order_sizing import finalize_order_qty, qty_from_cash, CASH_BUFFER  # noqa: E402


class TestQtyFromCash(unittest.TestCase):
    def test_fee_buffer_applied(self):
        # 1,000,000원 / 10,000원 = 100주지만 0.98 버퍼 → floor(980,000/10,000)=98
        self.assertEqual(qty_from_cash(1_000_000, 10_000), 98)

    def test_floor_not_round(self):
        # 99,000*0.98/1000 = 97.02 → floor 97
        self.assertEqual(qty_from_cash(99_000, 1_000), 97)

    def test_zero_price_or_amount(self):
        self.assertEqual(qty_from_cash(1_000_000, 0), 0)
        self.assertEqual(qty_from_cash(0, 1000), 0)
        self.assertEqual(qty_from_cash(-5, 1000), 0)

    def test_invalid_inputs(self):
        self.assertEqual(qty_from_cash(None, 1000), 0)
        self.assertEqual(qty_from_cash("x", 1000), 0)

    def test_buffer_constant(self):
        self.assertEqual(CASH_BUFFER, 0.98)


class TestFinalizeOrderQty(unittest.TestCase):
    def test_deposit_large_but_orderable_qty_small(self):
        """예수금(금액)은 크지만 KIS 주문가능수량이 작으면 그 수량으로 제한."""
        # 전략 100, 금액환산 floor(1e9*0.98/1000)=980000, KIS수량 3 → 3
        self.assertEqual(finalize_order_qty(100, 3, 1_000_000_000, 1000), 3)

    def test_amount_present_but_qty_zero_blocks(self):
        """금액은 있어도 KIS 주문가능수량 0 → 최종 0(BUY_BLOCKED)."""
        self.assertEqual(finalize_order_qty(100, 0, 1_000_000_000, 1000), 0)

    def test_amount_cap_dominates(self):
        """금액환산 수량이 가장 작으면 그 값이 최종."""
        # 전략 100, KIS수량 100, 금액 50,000/1,000 → floor(49000/1000)=49
        self.assertEqual(finalize_order_qty(100, 100, 50_000, 1000), 49)

    def test_strategy_qty_dominates(self):
        # 전략 5 가 가장 작음
        self.assertEqual(finalize_order_qty(5, 100, 1_000_000, 1000), 5)

    def test_zero_price_blocks(self):
        self.assertEqual(finalize_order_qty(100, 100, 1_000_000, 0), 0)

    def test_negative_or_invalid(self):
        self.assertEqual(finalize_order_qty(-1, 100, 1_000_000, 1000), 0)
        self.assertEqual(finalize_order_qty(100, 100, 1_000_000, None), 0)
        self.assertEqual(finalize_order_qty("x", 100, 1_000_000, 1000), 0)

    def test_us_fractional_price(self):
        # 미국 소수 단가: 전략10, KIS수량10, floor(196*0.98/50)=3
        self.assertEqual(finalize_order_qty(10, 10, 196.0, 50.0), 3)


if __name__ == "__main__":
    unittest.main()
