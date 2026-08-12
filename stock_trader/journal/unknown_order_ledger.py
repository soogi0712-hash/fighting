"""UNKNOWN 주문 영속 원장 (SQLite, trading_journal.db 공유).

접수 여부가 불명확한(타임아웃/HTTP 500/파싱실패) 국내 BUY POST 를 '즉시' 영속
기록하고, 해소될 때까지 해당 종목·방향의 신규 BUY 를 차단한다. 프로세스 재시작
후에도 차단이 유지된다(메모리 아님).

상태(status):
  PENDING              : 접수 불명확 → 조사 대기(BUY 차단)
  UNKNOWN_NOT_FOUND    : 당일주문 조회 성공했으나 동일조건 후보 0건 → 계속 차단.
                          단순 시간경과·조회횟수·장마감·날짜변경으로 자동해제하지 않는다.
                          이후 후보가 늦게 나타나면(조회지연/일자경계) 승격·부킹으로 해소.
  RESOLVED_ACCEPTED    : KIS 당일주문에서 주문 발견(ODNO 연결) → 정상 pending 승격(차단 해제)
  RESOLVED_FILLED      : 체결 발견 → 체결기반 부킹으로 넘김(차단 해제)
  RESOLVED_MANUAL      : 운영자 명시적 사유로 수동 해제(차단 해제). 자동경로 금지.
  AMBIGUOUS_MATCH      : 동일조건 후보 2건 이상 → 자동해제·자동재주문 금지, 계속 차단
  MANUAL_REVIEW        : 승격·부킹 미확인 등 → 계속 차단, 수동확인 필요

자동해제 정책(P0): 후보 0건 반복은 명확한 미접수 증거가 아니다(조회지연·조회범위·
일자경계로 실제 접수 주문이 늦게 나타날 수 있음). 따라서 not_found_streak 가 아무리
증가해도 자동으로 해제하지 않는다. 자동해제는 (1)고유 후보 발견 승격/부킹, 또는
(2)KIS 가 명시적 주문거절/미접수를 식별 가능한 증거로 반환하는 경우에만 허용한다.
그 외에는 release_unknown(사유 필수) 로만 수동 해제한다.

차단 상태 = PENDING, UNKNOWN_NOT_FOUND, AMBIGUOUS_MATCH, MANUAL_REVIEW.
재점검(자동 상태변경 가능) 상태 = PENDING, UNKNOWN_NOT_FOUND.
"""
from __future__ import annotations

import os
import json
import sqlite3
import threading

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "data", "trading_journal.db")

# 차단을 유지하는 상태(신규 BUY 차단)
BLOCKING_STATUSES = ("PENDING", "UNKNOWN_NOT_FOUND", "AMBIGUOUS_MATCH",
                     "MANUAL_REVIEW")
# ★ 비차단 확인대기(SELL rt_cd=0·ODNO 미수신): 신규 주문을 '차단하지 않지만'
#   당일주문/체결조회 정합화로 ODNO·체결을 연결하기 위해 영속·재점검한다.
NONBLOCK_STATUSES = ("PENDING_CONFIRM",)
# 정합화 잡이 자동으로 재점검·상태변경할 수 있는 상태
# (AMBIGUOUS_MATCH/MANUAL_REVIEW 는 수동확인 전용 → 자동 변경 안 함)
RECHECK_STATUSES = ("PENDING", "UNKNOWN_NOT_FOUND", "PENDING_CONFIRM")


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
                    history         TEXT,
                    not_found_streak INTEGER DEFAULT 0
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS ix_unknown_active "
                      "ON unknown_orders(account, market, code, side, status)")
            # ── 마이그레이션: 기존 DB 에 없는 컬럼 안전 추가 ──────────────
            _cols = {r["name"] for r in c.execute(
                "PRAGMA table_info(unknown_orders)").fetchall()}
            if "not_found_streak" not in _cols:
                c.execute("ALTER TABLE unknown_orders "
                          "ADD COLUMN not_found_streak INTEGER DEFAULT 0")

    # ── 기록 ────────────────────────────────────────────────────
    def record(self, account, market, code, side, qty, price, ord_dvsn,
               created_at, created_hhmmss="", reason="", blocking=True) -> int:
        """접수 불명확 POST 를 영속 기록. 새 row id 반환.

        blocking=True  → PENDING(차단): BUY 중복주문 방지용(신규 BUY 차단).
        blocking=False → PENDING_CONFIRM(비차단): SELL rt_cd=0·ODNO 미수신 등
          '확인대기'. 신규 주문을 차단하지 않으며, 정합화가 ODNO·체결을 연결한다.
        """
        _status = "PENDING" if blocking else "PENDING_CONFIRM"
        _ev = "RECORD_UNKNOWN" if blocking else "RECORD_CONFIRM_PENDING"
        hist = json.dumps([{"ts": created_at, "event": _ev,
                            "note": reason}], ensure_ascii=False)
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO unknown_orders (account, market, code, side, qty, "
                "price, ord_dvsn, created_at, created_hhmmss, status, odno, "
                "last_checked_at, resolved_at, history) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account, market, code, side, int(qty), int(price or 0),
                 ord_dvsn or "", created_at, created_hhmmss, _status, "",
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

    def list_reconcilable(self, account=None, market=None):
        """정합화가 자동 재점검할 수 있는 row 목록(차단 PENDING/UNKNOWN_NOT_FOUND +
        비차단 PENDING_CONFIRM). 리콘실러가 이 목록을 순회한다."""
        q = ("SELECT * FROM unknown_orders WHERE status IN (%s)"
             % ",".join("?" * len(RECHECK_STATUSES)))
        args = list(RECHECK_STATUSES)
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

    def mark_not_found(self, rid, streak, note="", ts="") -> None:
        """동일조건 주문 0건(조회지연 가능) → 상태 UNKNOWN_NOT_FOUND 로 두고 계속 차단.

        자동해제하지 않는다. not_found_streak 는 진단·이력용으로만 증가시키며,
        이후 후보가 나타나면 승격·부킹 경로로 해소된다(RECHECK_STATUSES 에 포함).
        """
        with self._lock, self._conn() as c:
            hist = self._append_history(c, rid, "NOT_FOUND", note, ts)
            c.execute("UPDATE unknown_orders SET status='UNKNOWN_NOT_FOUND', "
                      "not_found_streak=?, last_checked_at=?, history=? WHERE id=?",
                      (int(streak), ts, hist, rid))

    def reset_not_found(self, rid, ts="") -> None:
        """후보 발견 등으로 0건 스트릭 리셋."""
        with self._lock, self._conn() as c:
            c.execute("UPDATE unknown_orders SET not_found_streak=0, "
                      "last_checked_at=? WHERE id=?", (ts, rid))

    def resolve(self, rid, status, odno="", note="", ts="") -> None:
        """상태 확정(RESOLVED_ACCEPTED/RESOLVED_FILLED/AMBIGUOUS_MATCH/MANUAL_REVIEW)
        + 이력·해소시각 기록. 후보 발견 기반 승격·부킹 등 '증거 기반' 경로에서만 호출.

        주의: 시간경과·조회횟수 기반 자동 미접수 해제는 제공하지 않는다.
        운영자 수동 해제는 release_unknown() 를 사용한다.
        """
        _resolved_at = ts if status.startswith("RESOLVED_") else ""
        with self._lock, self._conn() as c:
            hist = self._append_history(c, rid, status, note, ts)
            c.execute(
                "UPDATE unknown_orders SET status=?, odno=?, last_checked_at=?, "
                "resolved_at=?, history=? WHERE id=?",
                (status, odno or "", ts, _resolved_at, hist, rid))

    def release_unknown(self, rid, operator_reason, operator="", ts="") -> bool:
        """운영자 수동 해제(RESOLVED_MANUAL). 명시적 사유가 있어야만 해제한다.

        - 사유(operator_reason)가 비어 있으면 ValueError → 해제 금지.
        - 이력에 '이전 상태·해제시각·사유·운영자'를 기록한다.
        - 일반 매매 루프·시간 스케줄러에서는 절대 호출하지 않는다(수동 관리 전용).
        반환: True(해제됨). 대상이 없거나 이미 차단상태가 아니면 False.
        """
        reason = (operator_reason or "").strip()
        if not reason:
            raise ValueError("수동 해제에는 명시적 사유가 필요합니다(빈 사유 금지).")
        with self._lock, self._conn() as c:
            row = c.execute("SELECT status FROM unknown_orders WHERE id=?",
                            (rid,)).fetchone()
            if row is None:
                return False
            prev = row["status"]
            if prev not in BLOCKING_STATUSES:
                return False   # 이미 해소됨 → 중복 해제 방지
            note = (f"수동해제 prev={prev} operator={operator or '-'} "
                    f"reason={reason}")
            hist = self._append_history(c, rid, "RESOLVED_MANUAL", note, ts)
            c.execute(
                "UPDATE unknown_orders SET status='RESOLVED_MANUAL', "
                "last_checked_at=?, resolved_at=?, history=? WHERE id=?",
                (ts, ts, hist, rid))
            return True


# ── 프로세스 공용 기본 원장(싱글턴) ──────────────────────────────
_DEFAULT_LEDGER = None
_DEFAULT_LEDGER_GUARD = threading.Lock()


def get_default_unknown_ledger() -> UnknownOrderLedger:
    global _DEFAULT_LEDGER
    with _DEFAULT_LEDGER_GUARD:
        if _DEFAULT_LEDGER is None:
            _DEFAULT_LEDGER = UnknownOrderLedger()
        return _DEFAULT_LEDGER
