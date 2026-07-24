"""Phoenix 테스트 공용 헬퍼.

phoenix 패키지를 import 하기 위해 stock_trader 루트를 sys.path 에 추가한다.
(테스트는 `cd stock_trader && python -m unittest discover -s tests/phoenix` 로 실행)
"""
import os
import sys
import tempfile
import unittest

# stock_trader/ 를 import 경로에 추가
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from phoenix import Database, EventStore, Projector  # noqa: E402


def fresh_db(path: str, busy_timeout_ms: int = 300) -> Database:
    db = Database(path, busy_timeout_ms=busy_timeout_ms)
    db.migrate()
    return db


class PhoenixTestCase(unittest.TestCase):
    """임시 DB 파일을 관리하는 베이스 케이스."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="phoenix-test-")
        self.db_path = os.path.join(self._tmp, "phoenix.db")

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(self._tmp)
        except OSError:
            pass

    def new_store(self, busy_timeout_ms: int = 300):
        db = fresh_db(self.db_path, busy_timeout_ms=busy_timeout_ms)
        return db, EventStore(db)
