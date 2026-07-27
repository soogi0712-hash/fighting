"""P0-0 in-flight 매도 가드 — PendingOrderRegistry.has_active_sell 단위 테스트.

실 KIS API 미호출. 임시 SQLite DB로 격리.
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
from journal.fill_observer import PendingOrderRegistry, PendingStatus  # noqa: E402

TS = "2026-07-27T09:00:00"


class InflightGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p0guard-")
        self._orig = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "j.db")
        # thread-local 연결 초기화 → 임시 DB로 재연결
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
        conn = fo._get_conn()
        conn.executescript(fo._PENDING_DDL)   # 테이블 보장
        self.reg = PendingOrderRegistry()

    def tearDown(self):
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
        fo._JOURNAL_DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_accepted_sell_is_active(self):
        self.reg.register("KR", "t1", "005930", "SELL", 7, TS)
        self.assertTrue(self.reg.has_active_sell("KR", "005930"))

    def test_partially_filled_sell_is_active(self):
        self.reg.register("KR", "t1", "005930", "SELL", 7, TS)
        self.reg.update_fill("t1", 3, PendingStatus.PARTIALLY_FILLED)
        self.assertTrue(self.reg.has_active_sell("KR", "005930"))

    def test_filled_sell_not_active(self):
        self.reg.register("KR", "t1", "005930", "SELL", 7, TS)
        self.reg.update_fill("t1", 7, PendingStatus.FILLED)
        self.assertFalse(self.reg.has_active_sell("KR", "005930"))

    def test_cancelled_sell_not_active(self):
        self.reg.register("KR", "t1", "005930", "SELL", 7, TS)
        self.reg.update_fill("t1", 0, PendingStatus.CANCELLED)
        self.assertFalse(self.reg.has_active_sell("KR", "005930"))

    def test_buy_not_counted_as_active_sell(self):
        self.reg.register("KR", "t1", "005930", "BUY", 7, TS)
        self.assertFalse(self.reg.has_active_sell("KR", "005930"))

    def test_other_code_isolated(self):
        self.reg.register("KR", "t1", "005930", "SELL", 7, TS)
        self.assertFalse(self.reg.has_active_sell("KR", "000660"))

    def test_other_market_isolated(self):
        self.reg.register("US", "t1", "TSLA", "SELL", 7, TS)
        self.assertFalse(self.reg.has_active_sell("KR", "TSLA"))
        self.assertTrue(self.reg.has_active_sell("US", "TSLA"))

    def test_no_orders_is_false(self):
        self.assertFalse(self.reg.has_active_sell("KR", "005930"))


if __name__ == "__main__":
    unittest.main()
