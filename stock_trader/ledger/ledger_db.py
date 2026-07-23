"""
ledger_db.py — data/ledger.db 연결 및 스키마 마이그레이션 (V1)

- 단일 테이블 `trades` (라운드트립 1건 = 1행).
- 가산적(additive) 마이그레이션만 허용. 파괴적 변경 금지.
- 멱등(CREATE IF NOT EXISTS): 여러 번 호출해도 안전.
"""
import os
import sqlite3
import threading

# ── DB 경로 (Shadow lab.db 와 물리적 분리) ──────────────────────
_PKG_DIR      = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR  = os.path.dirname(_PKG_DIR)
LEDGER_DB_PATH = os.path.join(_PROJECT_DIR, "data", "ledger.db")

# 스레드별 커넥션 (SocketIO/스케줄러 멀티스레드 대비)
_local = threading.local()


# ══════════════════════════════════════════════════════════════
# 마이그레이션 정의
# ══════════════════════════════════════════════════════════════
_MIGRATION_V1 = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY,

  -- 식별
  market TEXT NOT NULL,
  code   TEXT NOT NULL,
  name   TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN',
  entry_order_no TEXT NOT NULL,
  exit_order_no  TEXT,

  -- 전략/버전 (자기완결)
  strategy_version    TEXT,
  param_snapshot_json TEXT,

  -- 진입 (집계)
  entry_reason          TEXT,
  entry_indicators_json TEXT,
  entry_time            TEXT,
  entry_decision_price  REAL,
  avg_entry_price       REAL,
  entry_qty_total       INTEGER,
  entry_fill_count      INTEGER DEFAULT 0,

  -- 청산 (집계)
  exit_reason           TEXT,
  exit_indicators_json  TEXT,
  exit_time             TEXT,
  exit_decision_price   REAL,
  avg_exit_price        REAL,
  exit_qty_total        INTEGER DEFAULT 0,
  exit_fill_count       INTEGER DEFAULT 0,

  -- 손익 (실제 체결가 기준, 슬리피지 재차감 금지)
  realized_pnl REAL,
  net_pnl      REAL,
  fees         REAL,
  tax          REAL,

  -- 체결 품질 (분석 전용, net_pnl 미반영)
  entry_slippage REAL,
  exit_slippage  REAL,

  -- 경로
  mfe_pct REAL,
  mae_pct REAL,
  hold_seconds INTEGER,

  -- 원본 보존
  raw_orders_json TEXT,
  created_at TEXT,
  updated_at TEXT,

  -- 무결성
  CHECK (market IN ('KR','US')),
  CHECK (status IN ('OPEN','PARTIAL','CLOSED')),
  CHECK (entry_qty_total IS NULL OR entry_qty_total > 0),
  CHECK (exit_qty_total >= 0 AND (entry_qty_total IS NULL OR exit_qty_total <= entry_qty_total)),
  CHECK (status <> 'CLOSED' OR (
           exit_time      IS NOT NULL AND
           avg_exit_price IS NOT NULL AND
           net_pnl        IS NOT NULL AND
           exit_qty_total = entry_qty_total))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_entry_order ON trades(market, entry_order_no);
CREATE INDEX IF NOT EXISTS ix_trades_code    ON trades(market, code);
CREATE INDEX IF NOT EXISTS ix_trades_status  ON trades(status);
CREATE INDEX IF NOT EXISTS ix_trades_etime   ON trades(entry_time);
CREATE INDEX IF NOT EXISTS ix_trades_xtime   ON trades(exit_time);
CREATE INDEX IF NOT EXISTS ix_trades_version ON trades(strategy_version);
"""

# (version, sql) 오름차순. 추후 확장은 여기에 추가만 한다.
_MIGRATIONS = [
    (1, _MIGRATION_V1),
]


def get_conn(db_path: str = None) -> sqlite3.Connection:
    """스레드별 커넥션 반환 (WAL, FK on)."""
    path = db_path or LEDGER_DB_PATH
    key = f"conn::{path}"
    conn = getattr(_local, key, None)
    if conn is not None:
        return conn
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    setattr(_local, key, conn)
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT, note TEXT)"
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return row["v"] if row and row["v"] is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """미적용 마이그레이션을 순서대로 적용. 적용된 최종 버전 반환."""
    from datetime import datetime
    cur_v = _current_version(conn)
    for version, sql in _MIGRATIONS:
        if version <= cur_v:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at, note) VALUES (?,?,?)",
            (version, datetime.now().isoformat(), f"migration v{version}"),
        )
        conn.commit()
        cur_v = version
    return cur_v


def init_ledger(db_path: str = None) -> sqlite3.Connection:
    """커넥션 확보 + 마이그레이션 적용. 애플리케이션/테스트 진입점."""
    conn = get_conn(db_path)
    apply_migrations(conn)
    return conn
