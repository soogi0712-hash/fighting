"""Crash / 원자성 / 재구축 / lock / corruption 테스트 (Phase 2-2).

- event append 중 crash
- projection 갱신 중 crash
- commit 직전 crash
- commit 직후 crash
- 재시작 후 projection 재구축
- DB lock
- DB corruption 감지
"""
import os
import sqlite3

from ._helpers import PhoenixTestCase, fresh_db
from phoenix import Database, EventStore, Recovery, SimulatedCrash, ApplyStatus
from phoenix import check_integrity
from phoenix.models import execution_event, OrderSide

COID = "co-1"
CODE = "005930"


def _exec(cum, ord_qty=100):
    return execution_event(COID, CODE, OrderSide.BUY, cum, 100.0, ord_qty=ord_qty)


class TestCrashAtomicity(PhoenixTestCase):

    def _assert_rolled_back(self, point):
        db, store = self.new_store()
        db.set_crash_points(point)
        with self.assertRaises(SimulatedCrash):
            store.apply(_exec(10))
        # 재시작 모사
        db.reopen()
        store2 = EventStore(db)
        self.assertEqual(store2.event_count(), 0, f"{point}: 이벤트가 남으면 안 됨")
        self.assertIsNone(store2.get_position(CODE), f"{point}: projection 변경 금지")
        self.assertEqual(store2.last_seq(), 0, f"{point}: watermark 변경 금지")

    def test_crash_during_event_append(self):
        self._assert_rolled_back("after_append")

    def test_crash_during_projection_update(self):
        self._assert_rolled_back("after_projection")

    def test_crash_just_before_commit(self):
        self._assert_rolled_back("before_commit")

    def test_crash_just_after_commit_is_durable_and_exactly_once(self):
        db, store = self.new_store()
        db.set_crash_points("after_commit")
        ev = _exec(10)
        with self.assertRaises(SimulatedCrash):
            store.apply(ev)
        # commit 은 이미 완료 → 재시작 후에도 durable
        db.reopen()
        store2 = EventStore(db)
        self.assertEqual(store2.event_count(), 1)
        self.assertEqual(store2.get_position(CODE)["qty"], 10)
        # 동일 이벤트 재반영 시도 → 중복 반영 없음(exactly-once)
        again = execution_event(COID, CODE, OrderSide.BUY, 10, 100.0, ord_qty=100)
        res = store2.apply(again)
        self.assertEqual(res.status, ApplyStatus.ALREADY_APPLIED)
        self.assertEqual(store2.get_position(CODE)["qty"], 10)


class TestProjectionRebuild(PhoenixTestCase):

    def test_rebuild_matches_live_projection(self):
        db, store = self.new_store()
        # 매수/매도는 서로 다른 주문(client_order_id) — watermark 는 주문 단위
        store.apply(execution_event("co-buy", CODE, OrderSide.BUY, 100, 100.0,
                                    ord_qty=100))
        store.apply(execution_event("co-sell", CODE, OrderSide.SELL, 40, 105.0,
                                    ord_qty=40))
        before_pos = store.get_position(CODE)
        before_pnl = store.get_daily_pnl()
        self.assertEqual(before_pos["qty"], 60)          # 100 매수 - 40 매도
        self.assertAlmostEqual(before_pnl["realized_pnl"], 200.0, places=6)

        # projection 폐기 후 events 재생으로 재구축
        Recovery(db).rebuild_projection()

        self.assertEqual(store.get_position(CODE)["qty"], before_pos["qty"])
        self.assertAlmostEqual(store.get_position(CODE)["avg_price"],
                               before_pos["avg_price"], places=6)
        self.assertAlmostEqual(store.get_daily_pnl()["realized_pnl"],
                               before_pnl["realized_pnl"], places=6)
        # 40주 * (105-100) = 200 실현
        self.assertAlmostEqual(store.get_daily_pnl()["realized_pnl"], 200.0, places=6)


class TestDbLock(PhoenixTestCase):

    def test_second_writer_gets_locked(self):
        db1 = fresh_db(self.db_path, busy_timeout_ms=100)
        db2 = Database(self.db_path, busy_timeout_ms=100)
        db1.conn.execute("BEGIN IMMEDIATE")
        db1.conn.execute(
            "UPDATE processed_watermark SET last_seq=last_seq WHERE id=1")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db2.conn.execute("BEGIN IMMEDIATE")
                db2.conn.execute("UPDATE processed_watermark SET last_seq=1 WHERE id=1")
        finally:
            db1.conn.execute("ROLLBACK")
            db1.close()
            db2.close()


class TestCorruptionDetection(PhoenixTestCase):

    def test_healthy_db_integrity_ok(self):
        db, store = self.new_store()
        store.apply(_exec(10))
        self.assertTrue(db.integrity_ok())
        db.close()
        self.assertTrue(check_integrity(self.db_path).ok)

    def test_corrupt_header_detected(self):
        db, store = self.new_store()
        store.apply(_exec(10))
        # WAL 을 본 파일로 합치고 닫음
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        # 헤더(SQLite magic) 손상
        with open(self.db_path, "r+b") as f:
            f.write(b"\x00" * 16)
        res = check_integrity(self.db_path)
        self.assertFalse(res.ok)
        self.assertTrue(len(res.detail) > 0)
