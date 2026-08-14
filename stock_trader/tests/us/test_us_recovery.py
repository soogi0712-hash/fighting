"""US 손실 회복 트레일링 상태기계 경계 테스트 (§4/§9C/§9D — 결정론적, 부수효과 없음)."""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import strategies.us_recovery as R

T0 = datetime(2026, 8, 14, 22, 40, 0)  # 고정 기준시각(주입)


def _recovery_state(high_price=100.0, high_net=-5.0, started=T0, recovered=False):
    """RECOVERY_TRAILING 상태 dict 생성 헬퍼."""
    s = R.default_state(recovered=recovered)
    s["management_mode"]       = R.MODE_RECOVERY
    s["recovery_started_at"]   = started.isoformat()
    s["recovery_high_price"]   = high_price
    s["recovery_high_net_pct"] = high_net
    return s


class RecoveryEntryBoundaryTest(unittest.TestCase):
    # ── §9C 진입 경계 ─────────────────────────────────────────
    def test_enter_at_minus_4_99_stays_normal(self):
        s = R.default_state()
        d = R.evaluate(s, net_pct=-4.99, cur_price=95.01, now=T0)
        self.assertEqual(d.mode, R.MODE_NORMAL)
        self.assertFalse(d.sell)

    def test_enter_at_exactly_minus_5_00(self):
        s = R.default_state()
        d = R.evaluate(s, net_pct=-5.00, cur_price=95.0, now=T0)
        self.assertEqual(d.mode, R.MODE_RECOVERY)
        self.assertFalse(d.sell)  # 즉시매도 금지
        self.assertEqual(d.state["recovery_started_at"], T0.isoformat())
        self.assertEqual(d.state["recovery_high_price"], 95.0)

    def test_enter_then_same_price_no_immediate_high_drop(self):
        # 진입 직후 동일가 재평가 — -0.7% 조건 오발동 금지
        s = R.default_state()
        d1 = R.evaluate(s, net_pct=-5.0, cur_price=95.0, now=T0)
        d2 = R.evaluate(d1.state, net_pct=-5.0, cur_price=95.0,
                        now=T0 + timedelta(seconds=5))
        self.assertFalse(d2.sell)
        self.assertEqual(d2.mode, R.MODE_RECOVERY)


class RecoveryHardStopTest(unittest.TestCase):
    def test_minus_6_00_sell_all(self):
        s = _recovery_state()
        d = R.evaluate(s, net_pct=-6.00, cur_price=94.0, now=T0 + timedelta(minutes=1))
        self.assertTrue(d.sell)
        self.assertIn("hard", d.reason)

    def test_minus_5_69_no_high_drop_holds(self):
        # 반등 없이 하락하나 고점(-100)대비 가격드롭 0.65% (<0.7) + net -5.69 (>-6) → HOLD
        s = _recovery_state(high_price=100.0)
        d = R.evaluate(s, net_pct=-5.69, cur_price=99.35, now=T0 + timedelta(minutes=1))
        self.assertFalse(d.sell)
        self.assertEqual(d.mode, R.MODE_RECOVERY)

    def test_continuous_drop_triggers_hard_stop(self):
        # 가격 상승 없이 계속 하락 → 결국 -6% 하드손절
        s = _recovery_state(high_price=100.0)
        d = R.evaluate(s, net_pct=-6.01, cur_price=93.9, now=T0 + timedelta(minutes=2))
        self.assertTrue(d.sell)


class RecoveryHighDropTest(unittest.TestCase):
    def test_rebound_updates_recovery_high(self):
        s = _recovery_state(high_price=95.0, high_net=-5.0)
        # -5%→-3% 반등, 가격 97 로 신고점
        d = R.evaluate(s, net_pct=-3.0, cur_price=97.0, now=T0 + timedelta(minutes=1))
        self.assertEqual(d.state["recovery_high_price"], 97.0)
        self.assertFalse(d.sell)

    def test_high_drop_minus_0_69_holds(self):
        s = _recovery_state(high_price=100.0)
        # -0.69% 하락 → HOLD, net 은 트리거 안 되게 -3.0
        d = R.evaluate(s, net_pct=-3.0, cur_price=99.31, now=T0 + timedelta(minutes=1))
        self.assertFalse(d.sell)

    def test_high_drop_exactly_minus_0_70_sell(self):
        s = _recovery_state(high_price=100.0)
        d = R.evaluate(s, net_pct=-3.0, cur_price=99.30, now=T0 + timedelta(minutes=1))
        self.assertTrue(d.sell)
        self.assertIn("high_drop", d.reason)

    def test_recovery_high_never_lowers(self):
        s = _recovery_state(high_price=100.0)
        # 가격 하락 재평가로도 recovery_high 는 유지(낮아지지 않음)
        d = R.evaluate(s, net_pct=-3.0, cur_price=99.5, now=T0 + timedelta(minutes=1))
        self.assertEqual(d.state["recovery_high_price"], 100.0)


class RecoveryTimeStopTest(unittest.TestCase):
    def test_14m59s_net_minus_4_5_holds(self):
        s = _recovery_state(high_price=100.0)
        d = R.evaluate(s, net_pct=-4.5, cur_price=99.6,
                       now=T0 + timedelta(minutes=14, seconds=59))
        self.assertFalse(d.sell)

    def test_15m_net_minus_3_99_holds(self):
        s = _recovery_state(high_price=100.0)
        d = R.evaluate(s, net_pct=-3.99, cur_price=99.7,
                       now=T0 + timedelta(minutes=15))
        self.assertFalse(d.sell)

    def test_15m_net_exactly_minus_4_0_sell(self):
        s = _recovery_state(high_price=100.0)
        d = R.evaluate(s, net_pct=-4.00, cur_price=99.7,
                       now=T0 + timedelta(minutes=15))
        self.assertTrue(d.sell)
        self.assertIn("time_stop", d.reason)


class RecoveryExitTest(unittest.TestCase):
    def test_minus_2_01_stays_recovery(self):
        s = _recovery_state(high_price=100.0)
        d = R.evaluate(s, net_pct=-2.01, cur_price=99.8, now=T0 + timedelta(minutes=1))
        self.assertEqual(d.mode, R.MODE_RECOVERY)
        self.assertFalse(d.sell)

    def test_exactly_minus_2_0_returns_normal(self):
        s = _recovery_state(high_price=100.0)
        d = R.evaluate(s, net_pct=-2.00, cur_price=99.9, now=T0 + timedelta(minutes=1))
        self.assertEqual(d.mode, R.MODE_NORMAL)
        self.assertFalse(d.sell)
        self.assertIsNone(d.state["recovery_started_at"])

    def test_normal_then_profit_trailing_works(self):
        # NORMAL 복귀 후 수익 트레일링(§3): +1.5% 활성, 최고 대비 -1.0%p 하락 시 매도
        s = R.default_state(recovered=True)
        s = R.evaluate_profit_trailing(s, 1.5)["state"]
        self.assertTrue(s["profit_trail_active"])
        r = R.evaluate_profit_trailing(s, 0.5)  # 1.5 → 0.5 (-1.0%p)
        self.assertTrue(r["sell"])
        r2 = R.evaluate_profit_trailing(s, 0.6)  # -0.9%p → HOLD
        self.assertFalse(r2["sell"])


class ProfitTrailingActivateTest(unittest.TestCase):
    def test_below_1_5_not_active(self):
        s = R.default_state(recovered=True)
        r = R.evaluate_profit_trailing(s, 1.49)
        self.assertFalse(r["state"]["profit_trail_active"])
        self.assertFalse(r["sell"])

    def test_exactly_1_5_activates_no_sell(self):
        s = R.default_state(recovered=True)
        r = R.evaluate_profit_trailing(s, 1.5)
        self.assertTrue(r["activate"])
        self.assertFalse(r["sell"])  # 도달만으로는 매도 안 함


class RecoveryPersistenceTest(unittest.TestCase):
    # ── §9D 영속·재시작 ───────────────────────────────────────
    def test_state_roundtrip_via_merge(self):
        import json
        s = _recovery_state(high_price=101.0, started=T0)
        loaded = R.merge_state(json.loads(json.dumps(s)))
        self.assertEqual(loaded["management_mode"], R.MODE_RECOVERY)
        self.assertEqual(loaded["recovery_started_at"], T0.isoformat())
        self.assertEqual(loaded["recovery_high_price"], 101.0)

    def test_time_stop_survives_restart(self):
        # 재시작(=state 재로딩) 후에도 started_at 기준 15분 경과 판정 정상
        s = _recovery_state(high_price=100.0, started=T0)
        loaded = R.merge_state(dict(s))
        d = R.evaluate(loaded, net_pct=-4.0, cur_price=99.7,
                       now=T0 + timedelta(minutes=15))
        self.assertTrue(d.sell)

    def test_repeated_restore_does_not_lower_highs(self):
        s = _recovery_state(high_price=100.0)
        R.bump_highest_price(s, 120.0)  # 최고가 120
        # 반복 복원(낮은 현재가)로도 낮아지지 않음
        R.bump_highest_price(s, 90.0)
        self.assertEqual(s["highest_price"], 120.0)
        # recovery_high 도 낮은 가격 재평가로 유지
        d = R.evaluate(s, net_pct=-3.0, cur_price=95.0, now=T0 + timedelta(minutes=1))
        self.assertEqual(d.state["recovery_high_price"], 100.0)

    def test_legacy_json_defaults_safe(self):
        # 구버전 JSON(신규 필드 없음) → 안전 기본값
        legacy = {"code": "AAPL", "qty": 10, "avg_price": 50.0}
        m = R.merge_state(legacy)
        self.assertEqual(m["management_mode"], R.MODE_NORMAL)
        self.assertFalse(m["recovered"])
        self.assertEqual(m["highest_price"], 0.0)
        self.assertIsNone(m["recovery_started_at"])

    def test_exit_pending_blocks_evaluation(self):
        s = _recovery_state()
        s["management_mode"] = R.MODE_EXIT
        d = R.evaluate(s, net_pct=-6.0, cur_price=90.0, now=T0)
        self.assertFalse(d.sell)  # EXIT_PENDING → 판정 보류(중복 SELL 금지)
        self.assertEqual(d.mode, R.MODE_EXIT)


if __name__ == "__main__":
    unittest.main()


class ManagementPriorityTest(unittest.TestCase):
    """§8 통합 우선순위: 복원 면제 + 회복 우선."""
    def test_new_normal_net_above_5_defers(self):
        s = R.default_state(recovered=False)
        d = R.decide_management_action(s, net_pct=-3.0, cur_price=97.0, now=T0)
        self.assertEqual(d.action, R.ACT_DEFER)   # 기존 ①~⑩ 로직 사용

    def test_new_position_minus_5_enters_recovery_not_defer(self):
        s = R.default_state(recovered=False)
        d = R.decide_management_action(s, net_pct=-5.0, cur_price=95.0, now=T0)
        self.assertEqual(d.mode, R.MODE_RECOVERY)
        self.assertFalse(d.sell)   # 즉시 -5% 손절 대체 → 즉시매도 금지

    def test_recovered_normal_exempt_from_fixed_tp(self):
        # 복원 포지션이 +2.5% 여도 고정익절 면제 → HOLD(DEFER 아님)
        s = R.default_state(recovered=True)
        d = R.decide_management_action(s, net_pct=2.5, cur_price=110.0, now=T0)
        self.assertEqual(d.action, R.ACT_HOLD)
        self.assertNotEqual(d.action, R.ACT_DEFER)

    def test_recovered_profit_trailing_sells(self):
        s = R.default_state(recovered=True)
        # +2% 고점 후 +0.9% (-1.1%p) → 수익 트레일링 매도
        d1 = R.decide_management_action(s, net_pct=2.0, cur_price=110.0, now=T0)
        d2 = R.decide_management_action(d1.state, net_pct=0.9, cur_price=108.0,
                                        now=T0 + timedelta(minutes=1))
        self.assertTrue(d2.sell)

    def test_recovery_precedes_ma_stop(self):
        # RECOVERY 모드면 MA/−5% 로직보다 앞서 손실회복 판정(여기선 HOLD)
        s = _recovery_state(high_price=100.0)
        d = R.decide_management_action(s, net_pct=-4.5, cur_price=99.8,
                                       now=T0 + timedelta(minutes=1))
        self.assertIn(d.action, (R.ACT_HOLD, R.ACT_SELL_ALL))
        self.assertNotEqual(d.action, R.ACT_DEFER)

    def test_exit_pending_defers_to_hold(self):
        s = _recovery_state(); s["management_mode"] = R.MODE_EXIT
        d = R.decide_management_action(s, net_pct=-6.0, cur_price=90.0, now=T0)
        self.assertFalse(d.sell)
