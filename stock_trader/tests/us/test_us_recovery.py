"""US 손실 회복 + 수익 트레일링 상태기계 — 실제 완료봉 기반 종가확정 테스트.

핵심(§1~§5):
  breach 판정은 '실제 마감 완료봉 종가(bar1_close/bar5_close)'로, 급락 안전매도와
  하드손절은 '실시간 가격(cur_price/net)'로. 완료봉은 연속으로 세고 정상봉이 끼면
  초기화. 동일 봉 중복/늦은 정정/역행/데이터 없음을 안전 처리.
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


def _recovery_state(high_price=100.0, high_net=-5.0, armed=False):
    s = R.default_state()
    s["management_mode"]       = R.MODE_RECOVERY
    s["recovery_started_at"]   = T0.isoformat()
    s["recovery_high_price"]   = high_price
    s["recovery_high_net_pct"] = high_net
    s["recovery_trail_armed"]  = armed
    return s


def _profit_state(highest=100.0, active=False, high_net=None, closes=0,
                  prev=0, last_bar=None):
    s = R.default_state(highest_price=highest)
    s["profit_trail_active"]  = active
    s["profit_high_net_pct"]  = high_net
    s["profit_breach_closes"] = closes
    s["profit_prev_breach_closes"] = prev
    s["last_profit_breach_bar_at"] = last_bar
    return s


def _pt(state, net=1.0, cur=None, atr=0.0, ema9=98.0, rising=True,
        b1=None, b1c=None, b5=None, b5c=None):
    # cur 기본 = bar close(급락 아님). breach 는 bar close 로 판정.
    if cur is None:
        cur = b1c if b1c is not None else 100.0
    return R.evaluate_profit_trailing(state, net, cur, atr_pct=atr, ema9=ema9,
                                      ema9_rising=rising, bar1_ts=b1, bar1_close=b1c,
                                      bar5_ts=b5, bar5_close=b5c)


# ══════════════════════════════════════════════════════════════
# 손실 회복 — 진입/활성/트레일(완료봉)/하드/급락/보호연속성
# ══════════════════════════════════════════════════════════════
class RecoveryEntryTest(unittest.TestCase):
    def test_enter_minus_4_99_stays_normal(self):
        d = R.evaluate(R.default_state(), -4.99, 95.01, T0)
        self.assertEqual(d.mode, R.MODE_NORMAL)

    def test_enter_exactly_minus_5(self):
        d = R.evaluate(R.default_state(highest_price=100.0), -5.0, 95.0, T0)
        self.assertEqual(d.mode, R.MODE_RECOVERY)
        self.assertFalse(d.sell)


class RecoveryArmTest(unittest.TestCase):
    def test_minus_4_0_not_armed(self):
        d = R.evaluate(_recovery_state(96.0), -4.0, 96.0, T1)
        self.assertFalse(d.state["recovery_trail_armed"])

    def test_minus_3_5_arms(self):
        d = R.evaluate(_recovery_state(96.5), -3.5, 96.5, T1)
        self.assertTrue(d.state["recovery_trail_armed"])


class RecoveryTrailTest(unittest.TestCase):
    def test_high_drop_1_19_hold(self):
        # 완료봉 종가 -1.19% > -1.2% → 미이탈 → HOLD
        d = R.evaluate(_recovery_state(100.0, armed=True), -3.0, 98.81, T1,
                       atr_pct=0.0, bar1_ts="b1", bar1_close=98.81)
        self.assertFalse(d.sell)

    def test_high_drop_1_20_completed_bar_sells(self):
        # 완료봉 종가 -1.20% <= -1.2% + 완료봉 1개 → SELL
        d = R.evaluate(_recovery_state(100.0, armed=True), -3.0, 98.80, T1,
                       atr_pct=0.0, bar1_ts="b1", bar1_close=98.80)
        self.assertTrue(d.sell)

    def test_incomplete_bar_holds(self):
        # 완료봉 없음(bar1=None)이나 실시간가만 이탈 → 확정봉 매도 보류(HOLD)
        d = R.evaluate(_recovery_state(100.0, armed=True), -3.0, 98.80, T1,
                       atr_pct=0.0, bar1_ts=None, bar1_close=None)
        self.assertFalse(d.sell)

    def test_panic_live_price_immediate(self):
        # 실시간가 급락(추가 0.5%p) → 봉 없이 즉시 SELL
        d = R.evaluate(_recovery_state(100.0, armed=True), -3.0, 98.2, T1,
                       atr_pct=0.0, bar1_ts=None, bar1_close=None)  # live -1.8%
        self.assertTrue(d.sell)
        self.assertIn("panic", d.reason)

    def test_not_armed_no_sell(self):
        d = R.evaluate(_recovery_state(100.0, armed=False), -4.5, 98.0, T1,
                       atr_pct=0.0, bar1_ts="b1", bar1_close=98.0)
        self.assertFalse(d.sell)


class RecoveryHardTest(unittest.TestCase):
    def test_hard_stop_minus_6_live(self):
        # 데이터 없어도 실시간 net<=-6 → 하드손절 SELL
        d = R.evaluate(_recovery_state(100.0), -6.0, 94.0, T1,
                       bar1_ts=None, bar1_close=None)
        self.assertTrue(d.sell)
        self.assertIn("hard_stop", d.reason)


class RecoveryContinuityTest(unittest.TestCase):
    def test_minus_2_keeps_protection(self):
        d = R.evaluate(_recovery_state(100.0, armed=True), -2.0, 98.5, T1)
        self.assertEqual(d.mode, R.MODE_RECOVERY)
        self.assertTrue(d.state["recovery_reached_exit"])
        self.assertEqual(d.state["recovery_high_price"], 100.0)

    def test_minus5_minus3_minus2_then_drop_protects(self):
        d = R.evaluate(R.default_state(highest_price=100.0), -5.0, 95.0, T0)   # 진입 high95
        d = R.evaluate(d.state, -3.0, 97.0, T1)                                 # arm high97
        d = R.evaluate(d.state, -2.0, 98.0, T2)                                 # 성공 high98
        self.assertEqual(d.mode, R.MODE_RECOVERY)
        self.assertEqual(d.state["recovery_high_price"], 98.0)
        # 다시 하락: 98 대비 -1.53%(96.5) 완료봉 → 보호 SELL
        d = R.evaluate(d.state, -3.7, 96.5, T2 + timedelta(minutes=1),
                       atr_pct=0.0, bar1_ts="bX", bar1_close=96.5)
        self.assertTrue(d.sell)

    def test_zero_recovers_to_normal(self):
        d = R.evaluate(_recovery_state(100.0, armed=True), 0.0, 100.5, T1)
        self.assertEqual(d.mode, R.MODE_NORMAL)
        self.assertIsNone(d.state["recovery_high_price"])


# ══════════════════════════════════════════════════════════════
# 수익 트레일링 — 활성/완료봉확정/연속/EMA9가속/급락/5분/정정
# ══════════════════════════════════════════════════════════════
class ProfitActivateTest(unittest.TestCase):
    def test_plus_1_49_not_active(self):
        r = _pt(_profit_state(), net=1.49, cur=101.49)
        self.assertFalse(r["state"]["profit_trail_active"])

    def test_plus_1_50_activates(self):
        r = _pt(_profit_state(highest=101.5), net=1.50, cur=101.5)
        self.assertTrue(r["state"]["profit_trail_active"])
        self.assertFalse(r["sell"])


class ProfitBarConfirmTest(unittest.TestCase):
    def _st(self):
        return _profit_state(highest=100.0, active=True, high_net=3.0)

    def test_in_progress_bar_no_count(self):
        # 완료봉 없음(None) → count 0, HOLD (미완성 22:31봉)
        r = _pt(self._st(), b1=None, b1c=None, cur=98.8)
        self.assertEqual(r["state"]["profit_breach_closes"], 0)
        self.assertFalse(r["sell"])

    def test_completed_bar_breach_count_1(self):
        r = _pt(self._st(), b1="2231", b1c=98.8, cur=98.8)   # -1.2% 이탈
        self.assertEqual(r["state"]["profit_breach_closes"], 1)
        self.assertFalse(r["sell"])                          # 1봉 정상경로 HOLD

    def test_same_bar_10x_count_stays_1(self):
        s = self._st()
        for _ in range(10):
            r = _pt(s, b1="2231", b1c=98.8, cur=98.8)
            s = r["state"]
            self.assertFalse(r["sell"])
        self.assertEqual(s["profit_breach_closes"], 1)

    def test_two_consecutive_bars_sell(self):
        r1 = _pt(self._st(), b1="2231", b1c=98.8, cur=98.8)
        r2 = _pt(r1["state"], b1="2232", b1c=98.8, cur=98.8)
        self.assertTrue(r2["sell"])

    def test_non_consecutive_no_sell(self):
        # 22:31 이탈(1) → 22:32 정상(0) → 22:33 이탈(1) → HOLD
        r = _pt(self._st(), b1="2231", b1c=98.8, cur=98.8)
        self.assertEqual(r["state"]["profit_breach_closes"], 1)
        r = _pt(r["state"], b1="2232", b1c=100.5, cur=100.5)     # 정상봉
        self.assertEqual(r["state"]["profit_breach_closes"], 0)
        r = _pt(r["state"], b1="2233", b1c=98.8, cur=98.8)       # 다시 이탈
        self.assertEqual(r["state"]["profit_breach_closes"], 1)
        self.assertFalse(r["sell"])

    def test_restart_second_bar_sells(self):
        r1 = _pt(self._st(), b1="2231", b1c=98.8, cur=98.8)
        reloaded = R.merge_state(r1["state"])   # 재시작(저장·복원)
        self.assertEqual(reloaded["profit_breach_closes"], 1)
        self.assertEqual(reloaded["last_profit_breach_bar_at"], "2231")
        r2 = _pt(reloaded, b1="2232", b1c=98.8, cur=98.8)
        self.assertTrue(r2["sell"])

    def test_late_correction_breach_to_normal(self):
        r1 = _pt(self._st(), b1="2231", b1c=98.8, cur=98.8)      # 이탈 → 1
        self.assertEqual(r1["state"]["profit_breach_closes"], 1)
        r2 = _pt(r1["state"], b1="2231", b1c=100.5, cur=100.5)   # 같은 봉 정정→정상
        self.assertEqual(r2["state"]["profit_breach_closes"], 0)  # 안전 교정

    def test_regression_ignored(self):
        r1 = _pt(self._st(), b1="2232", b1c=98.8, cur=98.8)      # 1
        r2 = _pt(r1["state"], b1="2231", b1c=98.8, cur=98.8)     # 역행 → 무시
        self.assertEqual(r2["state"]["profit_breach_closes"], 1)

    def test_ema9_rising_not_veto_two_bars(self):
        # EMA9 상승·종가>EMA9 여도 완료봉 2개면 SELL
        r1 = _pt(self._st(), b1="2231", b1c=98.8, cur=98.8, ema9=98.0, rising=True)
        r2 = _pt(r1["state"], b1="2232", b1c=98.8, cur=98.8, ema9=98.0, rising=True)
        self.assertTrue(r2["sell"])

    def test_ema9_down_one_bar_fast(self):
        # 이탈 완료봉 + 종가<EMA9 → 1봉 빠른 SELL
        r = _pt(self._st(), b1="2231", b1c=98.8, cur=98.8, ema9=99.5, rising=False)
        self.assertTrue(r["sell"])
        self.assertIn("fast", r["reason"])

    def test_ema9_down_alone_no_sell(self):
        # 이탈 없는데 종가<EMA9 만으로는 SELL 금지
        r = _pt(self._st(), b1="2231", b1c=100.5, cur=100.5, ema9=101.0, rising=False)
        self.assertFalse(r["sell"])

    def test_bar5_completed_immediate(self):
        r = _pt(self._st(), b1=None, b1c=None, b5="b5", b5c=98.8, cur=100.0)
        self.assertTrue(r["sell"])
        self.assertIn("bar5", r["reason"])

    def test_bar5_in_progress_none_holds(self):
        # 진행중 5분봉(None) → HOLD
        r = _pt(self._st(), b1=None, b1c=None, b5=None, b5c=None, cur=100.0)
        self.assertFalse(r["sell"])

    def test_panic_live_immediate(self):
        # 실시간가 급락(추가 0.5%p) → 봉 없이 즉시 SELL
        r = _pt(self._st(), b1=None, b1c=None, cur=98.4)   # live -1.6% <= -1.5
        self.assertTrue(r["sell"])
        self.assertIn("panic", r["reason"])

    def test_missing_data_holds_count(self):
        s = _profit_state(highest=100.0, active=True, high_net=3.0, closes=1,
                          prev=0, last_bar="2231")
        r = _pt(s, b1=None, b1c=None, cur=100.0)   # 데이터 없음 → 카운트 유지, 매도 보류
        self.assertEqual(r["state"]["profit_breach_closes"], 1)
        self.assertFalse(r["sell"])


class ProfitTrailWidthTest(unittest.TestCase):
    def test_atr_clamp_min(self):
        # atr=0 → trail=1.0. 완료봉 -1.1% + 종가<EMA9 → fast SELL
        r = _pt(_profit_state(100.0, active=True, high_net=3.0),
                b1="a", b1c=98.9, cur=98.9, ema9=99.5, rising=False)
        self.assertTrue(r["sell"])

    def test_atr_clamp_max(self):
        # atr 큼 → trail=2.5. 완료봉 -2.0% > -2.5 → 미이탈 HOLD
        r = _pt(_profit_state(100.0, active=True, high_net=3.0),
                atr=10.0, b1="a", b1c=98.0, cur=98.0, ema9=99.5, rising=False)
        self.assertFalse(r["sell"])


# ══════════════════════════════════════════════════════════════
# decide — 단일 매도판정 권위(DEFER 없음)
# ══════════════════════════════════════════════════════════════
class DecideTest(unittest.TestCase):
    def _ctx(self, b1=None, b1c=None, b5=None, b5c=None, ema9=98.0, rising=True, atr=0.0):
        return {"atr_pct": atr, "ema9": ema9, "ema9_rising": rising,
                "bar1_ts": b1, "bar1_close": b1c, "bar5_ts": b5, "bar5_close": b5c}

    def test_exit_pending_holds(self):
        s = R.default_state(); s["management_mode"] = R.MODE_EXIT
        d = R.decide_management_action(s, 3.0, 105.0, T0, ctx=self._ctx())
        self.assertEqual(d.action, R.ACT_HOLD)

    def test_net_minus5_enters_recovery(self):
        d = R.decide_management_action(R.default_state(highest_price=100.0),
                                       -5.0, 95.0, T0, ctx=self._ctx())
        self.assertEqual(d.state["management_mode"], R.MODE_RECOVERY)

    def test_new_position_holds_not_defer(self):
        d = R.decide_management_action(R.default_state(highest_price=100.0),
                                       0.5, 100.5, T0, ctx=self._ctx())
        self.assertEqual(d.action, R.ACT_HOLD)
        self.assertIn("single_sell_authority", d.reason)

    def test_never_returns_defer(self):
        for pct in [1.5, 2.0, 2.5, 5.0, 10.0]:
            d = R.decide_management_action(R.default_state(highest_price=100.0 + pct),
                                           pct, 100.0 + pct, T0, ctx=self._ctx())
            self.assertNotEqual(d.action, R.ACT_DEFER)

    def test_recovery_to_normal_then_profit(self):
        s = _recovery_state(100.0, armed=True)
        d = R.decide_management_action(s, 0.0, 100.5, T0, ctx=self._ctx())
        self.assertEqual(d.state["management_mode"], R.MODE_NORMAL)
        d2 = R.decide_management_action(d.state, 1.6, 101.6, T1, ctx=self._ctx())
        self.assertTrue(d2.state["profit_trail_active"])


class MergeTest(unittest.TestCase):
    def test_legacy_defaults(self):
        m = R.merge_state({"code": "AAPL", "qty": 10})
        self.assertEqual(m["profit_breach_closes"], 0)
        self.assertEqual(m["profit_prev_breach_closes"], 0)
        self.assertIsNone(m["last_profit_breach_bar_at"])

    def test_preserves_breach_state(self):
        s = _profit_state(active=True, closes=1, prev=0, last_bar="2231")
        m = R.merge_state(s)
        self.assertEqual(m["profit_breach_closes"], 1)
        self.assertEqual(m["last_profit_breach_bar_at"], "2231")


if __name__ == "__main__":
    unittest.main()
