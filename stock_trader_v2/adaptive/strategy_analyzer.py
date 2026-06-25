"""
adaptive/strategy_analyzer.py — 전략별 성과 분석 엔진
======================================================
trade_history.db에서 청산된 거래를 읽어 signal_type별로
EV / 승률 / 평균수익 / 평균손실 / 손익비 / 거래횟수를 계산.

출력:
  strategy_stats.json   — 전략별 누적 통계
  daily_stats.json      — 일별 통계 (최근 90일)

EV(기대값) 계산:
  EV = 승률 × 평균수익 + (1-승률) × 평균손실
  EV > 0 : 기대값 양수 (장기 수익 가능)
  EV < 0 : 기대값 음수 (장기 손실 확실)
"""

import os
import json
from datetime import datetime, date, timedelta
from typing import Optional
import sqlite3

from utils.v2_logger import get_logger
from adaptive.trade_recorder import _get_conn, _DATA_DIR

logger = get_logger("StrategyAnalyzer")

_STATS_FILE = os.path.join(_DATA_DIR, "strategy_stats.json")
_DAILY_FILE = os.path.join(_DATA_DIR, "daily_stats.json")

# 전략 상태 전이 기준
_WARN_THRESHOLD      = -0.3    # EV ≤ -0.3% → WARNING (qty 30%로 축소, 학습 유지)
_BLOCK_THRESHOLD     = -0.5    # EV ≤ -0.5% + 거래 ≥ 50건 → BLOCK (진입 완전 차단)
_BLOCK_MIN_TRADES    = 50      # BLOCK 판단 최소 거래 수
_WARN_MIN_TRADES     = 20      # WARNING 판단 최소 거래 수

# 하위 호환 alias (기존 코드에서 DISABLED 참조 시)
_DISABLE_THRESHOLD  = _BLOCK_THRESHOLD
_DISABLE_MIN_TRADES = _BLOCK_MIN_TRADES

# 가중치 조정에 사용할 최근 거래 윈도우
RECENT_WINDOW = 200


# ═══════════════════════════════════════════════════════════════
# 전략별 통계 계산
# ═══════════════════════════════════════════════════════════════

def _calc_stats(trades: list[dict]) -> dict:
    """
    거래 리스트 → EV/승률/평균수익/평균손실/손익비 계산.
    trades: TradeRecorder.get_closed_trades() 반환값
    """
    if not trades:
        return _empty_stats()

    pcts   = [t["exit_pct"] for t in trades if t.get("exit_pct") is not None]
    wins   = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p <= 0]

    total    = len(pcts)
    win_cnt  = len(wins)
    loss_cnt = len(losses)
    win_rate = win_cnt / total if total else 0.0

    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    ev       = win_rate * avg_win + (1 - win_rate) * avg_loss

    # 손익비 (평균수익 / |평균손실|)
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    # 최대 연속 손실
    max_consec_loss = 0
    cur_loss = 0
    for p in pcts:
        if p <= 0:
            cur_loss += 1
            max_consec_loss = max(max_consec_loss, cur_loss)
        else:
            cur_loss = 0

    # 평균 보유 시간
    holds = [t["hold_min"] for t in trades if t.get("hold_min")]
    avg_hold = sum(holds) / len(holds) if holds else 0.0

    # 총 실현 손익
    total_pnl = sum(t.get("pnl_krw", 0) or 0 for t in trades)

    return {
        "trade_count":    total,
        "win_count":      win_cnt,
        "loss_count":     loss_cnt,
        "win_rate":       round(win_rate * 100, 1),        # %
        "avg_win_pct":    round(avg_win,          3),
        "avg_loss_pct":   round(avg_loss,         3),
        "ev":             round(ev,               3),       # 기대값(%)
        "profit_factor":  round(profit_factor,    2),
        "max_consec_loss": max_consec_loss,
        "avg_hold_min":   round(avg_hold,         1),
        "total_pnl_krw":  round(total_pnl,        0),
    }


def _empty_stats() -> dict:
    return {
        "trade_count": 0, "win_count": 0, "loss_count": 0,
        "win_rate": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
        "ev": 0.0, "profit_factor": 0.0, "max_consec_loss": 0,
        "avg_hold_min": 0.0, "total_pnl_krw": 0.0,
    }


# ═══════════════════════════════════════════════════════════════
# 전략 상태 판정
# ═══════════════════════════════════════════════════════════════

def _judge_status(stats: dict, current_status: str = "ACTIVE") -> str:
    """
    EV + 거래 수 기반 상태 판정.
    안전장치(거래시간/재진입 등)는 절대 변경 안 함.

    상태 전이표:
      ACTIVE  : EV > 0%            → 정상 진입 (qty_scale=1.0)
      WARNING : EV ≤ -0.3%         → qty 30% 축소, BUY_SCORE 차감 없음 (학습 유지)
      BLOCK   : EV ≤ -0.5% ≥ 50건 → 진입 완전 차단 (BLOCK 후 EV>0 회복 시 WARNING 거쳐 복원)
    """
    n  = stats["trade_count"]
    ev = stats["ev"]

    # BLOCK → 개선 없으면 유지 (수동 복원만 가능)
    if current_status in ("BLOCK", "DISABLED"):  # DISABLED 하위호환
        if ev > 0 and n >= 10:
            return "WARNING"    # 소폭 회복 → WARNING으로 완화
        return "BLOCK"

    # 거래 수 부족 → 판단 보류
    if n < _WARN_MIN_TRADES:
        return current_status

    if ev <= _BLOCK_THRESHOLD and n >= _BLOCK_MIN_TRADES:
        return "BLOCK"
    if ev <= _WARN_THRESHOLD:
        return "WARNING"
    if ev > 0:
        return "ACTIVE"

    return current_status


# ═══════════════════════════════════════════════════════════════
# StrategyAnalyzer 클래스
# ═══════════════════════════════════════════════════════════════

class StrategyAnalyzer:
    """
    trade_history.db → 전략별 성과 통계 산출 및 저장.

    사용법:
        analyzer = StrategyAnalyzer()
        stats    = analyzer.run()   # 전체 분석 실행
        stats_kr = analyzer.get_signal_stats("KR")
    """

    # 관리 대상 signal_type 목록
    KR_SIGNALS = ["폭발돌파", "강한돌파", "초기돌파", "거래량폭증", "거래량증가", "기본진입"]
    US_SIGNALS = ["US_FULL", "US_EARLY", "US_SURGE", "US_기본"]

    def __init__(self):
        os.makedirs(_DATA_DIR, exist_ok=True)
        self._stats: dict = self._load_stats()

    # ── 메인 분석 실행 ────────────────────────────────────────

    def run(self, market: Optional[str] = None) -> dict:
        """
        전체 또는 특정 시장의 전략 통계 재계산.
        Returns: {signal_type: {stats + status + weight}}
        """
        signals = []
        if market in (None, "KR"):
            signals += [(s, "KR") for s in self.KR_SIGNALS]
        if market in (None, "US"):
            signals += [(s, "US") for s in self.US_SIGNALS]

        for sig, mkt in signals:
            self._analyze_one(sig, mkt)

        self._save_stats()
        self._update_daily_stats()
        logger.info(
            f"[StrategyAnalyzer] 분석 완료 — "
            f"{len(signals)}개 전략 업데이트"
        )
        return self._stats

    def _analyze_one(self, signal_type: str, market: str):
        """단일 signal_type 분석 후 _stats 갱신."""
        try:
            conn  = _get_conn()
            rows  = conn.execute("""
                SELECT exit_pct, hold_min, pnl_krw
                FROM trades
                WHERE status='closed'
                  AND signal_type=?
                  AND market=?
                ORDER BY exit_time DESC
                LIMIT ?
            """, (signal_type, market, RECENT_WINDOW)).fetchall()
            conn.close()
            trades = [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[StrategyAnalyzer] DB 조회 실패 {signal_type}: {e}")
            trades = []

        new_stats = _calc_stats(trades)

        key = f"{market}:{signal_type}"
        prev = self._stats.get(key, {})
        current_status = prev.get("status", "ACTIVE")
        new_status = _judge_status(new_stats, current_status)

        # ★ [긴급 안정화] READONLY 모드: status 자동 변경 차단
        # weight_adjuster.ADAPTIVE_READONLY 를 동적으로 참조
        try:
            from adaptive.weight_adjuster import ADAPTIVE_READONLY as _READONLY
        except ImportError:
            _READONLY = False

        if _READONLY and new_status != current_status:
            logger.info(
                f"[StrategyAnalyzer][READONLY] 상태 변경 억제 {signal_type} "
                f"{current_status} → {new_status} (READONLY 모드 — 변경 차단) "
                f"(EV={new_stats['ev']:+.3f}%, n={new_stats['trade_count']})"
            )
            new_status = current_status  # 상태 변경 차단, 기존 유지

        # 기존 가중치 유지 (WeightAdjuster가 별도 관리)
        weight = prev.get("weight", 1.0)

        self._stats[key] = {
            **new_stats,
            "signal_type":  signal_type,
            "market":       market,
            "status":       new_status,
            "weight":       weight,
            "updated_at":   datetime.now().isoformat(),
        }

        if new_status != current_status:
            logger.warning(
                f"[StrategyAnalyzer] 상태 변경 {signal_type} "
                f"{current_status} → {new_status} "
                f"(EV={new_stats['ev']:+.3f}%, n={new_stats['trade_count']})"
            )

    # ── 일별 통계 갱신 ────────────────────────────────────────

    def _update_daily_stats(self):
        """오늘 날짜 일별 집계를 daily_stats.json에 추가."""
        try:
            today = date.today().isoformat()
            conn  = _get_conn()
            rows  = conn.execute("""
                SELECT market, signal_type, exit_pct, pnl_krw, hold_min
                FROM trades
                WHERE status='closed'
                  AND date(exit_time)=?
            """, (today,)).fetchall()
            conn.close()

            trades = [dict(r) for r in rows]
            if not trades:
                return

            daily = self._load_daily()
            entry = {
                "date":     today,
                "all":      _calc_stats(trades),
                "KR":       _calc_stats([t for t in trades if t["market"] == "KR"]),
                "US":       _calc_stats([t for t in trades if t["market"] == "US"]),
                "by_signal": {}
            }
            for sig in self.KR_SIGNALS + self.US_SIGNALS:
                sub = [t for t in trades if t["signal_type"] == sig]
                if sub:
                    entry["by_signal"][sig] = _calc_stats(sub)

            # 기존 당일 항목 교체
            daily = [d for d in daily if d.get("date") != today]
            daily.append(entry)
            # 최근 90일만 보관
            daily = sorted(daily, key=lambda d: d["date"])[-90:]
            self._save_daily(daily)
        except Exception as e:
            logger.warning(f"[StrategyAnalyzer] 일별 통계 갱신 실패: {e}")

    # ── 조회 API ─────────────────────────────────────────────

    def get_signal_stats(self, market: Optional[str] = None) -> dict:
        """시장별 전략 통계 반환."""
        if market:
            return {k: v for k, v in self._stats.items()
                    if k.startswith(f"{market}:")}
        return self._stats

    def get_status(self, market: str, signal_type: str) -> str:
        """특정 전략 상태 반환 (ACTIVE / WARNING / BLOCK)."""
        raw = self._stats.get(
            f"{market}:{signal_type}", {}
        ).get("status", "ACTIVE")
        # 구버전 DISABLED → BLOCK 하위호환
        return "BLOCK" if raw == "DISABLED" else raw

    def is_active(self, market: str, signal_type: str) -> bool:
        return self.get_status(market, signal_type) not in ("BLOCK", "DISABLED")

    # ── 파일 I/O ─────────────────────────────────────────────

    def _load_stats(self) -> dict:
        try:
            if os.path.exists(_STATS_FILE):
                with open(_STATS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_stats(self):
        with open(_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._stats, f, ensure_ascii=False, indent=2)

    def _load_daily(self) -> list:
        try:
            if os.path.exists(_DAILY_FILE):
                with open(_DAILY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_daily(self, data: list):
        with open(_DAILY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
