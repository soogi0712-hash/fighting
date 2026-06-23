"""
adaptive/trade_recorder.py — 거래 이력 기록 엔진
==================================================
매 BUY/SELL 시 진입·청산 컨텍스트를 전부 저장.
SQLite(trade_history.db) + JSON(v2_trade_history.json) 이중 저장.

저장 구조:
  BUY  → trade_id 생성, 진입 컨텍스트 기록 (open 상태)
  SELL → 해당 trade_id 청산 컨텍스트 기록 (closed 상태)
         + signal_type 자동 분류 + 수익률/보유시간 계산

signal_type 분류 기준 (KR):
  "폭발돌파"   breakout_bonus=0.30
  "강한돌파"   breakout_bonus=0.20
  "초기돌파"   breakout_bonus=0.10
  "거래량폭증" vol_surge=True
  "거래량증가" vol_increase=True
  "기본진입"   기타

signal_type 분류 기준 (US):
  "US_FULL"    stage=FULL
  "US_EARLY"   stage=EARLY
  "US_SURGE"   vol_surge=True
  "US_기본"    기타
"""

import os
import json
import sqlite3
import threading
from datetime import datetime, date
from typing import Optional

from utils.v2_logger import get_logger

logger = get_logger("TradeRecorder")

_DATA_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
_DB_FILE   = os.path.join(_DATA_DIR, "trade_history.db")
_JSON_FILE = os.path.join(_DATA_DIR, "v2_trade_history.json")

_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
# signal_type 분류기
# ═══════════════════════════════════════════════════════════════

def classify_signal(market: str, iv: dict, stage: str = "") -> str:
    """진입 시 iv + stage → signal_type 문자열 반환."""
    if market == "KR":
        bp = iv.get("breakout_bonus", 0.0)
        if bp >= 0.30:  return "폭발돌파"
        if bp >= 0.20:  return "강한돌파"
        if bp >= 0.10:  return "초기돌파"
        vs = iv.get("vol_surge",    False)
        vi = iv.get("vol_increase", False)
        if vs:          return "거래량폭증"
        if vi:          return "거래량증가"
        return "기본진입"
    else:  # US
        vs = iv.get("vol_surge", False)
        if stage.upper() == "FULL":   return "US_FULL"
        if stage.upper() == "EARLY":  return "US_EARLY"
        if vs:                        return "US_SURGE"
        return "US_기본"


# ═══════════════════════════════════════════════════════════════
# DB 초기화
# ═══════════════════════════════════════════════════════════════

def _init_db(conn: sqlite3.Connection):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id      TEXT    UNIQUE NOT NULL,
        market        TEXT    NOT NULL,
        code          TEXT    NOT NULL,
        name          TEXT    NOT NULL,
        signal_type   TEXT    NOT NULL,
        status        TEXT    NOT NULL DEFAULT 'open',

        -- 진입
        entry_time    TEXT,
        entry_price   REAL,
        entry_qty     INTEGER,
        entry_reason  TEXT,
        buy_score     REAL,
        sell_score    INTEGER,
        vol_score     INTEGER,
        vwap_state    INTEGER,
        rsi           REAL,
        breakout_bonus REAL,
        strength      REAL,

        -- 청산
        exit_time     TEXT,
        exit_price    REAL,
        exit_reason   TEXT,
        exit_pct      REAL,
        hold_min      REAL,
        pnl_krw       REAL,

        -- ★ 학습용 확장 컬럼
        max_pct        REAL,          -- 보유 중 최고 수익률 %
        min_pct        REAL,          -- 보유 중 최저 수익률 %
        signal_time    TEXT,          -- 신호 발생 시각 (ISO)
        order_time     TEXT,          -- 주문 요청 시각
        fill_time      TEXT,          -- 실제 체결 시각 (KIS 체결내역)
        signal_delay_sec REAL,        -- signal→order 지연초
        fill_delay_sec   REAL,        -- order→fill 지연초
        price_gap_pct    REAL,        -- 신호가격 대비 체결가격 괴리율 %
        price_source     TEXT,        -- 시세소스 (KIS/yfinance_batch 등)
        order_no         TEXT,        -- KIS 주문번호
        kis_synced       INTEGER DEFAULT 0, -- KIS 체결내역 동기화 여부 (0/1)
        outcome          TEXT,        -- 'WIN'/'LOSS'/'BREAKEVEN'
        exit_category    TEXT,        -- 'TAKE_PROFIT'/'STOPLOSS'/'FORCE_CLOSE'/'TRAILING' 등

        -- 메타
        created_at    TEXT    DEFAULT (datetime('now','localtime'))
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_code ON trades(code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades(signal_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(entry_time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_order_no ON trades(order_no)"
    )
    conn.commit()

    # ★ 기존 DB에 신규 컬럼 추가 (ALTER TABLE — 없는 경우만)
    _new_cols = [
        ("max_pct",           "REAL"),
        ("min_pct",           "REAL"),
        ("signal_time",       "TEXT"),
        ("order_time",        "TEXT"),
        ("fill_time",         "TEXT"),
        ("signal_delay_sec",  "REAL"),
        ("fill_delay_sec",    "REAL"),
        ("price_gap_pct",     "REAL"),
        ("price_source",      "TEXT"),
        ("order_no",          "TEXT"),
        ("kis_synced",        "INTEGER DEFAULT 0"),
        ("outcome",           "TEXT"),
        ("exit_category",     "TEXT"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    for col_name, col_type in _new_cols:
        if col_name not in existing:
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


# ═══════════════════════════════════════════════════════════════
# 핵심 API
# ═══════════════════════════════════════════════════════════════

class TradeRecorder:
    """
    BUY/SELL 이벤트를 SQLite + JSON에 이중 저장.

    사용법:
        recorder = TradeRecorder()

        # 진입 시
        trade_id = recorder.record_entry(
            market="KR", code="005930", name="삼성전자",
            price=72000, qty=10, reason="Full진입 BUY_SCORE=0.62",
            iv={...}, stage="FULL"
        )

        # 청산 시
        recorder.record_exit(
            trade_id=trade_id, exit_price=73500, exit_qty=10,
            exit_reason="익절 +2.1%", exit_pct=2.1, pnl_krw=140000
        )
    """

    def __init__(self):
        os.makedirs(_DATA_DIR, exist_ok=True)
        # open 상태 거래 캐시 {code → trade_id} — SELL 시 빠른 매칭
        self._open: dict[str, str] = {}
        self._load_open_from_db()

    def _load_open_from_db(self):
        """재시작 시 open 상태 거래 복원."""
        try:
            conn = _get_conn()
            rows = conn.execute(
                "SELECT trade_id, code FROM trades WHERE status='open'"
            ).fetchall()
            for row in rows:
                self._open[row["code"]] = row["trade_id"]
            conn.close()
        except Exception as e:
            logger.warning(f"[TradeRecorder] open 복원 실패: {e}")

    # ── 진입 기록 ────────────────────────────────────────────

    def record_entry(self,
                     market:  str,
                     code:    str,
                     name:    str,
                     price:   float,
                     qty:     int,
                     reason:  str,
                     iv:      dict,
                     stage:   str = "",
                     signal_time:  Optional[str] = None,
                     order_time:   Optional[str] = None,
                     order_no:     str = "",
                     price_source: str = "") -> str:
        """
        BUY 체결 후 호출. trade_id 반환.
        iv: _calc_indicators() 반환값
        signal_time: 신호 발생 시각 (ISO)
        order_time:  주문 요청 시각 (ISO)
        order_no:    KIS 주문번호
        price_source: 시세소스 (KIS/yfinance_batch 등)
        """
        with _lock:
            signal_type = classify_signal(market, iv, stage)
            trade_id    = f"{market}_{code}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
            now_iso     = datetime.now().isoformat()
            entry_time  = now_iso

            # iv에서 지표 추출 (KR/US 통합)
            buy_score     = float(iv.get("buy_score",      0.0))
            sell_score    = int(  iv.get("sell_score",     0))
            vol_increase  = iv.get("vol_increase", iv.get("vol_ok", False))
            vol_surge     = iv.get("vol_surge", False)
            vol_score     = 2 if vol_surge else (1 if vol_increase else 0)
            vwap_above    = iv.get("vwap_above", iv.get("above_vwap", False))
            vwap_state    = 1 if vwap_above else 0
            rsi           = float(iv.get("rsi", 0.0))
            breakout_bonus= float(iv.get("breakout_bonus", 0.0))
            strength      = float(iv.get("strength",       0.0))

            # 지연 계산
            sig_delay = None
            if signal_time and order_time:
                try:
                    _s = datetime.fromisoformat(signal_time)
                    _o = datetime.fromisoformat(order_time)
                    sig_delay = (_o - _s).total_seconds()
                except Exception:
                    pass

            try:
                conn = _get_conn()
                conn.execute("""
                    INSERT INTO trades
                    (trade_id, market, code, name, signal_type, status,
                     entry_time, entry_price, entry_qty, entry_reason,
                     buy_score, sell_score, vol_score, vwap_state, rsi,
                     breakout_bonus, strength,
                     signal_time, order_time, signal_delay_sec,
                     price_source, order_no)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    trade_id, market, code, name, signal_type, "open",
                    entry_time, price, qty, reason,
                    buy_score, sell_score, vol_score, vwap_state, rsi,
                    breakout_bonus, strength,
                    signal_time or now_iso, order_time or now_iso, sig_delay,
                    price_source or "", order_no or "",
                ))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"[TradeRecorder] 진입 DB저장 실패 {code}: {e}")

            self._open[code] = trade_id
            self._append_json({
                "trade_id": trade_id, "market": market,
                "code": code, "name": name,
                "signal_type": signal_type, "status": "open",
                "entry_time": entry_time, "entry_price": price,
                "entry_qty": qty, "entry_reason": reason,
                "buy_score": buy_score, "sell_score": sell_score,
                "vol_score": vol_score, "vwap_state": vwap_state,
                "rsi": rsi, "breakout_bonus": breakout_bonus,
                "signal_time": signal_time, "order_time": order_time,
                "signal_delay_sec": sig_delay, "order_no": order_no,
                "price_source": price_source,
            })

            logger.info(
                f"[TradeRecorder] 진입 기록 | {market} {name}({code}) "
                f"signal={signal_type} buy={buy_score:.2f} "
                f"vol={vol_score} vwap={vwap_state}"
            )
            return trade_id

    # ── 청산 기록 ────────────────────────────────────────────

    def record_exit(self,
                    code:        str,
                    exit_price:  float,
                    exit_qty:    int,
                    exit_reason: str,
                    exit_pct:    float,
                    pnl_krw:     float,
                    trade_id:    Optional[str] = None,
                    fill_time:   Optional[str] = None,
                    exit_category: str = "",
                    max_pct:     Optional[float] = None,
                    min_pct:     Optional[float] = None) -> bool:
        """
        SELL 체결 후 호출.
        trade_id 미지정 시 open 캐시에서 자동 매칭.
        fill_time: KIS 실체결 시각
        exit_category: TAKE_PROFIT / STOPLOSS / FORCE_CLOSE / TRAILING 등
        max_pct / min_pct: 보유 중 최고/최저 수익률
        """
        with _lock:
            tid = trade_id or self._open.get(code)
            if not tid:
                logger.warning(f"[TradeRecorder] 매칭 실패 — open trade 없음: {code}")
                return False

            exit_time = datetime.now().isoformat()
            hold_min  = 0.0
            outcome   = "WIN" if exit_pct > 0.05 else ("LOSS" if exit_pct < -0.05 else "BREAKEVEN")
            # exit_category 자동 분류
            if not exit_category:
                r = exit_reason.lower()
                if "익절" in r or "take_profit" in r or "익절" in r:
                    exit_category = "TAKE_PROFIT"
                elif "손절" in r or "stoploss" in r or "손절" in r or "이탈" in r:
                    exit_category = "STOPLOSS"
                elif "강제" in r or "force" in r:
                    exit_category = "FORCE_CLOSE"
                elif "trailing" in r or "추적" in r:
                    exit_category = "TRAILING"
                else:
                    exit_category = "OTHER"

            # ── DB 업데이트 + 진입시각 조회 (단일 커넥션) ────────
            entry_time_str: Optional[str] = None
            fill_delay: Optional[float]   = None
            try:
                conn = _get_conn()
                row = conn.execute(
                    "SELECT entry_time, order_time FROM trades WHERE trade_id=?", (tid,)
                ).fetchone()
                if row:
                    entry_time_str = row["entry_time"]   # [TRADE_REVIEW] 매수시각
                    if row["entry_time"]:
                        entry_dt = datetime.fromisoformat(row["entry_time"])
                        hold_min = (datetime.now() - entry_dt).total_seconds() / 60.0
                    if fill_time and row["order_time"]:
                        try:
                            _o = datetime.fromisoformat(row["order_time"])
                            _f = datetime.fromisoformat(fill_time)
                            fill_delay = (_f - _o).total_seconds()
                        except Exception:
                            pass

                conn.execute("""
                    UPDATE trades
                    SET status='closed',
                        exit_time=?, exit_price=?,
                        exit_reason=?, exit_pct=?,
                        hold_min=?, pnl_krw=?,
                        fill_time=?, exit_category=?,
                        max_pct=?, min_pct=?,
                        outcome=?, fill_delay_sec=?,
                        kis_synced=1
                    WHERE trade_id=?
                """, (exit_time, exit_price, exit_reason,
                      exit_pct, hold_min, pnl_krw,
                      fill_time or exit_time, exit_category,
                      max_pct, min_pct,
                      outcome, fill_delay, tid))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"[TradeRecorder] 청산 DB저장 실패 {code}: {e}")
                return False

            self._open.pop(code, None)
            self._update_json_exit(tid, exit_time, exit_price,
                                   exit_reason, exit_pct, hold_min, pnl_krw)

            # ── [TRADE_REVIEW] KIS 실체결 기준 단일 거래 요약 ────
            # 기존 "[TradeRecorder] 청산 기록" 로그를 제거하고 이 로그로 통합.
            # 우선순위: KIS 실체결(fill_time) > TradeSync > TradeRecorder > Dashboard
            _max_s = f"{max_pct:+.2f}%" if max_pct is not None else "N/A"
            _min_s = f"{min_pct:+.2f}%" if min_pct is not None else "N/A"
            logger.info(
                f"[TRADE_REVIEW] "
                f"종목={code} | "
                f"매수시각={entry_time_str or 'N/A'} | "
                f"매도시각={fill_time or exit_time} | "
                f"최고수익률={_max_s} | "
                f"최저수익률={_min_s} | "
                f"매도사유={exit_reason} | "
                f"실현손익={pnl_krw:+,.0f}원({exit_pct:+.2f}%)"
            )
            return True

    # ── JSON 헬퍼 ────────────────────────────────────────────

    def _append_json(self, record: dict):
        try:
            data = self._load_json()
            data.append(record)
            self._save_json(data)
        except Exception:
            pass

    def _update_json_exit(self, trade_id, exit_time, exit_price,
                          exit_reason, exit_pct, hold_min, pnl_krw):
        try:
            data = self._load_json()
            for rec in data:
                if rec.get("trade_id") == trade_id:
                    rec.update({
                        "status": "closed",
                        "exit_time": exit_time,
                        "exit_price": exit_price,
                        "exit_reason": exit_reason,
                        "exit_pct": exit_pct,
                        "hold_min": hold_min,
                        "pnl_krw": pnl_krw,
                    })
                    break
            self._save_json(data)
        except Exception:
            pass

    def _load_json(self) -> list:
        try:
            if os.path.exists(_JSON_FILE):
                with open(_JSON_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_json(self, data: list):
        with open(_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(data[-2000:], f, ensure_ascii=False, indent=2)

    # ── 조회 API ─────────────────────────────────────────────
    # get_closed_trades / get_open_trade_id: 제거됨
    # 대안: 직접 DB 조회(trade_history.db) 또는 TradeSync / record_entry() 반환값 사용
