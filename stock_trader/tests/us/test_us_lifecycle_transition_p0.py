"""US 라이프사이클 상태전이 P0 수정 입증 (2026-08-12 사고 재현/회귀 고정).

사고: US SELL 접수 성공(ODNO 확보) 직후 lifecycle 이
  "UNKNOWN → ORDER_ACCEPTED 허용되지 않는 상태 전이" 로 실패 →
  registry ACCEPTED row + in-memory 차단 meta 가 ghost 로 남아
  SMCI/QUBT/IONQ/RXRX 매도가 장 마감까지 "미체결 매도 주문 존재 → 중복 매도 스킵"
  으로 영구 차단됨.

이 스위트는 실제 OrderLifecycleManager / PendingOrderRegistry 와
USStrategyManager 의 실제 메서드(_us_register_pending_order / _us_has_active_order /
_us_meta_is_stale / _us_restore_pending_meta / us_dispatch_fill / _us_apply_fill_delta)
를 바인딩해 다음을 입증한다:

  1. US SELL/BUY 정상 상태전이(UNKNOWN→SIGNAL_CONFIRMED→ORDER_SUBMITTED→ORDER_ACCEPTED)
     가 단일 helper 로 완료 — 사고 전이 예외 없음.
  2. register 실패(row 거부) 시 ghost active(registry·lifecycle·meta) 없음(rollback).
  3. advance 실패 시 registry REJECTED + lifecycle CANCELLED 로 이중 rollback.
  4. 동일 종목 실제 미체결 주문 존재 시 중복 매도 차단(req6).
  5. 내부 ghost + 로컬 단말 증거(FILLED/CANCELLED) 시 차단 해소, 재매도 허용(req5/7).
  6. 상태 불명확(로컬 비단말) 시 자동 해제·재주문 없음(req8).
  7. ODNO 없는 성공(rt_cd=0·미수신) → HOLD(확인대기), 차단 유지·재주문 금지(req9).
  8. FILLED 멱등 부킹 — delta 게이트로 정확히 1회(req12).
  9. 재시작 정합화: 사고 residue(registry ACCEPTED + lc UNKNOWN) 복구 + HOLD 유지 +
     orphan 정리(req10).
 10. SMCI/IONQ SELL_ALL + QUBT/RXRX trailing 시나리오 재현.
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
    _US_REG_REGISTERED, _US_REG_HOLD, _US_REG_ROLLBACK,
)

S = LifecycleState


class FakeOutbox:
    """_us_apply_fill_delta 의 delta 멱등 게이트만 검증하기 위한 경량 outbox."""

    def __init__(self):
        self.rows = {}
        self._cum = {}          # oid -> (cum_qty, cum_cost)

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
    """USStrategyManager 의 실제 등록/가드/정합화/체결 메서드만 바인딩한 경량 인스턴스."""
    _us_register_pending_order = _USM._us_register_pending_order
    _us_has_active_order       = _USM._us_has_active_order
    _us_meta_is_stale          = _USM._us_meta_is_stale
    _us_restore_pending_meta   = _USM._us_restore_pending_meta
    us_dispatch_fill           = _USM.us_dispatch_fill
    _us_apply_fill_delta       = _USM._us_apply_fill_delta

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


class USLifecycleTransitionP0Test(unittest.TestCase):
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

    # ── 헬퍼: 실제 caller(_do_buy/_do_sell 접수 경로)를 모사 ──────────────
    def _submit(self, side, symbol, qty, order_response, trade_id=""):
        """create(UNKNOWN) → put meta → _us_register_pending_order → rollback 시 meta pop.
        실제 caller 의 meta 처리 규약과 동일하게 동작한다."""
        lc_id = make_order_lifecycle_id("US", side, symbol)
        lc = self.mgr.create(
            trade_id=trade_id or lc_id, market="US", code=symbol,
            side=side, strategy_name="T", order_qty=qty)
        meta = (self.flow._us_pending_buy_meta if side == "BUY"
                else self.flow._us_pending_sell_meta)
        meta[lc.order_lifecycle_id] = {
            "code": symbol, "name": symbol, "qty": qty,
            "excd": "NASD", "reason": "t",
        }
        res = self.flow._us_register_pending_order(
            symbol=symbol, side=side, order_qty=qty,
            order_response=order_response, lifecycle_id=lc.order_lifecycle_id,
            excd="NASD", trade_id=trade_id)
        if res == _US_REG_ROLLBACK:
            meta.pop(lc.order_lifecycle_id, None)
        return lc.order_lifecycle_id, res

    def _state(self, lc_id):
        return self.mgr.load(lc_id).current_state

    # ══════════════════════════════════════════════════════════════
    # 1. 정상 상태전이 전 단계
    # ══════════════════════════════════════════════════════════════
    def test_sell_normal_transition_all_stages(self):
        """US SELL: UNKNOWN→…→ORDER_ACCEPTED 정상 완료(사고 전이예외 없음)."""
        lc_id, res = self._submit(
            "SELL", "SMCI", 10, {"rt_cd": "0", "output": {"ODNO": "OD-SELL-1"}})
        self.assertEqual(res, _US_REG_REGISTERED)
        self.assertEqual(self._state(lc_id), S.ORDER_ACCEPTED)
        self.assertEqual(self.mgr.load(lc_id).odno, "OD-SELL-1")
        # registry 미체결 매도 존재
        self.assertTrue(self.registry.has_active_sell("US", "SMCI"))
        # 차단 meta 존재
        self.assertIn(lc_id, self.flow._us_pending_sell_meta)
        # 체결: 부분 → 전량
        self.flow.us_dispatch_fill(lc_id, filled_qty=4, avg_fill_price=50.0, is_full=False)
        self.assertEqual(self._state(lc_id), S.PARTIALLY_FILLED)
        self.flow.us_dispatch_fill(lc_id, filled_qty=6, avg_fill_price=50.0, is_full=True)
        self.assertEqual(self._state(lc_id), S.FILLED)
        self.assertEqual(self.mgr.load(lc_id).filled_qty, 10)

    def test_buy_normal_transition_all_stages(self):
        """US BUY: UNKNOWN→…→ORDER_ACCEPTED 정상 완료."""
        lc_id, res = self._submit(
            "BUY", "UPST", 3, {"rt_cd": "0", "output": {"ODNO": "OD-BUY-1"}})
        self.assertEqual(res, _US_REG_REGISTERED)
        self.assertEqual(self._state(lc_id), S.ORDER_ACCEPTED)
        self.assertTrue(self.registry.has_active_order("US", "UPST", "BUY"))
        self.assertIn(lc_id, self.flow._us_pending_buy_meta)

    def test_regression_direct_accept_from_unknown_raises(self):
        """회귀 고정: 사고 원인인 UNKNOWN→ORDER_ACCEPTED 직접 전이는 반드시 예외."""
        from phoenix.lifecycle import LifecycleTransitionError
        lc = self.mgr.create(trade_id="t", market="US", code="IONQ",
                             side="SELL", order_qty=5)
        with self.assertRaises(LifecycleTransitionError):
            self.mgr.accept(lc)

    # ══════════════════════════════════════════════════════════════
    # 2~3. register/advance 실패 rollback → ghost 없음
    # ══════════════════════════════════════════════════════════════
    def test_register_row_reject_no_ghost(self):
        """register row 거부 → ROLLBACK, registry·lifecycle·meta ghost 없음."""
        # registry.register 를 거부(0 반환)로 강제
        self.registry.register = MagicMock(return_value=0)
        lc_id, res = self._submit(
            "SELL", "RXRX", 8, {"rt_cd": "0", "output": {"ODNO": "OD-X"}})
        self.assertEqual(res, _US_REG_ROLLBACK)
        # lifecycle 단말(CANCELLED)
        self.assertTrue(self.mgr.load(lc_id).is_terminal)
        # meta 없음 → 후속 매도 차단 안 됨
        self.assertNotIn(lc_id, self.flow._us_pending_sell_meta)
        self.assertFalse(self.flow._us_has_active_order("RXRX", "SELL"))

    def test_advance_failure_double_rollback(self):
        """advance 전이 예외 → registry REJECTED + lifecycle CANCELLED 이중 rollback."""
        # accept 단계에서 예외를 강제(전이는 submit 까지 진행됨)
        self.mgr.accept = MagicMock(side_effect=RuntimeError("boom"))
        lc_id, res = self._submit(
            "SELL", "QUBT", 12, {"rt_cd": "0", "output": {"ODNO": "OD-Q"}})
        self.assertEqual(res, _US_REG_ROLLBACK)
        # registry row 는 REJECTED 로 롤백
        row = self.registry.get_by_trade_id(lc_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], PendingStatus.REJECTED)
        # lifecycle 단말
        self.assertTrue(self.mgr.load(lc_id).is_terminal)
        # meta 제거 → 미체결 매도 없음
        self.assertFalse(self.registry.has_active_sell("US", "QUBT"))
        self.assertFalse(self.flow._us_has_active_order("QUBT", "SELL"))

    # ══════════════════════════════════════════════════════════════
    # 4. 실제 미체결 존재 → 중복 매도 차단(req6)
    # ══════════════════════════════════════════════════════════════
    def test_duplicate_sell_blocked_when_real_unfilled(self):
        lc_id, res = self._submit(
            "SELL", "SMCI", 10, {"rt_cd": "0", "output": {"ODNO": "OD-1"}})
        self.assertEqual(res, _US_REG_REGISTERED)
        # 동일 종목 재매도 시도 → 차단
        self.assertTrue(self.flow._us_has_active_order("SMCI", "SELL"))

    # ══════════════════════════════════════════════════════════════
    # 5. 로컬 단말 증거 → ghost 해소, 재매도 허용(req5/7)
    # ══════════════════════════════════════════════════════════════
    def test_ghost_resolved_on_filled(self):
        """lifecycle FILLED(로컬 단말) → 차단 해소."""
        lc_id, _ = self._submit(
            "SELL", "IONQ", 5, {"rt_cd": "0", "output": {"ODNO": "OD-I"}})
        self.assertTrue(self.flow._us_has_active_order("IONQ", "SELL"))
        # 전량 체결로 lifecycle FILLED
        self.flow.us_dispatch_fill(lc_id, filled_qty=5, avg_fill_price=12.0, is_full=True)
        self.assertEqual(self._state(lc_id), S.FILLED)
        # 이제 차단 해소 + meta ghost 제거
        self.assertFalse(self.flow._us_has_active_order("IONQ", "SELL"))
        self.assertNotIn(lc_id, self.flow._us_pending_sell_meta)

    def test_ghost_resolved_on_registry_cancelled(self):
        """registry 단말(CANCELLED) → 차단 해소(주문취소 감지)."""
        lc_id, _ = self._submit(
            "SELL", "IONQ", 5, {"rt_cd": "0", "output": {"ODNO": "OD-I2"}})
        self.assertTrue(self.flow._us_has_active_order("IONQ", "SELL"))
        # 외부 취소 → registry 단말
        self.registry.mark_terminal(lc_id, PendingStatus.CANCELLED, reason="canceled")
        self.assertFalse(self.flow._us_has_active_order("IONQ", "SELL"))
        self.assertNotIn(lc_id, self.flow._us_pending_sell_meta)

    # ══════════════════════════════════════════════════════════════
    # 6. 상태 불명확 → 자동 해제·재주문 없음(req8)
    # ══════════════════════════════════════════════════════════════
    def test_ambiguous_state_no_release(self):
        """lifecycle 비단말 + registry ACCEPTED → 차단 유지(자동 해제 금지)."""
        lc_id, _ = self._submit(
            "SELL", "ASTS", 7, {"rt_cd": "0", "output": {"ODNO": "OD-A"}})
        # ACCEPTED(비단말) 상태 — 로컬 단말 증거 없음
        self.assertFalse(self.flow._us_meta_is_stale(lc_id))
        self.assertTrue(self.flow._us_has_active_order("ASTS", "SELL"))
        # 여러 번 조회해도 계속 차단(재주문 금지)
        self.assertTrue(self.flow._us_has_active_order("ASTS", "SELL"))

    # ══════════════════════════════════════════════════════════════
    # 7. ODNO 없는 성공 → HOLD(확인대기), 차단 유지(req9)
    # ══════════════════════════════════════════════════════════════
    def test_no_odno_success_holds(self):
        """rt_cd=0 이나 ODNO 미수신 → HOLD, lifecycle ORDER_SUBMITTED, 차단 유지."""
        lc_id, res = self._submit(
            "SELL", "CEG", 9, {"rt_cd": "0", "output": {}})
        self.assertEqual(res, _US_REG_HOLD)
        # lifecycle 은 제출됨(ORDER_SUBMITTED) — 확인대기 durable
        self.assertEqual(self._state(lc_id), S.ORDER_SUBMITTED)
        # registry row 없음(odno 필수)
        self.assertIsNone(self.registry.get_by_trade_id(lc_id))
        # 차단 meta 유지 → 재주문 금지
        self.assertIn(lc_id, self.flow._us_pending_sell_meta)
        self.assertTrue(self.flow._us_has_active_order("CEG", "SELL"))

    # ══════════════════════════════════════════════════════════════
    # 8. FILLED 멱등 부킹(req12)
    # ══════════════════════════════════════════════════════════════
    def test_filled_idempotent_booking(self):
        """동일 누적 체결 재적용 → 부킹 정확히 1회(delta 게이트)."""
        outbox = FakeOutbox()
        self.flow._us_outbox = outbox
        self.flow._us_process_outbox_row = MagicMock()
        # 포지션 세팅(SELL 실현손익 경로)
        pos = MagicMock(); pos.qty = 5; pos.avg_price = 10.0
        self.flow.pos_mgr.positions = {"SMCI": pos}

        lc_id, _ = self._submit(
            "SELL", "SMCI", 5, {"rt_cd": "0", "output": {"ODNO": "OD-F"}})
        # 전량 체결 dispatch → 부킹 1회
        self.flow.us_dispatch_fill(lc_id, filled_qty=5, avg_fill_price=12.0, is_full=True)
        self.assertEqual(self.flow._us_process_outbox_row.call_count, 1)
        # 동일 lc 재적용(중복 폴링/재시작 재조회) → delta 0 → 부킹 추가 없음
        lc2 = self.mgr.load(lc_id)
        self.flow._us_apply_fill_delta(lc2)
        self.assertEqual(self.flow._us_process_outbox_row.call_count, 1)

    # ══════════════════════════════════════════════════════════════
    # 9. 재시작 정합화(req10)
    # ══════════════════════════════════════════════════════════════
    def test_restart_recovers_incident_residue(self):
        """사고 residue: registry ACCEPTED(실 ODNO) + lifecycle UNKNOWN(odno='')
        → 재시작 시 advance 로 일치, 차단 meta 복원(중복 매도 방지)."""
        # 사고 재현: lifecycle 은 UNKNOWN 그대로, registry 는 ACCEPTED row 직접 삽입
        lc = self.mgr.create(trade_id="t-smci", market="US", code="SMCI",
                             side="SELL", order_qty=10)
        lc_id = lc.order_lifecycle_id
        self.registry.register(
            market="US", trade_id=lc_id, code="SMCI", side="SELL",
            order_qty=10, submitted_at="2026-08-12T22:00:00", odno="OD-GHOST",
            currency="USD")
        self.assertEqual(self._state(lc_id), S.UNKNOWN)

        # 재시작 정합화
        self.flow._us_restore_pending_meta()

        # lifecycle 이 ACCEPTED 로 정합화되고 odno 주입
        self.assertEqual(self._state(lc_id), S.ORDER_ACCEPTED)
        self.assertEqual(self.mgr.load(lc_id).odno, "OD-GHOST")
        # 차단 meta 복원 → 중복 매도 방지(단, 실제 미체결이므로 정당한 차단)
        self.assertIn(lc_id, self.flow._us_pending_sell_meta)
        self.assertTrue(self.flow._us_has_active_order("SMCI", "SELL"))

    def test_restart_holds_submitted_no_odno(self):
        """재시작: ORDER_SUBMITTED + odno·registry 근거 無(HOLD 흔적) → 차단 복원."""
        lc = self.mgr.create(trade_id="t-ceg", market="US", code="CEG",
                             side="SELL", order_qty=9)
        lc_id = lc.order_lifecycle_id
        self.mgr.confirm_signal(lc)
        self.mgr.submit(lc)   # ORDER_SUBMITTED, odno='' , registry row 없음
        self.flow._us_restore_pending_meta()
        self.assertEqual(self._state(lc_id), S.ORDER_SUBMITTED)
        self.assertIn(lc_id, self.flow._us_pending_sell_meta)

    def test_restart_cancels_unsubmitted_orphan(self):
        """재시작: UNKNOWN + 근거 全無(미제출 orphan) → CANCELLED, 차단 미복원."""
        lc = self.mgr.create(trade_id="t-rklb", market="US", code="RKLB",
                             side="BUY", order_qty=4)
        lc_id = lc.order_lifecycle_id
        self.flow._us_restore_pending_meta()
        self.assertTrue(self.mgr.load(lc_id).is_terminal)
        self.assertNotIn(lc_id, self.flow._us_pending_buy_meta)

    def test_restart_skips_registry_terminal(self):
        """재시작: registry 단말(FILLED) → 차단 미복원(이미 해소)."""
        lc = self.mgr.create(trade_id="t-iova", market="US", code="IOVA",
                             side="SELL", order_qty=6)
        lc_id = lc.order_lifecycle_id
        self.registry.register(
            market="US", trade_id=lc_id, code="IOVA", side="SELL",
            order_qty=6, submitted_at="2026-08-12T22:00:00", odno="OD-DONE",
            currency="USD")
        # FILLED 는 체결 관측 경로에서만 도달하므로 여기선 set_status 로 직접 반영
        self.registry.set_status(lc_id, PendingStatus.FILLED)
        self.flow._us_restore_pending_meta()
        self.assertNotIn(lc_id, self.flow._us_pending_sell_meta)

    def test_restart_load_all_active_failure_no_exception(self):
        """load_all_active 예외 → 예외 미전파(안전)."""
        self.mgr.load_all_active = MagicMock(side_effect=RuntimeError("db"))
        try:
            self.flow._us_restore_pending_meta()
        except Exception as e:
            self.fail(f"_us_restore_pending_meta raised: {e}")

    # ══════════════════════════════════════════════════════════════
    # 10. 사고 종목 시나리오 재현(SMCI/IONQ SELL_ALL + QUBT/RXRX trailing)
    # ══════════════════════════════════════════════════════════════
    def test_incident_tickers_sell_all_and_trailing(self):
        """SMCI/IONQ(SELL_ALL) + QUBT/RXRX(trailing) 모두 접수 전이 정상 + 재매도 규약."""
        for sym, qty in [("SMCI", 19), ("IONQ", 30), ("QUBT", 50), ("RXRX", 40)]:
            lc_id, res = self._submit(
                "SELL", sym, qty, {"rt_cd": "0", "output": {"ODNO": f"OD-{sym}"}})
            # 사고와 달리 전이 예외 없이 ACCEPTED
            self.assertEqual(res, _US_REG_REGISTERED, sym)
            self.assertEqual(self._state(lc_id), S.ORDER_ACCEPTED, sym)
            # 접수 직후 동일 종목 재매도는 정당하게 차단(req6)
            self.assertTrue(self.flow._us_has_active_order(sym, "SELL"), sym)
            # 체결되면 차단 해소 → 다음 신호에 재매도 가능(영구차단 아님)
            self.flow.us_dispatch_fill(lc_id, filled_qty=qty, avg_fill_price=10.0, is_full=True)
            self.assertEqual(self._state(lc_id), S.FILLED, sym)
            self.assertFalse(self.flow._us_has_active_order(sym, "SELL"), sym)


if __name__ == "__main__":
    unittest.main()
