"""US 주문 crash-safe 제출-의도(submit-intent) 파이프라인 P0 입증.

2026-08-12 사고(UNKNOWN→ORDER_ACCEPTED 직접전이 → ghost 영구차단) 회귀 고정 +
최종 리뷰 지적(외부접수 후 로컬실패 중복주문 / rt_cd 오분류) 반영을 실제
OrderLifecycleManager / PendingOrderRegistry / USStrategyManager 메서드로 입증한다.

핵심 계약:
  1. KIS 호출 '전' durable submit-intent(PENDING_SUBMIT) 저장 — 저장 성공 시에만
     주문. 저장 실패 → 미주문. 접수 직후 crash 를 재현해도 재시작 후 동일 symbol/
     side 재주문이 차단된다(중복 0회).
  2. rt_cd≠0 전체를 명확 거절로 취급하지 않는다: ODNO 존재/ rt_cd=9(예외 래핑)/
     알 수 없는 코드는 UNKNOWN_CONFIRM(확인대기·재주문 금지). allowlist 명확 거절+
     ODNO 없음만 REJECTED(안전 해제).
  3. 외부접수(ODNO/rt_cd=0) 후 로컬 후처리 실패에도 종말화·해제 없이 차단 유지.
  4. cancel_local_only 는 ODNO 보유 시 취소 거부(외부 주문 보호).
  5. 재시작 정합화 + 운영자 감사기반 수동 해제.
"""
import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import journal.fill_observer as fo  # noqa: E402
from journal.fill_observer import PendingOrderRegistry, PendingStatus  # noqa: E402
from phoenix.lifecycle import (  # noqa: E402
    OrderLifecycleManager, LifecycleState, make_order_lifecycle_id,
)
from strategies.us_strategy_manager import (  # noqa: E402
    USStrategyManager as _USM,
    _US_OUTCOME_ACCEPTED, _US_OUTCOME_UNKNOWN_CONFIRM,
    _US_OUTCOME_REJECTED, _US_OUTCOME_NOT_SENT,
)

S = LifecycleState
PS = PendingStatus


class FakeOutbox:
    """_us_apply_fill_delta 의 delta 멱등 게이트만 검증하기 위한 경량 outbox."""

    def __init__(self):
        self.rows = {}
        self._cum = {}

    def last_cum(self, oid):
        return self._cum.get(oid, (0, 0.0))

    def event_key(self, oid, cum):
        return f"{oid}:{cum}"

    def insert_if_absent(self, row):
        ek = row["event_key"]
        if ek not in self.rows:
            self.rows[ek] = row
            self._cum[row["oid"]] = (row["cum_qty"], row["cum_cost"])

    def get(self, ek):
        return self.rows.get(ek)

    def set_flag(self, *a, **k):
        pass


class USFlow:
    """USStrategyManager 실제 메서드만 바인딩한 경량 인스턴스."""
    _us_classify_order_outcome = _USM._us_classify_order_outcome
    _us_begin_submit_intent    = _USM._us_begin_submit_intent
    _us_finalize_submit_intent = _USM._us_finalize_submit_intent
    _us_has_active_order       = _USM._us_has_active_order
    _us_meta_is_stale          = _USM._us_meta_is_stale
    _us_restore_pending_meta   = _USM._us_restore_pending_meta
    us_dispatch_fill           = _USM.us_dispatch_fill
    _us_apply_fill_delta       = _USM._us_apply_fill_delta
    us_manual_release_pending  = _USM.us_manual_release_pending
    us_list_pending_confirm    = _USM.us_list_pending_confirm

    def __init__(self, mgr, registry, outbox=None):
        self._us_lifecycle_mgr    = mgr
        self._us_pending_registry = registry
        self._us_pending_buy_meta  = {}
        self._us_pending_sell_meta = {}
        self._us_outbox = outbox
        self.api = MagicMock()
        self.api.get_usd_exchange_rate.return_value = 1350.0
        self.pos_mgr = MagicMock()
        self.pos_mgr.positions = {}


class USSubmitIntentP0Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="us-p0-")
        self._orig = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "pending.db")
        self._reset_conn()
        self.registry = PendingOrderRegistry()
        self.mgr = OrderLifecycleManager(os.path.join(self.tmp, "lifecycle.db"))
        self.flow = USFlow(self.mgr, self.registry)

    def tearDown(self):
        self._reset_conn()
        fo._JOURNAL_DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _reset_conn():
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None

    # ── 실제 caller 규약 모사: begin(intent 저장) → KIS → finalize(분류) ──────
    def _order(self, side, symbol, qty, kis_result, trade_id="", price=10.0):
        intent_id = make_order_lifecycle_id("US", side, symbol)
        ok = self.flow._us_begin_submit_intent(
            intent_id, symbol, side, qty, price, excd="NASD", trade_id=trade_id)
        if not ok:
            return intent_id, "NO_INTENT"
        outcome = self.flow._us_finalize_submit_intent(
            intent_id, symbol, side, qty, kis_result, excd="NASD", trade_id=trade_id)
        return intent_id, outcome

    def _state(self, lc_id):
        return self.mgr.load(lc_id).current_state

    def _row(self, lc_id):
        return self.registry.get_by_trade_id(lc_id)

    # ══════════════════════════════════════════════════════════════
    # A. durable submit-intent(크래시 안전)
    # ══════════════════════════════════════════════════════════════
    def test_intent_saved_before_kis_call(self):
        """begin_submit_intent 성공 → PENDING_SUBMIT row + lifecycle ORDER_SUBMITTED + 차단."""
        intent_id = make_order_lifecycle_id("US", "SELL", "SMCI")
        ok = self.flow._us_begin_submit_intent(
            intent_id, "SMCI", "SELL", 10, 45.0, excd="NASD")
        self.assertTrue(ok)
        row = self._row(intent_id)
        self.assertEqual(row["status"], PS.PENDING_SUBMIT)
        self.assertEqual(row["code"], "SMCI")
        self.assertEqual(self._state(intent_id), S.ORDER_SUBMITTED)
        self.assertTrue(self.flow._us_has_active_order("SMCI", "SELL"))

    def test_intent_save_failure_blocks_order(self):
        """submit-intent 저장 실패 → begin False(호출부는 KIS 미호출) + 차단/row 없음."""
        self.registry.register_intent = MagicMock(return_value=0)
        intent_id = make_order_lifecycle_id("US", "SELL", "SMCI")
        ok = self.flow._us_begin_submit_intent(
            intent_id, "SMCI", "SELL", 10, 45.0, excd="NASD")
        self.assertFalse(ok)
        # 차단 meta 없음(주문 자체를 하지 않으므로), row 없음
        self.assertFalse(self.flow._us_has_active_order("SMCI", "SELL"))

    def test_intent_save_exception_blocks_order(self):
        """register_intent 예외 → begin False(주문 미제출)."""
        self.registry.register_intent = MagicMock(side_effect=RuntimeError("db"))
        intent_id = make_order_lifecycle_id("US", "BUY", "UPST")
        ok = self.flow._us_begin_submit_intent(
            intent_id, "UPST", "BUY", 3, 20.0, excd="NASD")
        self.assertFalse(ok)

    def test_crash_after_accept_restart_blocks(self):
        """intent 저장 후 KIS 접수 직후 crash(=finalize 미도달) → 재시작 후 중복 SELL 0회."""
        intent_id = make_order_lifecycle_id("US", "SELL", "SMCI")
        self.assertTrue(self.flow._us_begin_submit_intent(
            intent_id, "SMCI", "SELL", 10, 45.0, excd="NASD"))
        # crash: finalize 호출 안 함. row=PENDING_SUBMIT, lifecycle=ORDER_SUBMITTED.
        # 재시작 재현: 새 in-memory 상태로 restore
        newflow = USFlow(self.mgr, self.registry)
        newflow._us_restore_pending_meta()
        self.assertTrue(newflow._us_has_active_order("SMCI", "SELL"))
        self.assertIn(intent_id, newflow._us_pending_sell_meta)

    def test_all_postprocess_fail_restart_blocks(self):
        """ODNO 접수 + finalize 후처리(registry/lifecycle) 모두 실패 → 재시작 차단 유지."""
        intent_id, outcome = None, None
        intent_id = make_order_lifecycle_id("US", "SELL", "QUBT")
        self.assertTrue(self.flow._us_begin_submit_intent(
            intent_id, "QUBT", "SELL", 12, 30.0, excd="NASD"))
        # finalize 의 registry/lifecycle 후처리를 모두 실패시킨다
        self.registry.update_odno = MagicMock(side_effect=RuntimeError("db"))
        self.registry.set_status = MagicMock(side_effect=RuntimeError("db"))
        self.mgr.accept = MagicMock(side_effect=RuntimeError("db"))
        outcome = self.flow._us_finalize_submit_intent(
            intent_id, "QUBT", "SELL", 12,
            {"rt_cd": "0", "output": {"ODNO": "OD-Q"}}, excd="NASD")
        self.assertEqual(outcome, _US_OUTCOME_ACCEPTED)
        # durable: registry 는 여전히 PENDING_SUBMIT(승격 실패했지만 앵커 유지)
        self.assertEqual(self._row(intent_id)["status"], PS.PENDING_SUBMIT)
        # 재시작 → 차단 유지
        newflow = USFlow(self.mgr, self.registry)
        newflow._us_restore_pending_meta()
        self.assertTrue(newflow._us_has_active_order("QUBT", "SELL"))

    # ══════════════════════════════════════════════════════════════
    # B. rt_cd 분류(명확 거절만 REJECTED)
    # ══════════════════════════════════════════════════════════════
    def test_classify_odno_present_is_accepted(self):
        _, odno = self.flow._us_classify_order_outcome(
            {"rt_cd": "0", "output": {"ODNO": "X"}})
        self.assertEqual(odno, "X")
        out, _ = self.flow._us_classify_order_outcome(
            {"rt_cd": "0", "output": {"ODNO": "X"}})
        self.assertEqual(out, _US_OUTCOME_ACCEPTED)

    def test_rt_cd_9_is_unknown_confirm_not_rejected(self):
        """rt_cd=9(예외 래핑) → UNKNOWN_CONFIRM, REJECTED 아님, 차단 유지."""
        lc_id, outcome = self._order(
            "SELL", "SMCI", 10, {"rt_cd": "9", "msg1": "HTTPError 500"})
        self.assertEqual(outcome, _US_OUTCOME_UNKNOWN_CONFIRM)
        self.assertEqual(self._row(lc_id)["status"], PS.UNKNOWN_CONFIRM)
        self.assertTrue(self.flow._us_has_active_order("SMCI", "SELL"))

    def test_http500_timeout_parsefail_is_unknown_confirm(self):
        for res in ({"rt_cd": "9", "msg1": "timeout"},
                    {"rt_cd": "9", "msg1": "500 Server Error"},
                    {"rt_cd": "9", "msg1": "Expecting value: JSONDecodeError"}):
            lc_id, outcome = self._order("SELL", "IONQ", 5, res)
            self.assertEqual(outcome, _US_OUTCOME_UNKNOWN_CONFIRM, res)
            self.assertTrue(self.flow._us_has_active_order("IONQ", "SELL"))

    def test_rtcd_nonzero_with_odno_is_accepted(self):
        """rt_cd≠0 이지만 ODNO 존재 → 외부 접수 가능 → ACCEPTED(차단 유지)."""
        lc_id, outcome = self._order(
            "SELL", "RXRX", 8, {"rt_cd": "1", "output": {"ODNO": "OD-R"}})
        self.assertEqual(outcome, _US_OUTCOME_ACCEPTED)
        self.assertEqual(self._row(lc_id)["status"], PS.ACCEPTED)
        self.assertTrue(self.flow._us_has_active_order("RXRX", "SELL"))

    def test_allowlist_clear_reject_no_odno_is_rejected(self):
        """allowlist 명확 거절 + ODNO 없음 → REJECTED, 차단 해제."""
        lc_id, outcome = self._order(
            "SELL", "SMCI", 10,
            {"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "매도가능수량 부족합니다"})
        self.assertEqual(outcome, _US_OUTCOME_REJECTED)
        self.assertEqual(self._row(lc_id)["status"], PS.REJECTED)
        self.assertTrue(self.mgr.load(lc_id).is_terminal)
        self.assertFalse(self.flow._us_has_active_order("SMCI", "SELL"))

    def test_unknown_code_no_odno_is_unknown_confirm(self):
        """알 수 없는 거절 코드 + ODNO 없음 → UNKNOWN_CONFIRM(차단 유지)."""
        lc_id, outcome = self._order(
            "SELL", "QUBT", 12, {"rt_cd": "1", "msg_cd": "ZZZ9999",
                                 "msg1": "알수없는응답코드"})
        self.assertEqual(outcome, _US_OUTCOME_UNKNOWN_CONFIRM)
        self.assertEqual(self._row(lc_id)["status"], PS.UNKNOWN_CONFIRM)
        self.assertTrue(self.flow._us_has_active_order("QUBT", "SELL"))

    def test_dry_run_is_not_sent(self):
        """dry-run/live-disabled → NOT_SENT, 차단 해제(미전송)."""
        lc_id, outcome = self._order(
            "SELL", "SMCI", 10, {"rt_cd": "9", "_dry_run": True,
                                 "_live_disabled": True, "msg1": "dry-run"})
        self.assertEqual(outcome, _US_OUTCOME_NOT_SENT)
        self.assertFalse(self.flow._us_has_active_order("SMCI", "SELL"))
        self.assertTrue(self.mgr.load(lc_id).is_terminal)

    def test_rt_cd_0_no_odno_is_unknown_confirm(self):
        """rt_cd=0 이나 ODNO 미수신 → UNKNOWN_CONFIRM, 차단 유지."""
        lc_id, outcome = self._order("SELL", "CEG", 9, {"rt_cd": "0", "output": {}})
        self.assertEqual(outcome, _US_OUTCOME_UNKNOWN_CONFIRM)
        self.assertEqual(self._state(lc_id), S.ORDER_SUBMITTED)
        self.assertTrue(self.flow._us_has_active_order("CEG", "SELL"))

    # BUY/SELL 각각 동일 검증
    def test_buy_side_same_classification(self):
        for res, exp, blocked in [
            ({"rt_cd": "0", "output": {"ODNO": "B1"}}, _US_OUTCOME_ACCEPTED, True),
            ({"rt_cd": "9", "msg1": "timeout"}, _US_OUTCOME_UNKNOWN_CONFIRM, True),
            ({"rt_cd": "1", "msg1": "매수가능금액 부족"}, _US_OUTCOME_REJECTED, False),
            ({"rt_cd": "1", "output": {"ODNO": "B2"}}, _US_OUTCOME_ACCEPTED, True),
        ]:
            lc_id, outcome = self._order("BUY", "UPST", 3, res)
            self.assertEqual(outcome, exp, res)
            self.assertEqual(self.flow._us_has_active_order("UPST", "BUY"), blocked, res)
            # 다음 케이스를 위해 차단 정리
            self.flow._us_pending_buy_meta.clear()

    # ══════════════════════════════════════════════════════════════
    # C. 정상 전이 + 회귀 고정
    # ══════════════════════════════════════════════════════════════
    def test_sell_normal_transition_all_stages(self):
        lc_id, outcome = self._order(
            "SELL", "SMCI", 10, {"rt_cd": "0", "output": {"ODNO": "OD-1"}})
        self.assertEqual(outcome, _US_OUTCOME_ACCEPTED)
        self.assertEqual(self._state(lc_id), S.ORDER_ACCEPTED)
        self.assertEqual(self.mgr.load(lc_id).odno, "OD-1")
        self.assertEqual(self._row(lc_id)["status"], PS.ACCEPTED)
        self.flow.us_dispatch_fill(lc_id, filled_qty=4, avg_fill_price=50.0, is_full=False)
        self.assertEqual(self._state(lc_id), S.PARTIALLY_FILLED)
        self.flow.us_dispatch_fill(lc_id, filled_qty=6, avg_fill_price=50.0, is_full=True)
        self.assertEqual(self._state(lc_id), S.FILLED)

    def test_regression_direct_accept_from_unknown_raises(self):
        from phoenix.lifecycle import LifecycleTransitionError
        lc = self.mgr.create(trade_id="t", market="US", code="IONQ",
                             side="SELL", order_qty=5)
        with self.assertRaises(LifecycleTransitionError):
            self.mgr.accept(lc)

    def test_cancel_local_only_refuses_when_odno_present(self):
        lc = self.mgr.create(trade_id="t", market="US", code="QUBT",
                             side="SELL", order_qty=5)
        self.mgr.confirm_signal(lc); self.mgr.submit(lc); self.mgr.accept(lc, "OD-999")
        res = self.mgr.cancel_local_only(lc, reason="should refuse")
        self.assertEqual(res, S.ORDER_ACCEPTED)
        self.assertFalse(self.mgr.load(lc.order_lifecycle_id).is_terminal)

    # ══════════════════════════════════════════════════════════════
    # D. 중복 차단 / 로컬 단말 해소 / 불명확 유지
    # ══════════════════════════════════════════════════════════════
    def test_duplicate_sell_blocked_when_real_unfilled(self):
        self._order("SELL", "SMCI", 10, {"rt_cd": "0", "output": {"ODNO": "OD-1"}})
        self.assertTrue(self.flow._us_has_active_order("SMCI", "SELL"))

    def test_ghost_resolved_on_filled(self):
        lc_id, _ = self._order("SELL", "IONQ", 5, {"rt_cd": "0", "output": {"ODNO": "OD-I"}})
        self.assertTrue(self.flow._us_has_active_order("IONQ", "SELL"))
        self.flow.us_dispatch_fill(lc_id, filled_qty=5, avg_fill_price=12.0, is_full=True)
        self.assertFalse(self.flow._us_has_active_order("IONQ", "SELL"))

    def test_ambiguous_state_no_release(self):
        lc_id, _ = self._order("SELL", "ASTS", 7, {"rt_cd": "0", "output": {"ODNO": "OD-A"}})
        self.assertFalse(self.flow._us_meta_is_stale(lc_id))
        self.assertTrue(self.flow._us_has_active_order("ASTS", "SELL"))

    # ══════════════════════════════════════════════════════════════
    # E. FILLED 멱등 부킹(req12)
    # ══════════════════════════════════════════════════════════════
    def test_filled_idempotent_booking(self):
        outbox = FakeOutbox()
        self.flow._us_outbox = outbox
        self.flow._us_process_outbox_row = MagicMock()
        pos = MagicMock(); pos.qty = 5; pos.avg_price = 10.0
        self.flow.pos_mgr.positions = {"SMCI": pos}
        lc_id, _ = self._order("SELL", "SMCI", 5, {"rt_cd": "0", "output": {"ODNO": "OD-F"}})
        self.flow.us_dispatch_fill(lc_id, filled_qty=5, avg_fill_price=12.0, is_full=True)
        self.assertEqual(self.flow._us_process_outbox_row.call_count, 1)
        self.flow._us_apply_fill_delta(self.mgr.load(lc_id))
        self.assertEqual(self.flow._us_process_outbox_row.call_count, 1)

    # ══════════════════════════════════════════════════════════════
    # F. 재시작 정합화
    # ══════════════════════════════════════════════════════════════
    def test_restart_recovers_incident_residue(self):
        lc = self.mgr.create(trade_id="t-smci", market="US", code="SMCI",
                             side="SELL", order_qty=10)
        lc_id = lc.order_lifecycle_id
        self.registry.register(
            market="US", trade_id=lc_id, code="SMCI", side="SELL",
            order_qty=10, submitted_at="2026-08-12T22:00:00", odno="OD-GHOST",
            currency="USD")
        self.flow._us_restore_pending_meta()
        self.assertEqual(self._state(lc_id), S.ORDER_ACCEPTED)
        self.assertEqual(self.mgr.load(lc_id).odno, "OD-GHOST")
        self.assertTrue(self.flow._us_has_active_order("SMCI", "SELL"))

    def test_restart_keeps_block_on_pending_submit(self):
        """재시작: registry PENDING_SUBMIT(crash 앵커) → 차단 유지."""
        intent_id = make_order_lifecycle_id("US", "SELL", "QUBT")
        self.flow._us_begin_submit_intent(intent_id, "QUBT", "SELL", 12, 30.0, "NASD")
        newflow = USFlow(self.mgr, self.registry)
        newflow._us_restore_pending_meta()
        self.assertTrue(newflow._us_has_active_order("QUBT", "SELL"))

    def test_restart_keeps_block_on_registry_expired(self):
        lc = self.mgr.create(trade_id="t-exp", market="US", code="IONQ",
                             side="SELL", order_qty=5)
        lc_id = lc.order_lifecycle_id
        self.registry.register(
            market="US", trade_id=lc_id, code="IONQ", side="SELL",
            order_qty=5, submitted_at="2026-08-12T22:00:00", odno="OD-EXP",
            currency="USD")
        self.registry.set_status(lc_id, PS.EXPIRED)
        self.flow._us_restore_pending_meta()
        self.assertTrue(self.flow._us_has_active_order("IONQ", "SELL"))
        self.assertFalse(self.flow._us_meta_is_stale(lc_id))

    def test_restart_skips_resolved(self):
        """재시작: registry FILLED/REJECTED(해소) → 차단 미복원."""
        for sym, status in (("IOVA", PS.FILLED), ("ASTS", PS.REJECTED)):
            lc = self.mgr.create(trade_id=f"t-{sym}", market="US", code=sym,
                                 side="SELL", order_qty=6)
            lc_id = lc.order_lifecycle_id
            self.registry.register(
                market="US", trade_id=lc_id, code=sym, side="SELL",
                order_qty=6, submitted_at="2026-08-12T22:00:00", odno=f"OD-{sym}",
                currency="USD")
            self.registry.set_status(lc_id, status)
            self.flow._us_restore_pending_meta()
            self.assertFalse(self.flow._us_has_active_order(sym, "SELL"), sym)

    def test_restart_cancels_unsubmitted_orphan(self):
        lc = self.mgr.create(trade_id="t-rklb", market="US", code="RKLB",
                             side="BUY", order_qty=4)
        lc_id = lc.order_lifecycle_id
        self.flow._us_restore_pending_meta()
        self.assertTrue(self.mgr.load(lc_id).is_terminal)
        self.assertFalse(self.flow._us_has_active_order("RKLB", "BUY"))

    def test_restart_load_all_active_failure_no_exception(self):
        self.mgr.load_all_active = MagicMock(side_effect=RuntimeError("db"))
        try:
            self.flow._us_restore_pending_meta()
        except Exception as e:
            self.fail(f"_us_restore_pending_meta raised: {e}")

    # ══════════════════════════════════════════════════════════════
    # G. 운영자 수동 해제(감사)
    # ══════════════════════════════════════════════════════════════
    def test_manual_release_requires_operator_reason_verified(self):
        lc_id, _ = self._order("SELL", "SMCI", 10, {"rt_cd": "0", "output": {"ODNO": "OD-M"}})
        for op, rs, ok in [("", "r", True), ("op", "", True), ("op", "r", False)]:
            res = self.flow.us_manual_release_pending(
                lc_id, operator=op, reason=rs, verified_no_open_order=ok)
            self.assertFalse(res["ok"])
        self.assertTrue(self.flow._us_has_active_order("SMCI", "SELL"))

    def test_manual_release_success_with_audit(self):
        lc_id, _ = self._order("SELL", "SMCI", 10, {"rt_cd": "0", "output": {"ODNO": "OD-M2"}})
        with self.assertLogs("USStrategy", level="WARNING") as cm:
            res = self.flow.us_manual_release_pending(
                lc_id, operator="alice", reason="KIS 앱 미체결 없음 확인",
                verified_no_open_order=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["odno"], "OD-M2")
        self.assertTrue(any("US AUDIT" in m and "alice" in m for m in cm.output))
        self.assertFalse(self.flow._us_has_active_order("SMCI", "SELL"))
        self.assertEqual(self._row(lc_id)["status"], PS.CANCELLED)
        self.assertTrue(self.mgr.load(lc_id).is_terminal)

    def test_list_pending_confirm_reports_blocked(self):
        self._order("SELL", "SMCI", 10, {"rt_cd": "0", "output": {"ODNO": "OD-L"}})
        rows = self.flow.us_list_pending_confirm()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "SMCI")
        self.assertEqual(rows[0]["odno"], "OD-L")

    # ══════════════════════════════════════════════════════════════
    # H. 사고 4종목 residue 복원·차단·분기
    # ══════════════════════════════════════════════════════════════
    def test_incident_residue_restart_recovers_and_blocks(self):
        residue = [("SMCI", 19), ("IONQ", 5), ("QUBT", 50), ("RXRX", 40)]
        ids = {}
        for sym, qty in residue:
            lc = self.mgr.create(trade_id=f"t-{sym}", market="US", code=sym,
                                 side="SELL", order_qty=qty)
            ids[sym] = lc.order_lifecycle_id
            self.registry.register(
                market="US", trade_id=lc.order_lifecycle_id, code=sym, side="SELL",
                order_qty=qty, submitted_at="2026-08-12T22:00:00",
                odno=f"OD-{sym}", currency="USD")
        self.flow._us_restore_pending_meta()
        report = {r["symbol"]: r for r in self.flow.us_list_pending_confirm()}
        for sym, _ in residue:
            self.assertEqual(self._state(ids[sym]), S.ORDER_ACCEPTED, sym)
            self.assertTrue(self.flow._us_has_active_order(sym, "SELL"), sym)
            self.assertIn(sym, report)
        # IONQ 체결 → 해제
        self.flow.us_dispatch_fill(ids["IONQ"], filled_qty=5, avg_fill_price=12.0, is_full=True)
        self.assertFalse(self.flow._us_has_active_order("IONQ", "SELL"))
        # QUBT 운영자 수동 해제
        self.flow.us_manual_release_pending(
            ids["QUBT"], operator="ops", reason="KIS 미체결 없음", verified_no_open_order=True)
        self.assertFalse(self.flow._us_has_active_order("QUBT", "SELL"))
        # SMCI/RXRX: 증거 없음 → 확인대기 유지(영구차단 아님, 수동 해제 대기)
        self.assertTrue(self.flow._us_has_active_order("SMCI", "SELL"))
        self.assertTrue(self.flow._us_has_active_order("RXRX", "SELL"))


if __name__ == "__main__":
    unittest.main()
