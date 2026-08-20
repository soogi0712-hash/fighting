"""US 수익 전용 트레일링(손절 없음) 상태기계 — 순수 로직 테스트.

정책(재정의):
  · 손실 구간에서는 어떤 자동 SELL 도 없다(HOLD, 재상승 대기). 고정 -5/-6 손절,
    구조하락+금액한도 손실매도, RECOVERY 손실청산 분기 전부 비활성화.
  · 활성화: 순수익률 최고점 >= trail_pct + MIN_NET_PROFIT_PCT (sticky).
  · 매도: 완성봉(5분 우선/없으면 1분) 종가가 고점 대비 동적 트레일 이탈 AND
          수수료·환율 반영 순손익률 >= MIN_NET_PROFIT_PCT 일 때만.
  · highest·활성상태·마지막 확인봉은 재시작 후에도 영속.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import strategies.us_recovery as R

T0 = datetime(2026, 8, 14, 22, 40, 0)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)


def _profit_state(highest=100.0, active=False, high_net=None, last_bar=None):
    s = R.default_state(highest_price=highest)
    s["profit_trail_active"]  = active
    s["profit_high_net_pct"]  = high_net
    s["last_profit_breach_bar_at"] = last_bar
    return s


def _pt(state, net=1.0, cur=100.0, atr=0.0, b1=None, b1c=None, b5=None, b5c=None):
    return R.evaluate_profit_trailing(state, net, cur, atr_pct=atr,
                                      bar1_ts=b1, bar1_close=b1c,
                                      bar5_ts=b5, bar5_close=b5c)


def _ctx(b1=None, b1c=None, b5=None, b5c=None, atr=0.0):
    return {"atr_pct": atr, "bar1_ts": b1, "bar1_close": b1c,
            "bar5_ts": b5, "bar5_close": b5c}


# ══════════════════════════════════════════════════════════════
# 정책 상수
# ══════════════════════════════════════════════════════════════
class PolicyConstantsTest(unittest.TestCase):
    def test_min_net_profit_pct(self):
        self.assertEqual(R.MIN_NET_PROFIT_PCT, 0.3)

    def test_trail_clamp_bounds(self):
        self.assertEqual(R.PROFIT_TRAIL_MIN, 1.0)
        self.assertEqual(R.PROFIT_TRAIL_MAX, 2.5)


# ══════════════════════════════════════════════════════════════
# 손실 구간: 어떤 자동 SELL 도 없다
# ══════════════════════════════════════════════════════════════
class NoLossSellTest(unittest.TestCase):
    def test_buy_then_minus1_holds(self):
        d = R.decide_management_action(R.default_state(highest_price=100.0),
                                       -1.0, 99.0, T0, ctx=_ctx())
        self.assertEqual(d.action, R.ACT_HOLD)
        self.assertFalse(d.sell)

    def test_buy_then_minus5_holds_no_recovery_sell(self):
        d = R.decide_management_action(R.default_state(highest_price=100.0),
                                       -5.0, 95.0, T0, ctx=_ctx(b5="c1", b5c=95.0))
        self.assertEqual(d.action, R.ACT_HOLD)
        self.assertEqual(d.state["management_mode"], R.MODE_NORMAL)   # RECOVERY 진입 안 함

    def test_buy_then_minus10_holds(self):
        d = R.decide_management_action(R.default_state(highest_price=100.0),
                                       -10.0, 90.0, T0, ctx=_ctx(b5="c1", b5c=90.0))
        self.assertFalse(d.sell)

    def test_structural_breakdown_plus_big_loss_still_holds(self):
        # 구조적 5분봉 연속 붕괴 + 큰 손실이어도 손절 없음 → HOLD (정책 비활성화)
        s = R.default_state(highest_price=100.0)
        d = R.decide_management_action(s, -6.0, 94.0, T0, ctx=_ctx(b5="c1", b5c=94.0, atr=1.0))
        d = R.decide_management_action(d.state, -6.5, 93.5, T1,
                                       ctx=_ctx(b5="c2", b5c=93.5, atr=1.0))
        d = R.decide_management_action(d.state, -7.0, 93.0, T2,
                                       ctx=_ctx(b5="c3", b5c=93.0, atr=1.0))
        self.assertFalse(d.sell)

    def test_loss_rebound_then_redrop_holds(self):
        # 손실 중 반등 후 재하락 → 매도 0회(수익 미확보로 트레일 미활성)
        s = R.default_state(highest_price=100.0)
        d = R.decide_management_action(s, -4.0, 96.0, T0, ctx=_ctx())           # 손실
        d = R.decide_management_action(d.state, -1.0, 99.0, T1, ctx=_ctx())     # 반등(여전히 손실)
        d = R.decide_management_action(d.state, -3.0, 97.0, T2,
                                       ctx=_ctx(b1="a", b1c=97.0))              # 재하락
        self.assertFalse(d.sell)
        self.assertFalse(d.state["profit_trail_active"])

    def test_evaluate_never_sells(self):
        # 구 evaluate 는 하위호환용이지만 절대 SELL 하지 않는다
        s = R.default_state(highest_price=100.0)
        d = R.evaluate(s, -8.0, 92.0, T0, atr_pct=1.0, bar5_ts="c1", bar5_close=92.0,
                       symbol_risk_exceeded=True, atr_valid=True)
        self.assertFalse(d.sell)
        self.assertEqual(d.action, R.ACT_HOLD)


# ══════════════════════════════════════════════════════════════
# 활성화: highest_net >= trail_pct + MIN_NET_PROFIT_PCT
# ══════════════════════════════════════════════════════════════
class ActivateTest(unittest.TestCase):
    def test_plus_1_29_not_active(self):
        # trail 1.0(atr0) + 0.3 = 1.3 → 1.29 미활성
        r = _pt(_profit_state(highest=101.29), net=1.29, cur=101.29)
        self.assertFalse(r["state"]["profit_trail_active"])
        self.assertFalse(r["sell"])

    def test_plus_1_30_activates_at_trail_1(self):
        # trail 1.0 + 0.3 = 1.3 → 1.30 활성
        r = _pt(_profit_state(highest=101.30), net=1.30, cur=101.30)
        self.assertTrue(r["state"]["profit_trail_active"])
        self.assertFalse(r["sell"])

    def test_activation_sticky(self):
        r = _pt(_profit_state(highest=101.30), net=1.30, cur=101.30)
        self.assertTrue(r["state"]["profit_trail_active"])
        # 이후 수익률이 내려가도 활성 유지(sticky)
        r2 = _pt(r["state"], net=0.5, cur=100.5)
        self.assertTrue(r2["state"]["profit_trail_active"])

    def test_activation_need_scales_with_atr(self):
        # atr 큼 → trail 2.5 → 활성 need = 2.8. hi=2.0 이면 미활성.
        r = _pt(_profit_state(highest=102.0), net=2.0, cur=102.0, atr=10.0)
        self.assertFalse(r["state"]["profit_trail_active"])
        # hi=2.9 이면 활성
        r2 = _pt(_profit_state(highest=102.9), net=2.9, cur=102.9, atr=10.0)
        self.assertTrue(r2["state"]["profit_trail_active"])


# ══════════════════════════════════════════════════════════════
# 매도: 완성봉 트레일 이탈 AND 순손익 >= 0.3%
# ══════════════════════════════════════════════════════════════
class ProfitSellGateTest(unittest.TestCase):
    def _active(self, highest=102.0):
        return _profit_state(highest=highest, active=True, high_net=3.0)

    def test_no_completed_bar_holds(self):
        r = _pt(self._active(), net=1.0, cur=100.5, b1=None, b1c=None)
        self.assertFalse(r["sell"])
        self.assertIn("no_completed_bar", r["reason"])

    def test_bar_not_below_trail_holds(self):
        # 완성봉 종가가 트레일(고점102×-1%=100.98) 위 → HOLD
        r = _pt(self._active(), net=1.0, cur=101.2, b1="a", b1c=101.2)
        self.assertFalse(r["sell"])

    def test_breach_but_net_0_29_holds(self):
        # 완성봉 이탈(100.7<100.98) but 순손익 +0.29% → HOLD(재상승 대기)
        r = _pt(self._active(), net=0.29, cur=100.54, b1="a", b1c=100.7)
        self.assertFalse(r["sell"])
        self.assertIn("net_below_min", r["reason"])

    def test_breach_and_net_0_30_sells(self):
        # 완성봉 이탈 AND 순손익 +0.30% → SELL
        r = _pt(self._active(), net=0.30, cur=100.55, b1="a", b1c=100.7)
        self.assertTrue(r["sell"])
        self.assertIn("profit_trail_exit", r["reason"])

    def test_bar5_preferred_over_bar1(self):
        # 5분봉이 있으면 우선 사용
        r = _pt(self._active(), net=1.0, cur=101.0, b1="a", b1c=101.5, b5="b5", b5c=100.5)
        self.assertTrue(r["sell"])
        self.assertIn("5m", r["reason"])

    def test_last_confirmed_bar_persisted(self):
        r = _pt(self._active(), net=1.0, cur=101.0, b1="2231", b1c=101.2)  # 미이탈이어도
        self.assertEqual(r["state"]["last_profit_breach_bar_at"], "2231")

    def test_loss_never_sells_even_when_active(self):
        # 활성 상태여도 순손익 음수면 절대 SELL 안 함(트레일 이탈이어도)
        r = _pt(self._active(), net=-0.5, cur=99.0, b1="a", b1c=100.0)
        self.assertFalse(r["sell"])


# ══════════════════════════════════════════════════════════════
# 재시작 영속: highest·활성상태·마지막 확인봉
# ══════════════════════════════════════════════════════════════
class RestartPersistTest(unittest.TestCase):
    def test_restart_preserves_highest_active_lastbar(self):
        r = _pt(_profit_state(highest=102.0), net=1.5, cur=102.0)   # 활성화
        r = _pt(r["state"], net=1.0, cur=101.0, b1="2231", b1c=101.2)
        reloaded = R.merge_state(r["state"])
        self.assertTrue(reloaded["profit_trail_active"])
        self.assertEqual(reloaded["highest_price"], 102.0)
        self.assertEqual(reloaded["last_profit_breach_bar_at"], "2231")

    def test_restart_then_breach_sells(self):
        r = _pt(_profit_state(highest=102.0), net=1.5, cur=102.0)   # 활성화
        reloaded = R.merge_state(r["state"])
        r2 = _pt(reloaded, net=0.4, cur=100.6, b1="2232", b1c=100.7)  # 이탈+순익>0.3
        self.assertTrue(r2["sell"])


# ══════════════════════════════════════════════════════════════
# decide — 단일 매도판정 권위(손절 없음)
# ══════════════════════════════════════════════════════════════
class DecideTest(unittest.TestCase):
    def test_exit_pending_holds(self):
        s = R.default_state(); s["management_mode"] = R.MODE_EXIT
        d = R.decide_management_action(s, 3.0, 105.0, T0, ctx=_ctx(b5="b5", b5c=100.0))
        self.assertEqual(d.action, R.ACT_HOLD)   # EXIT 유지, 재제출 없음
        self.assertEqual(d.state["management_mode"], R.MODE_EXIT)

    def test_normalizes_legacy_recovery_wait(self):
        s = R.default_state(highest_price=100.0)
        s["management_mode"] = R.MODE_RECOVERY_WAIT
        d = R.decide_management_action(s, -4.0, 96.0, T0, ctx=_ctx())
        self.assertEqual(d.state["management_mode"], R.MODE_NORMAL)  # NORMAL 정규화
        self.assertFalse(d.sell)

    def test_profit_exit_via_decide(self):
        s = R.default_state(highest_price=100.0)
        d = R.decide_management_action(s, 1.5, 101.5, T0, ctx=_ctx())   # 활성화(highest=101.5)
        self.assertTrue(d.state["profit_trail_active"])
        # 완성봉 종가 100.4 < 트레일(101.5×0.99=100.485) 이탈 + 순익 +0.5% → SELL
        d2 = R.decide_management_action(d.state, 0.5, 100.6, T1,
                                        ctx=_ctx(b1="a", b1c=100.4))
        self.assertTrue(d2.sell)
        self.assertEqual(d2.action, R.ACT_SELL_ALL)

    def test_never_returns_defer(self):
        for pct in [-5.0, 0.0, 1.5, 2.5, 10.0]:
            d = R.decide_management_action(R.default_state(highest_price=100.0 + pct),
                                           pct, 100.0 + pct, T0, ctx=_ctx())
            self.assertNotEqual(d.action, R.ACT_DEFER)

    def test_highest_tracked_from_entry(self):
        d = R.decide_management_action(R.default_state(highest_price=0.0),
                                       0.5, 100.5, T0, ctx=_ctx())
        self.assertGreaterEqual(d.state["highest_price"], 100.5)


# ══════════════════════════════════════════════════════════════
# 위험 사이징(BUY 수량 축소) — 유지
# ══════════════════════════════════════════════════════════════
class RiskSizingTest(unittest.TestCase):
    def test_caps_at_max_loss(self):
        q = R.risk_capped_qty(100.0, atr_pct=0.0, budget_qty=100, max_loss_usd=30)
        self.assertEqual(q, 10)

    def test_never_exceeds_budget(self):
        q = R.risk_capped_qty(10.0, atr_pct=0.0, budget_qty=5, max_loss_usd=100000)
        self.assertEqual(q, 5)


class AutoSellGateTest(unittest.TestCase):
    """자동 SELL 최종 게이트: 순수익률(%) + 절대금액($) 동시 충족 필요, 손실은 항상 차단."""
    def test_loss_always_blocked(self):
        for cur in (99.0, 95.0, 90.0, 70.0):   # -1/-5/-10/-30%
            ok, m = R.auto_sell_allowed(100.0, cur, 10)
            self.assertFalse(ok)
            self.assertLess(m["net_pct"], 0.3)

    def test_pct_pass_but_usd_below_min_blocked(self):
        # 소수량: +0.30%여도 순익 절대금액 미달이면 차단(체결 미끄러짐 대비)
        ok, m = R.auto_sell_allowed(100.0, 100.55, 1)   # net_pct 0.30, net_usd≈0.30
        self.assertFalse(ok)
        self.assertGreaterEqual(m["net_pct"], 0.3)
        self.assertLess(m["net_usd"], m["required_usd"])

    def test_pct_and_usd_pass_allows(self):
        ok, m = R.auto_sell_allowed(100.0, 100.6, 10)   # net_pct 0.35, net_usd 3.5>=3.0
        self.assertTrue(ok)

    def test_boundary_030pct_qty10(self):
        # net_pct 정확히 0.30, net_usd 정확히 3.0 == required 3.0 → 허용
        ok, m = R.auto_sell_allowed(100.0, 100.55, 10)
        self.assertTrue(ok)
        self.assertAlmostEqual(m["net_usd"], 3.0, places=2)
        self.assertAlmostEqual(m["required_usd"], 3.0, places=2)

    def test_reuses_fee_model(self):
        # 왕복비용 = FEE_ROUND_TRIP_PCT% × (avg×qty), net_pct = gross - fee
        m = R.expected_net_profit(100.0, 102.0, 10)
        self.assertAlmostEqual(m["gross_pct"], 2.0, places=3)
        self.assertAlmostEqual(m["net_pct"], 2.0 - R.FEE_ROUND_TRIP_PCT, places=3)
        self.assertAlmostEqual(m["round_trip_cost_usd"],
                               R.FEE_ROUND_TRIP_PCT / 100.0 * 1000.0, places=3)

    def test_configurable_min_usd(self):
        # 설정된 최소 달러 수익을 높이면 더 큰 순익을 요구
        ok, _ = R.auto_sell_allowed(100.0, 100.6, 10, min_net_usd=100.0)
        self.assertFalse(ok)


class MergeTest(unittest.TestCase):
    def test_legacy_defaults(self):
        m = R.merge_state({"code": "AAPL", "qty": 10})
        self.assertIsNone(m["last_profit_breach_bar_at"])
        self.assertFalse(m["profit_trail_active"])

    def test_preserves_active_and_lastbar(self):
        s = _profit_state(active=True, last_bar="2231")
        m = R.merge_state(s)
        self.assertTrue(m["profit_trail_active"])
        self.assertEqual(m["last_profit_breach_bar_at"], "2231")


if __name__ == "__main__":
    unittest.main()
