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


def _recovery_state(high_price=100.0, high_net=-5.0):
    s = R.default_state()
    s["management_mode"]       = R.MODE_RECOVERY_WAIT
    s["recovery_started_at"]   = T0.isoformat()
    s["recovery_high_price"]   = high_price
    s["recovery_high_net_pct"] = high_net
    return s


def _rec(state, net, cur, now=T1, atr=0.0, b5=None, b5c=None, acct=False, sym=False):
    return R.evaluate(state, net, cur, now, atr_pct=atr, bar5_ts=b5,
                      bar5_close=b5c, account_risk_exceeded=acct,
                      symbol_risk_exceeded=sym)


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
# 손실 관리(RECOVERY_WAIT) — 고정 손절 제거 / 구조적 붕괴·계좌위험만 매도
# ══════════════════════════════════════════════════════════════
class RecoveryEnterTest(unittest.TestCase):
    def test_enter_minus_4_99_stays_normal(self):
        d = _rec(R.default_state(), -4.99, 95.01, now=T0)
        self.assertEqual(d.mode, R.MODE_NORMAL)

    def test_enter_exactly_minus_5_no_sell(self):
        d = _rec(R.default_state(highest_price=100.0), -5.0, 95.0, now=T0)
        self.assertEqual(d.mode, R.MODE_RECOVERY_WAIT)
        self.assertFalse(d.sell)

    def test_minus_6_is_warning_not_sell(self):
        d = _rec(R.default_state(highest_price=100.0), -6.0, 94.0, now=T0)  # 진입
        self.assertFalse(d.sell)                        # -6 은 경고, 매도 아님
        self.assertTrue(d.state["recovery_warn"])
        d2 = _rec(d.state, -6.5, 93.5)                  # RECOVERY_WAIT 유지, 여전히 매도 안 함
        self.assertFalse(d2.sell)
        self.assertEqual(d2.mode, R.MODE_RECOVERY_WAIT)


class NoFixedStopTest(unittest.TestCase):
    def test_no_hard_stop_minus_6(self):
        # 예전 -6 하드손절 제거: RECOVERY_WAIT 에서 -6 이어도 매도 안 함
        d = _rec(_recovery_state(100.0), -6.0, 94.0, b5=None, b5c=None)
        self.assertFalse(d.sell)

    def test_no_simple_high_drop_sell(self):
        # recovery high 대비 단순 하락(예: -3%)만으로는 매도하지 않는다
        d = _rec(_recovery_state(100.0), -3.0, 97.0, b5="c1", b5c=97.0)  # -3% < 구조3%
        self.assertFalse(d.sell)


class LossSellConjunctionTest(unittest.TestCase):
    """손실 매도는 '구조적 붕괴 연속 + 계좌위험 + 종목위험' **모두** 충족 시에만."""
    def _confirm_struct(self, acct, sym):
        # 연속 2봉 구조이탈 상태를 만들고 마지막 봉에서 위험조건을 적용
        d = _rec(_recovery_state(100.0), -4.0, 96.0, atr=0.0, b5="c1", b5c=96.0,
                 acct=acct, sym=sym)
        return _rec(d.state, -4.2, 95.8, atr=0.0, b5="c2", b5c=95.8,
                    acct=acct, sym=sym)

    def test_all_three_sells(self):
        d = self._confirm_struct(acct=True, sym=True)
        self.assertTrue(d.sell)
        self.assertIn("structural_breakdown+account_risk+symbol_risk", d.reason)

    def test_struct_only_holds(self):
        d = self._confirm_struct(acct=False, sym=False)
        self.assertFalse(d.sell)               # 구조 확정이나 위험한도 미충족 → HOLD
        self.assertEqual(d.state["struct_breach_closes"], 2)

    def test_struct_and_account_only_holds(self):
        d = self._confirm_struct(acct=True, sym=False)   # 종목위험 없음
        self.assertFalse(d.sell)

    def test_struct_and_symbol_only_holds(self):
        d = self._confirm_struct(acct=False, sym=True)   # 계좌위험 없음
        self.assertFalse(d.sell)

    def test_risks_without_struct_holds(self):
        # 계좌+종목 위험이어도 구조적 붕괴 연속 미확인이면 매도 안 함(단순 하락 보호)
        d = _rec(_recovery_state(100.0), -4.0, 96.0, atr=0.0, b5="c1", b5c=96.0,
                 acct=True, sym=True)          # 구조 1봉만
        self.assertFalse(d.sell)

    def test_single_5m_breakdown_holds_even_with_risk(self):
        d = _rec(_recovery_state(100.0), -4.0, 96.0, atr=0.0, b5="c1", b5c=96.0,
                 acct=True, sym=True)
        self.assertFalse(d.sell)
        self.assertEqual(d.state["struct_breach_closes"], 1)

    def test_non_consecutive_resets(self):
        d = _rec(_recovery_state(100.0), -4.0, 96.0, atr=0.0, b5="c1", b5c=96.0,
                 acct=True, sym=True)
        d = _rec(d.state, -2.5, 97.5, atr=0.0, b5="c2", b5c=97.5,
                 acct=True, sym=True)          # 정상 5분봉 → 초기화
        self.assertEqual(d.state["struct_breach_closes"], 0)
        self.assertFalse(d.sell)

    def test_atr_widens_structural_distance(self):
        d = _rec(_recovery_state(100.0), -4.0, 96.0, atr=2.0, b5="c1", b5c=96.0,
                 acct=True, sym=True)          # 거리6% → -4% 미이탈
        self.assertEqual(d.state["struct_breach_closes"], 0)

    def test_no_5m_data_no_sell(self):
        d = _rec(_recovery_state(100.0), -5.0, 95.0, b5=None, b5c=None,
                 acct=True, sym=True)
        self.assertFalse(d.sell)


class RecoveryToNormalTest(unittest.TestCase):
    def test_minus_2_marks_success_no_normal(self):
        d = _rec(_recovery_state(100.0), -2.0, 98.5)
        self.assertEqual(d.mode, R.MODE_RECOVERY_WAIT)
        self.assertTrue(d.state["recovery_reached_exit"])

    def test_zero_recovers_to_normal(self):
        d = _rec(_recovery_state(100.0), 0.0, 100.5)
        self.assertEqual(d.mode, R.MODE_NORMAL)
        self.assertIsNone(d.state["recovery_high_price"])

    def test_crash_then_rebound_no_sell(self):
        # 급락 -5→-6 (경고) → 반등 → 0% 회복 → NORMAL, 매도 0회(구조붕괴/위험 미충족)
        d = _rec(R.default_state(highest_price=100.0), -5.0, 95.0, now=T0)   # 진입
        d = _rec(d.state, -6.2, 93.8, b5="c1", b5c=93.8, acct=True, sym=True) # 급락, 단일봉
        self.assertFalse(d.sell)                # 구조 1봉만 → 위험 충족해도 HOLD
        self.assertTrue(d.state["recovery_warn"])
        d = _rec(d.state, -3.0, 97.0)                                        # 반등
        self.assertFalse(d.sell)
        d = _rec(d.state, 0.1, 100.1)                                       # 손익분기 회복
        self.assertEqual(d.mode, R.MODE_NORMAL)
        self.assertFalse(d.sell)


class RecoveryAnalyticsTest(unittest.TestCase):
    def test_records_on_recovered(self):
        # 진입 후 최대하락 갱신 → 0% 회복 시 분석 레코드 생성
        d = _rec(R.default_state(highest_price=100.0), -5.0, 95.0, now=T0)   # 진입
        d = _rec(d.state, -6.5, 93.5, now=T1)                                # 최대하락 -6.5
        d = _rec(d.state, 0.2, 100.2, now=T2)                                # 회복 → NORMAL
        rec = d.state["recovery_last_outcome"]
        self.assertEqual(rec["outcome"], "recovered")
        self.assertAlmostEqual(rec["entry_net_pct"], -5.0)
        self.assertAlmostEqual(rec["max_drawdown_net_pct"], -6.5)
        self.assertAlmostEqual(rec["final_net_pct"], 0.2)
        self.assertEqual(rec["hypothetical_early_stop_net_pct"], -5.0)   # 조기손절 가상기준
        self.assertGreater(rec["recovery_seconds"], 0)

    def test_records_on_structural_and_risk_sell(self):
        d = _rec(_recovery_state(100.0), -4.0, 96.0, atr=0.0, b5="c1", b5c=96.0,
                 acct=True, sym=True)
        d = _rec(d.state, -4.2, 95.8, atr=0.0, b5="c2", b5c=95.8, acct=True, sym=True)
        self.assertTrue(d.sell)
        rec = d.state["recovery_last_outcome"]
        self.assertEqual(rec["outcome"], "structural_and_risk_sell")
        self.assertIsNotNone(rec["max_drawdown_net_pct"])


class RiskSizingTest(unittest.TestCase):
    def test_reduces_qty_for_high_atr(self):
        # ATR 클수록 위험거리 넓어져 수량 축소
        q_lo = R.risk_capped_qty(100.0, atr_pct=1.0, budget_qty=100, max_loss_usd=60)
        q_hi = R.risk_capped_qty(100.0, atr_pct=5.0, budget_qty=100, max_loss_usd=60)
        self.assertGreater(q_lo, q_hi)

    def test_caps_at_max_loss(self):
        # max_loss=30, price=100, 구조거리=3%(atr0) → per-share risk=3 → cap=10
        q = R.risk_capped_qty(100.0, atr_pct=0.0, budget_qty=100, max_loss_usd=30)
        self.assertEqual(q, 10)

    def test_never_exceeds_budget(self):
        q = R.risk_capped_qty(10.0, atr_pct=0.0, budget_qty=5, max_loss_usd=100000)
        self.assertEqual(q, 5)

    def test_zero_price_returns_budget(self):
        self.assertEqual(R.risk_capped_qty(0.0, 1.0, 7, 60), 7)


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
        self.assertEqual(d.state["management_mode"], R.MODE_RECOVERY_WAIT)
        self.assertFalse(d.sell)

    def test_account_risk_alone_holds(self):
        # 계좌위험만으로는 손실 매도 안 함(구조붕괴+종목위험 동시 필요)
        c = self._ctx(); c["account_risk_exceeded"] = True
        d = R.decide_management_action(_recovery_state(100.0), -3.0, 97.0, T0, ctx=c)
        self.assertFalse(d.sell)

    def test_all_three_via_decide_sells(self):
        c = self._ctx(b5="c1", b5c=96.0); c["account_risk_exceeded"] = True
        c["symbol_risk_exceeded"] = True
        d = R.decide_management_action(_recovery_state(100.0), -4.0, 96.0, T0, ctx=c)
        # 1봉만 → HOLD
        self.assertFalse(d.sell)
        c2 = self._ctx(b5="c2", b5c=95.8); c2["account_risk_exceeded"] = True
        c2["symbol_risk_exceeded"] = True
        d2 = R.decide_management_action(d.state, -4.2, 95.8, T1, ctx=c2)
        self.assertTrue(d2.sell)

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
        s = _recovery_state(100.0)
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
