"""
전략 실험실 DB — SQLite 영속 저장
=====================================

테이블 구성:
  lab_trades       — 가상 거래 기록 (전략별 BUY/SELL/ADD_BUY)
  lab_equity       — 일별 자산 곡선 스냅샷 (전략별)
  lab_rankings     — 주간 랭킹 스냅샷 (전략별 점수 + 메트릭)
  lab_tier_history — 계층 변경 이력 (EXPERIMENT→CANDIDATE→LIVE)
  lab_ai_recs      — AI 추천 기록 (주간 등급 + 요약)
  lab_regime       — 시장 국면 기록 (BULL/BEAR/LATERAL)

★ 설계 원칙 ★
  - JSON 파일 대신 SQLite 를 주 저장소로 활용 (조회/집계 가능)
  - lab_engine.py 는 JSON 파일도 계속 유지 (호환성)
  - 모든 CRUD 는 이 모듈을 통해서만 수행
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sqlite3
import json
import threading
from contextlib import contextmanager
from datetime import date, datetime
from typing import Optional, List, Dict

from utils.logger import get_logger

logger = get_logger("LabDB")

# DB 경로
LAB_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "lab.db"
)


# ──────────────────────────────────────────────────────────────
# 연결 관리
# ──────────────────────────────────────────────────────────────

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """스레드-로컬 SQLite 연결 반환 (없으면 생성)"""
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(os.path.dirname(LAB_DB_PATH), exist_ok=True)
        conn = sqlite3.connect(LAB_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


@contextmanager
def _tx():
    """트랜잭션 컨텍스트 매니저"""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[LabDB] 트랜잭션 롤백: {e}")
        raise


# ──────────────────────────────────────────────────────────────
# 스키마 초기화
# ──────────────────────────────────────────────────────────────

def init_db():
    """
    DB 테이블 생성.
    앱 시작 시 한 번 호출. 이미 존재하면 무시(CREATE IF NOT EXISTS).
    """
    conn = _get_conn()
    with _tx():
        conn.executescript("""
        -- ─────────────────────────────────────────────────────
        -- 1. 가상 거래 기록
        -- ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS lab_trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id  TEXT    NOT NULL,       -- S1, T3, P2, BASE ...
            code         TEXT    NOT NULL,       -- 종목코드
            name         TEXT    NOT NULL,       -- 종목명
            action       TEXT    NOT NULL,       -- BUY / SELL / ADD_BUY
            price        REAL    NOT NULL,
            qty          INTEGER NOT NULL,
            amount       REAL    NOT NULL,       -- price × qty
            trade_date   TEXT    NOT NULL,       -- YYYY-MM-DD
            profit       REAL    DEFAULT 0,      -- 실현손익 (SELL 시)
            profit_pct   REAL    DEFAULT 0,      -- 수익률 %
            hold_days    INTEGER DEFAULT 0,      -- 보유일수 (SELL 시)
            reason       TEXT    DEFAULT '',     -- 매매 사유
            created_at   TEXT    DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_lt_strategy ON lab_trades(strategy_id);
        CREATE INDEX IF NOT EXISTS idx_lt_date     ON lab_trades(trade_date);
        CREATE INDEX IF NOT EXISTS idx_lt_code     ON lab_trades(code);

        -- ─────────────────────────────────────────────────────
        -- 2. 일별 자산 곡선
        -- ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS lab_equity (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id  TEXT    NOT NULL,
            snap_date    TEXT    NOT NULL,       -- YYYY-MM-DD
            equity       REAL    NOT NULL,       -- 총 자산 (현금 + 평가액)
            cash         REAL    DEFAULT 0,      -- 보유 현금
            position_cnt INTEGER DEFAULT 0,     -- 보유 종목 수
            UNIQUE (strategy_id, snap_date)
        );
        CREATE INDEX IF NOT EXISTS idx_le_strategy ON lab_equity(strategy_id);
        CREATE INDEX IF NOT EXISTS idx_le_date     ON lab_equity(snap_date);

        -- ─────────────────────────────────────────────────────
        -- 3. 주간 랭킹 스냅샷
        -- ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS lab_rankings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            calc_date    TEXT    NOT NULL,       -- YYYY-MM-DD (월요일)
            strategy_id  TEXT    NOT NULL,
            rank_no      INTEGER NOT NULL,       -- 순위
            score        REAL    NOT NULL,       -- 종합점수 (0-100)
            tier         TEXT    NOT NULL,       -- LIVE/CANDIDATE/EXPERIMENT
            regime       TEXT    DEFAULT 'LATERAL', -- 시장 국면
            metrics_json TEXT    DEFAULT '{}',   -- 전체 메트릭 JSON
            ai_grade     TEXT    DEFAULT '',     -- A/B/C/D/F
            UNIQUE (calc_date, strategy_id)
        );
        CREATE INDEX IF NOT EXISTS idx_lr_date     ON lab_rankings(calc_date);
        CREATE INDEX IF NOT EXISTS idx_lr_strategy ON lab_rankings(strategy_id);

        -- ─────────────────────────────────────────────────────
        -- 4. 계층 변경 이력
        -- ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS lab_tier_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            change_date  TEXT    NOT NULL,       -- YYYY-MM-DD
            strategy_id  TEXT    NOT NULL,
            old_tier     TEXT    NOT NULL,
            new_tier     TEXT    NOT NULL,
            reason       TEXT    DEFAULT '',
            auto         INTEGER DEFAULT 1,      -- 1=자동 승격, 0=수동
            created_at   TEXT    DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_lth_strategy ON lab_tier_history(strategy_id);
        CREATE INDEX IF NOT EXISTS idx_lth_date     ON lab_tier_history(change_date);

        -- ─────────────────────────────────────────────────────
        -- 5. AI 추천 기록
        -- ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS lab_ai_recs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            rec_date     TEXT    NOT NULL,       -- YYYY-MM-DD
            strategy_id  TEXT    NOT NULL,
            grade        TEXT    NOT NULL,       -- A/B/C/D/F
            score        REAL    DEFAULT 0,
            summary      TEXT    DEFAULT '',     -- 한 줄 요약
            recommendation TEXT  DEFAULT '',     -- 상세 추천 문구
            regime       TEXT    DEFAULT '',     -- 시장 국면
            UNIQUE (rec_date, strategy_id)
        );
        CREATE INDEX IF NOT EXISTS idx_lar_date     ON lab_ai_recs(rec_date);
        CREATE INDEX IF NOT EXISTS idx_lar_strategy ON lab_ai_recs(strategy_id);

        -- ─────────────────────────────────────────────────────
        -- 6. 시장 국면 기록
        -- ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS lab_regime (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            record_date  TEXT    NOT NULL UNIQUE, -- YYYY-MM-DD
            regime       TEXT    NOT NULL,         -- BULL/BEAR/LATERAL
            index_price  REAL    DEFAULT 0,        -- 당일 지수가
            index_ret_60d REAL   DEFAULT 0         -- 60일 수익률 %
        );
        CREATE INDEX IF NOT EXISTS idx_lrg_date ON lab_regime(record_date);
        """)
    logger.info(f"[LabDB] DB 초기화 완료: {LAB_DB_PATH}")


# ──────────────────────────────────────────────────────────────
# CRUD — lab_trades
# ──────────────────────────────────────────────────────────────

def insert_trade(
    strategy_id: str,
    code:        str,
    name:        str,
    action:      str,       # BUY / SELL / ADD_BUY
    price:       float,
    qty:         int,
    trade_date:  str,
    profit:      float = 0.0,
    profit_pct:  float = 0.0,
    hold_days:   int   = 0,
    reason:      str   = "",
) -> int:
    """가상 거래 1건 삽입 → 자동 생성된 id 반환"""
    amount = price * qty
    with _tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO lab_trades
              (strategy_id, code, name, action, price, qty, amount,
               trade_date, profit, profit_pct, hold_days, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (strategy_id, code, name, action, price, qty, amount,
             trade_date, profit, profit_pct, hold_days, reason),
        )
        return cur.lastrowid


def get_trades(
    strategy_id:  Optional[str] = None,
    code:         Optional[str] = None,
    action:       Optional[str] = None,
    start_date:   Optional[str] = None,
    end_date:     Optional[str] = None,
    limit:        int           = 200,
) -> List[Dict]:
    """가상 거래 조회 (다중 필터 지원)"""
    where, params = [], []
    if strategy_id:
        where.append("strategy_id = ?"); params.append(strategy_id)
    if code:
        where.append("code = ?");        params.append(code)
    if action:
        where.append("action = ?");      params.append(action)
    if start_date:
        where.append("trade_date >= ?"); params.append(start_date)
    if end_date:
        where.append("trade_date <= ?"); params.append(end_date)

    sql = "SELECT * FROM lab_trades"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY trade_date DESC, id DESC LIMIT {int(limit)}"

    conn = _get_conn()
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_trade_stats(strategy_id: str, start_date: Optional[str] = None) -> Dict:
    """전략별 거래 통계 (빠른 조회용)"""
    params = [strategy_id]
    date_filter = ""
    if start_date:
        date_filter = "AND trade_date >= ?"
        params.append(start_date)

    conn = _get_conn()
    row = conn.execute(f"""
        SELECT
            COUNT(CASE WHEN action='BUY'     THEN 1 END) AS buy_cnt,
            COUNT(CASE WHEN action='ADD_BUY' THEN 1 END) AS add_buy_cnt,
            COUNT(CASE WHEN action='SELL'    THEN 1 END) AS sell_cnt,
            SUM(CASE  WHEN action='SELL'     THEN profit ELSE 0 END) AS total_profit,
            AVG(CASE  WHEN action='SELL'     THEN profit_pct END)    AS avg_profit_pct,
            MAX(CASE  WHEN action='SELL'     THEN profit_pct END)    AS max_profit_pct,
            MIN(CASE  WHEN action='SELL'     THEN profit_pct END)    AS min_profit_pct,
            AVG(CASE  WHEN action='SELL'     THEN hold_days END)     AS avg_hold_days
        FROM lab_trades
        WHERE strategy_id = ? {date_filter}
    """, params).fetchone()

    if row:
        d = dict(row)
        sell_cnt = d.get("sell_cnt") or 0
        # 승률 계산
        if sell_cnt > 0:
            win_row = conn.execute("""
                SELECT COUNT(*) AS win_cnt FROM lab_trades
                WHERE strategy_id = ? AND action = 'SELL' AND profit > 0
            """ + (" AND trade_date >= ?" if start_date else ""),
                params).fetchone()
            d["win_cnt"]  = win_row["win_cnt"] if win_row else 0
            d["win_rate"] = round(d["win_cnt"] / sell_cnt * 100, 1)
        else:
            d["win_cnt"]  = 0
            d["win_rate"] = 0.0
        return d
    return {}


def delete_trades_by_strategy(strategy_id: str) -> int:
    """전략 거래 전체 삭제 (초기화 용도)"""
    with _tx() as conn:
        cur = conn.execute(
            "DELETE FROM lab_trades WHERE strategy_id = ?", (strategy_id,)
        )
        return cur.rowcount


# ──────────────────────────────────────────────────────────────
# CRUD — lab_equity
# ──────────────────────────────────────────────────────────────

def upsert_equity(
    strategy_id:  str,
    snap_date:    str,
    equity:       float,
    cash:         float = 0.0,
    position_cnt: int   = 0,
) -> None:
    """일별 자산 스냅샷 저장 (INSERT OR REPLACE)"""
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO lab_equity (strategy_id, snap_date, equity, cash, position_cnt)
            VALUES (?,?,?,?,?)
            ON CONFLICT(strategy_id, snap_date) DO UPDATE SET
                equity       = excluded.equity,
                cash         = excluded.cash,
                position_cnt = excluded.position_cnt
            """,
            (strategy_id, snap_date, equity, cash, position_cnt),
        )


def get_equity_curve(
    strategy_id: str,
    start_date:  Optional[str] = None,
    days:        int           = 120,
) -> List[Dict]:
    """자산 곡선 조회 (날짜 오름차순)"""
    params  = [strategy_id]
    filters = "WHERE strategy_id = ?"
    if start_date:
        filters += " AND snap_date >= ?"
        params.append(start_date)

    conn = _get_conn()
    rows = conn.execute(
        f"SELECT snap_date, equity, cash, position_cnt FROM lab_equity "
        f"{filters} ORDER BY snap_date ASC LIMIT {int(days)}",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_latest_equity(strategy_id: str) -> Optional[Dict]:
    """최신 자산 스냅샷 1건"""
    conn = _get_conn()
    row  = conn.execute(
        "SELECT * FROM lab_equity WHERE strategy_id = ? ORDER BY snap_date DESC LIMIT 1",
        (strategy_id,),
    ).fetchone()
    return dict(row) if row else None


# ──────────────────────────────────────────────────────────────
# CRUD — lab_rankings
# ──────────────────────────────────────────────────────────────

def upsert_ranking(
    calc_date:    str,
    strategy_id:  str,
    rank_no:      int,
    score:        float,
    tier:         str,
    regime:       str,
    metrics:      dict,
    ai_grade:     str = "",
) -> None:
    """랭킹 스냅샷 저장 (INSERT OR REPLACE)"""
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO lab_rankings
              (calc_date, strategy_id, rank_no, score, tier, regime, metrics_json, ai_grade)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(calc_date, strategy_id) DO UPDATE SET
                rank_no      = excluded.rank_no,
                score        = excluded.score,
                tier         = excluded.tier,
                regime       = excluded.regime,
                metrics_json = excluded.metrics_json,
                ai_grade     = excluded.ai_grade
            """,
            (calc_date, strategy_id, rank_no, score, tier, regime,
             json.dumps(metrics, ensure_ascii=False), ai_grade),
        )


def save_ranking_snapshot(
    ranking:   List[Dict],
    regime:    str,
    calc_date: Optional[str] = None,
) -> None:
    """
    calc_ranking() 결과 전체를 DB에 저장.
    ranking: StrategyLabEngine.calc_ranking() 반환값
    """
    today = calc_date or date.today().isoformat()
    for r in ranking:
        upsert_ranking(
            calc_date   = today,
            strategy_id = r["strategy_id"],
            rank_no     = r["rank"],
            score       = r["score"],
            tier        = r["tier"],
            regime      = regime,
            metrics     = r.get("metrics", {}),
            ai_grade    = r.get("ai", {}).get("grade", ""),
        )
    logger.info(f"[LabDB] 랭킹 저장: {len(ranking)}개 전략 ({today})")


def get_latest_ranking(limit: int = 20) -> List[Dict]:
    """최신 랭킹 스냅샷 (최신 calc_date 기준)"""
    conn = _get_conn()
    # 최신 calc_date 찾기
    row  = conn.execute(
        "SELECT MAX(calc_date) AS latest FROM lab_rankings"
    ).fetchone()
    if not row or not row["latest"]:
        return []
    latest = row["latest"]

    rows = conn.execute(
        """
        SELECT r.*, m.metrics_json
        FROM lab_rankings r
        LEFT JOIN lab_rankings m USING(calc_date, strategy_id)
        WHERE r.calc_date = ?
        ORDER BY r.rank_no ASC
        LIMIT ?
        """,
        (latest, limit),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["metrics"] = json.loads(d.get("metrics_json") or "{}")
        except Exception:
            d["metrics"] = {}
        result.append(d)
    return result


def get_ranking_history(strategy_id: str, weeks: int = 12) -> List[Dict]:
    """전략별 랭킹 변화 이력 (최근 N주)"""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT calc_date, rank_no, score, tier, ai_grade, metrics_json
        FROM   lab_rankings
        WHERE  strategy_id = ?
        ORDER  BY calc_date DESC
        LIMIT  ?
        """,
        (strategy_id, weeks),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["metrics"] = json.loads(d.get("metrics_json") or "{}")
        except Exception:
            d["metrics"] = {}
        result.append(d)
    return result


def get_ranking_dates() -> List[str]:
    """랭킹 계산 날짜 목록 (최신순)"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT calc_date FROM lab_rankings ORDER BY calc_date DESC LIMIT 52"
    ).fetchall()
    return [r["calc_date"] for r in rows]


# ──────────────────────────────────────────────────────────────
# CRUD — lab_tier_history
# ──────────────────────────────────────────────────────────────

def insert_tier_change(
    strategy_id:  str,
    old_tier:     str,
    new_tier:     str,
    reason:       str = "",
    auto:         bool = True,
    change_date:  Optional[str] = None,
) -> int:
    """계층 변경 이력 삽입"""
    today = change_date or date.today().isoformat()
    with _tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO lab_tier_history
              (change_date, strategy_id, old_tier, new_tier, reason, auto)
            VALUES (?,?,?,?,?,?)
            """,
            (today, strategy_id, old_tier, new_tier, reason, int(auto)),
        )
        return cur.lastrowid


def get_tier_history(
    strategy_id: Optional[str] = None,
    limit:       int           = 50,
) -> List[Dict]:
    """계층 변경 이력 조회"""
    conn   = _get_conn()
    params = []
    where  = ""
    if strategy_id:
        where  = "WHERE strategy_id = ?"
        params.append(strategy_id)

    rows = conn.execute(
        f"SELECT * FROM lab_tier_history {where} ORDER BY change_date DESC LIMIT {int(limit)}",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_latest_tier(strategy_id: str) -> Optional[str]:
    """전략의 현재 계층 (가장 최근 변경 기록에서)"""
    conn = _get_conn()
    row  = conn.execute(
        "SELECT new_tier FROM lab_tier_history WHERE strategy_id = ? ORDER BY change_date DESC LIMIT 1",
        (strategy_id,),
    ).fetchone()
    return row["new_tier"] if row else None


# ──────────────────────────────────────────────────────────────
# CRUD — lab_ai_recs
# ──────────────────────────────────────────────────────────────

def upsert_ai_rec(
    strategy_id:    str,
    grade:          str,    # A/B/C/D/F
    score:          float,
    summary:        str,
    recommendation: str,
    regime:         str,
    rec_date:       Optional[str] = None,
) -> None:
    """AI 추천 저장 (INSERT OR REPLACE)"""
    today = rec_date or date.today().isoformat()
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO lab_ai_recs
              (rec_date, strategy_id, grade, score, summary, recommendation, regime)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(rec_date, strategy_id) DO UPDATE SET
                grade          = excluded.grade,
                score          = excluded.score,
                summary        = excluded.summary,
                recommendation = excluded.recommendation,
                regime         = excluded.regime
            """,
            (today, strategy_id, grade, score, summary, recommendation, regime),
        )


def save_ai_recs_from_ranking(ranking: List[Dict], regime: str) -> None:
    """
    calc_ranking() 결과에서 AI 추천을 일괄 저장.
    """
    today = date.today().isoformat()
    for r in ranking:
        ai = r.get("ai", {})
        if not ai:
            continue
        upsert_ai_rec(
            strategy_id    = r["strategy_id"],
            grade          = ai.get("grade", ""),
            score          = r.get("score", 0.0),
            summary        = ai.get("summary", ""),
            recommendation = ai.get("recommendation", ""),
            regime         = regime,
            rec_date       = today,
        )


def get_latest_ai_recs(limit: int = 20) -> List[Dict]:
    """최신 AI 추천 전체 (최신 날짜 기준)"""
    conn = _get_conn()
    row  = conn.execute(
        "SELECT MAX(rec_date) AS latest FROM lab_ai_recs"
    ).fetchone()
    if not row or not row["latest"]:
        return []
    latest = row["latest"]

    rows = conn.execute(
        "SELECT * FROM lab_ai_recs WHERE rec_date = ? ORDER BY score DESC LIMIT ?",
        (latest, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_ai_rec_history(strategy_id: str, weeks: int = 12) -> List[Dict]:
    """전략별 AI 추천 변화 이력"""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT rec_date, grade, score, summary, recommendation, regime
        FROM   lab_ai_recs
        WHERE  strategy_id = ?
        ORDER  BY rec_date DESC
        LIMIT  ?
        """,
        (strategy_id, weeks),
    ).fetchall()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────
# CRUD — lab_regime
# ──────────────────────────────────────────────────────────────

def upsert_regime(
    regime:       str,
    index_price:  float = 0.0,
    index_ret_60d: float = 0.0,
    record_date:  Optional[str] = None,
) -> None:
    """시장 국면 저장"""
    today = record_date or date.today().isoformat()
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO lab_regime (record_date, regime, index_price, index_ret_60d)
            VALUES (?,?,?,?)
            ON CONFLICT(record_date) DO UPDATE SET
                regime        = excluded.regime,
                index_price   = excluded.index_price,
                index_ret_60d = excluded.index_ret_60d
            """,
            (today, regime, index_price, index_ret_60d),
        )


def get_regime_history(days: int = 90) -> List[Dict]:
    """시장 국면 이력 (최신순)"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM lab_regime ORDER BY record_date DESC LIMIT ?", (days,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_latest_regime() -> Optional[str]:
    """최신 시장 국면"""
    conn = _get_conn()
    row  = conn.execute(
        "SELECT regime FROM lab_regime ORDER BY record_date DESC LIMIT 1"
    ).fetchone()
    return row["regime"] if row else "LATERAL"


# ──────────────────────────────────────────────────────────────
# 복합 조회 — 대시보드용
# ──────────────────────────────────────────────────────────────

def get_dashboard_summary() -> Dict:
    """
    Strategy Lab 대시보드에 필요한 통계를 한 번의 쿼리 묶음으로 반환.
    {
      total_trades, total_profit, best_strategy_id, latest_ranking_date,
      regime, tier_counts: {LIVE, CANDIDATE, EXPERIMENT}
    }
    """
    conn = _get_conn()

    # 전체 거래 통계
    t_row = conn.execute("""
        SELECT
            COUNT(CASE WHEN action='SELL' THEN 1 END) AS sell_cnt,
            SUM(CASE  WHEN action='SELL' THEN profit ELSE 0 END) AS total_profit
        FROM lab_trades
    """).fetchone()

    # 최신 랭킹
    r_row = conn.execute(
        "SELECT MAX(calc_date) AS latest_date FROM lab_rankings"
    ).fetchone()
    latest_date = r_row["latest_date"] if r_row else ""

    best_strategy = ""
    if latest_date:
        b_row = conn.execute(
            "SELECT strategy_id FROM lab_rankings WHERE calc_date=? ORDER BY rank_no LIMIT 1",
            (latest_date,),
        ).fetchone()
        if b_row:
            best_strategy = b_row["strategy_id"]

    # 계층 분포 (최신 랭킹 날짜 기준)
    tier_counts = {"LIVE": 0, "CANDIDATE": 0, "EXPERIMENT": 0}
    if latest_date:
        tc_rows = conn.execute(
            "SELECT tier, COUNT(*) AS cnt FROM lab_rankings WHERE calc_date=? GROUP BY tier",
            (latest_date,),
        ).fetchall()
        for row in tc_rows:
            tier_counts[row["tier"]] = row["cnt"]

    # 최신 시장 국면
    regime = get_latest_regime()

    return {
        "total_sell_trades":  t_row["sell_cnt"]    if t_row else 0,
        "total_profit":       round(t_row["total_profit"] or 0, 0) if t_row else 0,
        "best_strategy_id":   best_strategy,
        "latest_ranking_date": latest_date,
        "regime":             regime,
        "tier_counts":        tier_counts,
    }


def get_strategy_performance_table() -> List[Dict]:
    """
    모든 전략의 최신 성과 테이블.
    [{strategy_id, rank_no, score, tier, ai_grade, total_return, mdd, sharpe, win_rate, trade_count}]
    """
    conn = _get_conn()
    row  = conn.execute(
        "SELECT MAX(calc_date) AS latest FROM lab_rankings"
    ).fetchone()
    if not row or not row["latest"]:
        return []

    latest = row["latest"]
    rows = conn.execute(
        """
        SELECT strategy_id, rank_no, score, tier, ai_grade, metrics_json
        FROM   lab_rankings
        WHERE  calc_date = ?
        ORDER  BY rank_no ASC
        """,
        (latest,),
    ).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        try:
            m = json.loads(d.pop("metrics_json") or "{}")
        except Exception:
            m = {}
        d["total_return"]  = round(m.get("total_return", 0), 2)
        d["cagr"]          = round(m.get("cagr", 0), 2)
        d["mdd"]           = round(m.get("mdd", 0), 2)
        d["sharpe"]        = round(m.get("sharpe", 0), 2)
        d["win_rate"]      = round(m.get("win_rate", 0), 1)
        d["profit_factor"] = round(m.get("profit_factor", 0), 2)
        d["trade_count"]   = m.get("trade_count", 0)
        d["return_3m"]     = round(m.get("return_3m", 0), 2)
        d["return_6m"]     = round(m.get("return_6m", 0), 2)
        result.append(d)
    return result


def get_weekly_best_by_regime() -> Dict:
    """
    시장 국면별 최고 성과 전략.
    {BULL: {strategy_id, score}, BEAR: ..., LATERAL: ...}
    """
    conn = _get_conn()
    result: Dict[str, Optional[Dict]] = {
        "BULL": None, "BEAR": None, "LATERAL": None
    }

    for regime in ("BULL", "BEAR", "LATERAL"):
        # 해당 국면의 최신 랭킹 날짜
        row = conn.execute(
            "SELECT calc_date FROM lab_rankings WHERE regime=? ORDER BY calc_date DESC LIMIT 1",
            (regime,),
        ).fetchone()
        if not row:
            continue
        latest = row["calc_date"]

        best = conn.execute(
            """
            SELECT strategy_id, score, tier, ai_grade
            FROM   lab_rankings
            WHERE  calc_date=? AND regime=?
            ORDER  BY rank_no ASC LIMIT 1
            """,
            (latest, regime),
        ).fetchone()
        if best:
            result[regime] = {
                "strategy_id": best["strategy_id"],
                "score":       best["score"],
                "tier":        best["tier"],
                "calc_date":   latest,
            }

    return result


# ──────────────────────────────────────────────────────────────
# 유틸리티
# ──────────────────────────────────────────────────────────────

def vacuum_old_data(keep_days: int = 365) -> None:
    """
    오래된 데이터 정리 (keep_days 이전 기록 삭제).
    용량 관리용 — 1년 이상 된 데이터 삭제.
    """
    cutoff = (
        datetime.today().replace(
            month=1, day=1
        ).strftime("%Y-%m-%d")
    )
    # 더 안전한 방법: keep_days 계산
    from datetime import timedelta
    cutoff = (datetime.today() - timedelta(days=keep_days)).strftime("%Y-%m-%d")

    with _tx() as conn:
        r1 = conn.execute("DELETE FROM lab_trades  WHERE trade_date < ?", (cutoff,)).rowcount
        r2 = conn.execute("DELETE FROM lab_equity  WHERE snap_date  < ?", (cutoff,)).rowcount
        r3 = conn.execute("DELETE FROM lab_regime  WHERE record_date < ?", (cutoff,)).rowcount

    logger.info(f"[LabDB] 오래된 데이터 정리: trades={r1}, equity={r2}, regime={r3} (기준={cutoff})")


def get_db_stats() -> Dict:
    """테이블별 레코드 수 반환 (모니터링용)"""
    conn = _get_conn()
    tables = ["lab_trades", "lab_equity", "lab_rankings",
              "lab_tier_history", "lab_ai_recs", "lab_regime"]
    stats = {}
    for t in tables:
        row = conn.execute(f"SELECT COUNT(*) AS cnt FROM {t}").fetchone()
        stats[t] = row["cnt"] if row else 0
    return stats


def reset_strategy_data(strategy_id: str) -> None:
    """
    특정 전략의 모든 DB 데이터 초기화.
    (전략 파라미터 변경 후 재시작 시 사용)
    """
    with _tx() as conn:
        conn.execute("DELETE FROM lab_trades  WHERE strategy_id = ?", (strategy_id,))
        conn.execute("DELETE FROM lab_equity  WHERE strategy_id = ?", (strategy_id,))
        conn.execute("DELETE FROM lab_rankings WHERE strategy_id = ?", (strategy_id,))
        conn.execute("DELETE FROM lab_ai_recs WHERE strategy_id = ?", (strategy_id,))
        conn.execute("DELETE FROM lab_tier_history WHERE strategy_id = ?", (strategy_id,))
    logger.info(f"[LabDB] 전략 데이터 초기화: {strategy_id}")


# ──────────────────────────────────────────────────────────────
# 앱 시작 시 자동 초기화 (import 시 호출)
# ──────────────────────────────────────────────────────────────
try:
    init_db()
except Exception as _e:
    logger.warning(f"[LabDB] init_db 자동 실행 실패 (나중에 수동 호출): {_e}")
