"""국내 수익 매도정책 = '전량 트레일링' 단일화 검증.

정책:
  1. 수익률 +1.5% 도달 시 트레일링 활성화
  2. 활성화 후 최고가 계속 갱신
  3. 최고가 대비 -1.0% 하락 시 전량매도
  4~8. +2.0%/+2.5%/+1.5%+SELL_SCORE 고정익절, KRW 금액익절, % 부분익절 폴백 제거
  9. 손절·하드손절·매도 안전장치 유지
  10. 정상 포지션 시간청산 유지
  11. recovered 복원 포지션은 시간청산 면제(트레일링만)
  12. 미국 매도정책 미변경
"""
import os
import sys
import shutil
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import strategies.pyramid_strategy as ps                       # noqa: E402
from strategies.pyramid_strategy import (                       # noqa: E402
    PyramidStrategyManager, TRAILING_ACTIVATE_PCT, TRAILING_STOP_PCT,
    STOP_LOSS_PCT, TIME_EXIT_40_MIN,
)
from screener.transaction_cost import price_for_net_pct_from_cost  # noqa: E402

AVG = 70000


class TrailingPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kr-trail-")
        self._orig_py = ps.PYRAMID_FILE
        self._orig_cp = ps.COMPOUND_FILE
        ps.PYRAMID_FILE  = os.path.join(self.tmp, "pyramid_positions.json")
        ps.COMPOUND_FILE = os.path.join(self.tmp, "compound_pool.json")
        self.mgr = PyramidStrategyManager(None, max_per_stock=1e9, max_total=1e9)

    def tearDown(self):
        ps.PYRAMID_FILE  = self._orig_py
        ps.COMPOUND_FILE = self._orig_cp
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pos(self, avg=AVG, qty=10, highest_net=None, level=4, recovered=False):
        """포지션을 심는다. level=4 로 두어 피라미딩 추가매수 분기를 배제."""
        p = self.mgr._build_reconciled_position("005930", "삼성전자", qty, avg,
                                                cur_price=avg)
        p.recovered     = recovered
        p.current_level = level
        p.highest_price = (price_for_net_pct_from_cost(avg, highest_net)
                           if highest_net is not None else avg)
        self.mgr.positions["005930"] = p
        return p

    def _eval(self, cur, sell_score=0, today_high=0.0):
        return self.mgr.evaluate("005930", "삼성전자", cur, 0, 0.0,
                                 today_high=today_high, buy_score_norm=0.0,
                                 sell_score=sell_score)

    # ── 활성화 경계 ───────────────────────────────────────────
    def test_below_activate_holds(self):
        """+1.49% (신고가) → 트레일링 비활성 → HOLD."""
        self._pos(highest_net=None)
        cur = price_for_net_pct_from_cost(AVG, 1.49)
        d = self._eval(cur, today_high=cur)
        self.assertEqual(d["action"], "HOLD")

    def test_at_activate_no_immediate_sell(self):
        """+1.50% 도달 → 활성화되지만 즉시 매도하지 않음(고점=현재가)."""
        self._pos(highest_net=None)
        cur = price_for_net_pct_from_cost(AVG, 1.50)
        d = self._eval(cur, today_high=cur)
        self.assertEqual(d["action"], "HOLD")

    def test_holds_at_peak_various_levels(self):
        """+2%/+2.5%/+5%/+10% 신고가 유지 중이면 HOLD(고정 익절 없음)."""
        for lvl in (2.0, 2.5, 5.0, 10.0):
            self._pos(highest_net=None)
            cur = price_for_net_pct_from_cost(AVG, lvl)
            d = self._eval(cur, today_high=cur)
            self.assertEqual(d["action"], "HOLD",
                             f"+{lvl}% 신고가인데 HOLD 아님: {d.get('reason')}")

    # ── 트레일링 청산 경계 ────────────────────────────────────
    def test_drop_099_from_high_holds(self):
        """고점(+활성 초과) 대비 -0.99% → HOLD(청산 임계 미달)."""
        p = self._pos(highest_net=None)
        p.highest_price = 100000           # 활성가(≈+1.5%)를 크게 초과 → 활성
        cur = 99010                        # 고점대비 -0.99% (정수, 부동소수 오차 없음)
        d = self._eval(cur)
        self.assertEqual(d["action"], "HOLD")

    def test_drop_100_from_high_sells_all(self):
        """고점(+활성 초과) 대비 -1.0% → SELL_ALL(전량 트레일링)."""
        p = self._pos(highest_net=None)
        p.highest_price = 100000
        cur = 99000                        # 고점대비 정확히 -1.0%
        d = self._eval(cur)
        self.assertEqual(d["action"], "SELL_ALL")
        self.assertIn("트레일링", d["reason"])

    def test_high_sellscore_no_fixed_sell_before_trailing(self):
        """SELL_SCORE 높아도 고점 대비 -1% 전에는 고정 익절하지 않음."""
        self._pos(highest_net=None)
        cur = price_for_net_pct_from_cost(AVG, 6.0)   # 신고가 +6%
        d = self._eval(cur, sell_score=27, today_high=cur)
        self.assertEqual(d["action"], "HOLD")

    def test_no_krw_amount_take_profit(self):
        """미실현이익 1만원·3만원을 넘어도 금액 익절하지 않음."""
        # 대량 보유로 미실현이익>3만원이지만 net%는 활성 미달·신고가 → HOLD
        p = self._pos(qty=100, highest_net=None)
        cur = AVG + 500                    # +0.7%gross, 이익 500*100=5만원(>3만)
        profit_amt = (cur - p.avg_price) * p.total_qty
        self.assertGreaterEqual(profit_amt, 30_000)
        d = self._eval(cur, today_high=cur)
        self.assertEqual(d["action"], "HOLD")

    def test_recovered_holds_at_profit_then_trails(self):
        """recovered 복원 직후 수익이어도 즉시 익절 안 함, 이후 트레일링만."""
        # 복원: cur>avg → highest=max(avg,cur)=cur (신고가) → 즉시 매도 없음
        p = self.mgr._build_reconciled_position(
            "005930", "삼성전자", 10, AVG,
            cur_price=price_for_net_pct_from_cost(AVG, 2.0))
        p.current_level = 4                # 피라미딩 추가매수 분기 배제
        self.mgr.positions["005930"] = p
        self.assertTrue(p.recovered)
        cur_peak = p.highest_price
        d1 = self._eval(cur_peak, today_high=cur_peak)
        self.assertEqual(d1["action"], "HOLD")         # 복원 직후 익절 안 함
        # 이후 고점 대비 -1.0% 하락 → 트레일링 SELL
        cur_drop = p.highest_price * 0.99
        d2 = self._eval(cur_drop)
        self.assertEqual(d2["action"], "SELL_ALL")
        self.assertIn("트레일링", d2["reason"])

    # ── 안전장치 회귀 없음 ────────────────────────────────────
    def test_stop_loss_still_fires(self):
        """손절(-5%) 회귀 없음 → SELL_ALL."""
        self._pos(highest_net=None)
        cur = price_for_net_pct_from_cost(AVG, STOP_LOSS_PCT - 0.5)  # < -5%
        d = self._eval(cur, today_high=cur)
        self.assertEqual(d["action"], "SELL_ALL")
        self.assertIn("손절", d["reason"])

    def test_time_exit_still_fires_for_normal(self):
        """정상 포지션 시간청산 유지(recovered 아님, 40분+ & 저수익)."""
        from datetime import datetime, timedelta
        p = self._pos(highest_net=None, recovered=False)
        p.created_at = (datetime.now()
                        - timedelta(minutes=TIME_EXIT_40_MIN + 5)).isoformat()
        cur = AVG + 30    # 실질 ~0%, 신고가(트레일링 미활성)
        d = self._eval(cur, today_high=cur)
        self.assertEqual(d["action"], "SELL_ALL")
        self.assertIn("시간청산", d["reason"])

    def test_recovered_exempt_from_time_exit(self):
        """recovered 포지션은 시간청산 면제(동일 조건에서 HOLD)."""
        from datetime import datetime, timedelta
        p = self._pos(highest_net=None, recovered=True)
        p.created_at = (datetime.now()
                        - timedelta(minutes=TIME_EXIT_40_MIN + 5)).isoformat()
        cur = AVG + 30
        d = self._eval(cur, today_high=cur)
        self.assertEqual(d["action"], "HOLD")


class USUnchangedTest(unittest.TestCase):
    """미국 매도정책 미변경 확인(고정 익절 상수·분기 유지)."""

    def test_us_constants_unchanged(self):
        import strategies.us_strategy_manager as us
        self.assertEqual(us.PROFIT_FULL_PCT, 2.0)
        self.assertEqual(us.PROFIT_SUPER_PCT, 2.5)
        self.assertEqual(us.PROFIT_TRAIL_PCT, 1.5)
        self.assertEqual(us.PROFIT_PARTIAL_KRW_US, 10_000)
        self.assertEqual(us.PROFIT_FULL_KRW_US, 30_000)

    def test_us_source_retains_fixed_profit_branches(self):
        """US 소스에 고정 익절 분기가 그대로 남아있다(정책 미변경)."""
        path = os.path.join(_ROOT, "strategies", "us_strategy_manager.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("+2.5%무조건전량익절", src)
        self.assertIn("+2.0%전량익절", src)
        self.assertIn("PROFIT_FULL_KRW_US", src)


if __name__ == "__main__":
    unittest.main()
