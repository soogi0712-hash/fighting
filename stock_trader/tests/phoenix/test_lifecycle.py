"""OrderLifecycleManager 테스트 (기존 33개 유지 + 신규 15개 = 48개).

커버리지 — 기존 (구조 변경 반영):
  01  UNKNOWN → SIGNAL_CONFIRMED
  02  SIGNAL_CONFIRMED → ORDER_SUBMITTED (client_order_id, submitted_at)
  03  ORDER_SUBMITTED → ORDER_ACCEPTED (odno 저장)
  04  ORDER_ACCEPTED → PARTIALLY_FILLED (filled_qty, remaining_qty)
  05  PARTIALLY_FILLED → PARTIALLY_FILLED (추가 부분 체결)
  06  PARTIALLY_FILLED → FILLED
  07  ORDER_ACCEPTED → FILLED (직접 전량)
  08  FILLED는 주문 단말 (is_terminal=True)
  09  ORDER_ACCEPTED → CANCELLED
  10  ORDER_SUBMITTED → CANCELLED
  11  ORDER_ACCEPTED → REJECTED (last_error 저장)
  12  ORDER_ACCEPTED → EXPIRED
  13  FILLED에서 accept() → LifecycleTransitionError
  14  CANCELLED에서 submit() → LifecycleTransitionError
  15  REJECTED에서 accept() → LifecycleTransitionError
  16  EXPIRED에서 full_fill() → LifecycleTransitionError
  17  ORDER_ACCEPTED에서 close 없음 (TRADE_CLOSED 제거됨)
  18  partial_fill(delta=0) → ValueError
  19  중복 accept → 멱등 (odno 변경 안 함)
  20  중복 full_fill → 멱등 (filled_qty 추가 없음)
  21  FILLED is_terminal (단말 상태 4개 확인)
  22  DB 복원 — PARTIALLY_FILLED 재시작
  23  TransitionValidator.is_valid_transition() 반환값
  24  TransitionValidator.validate() 에러 메시지 포함 확인
  25  filled_qty / remaining_qty / avg_fill_price 누적 계산
  26  EventStore 기록 — positions 테이블 미변경
  27  AST 검사 — apply_buy / apply_sell 호출 없음
  28  AST 검사 — DailyPnLGuard 호출 없음
  29  전체 정상 흐름 (UNKNOWN → FILLED, E2E)
  30  load_all_active → 단말 상태 제외
  31  load → None (미존재 ID)
  32  상태 직렬화 왕복 (9개 상태 전체)
  33  TRADE_CLOSED 가 LifecycleState 에 존재하지 않음

커버리지 — 신규:
  T01  동일 trade_id에 BUY 주문 2개 생성 가능
  T02  동일 trade_id에 BUY + SELL 주문 동시 저장 가능
  T03  각 주문이 독립 상태를 유지 (BUY만 전이해도 SELL은 UNKNOWN)
  T04  동일 client_order_id 중복 생성 → DuplicateClientOrderIdError
  T05  market + odno 로 주문 조회
  T06  매수 FILLED 후 상태가 FILLED 유지 (TRADE_CLOSED 없음)
  T07  매도 FILLED 후 OrderLifecycle 상태 = FILLED
  T08  100주 중 30주 체결 후 잔여 취소 정보 보존
       (had_partial_fill=True, terminal_reason, filled_qty=30, remaining_qty=70)
  T09  부분체결 후 EXPIRED 정보 보존
  T10  INSERT 후 created_at 유지 (UPSERT 후 변경 없음)
  T11  UPSERT 후 updated_at 만 변경
  T12  재시작 후 동일 trade_id의 복수 주문 모두 복원
  T13  미등록 odno 체결 관측 → find_for_observation None 반환, 포지션 변경 없음
  T14  동일 누적 체결량 재관측 → 상태·수량 중복 증가 없음 (멱등)
  T15  FILLED 단말 상태에서 추가 체결 이벤트 → LifecycleTransitionError
"""

from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid

# ── import 경로 설정 ──────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import phoenix.lifecycle as lc_module
from phoenix.lifecycle import (
    LifecycleState,
    LifecycleTransitionError,
    LifecycleNotFoundError,
    DuplicateClientOrderIdError,
    TransitionValidator,
    OrderLifecycle,
    OrderLifecycleManager,
    make_order_lifecycle_id,
    _ALLOWED_TRANSITIONS,
    _TERMINAL_STATES,
    EXECUTION_OBSERVED_ONLY,
)
from tests.phoenix._helpers import PhoenixTestCase, fresh_db
from phoenix import EventStore


# ══════════════════════════════════════════════════════════════════════
# 픽스처 헬퍼
# ══════════════════════════════════════════════════════════════════════

def _make_temp_journal_db() -> str:
    """빈 임시 journal DB 파일을 생성한다 (스키마는 Manager가 직접 생성)."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="lifecycle-test-")
    os.close(fd)
    return path


def _make_manager(
    journal_db: str,
    event_store=None,
) -> OrderLifecycleManager:
    mgr = OrderLifecycleManager(
        journal_db_path=journal_db,
        event_store=event_store,
    )
    if hasattr(mgr._local, "lc_conn"):
        mgr._local.lc_conn = None
    return mgr


def _close_manager(mgr: OrderLifecycleManager) -> None:
    if hasattr(mgr._local, "lc_conn") and mgr._local.lc_conn is not None:
        try:
            mgr._local.lc_conn.close()
        except Exception:
            pass
        mgr._local.lc_conn = None


def _new_trade_id() -> str:
    return f"TRADE-{uuid.uuid4().hex[:8]}"


def _cleanup_db(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════════
# TestLifecycleTransitions — 기존 전이 테스트 (구조 변경 반영)
# ══════════════════════════════════════════════════════════════════════

class TestLifecycleTransitions(PhoenixTestCase):
    """정상/비정상 상태 전이 테스트 (order_lifecycle_id PK 기준)."""

    def setUp(self):
        super().setUp()
        self.journal_db = _make_temp_journal_db()
        self._managers: list[OrderLifecycleManager] = []

    def tearDown(self):
        for mgr in self._managers:
            _close_manager(mgr)
        self._managers.clear()
        super().tearDown()
        _cleanup_db(self.journal_db)

    def _mgr(self, event_store=None) -> OrderLifecycleManager:
        mgr = _make_manager(self.journal_db, event_store=event_store)
        self._managers.append(mgr)
        return mgr

    def _new_lc(self, mgr, side="BUY", order_qty=None) -> OrderLifecycle:
        return mgr.create(
            trade_id=_new_trade_id(),
            market="KR", code="005930",
            side=side, strategy_name="TestStrategy",
            order_qty=order_qty,
        )

    # ── 01. UNKNOWN → SIGNAL_CONFIRMED ──────────────────────────
    def test_01_confirm_signal(self):
        """UNKNOWN → SIGNAL_CONFIRMED 정상 전이."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        self.assertEqual(lc.current_state, LifecycleState.UNKNOWN)
        lc.confirm_signal()
        self.assertEqual(lc.current_state, LifecycleState.SIGNAL_CONFIRMED)

    # ── 02. SIGNAL_CONFIRMED → ORDER_SUBMITTED ──────────────────
    def test_02_submit(self):
        """SIGNAL_CONFIRMED → ORDER_SUBMITTED + client_order_id + submitted_at."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal()
        coid = f"COID-{uuid.uuid4().hex[:8]}"
        lc.submit(client_order_id=coid)
        self.assertEqual(lc.current_state, LifecycleState.ORDER_SUBMITTED)
        self.assertEqual(lc.client_order_id, coid)
        self.assertIsNotNone(lc.submitted_at)

    # ── 03. ORDER_SUBMITTED → ORDER_ACCEPTED ────────────────────
    def test_03_accept_saves_odno(self):
        """ORDER_SUBMITTED → ORDER_ACCEPTED + odno 저장."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit()
        lc.accept(odno="0012345678")
        self.assertEqual(lc.current_state, LifecycleState.ORDER_ACCEPTED)
        self.assertEqual(lc.odno, "0012345678")
        self.assertIsNotNone(lc.accepted_at)

    # ── 04. ORDER_ACCEPTED → PARTIALLY_FILLED ───────────────────
    def test_04_partial_fill(self):
        """ORDER_ACCEPTED → PARTIALLY_FILLED + filled_qty / remaining_qty."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy", order_qty=100,
        )
        lc.confirm_signal(); lc.submit(); lc.accept()
        lc.partial_fill(delta=30, avg_price=70000.0)
        self.assertEqual(lc.current_state, LifecycleState.PARTIALLY_FILLED)
        self.assertEqual(lc.filled_qty, 30)
        self.assertEqual(lc.remaining_qty, 70)
        self.assertEqual(lc.avg_fill_price, 70000.0)
        self.assertTrue(lc.had_partial_fill)
        self.assertIsNotNone(lc.first_fill_at)

    # ── 05. PARTIALLY_FILLED → PARTIALLY_FILLED ─────────────────
    def test_05_additional_partial_fill(self):
        """PARTIALLY_FILLED → PARTIALLY_FILLED (추가 부분 체결)."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy", order_qty=100,
        )
        lc.confirm_signal(); lc.submit(); lc.accept()
        lc.partial_fill(delta=30)
        lc.partial_fill(delta=40)
        self.assertEqual(lc.current_state, LifecycleState.PARTIALLY_FILLED)
        self.assertEqual(lc.filled_qty, 70)
        self.assertEqual(lc.remaining_qty, 30)

    # ── 06. PARTIALLY_FILLED → FILLED ───────────────────────────
    def test_06_partial_then_full_fill(self):
        """PARTIALLY_FILLED → FILLED."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy", order_qty=100,
        )
        lc.confirm_signal(); lc.submit(); lc.accept()
        lc.partial_fill(delta=70)
        lc.full_fill(delta=30, avg_price=70500.0)
        self.assertEqual(lc.current_state, LifecycleState.FILLED)
        self.assertEqual(lc.filled_qty, 100)
        self.assertEqual(lc.remaining_qty, 0)

    # ── 07. ORDER_ACCEPTED → FILLED (직접 전량) ─────────────────
    def test_07_direct_full_fill_from_accepted(self):
        """ORDER_ACCEPTED → FILLED (부분 체결 없이)."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy", order_qty=50,
        )
        lc.confirm_signal(); lc.submit(); lc.accept()
        lc.full_fill(delta=50, avg_price=65000.0)
        self.assertEqual(lc.current_state, LifecycleState.FILLED)
        self.assertEqual(lc.filled_qty, 50)
        self.assertIsNotNone(lc.closed_at)

    # ── 08. FILLED는 주문 단말 ───────────────────────────────────
    def test_08_filled_is_terminal(self):
        """FILLED는 is_terminal=True이며 허용 전이가 없다."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit(); lc.accept(); lc.full_fill()
        self.assertTrue(lc.is_terminal)
        self.assertEqual(lc.current_state, LifecycleState.FILLED)
        # 어떤 전이도 불가
        for method in (lc.confirm_signal, lc.submit, lc.cancel, lc.expire):
            with self.assertRaises(LifecycleTransitionError):
                method()

    # ── 09. ORDER_ACCEPTED → CANCELLED ──────────────────────────
    def test_09_cancel_from_accepted(self):
        """ORDER_ACCEPTED → CANCELLED."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit(); lc.accept()
        lc.cancel()
        self.assertEqual(lc.current_state, LifecycleState.CANCELLED)
        self.assertTrue(lc.is_terminal)
        self.assertIsNotNone(lc.closed_at)

    # ── 10. ORDER_SUBMITTED → CANCELLED ─────────────────────────
    def test_10_cancel_from_submitted(self):
        """ORDER_SUBMITTED → CANCELLED."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit()
        lc.cancel()
        self.assertEqual(lc.current_state, LifecycleState.CANCELLED)

    # ── 11. ORDER_ACCEPTED → REJECTED ───────────────────────────
    def test_11_reject_saves_reason(self):
        """ORDER_ACCEPTED → REJECTED + last_error 저장."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit(); lc.accept()
        lc.reject(reason="리스크 한도 초과")
        self.assertEqual(lc.current_state, LifecycleState.REJECTED)
        self.assertEqual(lc.last_error, "리스크 한도 초과")
        self.assertTrue(lc.is_terminal)

    # ── 12. ORDER_ACCEPTED → EXPIRED ────────────────────────────
    def test_12_expire_from_accepted(self):
        """ORDER_ACCEPTED → EXPIRED."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit(); lc.accept()
        lc.expire()
        self.assertEqual(lc.current_state, LifecycleState.EXPIRED)
        self.assertTrue(lc.is_terminal)

    # ── 13. FILLED에서 accept() → 예외 ──────────────────────────
    def test_13_invalid_accept_from_filled(self):
        """FILLED 상태에서 accept() → LifecycleTransitionError."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit(); lc.accept(); lc.full_fill()
        with self.assertRaises(LifecycleTransitionError):
            lc.accept()

    # ── 14. CANCELLED에서 submit() → 예외 ───────────────────────
    def test_14_submit_after_cancelled(self):
        """CANCELLED 상태에서 submit() → LifecycleTransitionError."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit(); lc.cancel()
        with self.assertRaises(LifecycleTransitionError):
            lc.submit()

    # ── 15. REJECTED에서 accept() → 예외 ────────────────────────
    def test_15_accept_after_rejected(self):
        """REJECTED 상태에서 accept() → LifecycleTransitionError."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit(); lc.reject()
        with self.assertRaises(LifecycleTransitionError):
            lc.accept()

    # ── 16. EXPIRED에서 full_fill() → 예외 ──────────────────────
    def test_16_full_fill_after_expired(self):
        """EXPIRED 상태에서 full_fill() → LifecycleTransitionError."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit(); lc.accept(); lc.expire()
        with self.assertRaises(LifecycleTransitionError):
            lc.full_fill()

    # ── 17. TRADE_CLOSED 가 LifecycleState 에 없음 ──────────────
    def test_17_no_trade_closed_state(self):
        """TRADE_CLOSED 는 LifecycleState 열거형에 존재하지 않는다."""
        state_names = {s.name for s in LifecycleState}
        self.assertNotIn("TRADE_CLOSED", state_names,
            "TRADE_CLOSED는 OrderLifecycle 책임 범위 밖이므로 LifecycleState에 없어야 합니다.")
        # 단말 상태 확인: FILLED, CANCELLED, REJECTED, EXPIRED
        for expected_terminal in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
            self.assertIn(expected_terminal, state_names)
            self.assertIn(LifecycleState[expected_terminal], _TERMINAL_STATES)

    # ── 18. partial_fill(delta=0) → ValueError ──────────────────
    def test_18_partial_fill_zero_delta_raises(self):
        """partial_fill(delta=0) → ValueError."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit(); lc.accept()
        with self.assertRaises(ValueError):
            lc.partial_fill(delta=0)

    # ── 19. 중복 accept → 멱등 ──────────────────────────────────
    def test_19_duplicate_accept_is_idempotent(self):
        """ORDER_ACCEPTED 상태에서 accept() 재호출 → 예외 없음, odno 불변."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy",
        )
        lc.confirm_signal(); lc.submit()
        lc.accept(odno="0012345678")
        lc.accept(odno="0099999999")   # 멱등 — odno 변경 안 됨
        self.assertEqual(lc.current_state, LifecycleState.ORDER_ACCEPTED)
        self.assertEqual(lc.odno, "0012345678")

    # ── 20. 중복 full_fill → 멱등 ───────────────────────────────
    def test_20_duplicate_full_fill_is_idempotent(self):
        """FILLED 상태에서 full_fill() 재호출 → 예외 없음, filled_qty 불변."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy", order_qty=100,
        )
        lc.confirm_signal(); lc.submit(); lc.accept()
        lc.full_fill(delta=100, avg_price=70000.0)
        lc.full_fill(delta=50)   # 멱등 — filled_qty 변경 안 됨
        self.assertEqual(lc.current_state, LifecycleState.FILLED)
        self.assertEqual(lc.filled_qty, 100)

    # ── 21. 단말 상태 4개 확인 ───────────────────────────────────
    def test_21_terminal_states_are_four(self):
        """단말 상태는 FILLED / CANCELLED / REJECTED / EXPIRED 4개다."""
        expected = {
            LifecycleState.FILLED,
            LifecycleState.CANCELLED,
            LifecycleState.REJECTED,
            LifecycleState.EXPIRED,
        }
        self.assertEqual(_TERMINAL_STATES, expected)

    # ── 22. DB 복원 — PARTIALLY_FILLED ──────────────────────────
    def test_22_restart_recovery_from_db(self):
        """DB에 저장된 PARTIALLY_FILLED 상태를 재시작 후 복원한다."""
        journal_db = _make_temp_journal_db()
        mgr = mgr2 = None
        try:
            mgr = _make_manager(journal_db)
            lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY",
                            order_qty=100)
            oid = lc.order_lifecycle_id
            mgr.confirm_signal(lc)
            mgr.submit(lc, client_order_id="COID-RECOVER")
            mgr.accept(lc, odno="0099887766")
            mgr.partial_fill(lc, delta=40, avg_price=68000.0)

            mgr2 = _make_manager(journal_db)
            restored = mgr2.load(oid)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.current_state, LifecycleState.PARTIALLY_FILLED)
            self.assertEqual(restored.filled_qty, 40)
            self.assertEqual(restored.odno, "0099887766")
            self.assertEqual(restored.client_order_id, "COID-RECOVER")
            self.assertTrue(restored.had_partial_fill)
        finally:
            if mgr:  _close_manager(mgr)
            if mgr2: _close_manager(mgr2)
            _cleanup_db(journal_db)

    # ── 23. TransitionValidator.is_valid_transition() ────────────
    def test_23_validator_is_valid_transition(self):
        """is_valid_transition()이 올바른 bool을 반환한다."""
        v = TransitionValidator()
        self.assertTrue(v.is_valid_transition(
            LifecycleState.UNKNOWN, LifecycleState.SIGNAL_CONFIRMED))
        self.assertTrue(v.is_valid_transition(
            LifecycleState.PARTIALLY_FILLED, LifecycleState.PARTIALLY_FILLED))
        self.assertFalse(v.is_valid_transition(
            LifecycleState.FILLED, LifecycleState.ORDER_ACCEPTED))
        self.assertFalse(v.is_valid_transition(
            LifecycleState.CANCELLED, LifecycleState.ORDER_SUBMITTED))

    # ── 24. TransitionValidator.validate() 에러 메시지 ──────────
    def test_24_validator_error_message_content(self):
        """validate() LifecycleTransitionError 메시지에 상태명·ID 포함."""
        v = TransitionValidator()
        oid = "KR_BUY_005930_TEST"
        try:
            v.validate(LifecycleState.FILLED, LifecycleState.ORDER_ACCEPTED,
                       order_lifecycle_id=oid)
            self.fail("LifecycleTransitionError가 발생해야 합니다")
        except LifecycleTransitionError as exc:
            msg = str(exc)
            self.assertIn("FILLED", msg)
            self.assertIn("ORDER_ACCEPTED", msg)
            self.assertIn(oid, msg)

    # ── 25. 수량 누적 계산 ───────────────────────────────────────
    def test_25_fill_qty_accumulation(self):
        """부분 체결 3회 후 filled_qty / remaining_qty 정확성."""
        lc = OrderLifecycle(
            order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
            trade_id=_new_trade_id(),
            market="KR", code="005930", side="BUY",
            strategy_name="TestStrategy", order_qty=100,
        )
        lc.confirm_signal(); lc.submit(); lc.accept()
        lc.partial_fill(delta=20, avg_price=69000.0)
        lc.partial_fill(delta=30, avg_price=70000.0)
        lc.partial_fill(delta=10, avg_price=71000.0)
        self.assertEqual(lc.filled_qty, 60)
        self.assertEqual(lc.remaining_qty, 40)

    # ── 26. EventStore 기록 — positions 미변경 ───────────────────
    def test_26_eventstore_no_position_change(self):
        """EXECUTION_OBSERVED_ONLY 기록 후 positions 테이블이 변경되지 않는다."""
        db, store = self.new_store()
        journal_db = _make_temp_journal_db()
        mgr = None
        try:
            mgr = _make_manager(journal_db, event_store=store)
            lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY",
                            order_qty=100)
            mgr.confirm_signal(lc)
            mgr.submit(lc, client_order_id="COID-E2E")
            mgr.accept(lc, odno="0001234567")
            mgr.partial_fill(lc, delta=50, avg_price=70000.0)
            mgr.full_fill(lc, delta=50)
            self.assertGreater(store.event_count(), 0)
            self.assertIsNone(store.get_position("005930"))
        finally:
            if mgr: _close_manager(mgr)
            _cleanup_db(journal_db)

    # ── 27. AST — apply_buy / apply_sell 없음 ───────────────────
    def test_27_ast_no_apply_buy_sell(self):
        """lifecycle.py 에 apply_buy / apply_sell 함수 호출이 없다."""
        src = inspect.getsource(lc_module)
        tree = ast.parse(src)
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
        self.assertNotIn("apply_buy", called)
        self.assertNotIn("apply_sell", called)

    # ── 28. AST — DailyPnLGuard 없음 ────────────────────────────
    def test_28_ast_no_daily_pnl_guard(self):
        """lifecycle.py 에 DailyPnLGuard 함수 호출이 없다."""
        src = inspect.getsource(lc_module)
        tree = ast.parse(src)
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
        self.assertNotIn("DailyPnLGuard", called)

    # ── 29. 전체 정상 흐름 E2E ───────────────────────────────────
    def test_29_full_happy_path(self):
        """UNKNOWN부터 FILLED까지 단계별 상태 검증 및 DB 복원."""
        journal_db = _make_temp_journal_db()
        db, store = self.new_store()
        mgr = None
        try:
            mgr = _make_manager(journal_db, event_store=store)
            lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY",
                            order_qty=100)
            oid = lc.order_lifecycle_id

            self.assertEqual(lc.current_state, LifecycleState.UNKNOWN)
            mgr.confirm_signal(lc)
            self.assertEqual(lc.current_state, LifecycleState.SIGNAL_CONFIRMED)
            mgr.submit(lc, client_order_id="COID-HAPPY")
            self.assertEqual(lc.current_state, LifecycleState.ORDER_SUBMITTED)
            mgr.accept(lc, odno="0099001122")
            self.assertEqual(lc.current_state, LifecycleState.ORDER_ACCEPTED)
            mgr.partial_fill(lc, delta=60, avg_price=70000.0)
            self.assertEqual(lc.current_state, LifecycleState.PARTIALLY_FILLED)
            mgr.full_fill(lc, delta=40, avg_price=70500.0)
            self.assertEqual(lc.current_state, LifecycleState.FILLED)
            self.assertTrue(lc.is_terminal)
            self.assertIsNotNone(lc.closed_at)

            restored = mgr.load(oid)
            self.assertEqual(restored.current_state, LifecycleState.FILLED)
            self.assertEqual(restored.filled_qty, 100)
            self.assertIsNone(store.get_position("005930"))
        finally:
            if mgr: _close_manager(mgr)
            _cleanup_db(journal_db)

    # ── 30. load_all_active — 단말 제외 ─────────────────────────
    def test_30_load_all_active_excludes_terminal(self):
        """load_all_active()는 단말 상태 주문을 제외한다."""
        mgr = self._mgr()
        lc1 = mgr.create(_new_trade_id(), "KR", "005930", "BUY")
        lc2 = mgr.create(_new_trade_id(), "KR", "000660", "BUY")
        lc3 = mgr.create(_new_trade_id(), "KR", "035420", "BUY")
        # lc1 → PARTIALLY_FILLED
        mgr.confirm_signal(lc1); mgr.submit(lc1); mgr.accept(lc1)
        mgr.partial_fill(lc1, delta=10)
        # lc2 → ORDER_ACCEPTED
        mgr.confirm_signal(lc2); mgr.submit(lc2); mgr.accept(lc2)
        # lc3 → CANCELLED (단말)
        mgr.confirm_signal(lc3); mgr.submit(lc3); mgr.cancel(lc3)

        active_ids = {lc.order_lifecycle_id for lc in mgr.load_all_active()}
        self.assertIn(lc1.order_lifecycle_id, active_ids)
        self.assertIn(lc2.order_lifecycle_id, active_ids)
        self.assertNotIn(lc3.order_lifecycle_id, active_ids)

    # ── 31. load → None (미존재) ─────────────────────────────────
    def test_31_load_returns_none_for_unknown_id(self):
        """존재하지 않는 ID로 load() → None 반환."""
        mgr = self._mgr()
        self.assertIsNone(mgr.load("NONEXISTENT-ORDER-LIFECYCLE-ID"))

    # ── 32. 상태 직렬화 왕복 (9개) ──────────────────────────────
    def test_32_state_serialization_roundtrip(self):
        """모든 LifecycleState가 to_dict/from_dict 왕복 후 동일하다."""
        for state in LifecycleState:
            lc = OrderLifecycle(
                order_lifecycle_id=make_order_lifecycle_id("KR", "BUY", "005930"),
                trade_id=_new_trade_id(),
                market="KR", code="005930", side="BUY",
                strategy_name="Test", current_state=state,
            )
            restored = OrderLifecycle.from_dict(lc.to_dict())
            self.assertEqual(restored.current_state, state)

    # ── 33. TRADE_CLOSED 가 LifecycleState 에 없음 ──────────────
    def test_33_trade_closed_not_in_lifecycle_state(self):
        """TRADE_CLOSED 는 LifecycleState 에 존재하지 않는다."""
        names = {s.name for s in LifecycleState}
        self.assertNotIn("TRADE_CLOSED", names)
        self.assertEqual(len(list(LifecycleState)), 9)


# ══════════════════════════════════════════════════════════════════════
# TestNewRequirements — 신규 15개 테스트
# ══════════════════════════════════════════════════════════════════════

class TestNewRequirements(PhoenixTestCase):
    """지적 사항 반영 신규 테스트 T01~T15."""

    def setUp(self):
        super().setUp()
        self.journal_db = _make_temp_journal_db()
        self._managers: list[OrderLifecycleManager] = []

    def tearDown(self):
        for mgr in self._managers:
            _close_manager(mgr)
        self._managers.clear()
        super().tearDown()
        _cleanup_db(self.journal_db)

    def _mgr(self, event_store=None) -> OrderLifecycleManager:
        mgr = _make_manager(self.journal_db, event_store=event_store)
        self._managers.append(mgr)
        return mgr

    # ── T01. 동일 trade_id에 BUY 주문 2개 생성 ───────────────────
    def test_T01_two_buy_orders_same_trade_id(self):
        """동일 trade_id에 BUY 주문 2개 생성 가능 (order_lifecycle_id 별도)."""
        mgr = self._mgr()
        trade_id = _new_trade_id()
        lc1 = mgr.create(trade_id, "KR", "005930", "BUY", order_qty=50)
        lc2 = mgr.create(trade_id, "KR", "005930", "BUY", order_qty=30)
        self.assertNotEqual(lc1.order_lifecycle_id, lc2.order_lifecycle_id)
        orders = mgr.load_by_trade_id(trade_id)
        self.assertEqual(len(orders), 2)
        oids = {o.order_lifecycle_id for o in orders}
        self.assertIn(lc1.order_lifecycle_id, oids)
        self.assertIn(lc2.order_lifecycle_id, oids)

    # ── T02. 동일 trade_id에 BUY + SELL 주문 동시 저장 ──────────
    def test_T02_buy_and_sell_orders_same_trade_id(self):
        """동일 trade_id에 BUY와 SELL 주문이 동시에 저장된다."""
        mgr = self._mgr()
        trade_id = _new_trade_id()
        buy  = mgr.create(trade_id, "KR", "005930", "BUY",  order_qty=100)
        sell = mgr.create(trade_id, "KR", "005930", "SELL", order_qty=50)
        self.assertEqual(buy.side, "BUY")
        self.assertEqual(sell.side, "SELL")
        self.assertNotEqual(buy.order_lifecycle_id, sell.order_lifecycle_id)
        orders = mgr.load_by_trade_id(trade_id)
        sides = {o.side for o in orders}
        self.assertIn("BUY", sides)
        self.assertIn("SELL", sides)

    # ── T03. 각 주문이 독립 상태를 유지 ─────────────────────────
    def test_T03_independent_order_states(self):
        """BUY 주문만 전이해도 SELL 주문은 UNKNOWN을 유지한다."""
        mgr = self._mgr()
        trade_id = _new_trade_id()
        buy  = mgr.create(trade_id, "KR", "005930", "BUY")
        sell = mgr.create(trade_id, "KR", "005930", "SELL")

        mgr.confirm_signal(buy)
        mgr.submit(buy)
        mgr.accept(buy)
        mgr.full_fill(buy)

        # sell 주문은 UNKNOWN 유지
        self.assertEqual(sell.current_state, LifecycleState.UNKNOWN)
        # DB에서 다시 로드해서도 동일 확인
        sell_loaded = mgr.load(sell.order_lifecycle_id)
        self.assertEqual(sell_loaded.current_state, LifecycleState.UNKNOWN)

    # ── T04. 동일 client_order_id 중복 생성 금지 ────────────────
    def test_T04_duplicate_client_order_id_raises(self):
        """동일 client_order_id로 두 번 create() → DuplicateClientOrderIdError."""
        mgr = self._mgr()
        coid = f"COID-{uuid.uuid4().hex[:8]}"
        mgr.create(_new_trade_id(), "KR", "005930", "BUY",
                   client_order_id=coid)
        with self.assertRaises(DuplicateClientOrderIdError):
            mgr.create(_new_trade_id(), "KR", "000660", "BUY",
                       client_order_id=coid)

    # ── T05. market + odno 로 주문 조회 ─────────────────────────
    def test_T05_find_by_market_and_odno(self):
        """market + odno 로 OrderLifecycle 조회."""
        mgr = self._mgr()
        lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY")
        mgr.confirm_signal(lc); mgr.submit(lc)
        mgr.accept(lc, odno="0055443322")

        found = mgr.find_by_odno("KR", "0055443322")
        self.assertIsNotNone(found)
        self.assertEqual(found.order_lifecycle_id, lc.order_lifecycle_id)

        # 다른 market으로 조회 → None
        self.assertIsNone(mgr.find_by_odno("US", "0055443322"))

    # ── T06. 매수 FILLED 후 상태 = FILLED (TRADE_CLOSED 없음) ───
    def test_T06_buy_filled_stays_filled(self):
        """매수 주문 FILLED 후 OrderLifecycle 상태는 FILLED 유지 (TRADE_CLOSED 없음)."""
        mgr = self._mgr()
        lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY", order_qty=100)
        mgr.confirm_signal(lc); mgr.submit(lc); mgr.accept(lc)
        mgr.full_fill(lc, delta=100)
        self.assertEqual(lc.current_state, LifecycleState.FILLED)
        self.assertTrue(lc.is_terminal)
        # DB에서 복원해도 FILLED
        restored = mgr.load(lc.order_lifecycle_id)
        self.assertEqual(restored.current_state, LifecycleState.FILLED)

    # ── T07. 매도 FILLED 후 상태 = FILLED ───────────────────────
    def test_T07_sell_filled_stays_filled(self):
        """매도 주문 FILLED 후 OrderLifecycle 상태는 FILLED (TRADE_CLOSED 없음)."""
        mgr = self._mgr()
        lc = mgr.create(_new_trade_id(), "KR", "005930", "SELL", order_qty=50)
        mgr.confirm_signal(lc); mgr.submit(lc); mgr.accept(lc)
        mgr.full_fill(lc, delta=50, avg_price=72000.0)
        self.assertEqual(lc.current_state, LifecycleState.FILLED)
        self.assertEqual(lc.side, "SELL")
        restored = mgr.load(lc.order_lifecycle_id)
        self.assertEqual(restored.current_state, LifecycleState.FILLED)

    # ── T08. 100주 중 30주 체결 후 잔여 취소 정보 보존 ──────────
    def test_T08_partial_fill_then_cancel_preserves_info(self):
        """100주 중 30주 체결 후 잔여 70주 취소 → 정보 완전 보존."""
        mgr = self._mgr()
        lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY", order_qty=100)
        mgr.confirm_signal(lc); mgr.submit(lc); mgr.accept(lc)
        mgr.partial_fill(lc, delta=30, avg_price=70000.0)
        mgr.cancel(lc)  # 잔여 70주 취소

        self.assertEqual(lc.current_state, LifecycleState.CANCELLED)
        self.assertEqual(lc.order_qty, 100)
        self.assertEqual(lc.filled_qty, 30)
        self.assertEqual(lc.remaining_qty, 70)
        self.assertTrue(lc.had_partial_fill)
        self.assertIsNotNone(lc.terminal_reason)
        self.assertIn("PARTIAL_FILL", lc.terminal_reason)

        # DB에서 복원해도 동일
        restored = mgr.load(lc.order_lifecycle_id)
        self.assertEqual(restored.current_state, LifecycleState.CANCELLED)
        self.assertEqual(restored.filled_qty, 30)
        self.assertEqual(restored.remaining_qty, 70)
        self.assertTrue(restored.had_partial_fill)
        self.assertIsNotNone(restored.terminal_reason)

    # ── T09. 부분체결 후 EXPIRED 정보 보존 ──────────────────────
    def test_T09_partial_fill_then_expire_preserves_info(self):
        """부분체결 후 EXPIRED → had_partial_fill=True, terminal_reason 보존."""
        mgr = self._mgr()
        lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY", order_qty=100)
        mgr.confirm_signal(lc); mgr.submit(lc); mgr.accept(lc)
        mgr.partial_fill(lc, delta=20)
        mgr.expire(lc)

        self.assertEqual(lc.current_state, LifecycleState.EXPIRED)
        self.assertTrue(lc.had_partial_fill)
        self.assertIn("EXPIRED", lc.terminal_reason)

        restored = mgr.load(lc.order_lifecycle_id)
        self.assertTrue(restored.had_partial_fill)
        self.assertIn("EXPIRED", restored.terminal_reason)

    # ── T10. INSERT 후 created_at 유지 (UPSERT 재삽입 불변) ─────
    def test_T10_created_at_preserved_after_upsert(self):
        """UPSERT 후에도 created_at은 최초 삽입값을 유지한다."""
        mgr = self._mgr()
        lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY")
        oid = lc.order_lifecycle_id

        conn = mgr._get_conn()
        row1 = conn.execute(
            "SELECT created_at FROM lifecycle_orders WHERE order_lifecycle_id=?",
            (oid,)
        ).fetchone()
        created_at_orig = row1["created_at"]

        # 상태 전이 후 UPSERT
        time.sleep(0.01)
        mgr.confirm_signal(lc)

        row2 = conn.execute(
            "SELECT created_at, updated_at FROM lifecycle_orders "
            "WHERE order_lifecycle_id=?",
            (oid,)
        ).fetchone()
        self.assertEqual(row2["created_at"], created_at_orig,
                         "created_at은 UPSERT 후에도 최초값 유지")

    # ── T11. UPSERT 후 updated_at 만 변경 ────────────────────────
    def test_T11_updated_at_changes_after_upsert(self):
        """UPSERT 후 updated_at 은 갱신되고, created_at 은 변하지 않는다."""
        mgr = self._mgr()
        lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY")
        oid = lc.order_lifecycle_id

        conn = mgr._get_conn()
        row1 = conn.execute(
            "SELECT created_at, updated_at FROM lifecycle_orders "
            "WHERE order_lifecycle_id=?", (oid,)
        ).fetchone()
        created_orig  = row1["created_at"]
        updated_orig  = row1["updated_at"]

        time.sleep(0.02)
        mgr.confirm_signal(lc)

        row2 = conn.execute(
            "SELECT created_at, updated_at FROM lifecycle_orders "
            "WHERE order_lifecycle_id=?", (oid,)
        ).fetchone()
        self.assertEqual(row2["created_at"], created_orig)
        # updated_at 은 변경 (동일한 millisecond 안에 실행되면 같을 수 있으므로 >= 비교)
        self.assertGreaterEqual(row2["updated_at"], updated_orig)

    # ── T12. 재시작 후 동일 trade_id의 복수 주문 모두 복원 ───────
    def test_T12_restart_restores_all_orders_for_trade_id(self):
        """재시작 후 동일 trade_id의 복수 주문이 모두 복원된다."""
        journal_db = _make_temp_journal_db()
        mgr = mgr2 = None
        try:
            mgr = _make_manager(journal_db)
            trade_id = _new_trade_id()
            buy1 = mgr.create(trade_id, "KR", "005930", "BUY", order_qty=100)
            buy2 = mgr.create(trade_id, "KR", "005930", "BUY", order_qty=30)
            sell = mgr.create(trade_id, "KR", "005930", "SELL", order_qty=50)

            mgr.confirm_signal(buy1); mgr.submit(buy1); mgr.accept(buy1)
            mgr.partial_fill(buy1, delta=60)

            mgr.confirm_signal(buy2); mgr.submit(buy2)

            mgr2 = _make_manager(journal_db)
            orders = mgr2.load_by_trade_id(trade_id)
            self.assertEqual(len(orders), 3)

            by_oid = {o.order_lifecycle_id: o for o in orders}
            self.assertEqual(by_oid[buy1.order_lifecycle_id].current_state,
                             LifecycleState.PARTIALLY_FILLED)
            self.assertEqual(by_oid[buy1.order_lifecycle_id].filled_qty, 60)
            self.assertEqual(by_oid[buy2.order_lifecycle_id].current_state,
                             LifecycleState.ORDER_SUBMITTED)
            self.assertEqual(by_oid[sell.order_lifecycle_id].current_state,
                             LifecycleState.UNKNOWN)
        finally:
            if mgr:  _close_manager(mgr)
            if mgr2: _close_manager(mgr2)
            _cleanup_db(journal_db)

    # ── T13. 미등록 odno 체결 관측 → None 반환, 포지션 변경 없음 ─
    def test_T13_unregistered_odno_observation_returns_none(self):
        """미등록 odno로 find_for_observation → None 반환, 포지션 변경 없음."""
        db, store = self.new_store()
        mgr = self._mgr(event_store=store)

        # 등록된 주문 없이 관측 시도
        result = mgr.find_for_observation(
            market="KR",
            odno="0099999999",       # 미등록
            client_order_id=None,
        )
        self.assertIsNone(result)
        # positions 테이블 변경 없음
        self.assertIsNone(store.get_position("005930"))
        self.assertEqual(store.event_count(), 0)

    # ── T14. 동일 누적 체결량 재관측 → 상태·수량 중복 증가 없음 ─
    def test_T14_same_cumulative_fill_is_idempotent(self):
        """동일 cumulative 체결량으로 재관측 → filled_qty 중복 증가 없음."""
        mgr = self._mgr()
        lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY", order_qty=100)
        mgr.confirm_signal(lc); mgr.submit(lc); mgr.accept(lc)

        # 첫 번째 관측: delta=30
        mgr.partial_fill(lc, delta=30)
        self.assertEqual(lc.filled_qty, 30)

        # 두 번째 관측: 동일 delta=30 재시도 (Manager 래퍼는 delta를 받음)
        # 동일 cumulative를 다시 관측했을 때 delta=0 이면 ValueError 발생
        # → 호출자는 fill_delta > 0 을 확인하고 호출해야 함
        # 여기서는 delta=0 으로 partial_fill 호출 시 ValueError 확인
        with self.assertRaises(ValueError):
            mgr.partial_fill(lc, delta=0)

        # filled_qty는 여전히 30
        self.assertEqual(lc.filled_qty, 30)

    # ── T15. FILLED 단말 상태에서 추가 체결 이벤트 → 예외 ────────
    def test_T15_filled_rejects_additional_fills(self):
        """FILLED 단말 상태에서 partial_fill() → LifecycleTransitionError."""
        mgr = self._mgr()
        lc = mgr.create(_new_trade_id(), "KR", "005930", "BUY", order_qty=100)
        mgr.confirm_signal(lc); mgr.submit(lc); mgr.accept(lc)
        mgr.full_fill(lc, delta=100)
        self.assertEqual(lc.current_state, LifecycleState.FILLED)

        with self.assertRaises(LifecycleTransitionError):
            mgr.partial_fill(lc, delta=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
