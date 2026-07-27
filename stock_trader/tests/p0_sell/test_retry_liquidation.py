"""P0-2 재시도/워치독 강제청산 라우팅 — submit_retry_liquidation 동작 테스트.

원칙 검증:
  1. retry 성공 시 Lifecycle + PendingRegistry 에 정상 등록만 수행(체결 아님).
  2. retry 체결 시 apply_sell 이 정확히 1회 실행된다.
  3. FillObserver 선행/중복 체결이 와도 booking(apply_sell) 은 1회뿐(멱등).
  4. watchdog 와 retry 가 동시에 발생해도 실제 주문(api.sell)은 1건만 나간다.

실 KIS API 미호출(FakeApi). Lifecycle/PendingRegistry 는 실제 구현을 임시 DB로 사용.
StrategyManager 의 실제 메서드(submit_retry_liquidation / has_active_sell /
_register_pending_order / _handle_sell_filled / dispatch_fill)를 그대로 바인딩해
배포 코드 경로를 그대로 실행한다.
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
from phoenix.lifecycle import OrderLifecycleManager, LifecycleState  # noqa: E402
from phoenix.execution_driven import ExecutionDrivenPositionUpdater  # noqa: E402
from strategies.strategy_manager import StrategyManager  # noqa: E402

ACCEPTED_RESP = {"rt_cd": "0", "output": {"KNO_ORD_NO": "KIS-0001"}}
REJECT_RESP = {"rt_cd": "1", "msg1": "주문가능수량초과"}


class FakeApi:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.sell_calls = []

    def sell(self, code, qty, price, ord_dvsn=None):
        self.sell_calls.append((code, qty, price, ord_dvsn))
        if self._results:
            return self._results.pop(0)
        return dict(ACCEPTED_RESP)


class FakePyramid:
    def __init__(self):
        self.apply_sell_calls = []
        self.compound_pool = 0

    def apply_sell(self, code, qty, price, level=None, is_full=True):
        self.apply_sell_calls.append((code, qty, price, level, is_full))
        return {"net_profit_pct": 1.0, "net_profit": 1000.0}


class FakePnlGuard:
    can_buy = False  # 재배분 경로 진입 방지 (테스트 단순화)

    def record(self, amt):
        pass

    def status_dict(self):
        return {"realized_pnl": 0.0, "peak_pnl": 0.0, "state": "TRADING"}


class FakeReentry:
    def __init__(self):
        self.calls = []

    def record_sell(self, **kw):
        self.calls.append(kw)


class Harness:
    """StrategyManager 의 실제 매도 라우팅 메서드만 바인딩한 경량 인스턴스."""
    submit_retry_liquidation = StrategyManager.submit_retry_liquidation
    has_active_sell          = StrategyManager.has_active_sell
    _register_pending_order  = StrategyManager._register_pending_order
    _handle_sell_filled      = StrategyManager._handle_sell_filled
    _handle_buy_filled       = StrategyManager._handle_buy_filled
    dispatch_fill            = StrategyManager.dispatch_fill

    def __init__(self, api, lifecycle_mgr, pending_registry):
        self.api               = api
        self._lifecycle_mgr    = lifecycle_mgr
        self._pending_registry = pending_registry
        self._pending_sell_meta = {}
        self.pyramid  = FakePyramid()
        self.pnl_guard = FakePnlGuard()
        self.reentry   = FakeReentry()
        self._updater = ExecutionDrivenPositionUpdater(
            on_buy_filled  = self._handle_buy_filled,
            on_sell_filled = self._handle_sell_filled,
        )

    # _handle_sell_filled 이 호출하는 부수 메서드 (로그/재배분) — 무해 stub
    def _log_trade(self, *a, **k):
        pass

    def _try_recycle_to_strong(self, **k):
        pass


class RetryLiquidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p0retry-")
        # PendingOrderRegistry 는 fo._JOURNAL_DB_PATH 사용 → 임시 DB로 격리
        self._orig = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "pending.db")
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
        self.registry = PendingOrderRegistry()   # __init__ 이 pending_orders DDL 생성
        self.lifecycle = OrderLifecycleManager(os.path.join(self.tmp, "lifecycle.db"))

    def tearDown(self):
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
        fo._JOURNAL_DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _harness(self, api_results=None):
        return Harness(FakeApi(api_results), self.lifecycle, self.registry)

    # ── 1. retry 성공 → Lifecycle + PendingRegistry 정상 등록만 ──────────
    def test_retry_success_registers_lifecycle_no_booking(self):
        h = self._harness([dict(ACCEPTED_RESP)])
        res = h.submit_retry_liquidation("005930", "삼성전자", 7, reason="SELL_RETRY")

        self.assertEqual(res["status"], "accepted")
        lc_id = res["lifecycle_id"]
        # Lifecycle 상태 = ACCEPTED (체결 아님)
        lc = h._lifecycle_mgr.load(lc_id)
        self.assertIsNotNone(lc)
        self.assertEqual(lc.current_state, LifecycleState.ORDER_ACCEPTED)
        # PendingRegistry 에 미체결 매도로 등록됨
        self.assertTrue(h._pending_registry.has_active_sell("KR", "005930"))
        # 아직 apply_sell 은 호출되지 않음 (접수 ≠ 체결)
        self.assertEqual(len(h.pyramid.apply_sell_calls), 0)
        # 시장가(ORD_UNPR=0) 로 정확히 1회 주문
        self.assertEqual(len(h.api.sell_calls), 1)
        self.assertEqual(h.api.sell_calls[0][2], 0)

    # ── 2. retry 체결 → apply_sell 정확히 1회 ────────────────────────────
    def test_retry_fill_applies_sell_exactly_once(self):
        h = self._harness([dict(ACCEPTED_RESP)])
        res = h.submit_retry_liquidation("005930", "삼성전자", 7, reason="SELL_RETRY")
        lc_id = res["lifecycle_id"]

        # FillObserver 가 전량 체결 감지 → dispatch_fill (실제 경로)
        h.dispatch_fill(lc_id, filled_qty=7, avg_fill_price=54000.0, is_full=True)

        self.assertEqual(len(h.pyramid.apply_sell_calls), 1)
        code, qty, price, level, is_full = h.pyramid.apply_sell_calls[0]
        self.assertEqual((code, qty, price), ("005930", 7, 54000.0))
        # 체결 후 lifecycle = FILLED
        self.assertEqual(h._lifecycle_mgr.load(lc_id).current_state,
                         LifecycleState.FILLED)
        # reentry 기록도 1회
        self.assertEqual(len(h.reentry.calls), 1)

    # ── 3. FillObserver 선행/중복 체결 → booking 은 1회뿐 (멱등) ──────────
    def test_duplicate_fill_no_double_booking(self):
        h = self._harness([dict(ACCEPTED_RESP)])
        res = h.submit_retry_liquidation("005930", "삼성전자", 7, reason="SELL_RETRY")
        lc_id = res["lifecycle_id"]

        # 동일 주문에 대해 체결 dispatch 가 두 번 들어와도 (중복 관측)
        h.dispatch_fill(lc_id, filled_qty=7, avg_fill_price=54000.0, is_full=True)
        h.dispatch_fill(lc_id, filled_qty=7, avg_fill_price=54000.0, is_full=True)

        # apply_sell 은 정확히 1회만 (full_fill 멱등 보장)
        self.assertEqual(len(h.pyramid.apply_sell_calls), 1)
        self.assertEqual(len(h.reentry.calls), 1)

    # ── 4. watchdog + retry 동시 발생 → 실제 주문 1건만 ──────────────────
    def test_watchdog_and_retry_collision_single_order(self):
        h = self._harness([dict(ACCEPTED_RESP), dict(ACCEPTED_RESP)])
        # 첫 강제청산(예: retry) → 접수 성공 (in-flight 등록)
        r1 = h.submit_retry_liquidation("005930", "삼성전자", 7, reason="SELL_RETRY")
        self.assertEqual(r1["status"], "accepted")
        # 곧바로 두 번째 강제청산(예: watchdog) 시도 → in-flight 가드로 스킵
        r2 = h.submit_retry_liquidation("005930", "삼성전자", 7, reason="Watchdog W2")
        self.assertEqual(r2["status"], "skipped_inflight")

        # 실제 api.sell 은 딱 1회만 나감 (중복 매도 방지)
        self.assertEqual(len(h.api.sell_calls), 1)

    # ── 보강: rt_cd != 0 이면 lifecycle/pending 등록 없이 rejected ────────
    def test_rejected_order_not_registered(self):
        h = self._harness([dict(REJECT_RESP)])
        res = h.submit_retry_liquidation("005930", "삼성전자", 7, reason="SELL_RETRY")
        self.assertEqual(res["status"], "rejected")
        self.assertFalse(h._pending_registry.has_active_sell("KR", "005930"))
        self.assertEqual(len(h.pyramid.apply_sell_calls), 0)


if __name__ == "__main__":
    unittest.main()
