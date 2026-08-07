"""현금 기반 사이징 — 회귀/안전 테스트.

검증 목표(승인 범위):
  1. 매수 예산 = 실제 주문가능현금(전달된 cash). compound_pool 미가산.
  2. 현금 100%까지 한 종목 투자 가능(단, 총주문금액 ≤ 현금).
  3. Early → Full: 남은 현금 범위에서 추가, 누적 총주문금액 ≤ 초기 현금.
  4. 다종목 연속매수(루프 내 차감): 사용액 합계 ≤ 초기 현금.
  5. 수수료+안전버퍼 반영으로 총 주문금액이 현금을 절대 초과하지 않음.
전략 점수/조건/손절/익절/트레일링은 건드리지 않는다.
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from strategies.pyramid_strategy import (  # noqa: E402
    PyramidStrategyManager, PyramidPosition,
    BUY_SCORE_EARLY, BUY_SCORE_FULL, CASH_SAFETY_BUFFER,
)
from screener.transaction_cost import calc_buy_cost, BUY_COMMISSION_RATE  # noqa: E402

CODE = "005930"
NAME = "삼성전자"
ACCOUNT = 5_000_000.0
PRICE = 50_000.0


def _mgr(compound_pool=0.0):
    m = PyramidStrategyManager(kis_api=None,
                              max_per_stock=ACCOUNT, max_total=ACCOUNT)
    m.positions = {}
    m.cooldown = {}
    m.daily_losses = {}
    m.compound_pool = compound_pool
    return m


def _total_cost(qty, price=PRICE):
    return calc_buy_cost(price, qty).total_cost


class TestCashBudget(unittest.TestCase):

    def test_full_entry_never_exceeds_cash(self):
        cash = 1_000_000.0
        d = _mgr().evaluate(CODE, NAME, PRICE, 5, cash, buy_score_norm=0.80)
        self.assertEqual(d["action"], "BUY_LEVEL1_FULL")
        tc = _total_cost(d["qty"])
        self.assertLessEqual(tc, cash, "총주문금액이 현금 초과")
        # 한 주 더 사면 현금 초과(=최대 활용 확인)
        self.assertGreater(_total_cost(d["qty"] + 1), cash)

    def test_compound_pool_not_added_to_buying_power(self):
        cash = 1_000_000.0
        # 복리풀을 현금의 5배로 크게 줘도 수량은 '현금' 기준이어야 함
        d = _mgr(compound_pool=5_000_000.0).evaluate(
            CODE, NAME, PRICE, 5, cash, buy_score_norm=0.80)
        tc = _total_cost(d["qty"])
        self.assertLessEqual(tc, cash, "compound_pool이 매수여력을 부풀림(이중계상)")
        # 풀 가산 시 나올 수량(≈119주)과 확연히 다름
        self.assertLess(d["qty"], 30)
        # 매수 결정에 using_compound가 0 (풀 미차감)
        self.assertEqual(d.get("using_compound", 0), 0)

    def test_cash_safety_buffer_applied(self):
        cash = 1_000_000.0
        d = _mgr().evaluate(CODE, NAME, PRICE, 5, cash, buy_score_norm=0.80)
        # 예산은 cash × buffer 이하 → 총주문금액 ≤ cash × buffer 근처
        self.assertLessEqual(_total_cost(d["qty"]), cash * CASH_SAFETY_BUFFER)


class TestNoPerStockOrTotalCap(unittest.TestCase):
    """max_per_stock/max_total 한도 제거 — 현금이 계좌한도보다 커도 현금만큼 투자."""

    def test_cash_over_account_cap_uses_full_cash(self):
        # 종목당·전체 한도를 현금보다 훨씬 작게(1M) 설정해도, 현금(5M) 전액 투자 가능
        m = PyramidStrategyManager(kis_api=None,
                                   max_per_stock=1_000_000.0, max_total=1_000_000.0)
        m.positions = {}; m.cooldown = {}; m.daily_losses = {}; m.compound_pool = 0.0
        cash = 5_000_000.0
        d = m.evaluate(CODE, NAME, PRICE, 5, cash, buy_score_norm=0.80)
        self.assertEqual(d["action"], "BUY_LEVEL1_FULL")
        tc = _total_cost(d["qty"])
        self.assertGreater(tc, 1_000_000.0, "종목당/전체 한도가 아직 적용됨(캡 미제거)")
        self.assertLessEqual(tc, cash, "현금 초과")
        self.assertGreater(tc, cash * 0.98)   # 현금의 ~100% 활용


class TestEarlyThenFull(unittest.TestCase):

    def test_early_then_full_cumulative_within_initial_cash(self):
        initial_cash = 1_000_000.0
        m = _mgr()
        # 1) Early 진입 (0.40 ≤ score < 0.55)
        early = m.evaluate(CODE, NAME, PRICE, 5, initial_cash,
                           buy_score_norm=(BUY_SCORE_EARLY + BUY_SCORE_FULL) / 2)
        self.assertEqual(early["action"], "BUY_LEVEL1_EARLY")
        early_cost = _total_cost(early["qty"])

        # 2) Early 체결 상태를 반영한 포지션 구성
        avg = early_cost / early["qty"]
        pos = PyramidPosition.from_dict({
            "code": CODE, "name": NAME, "entry_price": avg, "highest_price": PRICE,
            "current_level": 1, "avg_price": avg, "total_qty": early["qty"],
            "level_entries": {"1": {"price": avg, "avg_price": avg,
                                    "qty": early["qty"], "remaining": early["qty"],
                                    "total_cost": early_cost,
                                    "added_at": "2026-01-01T00:00:00"}},
            "created_at": "2026-01-01T00:00:00"})

        # 3) 남은 주문가능현금으로 Full 추가 (그 시점 remaining cash 전달)
        remaining = initial_cash - early_cost
        full = m._try_full_entry(CODE, NAME, PRICE, 5, remaining, pos)
        self.assertTrue(full["action"].startswith("BUY_LEVEL1_FULL"))
        full_cost = _total_cost(full["qty"])

        # 누적 총주문금액 ≤ 초기 현금 (미수 없음)
        self.assertLessEqual(early_cost + full_cost, initial_cash)
        # 남은 현금을 대부분 활용(≥ 초기의 90%)
        self.assertGreater(early_cost + full_cost, initial_cash * 0.90)


class TestMultiStockLoopDedup(unittest.TestCase):

    def test_sequential_buys_do_not_reuse_same_cash(self):
        """전략 신호가 매번 성립해도, 매수 직전 KIS 현금 주문가능금액(=남은 현금)
        으로 최종수량을 확정하면 누적 주문금액이 현금을 초과하지 않는다(미수 없음).

        ★ 신규 계약: 예수금 선차단이 아니라 KIS 현금 주문가능금액이 권위값.
          pyramid.evaluate 는 비중(invest_ratio)만 제안하고, 최종수량은
          finalize_order_qty(KIS 현금 기준)로 확정된다.
        """
        from utils.order_sizing import finalize_order_qty
        initial_cash = 1_000_000.0
        m = _mgr()
        remaining = initial_cash
        spent_total = 0.0
        for code in ("AAA", "BBB", "CCC"):
            d = m.evaluate(code, code, PRICE, 5, remaining, buy_score_norm=0.80)
            if not d["action"].startswith("BUY"):
                continue
            ratio = float(d.get("invest_ratio", 1.0))
            # 매수 직전 KIS 현금 주문가능금액(= 남은 현금)으로 최종수량 확정
            ratio_cash = remaining * ratio
            final_qty  = finalize_order_qty(
                int(ratio_cash / PRICE), 10**9, ratio_cash, PRICE)
            if final_qty <= 0:
                continue
            cost = _total_cost(final_qty)
            remaining = max(0.0, remaining - cost)
            spent_total += cost
        # 세 종목 합계가 초기 현금을 절대 초과하지 않음(0.98 버퍼로 여유)
        self.assertLessEqual(spent_total, initial_cash)
        self.assertGreaterEqual(remaining, 0.0)


if __name__ == "__main__":
    unittest.main()
