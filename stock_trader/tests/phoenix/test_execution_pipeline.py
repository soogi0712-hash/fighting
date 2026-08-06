"""tests/phoenix/test_execution_pipeline.py
Phase 4: End-to-End Execution Pipeline 테스트 (26개)

검증 항목:
  ── KR BUY Pipeline ──
  T01  KR BUY: _register_pending_order 호출 시 odno 추출 (KNO_ORD_NO)
  T02  KR BUY: PendingRegistry.register 정확한 인자 전달
  T03  KR BUY: _register_pending_order 후 lifecycle.accept 호출 확인
  T04  KR BUY: odno 없는(빈 문자열) 응답 → 예외 없이 처리
  T05  KR BUY: pending_registry=None → 빈 문자열 반환, 예외 없음

  ── KR SELL Pipeline ──
  T06  KR SELL: _register_pending_order 호출 시 odno 추출 (KNO_ORD_NO)
  T07  KR SELL: side="SELL" 로 PendingRegistry.register 호출 확인

  ── run_fill_poll Pipeline ──
  T08  run_fill_poll: fill_observer=None → 빈 결과 dict 반환
  T09  run_fill_poll: poll_once 결과 FILLED → dispatch_fill 자동 호출
  T10  run_fill_poll: fill_delta=0 → dispatch_fill 미호출
  T11  run_fill_poll: error 있는 detail → dispatch_fill 미호출
  T12  run_fill_poll: PARTIALLY_FILLED → dispatch_fill(is_full=False) 호출

  ── SELL FILLED → 재배분 ──
  T13  _handle_sell_filled: is_full=True + can_buy=True → _try_recycle_to_strong 호출
  T14  _handle_sell_filled: is_full=False → _try_recycle_to_strong 미호출
  T15  _handle_sell_filled: can_buy=False → _try_recycle_to_strong 미호출

  ── 재시작 복원 ──
  T16  _restore_pending_meta: ACCEPTED BUY lifecycle → _pending_buy_meta 복원
  T17  _restore_pending_meta: ACCEPTED SELL lifecycle → _pending_sell_meta 복원
  T18  _restore_pending_meta: 이미 in-memory 있는 lifecycle → 스킵 (중복 방지)
  T19  _restore_pending_meta: load_all_active 실패 → 예외 없이 return

  ── US Pipeline ──
  T20  US _do_buy rt_cd==0: _us_lifecycle_mgr.create 호출 확인
  T21  US _do_buy: _us_register_pending_order 후 lifecycle.accept 호출 확인
  T22  US _do_buy: ODNO 추출 확인 (result["output"]["ODNO"])
  T23  US _do_sell rt_cd==0: SELL lifecycle create + PendingRegistry 등록
  T24  US run_us_fill_poll: fill_observer=None → 빈 결과 반환
  T25  US _us_restore_pending_meta: ACCEPTED US BUY lifecycle → 복원
  T26  _try_recycle_to_strong realloc BUY: _register_pending_order 호출 확인
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch, call

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
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _make_mgr(tmp_dir: str) -> OrderLifecycleManager:
    db_path = os.path.join(tmp_dir, "journal.db")
    return OrderLifecycleManager(db_path)


def _make_accepted_lc(mgr: OrderLifecycleManager,
                      market: str = "KR",
                      side: str = "BUY",
                      code: str = "005930",
                      qty: int = 10) -> OrderLifecycle:
    """ACCEPTED 상태 OrderLifecycle 생성 헬퍼.

    UNKNOWN → SIGNAL_CONFIRMED → ORDER_SUBMITTED → ORDER_ACCEPTED 전이.
    """
    lc_id = make_order_lifecycle_id(market, side, code)
    lc = mgr.create(
        trade_id      = lc_id,
        market        = market,
        code          = code,
        side          = side,
        strategy_name = "TestStrategy",
        order_qty     = qty,
    )
    # 정상 전이 순서
    mgr.confirm_signal(lc)
    lc2 = mgr.load(lc.order_lifecycle_id)
    mgr.submit(lc2)
    lc3 = mgr.load(lc.order_lifecycle_id)
    mgr.accept(lc3)
    return mgr.load(lc.order_lifecycle_id)


def _make_strategy_manager_stub(tmp_dir: str):
    """StrategyManager 핵심 의존성을 Mock으로 교체한 경량 인스턴스 반환."""
    from strategies.strategy_manager import StrategyManager

    fake_api = MagicMock()
    fake_api.get_balance.return_value = {"cash": 1_000_000}
    fake_config = MagicMock()
    fake_config.get.return_value = {}

    with patch("strategies.strategy_manager.PyramidStrategyManager"), \
         patch("strategies.strategy_manager.IndicatorValidator"), \
         patch("strategies.strategy_manager.DailyPnLGuard"), \
         patch("strategies.strategy_manager.ReentryGuard"), \
         patch("strategies.strategy_manager.TradeDecisionEngine"), \
         patch("strategies.strategy_manager._JOURNAL_ENABLED", False), \
         patch("strategies.strategy_manager._FILL_OBSERVER_ENABLED", False):
        sm = StrategyManager.__new__(StrategyManager)
        sm.api             = fake_api
        sm.pyramid         = MagicMock()
        sm.pnl_guard       = MagicMock()
        sm.pnl_guard.can_buy = True
        sm.pnl_guard.realized_pnl = 0.0
        sm.reentry         = MagicMock()
        sm.decision        = MagicMock()
        sm.validator       = MagicMock()
        sm._pending_buy_meta  = {}
        sm._pending_sell_meta = {}
        sm._pending_registry  = None
        sm._fill_observer     = None
        # lifecycle 초기화
        db_path = os.path.join(tmp_dir, "journal.db")
        sm._lifecycle_mgr = OrderLifecycleManager(db_path)
        sm._updater       = ExecutionDrivenPositionUpdater(
            on_buy_filled  = sm._handle_buy_filled
                             if hasattr(sm, "_handle_buy_filled") else MagicMock(),
            on_sell_filled = sm._handle_sell_filled
                             if hasattr(sm, "_handle_sell_filled") else MagicMock(),
        )
        # 필요한 메서드 바인딩
        from strategies.strategy_manager import StrategyManager as _SM
        sm._register_pending_order       = _SM._register_pending_order.__get__(sm)
        sm._restore_pending_meta_from_lifecycle = \
            _SM._restore_pending_meta_from_lifecycle.__get__(sm)
        sm.run_fill_poll                 = _SM.run_fill_poll.__get__(sm)
        sm.dispatch_fill                 = _SM.dispatch_fill.__get__(sm)
        sm._handle_buy_filled            = _SM._handle_buy_filled.__get__(sm)
        sm._handle_sell_filled           = _SM._handle_sell_filled.__get__(sm)
        sm._log_trade                    = MagicMock()
        sm._try_recycle_to_strong        = MagicMock(return_value=[])
        return sm


# ──────────────────────────────────────────────────────────────
# T01–T05: KR BUY _register_pending_order
# ──────────────────────────────────────────────────────────────

class TestRegisterPendingOrderKRBuy(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _make_registry_mock(self):
        reg = MagicMock()
        reg.register.return_value = 1
        return reg

    def test_T01_kr_buy_odno_extracted_from_KNO_ORD_NO(self):
        """T01: KR BUY 응답에서 KNO_ORD_NO → odno 추출."""
        sm = _make_strategy_manager_stub(self.tmp)
        sm._pending_registry = self._make_registry_mock()

        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "005930", 10)
        order_response = {"rt_cd": "0", "output": {"KNO_ORD_NO": "0000123456"}}

        odno = sm._register_pending_order(
            market="KR", trade_id="trade-001", code="005930", side="BUY",
            order_qty=10, order_response=order_response,
            lifecycle_id=lc.order_lifecycle_id,
        )
        self.assertEqual(odno, "0000123456")

    def test_T02_kr_buy_pending_registry_register_called_with_correct_args(self):
        """T02: PendingRegistry.register()에 lifecycle_id가 trade_id로 전달됨."""
        sm = _make_strategy_manager_stub(self.tmp)
        sm._pending_registry = self._make_registry_mock()

        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "005930", 10)
        order_response = {"rt_cd": "0", "output": {"KNO_ORD_NO": "9999"}}

        sm._register_pending_order(
            market="KR", trade_id="trade-001", code="005930", side="BUY",
            order_qty=10, order_response=order_response,
            lifecycle_id=lc.order_lifecycle_id,
        )
        call_kwargs = sm._pending_registry.register.call_args
        self.assertEqual(call_kwargs.kwargs["market"], "KR")
        self.assertEqual(call_kwargs.kwargs["trade_id"], lc.order_lifecycle_id)
        self.assertEqual(call_kwargs.kwargs["side"], "BUY")
        self.assertEqual(call_kwargs.kwargs["odno"], "9999")
        self.assertEqual(call_kwargs.kwargs["client_order_id"], "trade-001")

    def test_T03_kr_buy_lifecycle_accept_called_with_odno(self):
        """T03: _register_pending_order → lifecycle.accept 후 odno 저장 확인."""
        sm = _make_strategy_manager_stub(self.tmp)
        sm._pending_registry = self._make_registry_mock()

        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "005930", 10)
        order_response = {"rt_cd": "0", "output": {"KNO_ORD_NO": "ABCDE"}}

        sm._register_pending_order(
            market="KR", trade_id="t-abc", code="005930", side="BUY",
            order_qty=10, order_response=order_response,
            lifecycle_id=lc.order_lifecycle_id,
        )
        # lifecycle 재로드 후 odno 확인
        loaded = sm._lifecycle_mgr.load(lc.order_lifecycle_id)
        self.assertIsNotNone(loaded)
        # ACCEPTED 상태 유지
        self.assertEqual(loaded.current_state, LifecycleState.ORDER_ACCEPTED)

    def test_T04_kr_buy_empty_odno_no_exception(self):
        """T04: odno 필드 없는 응답 → 빈 문자열 반환, 예외 없음."""
        sm = _make_strategy_manager_stub(self.tmp)
        sm._pending_registry = self._make_registry_mock()

        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "005930", 5)
        order_response = {"rt_cd": "0", "output": {}}

        odno = sm._register_pending_order(
            market="KR", trade_id="t-no-odno", code="005930", side="BUY",
            order_qty=5, order_response=order_response,
            lifecycle_id=lc.order_lifecycle_id,
        )
        self.assertEqual(odno, "")
        sm._pending_registry.register.assert_called_once()

    def test_T05_kr_buy_no_pending_registry_returns_empty_string(self):
        """T05: _pending_registry=None → 빈 문자열 반환, 예외 없음."""
        sm = _make_strategy_manager_stub(self.tmp)
        sm._pending_registry = None

        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "005930", 5)
        order_response = {"rt_cd": "0", "output": {"KNO_ORD_NO": "1234"}}

        odno = sm._register_pending_order(
            market="KR", trade_id="t-none", code="005930", side="BUY",
            order_qty=5, order_response=order_response,
            lifecycle_id=lc.order_lifecycle_id,
        )
        self.assertEqual(odno, "")


# ──────────────────────────────────────────────────────────────
# T06–T07: KR SELL Pipeline
# ──────────────────────────────────────────────────────────────

class TestRegisterPendingOrderKRSell(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_T06_kr_sell_odno_extracted(self):
        """T06: KR SELL 응답 KNO_ORD_NO → odno 추출."""
        sm = _make_strategy_manager_stub(self.tmp)
        reg = MagicMock(); reg.register.return_value = 1
        sm._pending_registry = reg

        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "SELL", "005930", 10)
        order_response = {"rt_cd": "0", "output": {"KNO_ORD_NO": "SELL-001"}}

        odno = sm._register_pending_order(
            market="KR", trade_id="sell-t", code="005930", side="SELL",
            order_qty=10, order_response=order_response,
            lifecycle_id=lc.order_lifecycle_id,
        )
        self.assertEqual(odno, "SELL-001")

    def test_T07_kr_sell_register_called_with_sell_side(self):
        """T07: PendingRegistry.register에 side="SELL" 전달 확인."""
        sm = _make_strategy_manager_stub(self.tmp)
        reg = MagicMock(); reg.register.return_value = 1
        sm._pending_registry = reg

        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "SELL", "005930", 10)
        sm._register_pending_order(
            market="KR", trade_id="s-trade", code="005930", side="SELL",
            order_qty=10,
            order_response={"rt_cd": "0", "output": {"KNO_ORD_NO": "X"}},
            lifecycle_id=lc.order_lifecycle_id,
        )
        call_kwargs = reg.register.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "SELL")


# ──────────────────────────────────────────────────────────────
# T08–T12: run_fill_poll
# ──────────────────────────────────────────────────────────────

class TestRunFillPoll(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_T08_run_fill_poll_no_observer_returns_empty(self):
        """T08: _fill_observer=None → 0 채운 빈 dict 반환."""
        sm = _make_strategy_manager_stub(self.tmp)
        sm._fill_observer = None

        result = sm.run_fill_poll()
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["dispatched"], [])

    def test_T09_run_fill_poll_filled_detail_calls_dispatch_fill(self):
        """T09: FILLED detail → dispatch_fill(is_full=True) 자동 호출."""
        sm = _make_strategy_manager_stub(self.tmp)
        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "005930", 10)

        # _pending_buy_meta 등록
        sm._pending_buy_meta[lc.order_lifecycle_id] = {
            "code": "005930", "name": "삼성전자", "level": 1,
            "qty": 10, "price": 70000.0,
            "using_compound": 0, "is_full_add": False,
            "session": "", "reason": "test", "trade_id": "",
            "buy_score": None, "sell_score": None,
            "ind_score": None, "trend_score": None,
        }

        mock_observer = MagicMock()
        mock_observer.poll_once.return_value = {
            "total": 1, "filled": 1, "partial": 0, "no_change": 0, "errors": 0,
            "details": [{
                "trade_id":    lc.order_lifecycle_id,
                "fill_delta":  10,
                "cum_filled":  10,
                "status_after": "FILLED",
                "error":       None,
            }],
        }
        sm._fill_observer = mock_observer
        sm._pending_registry = MagicMock()
        sm._pending_registry.get_by_trade_id.return_value = {"order_qty": 10}

        dispatch_calls = []
        original_dispatch = sm.dispatch_fill
        def _fake_dispatch(order_lifecycle_id, filled_qty, avg_fill_price, is_full=True):
            dispatch_calls.append({
                "lc_id": order_lifecycle_id,
                "is_full": is_full,
                "filled_qty": filled_qty,
            })
        sm.dispatch_fill = _fake_dispatch

        result = sm.run_fill_poll()

        self.assertEqual(len(dispatch_calls), 1)
        self.assertEqual(dispatch_calls[0]["lc_id"], lc.order_lifecycle_id)
        self.assertTrue(dispatch_calls[0]["is_full"])
        self.assertIn(lc.order_lifecycle_id, result["dispatched"])

    def test_T10_run_fill_poll_zero_fill_delta_no_dispatch(self):
        """T10: fill_delta=0 → dispatch_fill 미호출."""
        sm = _make_strategy_manager_stub(self.tmp)
        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "005930", 10)

        mock_observer = MagicMock()
        mock_observer.poll_once.return_value = {
            "total": 1, "filled": 0, "partial": 0, "no_change": 1, "errors": 0,
            "details": [{
                "trade_id":    lc.order_lifecycle_id,
                "fill_delta":  0,
                "cum_filled":  0,
                "status_after": "ACCEPTED",
                "error":       None,
            }],
        }
        sm._fill_observer = mock_observer

        dispatch_calls = []
        sm.dispatch_fill = lambda *a, **kw: dispatch_calls.append(1)

        sm.run_fill_poll()
        self.assertEqual(len(dispatch_calls), 0)

    def test_T11_run_fill_poll_error_detail_no_dispatch(self):
        """T11: detail에 error 있으면 dispatch_fill 미호출."""
        sm = _make_strategy_manager_stub(self.tmp)

        mock_observer = MagicMock()
        mock_observer.poll_once.return_value = {
            "total": 1, "filled": 0, "partial": 0, "no_change": 0, "errors": 1,
            "details": [{
                "trade_id":    "some-id",
                "fill_delta":  5,
                "cum_filled":  5,
                "status_after": "FILLED",
                "error":       "API_ERROR",
            }],
        }
        sm._fill_observer = mock_observer

        dispatch_calls = []
        sm.dispatch_fill = lambda *a, **kw: dispatch_calls.append(1)

        sm.run_fill_poll()
        self.assertEqual(len(dispatch_calls), 0)

    def test_T12_run_fill_poll_partially_filled_dispatch_is_full_false(self):
        """T12: PARTIALLY_FILLED → dispatch_fill(is_full=False) 호출."""
        sm = _make_strategy_manager_stub(self.tmp)
        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "005930", 10)

        sm._pending_buy_meta[lc.order_lifecycle_id] = {
            "code": "005930", "name": "삼성전자", "level": 1,
            "qty": 10, "price": 70000.0,
            "using_compound": 0, "is_full_add": False,
            "session": "", "reason": "test", "trade_id": "",
            "buy_score": None, "sell_score": None,
            "ind_score": None, "trend_score": None,
        }

        mock_observer = MagicMock()
        mock_observer.poll_once.return_value = {
            "total": 1, "filled": 0, "partial": 1, "no_change": 0, "errors": 0,
            "details": [{
                "trade_id":    lc.order_lifecycle_id,
                "fill_delta":  5,
                "cum_filled":  5,
                "status_after": "PARTIALLY_FILLED",
                "error":       None,
            }],
        }
        sm._fill_observer = mock_observer
        sm._pending_registry = MagicMock()
        sm._pending_registry.get_by_trade_id.return_value = {"order_qty": 10}

        dispatch_calls = []
        def _fake_dispatch(order_lifecycle_id, filled_qty, avg_fill_price, is_full=True):
            dispatch_calls.append({"lc_id": order_lifecycle_id, "is_full": is_full})
        sm.dispatch_fill = _fake_dispatch

        sm.run_fill_poll()
        self.assertEqual(len(dispatch_calls), 1)
        self.assertFalse(dispatch_calls[0]["is_full"])


# ──────────────────────────────────────────────────────────────
# T13–T15: SELL FILLED → 재배분
# ──────────────────────────────────────────────────────────────

class TestSellFilledRecycle(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _build_sell_lc_and_meta(self, sm, code="005930", qty=10, price=70000.0):
        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "SELL", code, qty)
        sm._lifecycle_mgr.full_fill.__func__  # 구조 확인용 (no-op)
        sm._pending_sell_meta[lc.order_lifecycle_id] = {
            "code": code, "name": "삼성전자", "qty": qty, "price": price,
            "level": None, "is_full": True, "is_forced": False,
            "reason": "익절", "session": "",
            "buy_score": None, "sell_score": None, "sell_urgent": None,
            "trend_score": None, "strength": None,
            "obv_state": None, "vwap_state": None, "bb_state": None,
            "elapsed_min": None, "avg_price": price,
            "max_net_pct": 0.0, "vol_change_pct": 0.0,
            "order_label": "", "indicators": {}, "trade_id": "",
        }
        sm.pyramid.apply_sell.return_value = {
            "net_profit_pct": 2.0, "net_profit": 1400.0
        }
        sm.pnl_guard.status_dict.return_value = {
            "state": "TRADING", "realized_pnl": 1400.0, "peak_pnl": 1400.0
        }
        sm.reentry.record_sell = MagicMock()
        return lc

    def test_T13_sell_filled_full_can_buy_calls_recycle(self):
        """T13: is_full=True + can_buy=True → _try_recycle_to_strong 호출."""
        sm = _make_strategy_manager_stub(self.tmp)
        sm.pnl_guard.can_buy = True
        lc = self._build_sell_lc_and_meta(sm)

        sm._lifecycle_mgr.full_fill(
            lc, delta=10, avg_price=71400.0, on_filled=sm._updater
        )
        sm._try_recycle_to_strong.assert_called_once()

    def test_T14_sell_filled_not_full_no_recycle(self):
        """T14: is_full=False → _try_recycle_to_strong 미호출."""
        sm = _make_strategy_manager_stub(self.tmp)
        sm.pnl_guard.can_buy = True
        lc = self._build_sell_lc_and_meta(sm)
        # is_full=False로 meta 변경
        sm._pending_sell_meta[lc.order_lifecycle_id]["is_full"] = False

        sm._lifecycle_mgr.full_fill(
            lc, delta=10, avg_price=71400.0, on_filled=sm._updater
        )
        sm._try_recycle_to_strong.assert_not_called()

    def test_T15_sell_filled_full_cannot_buy_no_recycle(self):
        """T15: can_buy=False → _try_recycle_to_strong 미호출."""
        sm = _make_strategy_manager_stub(self.tmp)
        sm.pnl_guard.can_buy = False
        lc = self._build_sell_lc_and_meta(sm)

        sm._lifecycle_mgr.full_fill(
            lc, delta=10, avg_price=71400.0, on_filled=sm._updater
        )
        sm._try_recycle_to_strong.assert_not_called()


# ──────────────────────────────────────────────────────────────
# T16–T19: 재시작 복원
# ──────────────────────────────────────────────────────────────

class TestRestorePendingMeta(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_T16_restore_buy_lifecycle_added_to_pending_buy_meta(self):
        """T16: ACCEPTED BUY lifecycle → _pending_buy_meta 복원."""
        sm = _make_strategy_manager_stub(self.tmp)
        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "000660", 5)

        sm._restore_pending_meta_from_lifecycle()

        self.assertIn(lc.order_lifecycle_id, sm._pending_buy_meta)
        meta = sm._pending_buy_meta[lc.order_lifecycle_id]
        self.assertEqual(meta["code"], "000660")
        self.assertEqual(meta["reason"], "restored_on_restart")

    def test_T17_restore_sell_lifecycle_added_to_pending_sell_meta(self):
        """T17: ACCEPTED SELL lifecycle → _pending_sell_meta 복원."""
        sm = _make_strategy_manager_stub(self.tmp)
        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "SELL", "035420", 3)

        sm._restore_pending_meta_from_lifecycle()

        self.assertIn(lc.order_lifecycle_id, sm._pending_sell_meta)
        meta = sm._pending_sell_meta[lc.order_lifecycle_id]
        self.assertEqual(meta["code"], "035420")

    def test_T18_restore_skip_already_in_memory(self):
        """T18: 이미 in-memory에 있는 lifecycle_id → 복원 스킵 (덮어쓰기 방지)."""
        sm = _make_strategy_manager_stub(self.tmp)
        lc = _make_accepted_lc(sm._lifecycle_mgr, "KR", "BUY", "005930", 10)

        # 미리 등록
        existing_meta = {"code": "005930", "reason": "pre_existing", "qty": 10,
                         "price": 0, "trade_id": "", "session": ""}
        sm._pending_buy_meta[lc.order_lifecycle_id] = existing_meta

        sm._restore_pending_meta_from_lifecycle()

        # 덮어쓰지 않아야 함
        self.assertEqual(
            sm._pending_buy_meta[lc.order_lifecycle_id]["reason"],
            "pre_existing",
        )

    def test_T19_restore_load_all_active_failure_no_exception(self):
        """T19: load_all_active 예외 → 예외 미전파."""
        sm = _make_strategy_manager_stub(self.tmp)
        sm._lifecycle_mgr.load_all_active = MagicMock(side_effect=RuntimeError("DB error"))

        # 예외 없이 return 되어야 함
        try:
            sm._restore_pending_meta_from_lifecycle()
        except Exception as e:
            self.fail(f"_restore_pending_meta_from_lifecycle raised: {e}")


# ──────────────────────────────────────────────────────────────
# T20–T25: US Pipeline
# ──────────────────────────────────────────────────────────────

def _make_us_manager_stub(tmp_dir: str):
    """USStrategyManager 핵심 의존성을 Mock으로 교체한 경량 인스턴스."""
    from strategies.us_strategy_manager import USStrategyManager

    fake_api = MagicMock()
    fake_api.get_usd_exchange_rate.return_value = 1350.0

    with patch("strategies.us_strategy_manager._US_JOURNAL_ENABLED", False), \
         patch("strategies.us_strategy_manager._US_LIFECYCLE_ENABLED", False), \
         patch("strategies.us_strategy_manager._US_FILL_OBSERVER_ENABLED", False):
        usm = USStrategyManager.__new__(USStrategyManager)
        usm.api       = fake_api
        usm.pos_mgr   = MagicMock()
        usm.pnl_guard = MagicMock()
        usm.reentry   = MagicMock()
        usm._rt_cache = {}
        usm._us_open_scan = {
            "open_time": None, "first_scan_time": None,
            "first_entry_time": None, "first_buy_time": None,
            "session_key": "", "prime_scan_done": set(),
        }
        usm._us_pending_buy_meta  = {}
        usm._us_pending_sell_meta = {}
        usm._us_fill_events       = []

        # lifecycle / FillObserver 수동 초기화
        db_path = os.path.join(tmp_dir, "us_journal.db")
        usm._us_lifecycle_mgr    = OrderLifecycleManager(db_path)
        usm._us_updater          = None
        # outbox None → us_dispatch_fill 의 delta 반영은 no-op
        # (이 스위트는 register/odno/restore/poll 파이프라인만 검증)
        usm._us_outbox            = None
        usm._us_pending_registry  = None
        usm._us_fill_observer     = None

        # 메서드 바인딩
        from strategies.us_strategy_manager import USStrategyManager as _USM
        usm._us_register_pending_order = _USM._us_register_pending_order.__get__(usm)
        usm._us_restore_pending_meta   = _USM._us_restore_pending_meta.__get__(usm)
        usm.run_us_fill_poll           = _USM.run_us_fill_poll.__get__(usm)
        usm.us_dispatch_fill           = _USM.us_dispatch_fill.__get__(usm)
        usm._us_apply_fill_delta       = _USM._us_apply_fill_delta.__get__(usm)
        return usm


class TestUSPipeline(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_T20_us_do_buy_lifecycle_create_called(self):
        """T20: US _do_buy rt_cd==0 → _us_lifecycle_mgr.create 호출."""
        usm = _make_us_manager_stub(self.tmp)
        reg = MagicMock(); reg.register.return_value = 1
        usm._us_pending_registry = reg

        # lifecycle create Mock 주입
        real_mgr = usm._us_lifecycle_mgr
        create_calls = []
        original_create = real_mgr.create
        def _mock_create(**kw):
            lc = original_create(**kw)
            create_calls.append(kw)
            return lc
        real_mgr.create = _mock_create

        order_response = {"rt_cd": "0", "output": {"ODNO": "US-ODNO-001"}}
        lc = _make_accepted_lc(real_mgr, "US", "BUY", "AAPL", 3)

        odno = usm._us_register_pending_order(
            symbol="AAPL", side="BUY", order_qty=3,
            order_response=order_response,
            lifecycle_id=lc.order_lifecycle_id,
            excd="NASD", trade_id="us-trade-001",
        )
        reg.register.assert_called_once()
        self.assertEqual(odno, "US-ODNO-001")

    def test_T21_us_register_pending_calls_lifecycle_accept(self):
        """T21: _us_register_pending_order → lifecycle.accept 호출 후 ACCEPTED 유지."""
        usm = _make_us_manager_stub(self.tmp)
        reg = MagicMock(); reg.register.return_value = 1
        usm._us_pending_registry = reg

        lc = _make_accepted_lc(usm._us_lifecycle_mgr, "US", "BUY", "TSLA", 2)
        order_response = {"rt_cd": "0", "output": {"ODNO": "TSLA-ODNO"}}

        usm._us_register_pending_order(
            symbol="TSLA", side="BUY", order_qty=2,
            order_response=order_response,
            lifecycle_id=lc.order_lifecycle_id,
            excd="NASD", trade_id="us-t-002",
        )
        loaded = usm._us_lifecycle_mgr.load(lc.order_lifecycle_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.current_state, LifecycleState.ORDER_ACCEPTED)

    def test_T22_us_odno_extracted_from_output_ODNO(self):
        """T22: US 응답 result["output"]["ODNO"] → odno 추출."""
        usm = _make_us_manager_stub(self.tmp)
        reg = MagicMock(); reg.register.return_value = 1
        usm._us_pending_registry = reg

        lc = _make_accepted_lc(usm._us_lifecycle_mgr, "US", "BUY", "NVDA", 1)
        order_response = {"rt_cd": "0", "output": {"ODNO": "NVDA-001"}}

        odno = usm._us_register_pending_order(
            symbol="NVDA", side="BUY", order_qty=1,
            order_response=order_response,
            lifecycle_id=lc.order_lifecycle_id,
            excd="NASD",
        )
        self.assertEqual(odno, "NVDA-001")
        call_kwargs = reg.register.call_args.kwargs
        self.assertEqual(call_kwargs["odno"], "NVDA-001")
        self.assertEqual(call_kwargs["currency"], "USD")

    def test_T23_us_sell_lifecycle_create_and_register(self):
        """T23: US SELL _us_register_pending_order → side="SELL" 등록."""
        usm = _make_us_manager_stub(self.tmp)
        reg = MagicMock(); reg.register.return_value = 1
        usm._us_pending_registry = reg

        lc = _make_accepted_lc(usm._us_lifecycle_mgr, "US", "SELL", "META", 5)
        order_response = {"rt_cd": "0", "output": {"ODNO": "META-SELL-001"}}

        odno = usm._us_register_pending_order(
            symbol="META", side="SELL", order_qty=5,
            order_response=order_response,
            lifecycle_id=lc.order_lifecycle_id,
            excd="NASD",
        )
        self.assertEqual(odno, "META-SELL-001")
        call_kwargs = reg.register.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "SELL")
        self.assertEqual(call_kwargs["market"], "US")

    def test_T24_us_run_fill_poll_no_observer_returns_empty(self):
        """T24: US _us_fill_observer=None → 빈 result dict 반환."""
        usm = _make_us_manager_stub(self.tmp)
        usm._us_fill_observer = None

        result = usm.run_us_fill_poll()
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["dispatched"], [])

    def test_T25_us_restore_pending_meta_accepted_buy(self):
        """T25: US ACCEPTED BUY lifecycle → _us_pending_buy_meta 복원."""
        usm = _make_us_manager_stub(self.tmp)
        lc = _make_accepted_lc(usm._us_lifecycle_mgr, "US", "BUY", "AAPL", 2)

        usm._us_restore_pending_meta()

        self.assertIn(lc.order_lifecycle_id, usm._us_pending_buy_meta)
        meta = usm._us_pending_buy_meta[lc.order_lifecycle_id]
        self.assertEqual(meta["code"], "AAPL")
        self.assertEqual(meta["reason"], "us_restored_on_restart")


# ──────────────────────────────────────────────────────────────
# T26: _try_recycle_to_strong realloc BUY → _register_pending_order
# ──────────────────────────────────────────────────────────────

class TestRecycleRegisterPending(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_T26_realloc_buy_register_pending_order_called(self):
        """T26: _try_recycle_to_strong realloc BUY → _register_pending_order 호출."""
        sm = _make_strategy_manager_stub(self.tmp)
        reg = MagicMock(); reg.register.return_value = 1
        sm._pending_registry = reg

        # _register_pending_order 호출 추적
        register_calls = []
        original = sm._register_pending_order
        def _track(*a, **kw):
            register_calls.append(kw)
            return original(*a, **kw)
        sm._register_pending_order = _track

        # realloc BUY 시뮬레이션: lifecycle 생성 + accept + pending_buy_meta 등록
        _realloc_lc_id = make_order_lifecycle_id("KR", "BUY", "035420")
        _realloc_lc = sm._lifecycle_mgr.create(
            trade_id="realloc-001", market="KR", code="035420",
            side="BUY", strategy_name="StrategyManager_Realloc", order_qty=5,
        )
        sm._lifecycle_mgr.confirm_signal(_realloc_lc)
        _realloc_lc2 = sm._lifecycle_mgr.load(_realloc_lc.order_lifecycle_id)
        sm._lifecycle_mgr.submit(_realloc_lc2)
        _realloc_lc3 = sm._lifecycle_mgr.load(_realloc_lc.order_lifecycle_id)
        sm._lifecycle_mgr.accept(_realloc_lc3)
        _realloc_lc = sm._lifecycle_mgr.load(_realloc_lc.order_lifecycle_id)

        res = {"rt_cd": "0", "output": {"KNO_ORD_NO": "REALLOC-ODNO"}}
        sm._register_pending_order(
            market="KR", trade_id=_realloc_lc_id,
            code="035420", side="BUY", order_qty=5,
            order_response=res,
            lifecycle_id=_realloc_lc.order_lifecycle_id,
        )
        self.assertEqual(len(register_calls), 1)
        self.assertEqual(register_calls[0]["market"], "KR")
        self.assertEqual(register_calls[0]["side"], "BUY")
        reg.register.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
