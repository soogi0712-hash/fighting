"""P0-4 통합 테스트 — restart 후 4개 저장소 일관성.

Lifecycle(lifecycle_orders) · PendingRegistry(pending_orders) ·
FillObserver(관측→dispatch) · PyramidPosition(pyramid_positions) 가
restart 후 문서화된 4단계 reconcile 순서를 거쳐 일관된 상태에 도달함을 검증한다.

  순서: (1) load_all_active→meta 복원 → (2) 체결 booking(run_fill_poll 대체:
         dispatch_fill) → (3) reconcile_stale_pendings → (4) 잔고 기준 포지션 정합화

두 시나리오:
  A. 다운타임 중 체결됨      → FILLED + 포지션 제거 + apply_sell 1회
  B. 다운타임 중 주문 소멸(미체결) → EXPIRED(회계 무개입) + 포지션 보존 + apply_sell 0회
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
import strategies.pyramid_strategy as ps  # noqa: E402
from journal.fill_observer import PendingOrderRegistry, PendingStatus  # noqa: E402
from phoenix.lifecycle import OrderLifecycleManager, LifecycleState  # noqa: E402
from phoenix.execution_driven import ExecutionDrivenPositionUpdater  # noqa: E402
from strategies.pyramid_strategy import PyramidStrategyManager  # noqa: E402
from strategies.strategy_manager import StrategyManager  # noqa: E402


class FakeApi:
    def __init__(self, open_result, balance):
        self._open = open_result
        self._balance = balance

    def get_open_orders_checked(self, order_type="BUY"):
        return self._open

    def get_balance(self):
        return self._balance


class FakePnl:
    can_buy = False
    def record(self, amt): pass
    def status_dict(self):
        return {"realized_pnl": 0.0, "peak_pnl": 0.0, "state": "TRADING"}


class FakeReentry:
    def __init__(self): self.calls = []
    def record_sell(self, **kw): self.calls.append(kw)


class Node:
    """restart 후의 StrategyManager 를 대체하는 경량 노드(실제 메서드 바인딩)."""
    _restore_pending_meta_from_lifecycle = StrategyManager._restore_pending_meta_from_lifecycle
    dispatch_fill            = StrategyManager.dispatch_fill
    _handle_sell_filled      = StrategyManager._handle_sell_filled
    _handle_buy_filled       = StrategyManager._handle_buy_filled
    reconcile_stale_pendings = StrategyManager.reconcile_stale_pendings
    _expire_stale_pending    = StrategyManager._expire_stale_pending
    _resolve_gone_order      = StrategyManager._resolve_gone_order
    _probe_fill_evidence     = StrategyManager._probe_fill_evidence
    _ensure_min_meta         = StrategyManager._ensure_min_meta
    active_order_codes       = StrategyManager.active_order_codes
    STALE_PENDING_SEC        = StrategyManager.STALE_PENDING_SEC

    def __init__(self, api, mgr, reg, pyramid):
        self.api = api
        self._lifecycle_mgr = mgr
        self._pending_registry = reg
        self._fill_observer = None
        self.pyramid = pyramid
        self.pnl_guard = FakePnl()
        self.reentry = FakeReentry()
        self._pending_buy_meta = {}
        self._pending_sell_meta = {}
        self._updater = ExecutionDrivenPositionUpdater(
            on_buy_filled=self._handle_buy_filled,
            on_sell_filled=self._handle_sell_filled,
        )

    def _log_trade(self, *a, **k): pass
    def _try_recycle_to_strong(self, **k): pass

    def sync_positions(self):
        """app._sync_positions_from_balance 의 핵심(P0-3 reconcile) 재현."""
        holdings = self.api.get_balance().get("holdings", [])
        broker = {h["code"]: {"qty": int(h.get("qty", 0)),
                              "avg_price": float(h.get("avg_price", 0)),
                              "name": h.get("name", h["code"])}
                  for h in holdings if h.get("code")}
        return self.pyramid.reconcile_from_broker(
            broker, self.active_order_codes("KR"))


class P04IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p04int-")
        self._orig_j = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "pending.db")
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
        self._orig_py, self._orig_cp = ps.PYRAMID_FILE, ps.COMPOUND_FILE
        ps.PYRAMID_FILE = os.path.join(self.tmp, "pyramid.json")
        ps.COMPOUND_FILE = os.path.join(self.tmp, "compound.json")
        self.lc_db = os.path.join(self.tmp, "lifecycle.db")

    def tearDown(self):
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
        fo._JOURNAL_DB_PATH = self._orig_j
        ps.PYRAMID_FILE, ps.COMPOUND_FILE = self._orig_py, self._orig_cp
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session1_accept_sell(self, submitted_minutes_ago=0):
        """세션1: SELL 접수(lifecycle ACCEPTED + pending ACCEPTED + pyramid 보유)."""
        mgr = OrderLifecycleManager(self.lc_db)
        reg = PendingOrderRegistry()
        pyramid = PyramidStrategyManager(None, max_per_stock=1e9, max_total=1e9)
        pyramid.positions["005930"] = pyramid._build_reconciled_position(
            "005930", "삼성전자", 7, 50000)
        pyramid._save()

        lc = mgr.create(trade_id="grp", market="KR", code="005930",
                        side="SELL", order_qty=7)
        lc_id = lc.order_lifecycle_id
        mgr.confirm_signal(lc)
        mgr.submit(lc)
        mgr.accept(lc, odno="OD1")
        sub = (datetime.now() - timedelta(minutes=submitted_minutes_ago)).isoformat()
        reg.register("KR", lc_id, "005930", "SELL", 7, sub, odno="OD1")
        return lc_id

    # ── 시나리오 A: 다운타임 중 체결됨 → FILLED + 포지션 제거 ─────────────
    def test_restart_filled_during_downtime(self):
        lc_id = self._session1_accept_sell(submitted_minutes_ago=1)

        # ===== restart: 새 매니저/레지스트리/피라미드 (동일 DB/파일) =====
        mgr = OrderLifecycleManager(self.lc_db)
        reg = PendingOrderRegistry()
        pyramid = PyramidStrategyManager(None, max_per_stock=1e9, max_total=1e9)
        # 잔고: 체결되어 005930 없음
        api = FakeApi((True, []), {"holdings": []})
        node = Node(api, mgr, reg, pyramid)

        # (1) meta 복원
        node._restore_pending_meta_from_lifecycle()
        self.assertIn(lc_id, node._pending_sell_meta)

        # (2) 체결 booking (run_fill_poll 대체): FillObserver 가 전량체결 관측
        reg.update_fill(lc_id, 7, PendingStatus.FILLED)     # pending FILLED
        node.dispatch_fill(lc_id, filled_qty=7, avg_fill_price=54000.0,
                           is_full=True)                    # lifecycle FILLED + booking

        # (3) stale reconcile — 체결건은 후보 아님
        rep = node.reconcile_stale_pendings(poll_first=False)
        self.assertEqual(rep["expired"], [])

        # (4) 잔고 정합화
        node.sync_positions()

        # ===== 4개 저장소 일관성 =====
        self.assertEqual(mgr.load(lc_id).current_state, LifecycleState.FILLED)
        self.assertEqual(reg.get_by_trade_id(lc_id)["status"], PendingStatus.FILLED)
        self.assertNotIn("005930", pyramid.positions)       # 체결로 제거
        self.assertEqual(len(node.reentry.calls), 1)        # apply_sell 회계 1회
        self.assertFalse(reg.has_active_sell("KR", "005930"))

    # ── 시나리오 B: 다운타임 중 주문 소멸(미체결) → EXPIRED + 포지션 보존 ─
    def test_restart_stale_expired_position_preserved(self):
        lc_id = self._session1_accept_sell(submitted_minutes_ago=10)   # stale

        # ===== restart =====
        mgr = OrderLifecycleManager(self.lc_db)
        reg = PendingOrderRegistry()
        pyramid = PyramidStrategyManager(None, max_per_stock=1e9, max_total=1e9)
        # 잔고: 미체결이라 005930 7주 그대로 보유(주문만 거래소에서 소멸)
        balance = {"holdings": [{"code": "005930", "name": "삼성전자",
                                 "qty": 7, "avg_price": 50000}]}
        api = FakeApi((True, []), balance)   # open orders 비어있음(주문 소멸)
        node = Node(api, mgr, reg, pyramid)

        # (1) meta 복원
        node._restore_pending_meta_from_lifecycle()
        # (2) 체결 없음(run_fill_poll 결과 no-op)
        # (3) stale reconcile → odno 소멸 확인 → EXPIRED(회계 무개입)
        rep = node.reconcile_stale_pendings(poll_first=False)
        self.assertEqual(rep["expired"], [lc_id])
        # (4) 잔고 정합화
        node.sync_positions()

        # ===== 4개 저장소 일관성 =====
        self.assertEqual(mgr.load(lc_id).current_state, LifecycleState.EXPIRED)
        self.assertEqual(reg.get_by_trade_id(lc_id)["status"], PendingStatus.EXPIRED)
        self.assertIn("005930", pyramid.positions)          # 미체결 → 포지션 보존
        self.assertEqual(pyramid.positions["005930"].total_qty, 7)
        self.assertEqual(len(node.reentry.calls), 0)        # 가짜 PnL 없음(booking 0회)
        self.assertFalse(reg.has_active_sell("KR", "005930"))  # ACTIVE 해제됨

    # ── restart 후 동일 reconcile 2회 → 결과 동일(idempotent) ────────────
    def test_restart_reconcile_idempotent(self):
        lc_id = self._session1_accept_sell(submitted_minutes_ago=10)
        mgr = OrderLifecycleManager(self.lc_db)
        reg = PendingOrderRegistry()
        pyramid = PyramidStrategyManager(None, max_per_stock=1e9, max_total=1e9)
        balance = {"holdings": [{"code": "005930", "name": "삼성전자",
                                 "qty": 7, "avg_price": 50000}]}
        node = Node(FakeApi((True, []), balance), mgr, reg, pyramid)
        node._restore_pending_meta_from_lifecycle()

        rep1 = node.reconcile_stale_pendings(poll_first=False)
        node.sync_positions()
        snap1 = (mgr.load(lc_id).current_state,
                 reg.get_by_trade_id(lc_id)["status"],
                 pyramid.positions["005930"].total_qty)

        rep2 = node.reconcile_stale_pendings(poll_first=False)
        node.sync_positions()
        snap2 = (mgr.load(lc_id).current_state,
                 reg.get_by_trade_id(lc_id)["status"],
                 pyramid.positions["005930"].total_qty)

        self.assertEqual(rep1["expired"], [lc_id])
        self.assertEqual(rep2["candidates"], 0)   # 2회차: 후보 없음
        self.assertEqual(snap1, snap2)             # 상태 동일


if __name__ == "__main__":
    unittest.main()
