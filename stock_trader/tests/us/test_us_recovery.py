"""US 손실 회복 + 수익 트레일링 상태기계 경계 테스트 (결정론적, 부수효과 없음).

새 규칙:
  수익 트레일링: +1.5% 활성 / ATR 동적 폭 clamp(1.0~2.5) / 종가 2회 확정 /
                 EMA9 상승+위 HOLD / 트레일이탈+EMA9 하향이탈 → SELL.
  손실 회복    : -5 진입 / -0.7 즉시매도 삭제 / -3.5 회복해야 트레일 활성 /
                 활성 후 고점 -1.2%(또는 ATR) 이탈 → SELL / -6 하드손절 / -2 NORMAL.
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


def _profit_state(highest=100.0, active=False, high_net=None, closes=0, recovered=False):
    s = R.default_state(recovered=recovered, highest_price=highest)
    s["profit_trail_active"]  = active
    s["profit_high_net_pct"]  = high_net
    s["profit_breach_closes"] = closes
    return s


# ══════════════════════════════════════════════════════════════
# 손실 회복 — 진입/활성/트레일/하드/복귀
# ══════════════════════════════════════════════════════════════
class RecoveryEntryTest(unittest.TestCase):
    def test_enter_minus_4_99_stays_normal(self):
        d = R.evaluate(R.default_state(), net_pct=-4.99, cur_price=95.01, now=T0)
        self.assertEqual(d.mode, R.MODE_NORMAL)
        self.assertFalse(d.sell)

    def test_enter_exactly_minus_5_enters_recovery_no_sell(self):
        d = R.evaluate(R.default_state(highest_price=100.0), net_pct=-5.0,
                       cur_price=95.0, now=T0)
        self.assertEqual(d.mode, R.MODE_RECOVERY)
        self.assertFalse(d.sell)                       # 즉시매도 금지
        self.assertFalse(d.state["recovery_trail_armed"])
        self.assertEqual(d.state["recovery_high_price"], 95.0)

    def test_no_immediate_high_drop_sell_after_entry(self):
        # 진입 직후 고점 대비 -0.7% 하락해도 매도하지 않는다(규칙 삭제 확인)
        d1 = R.evaluate(R.default_state(highest_price=100.0), net_pct=-5.0,
                        cur_price=100.0, now=T0)
        # recovery_high=100, 이후 99.3 (-0.7%) → 매도 없어야 함(미활성)
        d2 = R.evaluate(d1.state, net_pct=-5.6, cur_price=99.3, now=T1)
        self.assertFalse(d2.sell)


class RecoveryArmTest(unittest.TestCase):
    def test_minus_4_0_not_armed(self):
        # -4.0 에서는 recovery trailing 비활성
        d = R.evaluate(_recovery_state(high_price=96.0), net_pct=-4.0,
                       cur_price=96.0, now=T1)
        self.assertFalse(d.state["recovery_trail_armed"])
        self.assertFalse(d.sell)

    def test_minus_3_5_arms(self):
        # -3.5 회복 → 활성화
        d = R.evaluate(_recovery_state(high_price=96.5), net_pct=-3.5,
                       cur_price=96.5, now=T1)
        self.assertTrue(d.state["recovery_trail_armed"])

    def test_small_rebound_does_not_arm(self):
        # -5 → -4.2 작은 반등만으로는 활성 안 됨
        d = R.evaluate(_recovery_state(high_price=95.8), net_pct=-4.2,
                       cur_price=95.8, now=T1)
        self.assertFalse(d.state["recovery_trail_armed"])


class RecoveryTrailTest(unittest.TestCase):
    def test_armed_high_drop_minus_1_19_hold(self):
        # 활성 상태, ATR=0 → trail=1.2. 고점100 대비 -1.19% → HOLD
        s = _recovery_state(high_price=100.0, armed=True)
        d = R.evaluate(s, net_pct=-3.0, cur_price=98.81, now=T1, atr_pct=0.0)
        self.assertFalse(d.sell)

    def test_armed_high_drop_minus_1_20_sell(self):
        # 활성 상태, ATR=0 → trail=1.2. 고점100 대비 -1.20% → SELL
        s = _recovery_state(high_price=100.0, armed=True)
        d = R.evaluate(s, net_pct=-3.0, cur_price=98.80, now=T1, atr_pct=0.0)
        self.assertTrue(d.sell)
        self.assertEqual(d.action, R.ACT_SELL_ALL)

    def test_not_armed_no_trail_sell(self):
        # 미활성이면 고점 -2% 하락도 매도 안 함
        s = _recovery_state(high_price=100.0, armed=False)
        d = R.evaluate(s, net_pct=-4.5, cur_price=98.0, now=T1, atr_pct=0.0)
        self.assertFalse(d.sell)

    def test_trail_width_atr_dynamic(self):
        # ATR% 큰 경우 trail 폭이 넓어져(clamp 2.5) 작은 이탈은 HOLD
        s = _recovery_state(high_price=100.0, armed=True)
        d = R.evaluate(s, net_pct=-3.0, cur_price=98.0, now=T1, atr_pct=5.0)  # trail=2.5
        self.assertFalse(d.sell)   # -2.0% > -2.5% → HOLD


class RecoveryHardExitTest(unittest.TestCase):
    def test_hard_stop_minus_6_sell(self):
        s = _recovery_state(high_price=100.0)
        d = R.evaluate(s, net_pct=-6.0, cur_price=94.0, now=T1)
        self.assertTrue(d.sell)
        self.assertIn("hard_stop", d.reason)

    def test_hard_stop_priority_over_arm(self):
        # -6 이면 armed 여부·트레일 무관하게 하드손절
        s = _recovery_state(high_price=100.0, armed=True)
        d = R.evaluate(s, net_pct=-6.5, cur_price=93.0, now=T1)
        self.assertTrue(d.sell)

    def test_exit_minus_2_returns_normal(self):
        s = _recovery_state(high_price=100.0, armed=True)
        d = R.evaluate(s, net_pct=-2.0, cur_price=98.0, now=T1)
        self.assertEqual(d.mode, R.MODE_NORMAL)
        self.assertFalse(d.sell)
        self.assertFalse(d.state["recovery_trail_armed"])

    def test_no_rebound_straight_to_minus_6(self):
        # 진입(-5, HOLD) → 반등 없이 -6 → SELL (2 루프)
        d1 = R.evaluate(R.default_state(highest_price=100.0), net_pct=-5.0,
                        cur_price=95.0, now=T0)
        self.assertFalse(d1.sell)
        d2 = R.evaluate(d1.state, net_pct=-6.0, cur_price=94.0, now=T1)
        self.assertTrue(d2.sell)


class RecoveryMonotonicTest(unittest.TestCase):
    def test_recovery_high_never_drops(self):
        s = _recovery_state(high_price=100.0, armed=True)
        d = R.evaluate(s, net_pct=-3.0, cur_price=97.0, now=T1)  # cur<high
        self.assertEqual(d.state["recovery_high_price"], 100.0)  # 유지
        d2 = R.evaluate(d.state, net_pct=-2.5, cur_price=103.0, now=T2)  # 신고점
        self.assertEqual(d2.state["recovery_high_price"], 103.0)


# ══════════════════════════════════════════════════════════════
# 수익 트레일링 — 활성/ATR clamp/종가확정/EMA9
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
        self.assertFalse(r["sell"])   # 활성만, 매도 없음

    def test_activation_sticky(self):
        # 활성 후 net 이 +1.5 아래로 내려가도 active 유지
        s = _profit_state(active=True, high_net=2.0)
        r = R.evaluate_profit_trailing(s, net_pct=0.5, cur_price=100.0, atr_pct=0.0,
                                       ema9=100.0, ema9_rising=True)
        self.assertTrue(r["state"]["profit_trail_active"])


class ProfitTrailWidthTest(unittest.TestCase):
    def _breach_once(self, atr_pct, drop_to_pct):
        # 활성 상태, highest=100, ema9 하향이탈 조건 + cur below → 1회 이탈
        s = _profit_state(highest=100.0, active=True, high_net=3.0, closes=1)  # 이미 1회
        cur = 100.0 * (1 + drop_to_pct / 100.0)
        return R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=cur,
                                          atr_pct=atr_pct, ema9=cur + 1.0,
                                          ema9_rising=False)

    def test_atr_zero_clamps_min_1_0(self):
        # trail=1.0. drop -0.99 → HOLD, -1.00 → 확정(closes 2)→ SELL
        r_hold = self._breach_once(0.0, -0.99)
        self.assertFalse(r_hold["sell"])
        self.assertEqual(r_hold["state"]["profit_breach_closes"], 0)  # 리셋
        r_sell = self._breach_once(0.0, -1.00)
        self.assertTrue(r_sell["sell"])

    def test_atr_large_clamps_max_2_5(self):
        # ATR 매우 큼 → trail=2.5. drop -2.0 → HOLD(폭 미달)
        r = self._breach_once(10.0, -2.0)
        self.assertFalse(r["sell"])

    def test_atr_mid(self):
        # ATR%=1.0 → trail=1.5. drop -1.6 → 이탈, closes=2 → SELL
        r = self._breach_once(1.0, -1.6)
        self.assertTrue(r["sell"])


class ProfitCloseConfirmTest(unittest.TestCase):
    def test_single_tick_breach_holds(self):
        # 첫 이탈(closes 0→1) → HOLD (단일 순간 이탈)
        s = _profit_state(highest=100.0, active=True, high_net=3.0, closes=0)
        r = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.5,
                                       atr_pct=0.0, ema9=99.5, ema9_rising=False)
        self.assertFalse(r["sell"])
        self.assertEqual(r["state"]["profit_breach_closes"], 1)

    def test_two_closes_confirm_sell(self):
        s = _profit_state(highest=100.0, active=True, high_net=3.0, closes=0)
        r1 = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.5,
                                        atr_pct=0.0, ema9=99.5, ema9_rising=False)
        self.assertFalse(r1["sell"])
        r2 = R.evaluate_profit_trailing(r1["state"], net_pct=1.0, cur_price=98.5,
                                        atr_pct=0.0, ema9=99.5, ema9_rising=False)
        self.assertTrue(r2["sell"])   # 2회 연속 종가 확정

    def test_bounce_resets_counter(self):
        s = _profit_state(highest=100.0, active=True, high_net=3.0, closes=1)
        # 반등(고점 위 회복) → 카운터 리셋
        r = R.evaluate_profit_trailing(s, net_pct=2.5, cur_price=100.0,
                                       atr_pct=0.0, ema9=99.0, ema9_rising=False)
        self.assertEqual(r["state"]["profit_breach_closes"], 0)

    def test_bar5_close_confirms_immediately(self):
        s = _profit_state(highest=100.0, active=True, high_net=3.0, closes=0)
        r = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.5, atr_pct=0.0,
                                       ema9=99.5, ema9_rising=False, bar5_close=True)
        self.assertTrue(r["sell"])   # 5분봉 종가 → 즉시 확정


class ProfitEMA9Test(unittest.TestCase):
    def test_ema9_rising_above_holds(self):
        # EMA9 상승 + 현재가 > EMA9 → 트레일 이탈이어도 HOLD
        s = _profit_state(highest=100.0, active=True, high_net=3.0, closes=1)
        r = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.0, atr_pct=0.0,
                                       ema9=97.0, ema9_rising=True)  # cur>ema9
        self.assertFalse(r["sell"])
        self.assertEqual(r["state"]["profit_breach_closes"], 0)

    def test_breach_needs_ema9_downward(self):
        # 트레일 이탈이지만 현재가 >= EMA9(하향이탈 아님) → 카운트 안 함
        s = _profit_state(highest=100.0, active=True, high_net=3.0, closes=1)
        r = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.0, atr_pct=0.0,
                                       ema9=97.5, ema9_rising=False)  # cur>ema9
        self.assertFalse(r["sell"])
        self.assertEqual(r["state"]["profit_breach_closes"], 0)

    def test_trail_plus_ema9_down_confirmed_sells(self):
        s = _profit_state(highest=100.0, active=True, high_net=3.0, closes=1)
        r = R.evaluate_profit_trailing(s, net_pct=1.0, cur_price=98.0, atr_pct=0.0,
                                       ema9=99.0, ema9_rising=False)  # cur<ema9
        self.assertTrue(r["sell"])


# ══════════════════════════════════════════════════════════════
# decide_management_action 통합 우선순위
# ══════════════════════════════════════════════════════════════
class DecideTest(unittest.TestCase):
    def _ctx(self, atr_pct=0.0, ema9=None, rising=False):
        return {"atr_pct": atr_pct, "ema9": ema9, "ema9_rising": rising}

    def test_exit_pending_holds(self):
        s = R.default_state(); s["management_mode"] = R.MODE_EXIT
        d = R.decide_management_action(s, 3.0, 105.0, T0, ctx=self._ctx())
        self.assertEqual(d.action, R.ACT_HOLD)

    def test_net_minus_5_enters_recovery(self):
        d = R.decide_management_action(R.default_state(highest_price=100.0),
                                       -5.0, 95.0, T0, ctx=self._ctx())
        self.assertEqual(d.state["management_mode"], R.MODE_RECOVERY)
        self.assertFalse(d.sell)

    def test_profit_trailing_governs_when_active(self):
        # 최고 net +2 → 활성 → 트레일+EMA9하향 2회 확정 SELL
        s = _profit_state(highest=100.0, active=True, high_net=2.0, closes=1)
        d = R.decide_management_action(s, 1.0, 98.0, T0,
                                       ctx=self._ctx(ema9=99.0, rising=False))
        self.assertTrue(d.sell)
        self.assertIn("profit_trailing_exit", d.reason)

    def test_new_position_defers_when_not_active(self):
        # 신규(비복원), 트레일 미활성, net 낮음 → DEFER(기존 로직)
        d = R.decide_management_action(R.default_state(highest_price=100.0),
                                       0.5, 100.5, T0, ctx=self._ctx())
        self.assertEqual(d.action, R.ACT_DEFER)

    def test_recovered_holds_when_not_active(self):
        # 복원 포지션, 트레일 미활성 → HOLD(고정익절 면제)
        s = R.default_state(recovered=True, highest_price=100.0)
        d = R.decide_management_action(s, 2.4, 102.4, T0, ctx=self._ctx())
        # net +2.4 이나 highest=102.4 활성화(+2.4>=1.5) → 트레일 지배 HOLD
        self.assertEqual(d.action, R.ACT_HOLD)
        self.assertFalse(d.sell)

    def test_recovered_not_profit_holds_no_defer(self):
        s = R.default_state(recovered=True, highest_price=100.0)
        d = R.decide_management_action(s, 0.5, 100.0, T0, ctx=self._ctx())
        self.assertEqual(d.action, R.ACT_HOLD)   # 복원 → 면제 HOLD, DEFER 아님


# ══════════════════════════════════════════════════════════════
# 상태 병합/영속 안전
# ══════════════════════════════════════════════════════════════
class MergeTest(unittest.TestCase):
    def test_legacy_defaults(self):
        m = R.merge_state({"code": "AAPL", "qty": 10})
        self.assertEqual(m["management_mode"], R.MODE_NORMAL)
        self.assertFalse(m["recovery_trail_armed"])
        self.assertEqual(m["profit_breach_closes"], 0)

    def test_preserves_recovery_state(self):
        s = _recovery_state(high_price=105.0, armed=True)
        m = R.merge_state(s)
        self.assertEqual(m["management_mode"], R.MODE_RECOVERY)
        self.assertTrue(m["recovery_trail_armed"])
        self.assertEqual(m["recovery_high_price"], 105.0)


if __name__ == "__main__":
    unittest.main()
