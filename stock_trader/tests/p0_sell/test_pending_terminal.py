"""P0-4 PendingRegistry 단말 마킹/ stale 조회 단위 테스트.

CANCELLED / REJECTED / EXPIRED 마킹, FILLED no-op(회계 보호), idempotent,
get_stale_trackable 경과 판정.
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


class PendingTerminalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p04term-")
        self._orig = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "j.db")
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None
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

    def _now(self):
        return datetime.now().isoformat()

    # ── CANCELLED / REJECTED / EXPIRED ───────────────────────────────────
    def test_mark_cancelled(self):
        self.reg.register("KR", "t1", "005930", "SELL", 7, self._now(), odno="OD0000001")
        self.assertTrue(self.reg.mark_terminal("t1", PendingStatus.CANCELLED))
        self.assertFalse(self.reg.has_active_sell("KR", "005930"))

    def test_mark_rejected(self):
        self.reg.register("KR", "t2", "005930", "BUY", 7, self._now(), odno="OD0000002")
        self.assertTrue(self.reg.mark_terminal("t2", PendingStatus.REJECTED))
        self.assertFalse(self.reg.has_active_order("KR", "005930", "BUY"))

    def test_mark_expired(self):
        self.reg.register("KR", "t3", "005930", "SELL", 7, self._now(), odno="OD0000003")
        self.assertTrue(self.reg.mark_terminal("t3", PendingStatus.EXPIRED,
                                               reason="stale"))
        self.assertFalse(self.reg.has_active_sell("KR", "005930"))

    # ── FILLED 는 절대 단말로 덮어쓰지 않음(회계 보호) ────────────────────
    def test_filled_not_overwritten(self):
        self.reg.register("KR", "t4", "005930", "SELL", 7, self._now(), odno="OD0000004")
        self.reg.update_fill("t4", 7, PendingStatus.FILLED)
        # 이미 FILLED → mark_terminal no-op(False)
        self.assertFalse(self.reg.mark_terminal("t4", PendingStatus.EXPIRED))
        row = self.reg.get_by_trade_id("t4")
        self.assertEqual(row["status"], PendingStatus.FILLED)

    # ── idempotent: 두 번째 mark_terminal 은 no-op ───────────────────────
    def test_mark_terminal_idempotent(self):
        self.reg.register("KR", "t5", "005930", "SELL", 7, self._now(), odno="OD0000005")
        self.assertTrue(self.reg.mark_terminal("t5", PendingStatus.EXPIRED))
        self.assertFalse(self.reg.mark_terminal("t5", PendingStatus.EXPIRED))

    def test_invalid_terminal_status_raises(self):
        self.reg.register("KR", "t6", "005930", "SELL", 7, self._now(), odno="OD0000006")
        with self.assertRaises(ValueError):
            self.reg.mark_terminal("t6", PendingStatus.FILLED)

    # ── get_stale_trackable ──────────────────────────────────────────────
    def test_stale_detection(self):
        old = (datetime.now() - timedelta(minutes=10)).isoformat()
        fresh = datetime.now().isoformat()
        self.reg.register("KR", "old1", "005930", "SELL", 7, old, odno="OD0000007")
        self.reg.register("KR", "new1", "000660", "SELL", 3, fresh, odno="OD0000008")
        stale = self.reg.get_stale_trackable(max_age_sec=300)
        ids = {r["trade_id"] for r in stale}
        self.assertIn("old1", ids)
        self.assertNotIn("new1", ids)

    def test_stale_excludes_terminal(self):
        old = (datetime.now() - timedelta(minutes=10)).isoformat()
        self.reg.register("KR", "old2", "005930", "SELL", 7, old, odno="OD0000009")
        self.reg.mark_terminal("old2", PendingStatus.EXPIRED)
        self.assertEqual(self.reg.get_stale_trackable(300), [])


if __name__ == "__main__":
    unittest.main()
