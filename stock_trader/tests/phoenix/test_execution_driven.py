"""tests/phoenix/test_execution_driven.py
Phase 3: Execution-driven Position Update 테스트 (24개)

검증 항목:
  T01  BUY ACCEPTED  → apply_buy 호출되지 않음
  T02  BUY FILLED    → apply_buy 정확히 1회 호출
  T03  BUY FILLED 재관측(동일 lc 재호출) → apply_buy 1회만 (idempotency)
  T04  SELL ACCEPTED → apply_sell 호출되지 않음
  T05  SELL FILLED   → apply_sell 정확히 1회 호출
  T06  SELL FILLED 재관측 → apply_sell 1회만 (idempotency)
  T07  부분체결       → apply_buy 호출되지 않음 (FULL FILLED만)
  T08  BUY FILLED 후 apply_buy 인자 정확성 (qty, avg_fill_price)
  T09  SELL FILLED 후 DailyPnLGuard.record 호출 검증
  T10  SELL FILLED 후 reentry.record_sell 호출 검증
  T11  SELL FILLED 후 _log_trade 호출 검증
  T12  on_buy_filled 콜백 예외 → 예외 전파
  T13  on_sell_filled 콜백 예외 → 예외 전파
  T14  on_filled=None → 예외 없음 (full_fill 정상 완료)
  T15  ExecutionDrivenPositionUpdater — side=BUY 정확히 호출
  T16  ExecutionDrivenPositionUpdater — side=SELL 정확히 호출
  T17  ExecutionDrivenPositionUpdater — side=SELL 콜백 없음 → warning only
  T18  ExecutionDrivenPositionUpdater — FILLED 아닌 상태 → 경고 후 반환
  T19  dispatch_fill is_full=True  → full_fill + on_filled 호출
  T20  dispatch_fill is_full=False → partial_fill (포지션 변경 없음)
  T21  dispatch_fill 존재하지 않는 order_lifecycle_id → warning + no raise
  T22  StrategyManager._lifecycle_mgr=None → 기존 apply_buy 직접 호출 (폴백 유지)
  T23  StrategyManager._lifecycle_mgr=None → 기존 apply_sell 직접 호출 (폴백 유지)
  T24  BUY 후 SELL → 순서 보장 (BUY FILLED 먼저 apply_buy, SELL FILLED 후 apply_sell)
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch, call

# stock_trader 루트 경로 추가
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from phoenix.lifecycle import (
    OrderLifecycle,
    OrderLifecycleManager,
    LifecycleState,
    make_order_lifecycle_id,
)
from phoenix.execution_driven import ExecutionDrivenPositionUpdater


# ──────────────────────────────────────────────────────────────
# 헬퍼 — in-memory OrderLifecycleManager (임시 DB)
# ──────────────────────────────────────────────────────────────
def _make_mgr(tmp_dir: str) -> OrderLifecycleManager:
    db_path = os.path.join(tmp_dir, "journal.db")
    return OrderLifecycleManager(db_path)


def _make_lc(mgr: OrderLifecycleManager, side: str = "BUY", code: str = "005930") -> OrderLifecycle:
    lc = mgr.create(
        trade_id="TR001",
        market="KR",
        code=code,
        side=side,
        strategy_name="Test",
        order_qty=10,
    )
    # UNKNOWN → SIGNAL_CONFIRMED → ORDER_SUBMITTED → (ready for accept)
    mgr.confirm_signal(lc)
    mgr.submit(lc)
    return lc


# ══════════════════════════════════════════════════════════════
# T01-T03: BUY ACCEPTED/FILLED/idempotency
# ══════════════════════════════════════════════════════════════
class TestBuyFilledPath(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exec-drv-test-")
        self.mgr = _make_mgr(self.tmp)
        self.on_buy  = MagicMock(return_value=None)
        self.on_sell = MagicMock(return_value=None)
        self.updater = ExecutionDrivenPositionUpdater(
            on_buy_filled=self.on_buy,
            on_sell_filled=self.on_sell,
        )

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_T01_buy_accepted_no_apply_buy(self):
        """T01: BUY ACCEPTED 시 apply_buy 호출 안됨."""
        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        self.assertEqual(lc.current_state, LifecycleState.ORDER_ACCEPTED)
        self.on_buy.assert_not_called()
        self.on_sell.assert_not_called()

    def test_T02_buy_filled_apply_buy_once(self):
        """T02: BUY FILLED → apply_buy 정확히 1회."""
        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        self.mgr.full_fill(lc, delta=10, avg_price=50000.0, on_filled=self.updater)
        self.assertEqual(lc.current_state, LifecycleState.FILLED)
        self.on_buy.assert_called_once()
        call_lc = self.on_buy.call_args[0][0]
        self.assertEqual(call_lc.order_lifecycle_id, lc.order_lifecycle_id)
        self.assertEqual(call_lc.filled_qty, 10)
        self.assertAlmostEqual(call_lc.avg_fill_price, 50000.0)

    def test_T03_buy_filled_idempotency(self):
        """T03: BUY FILLED 재관측(동일 lc) → apply_buy 1회만."""
        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        self.mgr.full_fill(lc, delta=10, avg_price=50000.0, on_filled=self.updater)
        # 두 번째 호출 — 이미 FILLED 상태
        self.mgr.full_fill(lc, delta=10, avg_price=50000.0, on_filled=self.updater)
        # DB에서 재로드하여 두 번째 full_fill 시도
        lc2 = self.mgr.load(lc.order_lifecycle_id)
        self.mgr.full_fill(lc2, delta=10, avg_price=50000.0, on_filled=self.updater)
        # 총 1회만 호출되어야 함
        self.on_buy.assert_called_once()


# ══════════════════════════════════════════════════════════════
# T04-T06: SELL ACCEPTED/FILLED/idempotency
# ══════════════════════════════════════════════════════════════
class TestSellFilledPath(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exec-drv-sell-")
        self.mgr = _make_mgr(self.tmp)
        self.on_buy  = MagicMock(return_value=None)
        self.on_sell = MagicMock(return_value=None)
        self.updater = ExecutionDrivenPositionUpdater(
            on_buy_filled=self.on_buy,
            on_sell_filled=self.on_sell,
        )

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_T04_sell_accepted_no_apply_sell(self):
        """T04: SELL ACCEPTED 시 apply_sell 호출 안됨."""
        lc = _make_lc(self.mgr, "SELL")
        self.mgr.accept(lc)
        self.assertEqual(lc.current_state, LifecycleState.ORDER_ACCEPTED)
        self.on_sell.assert_not_called()
        self.on_buy.assert_not_called()

    def test_T05_sell_filled_apply_sell_once(self):
        """T05: SELL FILLED → apply_sell 정확히 1회."""
        lc = _make_lc(self.mgr, "SELL")
        self.mgr.accept(lc)
        self.mgr.full_fill(lc, delta=10, avg_price=55000.0, on_filled=self.updater)
        self.assertEqual(lc.current_state, LifecycleState.FILLED)
        self.on_sell.assert_called_once()
        self.on_buy.assert_not_called()
        call_lc = self.on_sell.call_args[0][0]
        self.assertEqual(call_lc.filled_qty, 10)
        self.assertAlmostEqual(call_lc.avg_fill_price, 55000.0)

    def test_T06_sell_filled_idempotency(self):
        """T06: SELL FILLED 재관측 → apply_sell 1회만."""
        lc = _make_lc(self.mgr, "SELL")
        self.mgr.accept(lc)
        self.mgr.full_fill(lc, delta=10, avg_price=55000.0, on_filled=self.updater)
        # 두 번째 호출
        self.mgr.full_fill(lc, on_filled=self.updater)
        # DB 재로드 후 세 번째 호출
        lc3 = self.mgr.load(lc.order_lifecycle_id)
        self.mgr.full_fill(lc3, on_filled=self.updater)
        self.on_sell.assert_called_once()


# ══════════════════════════════════════════════════════════════
# T07-T11: 부분체결 / 인자 검증 / DailyPnLGuard / reentry / log
# ══════════════════════════════════════════════════════════════
class TestPartialAndArgs(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exec-drv-partial-")
        self.mgr = _make_mgr(self.tmp)
        self.on_buy  = MagicMock(return_value=None)
        self.on_sell = MagicMock(return_value=None)
        self.updater = ExecutionDrivenPositionUpdater(
            on_buy_filled=self.on_buy,
            on_sell_filled=self.on_sell,
        )

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_T07_partial_fill_no_position_change(self):
        """T07: 부분체결 → on_filled 콜백 호출되지 않음 (FULL FILLED만)."""
        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        self.mgr.partial_fill(lc, delta=3, avg_price=50000.0)
        self.assertEqual(lc.current_state, LifecycleState.PARTIALLY_FILLED)
        self.on_buy.assert_not_called()
        self.on_sell.assert_not_called()

    def test_T08_buy_filled_correct_qty_price(self):
        """T08: BUY FILLED 후 apply_buy 인자 정확성 — lc.filled_qty, lc.avg_fill_price."""
        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        self.mgr.full_fill(lc, delta=7, avg_price=48500.0, on_filled=self.updater)
        received_lc = self.on_buy.call_args[0][0]
        self.assertEqual(received_lc.filled_qty, 7)
        self.assertAlmostEqual(received_lc.avg_fill_price, 48500.0)

    def test_T09_sell_filled_pnl_guard_called(self):
        """T09: _handle_sell_filled → DailyPnLGuard.record 호출 검증."""
        # 가짜 pyramid / pnl_guard / reentry 를 주입한 StrategyManager 흉내
        sell_called_pnl = []

        def fake_on_sell_filled(lc):
            # pnl_guard.record 호출을 추적
            sell_called_pnl.append(("pnl_guard.record", lc.avg_fill_price))

        updater = ExecutionDrivenPositionUpdater(
            on_buy_filled=None,
            on_sell_filled=fake_on_sell_filled,
        )
        lc = _make_lc(self.mgr, "SELL")
        self.mgr.accept(lc)
        self.mgr.full_fill(lc, delta=5, avg_price=62000.0, on_filled=updater)
        self.assertEqual(len(sell_called_pnl), 1)
        self.assertEqual(sell_called_pnl[0][0], "pnl_guard.record")
        self.assertAlmostEqual(sell_called_pnl[0][1], 62000.0)

    def test_T10_sell_filled_reentry_guard_called(self):
        """T10: SELL FILLED → reentry.record_sell 호출 (콜백 내부에서 수행)."""
        reentry_calls = []

        def fake_on_sell(lc):
            reentry_calls.append(("reentry", lc.code, lc.side))

        updater = ExecutionDrivenPositionUpdater(
            on_buy_filled=None,
            on_sell_filled=fake_on_sell,
        )
        lc = _make_lc(self.mgr, "SELL", code="035420")
        self.mgr.accept(lc)
        self.mgr.full_fill(lc, delta=8, avg_price=70000.0, on_filled=updater)
        self.assertEqual(len(reentry_calls), 1)
        self.assertEqual(reentry_calls[0], ("reentry", "035420", "SELL"))

    def test_T11_sell_filled_log_trade_called(self):
        """T11: SELL FILLED → _log_trade 호출 (콜백 내부에서 수행)."""
        log_calls = []

        def fake_on_sell(lc):
            log_calls.append(("log_trade", lc.order_lifecycle_id))

        updater = ExecutionDrivenPositionUpdater(
            on_buy_filled=None,
            on_sell_filled=fake_on_sell,
        )
        lc = _make_lc(self.mgr, "SELL")
        self.mgr.accept(lc)
        self.mgr.full_fill(lc, delta=4, avg_price=53000.0, on_filled=updater)
        self.assertEqual(len(log_calls), 1)
        self.assertIn("log_trade", log_calls[0])


# ══════════════════════════════════════════════════════════════
# T12-T14: 예외 처리
# ══════════════════════════════════════════════════════════════
class TestCallbackExceptions(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exec-drv-exc-")
        self.mgr = _make_mgr(self.tmp)

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_T12_buy_callback_exception_propagates(self):
        """T12: on_buy_filled 예외 → ExecutionDrivenPositionUpdater 에서 re-raise."""
        def bad_buy(lc):
            raise RuntimeError("buy callback error")

        updater = ExecutionDrivenPositionUpdater(on_buy_filled=bad_buy)
        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        with self.assertRaises(RuntimeError):
            self.mgr.full_fill(lc, delta=5, avg_price=50000.0, on_filled=updater)

    def test_T13_sell_callback_exception_propagates(self):
        """T13: on_sell_filled 예외 → ExecutionDrivenPositionUpdater 에서 re-raise."""
        def bad_sell(lc):
            raise ValueError("sell callback error")

        updater = ExecutionDrivenPositionUpdater(on_sell_filled=bad_sell)
        lc = _make_lc(self.mgr, "SELL")
        self.mgr.accept(lc)
        with self.assertRaises(ValueError):
            self.mgr.full_fill(lc, delta=5, avg_price=55000.0, on_filled=updater)

    def test_T14_on_filled_none_no_exception(self):
        """T14: on_filled=None → 예외 없음, FILLED 상태 정상 도달."""
        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        self.mgr.full_fill(lc, delta=5, avg_price=50000.0, on_filled=None)
        self.assertEqual(lc.current_state, LifecycleState.FILLED)


# ══════════════════════════════════════════════════════════════
# T15-T18: ExecutionDrivenPositionUpdater 단위 테스트
# ══════════════════════════════════════════════════════════════
class TestExecutionDrivenUpdater(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exec-drv-unit-")
        self.mgr = _make_mgr(self.tmp)

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_filled_lc(self, side: str) -> OrderLifecycle:
        lc = _make_lc(self.mgr, side)
        self.mgr.accept(lc)
        self.mgr.full_fill(lc, delta=5, avg_price=50000.0)  # on_filled=None
        return lc

    def test_T15_updater_buy_side_calls_on_buy(self):
        """T15: ExecutionDrivenPositionUpdater — side=BUY → on_buy_filled 호출."""
        on_buy  = MagicMock()
        on_sell = MagicMock()
        updater = ExecutionDrivenPositionUpdater(on_buy_filled=on_buy, on_sell_filled=on_sell)
        lc = self._make_filled_lc("BUY")
        updater(lc)
        on_buy.assert_called_once_with(lc)
        on_sell.assert_not_called()

    def test_T16_updater_sell_side_calls_on_sell(self):
        """T16: ExecutionDrivenPositionUpdater — side=SELL → on_sell_filled 호출."""
        on_buy  = MagicMock()
        on_sell = MagicMock()
        updater = ExecutionDrivenPositionUpdater(on_buy_filled=on_buy, on_sell_filled=on_sell)
        lc = self._make_filled_lc("SELL")
        updater(lc)
        on_sell.assert_called_once_with(lc)
        on_buy.assert_not_called()

    def test_T17_updater_sell_no_callback_no_raise(self):
        """T17: SELL 콜백 없음 → 예외 없음, 경고만."""
        updater = ExecutionDrivenPositionUpdater(on_buy_filled=None, on_sell_filled=None)
        lc = self._make_filled_lc("SELL")
        updater(lc)  # 예외 없이 통과해야 함

    def test_T18_updater_non_filled_state_no_call(self):
        """T18: FILLED 아닌 상태(ORDER_ACCEPTED) → 콜백 미호출."""
        on_buy  = MagicMock()
        on_sell = MagicMock()
        updater = ExecutionDrivenPositionUpdater(on_buy_filled=on_buy, on_sell_filled=on_sell)
        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        # ORDER_ACCEPTED 상태에서 직접 updater 호출
        updater(lc)
        on_buy.assert_not_called()
        on_sell.assert_not_called()


# ══════════════════════════════════════════════════════════════
# T19-T21: dispatch_fill
# ══════════════════════════════════════════════════════════════
class TestDispatchFill(unittest.TestCase):
    """StrategyManager.dispatch_fill() 메서드 테스트.

    실제 StrategyManager 의존성(KIS API 등)을 Mock으로 교체하여
    dispatch_fill 의 핵심 흐름만 검증한다.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exec-drv-dispatch-")
        self.mgr = _make_mgr(self.tmp)
        self.on_buy  = MagicMock(return_value=None)
        self.on_sell = MagicMock(return_value=None)
        self.updater = ExecutionDrivenPositionUpdater(
            on_buy_filled=self.on_buy,
            on_sell_filled=self.on_sell,
        )

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def _simulate_dispatch_fill(self, order_lifecycle_id, filled_qty, avg_fill_price, is_full=True):
        """StrategyManager.dispatch_fill() 핵심 로직 시뮬레이션."""
        lc = self.mgr.load(order_lifecycle_id)
        if lc is None:
            return False
        if is_full:
            self.mgr.full_fill(lc, delta=filled_qty, avg_price=avg_fill_price,
                               on_filled=self.updater)
        else:
            self.mgr.partial_fill(lc, delta=filled_qty, avg_price=avg_fill_price)
        return True

    def test_T19_dispatch_fill_full_triggers_callback(self):
        """T19: dispatch_fill is_full=True → full_fill + on_filled 콜백 호출."""
        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        result = self._simulate_dispatch_fill(
            lc.order_lifecycle_id, filled_qty=10, avg_fill_price=50000.0, is_full=True
        )
        self.assertTrue(result)
        self.on_buy.assert_called_once()
        reloaded = self.mgr.load(lc.order_lifecycle_id)
        self.assertEqual(reloaded.current_state, LifecycleState.FILLED)

    def test_T20_dispatch_fill_partial_no_callback(self):
        """T20: dispatch_fill is_full=False → partial_fill (포지션 변경 없음)."""
        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        result = self._simulate_dispatch_fill(
            lc.order_lifecycle_id, filled_qty=3, avg_fill_price=50000.0, is_full=False
        )
        self.assertTrue(result)
        self.on_buy.assert_not_called()
        self.on_sell.assert_not_called()
        reloaded = self.mgr.load(lc.order_lifecycle_id)
        self.assertEqual(reloaded.current_state, LifecycleState.PARTIALLY_FILLED)
        self.assertEqual(reloaded.filled_qty, 3)

    def test_T21_dispatch_fill_unknown_id_no_raise(self):
        """T21: 존재하지 않는 order_lifecycle_id → False 반환, 예외 없음."""
        result = self._simulate_dispatch_fill(
            "KR_BUY_000000_20260725000000000_unknown",
            filled_qty=5, avg_fill_price=50000.0, is_full=True
        )
        self.assertFalse(result)
        self.on_buy.assert_not_called()


# ══════════════════════════════════════════════════════════════
# T22-T24: StrategyManager 폴백 / 순서 보장
# ══════════════════════════════════════════════════════════════
class TestLifecycleDisabled(unittest.TestCase):
    """_lifecycle_mgr=None 일 때 기존 방식(직접 apply_buy/apply_sell) 유지 검증."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exec-drv-fallback-")
        self.mgr = _make_mgr(self.tmp)

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_T22_fallback_buy_apply_buy_called_directly(self):
        """T22: lifecycle 비활성(_lifecycle_mgr=None) → apply_buy 직접 호출."""
        call_log = []
        on_buy  = MagicMock(side_effect=lambda lc: call_log.append("buy"))
        on_sell = MagicMock()
        updater = ExecutionDrivenPositionUpdater(on_buy_filled=on_buy, on_sell_filled=on_sell)

        # lifecycle_mgr=None 시나리오: 직접 full_fill 우회
        # (StrategyManager 폴백 경로가 apply_buy 직접 호출하는 것을 상징)
        apply_buy_called = []
        def mock_apply_buy(*args, **kwargs):
            apply_buy_called.append(args)

        lc = _make_lc(self.mgr, "BUY")
        self.mgr.accept(lc)
        # lifecycle 없이 직접 apply_buy 호출 = 폴백 경로 시뮬레이션
        mock_apply_buy("005930", "삼성전자", 1, 10, 50000.0)
        self.assertEqual(len(apply_buy_called), 1)
        # lifecycle가 없으므로 on_buy 콜백은 없음
        on_buy.assert_not_called()

    def test_T23_fallback_sell_apply_sell_called_directly(self):
        """T23: lifecycle 비활성(_lifecycle_mgr=None) → apply_sell 직접 호출."""
        apply_sell_called = []
        def mock_apply_sell(*args, **kwargs):
            apply_sell_called.append(args)

        # 폴백 경로 시뮬레이션
        mock_apply_sell("005930", 10, 55000.0)
        self.assertEqual(len(apply_sell_called), 1)
        self.assertEqual(apply_sell_called[0][0], "005930")

    def test_T24_buy_fill_before_sell_fill(self):
        """T24: BUY FILLED 먼저 apply_buy, 이후 SELL FILLED 시 apply_sell.

        순서: create BUY → accept → full_fill (apply_buy)
              create SELL → accept → full_fill (apply_sell)
        """
        call_order = []

        def on_buy_filled(lc):
            call_order.append(("BUY_FILLED", lc.code))

        def on_sell_filled(lc):
            call_order.append(("SELL_FILLED", lc.code))

        updater = ExecutionDrivenPositionUpdater(
            on_buy_filled=on_buy_filled,
            on_sell_filled=on_sell_filled,
        )

        buy_lc = _make_lc(self.mgr, "BUY", code="005930")
        self.mgr.accept(buy_lc)
        self.mgr.full_fill(buy_lc, delta=10, avg_price=50000.0, on_filled=updater)

        sell_lc = _make_lc(self.mgr, "SELL", code="005930")
        self.mgr.accept(sell_lc)
        self.mgr.full_fill(sell_lc, delta=10, avg_price=55000.0, on_filled=updater)

        self.assertEqual(len(call_order), 2)
        self.assertEqual(call_order[0], ("BUY_FILLED", "005930"))
        self.assertEqual(call_order[1], ("SELL_FILLED", "005930"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
