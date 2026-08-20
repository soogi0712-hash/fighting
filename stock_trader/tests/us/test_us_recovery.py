"""US 손실 회복 + 수익 트레일링 상태기계 경계 테스트 (결정론적, 부수효과 없음).

새 규칙:
  종가 확인 : 서로 다른 마감 완료 봉(bar1_ts)만 카운트(봉 중복 방지). 미완성봉 None.
  수익 트레일: +1.5 활성 / ATR clamp(1.0~2.5) / 2봉 확정 / EMA9하락·종가<EMA9 1봉 가속 /
              5분봉 마감 즉시 / 추가 0.5%p 급락 즉시. EMA9 상승은 veto 아님.
  손실 회복 : -5 진입 / -3.5 활성 / 고점 동적 트레일(1봉 or 급락) / -6 하드 /
              -2 성공표시(보호유지) / 0% NORMAL 전환.
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


def _recovery_state(high_price=100.0, high_net=-5.0, armed=False, recovered=False):
    s = R.default_state(recovered=recovered)
    s["management_mode"]       = R.MODE_RECOVERY
    s["recovery_started_at"]   = T0.isoformat()
    s["recovery_high_price"]   = high_price
    s["recovery_high_net_pct"] = high_net
    s["recovery_trail_armed"]  = armed
    return s


def _profit_state(highest=100.0, active=False, high_net=None, closes=0,
                  last_bar=None, recovered=False):
    s = R.default_state(recovered=recovered, highest_price=highest)
    s["profit_trail_active"]  = active
    s["profit_high_net_pct"]  = high_net
    s["profit_breach_closes"] = closes
    s["last_profit_breach_bar_at"] = last_bar
    return s


# ══════════════════════════════════════════════════════════════
# 손실 회복 — 진입/활성/트레일/하드/보호연속성
# ══════════════════════════════════════════════════════════════
class RecoveryEntryTest(unittest.TestCase):
    def test_enter_minus_4_99_stays_normal(self):
        d = R.evaluate(R.default_state(), net_pct=-4.99, cur_price=95.01, now=T0)
        self.assertEqual(d.mode, R.MODE_NORMAL)
        self.assertFalse(d.sell)

    def test_enter_exactly_minus_5(self):
        d = R.evaluate(R.default_state(highest_price=100.0), net_pct=-5.0,
                       cur_price=95.0, now=T0)
        self.assertEqual(d.mode, R.MODE_RECOVERY)
        self.assertFalse(d.sell)
        self.assertFalse(d.state["recovery_trail_armed"])

    def test_no_immediate_high_drop_sell(self):
        d1 = R.evaluate(R.default_state(highest_price=100.0), net_pct=-5.0,
                        cur_price=100.0, now=T0)
        d2 = R.evaluate(d1.state, net_pct=-5.6, cur_price=99.3, now=T1)  # -0.7% from high
        self.assertFalse(d2.sell)   # -0.7 즉시매도 규칙 삭제


class RecoveryArmTest(unittest.TestCase):
    def test_minus_4_0_not_armed(self):
        d = R.evaluate(_recovery_state(96.0), net_pct=-4.0, cur_price=96.0, now=T1)
        self.assertFalse(d.state["recovery_trail_armed"])

    def test_minus_3_5_arms(self):
        d = R.evaluate(_recovery_state(96.5), net_pct=-3.5, cur_price=96.5, now=T1)
        self.assertTrue(d.state["recovery_trail_armed"])

    def test_small_rebound_not_arm(self):
        d = R.evaluate(_recovery_state(95.8), net_pct=-4.2, cur_price=95.8, now=T1)
        self.assertFalse(d.state["recovery_trail_armed"])


class RecoveryTrailTest(unittest.TestCase):
    def test_high_drop_1_19_hold(self):
        d = R.evaluate(_recovery_state(100.0, armed=True), net_pct=-3.0,
                       cur_price=98.81, now=T1, atr_pct=0.0, bar1_ts="b1")
        self.assertFalse(d.sell)   # -1.19% > -1.2% → 미이탈

    def test_high_drop_1_20_confirmed_bar_sells(self):
        # -1.20% 이탈 + 완료봉 1개 → SELL (RECOVERY_TRAIL_CONFIRM_BARS=1)
        d = R.evaluate(_recovery_state(100.0, armed=True), net_pct=-3.0,
                       cur_price=98.80, now=T1, atr_pct=0.0, bar1_ts="b1")
        self.assertTrue(d.sell)
        self.assertEqual(d.action, R.ACT_SELL_ALL)

    def test_high_drop_incomplete_bar_holds(self):
        # 이탈이지만 미완성봉(None) → 카운트 안 함 → HOLD
        d = R.evaluate(_recovery_state(100.0, armed=True), net_pct=-3.0,
                       cur_price=98.80, now=T1, atr_pct=0.0, bar1_ts=None)
        self.assertFalse(d.sell)

    def test_not_armed_no_sell(self):
        d = R.evaluate(_recovery_state(100.0, armed=False), net_pct=-4.5,
                       cur_price=98.0, now=T1, atr_pct=0.0, bar1_ts="b1")
        self.assertFalse(d.sell)

    def test_panic_extra_half_immediate(self):
        # trail=1.2, 추가 0.5 → -1.7% 이상 급락 → 봉 없이 즉시 SELL
        d = R.evaluate(_recovery_state(100.0, armed=True), net_pct=-3.0,
                       cur_price=98.2, now=T1, atr_pct=0.0, bar1_ts=None)  # -1.8%
        self.assertTrue(d.sell)
        self.assertIn("panic", d.reason)

    def test_trail_atr_dynamic(self):
        d = R.evaluate(_recovery_state(100.0, armed=True), net_pct=-3.0,
                       cur_price=98.0, now=T1, atr_pct=5.0, bar1_ts="b1")  # trail=2.5
        self.assertFalse(d.sell)   # -2.0 > -2.5


class RecoveryHardTest(unittest.TestCase):
    def test_hard_stop_minus_6(self):
        d = R.evaluate(_recovery_state(100.0), net_pct=-6.0, cur_price=94.0, now=T1)
        self.assertTrue(d.sell)
        self.assertIn("hard_stop", d.reason)

    def test_no_rebound_straight_minus_6(self):
        d1 = R.evaluate(R.default_state(highest_price=100.0), net_pct=-5.0,
                        cur_price=95.0, now=T0)
        d2 = R.evaluate(d1.state, net_pct=-6.0, cur_price=94.0, now=T1)
        self.assertTrue(d2.sell)


class RecoveryContinuityTest(unittest.TestCase):
    def test_minus_2_keeps_protection_not_normal(self):
        # -2 회복은 성공표시만, RECOVERY 유지(보호 계속)
        d = R.evaluate(_recovery_state(100.0, armed=True), net_pct=-2.0,
                       cur_price=98.5, now=T1)
        self.assertEqual(d.mode, R.MODE_RECOVERY)
        self.assertTrue(d.state["recovery_reached_exit"])
        self.assertEqual(d.state["recovery_high_price"], 100.0)   # 보호 유지

    def test_minus5_minus3_minus2_then_drop_protects(self):
        # -5 진입 → -3 활성 → -2 성공 → 다시 하락 시 recovery high 보호로 SELL
        d = R.evaluate(R.default_state(highest_price=100.0), net_pct=-5.0,
                       cur_price=95.0, now=T0)                     # 진입, high=95
        d = R.evaluate(d.state, net_pct=-3.0, cur_price=97.0, now=T1)   # arm, high=97
        self.assertTrue(d.state["recovery_trail_armed"])
        d = R.evaluate(d.state, net_pct=-2.0, cur_price=98.0, now=T2)   # 성공, high=98
        self.assertEqual(d.mode, R.MODE_RECOVERY)
        self.assertEqual(d.state["recovery_high_price"], 98.0)
        # 다시 하락: 98 대비 -1.5% (96.53) + 완료봉 → SELL (보호 유지 확인)
        d = R.evaluate(d.state, net_pct=-3.7, cur_price=96.5, now=T2 + timedelta(minutes=1),
                       atr_pct=0.0, bar1_ts="bX")
        self.assertTrue(d.sell)

    def test_zero_recovers_to_normal(self):
        d = R.evaluate(_recovery_state(100.0, armed=True), net_pct=0.0,
                       cur_price=100.5, now=T1)
        self.assertEqual(d.mode, R.MODE_NORMAL)
        self.assertIsNone(d.state["recovery_high_price"])
        self.assertFalse(d.state["recovery_trail_armed"])

    def test_recovery_high_monotonic(self):
        s = _recovery_state(100.0, armed=True)
        d = R.evaluate(s, net_pct=-3.0, cur_price=97.0, now=T1)
        self.assertEqual(d.state["recovery_high_price"], 100.0)
        d2 = R.evaluate(d.state, net_pct=-1.0, cur_price=103.0, now=T2)  # 신고점→NORMAL? net-1<0? no
        # net=-1 <0 이므로 아직 RECOVERY, high=103 갱신
        self.assertEqual(d2.state["recovery_high_price"], 103.0)


# ══════════════════════════════════════════════════════════════
# 수익 트레일링 — 활성/ATR/봉확정/EMA9/급락/5분
# ══════════════════════════════════════════════════════════════
class ProfitActivateTest(unittest.TestCase):
    def test_plus_1_49_not_active(self):
        r = R.evaluate_profit_trailing(_profit_state(), net_pct=1.49, cur_price=101.49,
                                       atr_pct=0.0, ema9=100.0, ema9_rising=True)
        self.assertFalse(r["state"]["profit_trail_active"])
        self.assertFalse(r["sell"])

    def test_plus_1_50_activates_no_sell(self):
        r = R.evaluate_profit_trailing(_profit_state(highest=101.5), net_pct=1.50,
                                       cur_price=101.5, atr_pct=0.0, ema9=100.0,
                                       ema9_rising=True)
        self.assertTrue(r["state"]["profit_trail_active"])
        self.assertFalse(r["sell"])


class ProfitBarConfirmTest(unittest.TestCase):
    # 정상(EMA9 상승+위) 경로: 서로 다른 1분봉 2개 확정 필요
    def _st(self):
        return _profit_state(highest=100.0, active=True, high_net=3.0)

    def test_same_bar_10x_count_stays_1(self):
        s = self._st()
        for _ in range(10):
            r = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.8, atr_pct=0.0,
                                           ema9=98.0, ema9_rising=True, bar1_ts="b1")
            s = r["state"]
            self.assertFalse(r["sell"])              # 동일 봉 → 미확정
        self.assertEqual(s["profit_breach_closes"], 1)   # 카운트 1 고정

    def test_two_distinct_bars_sell(self):
        s = self._st()
        r1 = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.8, atr_pct=0.0,
                                        ema9=98.0, ema9_rising=True, bar1_ts="b1")
        self.assertFalse(r1["sell"])
        r2 = R.evaluate_profit_trailing(r1["state"], net_pct=1.0, cur_price=98.8,
                                        atr_pct=0.0, ema9=98.0, ema9_rising=True,
                                        bar1_ts="b2")
        self.assertTrue(r2["sell"])                  # 서로 다른 2봉 → SELL

    def test_incomplete_bar_holds(self):
        s = self._st()
        r = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.8, atr_pct=0.0,
                                       ema9=98.0, ema9_rising=True, bar1_ts=None)
        self.assertFalse(r["sell"])
        self.assertEqual(r["state"]["profit_breach_closes"], 0)   # 미완성봉 미카운트

    def test_restart_second_bar_sells(self):
        s = self._st()
        r1 = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.8, atr_pct=0.0,
                                        ema9=98.0, ema9_rising=True, bar1_ts="b1")
        # 재시작 시뮬: merge_state 로 저장·복원(마지막 확인봉·카운터 유지)
        reloaded = R.merge_state(r1["state"])
        self.assertEqual(reloaded["profit_breach_closes"], 1)
        self.assertEqual(reloaded["last_profit_breach_bar_at"], "b1")
        r2 = R.evaluate_profit_trailing(reloaded, net_pct=1.0, cur_price=98.8,
                                        atr_pct=0.0, ema9=98.0, ema9_rising=True,
                                        bar1_ts="b2")
        self.assertTrue(r2["sell"])

    def test_ema9_rising_not_veto_two_bars_sell(self):
        # EMA9 상승 중이어도(cur>ema9) 확정봉 2개면 SELL (veto 아님)
        s = self._st()
        r1 = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.8, atr_pct=0.0,
                                        ema9=98.0, ema9_rising=True, bar1_ts="b1")
        r2 = R.evaluate_profit_trailing(r1["state"], net_pct=1.0, cur_price=98.8,
                                        atr_pct=0.0, ema9=98.0, ema9_rising=True,
                                        bar1_ts="b2")
        self.assertTrue(r2["sell"])

    def test_ema9_down_one_bar_fast_sell(self):
        # EMA9 하락(또는 종가<EMA9) → 확정봉 1개로 빠른 SELL
        s = self._st()
        r = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.8, atr_pct=0.0,
                                       ema9=99.5, ema9_rising=False, bar1_ts="b1")
        self.assertTrue(r["sell"])
        self.assertIn("fast", r["reason"])

    def test_bar5_close_immediate(self):
        s = self._st()
        r = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.8, atr_pct=0.0,
                                       ema9=98.0, ema9_rising=True, bar1_ts=None,
                                       bar5_ts="b5")
        self.assertTrue(r["sell"])
        self.assertIn("bar5", r["reason"])

    def test_panic_extra_half_immediate(self):
        # trail=1.0(atr0), 추가 0.5 → -1.5% 이상 급락 → 봉 없이 즉시 SELL
        s = self._st()
        r = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.4, atr_pct=0.0,
                                       ema9=98.0, ema9_rising=True, bar1_ts=None)  # -1.6%
        self.assertTrue(r["sell"])
        self.assertIn("panic", r["reason"])

    def test_bounce_resets_counter(self):
        s = _profit_state(highest=100.0, active=True, high_net=3.0, closes=1, last_bar="b1")
        r = R.evaluate_profit_trailing(s, net_pct=2.5, cur_price=100.0, atr_pct=0.0,
                                       ema9=99.0, ema9_rising=True, bar1_ts="b2")
        self.assertEqual(r["state"]["profit_breach_closes"], 0)   # 미이탈 → 리셋
        self.assertIsNone(r["state"]["last_profit_breach_bar_at"])


class ProfitTrailWidthTest(unittest.TestCase):
    def test_atr_clamp_min_max(self):
        # atr=0 → 1.0, atr 큼 → 2.5
        s = _profit_state(highest=100.0, active=True, high_net=3.0)
        r_min = R.evaluate_profit_trailing(s, 1.0, 98.9, atr_pct=0.0, ema9=99.5,
                                           ema9_rising=False, bar1_ts="a")  # -1.1<=-1.0 fast
        self.assertTrue(r_min["sell"])
        s2 = _profit_state(highest=100.0, active=True, high_net=3.0)
        r_max = R.evaluate_profit_trailing(s2, 1.0, 98.0, atr_pct=10.0, ema9=99.5,
                                           ema9_rising=False, bar1_ts="a")  # -2.0>-2.5 미이탈
        self.assertFalse(r_max["sell"])


# ══════════════════════════════════════════════════════════════
# decide — 단일 매도판정 권위(DEFER 없음), 전환
# ══════════════════════════════════════════════════════════════
class DecideTest(unittest.TestCase):
    def _ctx(self, atr_pct=0.0, ema9=None, rising=False, bar1="b1", bar5=None):
        return {"atr_pct": atr_pct, "ema9": ema9, "ema9_rising": rising,
                "bar1_ts": bar1, "bar5_ts": bar5}

    def test_exit_pending_holds(self):
        s = R.default_state(); s["management_mode"] = R.MODE_EXIT
        d = R.decide_management_action(s, 3.0, 105.0, T0, ctx=self._ctx())
        self.assertEqual(d.action, R.ACT_HOLD)

    def test_net_minus5_enters_recovery(self):
        d = R.decide_management_action(R.default_state(highest_price=100.0),
                                       -5.0, 95.0, T0, ctx=self._ctx())
        self.assertEqual(d.state["management_mode"], R.MODE_RECOVERY)

    def test_new_position_holds_not_defer(self):
        # 신규·비활성 → HOLD(단일 권위). DEFER 아님.
        d = R.decide_management_action(R.default_state(highest_price=100.0),
                                       0.5, 100.5, T0, ctx=self._ctx())
        self.assertEqual(d.action, R.ACT_HOLD)
        self.assertIn("single_sell_authority", d.reason)

    def test_never_returns_defer_across_range(self):
        # +1.5~+10 구간 어디서도 DEFER 없음(고정익절 위임 금지)
        for pct in [1.5, 2.0, 2.5, 3.0, 5.0, 7.0, 10.0]:
            d = R.decide_management_action(R.default_state(highest_price=100.0 + pct),
                                           pct, 100.0 + pct, T0, ctx=self._ctx())
            self.assertNotEqual(d.action, R.ACT_DEFER)

    def test_recovery_to_normal_then_profit_trailing(self):
        # 0% 회복 → NORMAL, 이후 +1.5 도달 → profit trailing 전환
        s = _recovery_state(100.0, armed=True)
        d = R.decide_management_action(s, 0.0, 100.5, T0, ctx=self._ctx())  # → NORMAL
        self.assertEqual(d.state["management_mode"], R.MODE_NORMAL)
        d2 = R.decide_management_action(d.state, 1.6, 101.6, T1, ctx=self._ctx())
        self.assertTrue(d2.state["profit_trail_active"])   # profit trailing 전환
        self.assertIn("profit_trailing", d2.reason)


class MergeTest(unittest.TestCase):
    def test_legacy_defaults(self):
        m = R.merge_state({"code": "AAPL", "qty": 10})
        self.assertEqual(m["management_mode"], R.MODE_NORMAL)
        self.assertFalse(m["recovery_trail_armed"])
        self.assertEqual(m["profit_breach_closes"], 0)
        self.assertIsNone(m["last_profit_breach_bar_at"])
        self.assertEqual(m["recovery_breach_closes"], 0)

    def test_preserves_breach_bars(self):
        s = _profit_state(active=True, closes=1, last_bar="b1")
        m = R.merge_state(s)
        self.assertEqual(m["profit_breach_closes"], 1)
        self.assertEqual(m["last_profit_breach_bar_at"], "b1")


if __name__ == "__main__":
    unittest.main()
