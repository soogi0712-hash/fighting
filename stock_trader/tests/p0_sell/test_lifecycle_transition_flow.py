"""P0-2b Lifecycle 상태 전이 순서 입증.

배포된 abd236f 의 정규 BUY/SELL 접수 경로는 create() 직후 accept() 를 호출해
UNKNOWN → ORDER_ACCEPTED 전이 예외로 항상 실패(→ apply_* 폴백)했다.

이 테스트는 수정 후 정규 주문이 실제로
  UNKNOWN → SIGNAL_CONFIRMED → ORDER_SUBMITTED → ORDER_ACCEPTED
  → PARTIALLY_FILLED → FILLED
순서로 흘러가는 것을, 실제 OrderLifecycleManager / PendingOrderRegistry /
ExecutionDrivenPositionUpdater 와 StrategyManager 의 실제 메서드
(_register_pending_order / dispatch_fill)를 사용해 입증한다.

전이 시퀀스는 수정된 strategy_manager 의 각 접수 경로가 실제로 수행하는 호출 순서와
동일하다:
  - 비동기 체결 경로(BUY-ok / SELL / realloc): create → confirm_signal → submit
    → _register_pending_order(=accept+odno) → (FillObserver) dispatch_fill(partial→full)
  - 동기 체결 경로(BUY 잔고재확인): create → confirm_signal → submit → accept
    → full_fill(on_filled)
"""
import os
import sys
import shutil
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import journal.fill_observer as fo  # noqa: E402
from journal.fill_observer import PendingOrderRegistry  # noqa: E402
from phoenix.lifecycle import (  # noqa: E402
    OrderLifecycleManager, LifecycleState, LifecycleTransitionError,
    make_order_lifecycle_id,
)
from phoenix.execution_driven import ExecutionDrivenPositionUpdater  # noqa: E402
from strategies.strategy_manager import StrategyManager  # noqa: E402

S = LifecycleState


class Flow:
    """StrategyManager 의 실제 등록/체결 디스패치 메서드만 바인딩한 경량 인스턴스."""
    _register_pending_order = StrategyManager._register_pending_order
    dispatch_fill           = StrategyManager.dispatch_fill

    def __init__(self, lifecycle_mgr, registry):
        self._lifecycle_mgr    = lifecycle_mgr
        self._pending_registry = registry
        self.buy_filled  = []
        self.sell_filled = []
        self._updater = ExecutionDrivenPositionUpdater(
            on_buy_filled  = lambda lc: self.buy_filled.append(lc.order_lifecycle_id),
            on_sell_filled = lambda lc: self.sell_filled.append(lc.order_lifecycle_id),
        )


class TransitionFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p0flow-")
        self._orig = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "pending.db")
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
        self.registry = PendingOrderRegistry()
        self.mgr = OrderLifecycleManager(os.path.join(self.tmp, "lifecycle.db"))
        self.flow = Flow(self.mgr, self.registry)

    def tearDown(self):
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
        fo._JOURNAL_DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self, lc_id):
        return self.mgr.load(lc_id).current_state

    # ── 회귀 고정: 기존 버그(create 직후 accept)는 반드시 예외 ─────────────
    def test_regression_create_then_accept_raises(self):
        lc = self.mgr.create(trade_id="t", market="KR", code="005930",
                             side="SELL", order_qty=7)
        self.assertEqual(lc.current_state, S.UNKNOWN)
        with self.assertRaises(LifecycleTransitionError):
            self.mgr.accept(lc)   # UNKNOWN → ORDER_ACCEPTED 금지

    # ── 비동기 SELL 정규 주문: 전 구간 순서 입증 ──────────────────────────
    def test_sell_async_full_transition_order(self):
        lc = self.mgr.create(trade_id="ts", market="KR", code="005930",
                             side="SELL", order_qty=7)
        lc_id = lc.order_lifecycle_id
        # 1. create → UNKNOWN
        self.assertEqual(self._state(lc_id), S.UNKNOWN)
        # 2. confirm_signal → SIGNAL_CONFIRMED
        self.mgr.confirm_signal(lc)
        self.assertEqual(self._state(lc_id), S.SIGNAL_CONFIRMED)
        # 3. submit → ORDER_SUBMITTED
        self.mgr.submit(lc)
        self.assertEqual(self._state(lc_id), S.ORDER_SUBMITTED)
        # 4. _register_pending_order → ORDER_ACCEPTED (odno 주입 + pending 등록)
        odno = self.flow._register_pending_order(
            market="KR", trade_id="", code="005930", side="SELL",
            order_qty=7, order_response={"output": {"KNO_ORD_NO": "OD-1"}},
            lifecycle_id=lc_id,
        )
        self.assertEqual(odno, "OD-1")
        self.assertEqual(self._state(lc_id), S.ORDER_ACCEPTED)
        self.assertEqual(self.mgr.load(lc_id).odno, "OD-1")
        self.assertTrue(self.registry.has_active_sell("KR", "005930"))
        # 5. 부분체결 → PARTIALLY_FILLED
        self.flow.dispatch_fill(lc_id, filled_qty=3, avg_fill_price=54000.0,
                                is_full=False)
        self.assertEqual(self._state(lc_id), S.PARTIALLY_FILLED)
        self.assertEqual(self.mgr.load(lc_id).filled_qty, 3)
        self.assertEqual(self.flow.sell_filled, [])   # 아직 booking 없음
        # 6. 전량체결 → FILLED, on_sell_filled 정확히 1회
        self.flow.dispatch_fill(lc_id, filled_qty=4, avg_fill_price=54000.0,
                                is_full=True)
        self.assertEqual(self._state(lc_id), S.FILLED)
        self.assertEqual(self.mgr.load(lc_id).filled_qty, 7)
        self.assertEqual(self.flow.sell_filled, [lc_id])

    # ── 비동기 BUY 정규 주문: 전 구간 순서 입증 ──────────────────────────
    def test_buy_async_full_transition_order(self):
        lc = self.mgr.create(trade_id="tb", market="KR", code="000660",
                             side="BUY", order_qty=10)
        lc_id = lc.order_lifecycle_id
        self.assertEqual(self._state(lc_id), S.UNKNOWN)
        self.mgr.confirm_signal(lc)
        self.assertEqual(self._state(lc_id), S.SIGNAL_CONFIRMED)
        self.mgr.submit(lc)
        self.assertEqual(self._state(lc_id), S.ORDER_SUBMITTED)
        self.flow._register_pending_order(
            market="KR", trade_id="", code="000660", side="BUY",
            order_qty=10, order_response={"output": {"KNO_ORD_NO": "OD-2"}},
            lifecycle_id=lc_id,
        )
        self.assertEqual(self._state(lc_id), S.ORDER_ACCEPTED)
        self.assertTrue(self.registry.has_active_order("KR", "000660", "BUY"))
        # partial → full
        self.flow.dispatch_fill(lc_id, filled_qty=4, avg_fill_price=180000.0,
                                is_full=False)
        self.assertEqual(self._state(lc_id), S.PARTIALLY_FILLED)
        self.flow.dispatch_fill(lc_id, filled_qty=6, avg_fill_price=180000.0,
                                is_full=True)
        self.assertEqual(self._state(lc_id), S.FILLED)
        self.assertEqual(self.mgr.load(lc_id).filled_qty, 10)
        self.assertEqual(self.flow.buy_filled, [lc_id])

    # ── 동기 BUY(잔고 재확인) 정규 주문: create→…→accept→full_fill ────────
    def test_buy_sync_balance_reconfirm_transition_order(self):
        lc = self.mgr.create(trade_id="tsync", market="KR", code="005930",
                             side="BUY", order_qty=5)
        lc_id = lc.order_lifecycle_id
        self.assertEqual(self._state(lc_id), S.UNKNOWN)
        self.mgr.confirm_signal(lc)
        self.mgr.submit(lc)
        self.mgr.accept(lc)   # 이 경로는 register 대신 직접 accept
        self.assertEqual(self._state(lc_id), S.ORDER_ACCEPTED)
        # 즉시 full_fill (잔고로 체결 확인됨)
        self.mgr.full_fill(lc, delta=5, avg_price=1000.0,
                           on_filled=self.flow._updater)
        self.assertEqual(self._state(lc_id), S.FILLED)
        self.assertEqual(self.flow.buy_filled, [lc_id])

    # ── 직행 전량체결(부분체결 없이 ACCEPTED→FILLED)도 허용됨 입증 ────────
    def test_accepted_direct_to_filled(self):
        lc = self.mgr.create(trade_id="td", market="KR", code="005930",
                             side="SELL", order_qty=7)
        lc_id = lc.order_lifecycle_id
        self.mgr.confirm_signal(lc)
        self.mgr.submit(lc)
        self.flow._register_pending_order(
            market="KR", trade_id="", code="005930", side="SELL",
            order_qty=7, order_response={"output": {"KNO_ORD_NO": "OD-3"}},
            lifecycle_id=lc_id,
        )
        self.assertEqual(self._state(lc_id), S.ORDER_ACCEPTED)
        # 부분체결 없이 곧바로 전량체결
        self.flow.dispatch_fill(lc_id, filled_qty=7, avg_fill_price=54000.0,
                                is_full=True)
        self.assertEqual(self._state(lc_id), S.FILLED)
        self.assertEqual(self.flow.sell_filled, [lc_id])


if __name__ == "__main__":
    unittest.main()
