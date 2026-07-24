"""SQLite 스키마 정의 및 버전 관리.

- events: append-only 권위 원천 (UNIQUE idempotency_key)
- positions / order_index / daily_pnl: projection (언제든 events 재생으로 재구축 가능)
- schema_meta / engine_state / processed_watermark / snapshot_meta: 메타/제어

마이그레이션은 MIGRATIONS 리스트에 버전별 DDL 을 추가하는 방식으로 확장한다.
다운그레이드(코드보다 높은 버전 DB 열기)는 금지한다.
"""

SCHEMA_VERSION = 1

_V1 = """
CREATE TABLE IF NOT EXISTS schema_meta (
  id      INTEGER PRIMARY KEY CHECK(id = 1),
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uuid      TEXT    NOT NULL UNIQUE,
  ts              TEXT    NOT NULL,
  type            TEXT    NOT NULL,
  aggregate_type  TEXT    NOT NULL,
  aggregate_id    TEXT    NOT NULL,
  client_order_id TEXT,
  odno            TEXT,
  code            TEXT,
  side            TEXT,
  qty             INTEGER,
  price           REAL,
  cum_filled_qty  INTEGER,
  realized_pnl    REAL,
  idempotency_key TEXT UNIQUE,
  payload         TEXT,
  schema_ver      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_events_order ON events(client_order_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_code  ON events(code, seq);

CREATE TABLE IF NOT EXISTS positions (
  code              TEXT PRIMARY KEY,
  qty               INTEGER NOT NULL DEFAULT 0,
  avg_price         REAL    NOT NULL DEFAULT 0,
  cost_basis        REAL    NOT NULL DEFAULT 0,
  balance_confirmed INTEGER NOT NULL DEFAULT 0,   -- 브로커 잔고 확증 여부(SELL 게이트)
  updated_seq       INTEGER
);

CREATE TABLE IF NOT EXISTS order_index (
  client_order_id TEXT PRIMARY KEY,
  odno            TEXT,
  orgn_odno       TEXT,
  code            TEXT,
  side            TEXT,
  ord_qty         INTEGER NOT NULL DEFAULT 0,
  applied_qty     INTEGER NOT NULL DEFAULT 0,     -- 이미 포지션 반영한 누적 체결수량(watermark)
  state           TEXT    NOT NULL DEFAULT 'INTENT',
  updated_seq     INTEGER
);
CREATE INDEX IF NOT EXISTS ix_order_odno ON order_index(odno);

CREATE TABLE IF NOT EXISTS daily_pnl (
  session_key  TEXT PRIMARY KEY,
  realized_pnl REAL    NOT NULL DEFAULT 0,
  peak_pnl     REAL    NOT NULL DEFAULT 0,
  trades       INTEGER NOT NULL DEFAULT 0,
  risk_state   TEXT    NOT NULL DEFAULT 'TRADING',
  updated_seq  INTEGER
);

CREATE TABLE IF NOT EXISTS engine_state (
  id               INTEGER PRIMARY KEY CHECK(id = 1),
  safe_halt        INTEGER NOT NULL DEFAULT 0,
  safe_halt_reason TEXT,
  recovery_state   TEXT    NOT NULL DEFAULT 'RECOVERING',  -- 부팅마다 재게이트(이벤트 재생 아님)
  session_key      TEXT    NOT NULL DEFAULT 'INIT'
);

CREATE TABLE IF NOT EXISTS processed_watermark (
  id       INTEGER PRIMARY KEY CHECK(id = 1),
  last_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshot_meta (
  id       INTEGER PRIMARY KEY CHECK(id = 1),
  last_seq INTEGER NOT NULL DEFAULT 0,
  blob     TEXT
);
"""

# 버전 index = 목표 스키마 버전. MIGRATIONS[0] 은 v1.
MIGRATIONS = [_V1]


def initial_rows_sql() -> list[str]:
    """싱글턴 메타 행 초기화(존재하지 않을 때만)."""
    return [
        "INSERT OR IGNORE INTO engine_state(id, safe_halt, recovery_state, session_key) "
        "VALUES (1, 0, 'RECOVERING', 'INIT')",
        "INSERT OR IGNORE INTO processed_watermark(id, last_seq) VALUES (1, 0)",
        "INSERT OR IGNORE INTO snapshot_meta(id, last_seq, blob) VALUES (1, 0, NULL)",
    ]
