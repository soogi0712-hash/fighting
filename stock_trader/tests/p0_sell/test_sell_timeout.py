"""P0-4 SELL 타임아웃(접수 불명) 검증 — _handle_sell_timeout 테스트.

이중매도 방지가 핵심:
  - open orders 에 살아있음 → 신규주문 없이 lifecycle+pending 추적 등록(재시도 X).
  - open orders 미발견 / API 실패 → SELL_TIMEOUT_UNVERIFIED (재시도 X, 부킹 X).
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
from strategies.strategy_manager import StrategyManager  # noqa: E402


class FakeApi:
    def __init__(self, open_result):
        self._open = open_result
        self.sell_calls = []

    def get_open_orders_checked(self, order_type="BUY"):
        return self._open

    def sell(self, code, qty, price, ord_dvsn=None):
        self.sell_calls.append((code, qty, price))
        return {"rt_cd": "0", "output": {"KNO_ORD_NO": "SHOULD_NOT_HAPPEN"}}


class Flow:
    _handle_sell_timeout    = StrategyManager._handle_sell_timeout
    _track_existing_sell    = StrategyManager._track_existing_sell
    _register_pending_order = StrategyManager._register_pending_order
    has_active_sell         = StrategyManager.has_active_sell

    def __init__(self, api, mgr, reg):
        self.api = api
        self._lifecycle_mgr = mgr
        self._pending_registry = reg
        self._pending_sell_meta = {}


SESS = {"session": "정규장"}


class SellTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p04to-")
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

    # ── 타임아웃인데 거래소에 실제 접수됨 → 추적 등록, 재시도 안 함 ───────
    def test_timeout_live_order_tracked(self):
        orders = [{"order_no": "OD9", "stock_code": "005930",
                   "sll_buy_dvsn_cd": "01"}]
        flow = Flow(FakeApi((True, orders)), self.mgr, self.reg)
        res = flow._handle_sell_timeout("005930", "삼성전자", 7, 54000,
                                        None, True, "손절", SESS)
        self.assertEqual(res["action"], "SELL")
        self.assertTrue(res.get("_timeout_recovered"))
        # 신규 주문(api.sell) 은 호출되지 않음(이미 접수됨)
        self.assertEqual(flow.api.sell_calls, [])
        # lifecycle+pending 추적 등록됨
        lc_id = res["_lifecycle_id"]
        self.assertEqual(self.mgr.load(lc_id).current_state,
                         LifecycleState.ORDER_ACCEPTED)
        self.assertTrue(self.reg.has_active_sell("KR", "005930"))

    # ── 타임아웃 + open orders 미발견 → UNVERIFIED (재시도 X) ────────────
    def test_timeout_not_found_unverified(self):
        flow = Flow(FakeApi((True, [])), self.mgr, self.reg)
        res = flow._handle_sell_timeout("005930", "삼성전자", 7, 54000,
                                        None, True, "손절", SESS)
        self.assertEqual(res["action"], "SELL_TIMEOUT_UNVERIFIED")
        self.assertEqual(res["_timeout_detail"], "not_in_open_orders")
        self.assertFalse(self.reg.has_active_sell("KR", "005930"))
        self.assertEqual(flow.api.sell_calls, [])

    # ── 타임아웃 + 검증 API 실패 → UNVERIFIED (절대 재시도/부킹 X) ────────
    def test_timeout_api_fail_unverified(self):
        flow = Flow(FakeApi((False, [])), self.mgr, self.reg)
        res = flow._handle_sell_timeout("005930", "삼성전자", 7, 54000,
                                        None, True, "손절", SESS)
        self.assertEqual(res["action"], "SELL_TIMEOUT_UNVERIFIED")
        self.assertEqual(res["_timeout_detail"], "verify_api_failed")
        self.assertFalse(self.reg.has_active_sell("KR", "005930"))


if __name__ == "__main__":
    unittest.main()
