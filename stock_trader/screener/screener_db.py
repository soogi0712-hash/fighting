"""
SQLite DB 스키마 및 CRUD
==========================
테이블:
  stock_scores   — 매일 분석된 종목별 점수 결과
  screen_summary — 일별 스크리닝 요약
  watchlist_db   — 감시대상 목록 (200개 이내)
  focus_list     — 집중감시 목록 (30개)
  buy_candidates — 매수 후보 (10개 이내)
"""

import os
import json
import sqlite3
from datetime import datetime, date
from utils.logger import get_logger

logger = get_logger("ScreenerDB")

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "screener.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS stock_scores (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT NOT NULL,
            code          TEXT NOT NULL,
            name          TEXT,
            market        TEXT,
            sector        TEXT,
            total_score   REAL,
            base_score    REAL,
            market_penalty REAL,
            bonus         REAL,
            rs_value      REAL,
            grade         TEXT,
            buy_eligible  INTEGER,
            buy_reason    TEXT,
            excluded      INTEGER DEFAULT 0,
            exclude_reason TEXT,
            detail_json   TEXT,
            created_at    TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(date, code)
        );

        CREATE TABLE IF NOT EXISTS screen_summary (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT UNIQUE,
            total_analyzed INTEGER,
            total_excluded INTEGER,
            buy_candidate  INTEGER,
            watch_high     INTEGER,
            watch          INTEGER,
            hold_only      INTEGER,
            exclude        INTEGER,
            market_kospi_ret REAL,
            market_kosdaq_ret REAL,
            created_at    TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS watchlist_db (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT UNIQUE,
            name        TEXT,
            market      TEXT,
            sector      TEXT,
            added_date  TEXT,
            last_score  REAL,
            last_grade  TEXT,
            is_focus    INTEGER DEFAULT 0,
            is_candidate INTEGER DEFAULT 0,
            updated_at  TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS buy_candidates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT NOT NULL,
            rank         INTEGER,
            code         TEXT,
            name         TEXT,
            sector       TEXT,
            score        REAL,
            rs_value     REAL,
            grade        TEXT,
            cur_price    REAL,
            market_cap   REAL,
            daily_amount REAL,
            reason       TEXT,
            created_at   TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(date, code)
        );

        CREATE INDEX IF NOT EXISTS idx_scores_date  ON stock_scores(date);
        CREATE INDEX IF NOT EXISTS idx_scores_grade ON stock_scores(grade);
        CREATE INDEX IF NOT EXISTS idx_scores_score ON stock_scores(total_score DESC);
        """)
    logger.info(f"DB 초기화 완료: {DB_PATH}")


# ── CRUD ──────────────────────────────────────────────────────

def upsert_score(date_str: str, result: dict):
    """종목 점수 저장 (upsert)"""
    with get_conn() as conn:
        conn.execute("""
        INSERT INTO stock_scores
            (date, code, name, market, sector, total_score, base_score,
             market_penalty, bonus, rs_value, grade, buy_eligible, buy_reason,
             excluded, exclude_reason, detail_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(date,code) DO UPDATE SET
            total_score=excluded.total_score, base_score=excluded.base_score,
            market_penalty=excluded.market_penalty, bonus=excluded.bonus,
            rs_value=excluded.rs_value, grade=excluded.grade,
            buy_eligible=excluded.buy_eligible, buy_reason=excluded.buy_reason,
            excluded=excluded.excluded, exclude_reason=excluded.exclude_reason,
            detail_json=excluded.detail_json,
            created_at=datetime('now','localtime')
        """, (
            date_str,
            result.get("code"), result.get("name"),
            result.get("market"), result.get("sector"),
            result.get("total_score"), result.get("base_score"),
            result.get("market_penalty"), result.get("bonus"),
            result.get("rs_value"), result.get("grade"),
            int(result.get("buy_eligible", False)),
            result.get("buy_reason"),
            int(result.get("excluded", False)),
            result.get("exclude_reason"),
            json.dumps(result.get("detail", {}), ensure_ascii=False),
        ))


def save_summary(date_str: str, summary: dict):
    with get_conn() as conn:
        conn.execute("""
        INSERT INTO screen_summary
            (date, total_analyzed, total_excluded, buy_candidate, watch_high,
             watch, hold_only, exclude, market_kospi_ret, market_kosdaq_ret)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
            total_analyzed=excluded.total_analyzed,
            total_excluded=excluded.total_excluded,
            buy_candidate=excluded.buy_candidate,
            watch_high=excluded.watch_high,
            watch=excluded.watch, hold_only=excluded.hold_only,
            exclude=excluded.exclude,
            market_kospi_ret=excluded.market_kospi_ret,
            market_kosdaq_ret=excluded.market_kosdaq_ret
        """, (
            date_str,
            summary.get("total_analyzed"), summary.get("total_excluded"),
            summary.get("buy_candidate"), summary.get("watch_high"),
            summary.get("watch"), summary.get("hold_only"), summary.get("exclude"),
            summary.get("market_kospi_ret"), summary.get("market_kosdaq_ret"),
        ))


def save_candidates(date_str: str, candidates: list[dict]):
    with get_conn() as conn:
        conn.execute("DELETE FROM buy_candidates WHERE date=?", (date_str,))
        for rank, c in enumerate(candidates[:10], 1):
            conn.execute("""
            INSERT OR IGNORE INTO buy_candidates
                (date, rank, code, name, sector, score, rs_value, grade,
                 cur_price, market_cap, daily_amount, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                date_str, rank,
                c.get("code"), c.get("name"), c.get("sector"),
                c.get("total_score"), c.get("rs_value"), c.get("grade"),
                c.get("cur_price"), c.get("market_cap"), c.get("daily_amount"),
                c.get("buy_reason"),
            ))


def update_watchlist(candidates: list[dict], focus_list: list[dict]):
    """감시대상 200개, 집중감시 30개 업데이트"""
    with get_conn() as conn:
        conn.execute("UPDATE watchlist_db SET is_focus=0, is_candidate=0")
        today = date.today().isoformat()
        for c in candidates[:200]:
            is_focus     = 1 if any(f["code"] == c["code"] for f in focus_list) else 0
            is_candidate = 1 if c.get("buy_eligible") else 0
            conn.execute("""
            INSERT INTO watchlist_db (code, name, market, sector, added_date,
                                      last_score, last_grade, is_focus, is_candidate)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name, last_score=excluded.last_score,
                last_grade=excluded.last_grade, is_focus=excluded.is_focus,
                is_candidate=excluded.is_candidate,
                updated_at=datetime('now','localtime')
            """, (
                c.get("code"), c.get("name"), c.get("market"), c.get("sector"),
                today, c.get("total_score"), c.get("grade"),
                is_focus, is_candidate,
            ))


# ── 조회 ─────────────────────────────────────────────────────

def get_latest_scores(limit: int = 200, grade_filter: str = None) -> list[dict]:
    today = date.today().isoformat()
    with get_conn() as conn:
        if grade_filter:
            rows = conn.execute("""
                SELECT * FROM stock_scores
                WHERE date=? AND grade=? AND excluded=0
                ORDER BY total_score DESC LIMIT ?
            """, (today, grade_filter, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM stock_scores
                WHERE date=? AND excluded=0
                ORDER BY total_score DESC LIMIT ?
            """, (today, limit)).fetchall()
    return [dict(r) for r in rows]


def get_buy_candidates(date_str: str = None) -> list[dict]:
    if not date_str:
        date_str = date.today().isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM buy_candidates WHERE date=? ORDER BY rank
        """, (date_str,)).fetchall()
    return [dict(r) for r in rows]


def get_screen_summary(days: int = 7) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM screen_summary
            ORDER BY date DESC LIMIT ?
        """, (days,)).fetchall()
    return [dict(r) for r in rows]


def get_watchlist_db(focus_only: bool = False) -> list[dict]:
    with get_conn() as conn:
        if focus_only:
            rows = conn.execute(
                "SELECT * FROM watchlist_db WHERE is_focus=1 ORDER BY last_score DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM watchlist_db ORDER BY last_score DESC LIMIT 200"
            ).fetchall()
    return [dict(r) for r in rows]


def get_score_history(code: str, days: int = 30) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT date, total_score, grade, rs_value, market_penalty, bonus
            FROM stock_scores WHERE code=?
            ORDER BY date DESC LIMIT ?
        """, (code, days)).fetchall()
    return [dict(r) for r in rows]
