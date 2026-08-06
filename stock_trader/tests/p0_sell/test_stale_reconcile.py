"""P0-4 stale pending 자동 reconcile — 보수적 정리 동작 테스트.

실제 StrategyManager.reconcile_stale_pendings / _expire_stale_pending 를
실제 OrderLifecycleManager + PendingOrderRegistry 로 실행한다(임시 DB).
poll_first=False 로 호출해 KIS fills 조회 없이 stale 판정 경로만 검증한다.
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
from phoenix.lifecycle import (  # noqa: E402
    OrderLifecycleManager, LifecycleState, make_order_lifecycle_id,
)
from strategies.strategy_manager import StrategyManager  # noqa: E402


class FakeApi:
    """get_open_orders_checked 만 제공. open=(ok, orders) 또는 callable."""
    def __init__(self, open_result):
        self._open = open_result
        self.sell_calls = []

    def get_open_orders_checked(self, order_type="BUY"):
        if callable(self._open):
            return self._open(order_type)
        return self._open

    def sell(self, code, qty, price, ord_dvsn=None):
        self.sell_calls.append((code, qty, price))
        return {"rt_cd": "0", "output": {"KNO_ORD_NO": "NEW"}}


def open_order(odno, code, side_cd="01"):
    return {"order_no": odno, "stock_code": code, "sll_buy_dvsn_cd": side_cd}


class Flow:
    reconcile_stale_pendings = StrategyManager.reconcile_stale_pendings
    _expire_stale_pending    = StrategyManager._expire_stale_pending
    _resolve_gone_order      = StrategyManager._resolve_gone_order
    _probe_fill_evidence     = StrategyManager._probe_fill_evidence
    _ensure_min_meta         = StrategyManager._ensure_min_meta
    STALE_PENDING_SEC        = StrategyManager.STALE_PENDING_SEC

    def __init__(self, api, mgr, reg):
        self.api = api
        self._lifecycle_mgr = mgr
        self._pending_registry = reg
        self._fill_observer = None      # poll_first=False 로만 호출
        self._pending_sell_meta = {}
        self._pending_buy_meta = {}

    def run_fill_poll(self):            # poll_first=False 라 호출 안 됨(방어용)
        return {}


class StaleReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p04recon-")
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
        # 실제 코드와 동일하게 pending.trade_id = lifecycle.order_lifecycle_id 로 키를 맞춘다.
        lc = self.mgr.create(trade_id="grp", market="KR", code=code,
                             side="SELL", order_qty=qty)
        lc_id = lc.order_lifecycle_id
        self.mgr.confirm_signal(lc)
        self.mgr.submit(lc)
        self.mgr.accept(lc, odno=odno)
        old = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        self.reg.register("KR", lc_id, code, "SELL", qty, old, odno=odno)
        return lc_id

    # ── stale pending cleanup: odno 사라짐 → EXPIRED(회계 무개입) ─────────
    def test_stale_expired_when_gone(self):
        lc_id = self._stale_sell("005930", 7, "OD1")
        flow = Flow(FakeApi((True, [])), self.mgr, self.reg)   # open orders 비어있음
        rep = flow.reconcile_stale_pendings(poll_first=False)

        self.assertEqual(rep["expired"], [lc_id])
        self.assertEqual(self.mgr.load(lc_id).current_state, LifecycleState.EXPIRED)
        self.assertFalse(self.reg.has_active_sell("KR", "005930"))
        self.assertEqual(self.reg.get_by_trade_id(lc_id)["status"],
                         PendingStatus.EXPIRED)

    # ── API 실패 → 절대 해제하지 않음(ACTIVE 유지) ───────────────────────
    def test_api_failure_keeps_active(self):
        lc_id = self._stale_sell("005930", 7, "OD1")
        flow = Flow(FakeApi((False, [])), self.mgr, self.reg)   # 조회 실패
        rep = flow.reconcile_stale_pendings(poll_first=False)

        self.assertEqual(rep["expired"], [])
        self.assertIn(lc_id, rep["kept_unverified"])
        self.assertIn("KR", rep["api_failed_markets"])
        self.assertTrue(self.reg.has_active_sell("KR", "005930"))   # 유지
        self.assertFalse(self.mgr.load(lc_id).is_terminal)

    # ── odno 아직 거래소에 살아있음 → 유지 ───────────────────────────────
    def test_still_open_keeps_live(self):
        lc_id = self._stale_sell("005930", 7, "OD1")
        flow = Flow(FakeApi((True, [open_order("OD1", "005930")])),
                    self.mgr, self.reg)
        rep = flow.reconcile_stale_pendings(poll_first=False)

        self.assertEqual(rep["expired"], [])
        self.assertIn(lc_id, rep["kept_live"])
        self.assertTrue(self.reg.has_active_sell("KR", "005930"))

    # ── duplicate reconcile: 두 번 실행해도 결과 동일(idempotent) ─────────
    def test_duplicate_reconcile_idempotent(self):
        lc_id = self._stale_sell("005930", 7, "OD1")
        flow = Flow(FakeApi((True, [])), self.mgr, self.reg)
        rep1 = flow.reconcile_stale_pendings(poll_first=False)
        self.assertEqual(rep1["expired"], [lc_id])
        # 2회차: 이미 EXPIRED → 후보 없음
        rep2 = flow.reconcile_stale_pendings(poll_first=False)
        self.assertEqual(rep2["candidates"], 0)
        self.assertEqual(rep2["expired"], [])
        self.assertEqual(self.mgr.load(lc_id).current_state, LifecycleState.EXPIRED)

    # ── FillObserver 선행 체결과 동시 reconcile: 체결건은 EXPIRE 안 함 ─────
    def test_filled_order_not_expired_by_reconcile(self):
        lc_id = self._stale_sell("005930", 7, "OD1")
        # FillObserver 가 먼저 전량 체결 반영(상태 FILLED)
        lc = self.mgr.load(lc_id)
        self.mgr.full_fill(lc, delta=7, avg_price=54000.0)   # lifecycle FILLED
        self.reg.update_fill(lc_id, 7, PendingStatus.FILLED)  # pending FILLED

        flow = Flow(FakeApi((True, [])), self.mgr, self.reg)  # 체결되어 open book 에 없음
        rep = flow.reconcile_stale_pendings(poll_first=False)

        # 체결건은 stale 후보가 아니므로 EXPIRE 되지 않음
        self.assertEqual(rep["candidates"], 0)
        self.assertEqual(rep["expired"], [])
        self.assertEqual(self.mgr.load(lc_id).current_state, LifecycleState.FILLED)
        self.assertEqual(self.reg.get_by_trade_id(lc_id)["status"],
                         PendingStatus.FILLED)

    # ── odno 없으면 PendingRegistry 등록 거부(오인 매칭 방지, P0-5) ────────
    def test_no_odno_registration_rejected(self):
        lc = self.mgr.create(trade_id="grp", market="KR", code="005930",
                             side="SELL", order_qty=7)
        lc_id = lc.order_lifecycle_id
        self.mgr.confirm_signal(lc)
        self.mgr.submit(lc)
        self.mgr.accept(lc)   # odno 없음
        old = (datetime.now() - timedelta(minutes=10)).isoformat()
        # P0-5: odno 가 비면 등록을 거부한다(return 0, 행 미생성).
        rowid = self.reg.register("KR", lc_id, "005930", "SELL", 7, old, odno="")
        self.assertEqual(rowid, 0)
        # 행 자체가 없으므로 어떤 pending 목록에도 나타나지 않는다.
        self.assertIsNone(self.reg.get_by_trade_id(lc_id))
        self.assertFalse(self.reg.has_active_sell("KR", "005930"))
        # reconcile 도 후보가 없어 아무 것도 EXPIRE 하지 않는다.
        flow = Flow(FakeApi((True, [])), self.mgr, self.reg)
        rep = flow.reconcile_stale_pendings(poll_first=False)
        self.assertEqual(rep["expired"], [])
        self.assertNotIn(lc_id, rep["kept_unverified"])


if __name__ == "__main__":
    unittest.main()
