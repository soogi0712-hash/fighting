"""UNKNOWN 주문 영속 원장 (SQLite, trading_journal.db 공유).

접수 여부가 불명확한(타임아웃/HTTP 500/파싱실패) 국내 BUY POST 를 '즉시' 영속
기록하고, 해소될 때까지 해당 종목·방향의 신규 BUY 를 차단한다. 프로세스 재시작
후에도 차단이 유지된다(메모리 아님).

상태(status):
  PENDING              : 접수 불명확 → 조사 대기(BUY 차단)
  RESOLVED_ACCEPTED    : KIS 당일주문에서 주문 발견(ODNO 연결) → 정상 pending 승격(차단 해제)
  RESOLVED_FILLED      : 체결 발견 → 체결기반 부킹으로 넘김(차단 해제)
  RESOLVED_NOT_ACCEPTED: 명확한 미접수 증거 확인 → 차단 해제
  MANUAL_REVIEW        : 후보 다수·식별 불가 → 계속 차단, 수동확인 필요

차단 상태 = PENDING, MANUAL_REVIEW.
"""
from __future__ import annotations

import os
import json
import sqlite3
import threading

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "data", "trading_journal.db")

# 차단을 유지하는 상태
BLOCKING_STATUSES = ("PENDING", "MANUAL_REVIEW")


class UnknownOrderLedger:
    def __init__(self, db_path: str = _DEFAULT_DB):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        with self._lock, self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS unknown_orders (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    account         TEXT,
                    market          TEXT,
                    code            TEXT,
                    side            TEXT,
                    qty             INTEGER,
                    price           INTEGER,
                    ord_dvsn        TEXT,
                    created_at      TEXT,
                    created_hhmmss  TEXT,
                    status          TEXT,
                    odno            TEXT,
                    last_checked_at TEXT,
                    resolved_at     TEXT,
                    history         TEXT
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS ix_unknown_active "
                      "ON unknown_orders(account, market, code, side, status)")

    # ── 기록 ────────────────────────────────────────────────────
    def record(self, account, market, code, side, qty, price, ord_dvsn,
               created_at, created_hhmmss="", reason="") -> int:
        """접수 불명확 POST 를 PENDING 으로 영속 기록. 새 row id 반환."""
        hist = json.dumps([{"ts": created_at, "event": "RECORD_UNKNOWN",
                            "note": reason}], ensure_ascii=False)
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO unknown_orders (account, market, code, side, qty, "
                "price, ord_dvsn, created_at, created_hhmmss, status, odno, "
                "last_checked_at, resolved_at, history) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account, market, code, side, int(qty), int(price or 0),
                 ord_dvsn or "", created_at, created_hhmmss, "PENDING", "",
                 "", "", hist))
            return cur.lastrowid

    # ── 조회 ────────────────────────────────────────────────────
    def has_active(self, account, market, code, side) -> bool:
        """해당 계좌·시장·종목·방향에 차단 상태(PENDING/MANUAL_REVIEW) 존재?"""
        q = ("SELECT 1 FROM unknown_orders WHERE account=? AND market=? AND "
             "code=? AND side=? AND status IN (%s) LIMIT 1"
             % ",".join("?" * len(BLOCKING_STATUSES)))
        with self._lock, self._conn() as c:
            row = c.execute(q, (account, market, code, side, *BLOCKING_STATUSES)
                            ).fetchone()
            return row is not None

    def list_active(self, account=None, market=None):
        """차단 상태 row 목록(재시작 후 재구성·정합화 대상)."""
        q = ("SELECT * FROM unknown_orders WHERE status IN (%s)"
             % ",".join("?" * len(BLOCKING_STATUSES)))
        args = list(BLOCKING_STATUSES)
        if account:
            q += " AND account=?"; args.append(account)
        if market:
            q += " AND market=?"; args.append(market)
        q += " ORDER BY id ASC"
        with self._lock, self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def get(self, rid):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM unknown_orders WHERE id=?", (rid,)).fetchone()
            return dict(r) if r else None

    def active_display(self, account=None):
        """대시보드/로그용 최소 필드(계좌번호·원문 제외)."""
        return [{"code": r["code"], "side": r["side"], "qty": r["qty"],
                 "price": r["price"], "status": r["status"],
                 "created_at": r["created_at"]}
                for r in self.list_active(account=account)]

    # ── 상태 변경(이력 append) ──────────────────────────────────
    def _append_history(self, c, rid, event, note, ts):
        row = c.execute("SELECT history FROM unknown_orders WHERE id=?",
                        (rid,)).fetchone()
        try:
            hist = json.loads(row["history"]) if row and row["history"] else []
        except Exception:
            hist = []
        hist.append({"ts": ts, "event": event, "note": note})
        return json.dumps(hist, ensure_ascii=False)

    def mark_checked(self, rid, note="", ts="") -> None:
        """정합화 조회 시각·이력 기록(상태는 유지)."""
        with self._lock, self._conn() as c:
            hist = self._append_history(c, rid, "CHECKED", note, ts)
            c.execute("UPDATE unknown_orders SET last_checked_at=?, history=? "
                      "WHERE id=?", (ts, hist, rid))

    def resolve(self, rid, status, odno="", note="", ts="") -> None:
        """상태 확정(RESOLVED_* / MANUAL_REVIEW) + 이력·해소시각 기록."""
        _resolved_at = ts if status.startswith("RESOLVED_") else ""
        with self._lock, self._conn() as c:
            hist = self._append_history(c, rid, status, note, ts)
            c.execute(
                "UPDATE unknown_orders SET status=?, odno=?, last_checked_at=?, "
                "resolved_at=?, history=? WHERE id=?",
                (status, odno or "", ts, _resolved_at, hist, rid))


# ── 프로세스 공용 기본 원장(싱글턴) ──────────────────────────────
_DEFAULT_LEDGER = None
_DEFAULT_LEDGER_GUARD = threading.Lock()


def get_default_unknown_ledger() -> UnknownOrderLedger:
    global _DEFAULT_LEDGER
    with _DEFAULT_LEDGER_GUARD:
        if _DEFAULT_LEDGER is None:
            _DEFAULT_LEDGER = UnknownOrderLedger()
        return _DEFAULT_LEDGER
