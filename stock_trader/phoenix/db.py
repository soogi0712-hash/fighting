"""SQLite 연결 관리 · PRAGMA · 트랜잭션 헬퍼 · 스키마 마이그레이션.

설계 원칙(Phase 1 §4):
  - 단일 파일(phoenix.db), WAL, synchronous=FULL
  - 모든 상태 전이는 하나의 트랜잭션(BEGIN IMMEDIATE … COMMIT)
  - crash injection seam: 테스트가 트랜잭션 내 특정 지점에서 프로세스 종료를 모사
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Optional

from . import schema as _schema
from .errors import SchemaVersionError


class SimulatedCrash(BaseException):
    """crash injection: 정상 예외 계층(Exception) 밖에 두어
    애플리케이션 except 절에 조용히 삼켜지지 않게 한다."""

    def __init__(self, point: str):
        super().__init__(f"simulated crash at {point!r}")
        self.point = point


class Database:
    """phoenix.db 연결 + 트랜잭션 + 마이그레이션."""

    def __init__(self, path: str, *, synchronous: str = "FULL",
                 busy_timeout_ms: int = 5000):
        self.path = path
        self._synchronous = synchronous
        self._busy_timeout_ms = busy_timeout_ms
        self._crash_points: set[str] = set()
        self.conn = self._open()

    # ── 연결/PRAGMA ────────────────────────────────────────────────
    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)  # 수동 트랜잭션
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA synchronous={self._synchronous}")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def reopen(self) -> None:
        """재시작 모사: 연결을 닫고 다시 연다(미커밋 트랜잭션은 폐기됨)."""
        self.close()
        self.conn = self._open()

    # ── crash injection ────────────────────────────────────────────
    def set_crash_points(self, *points: str) -> None:
        self._crash_points = set(points)

    def clear_crash_points(self) -> None:
        self._crash_points = set()

    def trip(self, point: str) -> None:
        if point in self._crash_points:
            # 한 번만 발동하도록 소모(재시도 루프 무한발동 방지)
            self._crash_points.discard(point)
            raise SimulatedCrash(point)

    # ── 트랜잭션 헬퍼 ──────────────────────────────────────────────
    @contextmanager
    def transaction(self, mode: str = "IMMEDIATE"):
        conn = self.conn
        conn.execute(f"BEGIN {mode}")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ── 마이그레이션/버전 ──────────────────────────────────────────
    def migrate(self) -> int:
        """스키마를 SCHEMA_VERSION 까지 올린다. 현재 버전 반환.
        DB 버전이 코드보다 높으면 SchemaVersionError(다운그레이드 금지)."""
        cur_ver = self._read_version()
        target = _schema.SCHEMA_VERSION
        if cur_ver > target:
            raise SchemaVersionError(
                f"DB schema v{cur_ver} > code v{target} (downgrade 금지)")
        # executescript 는 COMMIT 을 먼저 발행하고 isolation_level 을 무시하므로
        # 명시적 트랜잭션 밖에서 실행한다(autocommit 모드에서 각 DDL 이 확정됨).
        for ver in range(cur_ver, target):
            self.conn.executescript(_schema.MIGRATIONS[ver])
        with self.transaction():
            for sql in _schema.initial_rows_sql():
                self.conn.execute(sql)
            self.conn.execute(
                "INSERT INTO schema_meta(id, version) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version",
                (target,))
        return target

    def _read_version(self) -> int:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if not row:
            return 0
        r = self.conn.execute("SELECT version FROM schema_meta WHERE id=1").fetchone()
        return int(r["version"]) if r else 0

    def schema_version(self) -> int:
        return self._read_version()

    # ── 무결성 ─────────────────────────────────────────────────────
    def integrity_ok(self) -> bool:
        try:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and row[0] == "ok"
        except sqlite3.DatabaseError:
            return False
