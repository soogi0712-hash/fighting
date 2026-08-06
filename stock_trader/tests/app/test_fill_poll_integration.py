"""tests/app/test_fill_poll_integration.py
Phase 4 — app.py 폴링 연결 통합 테스트 (8개 TC)

검증 항목:
  T01  run_fill_poll: _fill_observer=None → 빈 dict 반환, 예외 없음
  T02  run_us_fill_poll: _us_fill_observer=None → 빈 dict 반환, 예외 없음
  T03  run_fill_poll: ACTIVE 주문 0건 → poll_once 미호출 (early-return guard)
  T04  run_us_fill_poll: ACTIVE 주문 0건 → poll_once 미호출 (early-return guard)
  T05  run_fill_poll: pending 1건 FILLED → dispatched 리스트에 lifecycle_id 포함
  T06  run_us_fill_poll: pending 1건 FILLED → dispatched 리스트에 lifecycle_id 포함
  T07  run_fill_poll: poll_once() 예외 → errors=1 반환, 상위 예외 전파 없음
  T08  double accept — accept() 2회 호출 시 LifecycleTransitionError 미발생 (멱등)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from phoenix.lifecycle import (
    OrderLifecycleManager,
    make_order_lifecycle_id,
)


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _make_accepted_lc(mgr: OrderLifecycleManager,
                      market: str = "KR",
                      side: str = "BUY",
                      code: str = "005930",
                      qty: int = 10):
    """UNKNOWN → SIGNAL_CONFIRMED → ORDER_SUBMITTED → ORDER_ACCEPTED."""
    lc_id = make_order_lifecycle_id(market, side, code)
    lc = mgr.create(
        trade_id=lc_id, market=market, code=code,
        side=side, strategy_name="TestStrategy", order_qty=qty,
    )
    mgr.confirm_signal(lc)
    lc2 = mgr.load(lc.order_lifecycle_id)
    mgr.submit(lc2)
    lc3 = mgr.load(lc.order_lifecycle_id)
    mgr.accept(lc3)
    return mgr.load(lc.order_lifecycle_id)


def _make_kr_strategy_stub(tmp_dir: str):
    """KR StrategyManager 경량 stub — _fill_observer / _pending_registry 직접 주입 가능."""
    from strategies.strategy_manager import StrategyManager

    fake_api = MagicMock()
    fake_api.get_balance.return_value = {"cash": 1_000_000}

    with patch("strategies.strategy_manager.PyramidStrategyManager"), \
         patch("strategies.strategy_manager.IndicatorValidator"), \
         patch("strategies.strategy_manager.DailyPnLGuard"), \
         patch("strategies.strategy_manager.ReentryGuard"), \
         patch("strategies.strategy_manager.TradeDecisionEngine"), \
         patch("strategies.strategy_manager._JOURNAL_ENABLED", False), \
         patch("strategies.strategy_manager._FILL_OBSERVER_ENABLED", False):
        sm = StrategyManager.__new__(StrategyManager)
        sm.api               = fake_api
        sm.pyramid           = MagicMock()
        sm.pnl_guard         = MagicMock()
        sm.pnl_guard.can_buy = True
        sm.reentry           = MagicMock()
        sm.decision          = MagicMock()
        sm.validator         = MagicMock()
        sm._pending_buy_meta  = {}
        sm._pending_sell_meta = {}
        sm._pending_registry  = None
        sm._fill_observer     = None
        sm._log_trade         = MagicMock()

        db_path = os.path.join(tmp_dir, "kr_journal.db")
        sm._lifecycle_mgr = OrderLifecycleManager(db_path)

        # 실제 메서드 바인딩
        from strategies.strategy_manager import StrategyManager as _SM
        sm.run_fill_poll  = _SM.run_fill_poll.__get__(sm)
        sm.dispatch_fill  = _SM.dispatch_fill.__get__(sm)
        return sm


def _make_us_strategy_stub(tmp_dir: str):
    """US USStrategyManager 경량 stub."""
    from strategies.us_strategy_manager import USStrategyManager

    fake_api = MagicMock()

    with patch("strategies.us_strategy_manager._US_LIFECYCLE_ENABLED", False), \
         patch("strategies.us_strategy_manager._US_FILL_OBSERVER_ENABLED", False):
        us = USStrategyManager.__new__(USStrategyManager)
        us.api                   = fake_api
        us.pos_mgr               = MagicMock()
        us._us_pending_registry  = None
        us._us_fill_observer     = None
        us._us_lifecycle_mgr     = None
        us._us_pending_buy_meta  = {}
        us._us_pending_sell_meta = {}
        us._us_fill_events       = []
        us._us_applied_store     = None   # delta 부킹 비활성(디스패치 경로만 검증)

        db_path = os.path.join(tmp_dir, "us_journal.db")
        us._us_lifecycle_mgr = OrderLifecycleManager(db_path)

        # 실제 메서드 바인딩
        from strategies.us_strategy_manager import USStrategyManager as _US
        us.run_us_fill_poll  = _US.run_us_fill_poll.__get__(us)
        us.us_dispatch_fill  = _US.us_dispatch_fill.__get__(us)
        return us


def _filled_poll_result(lifecycle_id: str) -> dict:
    """FILLED 상태의 poll_once() mock 반환값."""
    from journal.fill_observer import PendingStatus
    return {
        "total": 1, "filled": 1, "partial": 0,
        "no_change": 0, "errors": 0,
        "details": [{
            "trade_id":    lifecycle_id,
            "fill_delta":  10,
            "cum_filled":  10,
            "status_after": PendingStatus.FILLED,
            "error":       None,
        }],
    }


# ──────────────────────────────────────────────────────────────
# T01: KR run_fill_poll — fill_observer=None → 빈 dict 반환
# ──────────────────────────────────────────────────────────────

class TestT01KrFillPollObserverNone(unittest.TestCase):
    def test_returns_empty_dict_when_observer_is_none(self):
        """T01: _fill_observer=None → 즉시 빈 dict 반환, 예외 없음."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_kr_strategy_stub(tmp)
            sm._fill_observer = None   # 명시적으로 None

            result = sm.run_fill_poll()

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["filled"], 0)
        self.assertEqual(result["dispatched"], [])
        self.assertEqual(result["errors"], 0)


# ──────────────────────────────────────────────────────────────
# T02: US run_us_fill_poll — fill_observer=None → 빈 dict 반환
# ──────────────────────────────────────────────────────────────

class TestT02UsFillPollObserverNone(unittest.TestCase):
    def test_returns_empty_dict_when_us_observer_is_none(self):
        """T02: _us_fill_observer=None → 즉시 빈 dict 반환, 예외 없음."""
        with tempfile.TemporaryDirectory() as tmp:
            us = _make_us_strategy_stub(tmp)
            us._us_fill_observer = None

            result = us.run_us_fill_poll()

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["dispatched"], [])
        self.assertEqual(result["errors"], 0)


# ──────────────────────────────────────────────────────────────
# T03: KR early-return guard — ACTIVE 주문 0건 → poll_once 미호출
# ──────────────────────────────────────────────────────────────

class TestT03KrEarlyReturnGuard(unittest.TestCase):
    def test_poll_once_not_called_when_no_active_orders(self):
        """T03: pending_registry.get_trackable() == [] → poll_once 미호출."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_kr_strategy_stub(tmp)

            # registry mock: trackable 0건
            mock_registry = MagicMock()
            mock_registry.get_trackable.return_value = []
            sm._pending_registry = mock_registry

            mock_observer = MagicMock()
            sm._fill_observer = mock_observer

            result = sm.run_fill_poll()

        # poll_once는 호출되지 않아야 함 (KIS API 호출 없음)
        mock_observer.poll_once.assert_not_called()
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["dispatched"], [])


# ──────────────────────────────────────────────────────────────
# T04: US early-return guard — ACTIVE 주문 0건 → poll_once 미호출
# ──────────────────────────────────────────────────────────────

class TestT04UsEarlyReturnGuard(unittest.TestCase):
    def test_poll_once_not_called_when_no_active_us_orders(self):
        """T04: _us_pending_registry.get_trackable() == [] → poll_once 미호출."""
        with tempfile.TemporaryDirectory() as tmp:
            us = _make_us_strategy_stub(tmp)

            mock_registry = MagicMock()
            mock_registry.get_trackable.return_value = []
            us._us_pending_registry = mock_registry

            mock_observer = MagicMock()
            us._us_fill_observer = mock_observer

            result = us.run_us_fill_poll()

        mock_observer.poll_once.assert_not_called()
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["dispatched"], [])


# ──────────────────────────────────────────────────────────────
# T05: KR pending 1건 FILLED → dispatched에 lifecycle_id 포함
# ──────────────────────────────────────────────────────────────

class TestT05KrFillPollDispatch(unittest.TestCase):
    def test_filled_order_dispatched(self):
        """T05: poll_once → FILLED 1건 → dispatched 리스트에 lifecycle_id 포함."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_kr_strategy_stub(tmp)

            # ACCEPTED lifecycle 생성
            lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "005930", 10)
            lc_id = lc.order_lifecycle_id

            # registry: 1건 trackable 반환
            mock_registry = MagicMock()
            mock_registry.get_trackable.return_value = [{"trade_id": lc_id}]
            mock_registry.get_by_trade_id.return_value = {"order_qty": 10}
            sm._pending_registry = mock_registry

            # observer: FILLED poll_result 반환
            mock_observer = MagicMock()
            mock_observer.poll_once.return_value = _filled_poll_result(lc_id)
            sm._fill_observer = mock_observer

            # dispatch_fill이 실제로 lifecycle을 전이하지 않도록 mock
            sm.dispatch_fill = MagicMock(return_value=None)

            result = sm.run_fill_poll()

        mock_observer.poll_once.assert_called_once()
        self.assertIn(lc_id, result["dispatched"])
        self.assertEqual(result["filled"], 1)
        sm.dispatch_fill.assert_called_once_with(
            order_lifecycle_id=lc_id,
            filled_qty=10,
            avg_fill_price=unittest.mock.ANY,
            is_full=True,
        )


# ──────────────────────────────────────────────────────────────
# T06: US pending 1건 FILLED → dispatched에 lifecycle_id 포함
# ──────────────────────────────────────────────────────────────

class TestT06UsFillPollDispatch(unittest.TestCase):
    def test_us_filled_order_dispatched(self):
        """T06: US poll_once → FILLED 1건 → dispatched 리스트에 lifecycle_id 포함."""
        with tempfile.TemporaryDirectory() as tmp:
            us = _make_us_strategy_stub(tmp)

            lc = _make_accepted_lc(us._us_lifecycle_mgr, "US", "BUY", "AAPL", 5)
            lc_id = lc.order_lifecycle_id

            # registry: 1건 trackable
            mock_registry = MagicMock()
            mock_registry.get_trackable.return_value = [{"trade_id": lc_id}]
            us._us_pending_registry = mock_registry

            # observer: FILLED
            mock_observer = MagicMock()
            mock_observer.poll_once.return_value = _filled_poll_result(lc_id)
            us._us_fill_observer = mock_observer

            us.us_dispatch_fill = MagicMock(return_value=None)

            result = us.run_us_fill_poll()

        mock_observer.poll_once.assert_called_once()
        self.assertIn(lc_id, result["dispatched"])
        self.assertEqual(result["filled"], 1)
        us.us_dispatch_fill.assert_called_once_with(
            order_lifecycle_id=lc_id,
            filled_qty=10,
            avg_fill_price=unittest.mock.ANY,
            is_full=True,
        )


# ──────────────────────────────────────────────────────────────
# T07: KR run_fill_poll — poll_once() 예외 → errors=1, 예외 전파 없음
# ──────────────────────────────────────────────────────────────

class TestT07KrPollOnceException(unittest.TestCase):
    def test_poll_once_exception_returns_error_result(self):
        """T07: poll_once()가 예외를 던지면 errors=1 반환, 상위 예외 전파 없음."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_kr_strategy_stub(tmp)

            mock_registry = MagicMock()
            mock_registry.get_trackable.return_value = [{"trade_id": "some-id"}]
            sm._pending_registry = mock_registry

            mock_observer = MagicMock()
            mock_observer.poll_once.side_effect = RuntimeError("KIS API 장애")
            sm._fill_observer = mock_observer

            # 예외가 전파되지 않아야 함
            try:
                result = sm.run_fill_poll()
            except Exception as e:
                self.fail(f"run_fill_poll()이 예외를 전파했습니다: {e}")

        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["dispatched"], [])
        self.assertEqual(result["total"], 0)


# ──────────────────────────────────────────────────────────────
# T08: double accept 멱등성 — accept() 2회 호출 시 예외 없음
# ──────────────────────────────────────────────────────────────

class TestT08DoubleAcceptIdempotent(unittest.TestCase):
    def test_accept_twice_no_exception(self):
        """T08: lifecycle.accept() 2회 호출 → LifecycleTransitionError 미발생 (멱등)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "journal.db")
            mgr = OrderLifecycleManager(db_path)

            lc = _make_accepted_lc(mgr, "KR", "BUY", "005930", 10)
            # 현재 상태: ORDER_ACCEPTED

            # 두 번째 accept — 멱등, 예외 없어야 함
            try:
                lc_reload = mgr.load(lc.order_lifecycle_id)
                mgr.accept(lc_reload)   # 이미 ACCEPTED → early return
            except Exception as e:
                self.fail(f"두 번째 accept()에서 예외 발생: {e}")

            # 상태는 여전히 ORDER_ACCEPTED
            from phoenix.lifecycle import LifecycleState
            lc_final = mgr.load(lc.order_lifecycle_id)
            self.assertEqual(lc_final.current_state, LifecycleState.ORDER_ACCEPTED)


# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main()
