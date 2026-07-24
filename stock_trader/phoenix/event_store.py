"""EventStore — append-only 이벤트 + 단일 트랜잭션 apply.

apply(event) 한 번에:
  1) idempotency 검사(UNIQUE idempotency_key)
  2) events append
  3) projection 갱신 (Projector)
  4) processed_watermark 갱신
  → 2·3·4 가 하나의 트랜잭션. 부분 반영이 디스크에 존재할 수 없다(Phase 1 §4).

crash injection 지점(테스트용):
  before_append / after_append / after_projection / before_commit / after_commit
"""
from __future__ import annotations

import sqlite3

from .db import Database
from .errors import AlreadyApplied, SafeHaltError
from .models import Event, ApplyResult, ApplyStatus
from .projections import Projector


class EventStore:
    def __init__(self, db: Database, projector: Projector | None = None):
        self.db = db
        self.projector = projector or Projector()

    # ── 반영 ───────────────────────────────────────────────────────
    def apply(self, ev: Event, *, allow_during_halt: bool = False) -> ApplyResult:
        if not allow_during_halt and self.is_safe_halt():
            raise SafeHaltError("Safe-Halt 상태 — 이벤트 반영 거부")
        try:
            with self.db.transaction() as conn:
                self.db.trip("before_append")
                seq = self._append(conn, ev)
                self.db.trip("after_append")
                self.projector.apply(conn, ev, seq)
                self.db.trip("after_projection")
                conn.execute(
                    "UPDATE processed_watermark SET last_seq=? WHERE id=1", (seq,))
                self.db.trip("before_commit")
            # 여기서 COMMIT 완료
            self.db.trip("after_commit")
            ev.seq = seq
            return ApplyResult(ApplyStatus.APPLIED, seq)
        except AlreadyApplied:
            return ApplyResult(ApplyStatus.ALREADY_APPLIED, None)

    def _append(self, conn: sqlite3.Connection, ev: Event) -> int:
        try:
            cur = conn.execute(
                "INSERT INTO events(event_uuid, ts, type, aggregate_type, "
                "aggregate_id, client_order_id, odno, code, side, qty, price, "
                "cum_filled_qty, realized_pnl, idempotency_key, payload, schema_ver) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ev.insert_tuple(),
            )
        except sqlite3.IntegrityError as e:
            # UNIQUE(idempotency_key) 또는 UNIQUE(event_uuid) 충돌 → 이미 반영
            raise AlreadyApplied(str(e)) from e
        return int(cur.lastrowid)

    # ── 조회 ───────────────────────────────────────────────────────
    def last_seq(self) -> int:
        r = self.db.conn.execute(
            "SELECT last_seq FROM processed_watermark WHERE id=1").fetchone()
        return int(r["last_seq"]) if r else 0

    def event_count(self) -> int:
        r = self.db.conn.execute("SELECT COUNT(*) c FROM events").fetchone()
        return int(r["c"])

    def get_position(self, code: str) -> dict | None:
        r = self.db.conn.execute(
            "SELECT * FROM positions WHERE code=?", (code,)).fetchone()
        return dict(r) if r else None

    def get_order(self, client_order_id: str) -> dict | None:
        r = self.db.conn.execute(
            "SELECT * FROM order_index WHERE client_order_id=?",
            (client_order_id,)).fetchone()
        return dict(r) if r else None

    def get_daily_pnl(self, session_key: str | None = None) -> dict | None:
        if session_key is None:
            r = self.db.conn.execute(
                "SELECT session_key FROM engine_state WHERE id=1").fetchone()
            session_key = r["session_key"] if r else "INIT"
        r = self.db.conn.execute(
            "SELECT * FROM daily_pnl WHERE session_key=?", (session_key,)).fetchone()
        return dict(r) if r else None

    # ── Safe-Halt ──────────────────────────────────────────────────
    def is_safe_halt(self) -> bool:
        r = self.db.conn.execute(
            "SELECT safe_halt FROM engine_state WHERE id=1").fetchone()
        return bool(r and r["safe_halt"])

    def enter_safe_halt(self, reason: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE engine_state SET safe_halt=1, safe_halt_reason=? WHERE id=1",
                (reason,))

    def clear_safe_halt(self) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE engine_state SET safe_halt=0, safe_halt_reason=NULL WHERE id=1")
