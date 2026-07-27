"""P0-4a 늦은 FILLED 복구 — EXPIRED 이후 실체결 발견 시 회계 무손실 보장.

- fill-aware expiry: open book 에서 사라진 주문도 체결조회로 실체결이면 booking.
- recover_expired_fills: EXPIRED 로 마킹된 뒤 늦게 체결이 확인되면 정확히 1회 복구.
- idempotent: 복구는 1회만, 재실행 no-op.
- 통합: restart → EXPIRED → 늦은 체결조회 → 복구 → 4개 저장소 일관 + apply_sell 1회.
"""
import os
import sys
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import journal.fill_observer as fo  # noqa: E402
from journal.fill_observer import PendingOrderRegistry, PendingStatus  # noqa: E402
from phoenix.lifecycle import OrderLifecycleManager, LifecycleState  # noqa: E402
from phoenix.execution_driven import ExecutionDrivenPositionUpdater  # noqa: E402
from strategies.strategy_manager import StrategyManager  # noqa: E402


class FakeApi:
    def __init__(self, open_result, ccld=None):
        self._open = open_result       # (ok, orders)
        self._ccld = dict(ccld or {})  # odno -> raw ccld dict

    def get_open_orders_checked(self, order_type="BUY"):
        return self._open

    def get_kr_ccld_by_odno(self, odno="", code=""):
        return self._ccld.get(odno, {})

    def set_ccld(self, odno, cum, avg, order_qty=7):
        self._ccld[odno] = {"cum_filled_qty": cum, "avg_fill_price": avg,
                            "order_qty": order_qty}


class FakePyramid:
    def __init__(self):
        self.apply_sell_calls = []
        self.compound_pool = 0
    def apply_sell(self, code, qty, price, level=None, is_full=True):
        self.apply_sell_calls.append((code, qty, price))
        return {"net_profit_pct": 1.0, "net_profit": 1000.0}


class FakePnl:
    can_buy = False
    def record(self, amt): pass
    def status_dict(self):
        return {"realized_pnl": 0.0, "peak_pnl": 0.0, "state": "TRADING"}


class FakeReentry:
    def __init__(self): self.calls = []
    def record_sell(self, **kw): self.calls.append(kw)


class Flow:
    reconcile_stale_pendings = StrategyManager.reconcile_stale_pendings
    _resolve_gone_order      = StrategyManager._resolve_gone_order
    _probe_fill_evidence     = StrategyManager._probe_fill_evidence
    _expire_stale_pending    = StrategyManager._expire_stale_pending
    _ensure_min_meta         = StrategyManager._ensure_min_meta
    recover_expired_fills    = StrategyManager.recover_expired_fills
    reconcile_execution_state = StrategyManager.reconcile_execution_state
    _recovery_window         = StrategyManager._recovery_window
    _prev_business_day       = StrategyManager._prev_business_day
    dispatch_fill            = StrategyManager.dispatch_fill
    _handle_sell_filled      = StrategyManager._handle_sell_filled
    _handle_buy_filled       = StrategyManager._handle_buy_filled
    STALE_PENDING_SEC        = StrategyManager.STALE_PENDING_SEC

    def __init__(self, api, mgr, reg):
        self.api = api
        self._lifecycle_mgr = mgr
        self._pending_registry = reg
        self._fill_observer = None
        self.pyramid = FakePyramid()
        self.pnl_guard = FakePnl()
        self.reentry = FakeReentry()
        self._pending_sell_meta = {}
        self._pending_buy_meta = {}
        self._updater = ExecutionDrivenPositionUpdater(
            on_buy_filled=self._handle_buy_filled,
            on_sell_filled=self._handle_sell_filled,
        )
    def run_fill_poll(self): return {}
    def _log_trade(self, *a, **k): pass
    def _try_recycle_to_strong(self, **k): pass


class ExpiredFillRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p04a-")
        self._orig = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "pending.db")
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
        self.reg = PendingOrderRegistry()
        self.mgr = OrderLifecycleManager(os.path.join(self.tmp, "lifecycle.db"))

    def tearDown(self):
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
        fo._JOURNAL_DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stale_sell(self, code, qty, odno, minutes=10):
        lc = self.mgr.create(trade_id="grp", market="KR", code=code,
                             side="SELL", order_qty=qty)
        lc_id = lc.order_lifecycle_id
        self.mgr.confirm_signal(lc)
        self.mgr.submit(lc)
        self.mgr.accept(lc, odno=odno)
        old = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        self.reg.register("KR", lc_id, code, "SELL", qty, old, odno=odno)
        return lc_id

    # ── fill-aware expiry: 사라진 주문이 실제로는 체결됨 → booking(EXPIRE 안 함) ─
    def test_gone_order_actually_filled_is_booked(self):
        lc_id = self._stale_sell("005930", 7, "OD1")
        api = FakeApi((True, []),
                      ccld={"OD1": {"cum_filled_qty": 7, "avg_fill_price": 54000.0,
                                    "order_qty": 7}})
        flow = Flow(api, self.mgr, self.reg)
        rep = flow.reconcile_stale_pendings(poll_first=False)

        self.assertEqual(rep["booked"], [lc_id])
        self.assertEqual(rep["expired"], [])
        self.assertEqual(self.mgr.load(lc_id).current_state, LifecycleState.FILLED)
        self.assertEqual(self.reg.get_by_trade_id(lc_id)["status"],
                         PendingStatus.FILLED)
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 1)   # 실체결 1회 booking

    # ── 체결 증거 없음 → EXPIRED ─────────────────────────────────────────
    def test_gone_order_no_fill_is_expired(self):
        lc_id = self._stale_sell("005930", 7, "OD1")
        flow = Flow(FakeApi((True, []), ccld={}), self.mgr, self.reg)  # ccld 없음
        rep = flow.reconcile_stale_pendings(poll_first=False)
        self.assertEqual(rep["expired"], [lc_id])
        self.assertEqual(rep["booked"], [])
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 0)

    # ── recover: EXPIRED 이후 늦은 체결 발견 → 복구(정확히 1회) ────────────
    def test_recover_late_fill_after_expired(self):
        lc_id = self._stale_sell("005930", 7, "OD1")
        api = FakeApi((True, []), ccld={})            # 최초엔 체결기록 없음
        flow = Flow(api, self.mgr, self.reg)
        flow.reconcile_stale_pendings(poll_first=False)     # → EXPIRED
        self.assertEqual(self.mgr.load(lc_id).current_state, LifecycleState.EXPIRED)

        # 이후 KIS 체결조회에 실체결이 나타남(늦은 FILLED)
        api.set_ccld("OD1", cum=7, avg=54000.0)
        rep = flow.recover_expired_fills()

        self.assertEqual(rep["recovered"], [lc_id])
        # 4개 저장소 정정: lifecycle/pending FILLED, apply_sell 1회, PnL 기록 1회
        self.assertEqual(self.mgr.load(lc_id).current_state, LifecycleState.FILLED)
        self.assertEqual(self.reg.get_by_trade_id(lc_id)["status"],
                         PendingStatus.FILLED)
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 1)
        self.assertEqual(len(flow.reentry.calls), 1)

    # ── recover idempotent: 두 번 실행해도 booking 1회 ───────────────────
    def test_recover_idempotent(self):
        lc_id = self._stale_sell("005930", 7, "OD1")
        api = FakeApi((True, []), ccld={})
        flow = Flow(api, self.mgr, self.reg)
        flow.reconcile_stale_pendings(poll_first=False)   # EXPIRED
        api.set_ccld("OD1", cum=7, avg=54000.0)

        rep1 = flow.recover_expired_fills()
        rep2 = flow.recover_expired_fills()
        self.assertEqual(rep1["recovered"], [lc_id])
        self.assertEqual(rep2["recovered"], [])           # 2회차 no-op
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 1)   # booking 1회뿐

    # ── recover: EXPIRED 인데 실제로도 미체결 → 복구 안 함(EXPIRED 유지) ───
    def test_recover_skips_genuinely_unfilled(self):
        lc_id = self._stale_sell("005930", 7, "OD1")
        flow = Flow(FakeApi((True, []), ccld={}), self.mgr, self.reg)
        flow.reconcile_stale_pendings(poll_first=False)   # EXPIRED
        rep = flow.recover_expired_fills()                # ccld 여전히 없음
        self.assertEqual(rep["recovered"], [])
        self.assertEqual(self.mgr.load(lc_id).current_state, LifecycleState.EXPIRED)
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 0)

    # ── 통합: restart → EXPIRED → 늦은 체결조회 → 복구 → 일관성 ──────────
    def test_restart_expired_then_recovered_integration(self):
        # 세션1: 접수(stale) — 별도 매니저
        lc_id = self._stale_sell("005930", 7, "OD1")

        # restart: 동일 DB 로 새 매니저/레지스트리
        mgr2 = OrderLifecycleManager(os.path.join(self.tmp, "lifecycle.db"))
        reg2 = PendingOrderRegistry()
        api = FakeApi((True, []), ccld={})     # 재시작 시엔 체결기록 아직 없음
        flow = Flow(api, mgr2, reg2)

        # (3) stale reconcile → 체결 미확인 → EXPIRED
        flow.reconcile_stale_pendings(poll_first=False)
        self.assertEqual(mgr2.load(lc_id).current_state, LifecycleState.EXPIRED)
        self.assertFalse(reg2.has_active_sell("KR", "005930"))

        # (4) 이후 KIS 체결조회에 실체결 확인됨 → recover
        api.set_ccld("OD1", cum=7, avg=54000.0)
        rep = flow.recover_expired_fills()

        # 4개 저장소 최종 일관성
        self.assertEqual(rep["recovered"], [lc_id])
        self.assertEqual(mgr2.load(lc_id).current_state, LifecycleState.FILLED)
        self.assertEqual(reg2.get_by_trade_id(lc_id)["status"], PendingStatus.FILLED)
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 1)   # PnL 무손실
        self.assertEqual(len(flow.reentry.calls), 1)


if __name__ == "__main__":
    unittest.main()
