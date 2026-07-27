"""P0-4a 추가 검증 — 최종 커밋 전 4가지 보강.

2. 늦은 BUY 체결 복구
3. 부분체결 후 누적체결 증가 시 '차액 수량만' 추가 booking(중복 카운트 없음)
4. Lifecycle↔PendingRegistry 저장 단계별 강제 실패(F1/F2) 후 재시작해도
   중복 booking / 체결 누락이 없음 (fault-injection)
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
    def __init__(self, open_result=(True, []), ccld=None):
        self._open = open_result
        self._ccld = dict(ccld or {})
    def get_open_orders_checked(self, order_type="BUY"):
        return self._open
    def get_kr_ccld_by_odno(self, odno="", code="", start_date="", end_date=""):
        return self._ccld.get(odno, {})
    def set_ccld(self, odno, cum, avg, order_qty=7):
        self._ccld[odno] = {"cum_filled_qty": cum, "avg_fill_price": avg,
                            "order_qty": order_qty}


class FakePyramid:
    def __init__(self):
        self.apply_sell_calls = []
        self.apply_buy_calls = []
        self.compound_pool = 0
        self.positions = {}
    def apply_sell(self, code, qty, price, level=None, is_full=True):
        self.apply_sell_calls.append((code, qty, price))
        return {"net_profit_pct": 1.0, "net_profit": 1000.0}
    def apply_buy(self, code, name, level, qty, price, using_compound=0,
                  is_full_add=False):
        self.apply_buy_calls.append((code, qty, price))
    def _save(self): pass


class FakePnl:
    can_buy = False
    realized_pnl = 0.0
    state = "TRADING"
    def record(self, amt): pass
    def status_dict(self):
        return {"realized_pnl": 0.0, "peak_pnl": 0.0, "state": "TRADING"}


class FakeReentry:
    def __init__(self): self.calls = []
    def record_sell(self, **kw): self.calls.append(kw)


class Flow:
    reconcile_stale_pendings  = StrategyManager.reconcile_stale_pendings
    reconcile_execution_state = StrategyManager.reconcile_execution_state
    recover_expired_fills     = StrategyManager.recover_expired_fills
    _resolve_gone_order       = StrategyManager._resolve_gone_order
    _expire_stale_pending     = StrategyManager._expire_stale_pending
    _probe_fill_evidence      = StrategyManager._probe_fill_evidence
    _ensure_min_meta          = StrategyManager._ensure_min_meta
    _recovery_window          = StrategyManager._recovery_window
    _prev_business_day        = StrategyManager._prev_business_day
    dispatch_fill             = StrategyManager.dispatch_fill
    _handle_sell_filled       = StrategyManager._handle_sell_filled
    _handle_buy_filled        = StrategyManager._handle_buy_filled
    STALE_PENDING_SEC         = StrategyManager.STALE_PENDING_SEC

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


class P04aHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p04ah-")
        self._orig = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "pending.db")
        if getattr(fo._local, "fo_conn", None) is not None:
            try: fo._local.fo_conn.close()
            except Exception: pass
            fo._local.fo_conn = None
        self.reg = PendingOrderRegistry()
        self.mgr = OrderLifecycleManager(os.path.join(self.tmp, "lifecycle.db"))

    def tearDown(self):
        if getattr(fo._local, "fo_conn", None) is not None:
            try: fo._local.fo_conn.close()
            except Exception: pass
            fo._local.fo_conn = None
        fo._JOURNAL_DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _accept(self, side, code, qty, odno, minutes=10):
        lc = self.mgr.create(trade_id="grp", market="KR", code=code,
                             side=side, order_qty=qty)
        lc_id = lc.order_lifecycle_id
        self.mgr.confirm_signal(lc)
        self.mgr.submit(lc)
        self.mgr.accept(lc, odno=odno)
        old = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        self.reg.register("KR", lc_id, code, side, qty, old, odno=odno)
        return lc_id

    # ── 2. 늦은 BUY 체결 복구 ────────────────────────────────────────────
    def test_late_buy_fill_recovered(self):
        lc_id = self._accept("BUY", "005930", 10, "OB1")
        api = FakeApi((True, []), ccld={})
        flow = Flow(api, self.mgr, self.reg)
        flow.reconcile_stale_pendings(poll_first=False)      # → EXPIRED (no fill)
        self.assertEqual(self.mgr.load(lc_id).current_state, LifecycleState.EXPIRED)

        api.set_ccld("OB1", cum=10, avg=180000.0, order_qty=10)  # 늦은 BUY 체결
        rep = flow.recover_expired_fills()

        self.assertEqual(rep["recovered"], [lc_id])
        self.assertEqual(self.mgr.load(lc_id).current_state, LifecycleState.FILLED)
        self.assertEqual(self.reg.get_by_trade_id(lc_id)["status"],
                         PendingStatus.FILLED)
        self.assertEqual(len(flow.pyramid.apply_buy_calls), 1)        # BUY booking 1회
        self.assertEqual(flow.pyramid.apply_buy_calls[0], ("005930", 10, 180000.0))

    # ── 3. 부분체결 후 누적 증가 → 차액 수량만 추가 booking(중복 없음) ────
    def test_partial_then_cumulative_delta_only(self):
        lc_id = self._accept("SELL", "005930", 7, "OD1")
        # 부분체결 3주 관측 (watermark=3)
        self.dispatch_partial(lc_id, 3, 54000.0)
        self.assertEqual(self.mgr.load(lc_id).current_state,
                         LifecycleState.PARTIALLY_FILLED)
        self.assertEqual(self.mgr.load(lc_id).filled_qty, 3)

        # 이후 주문이 open book 에서 사라지고 체결조회 누적=7 → 차액 4만 추가
        api = FakeApi((True, []),
                      ccld={"OD1": {"cum_filled_qty": 7, "avg_fill_price": 54000.0,
                                    "order_qty": 7}})
        flow = Flow(api, self.mgr, self.reg)
        rep = flow.reconcile_stale_pendings(poll_first=False)

        self.assertEqual(rep["booked"], [lc_id])
        lc = self.mgr.load(lc_id)
        self.assertEqual(lc.current_state, LifecycleState.FILLED)
        self.assertEqual(lc.filled_qty, 7)          # 3+4=7 (10 아님 — 차액만)
        # apply_sell 은 최종 1회, 총 체결수량 7주로 booking
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 1)
        self.assertEqual(flow.pyramid.apply_sell_calls[0][1], 7)

    def dispatch_partial(self, lc_id, cum, avg):
        """부분체결 관측을 재현: lifecycle partial_fill + pending update_fill."""
        lc = self.mgr.load(lc_id)
        self.mgr.partial_fill(lc, cum, avg)
        self.reg.update_fill(lc_id, cum, PendingStatus.PARTIALLY_FILLED)

    # ── 4-F1: lifecycle FILLED(booked) & pending ACCEPTED(크래시) → 중복 없음 ─
    def test_faultinject_F1_lifecycle_filled_pending_stale(self):
        lc_id = self._accept("SELL", "005930", 7, "OD1", minutes=0)
        flow = Flow(FakeApi(), self.mgr, self.reg)
        # 정상 체결 booking (세션1): lifecycle FILLED + apply_sell 1회
        self._ensure_meta_sell(flow, lc_id)
        lc = self.mgr.load(lc_id)
        self.mgr.full_fill(lc, delta=7, avg_price=54000.0, on_filled=flow._updater)
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 1)
        # ★ 크래시 시뮬레이션: pending 을 FILLED 로 갱신하기 전에 죽음 → ACCEPTED 잔존
        self.assertEqual(self.reg.get_by_trade_id(lc_id)["status"],
                         PendingStatus.ACCEPTED)

        # restart → reconcile_execution_state: F1 수렴(재-booking 없음)
        flow2 = Flow(FakeApi(), self.mgr, self.reg)
        rep = flow2.reconcile_execution_state()
        self.assertIn(lc_id, rep["f1_synced"])
        self.assertEqual(self.reg.get_by_trade_id(lc_id)["status"],
                         PendingStatus.FILLED)
        self.assertEqual(len(flow2.pyramid.apply_sell_calls), 0)   # 재-booking 없음
        # idempotent: 재실행해도 변화 없음
        rep2 = flow2.reconcile_execution_state()
        self.assertEqual(rep2["f1_synced"], [])

    # ── 4-F2: pending FILLED & lifecycle ACCEPTED(크래시) → 정확히 1회 booking ─
    def test_faultinject_F2_pending_filled_lifecycle_unbooked(self):
        lc_id = self._accept("SELL", "005930", 7, "OD1", minutes=0)
        # ★ 크래시 시뮬레이션: pending 은 FILLED 됐지만 lifecycle booking 전에 죽음
        self.reg.update_fill(lc_id, 7, PendingStatus.FILLED)
        self.assertEqual(self.mgr.load(lc_id).current_state,
                         LifecycleState.ORDER_ACCEPTED)   # 아직 미booking

        # restart → reconcile_execution_state: F2 미완료 booking 완료(체결가는 ccld)
        api = FakeApi((True, []),
                      ccld={"OD1": {"cum_filled_qty": 7, "avg_fill_price": 54000.0,
                                    "order_qty": 7}})
        flow = Flow(api, self.mgr, self.reg)
        rep = flow.reconcile_execution_state()

        self.assertIn(lc_id, rep["f2_booked"])
        self.assertEqual(self.mgr.load(lc_id).current_state, LifecycleState.FILLED)
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 1)     # 정확히 1회
        self.assertEqual(len(flow.reentry.calls), 1)
        # idempotent: 재실행 → 재-booking 없음
        rep2 = flow.reconcile_execution_state()
        self.assertEqual(rep2["f2_booked"], [])
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 1)

    # ── 4-F2 잔여리스크: 체결가 미확보 시 가짜 booking 금지(누락으로 로깅) ──
    def test_faultinject_F2_unbookable_when_no_price(self):
        lc_id = self._accept("SELL", "005930", 7, "OD1", minutes=0)
        self.reg.update_fill(lc_id, 7, PendingStatus.FILLED)   # pending FILLED
        # ccld 도 lifecycle 도 체결가 없음 → 가짜 가격 booking 금지
        flow = Flow(FakeApi((True, []), ccld={}), self.mgr, self.reg)
        rep = flow.reconcile_execution_state()
        self.assertIn(lc_id, rep["f2_unbookable"])
        self.assertEqual(len(flow.pyramid.apply_sell_calls), 0)   # 가짜 booking 없음
        self.assertNotEqual(self.mgr.load(lc_id).current_state,
                            LifecycleState.FILLED)

    def _ensure_meta_sell(self, flow, lc_id):
        flow._pending_sell_meta[lc_id] = {
            "name": "삼성전자", "qty": 7, "price": 0.0, "level": None,
            "is_full": True, "reason": "test", "is_forced": True,
        }


if __name__ == "__main__":
    unittest.main()
