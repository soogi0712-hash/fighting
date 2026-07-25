"""
거래 저널 DB (trading_journal.db)
=================================

목적: 매수→매도 전 주기를 구조화된 SQLite DB로 기록한다.
      기존 trade_log.json / phoenix.db / screener.db 는 건드리지 않는다.

핵심 원칙:
  1. 관측·기록 전용 — 기존 매매 판단 로직에 영향 없음.
  2. 저널 기록 실패 시 예외를 상위로 전파하지 않음. 오류 횟수 카운터로 추적.
  3. SQLite WAL 모드 + busy_timeout=5000ms → 동시 접근 충돌 방지.
  4. 모든 연결/커서 조작은 thread-local 방식으로 thread-safe 처리.
  5. payload_json 직렬화 실패 시 안전 변환 후 기록 (전체 기록 실패 없음).

체결 확인 원칙:
  - ORDER_SUBMITTED  : api.buy()/sell() 호출 직전
  - ORDER_ACCEPTED   : rt_cd="0" — KIS 접수 성공. 체결 미확인.
                       fill_price / fill_time = NULL 유지.
  - ORDER_REJECTED   : rt_cd≠"0" — KIS 거부
  - ORDER_FILLED     : 잔고 재확인 또는 체결조회로 실체결이 확인된 경우만.
                       (fill_confirmed=False인 ORDER_FILLED 이벤트 생성 금지)
  - ORDER_PARTIALLY_FILLED : 부분체결. 현재 KIS 체결조회 미연동으로 미지원.
                              잘못된 전량체결 처리를 방지하기 위해 명시적 미지원.
  - SELL_ORDER_FILLED / TRADE_CLOSED : 매도 실체결 확인 후에만.
                       접수(rt_cd=0) 직후에는 SELL_ORDER_ACCEPTED까지만 기록.
  - PRICE_HIGH_UPDATED / PRICE_LOW_UPDATED : 최소 1호가(단주 1원 이상) 갱신 시만
                       trade_events에 기록. (과도한 DB 이벤트 방지)

이벤트 타입 전체 (8+6=14):
  매수계: SIGNAL_CONFIRMED, ORDER_SUBMITTED, ORDER_ACCEPTED,
           ORDER_REJECTED, ORDER_FILLED, ORDER_PARTIALLY_FILLED(미지원)
  매도계: SELL_SIGNAL_CONFIRMED, SELL_ORDER_SUBMITTED, SELL_ORDER_ACCEPTED,
           SELL_ORDER_REJECTED, SELL_ORDER_FILLED, TRADE_CLOSED
  기타  : PRICE_HIGH_UPDATED, PRICE_LOW_UPDATED

trade_events.side : BUY / SELL — ORDER_FILLED 이벤트의 BUY/SELL 구분.
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Optional
from utils.logger import get_logger

logger = get_logger("TradingJournal")

# ── DB 경로 ────────────────────────────────────────────────────
DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "trading_journal.db"
)

# ── 이벤트 타입 상수 ───────────────────────────────────────────
class EventType:
    SIGNAL_CONFIRMED        = "SIGNAL_CONFIRMED"
    ORDER_SUBMITTED         = "ORDER_SUBMITTED"
    ORDER_ACCEPTED          = "ORDER_ACCEPTED"
    ORDER_REJECTED          = "ORDER_REJECTED"
    ORDER_FILLED            = "ORDER_FILLED"           # 실체결 확인 후만
    ORDER_PARTIALLY_FILLED  = "ORDER_PARTIALLY_FILLED" # 미지원 (KIS 체결조회 미연동)
    SELL_SIGNAL_CONFIRMED   = "SELL_SIGNAL_CONFIRMED"
    SELL_ORDER_SUBMITTED    = "SELL_ORDER_SUBMITTED"
    SELL_ORDER_ACCEPTED     = "SELL_ORDER_ACCEPTED"
    SELL_ORDER_REJECTED     = "SELL_ORDER_REJECTED"
    SELL_ORDER_FILLED       = "SELL_ORDER_FILLED"      # 실체결 확인 후만
    TRADE_CLOSED            = "TRADE_CLOSED"           # 실체결 확인 후만
    PRICE_HIGH_UPDATED      = "PRICE_HIGH_UPDATED"     # ≥1호가 갱신 시만
    PRICE_LOW_UPDATED       = "PRICE_LOW_UPDATED"      # ≥1호가 갱신 시만

# ── DDL ────────────────────────────────────────────────────────
_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

-- 진입 기록 (매수 신호 → 체결까지)
CREATE TABLE IF NOT EXISTS trade_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT    NOT NULL UNIQUE,   -- KR_005930_20260725143022123_a1b2c3d4
    market              TEXT    NOT NULL,           -- KR / US
    code                TEXT    NOT NULL,           -- 종목코드 / ticker
    name                TEXT,
    entry_type          TEXT,   -- BUY_LEVEL1_EARLY / BUY_LEVEL1_FULL / BUY_LEVEL2 / ADD / ...
    strategy_name       TEXT,   -- 전략 이름 (예: PyramidStrategy / USMomentum)
    signal_time         TEXT,   -- SIGNAL_CONFIRMED 시각 (ISO8601)
    signal_price        REAL,   -- 신호 확정 시점 현재가
    buy_score           REAL,   -- buy_score_norm (KR 0~1.0) or int (US 0~10)
    sell_score          REAL,   -- sell_score (0~27)
    rsi                 REAL,
    bb_upper            REAL,
    bb_middle           REAL,
    bb_lower            REAL,
    atr                 REAL,
    volume              REAL,
    volume_ratio        REAL,   -- 직전봉 대비 배율
    ai_total_score      REAL,   -- screener stock_scores.total_score
    rs_value            REAL,   -- screener stock_scores.rs_value
    orderable_cash      REAL,   -- 주문 직전 가용 현금
    remaining_cash_after REAL,  -- 주문 후 예상 잔여 현금 (NULL: 미확인)
    market_status       TEXT,   -- 장 상태 (정규장 / 장전시간외 등)
    order_price         REAL,   -- api.buy()에 제출한 가격
    order_qty           INTEGER,
    order_time          TEXT,   -- ORDER_SUBMITTED 시각
    -- 아래는 실체결 확인 후에만 값이 채워짐
    fill_price          REAL,   -- NULL: 미확인 (ORDER_ACCEPTED 상태)
    fill_qty            INTEGER,
    fill_time           TEXT,   -- NULL: 미확인
    fill_confirmed      INTEGER DEFAULT 0,  -- 0: 접수만(ORDER_ACCEPTED), 1: 체결확인(ORDER_FILLED)
    avg_price           REAL,   -- 포지션 취득 단가 (수수료 포함)
    buy_commission      REAL,
    entry_reason        TEXT,
    session             TEXT,
    state               TEXT    DEFAULT 'OPEN',  -- OPEN / CLOSED / REJECTED
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime'))
);

-- 청산 기록 (매도 신호 → 실체결 확인까지)
-- ★ 이 레코드는 SELL_ORDER_FILLED / TRADE_CLOSED 확인 후에만 INSERT된다.
-- ★ SELL_ORDER_ACCEPTED 단계에서는 INSERT하지 않는다.
CREATE TABLE IF NOT EXISTS trade_exits (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT    NOT NULL UNIQUE,   -- trade_entries.trade_id 참조
    market              TEXT    NOT NULL,
    code                TEXT    NOT NULL,
    name                TEXT,
    exit_reason         TEXT,
    sell_signal_time    TEXT,   -- SELL_SIGNAL_CONFIRMED 시각
    sell_signal_price   REAL,
    sell_score          REAL,
    sell_order_price    REAL,   -- api.sell()에 제출한 가격
    sell_order_qty      INTEGER,
    sell_order_time     TEXT,   -- SELL_ORDER_SUBMITTED 시각
    -- 아래는 실체결 확인 후에만 값이 채워짐
    sell_fill_price     REAL,
    sell_fill_qty       INTEGER,
    sell_fill_time      TEXT,
    sell_fill_confirmed INTEGER DEFAULT 0,
    avg_price           REAL,   -- 매수 취득 단가
    gross_profit        REAL,   -- 수수료·세금 차감 전 손익 = (sell_fill_price - avg_price) * fill_qty
    net_profit          REAL,   -- 실질 순손익 (수수료·세금 차감 후, 원 / USD)
    net_profit_pct      REAL,   -- 실질 수익률 %
    sell_commission     REAL,
    transaction_tax     REAL,
    total_fee           REAL,
    net_proceeds        REAL,
    holding_seconds     REAL,   -- 보유 시간 (초)
    highest_price       REAL,   -- 보유 중 최고가 (PRICE_HIGH_UPDATED 기준)
    lowest_price        REAL,   -- 보유 중 최저가 (PRICE_LOW_UPDATED 기준)
    max_profit_pct      REAL,   -- 보유 중 최고가 기준 단순 가격 수익률 = (highest_price-avg_price)/avg_price*100
    max_drawdown_pct    REAL,   -- 보유 중 최저가 기준 낙폭 = (lowest_price-avg_price)/avg_price*100 (음수)
    pnl_krw             REAL,   -- US: USD→KRW 환산(근사), KR: net_profit과 동일
    fx_rate             REAL,   -- US만: 적용 환율 (KR: NULL)
    is_forced           INTEGER DEFAULT 0,  -- 1: 강제 청산
    is_stoploss         INTEGER DEFAULT 0,
    pyramid_level       INTEGER,
    realized_pnl_cumul  REAL,   -- 당일 누적 실현 손익 (기록 시점)
    pnl_state           TEXT,   -- TRADING / PROFIT_LOCK / LOSS_LIMIT
    strategy_name       TEXT,   -- 전략 이름
    market_status       TEXT,   -- 장 상태
    session             TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime'))
);

-- 개별 이벤트 로그 (주문 수명주기 + 가격 갱신 등)
CREATE TABLE IF NOT EXISTS trade_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    side        TEXT,   -- BUY / SELL (ORDER_FILLED 계열의 BUY/SELL 구분, 그 외 NULL)
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime')),
    market      TEXT,
    code        TEXT,
    price       REAL,
    qty         INTEGER,
    rt_cd       TEXT,   -- KIS 응답 코드 (주문 이벤트만)
    msg1        TEXT,   -- KIS 응답 메시지
    note        TEXT,   -- 자유 텍스트 메모
    payload_json TEXT   -- 추가 데이터 JSON
);

-- 일별 요약 (TRADE_CLOSED 확정 거래만 집계)
-- ★ ORDER_ACCEPTED 상태의 미체결 거래는 포함하지 않는다.
CREATE TABLE IF NOT EXISTS daily_trade_summary (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date          TEXT    NOT NULL,   -- YYYY-MM-DD
    market              TEXT    NOT NULL,   -- KR / US / ALL
    total_trades        INTEGER DEFAULT 0,  -- TRADE_CLOSED 확정 거래 수
    winning_trades      INTEGER DEFAULT 0,  -- net_profit > 0
    losing_trades       INTEGER DEFAULT 0,  -- net_profit < 0
    break_even_trades   INTEGER DEFAULT 0,  -- net_profit == 0
    gross_profit        REAL    DEFAULT 0.0, -- 승리 거래 net_profit 합산
    gross_loss          REAL    DEFAULT 0.0, -- 패배 거래 net_profit 합산 (음수)
    commission          REAL    DEFAULT 0.0, -- 수수료 합산
    tax                 REAL    DEFAULT 0.0, -- 세금 합산
    net_profit          REAL    DEFAULT 0.0, -- gross_profit + gross_loss (= total_net_profit)
    win_rate            REAL,               -- winning_trades / total_trades * 100 (NULL if 0)
    avg_profit_pct      REAL,               -- 승리 거래 평균 수익률 (NULL if 0 wins)
    avg_loss_pct        REAL,               -- 패배 거래 평균 손실률 (NULL if 0 losses)
    profit_factor       REAL,               -- gross_profit / |gross_loss| (NULL if gross_loss=0)
    avg_holding_seconds REAL    DEFAULT 0.0,
    max_profit_trade    REAL,               -- 단일 거래 최대 수익 (net_profit 기준)
    max_loss_trade      REAL,               -- 단일 거래 최대 손실 (net_profit 기준)
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime')),
    UNIQUE(trade_date, market)
);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_te_code     ON trade_entries(code);
CREATE INDEX IF NOT EXISTS idx_te_market   ON trade_entries(market);
CREATE INDEX IF NOT EXISTS idx_te_created  ON trade_entries(created_at);
CREATE INDEX IF NOT EXISTS idx_te_state    ON trade_entries(state);
CREATE INDEX IF NOT EXISTS idx_tx_code     ON trade_exits(code);
CREATE INDEX IF NOT EXISTS idx_tx_created  ON trade_exits(created_at);
CREATE INDEX IF NOT EXISTS idx_ev_trade_id ON trade_events(trade_id);
CREATE INDEX IF NOT EXISTS idx_ev_type     ON trade_events(event_type);
CREATE INDEX IF NOT EXISTS idx_ev_ts       ON trade_events(ts);
CREATE INDEX IF NOT EXISTS idx_ds_date     ON daily_trade_summary(trade_date);
"""

# ── Thread-local 연결 ──────────────────────────────────────────
_local = threading.local()

def _get_conn() -> sqlite3.Connection:
    """thread-local SQLite 연결 반환 (없으면 생성)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return _local.conn


def _init_db():
    """스키마 초기화 + 마이그레이션 (모듈 import 시 1회 실행).

    전략:
    1. CREATE TABLE IF NOT EXISTS — 신규 테이블 생성 (이미 존재하면 무시)
    2. ALTER TABLE ADD COLUMN IF NOT EXISTS — 기존 테이블에 누락 컬럼 추가
    3. daily_trade_summary 가 구버전(date 컬럼) 이면 DROP 후 재생성
       (거래 저널 외부 연결 없는 내부 집계 테이블이므로 안전)
    4. 인덱스 CREATE INDEX IF NOT EXISTS — 중복 생성 없음
    """
    conn = _get_conn()

    # ── 3. daily_trade_summary 구버전 감지 → 재생성 ──────────
    _dts_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_trade_summary)").fetchall()}
    if _dts_cols and "trade_date" not in _dts_cols:
        # 구버전(date 컬럼 사용) 감지 → DROP 후 신규 DDL로 재생성
        logger.warning(
            "[Journal] daily_trade_summary 구버전 스키마 감지 → DROP & RECREATE "
            "(집계 전용 테이블, 데이터 없이 재생성)"
        )
        conn.execute("DROP TABLE IF EXISTS daily_trade_summary")
        conn.commit()

    # ── 1+2. 테이블 생성 및 누락 컬럼 추가 ──────────────────
    conn.executescript(_DDL)

    # ── ALTER TABLE: trade_entries 누락 컬럼 추가 ───────────
    _te_cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_entries)").fetchall()}
    _te_add = [
        ("strategy_name",        "TEXT"),
        ("remaining_cash_after", "REAL"),
        ("market_status",        "TEXT"),
    ]
    for col, typ in _te_add:
        if col not in _te_cols:
            conn.execute(f"ALTER TABLE trade_entries ADD COLUMN {col} {typ}")
            logger.info(f"[Journal] trade_entries: 컬럼 추가 → {col} {typ}")

    # ── ALTER TABLE: trade_exits 누락 컬럼 추가 ─────────────
    _tx_cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_exits)").fetchall()}
    _tx_add = [
        ("gross_profit",   "REAL"),
        ("max_profit_pct", "REAL"),
        ("strategy_name",  "TEXT"),
        ("market_status",  "TEXT"),
    ]
    for col, typ in _tx_add:
        if col not in _tx_cols:
            conn.execute(f"ALTER TABLE trade_exits ADD COLUMN {col} {typ}")
            logger.info(f"[Journal] trade_exits: 컬럼 추가 → {col} {typ}")

    # ── ALTER TABLE: trade_events 누락 컬럼 추가 ────────────
    _ev_cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_events)").fetchall()}
    if "side" not in _ev_cols:
        conn.execute("ALTER TABLE trade_events ADD COLUMN side TEXT")
        logger.info("[Journal] trade_events: 컬럼 추가 → side TEXT")

    conn.commit()
    logger.debug("[Journal] _init_db 완료")


# ── 안전 JSON 직렬화 ──────────────────────────────────────────
def _safe_json(obj) -> Optional[str]:
    """JSON 직렬화. 실패하는 키는 str()로 fallback."""
    if obj is None:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        # 직렬화 실패 키를 str()로 변환
        safe = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                try:
                    json.dumps(v)
                    safe[k] = v
                except (TypeError, ValueError):
                    safe[k] = str(v)
        else:
            safe = {"_raw": str(obj)}
        try:
            return json.dumps(safe, ensure_ascii=False)
        except Exception:
            return None


# ── 오류 카운터 ──────────────────────────────────────────────
_error_counter: dict[str, int] = {}


def _inc_error(key: str, exc: Exception):
    """오류 횟수 카운터 증가. 로그 출력 후 예외는 삼킨다."""
    _error_counter[key] = _error_counter.get(key, 0) + 1
    logger.error(f"[Journal] {key} 오류 #{_error_counter[key]}: {type(exc).__name__}: {exc}")


def get_error_counts() -> dict:
    """오류 카운터 현황 반환. 조용한 무시 방지용."""
    return dict(_error_counter)


# ── trade_id 생성 ──────────────────────────────────────────────
def make_trade_id(market: str, code: str) -> str:
    """KR_005930_20260725143022123_a1b2c3d4 형식의 거래 고유 ID."""
    ts17 = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]  # 17자리 (ms 포함)
    uid8 = uuid.uuid4().hex[:8]
    return f"{market}_{code}_{ts17}_{uid8}"


# ─────────────────────────────────────────────────────────────
# 매수 계열 기록 함수
# ─────────────────────────────────────────────────────────────

def record_signal(
    trade_id: str,
    market: str,
    code: str,
    name: str,
    entry_type: str,
    signal_price: float,
    buy_score: float,
    sell_score: float,
    rsi: Optional[float],
    bb_upper: Optional[float],
    bb_middle: Optional[float],
    bb_lower: Optional[float],
    atr: Optional[float],
    volume: Optional[float],
    volume_ratio: Optional[float],
    ai_total_score: Optional[float],
    rs_value: Optional[float],
    orderable_cash: Optional[float],
    session: str,
    entry_reason: str,
    strategy_name: Optional[str] = None,
    market_status: Optional[str] = None,
    remaining_cash_after: Optional[float] = None,
    payload: Optional[dict] = None,
) -> bool:
    """SIGNAL_CONFIRMED: 매수 신호 확정. trade_entries 최초 INSERT."""
    try:
        conn = _get_conn()
        now  = datetime.now().isoformat()
        conn.execute(
            """
            INSERT INTO trade_entries
              (trade_id, market, code, name, entry_type, strategy_name,
               signal_time, signal_price,
               buy_score, sell_score,
               rsi, bb_upper, bb_middle, bb_lower, atr,
               volume, volume_ratio, ai_total_score, rs_value,
               orderable_cash, remaining_cash_after, market_status,
               entry_reason, session, state, created_at, updated_at)
            VALUES
              (?,?,?,?,?,?, ?,?, ?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?,?)
            """,
            (
                trade_id, market, code, name, entry_type, strategy_name,
                now, signal_price,
                buy_score, sell_score,
                rsi, bb_upper, bb_middle, bb_lower, atr,
                volume, volume_ratio, ai_total_score, rs_value,
                orderable_cash, remaining_cash_after, market_status,
                entry_reason, session, "OPEN", now, now,
            ),
        )
        conn.execute(
            "INSERT INTO trade_events(trade_id,event_type,side,market,code,price,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (trade_id, EventType.SIGNAL_CONFIRMED, "BUY", market, code, signal_price,
             f"entry_type={entry_type}|reason={entry_reason}", _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_signal", e)
        return False


def record_order_submitted(
    trade_id: str,
    market: str,
    code: str,
    order_price: float,
    order_qty: int,
    payload: Optional[dict] = None,
) -> bool:
    """ORDER_SUBMITTED: api.buy() 호출 직전."""
    try:
        conn = _get_conn()
        now  = datetime.now().isoformat()
        conn.execute(
            "UPDATE trade_entries SET order_price=?, order_qty=?, order_time=?, updated_at=?"
            " WHERE trade_id=?",
            (order_price, order_qty, now, now, trade_id),
        )
        conn.execute(
            "INSERT INTO trade_events(trade_id,event_type,side,market,code,price,qty,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (trade_id, EventType.ORDER_SUBMITTED, "BUY", market, code,
             order_price, order_qty, f"order_price={order_price}|order_qty={order_qty}",
             _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_order_submitted", e)
        return False


def record_order_accepted(
    trade_id: str,
    market: str,
    code: str,
    rt_cd: str,
    msg1: str,
    payload: Optional[dict] = None,
) -> bool:
    """ORDER_ACCEPTED: rt_cd="0" 수신. 접수 성공. 체결 미확인.
    ★ fill_price / fill_time 은 NULL 유지.
    ★ ORDER_FILLED 는 실체결 확인 후에만 기록한다.
    """
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO trade_events"
            "(trade_id,event_type,side,market,code,rt_cd,msg1,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (trade_id, EventType.ORDER_ACCEPTED, "BUY", market, code,
             rt_cd, msg1,
             "접수성공_체결미확인: fill_price/fill_time=NULL",
             _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_order_accepted", e)
        return False


def record_order_rejected(
    trade_id: str,
    market: str,
    code: str,
    rt_cd: str,
    msg1: str,
    payload: Optional[dict] = None,
) -> bool:
    """ORDER_REJECTED: rt_cd≠"0". 주문 거부. state=REJECTED."""
    try:
        conn = _get_conn()
        now  = datetime.now().isoformat()
        conn.execute(
            "UPDATE trade_entries SET state='REJECTED', updated_at=? WHERE trade_id=?",
            (now, trade_id),
        )
        conn.execute(
            "INSERT INTO trade_events"
            "(trade_id,event_type,side,market,code,rt_cd,msg1,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (trade_id, EventType.ORDER_REJECTED, "BUY", market, code,
             rt_cd, msg1, _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_order_rejected", e)
        return False


def record_order_filled(
    trade_id: str,
    market: str,
    code: str,
    fill_price: float,
    fill_qty: int,
    avg_price: float,
    buy_commission: Optional[float] = None,
    fill_confirmed: bool = True,
    payload: Optional[dict] = None,
) -> bool:
    """ORDER_FILLED: 실체결이 확인된 경우에만 호출.
    ★ fill_confirmed=False 로는 호출하지 않는다.
       잔고 재확인 또는 체결조회로 확인된 경우에만 기록.
    side='BUY'.
    """
    if not fill_confirmed:
        # 접수 성공(rt_cd=0)만으로는 ORDER_FILLED 기록 금지
        logger.warning(
            f"[Journal] record_order_filled 무시: trade_id={trade_id} "
            f"fill_confirmed=False → ORDER_ACCEPTED 상태 유지"
        )
        return False
    try:
        conn = _get_conn()
        now  = datetime.now().isoformat()
        conn.execute(
            """UPDATE trade_entries
               SET fill_price=?, fill_qty=?, fill_time=?, fill_confirmed=1,
                   avg_price=?, buy_commission=?, updated_at=?
               WHERE trade_id=?""",
            (fill_price, fill_qty, now, avg_price, buy_commission, now, trade_id),
        )
        conn.execute(
            "INSERT INTO trade_events"
            "(trade_id,event_type,side,market,code,price,qty,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (trade_id, EventType.ORDER_FILLED, "BUY", market, code,
             fill_price, fill_qty,
             f"fill_confirmed=True|avg_price={avg_price}",
             _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_order_filled", e)
        return False


def record_price_high(
    trade_id: str,
    market: str,
    code: str,
    new_high: float,
    prev_high: float = 0.0,
    payload: Optional[dict] = None,
) -> bool:
    """PRICE_HIGH_UPDATED: 보유 중 최고가 갱신.
    ★ 최소 1원(1호가) 이상 갱신된 경우에만 trade_events에 기록.
       가격 갱신 자체(pyramid_strategy.update_high)는 이 함수와 무관하게 항상 수행.
    """
    if prev_high > 0 and (new_high - prev_high) < 1.0:
        return False  # 1호가 미만 갱신: 이벤트 기록 생략
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO trade_events"
            "(trade_id,event_type,side,market,code,price,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (trade_id, EventType.PRICE_HIGH_UPDATED, None, market, code,
             new_high,
             f"prev={prev_high:.0f}→new={new_high:.0f}(+{new_high-prev_high:.0f})",
             _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_price_high", e)
        return False


def record_price_low(
    trade_id: str,
    market: str,
    code: str,
    new_low: float,
    prev_low: float = 0.0,
    payload: Optional[dict] = None,
) -> bool:
    """PRICE_LOW_UPDATED: 보유 중 최저가 갱신.
    ★ 최소 1원(1호가) 이상 갱신된 경우에만 trade_events에 기록.
    """
    if prev_low > 0 and (prev_low - new_low) < 1.0:
        return False  # 1호가 미만 갱신: 이벤트 기록 생략
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO trade_events"
            "(trade_id,event_type,side,market,code,price,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (trade_id, EventType.PRICE_LOW_UPDATED, None, market, code,
             new_low,
             f"prev={prev_low:.0f}→new={new_low:.0f}(-{prev_low-new_low:.0f})",
             _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_price_low", e)
        return False


# ─────────────────────────────────────────────────────────────
# 매도 계열 기록 함수
# ─────────────────────────────────────────────────────────────

def record_sell_signal(
    trade_id: str,
    market: str,
    code: str,
    sell_price: float,
    sell_score: float,
    exit_reason: str,
    payload: Optional[dict] = None,
) -> bool:
    """SELL_SIGNAL_CONFIRMED: 매도 신호 확정."""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO trade_events"
            "(trade_id,event_type,side,market,code,price,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (trade_id, EventType.SELL_SIGNAL_CONFIRMED, "SELL", market, code,
             sell_price, f"exit_reason={exit_reason}|sell_score={sell_score}",
             _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_sell_signal", e)
        return False


def record_sell_order_submitted(
    trade_id: str,
    market: str,
    code: str,
    sell_price: float,
    sell_qty: int,
    payload: Optional[dict] = None,
) -> bool:
    """SELL_ORDER_SUBMITTED: api.sell() 호출 직전."""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO trade_events"
            "(trade_id,event_type,side,market,code,price,qty,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (trade_id, EventType.SELL_ORDER_SUBMITTED, "SELL", market, code,
             sell_price, sell_qty,
             f"sell_price={sell_price}|sell_qty={sell_qty}",
             _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_sell_order_submitted", e)
        return False


def record_sell_order_accepted(
    trade_id: str,
    market: str,
    code: str,
    rt_cd: str,
    msg1: str,
    payload: Optional[dict] = None,
) -> bool:
    """SELL_ORDER_ACCEPTED: 매도 주문 rt_cd="0" 접수 성공.
    ★ 체결 미확인. trade_exits INSERT 금지. trade_entries.state 변경 금지.
       TRADE_CLOSED 이벤트 생성 금지.
    """
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO trade_events"
            "(trade_id,event_type,side,market,code,rt_cd,msg1,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (trade_id, EventType.SELL_ORDER_ACCEPTED, "SELL", market, code,
             rt_cd, msg1,
             "매도접수성공_체결미확인: trade_exits/CLOSED 기록 대기",
             _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_sell_order_accepted", e)
        return False


def record_sell_order_rejected(
    trade_id: str,
    market: str,
    code: str,
    rt_cd: str,
    msg1: str,
    payload: Optional[dict] = None,
) -> bool:
    """SELL_ORDER_REJECTED: 매도 주문 거부."""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO trade_events(trade_id,event_type,side,market,code,rt_cd,msg1,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (trade_id, EventType.SELL_ORDER_REJECTED, "SELL", market, code,
             rt_cd, msg1, _safe_json(payload)),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_sell_order_rejected", e)
        return False


def record_sell_order_filled(
    trade_id: str,
    market: str,
    code: str,
    sell_fill_price: Optional[float],
    sell_fill_qty: Optional[int],
    fill_confirmed: bool = True,
    payload: Optional[dict] = None,
) -> bool:
    """SELL_ORDER_FILLED: 매도 실체결 확인 후에만 호출.
    ★ fill_confirmed=False 로는 호출하지 않는다.
    side='SELL'.
    """
    if not fill_confirmed:
        logger.warning(
            f"[Journal] record_sell_order_filled 무시: trade_id={trade_id} "
            f"fill_confirmed=False → SELL_ORDER_ACCEPTED 상태 유지"
        )
        return False
    try:
        conn = _get_conn()
        now  = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO trade_events"
            "(trade_id,event_type,side,market,code,price,qty,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (trade_id, EventType.SELL_ORDER_FILLED, "SELL", market, code,
             sell_fill_price, sell_fill_qty,
             f"sell_fill_confirmed=True",
             _safe_json(payload)),
        )
        # trade_exits 가 이미 존재하면 fill 정보만 업데이트
        conn.execute(
            """UPDATE trade_exits
               SET sell_fill_price=COALESCE(?,sell_fill_price),
                   sell_fill_qty=COALESCE(?,sell_fill_qty),
                   sell_fill_time=COALESCE(?,sell_fill_time),
                   sell_fill_confirmed=1
               WHERE trade_id=?""",
            (sell_fill_price, sell_fill_qty, now, trade_id),
        )
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_sell_order_filled", e)
        return False


def record_trade_closed(
    trade_id: str,
    market: str,
    code: str,
    name: str,
    exit_reason: str,
    sell_signal_price: float,
    sell_order_price: float,
    sell_order_qty: int,
    sell_fill_price: Optional[float],
    sell_fill_qty: Optional[int],
    sell_fill_confirmed: bool,
    avg_price: float,
    net_profit: float,
    net_profit_pct: float,
    sell_commission: Optional[float],
    transaction_tax: Optional[float],
    total_fee: Optional[float],
    net_proceeds: Optional[float],
    holding_seconds: float,
    highest_price: Optional[float],
    lowest_price: Optional[float],
    pnl_krw: Optional[float],
    fx_rate: Optional[float],
    is_forced: bool,
    is_stoploss: bool,
    pyramid_level: Optional[int],
    realized_pnl_cumul: Optional[float],
    pnl_state: Optional[str],
    session: str,
    sell_score: Optional[float] = None,
    strategy_name: Optional[str] = None,
    market_status: Optional[str] = None,
    payload: Optional[dict] = None,
) -> bool:
    """TRADE_CLOSED: 매도 실체결 완료 확인 후에만 호출.
    trade_exits INSERT + trade_entries state='CLOSED' + daily_trade_summary 업데이트.

    ★ rt_cd="0" 접수 직후에는 호출하지 않는다.
       실제 체결이 확인된 후에만 호출한다.

    max_profit_pct  = (highest_price - avg_price) / avg_price * 100 (단순 가격 수익률)
    max_drawdown_pct = (lowest_price - avg_price) / avg_price * 100 (음수)
    gross_profit    = (sell_fill_price - avg_price) * sell_fill_qty (수수료 차감 전)
    """
    try:
        conn  = _get_conn()
        now   = datetime.now().isoformat()
        today = datetime.now().strftime("%Y-%m-%d")

        # max_drawdown_pct, max_profit_pct, gross_profit 계산
        max_dd  = None
        max_pft = None
        gross_p = None
        if avg_price and avg_price > 0:
            if lowest_price:
                max_dd  = (lowest_price - avg_price) / avg_price * 100
            if highest_price:
                max_pft = (highest_price - avg_price) / avg_price * 100
        if sell_fill_price and avg_price and sell_fill_qty:
            gross_p = (sell_fill_price - avg_price) * sell_fill_qty

        sell_fill_time = now if sell_fill_confirmed else None

        conn.execute(
            """
            INSERT INTO trade_exits
              (trade_id, market, code, name,
               exit_reason, sell_signal_time, sell_signal_price,
               sell_score,
               sell_order_price, sell_order_qty, sell_order_time,
               sell_fill_price, sell_fill_qty, sell_fill_time, sell_fill_confirmed,
               avg_price,
               gross_profit, net_profit, net_profit_pct,
               sell_commission, transaction_tax, total_fee, net_proceeds,
               holding_seconds, highest_price, lowest_price,
               max_profit_pct, max_drawdown_pct,
               pnl_krw, fx_rate,
               is_forced, is_stoploss, pyramid_level,
               realized_pnl_cumul, pnl_state,
               strategy_name, market_status, session, created_at)
            VALUES
              (?,?,?,?,  ?,?,?,  ?,  ?,?,?,  ?,?,?,?,  ?,
               ?,?,?,  ?,?,?,?,  ?,?,?,  ?,?,  ?,?,
               ?,?,?,  ?,?,  ?,?,?,?)
            ON CONFLICT(trade_id) DO UPDATE SET
              sell_fill_price=excluded.sell_fill_price,
              sell_fill_time=excluded.sell_fill_time,
              sell_fill_confirmed=excluded.sell_fill_confirmed,
              gross_profit=excluded.gross_profit,
              net_profit=excluded.net_profit,
              net_profit_pct=excluded.net_profit_pct,
              max_profit_pct=excluded.max_profit_pct,
              max_drawdown_pct=excluded.max_drawdown_pct
            """,
            (
                trade_id, market, code, name,
                exit_reason, now, sell_signal_price,
                sell_score,
                sell_order_price, sell_order_qty, now,
                sell_fill_price, sell_fill_qty, sell_fill_time,
                1 if sell_fill_confirmed else 0,
                avg_price,
                gross_p, net_profit, net_profit_pct,
                sell_commission, transaction_tax, total_fee, net_proceeds,
                holding_seconds, highest_price, lowest_price,
                max_pft, max_dd,
                pnl_krw, fx_rate,
                1 if is_forced else 0,
                1 if is_stoploss else 0,
                pyramid_level,
                realized_pnl_cumul, pnl_state,
                strategy_name, market_status, session, now,
            ),
        )
        # trade_entries 상태 CLOSED로 변경
        conn.execute(
            "UPDATE trade_entries SET state='CLOSED', updated_at=? WHERE trade_id=?",
            (now, trade_id),
        )
        # TRADE_CLOSED 이벤트 기록 (side='SELL')
        conn.execute(
            "INSERT INTO trade_events"
            "(trade_id,event_type,side,market,code,price,qty,note,payload_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (trade_id, EventType.TRADE_CLOSED, "SELL", market, code,
             sell_fill_price or sell_order_price, sell_order_qty,
             f"net_pct={net_profit_pct:+.2f}%|{exit_reason}",
             _safe_json(payload)),
        )
        # daily_trade_summary 업데이트 (확정 거래만)
        _upsert_daily_summary(conn, today, market, net_profit_pct, net_profit,
                              holding_seconds,
                              sell_commission or 0.0,
                              transaction_tax or 0.0)
        conn.commit()
        return True
    except Exception as e:
        _inc_error("record_trade_closed", e)
        return False


# ─────────────────────────────────────────────────────────────
# daily_trade_summary UPSERT
# ─────────────────────────────────────────────────────────────

def _upsert_daily_summary(
    conn: sqlite3.Connection,
    date: str,
    market: str,
    net_profit_pct: float,
    net_profit: float,
    holding_seconds: float,
    commission: float = 0.0,
    tax: float = 0.0,
):
    """daily_trade_summary UPSERT.
    ★ TRADE_CLOSED 확정 거래만 집계. 커밋은 호출자가 처리.
    - win  : net_profit > 0
    - loss : net_profit < 0
    - break_even : net_profit == 0
    - profit_factor = gross_profit / |gross_loss|  (gross_loss=0이면 NULL)
    """
    now    = datetime.now().isoformat()
    is_win  = net_profit > 0
    is_loss = net_profit < 0

    existing = conn.execute(
        "SELECT * FROM daily_trade_summary WHERE trade_date=? AND market=?",
        (date, market),
    ).fetchone()

    if existing is None:
        # 신규 INSERT
        gross_p = net_profit if is_win  else 0.0
        gross_l = net_profit if is_loss else 0.0
        win_cnt  = 1 if is_win  else 0
        loss_cnt = 1 if is_loss else 0
        be_cnt   = 0 if (is_win or is_loss) else 1
        pf       = None if gross_l == 0.0 else (gross_p / abs(gross_l))
        win_rate = (win_cnt / 1) * 100 if True else None
        avg_pft  = net_profit_pct if is_win  else None
        avg_loss = net_profit_pct if is_loss else None
        conn.execute(
            """
            INSERT INTO daily_trade_summary
              (trade_date, market,
               total_trades, winning_trades, losing_trades, break_even_trades,
               gross_profit, gross_loss,
               commission, tax,
               net_profit,
               win_rate, avg_profit_pct, avg_loss_pct, profit_factor,
               avg_holding_seconds,
               max_profit_trade, max_loss_trade,
               created_at, updated_at)
            VALUES (?,?, ?,?,?,?, ?,?, ?,?, ?, ?,?,?,?, ?, ?,?, ?,?)
            """,
            (
                date, market,
                1, win_cnt, loss_cnt, be_cnt,
                gross_p, gross_l,
                commission, tax,
                net_profit,
                win_rate, avg_pft, avg_loss, pf,
                holding_seconds,
                net_profit if is_win  else None,
                net_profit if is_loss else None,
                now, now,
            ),
        )
    else:
        e = existing
        total   = e["total_trades"] + 1
        win_cnt  = e["winning_trades"]  + (1 if is_win  else 0)
        loss_cnt = e["losing_trades"]   + (1 if is_loss else 0)
        be_cnt   = e["break_even_trades"]+ (0 if (is_win or is_loss) else 1)
        gross_p  = (e["gross_profit"] or 0.0) + (net_profit if is_win  else 0.0)
        gross_l  = (e["gross_loss"]   or 0.0) + (net_profit if is_loss else 0.0)
        comm     = (e["commission"] or 0.0) + commission
        tx       = (e["tax"]        or 0.0) + tax
        total_np = (e["net_profit"] or 0.0) + net_profit
        pf       = None if gross_l == 0.0 else (gross_p / abs(gross_l))
        win_rate = (win_cnt / total * 100) if total > 0 else None
        avg_pft  = (gross_p / win_cnt)  if win_cnt  > 0 else None
        avg_loss_val = (gross_l / loss_cnt) if loss_cnt > 0 else None
        avg_hold = ((e["avg_holding_seconds"] or 0.0) * e["total_trades"] + holding_seconds) / total
        max_pft_tr  = max(e["max_profit_trade"] or float("-inf"), net_profit if is_win  else float("-inf"))
        max_loss_tr = min(e["max_loss_trade"]   or float("inf"),  net_profit if is_loss else float("inf"))
        conn.execute(
            """
            UPDATE daily_trade_summary SET
              total_trades=?, winning_trades=?, losing_trades=?, break_even_trades=?,
              gross_profit=?, gross_loss=?,
              commission=?, tax=?,
              net_profit=?,
              win_rate=?, avg_profit_pct=?, avg_loss_pct=?, profit_factor=?,
              avg_holding_seconds=?,
              max_profit_trade=?, max_loss_trade=?,
              updated_at=?
            WHERE trade_date=? AND market=?
            """,
            (
                total, win_cnt, loss_cnt, be_cnt,
                gross_p, gross_l,
                comm, tx,
                total_np,
                win_rate, avg_pft, avg_loss_val, pf,
                avg_hold,
                max_pft_tr if max_pft_tr != float("-inf") else None,
                max_loss_tr if max_loss_tr != float("inf") else None,
                now, date, market,
            ),
        )


# ─────────────────────────────────────────────────────────────
# 조회 API
# ─────────────────────────────────────────────────────────────

def query_journal(
    market: Optional[str] = None,
    code: Optional[str] = None,
    state: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """trade_entries 목록 조회."""
    try:
        conn   = _get_conn()
        wheres = []
        params = []
        if market:
            wheres.append("market=?"); params.append(market)
        if code:
            wheres.append("code=?");   params.append(code)
        if state:
            wheres.append("state=?");  params.append(state)
        if date_from:
            wheres.append("created_at >= ?"); params.append(date_from)
        if date_to:
            wheres.append("created_at <= ?"); params.append(date_to + "T23:59:59")
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params += [limit, offset]
        rows = conn.execute(
            f"SELECT * FROM trade_entries {where_sql}"
            f" ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _inc_error("query_journal", e)
        return []


def query_journal_detail(trade_id: str) -> Optional[dict]:
    """trade_id 상세 조회 (entry + exit + events)."""
    try:
        conn  = _get_conn()
        entry = conn.execute(
            "SELECT * FROM trade_entries WHERE trade_id=?", (trade_id,)
        ).fetchone()
        if entry is None:
            return None
        exit_ = conn.execute(
            "SELECT * FROM trade_exits WHERE trade_id=?", (trade_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT * FROM trade_events WHERE trade_id=? ORDER BY ts",
            (trade_id,),
        ).fetchall()
        return {
            "entry":  dict(entry),
            "exit":   dict(exit_) if exit_ else None,
            "events": [dict(e) for e in events],
        }
    except Exception as e:
        _inc_error("query_journal_detail", e)
        return None


def query_daily_summary(
    date: Optional[str] = None,
    market: Optional[str] = None,
    limit: int = 30,
) -> list:
    """daily_trade_summary 조회."""
    try:
        conn   = _get_conn()
        wheres = []
        params = []
        if date:
            wheres.append("trade_date=?"); params.append(date)
        if market:
            wheres.append("market=?"); params.append(market)
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM daily_trade_summary {where_sql}"
            f" ORDER BY trade_date DESC, market LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _inc_error("query_daily_summary", e)
        return []


# ── 모듈 import 시 자동 초기화 ────────────────────────────────
_init_db()
