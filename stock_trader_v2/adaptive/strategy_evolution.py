"""
adaptive/strategy_evolution.py — 전략 자동 진화 엔진
=====================================================
매일 DAILY_REVIEW 실행 후 자동으로 호출되어:

  1. 전략 A/B 각각 성과 집계 (거래수/승률/평균손익/EV)
  2. 최근 30거래 기준 손실/수익 공통 특징 자동 추출
  3. 개선 후보 필터 생성 (RSI/거래량/BB/시간대/hold_min 등)
  4. 각 후보를 실거래 DB로 백테스트
  5. 기존 전략 대비 승률·EV·합계손익 비교
  6. 개선폭 +10%p 이상 → [ADAPTIVE_PROPOSAL] 출력
  7. 채택안 / 보류안 / 폐기안 3구분 제안 (코드 수정 없음)
  8. EV 최악 원인 TOP3 + EV 개선 가능성 TOP3 자동 산출
  9. 전략 A/B 비중 변경 시뮬레이션 (예상 EV 변화)
 10. 최근 50거래 자본배분 시뮬레이션 (누적수익/MDD)
 11. [내일 추천 운영안] 최종 출력

설계 원칙:
  - 실제 코드 수정 절대 안 함 (제안만)
  - PROPOSAL은 data/evolution_proposals.json 에 누적 저장
  - 최소 30건 이상 있어야 실행 (통계 신뢰성)
  - 각 필터는 독립 백테스트 → 복합 필터도 평가
  - 채택안 없더라도 항상 최선 후보 및 추천 운영안 출력

변경 이력:
  2026-06-23: 초기 작성
  2026-06-23: EV 영향 분석 / 비중 시뮬 / 자본배분 시뮬 / 추천 운영안 추가
"""

from __future__ import annotations

import os
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, date
from typing import Any, Optional

import numpy as np
import pytz

from utils.v2_logger import get_logger

logger = get_logger("StrategyEvolution")
KST    = pytz.timezone("Asia/Seoul")

_DATA_DIR         = os.path.join(os.path.dirname(__file__), "..", "data")
_DB_PATH          = os.path.join(_DATA_DIR, "trade_history.db")
_PROPOSALS_FILE   = os.path.join(_DATA_DIR, "evolution_proposals.json")
_EVOLUTION_REPORT = os.path.join(_DATA_DIR, "evolution_report.json")

# ── 백테스트 필터 개선 임계 ──────────────────────────────────────
_IMPROVEMENT_THRESHOLD_WR  = 10.0   # 승률 개선 +10%p 이상 → 채택안
_IMPROVEMENT_THRESHOLD_EV  = 0.10   # EV 개선 +0.10% 이상 → 채택안
_MIN_TRADES_FOR_ANALYSIS   = 30     # 최소 거래 건수

# ── 시험적용안 승격 임계 ─────────────────────────────────────────
# EV 개선 가능성(ev_impact) TOP1 이 이 값 이상이면
# 채택안 없어도 "시험안"으로 자동 승격하여 내일 운영안에 포함
_TRIAL_THRESHOLD_EV        = 0.30   # EV 개선 +0.30%p 이상 → 시험안 승격

# ── 손절사유→완화 실험 매핑 ──────────────────────────────────────
# {그룹명: (짧은설명, 실험방향, 코드파일, 코드힌트)}
_GROUP_TRIAL_MAP: dict[str, dict] = {
    "돌파봉이탈_손절": {
        "short":      "돌파봉이탈 손절 완화",
        "direction":  "돌파봉저가이탈 조건 허용폭 확대 또는 1회 허용",
        "file":       "position_guard.py",
        "hint":       "position_guard.py: 돌파봉저가이탈 조건 완화\n"
                      "  → BREAKOUT_LOW_TOLERANCE = 0.003 (현재 0) 으로 0.3% 여유\n"
                      "  또는 1회 이탈 후 재진입 허용 로직 추가",
    },
    "약진입_손절": {
        "short":      "약진입(WEAK_ENTRY) 손절 임계 상향",
        "direction":  "WEAK_ENTRY 판단 BUY_SCORE 기준을 소폭 높여 진입 자체를 줄임",
        "file":       "kr_strategy.py",
        "hint":       "kr_strategy.py: BUY_SCORE_EARLY 임계 0.40 → 0.44 상향\n"
                      "  → 약한 진입 자체를 사전 차단하여 WEAK_ENTRY 손절 건수 감소",
    },
    "에어백_손절": {
        "short":      "에어백 손절 허용폭 완화",
        "direction":  "AIRBAG 손절 임계를 현재보다 0.2~0.3%p 낮춰 조기 손절 방지",
        "file":       "position_guard.py",
        "hint":       "position_guard.py: STOPLOSS_HARD_PCT 완화 또는\n"
                      "  에어백 발동 조건 횟수 기준 +1 완화",
    },
    "시간초과_손절": {
        "short":      "시간초과 손절 기준 연장",
        "direction":  "TIME_EXIT 보유시간 임계를 현재보다 2~5분 연장 실험",
        "file":       "position_guard.py",
        "hint":       "position_guard.py: TIME_EXIT_MIN 을 현재값+3분 으로 연장\n"
                      "  → 추가 반등 여유 부여",
    },
    "오후장_손실": {
        "short":      "오후장(11~13시) 신규매수 차단",
        "direction":  "11시 이후 신규 BUY 금지 시험 운영",
        "file":       "kr_strategy.py",
        "hint":       "kr_strategy.py: BUY_STOP_TIME = dtime(11, 0) 으로 단축\n"
                      "  → 오후장 전체 신규매수 차단 실험",
    },
    "RSI과매수_진입": {
        "short":      "RSI 과매수 진입 차단",
        "direction":  "RSI > 70 진입 금지 조건 추가",
        "file":       "kr_strategy.py",
        "hint":       "kr_strategy.py: _eval_entry()에 if iv['rsi'] > 70: return SKIP 추가",
    },
    "저BuyScore_손실": {
        "short":      "저BuyScore 진입 기준 상향",
        "direction":  "BUY_SCORE_EARLY 0.40 → 0.45 상향 실험",
        "file":       "kr_strategy.py",
        "hint":       "kr_strategy.py: BUY_SCORE_EARLY = 0.40 → 0.45 변경",
    },
    "비돌파_손실": {
        "short":      "비돌파(breakout_bonus=0) 진입 차단",
        "direction":  "돌파보너스=0인 진입 비허용 조건 추가",
        "file":       "kr_strategy.py",
        "hint":       "kr_strategy.py: if iv['breakout_bonus'] == 0: return SKIP 추가",
    },
    "즉시청산_손실": {
        "short":      "즉시청산 최소보유 2분 강제",
        "direction":  "보유 2분 미만 청산 방지 쿨다운 추가",
        "file":       "position_guard.py",
        "hint":       "position_guard.py: HOLD_MIN_BEFORE_EXIT = 2.0 추가\n"
                      "  → hold_min < 2.0 이면 WEAK_ENTRY 청산 보류",
    },
    "기타_손실": {
        "short":      "미분류 손실 패턴 추가 모니터링",
        "direction":  "exit_reason 기록 상세화로 분류 정확도 개선",
        "file":       "trade_recorder.py",
        "hint":       "trade_recorder.py: exit_reason에 세부 사유 코드 추가 기록\n"
                      "  → 다음 진화 사이클에서 정밀 분류 가능",
    },
}

# ── 후보 필터 정의 (각 필터가 TRUE이면 해당 거래 진입 허용) ───────
# 형식: (filter_id, label, sql_condition_or_python_fn_desc)
_CANDIDATE_FILTERS = [
    # ── RSI 필터 ──────────────────────────────────────────────
    ("rsi_40_60",   "RSI 40~60 제한",          lambda t: 40 <= (t["rsi"] or 0) <= 60),
    ("rsi_35_65",   "RSI 35~65 제한",          lambda t: 35 <= (t["rsi"] or 0) <= 65),
    ("rsi_45_65",   "RSI 45~65 제한",          lambda t: 45 <= (t["rsi"] or 0) <= 65),
    ("rsi_nonzero", "RSI > 0 (0제거)",         lambda t: (t["rsi"] or 0) > 0),

    # ── BUY_SCORE 필터 ────────────────────────────────────────
    ("bs_045",      "BUY_SCORE ≥ 0.45",        lambda t: (t["buy_score"] or 0) >= 0.45),
    ("bs_050",      "BUY_SCORE ≥ 0.50",        lambda t: (t["buy_score"] or 0) >= 0.50),
    ("bs_055",      "BUY_SCORE ≥ 0.55",        lambda t: (t["buy_score"] or 0) >= 0.55),

    # ── 거래량 필터 ────────────────────────────────────────────
    ("vol_surge",   "거래량폭증(vol_score≥1)",  lambda t: (t["vol_score"] or 0) >= 1),
    ("vol_vwap",    "거래량+VWAP 동시",         lambda t: (t["vol_score"] or 0) >= 1 and (t["vwap_state"] or 0) >= 1),

    # ── 볼린저밴드 / 돌파 필터 ───────────────────────────────
    ("bb_bonus",    "돌파보너스 > 0",           lambda t: (t["breakout_bonus"] or 0) > 0),
    ("bb_strong",   "강한돌파(bonus≥0.2)",      lambda t: (t["breakout_bonus"] or 0) >= 0.2),
    ("no_breakout", "비돌파(bonus=0) 제외",     lambda t: (t["breakout_bonus"] or 0) == 0),

    # ── 시간대 필터 (UTC hour, KST=UTC+9) ────────────────────
    ("hour_09",     "09시대만 (KST)",           lambda t: _get_kst_hour(t["entry_time"]) == 9),
    ("hour_09_10",  "09~10시대 (KST)",          lambda t: _get_kst_hour(t["entry_time"]) in (9, 10)),
    ("hour_09_11",  "09~11시대 (KST)",          lambda t: _get_kst_hour(t["entry_time"]) in (9, 10, 11)),

    # ── 보유시간 필터 ──────────────────────────────────────────
    ("hold_min_2",  "보유≥2분",                 lambda t: (t["hold_min"] or 0) >= 2.0),
    ("hold_min_5",  "보유≥5분",                 lambda t: (t["hold_min"] or 0) >= 5.0),

    # ── 신호유형 필터 ──────────────────────────────────────────
    ("sig_breakout", "초기돌파 전용",            lambda t: "돌파" in (t["signal_type"] or "")),
    ("sig_strong",  "강한/폭발돌파 전용",        lambda t: any(k in (t["signal_type"] or "") for k in ["강한돌파","폭발돌파"])),

    # ── 복합 필터 ─────────────────────────────────────────────
    ("combo_vol_bb",  "거래량+돌파 복합",
     lambda t: (t["vol_score"] or 0) >= 1 and (t["breakout_bonus"] or 0) > 0),
    ("combo_vol_vwap_bs050", "거래량+VWAP+BS≥0.50",
     lambda t: (t["vol_score"] or 0) >= 1 and (t["vwap_state"] or 0) >= 1 and (t["buy_score"] or 0) >= 0.50),
    ("combo_09_vol_bb", "09시+거래량+돌파",
     lambda t: _get_kst_hour(t["entry_time"]) == 9 and (t["vol_score"] or 0) >= 1 and (t["breakout_bonus"] or 0) > 0),
]

# ── 전략 B 전용 필터 ──────────────────────────────────────────────
_STRATEGY_B_FILTERS = [
    ("b_rsi_40_60",    "B: RSI 40~60 엄격",
     lambda t: 40 <= (t["rsi"] or 0) <= 60),
    ("b_rsi_35_65",    "B: RSI 35~65 완화",
     lambda t: 35 <= (t["rsi"] or 0) <= 65),
    ("b_low_bs",       "B: BUY_SCORE 0.36~0.45",
     lambda t: 0.36 <= (t["buy_score"] or 0) <= 0.45),
    ("b_vol_only",     "B: 거래량만",
     lambda t: (t["vol_score"] or 0) >= 1),
]


# ════════════════════════════════════════════════════════════════
# 유틸 함수
# ════════════════════════════════════════════════════════════════

def _get_kst_hour(entry_time_str: Optional[str]) -> int:
    """entry_time(ISO 문자열) → KST hour 반환. 실패 시 -1."""
    if not entry_time_str:
        return -1
    try:
        # DB에 UTC로 저장됨 → +9
        dt_str = entry_time_str[:19]
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        return (dt.hour + 9) % 24
    except Exception:
        return -1


def _ev(pcts: list[float]) -> float:
    return float(np.mean(pcts)) if pcts else 0.0


def _win_rate(pcts: list[float]) -> float:
    if not pcts:
        return 0.0
    return sum(1 for x in pcts if x > 0) / len(pcts) * 100


# ════════════════════════════════════════════════════════════════
class StrategyEvolutionEngine:
    """
    DAILY_REVIEW 후 자동 호출.
    실거래 DB 분석 → 필터 백테스트 → [ADAPTIVE_PROPOSAL] 제안.

    사용법:
        engine = StrategyEvolutionEngine()
        proposals = engine.run("KR")
    """

    @staticmethod
    def _conn() -> sqlite3.Connection:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    # ────────────────────────────────────────────────────────────
    # 메인 실행
    # ────────────────────────────────────────────────────────────

    def run(self, market: str = "KR",
            window: int = 30,
            full_window: int = 200) -> dict:
        """
        Args:
            market: "KR" or "US"
            window: 최근 N건 특징 추출용
            full_window: 백테스트용 전체 창
        Returns:
            proposals dict (채택안/보류안/폐기안 포함)
        """
        market_up = market.upper()
        now_str   = datetime.now(KST).isoformat()
        logger.info(f"[EVOLUTION] ── 전략 진화 분석 시작 | market={market_up} ──")

        # ── 데이터 로드 ──────────────────────────────────────
        all_trades  = self._fetch_closed(market_up, full_window)
        recent30    = all_trades[:window]  # 최신순 → 상위 30건

        if len(all_trades) < _MIN_TRADES_FOR_ANALYSIS:
            logger.info(
                f"[EVOLUTION] 거래 건수 부족 "
                f"({len(all_trades)}/{_MIN_TRADES_FOR_ANALYSIS}) → 건너뜀"
            )
            return {"status": "insufficient_data",
                    "count": len(all_trades),
                    "required": _MIN_TRADES_FOR_ANALYSIS}

        # ── [1] 전략 A/B 성과 집계 ──────────────────────────
        strategy_perf = self._calc_strategy_performance(all_trades)

        # ── [2] 손실/수익 공통 특징 추출 ────────────────────
        features = self._extract_features(recent30)

        # ── [3] 기준선 (전체 성과) ──────────────────────────
        baseline = self._calc_baseline(all_trades)

        # ── [4] 후보 필터 백테스트 ──────────────────────────
        backtest_results = self._run_filter_backtest(all_trades, baseline)

        # ── [5] 전략 B 전용 필터 백테스트 ──────────────────
        if strategy_perf.get("B", {}).get("count", 0) >= 10:
            b_trades = [t for t in all_trades
                        if (t.get("strategy") or "A") == "B"]
            b_baseline = self._calc_baseline(b_trades)
            b_results  = self._run_filter_backtest(
                b_trades, b_baseline, filters=_STRATEGY_B_FILTERS
            )
            backtest_results.extend(b_results)

        # ── [6] 제안 분류 (채택/보류/폐기) ─────────────────
        proposals = self._classify_proposals(
            backtest_results, baseline, features, strategy_perf
        )

        # ── [7] EV 영향 분석 (최악 원인 / 개선 가능성) ─────
        ev_impact = self._extract_ev_impact(recent30)

        # ── [8] 전략 비중 변경 시뮬레이션 ──────────────────
        weight_sim = self._simulate_weight_mix(strategy_perf)

        # ── [9] 자본배분 시뮬레이션 (최근 50거래) ──────────
        trades50     = all_trades[:50]
        capital_sim  = self._simulate_capital_allocation(trades50)

        # ── [10] 내일 추천 운영안 생성 ──────────────────────
        recommendation = self._generate_recommendation(
            strategy_perf, weight_sim, capital_sim, baseline, proposals,
            ev_impact   # ← EV 영향 분석 결과 추가 전달
        )

        # ── 로그 출력 ────────────────────────────────────
        self._print_evolution_report(
            market_up, strategy_perf, features, baseline, proposals,
            ev_impact, weight_sim, capital_sim, recommendation, now_str
        )

        # ── 파일 저장 ─────────────────────────────────────
        result = {
            "generated_at":   now_str,
            "market":         market_up,
            "trade_count":    len(all_trades),
            "recent_window":  window,
            "baseline":       baseline,
            "strategy_perf":  strategy_perf,
            "features":       features,
            "proposals":      proposals,
            "ev_impact":      ev_impact,
            "weight_sim":     weight_sim,
            "capital_sim":    capital_sim,
            "recommendation": recommendation,
        }
        self._save_proposals(result)
        logger.info(
            f"[EVOLUTION] 완료 | "
            f"채택={len(proposals['채택안'])} / "
            f"보류={len(proposals['보류안'])} / "
            f"폐기={len(proposals['폐기안'])}"
        )
        return result

    # ────────────────────────────────────────────────────────────
    # [1] 전략 A/B 성과 집계
    # ────────────────────────────────────────────────────────────

    def _calc_strategy_performance(self, trades: list[dict]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for strat in ("A", "B"):
            sub = [t for t in trades if (t.get("strategy") or "A") == strat]
            if not sub:
                continue
            pcts = [float(t["exit_pct"] or 0) for t in sub]
            wins = [p for p in pcts if p > 0]
            wr   = len(wins) / len(pcts) * 100
            avg  = float(np.mean(pcts))
            ev   = avg
            result[strat] = {
                "count":    len(sub),
                "win_rate": round(wr, 1),
                "avg_pct":  round(avg, 3),
                "ev":       round(ev, 3),
                "total_pct": round(sum(pcts), 3),
            }
        return result

    # ────────────────────────────────────────────────────────────
    # [2] 손실/수익 공통 특징 추출
    # ────────────────────────────────────────────────────────────

    def _extract_features(self, trades: list[dict]) -> dict:
        """
        최근 N건 기준으로 손실/수익 거래의 공통 특징을 추출.
        수치형 지표 비교 + 빈도 기반 패턴 탐지.
        """
        if not trades:
            return {}

        wins   = [t for t in trades if (t.get("exit_pct") or 0) > 0]
        losses = [t for t in trades if (t.get("exit_pct") or 0) <= 0]

        def stat(arr: list[float]) -> dict:
            if not arr:
                return {"mean": 0.0, "median": 0.0, "std": 0.0}
            return {
                "mean":   round(float(np.mean(arr)), 3),
                "median": round(float(np.median(arr)), 3),
                "std":    round(float(np.std(arr)), 3),
            }

        def freq(trades_: list[dict], key: str, top_n: int = 3) -> list:
            """지정 키 값의 빈도 Top-N."""
            counts: dict = defaultdict(int)
            for t in trades_:
                val = t.get(key)
                if val is not None:
                    # 수치형은 구간으로 변환
                    if isinstance(val, float):
                        val = round(val, 1)
                    counts[str(val)] += 1
            return sorted(counts.items(), key=lambda x: -x[1])[:top_n]

        # ── 수치 비교 ─────────────────────────────────────────
        def _vals(lst, key):
            return [float(t[key]) for t in lst if t.get(key) is not None]

        win_bs     = _vals(wins,   "buy_score")
        loss_bs    = _vals(losses, "buy_score")
        win_rsi    = _vals(wins,   "rsi")
        loss_rsi   = _vals(losses, "rsi")
        win_hold   = _vals(wins,   "hold_min")
        loss_hold  = _vals(losses, "hold_min")
        win_bb     = _vals(wins,   "breakout_bonus")
        loss_bb    = _vals(losses, "breakout_bonus")

        # ── 시간대 분포 ───────────────────────────────────────
        win_hours  = [_get_kst_hour(t.get("entry_time")) for t in wins]
        loss_hours = [_get_kst_hour(t.get("entry_time")) for t in losses]
        hour_counts: dict = defaultdict(lambda: {"win": 0, "loss": 0})
        for h in win_hours:
            if h >= 0:
                hour_counts[h]["win"] += 1
        for h in loss_hours:
            if h >= 0:
                hour_counts[h]["loss"] += 1

        # ── 손절사유 패턴 ────────────────────────────────────
        loss_reasons: dict = defaultdict(int)
        for t in losses:
            r = t.get("exit_reason") or ""
            if   "WEAK_ENTRY"  in r: loss_reasons["WEAK_ENTRY_EXIT"] += 1
            elif "돌파봉저가이탈" in r: loss_reasons["돌파봉이탈"]   += 1
            elif "에어백"       in r: loss_reasons["에어백손절"]      += 1
            elif "TIME_EXIT"   in r: loss_reasons["TIME_EXIT"]        += 1
            else:                    loss_reasons["기타"]             += 1

        win_reasons: dict = defaultdict(int)
        for t in wins:
            r = t.get("exit_reason") or ""
            if   "PROFIT_PROTECT" in r: win_reasons["PROFIT_PROTECT"] += 1
            elif "전량익절"        in r: win_reasons["전량익절"]       += 1
            elif "익절+SELL"      in r: win_reasons["익절+SELL"]      += 1
            else:                       win_reasons["기타"]            += 1

        # ── 손실 공통 특징 추출 ───────────────────────────────
        loss_traits: list[str] = []
        win_traits:  list[str] = []

        # BUY_SCORE
        if win_bs and loss_bs:
            w_mean = np.mean(win_bs)
            l_mean = np.mean(loss_bs)
            if abs(w_mean - l_mean) >= 0.02:
                loss_traits.append(
                    f"BUY_SCORE 낮음: 손실={l_mean:.3f} vs 수익={w_mean:.3f}"
                )

        # RSI (rsi>0인 것만 의미 있음)
        loss_rsi_nz = [r for r in loss_rsi if r > 0]
        win_rsi_nz  = [r for r in win_rsi  if r > 0]
        if loss_rsi_nz and win_rsi_nz:
            l_rsi = np.mean(loss_rsi_nz)
            w_rsi = np.mean(win_rsi_nz)
            if abs(l_rsi - w_rsi) >= 3:
                if l_rsi > w_rsi:
                    loss_traits.append(f"RSI 과매수 진입: 손실={l_rsi:.0f} vs 수익={w_rsi:.0f}")
                else:
                    loss_traits.append(f"RSI 과매도 진입: 손실={l_rsi:.0f} vs 수익={w_rsi:.0f}")

        # 보유시간
        if win_hold and loss_hold:
            w_hold = np.mean(win_hold)
            l_hold = np.mean(loss_hold)
            if w_hold - l_hold >= 2:
                win_traits.append(f"보유시간 길수록 유리: 수익={w_hold:.1f}분 vs 손실={l_hold:.1f}분")
            elif l_hold - w_hold >= 2:
                loss_traits.append(f"장기보유 손실 패턴: 손실={l_hold:.1f}분 vs 수익={w_hold:.1f}분")

        # 돌파보너스
        if win_bb and loss_bb:
            w_bb = np.mean(win_bb)
            l_bb = np.mean(loss_bb)
            if w_bb - l_bb >= 0.05:
                win_traits.append(f"돌파강도 강할수록 유리: 수익={w_bb:.2f} vs 손실={l_bb:.2f}")
            elif l_bb - w_bb >= 0.05:
                loss_traits.append(f"약한 돌파 손실: 손실={l_bb:.2f} vs 수익={w_bb:.2f}")

        # 시간대
        bad_hours  = [h for h, d in hour_counts.items()
                      if d["win"] + d["loss"] >= 3
                      and d["loss"] / (d["win"] + d["loss"]) >= 0.85]
        good_hours = [h for h, d in hour_counts.items()
                      if d["win"] + d["loss"] >= 3
                      and d["win"] / (d["win"] + d["loss"]) >= 0.40]

        if bad_hours:
            bad_detail = ", ".join(
                f"{h}시 L{hour_counts[h]['loss']}/W{hour_counts[h]['win']}"
                for h in sorted(bad_hours)
            )
            loss_traits.append(
                f"손실 집중 시간대: {sorted(bad_hours)}시 ({bad_detail})"
            )
        if good_hours:
            win_traits.append(
                f"수익 집중 시간대: {sorted(good_hours)}시"
            )

        # 손절 사유 주도 패턴
        if loss_reasons:
            top_reason = max(loss_reasons.items(), key=lambda x: x[1])
            r_pct = top_reason[1] / len(losses) * 100 if losses else 0
            if r_pct >= 50:
                loss_traits.append(
                    f"주요 손절 사유: {top_reason[0]} ({top_reason[1]}건, {r_pct:.0f}%)"
                )

        return {
            "sample_size": len(trades),
            "win_count":   len(wins),
            "loss_count":  len(losses),
            "buy_score":   {"win": stat(win_bs), "loss": stat(loss_bs)},
            "rsi":         {"win": stat(win_rsi_nz), "loss": stat(loss_rsi_nz)},
            "hold_min":    {"win": stat(win_hold), "loss": stat(loss_hold)},
            "breakout_bonus": {"win": stat(win_bb), "loss": stat(loss_bb)},
            "hour_dist":   dict(hour_counts),
            "loss_reasons": dict(loss_reasons),
            "win_reasons":  dict(win_reasons),
            "loss_traits": loss_traits,
            "win_traits":  win_traits,
        }

    # ────────────────────────────────────────────────────────────
    # [3] 기준선 계산
    # ────────────────────────────────────────────────────────────

    def _calc_baseline(self, trades: list[dict]) -> dict:
        if not trades:
            return {"count": 0, "win_rate": 0, "avg_pct": 0, "ev": 0, "total_pct": 0}
        pcts = [float(t["exit_pct"] or 0) for t in trades]
        wins = [p for p in pcts if p > 0]
        wr   = len(wins) / len(pcts) * 100
        avg  = float(np.mean(pcts))
        return {
            "count":     len(pcts),
            "win_rate":  round(wr, 1),
            "avg_pct":   round(avg, 3),
            "ev":        round(avg, 3),
            "total_pct": round(sum(pcts), 3),
        }

    # ────────────────────────────────────────────────────────────
    # [4] 후보 필터 백테스트
    # ────────────────────────────────────────────────────────────

    def _run_filter_backtest(
        self,
        trades: list[dict],
        baseline: dict,
        filters: list = None,
    ) -> list[dict]:
        """
        각 필터를 적용했을 때 전략 성과 시뮬레이션.
        baseline 대비 개선폭 계산 포함.
        """
        if filters is None:
            filters = _CANDIDATE_FILTERS

        results = []
        base_wr  = baseline["win_rate"]
        base_ev  = baseline["ev"]
        base_cnt = baseline["count"]

        for fid, label, fn in filters:
            try:
                filtered = [t for t in trades if fn(t)]
            except Exception as e:
                logger.debug(f"[EVOLUTION] 필터 {fid} 오류: {e}")
                continue

            if len(filtered) < 5:
                # 최소 5건 미만 → 통계 신뢰성 없음
                # buy_score/rsi 관련이면 RSI 버그 맥락 주석
                reason_hint = ""
                if fid.startswith("rsi") or fid.startswith("bs_") or fid.startswith("bb_strong") or fid.startswith("sig_strong"):
                    reason_hint = " ※ RSI 버그 수정(2026-06-23) 이후 데이터 누적 필요"
                results.append({
                    "filter_id":    fid,
                    "label":        label,
                    "status":       "insufficient",
                    "count":        len(filtered),
                    "filter_rate":  round((1 - len(filtered)/base_cnt)*100, 1) if base_cnt else 0,
                    "reason_hint":  reason_hint,
                })
                continue

            pcts = [float(t["exit_pct"] or 0) for t in filtered]
            wins = [p for p in pcts if p > 0]
            wr   = len(wins) / len(pcts) * 100
            avg  = float(np.mean(pcts))

            # 개선폭
            delta_wr = wr - base_wr
            delta_ev = avg - base_ev

            results.append({
                "filter_id":    fid,
                "label":        label,
                "status":       "evaluated",
                "count":        len(filtered),
                "filter_rate":  round((1 - len(filtered)/base_cnt)*100, 1) if base_cnt else 0,
                "win_rate":     round(wr,  1),
                "avg_pct":      round(avg, 3),
                "ev":           round(avg, 3),
                "total_pct":    round(sum(pcts), 3),
                "delta_wr":     round(delta_wr, 1),
                "delta_ev":     round(delta_ev, 3),
                "meets_threshold": (
                    delta_wr >= _IMPROVEMENT_THRESHOLD_WR or
                    delta_ev >= _IMPROVEMENT_THRESHOLD_EV
                ),
            })

        return results

    # ────────────────────────────────────────────────────────────
    # [5] 제안 분류 (채택/보류/폐기)
    # ────────────────────────────────────────────────────────────

    def _classify_proposals(
        self,
        backtest_results: list[dict],
        baseline: dict,
        features: dict,
        strategy_perf: dict,
    ) -> dict[str, list[dict]]:
        """
        백테스트 결과를 3가지로 분류:

        채택안: 개선폭 >= 임계 AND 잔여 거래수 >= 15건 AND 필터율 < 70%
        보류안: 개선폭은 있지만 잔여 거래수 부족 OR 필터율 과도
        폐기안: 개선폭 없거나 오히려 악화

        각 항목에 '실제 코드에서 수정해야 할 파라미터' 제안 포함.
        """
        채택안 = []
        보류안 = []
        폐기안 = []

        # EV 기준 내림차순 정렬
        evaluated = [r for r in backtest_results if r.get("status") == "evaluated"]
        evaluated.sort(key=lambda x: x.get("delta_ev", -999), reverse=True)

        for r in evaluated:
            delta_wr   = r.get("delta_wr", 0)
            delta_ev   = r.get("delta_ev", 0)
            count      = r.get("count", 0)
            filter_rt  = r.get("filter_rate", 100)
            meets      = r.get("meets_threshold", False)

            proposal = {
                "filter_id":   r["filter_id"],
                "label":       r["label"],
                "count":       count,
                "filter_rate": filter_rt,
                "win_rate":    r.get("win_rate", 0),
                "ev":          r.get("ev", 0),
                "delta_wr":    delta_wr,
                "delta_ev":    delta_ev,
                "code_hint":   self._generate_code_hint(r["filter_id"], r),
                "reason":      "",
            }

            if not meets:
                # 개선폭 미달
                if delta_ev < -0.05 or delta_wr < -5:
                    proposal["reason"] = (
                        f"성과 악화: EV={delta_ev:+.3f}%p, 승률={delta_wr:+.1f}%p"
                    )
                    폐기안.append(proposal)
                else:
                    proposal["reason"] = (
                        f"개선폭 미달: EV={delta_ev:+.3f}%p(기준≥{_IMPROVEMENT_THRESHOLD_EV}), "
                        f"승률={delta_wr:+.1f}%p(기준≥{_IMPROVEMENT_THRESHOLD_WR})"
                    )
                    폐기안.append(proposal)
            elif count < 15:
                proposal["reason"] = (
                    f"잔여 거래 {count}건 < 15건 최소 기준 (통계 불안정)"
                )
                보류안.append(proposal)
            elif filter_rt >= 70:
                proposal["reason"] = (
                    f"필터율 {filter_rt}% ≥ 70% — 기회 과도 감소 우려"
                )
                보류안.append(proposal)
            else:
                proposal["reason"] = (
                    f"✅ 개선폭 충족: EV={delta_ev:+.3f}%p, 승률={delta_wr:+.1f}%p, "
                    f"잔여 {count}건, 필터율 {filter_rt}%"
                )
                채택안.append(proposal)

        # insufficient 항목은 보류
        for r in backtest_results:
            if r.get("status") == "insufficient":
                reason_hint = r.get("reason_hint", "")
                보류안.append({
                    "filter_id": r["filter_id"],
                    "label":     r["label"],
                    "count":     r["count"],
                    "reason":    f"데이터 {r['count']}건으로 평가 불가 (최소 5건){reason_hint}",
                    "code_hint": self._generate_code_hint(r["filter_id"], r),
                    "delta_wr":  0,
                    "delta_ev":  0,
                })

        return {
            "채택안": 채택안,
            "보류안": 보류안,
            "폐기안": 폐기안,
        }

    # ────────────────────────────────────────────────────────────
    # ① 채택안 없을 때 최선 후보 강제 선정 (내부 유틸)
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _pick_best_pending(proposals: dict) -> dict | None:
        """
        채택안이 0개일 때 보류안 중 delta_ev 최고치를 '최선 후보'로 반환.
        evaluated(delta_ev 있는 것) 우선, insufficient는 차선.
        """
        candidates = [
            p for p in proposals["보류안"]
            if p.get("delta_ev") is not None and p.get("delta_ev", 0) > 0
        ]
        if not candidates:
            # delta_ev 양수 없으면 폐기안 중 최소 손해
            candidates = [
                p for p in proposals["폐기안"]
                if p.get("delta_ev") is not None
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda x: x.get("delta_ev", -999))

    # ────────────────────────────────────────────────────────────
    # ② EV 영향 분석 — 최악 원인 TOP3 + 개선 가능성 TOP3
    # ────────────────────────────────────────────────────────────

    def _extract_ev_impact(self, trades: list[dict]) -> dict:
        """
        최근 N건(recent30) 기준:
          - EV 최악 원인 TOP3: 손절사유 그룹별 EV 기여도 (가장 많이 갉아먹는 패턴)
          - EV 개선 가능성 TOP3: 해당 그룹 제거 시 EV 상승폭이 가장 큰 패턴

        EV 기여도 = (그룹 건수 / 전체 건수) × 그룹 평균 손익%
        제거 시 개선 EV = (전체 EV × 전체 건수 - 그룹기여%) / (전체 건수 - 그룹 건수)
        """
        if not trades:
            return {"worst_causes": [], "best_improvements": []}

        all_pcts  = [float(t.get("exit_pct") or 0) for t in trades]
        total_n   = len(all_pcts)
        base_ev   = float(np.mean(all_pcts)) if all_pcts else 0.0

        # ── 그룹 분류 함수 ────────────────────────────────────
        def _classify_group(t: dict) -> str:
            """거래 하나를 분석 그룹으로 분류."""
            pct    = float(t.get("exit_pct") or 0)
            reason = t.get("exit_reason") or ""
            hour   = _get_kst_hour(t.get("entry_time"))
            rsi    = float(t.get("rsi") or 0)
            bs     = float(t.get("buy_score") or 0)
            hold   = float(t.get("hold_min") or 0)
            bb     = float(t.get("breakout_bonus") or 0)

            # 손실 거래만 원인 분류 (수익 거래는 'PROFIT' 단일 그룹)
            if pct > 0:
                return "PROFIT"

            if   "WEAK_ENTRY"  in reason: return "약진입_손절"
            elif "에어백"       in reason: return "에어백_손절"
            elif "TIME_EXIT"   in reason: return "시간초과_손절"
            elif "돌파봉저가이탈" in reason: return "돌파봉이탈_손절"
            elif hour in (11, 12, 13):    return "오후장_손실"
            elif rsi > 70:                return "RSI과매수_진입"
            elif bs < 0.43:               return "저BuyScore_손실"
            elif bb == 0:                 return "비돌파_손실"
            elif hold < 1.0:              return "즉시청산_손실"
            else:                         return "기타_손실"

        # ── 그룹별 집계 ──────────────────────────────────────
        groups: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            g = _classify_group(t)
            groups[g].append(float(t.get("exit_pct") or 0))

        # ── EV 기여도 + 제거 시 개선폭 산출 ─────────────────
        impact_rows: list[dict] = []
        for group, pcts in groups.items():
            if group == "PROFIT":
                continue                # 수익 그룹은 원인 분석 제외
            n        = len(pcts)
            g_avg    = float(np.mean(pcts))
            # 기여도: 해당 그룹이 전체 EV를 얼마나 끌어내리는지
            # contribution = (n/total_n) × g_avg  (음수일수록 더 나쁨)
            contribution = (n / total_n) * g_avg

            # 제거 후 EV: 해당 그룹 빼고 나머지 평균
            rest_pcts = [p for t, p in zip(trades, all_pcts)
                         if _classify_group(t) != group]
            if rest_pcts:
                ev_without = float(np.mean(rest_pcts))
                delta_ev   = ev_without - base_ev
            else:
                ev_without = base_ev
                delta_ev   = 0.0

            impact_rows.append({
                "group":        group,
                "count":        n,
                "ratio_pct":    round(n / total_n * 100, 1),
                "avg_pct":      round(g_avg, 3),
                "contribution": round(contribution, 4),
                "ev_without":   round(ev_without, 3),
                "delta_ev":     round(delta_ev, 3),
            })

        # ── TOP3 선정 ────────────────────────────────────────
        # 최악 원인: contribution 가장 낮은(음수 큰) 순
        worst3 = sorted(
            impact_rows, key=lambda x: x["contribution"]
        )[:3]

        # 개선 가능성: 제거 시 delta_ev 가장 큰 순
        best3 = sorted(
            impact_rows, key=lambda x: x["delta_ev"], reverse=True
        )[:3]

        return {
            "base_ev":          round(base_ev, 3),
            "total_count":      total_n,
            "worst_causes":     worst3,
            "best_improvements": best3,
            "all_groups":       sorted(impact_rows,
                                       key=lambda x: x["contribution"]),
        }

    # ────────────────────────────────────────────────────────────
    # ③ 전략 비중 변경 시뮬레이션
    # ────────────────────────────────────────────────────────────

    def _simulate_weight_mix(self, strategy_perf: dict) -> dict:
        """
        전략 A/B 비중 배합별 예상 EV 계산.

        예상 EV = EV_A × w_A + EV_B × w_B
        예상 WR = WR_A × w_A + WR_B × w_B  (가중 평균)

        비중 배합: A:100 / A:80+B:20 / A:70+B:30 /
                   A:50+B:50 / A:30+B:70 / B:100
        """
        mixes = [
            (1.00, 0.00),
            (0.80, 0.20),
            (0.70, 0.30),
            (0.50, 0.50),
            (0.30, 0.70),
            (0.00, 1.00),
        ]

        perf_a = strategy_perf.get("A", {})
        perf_b = strategy_perf.get("B", {})

        ev_a = perf_a.get("ev", 0.0)
        ev_b = perf_b.get("ev", 0.0)
        wr_a = perf_a.get("win_rate", 0.0)
        wr_b = perf_b.get("win_rate", 0.0)
        cnt_a = perf_a.get("count", 0)
        cnt_b = perf_b.get("count", 0)

        # B 데이터가 없으면 A와 동일 추정치로 fallback
        if cnt_b == 0:
            ev_b = ev_a
            wr_b = wr_a

        rows: list[dict] = []
        for wa, wb in mixes:
            sim_ev = ev_a * wa + ev_b * wb
            sim_wr = wr_a * wa + wr_b * wb

            # 현행 대비 변화 (현행 = A100%)
            current_ev = ev_a
            current_wr = wr_a
            delta_ev = sim_ev - current_ev
            delta_wr = sim_wr - current_wr

            rows.append({
                "weight_a":   int(wa * 100),
                "weight_b":   int(wb * 100),
                "label":      f"A{int(wa*100)}+B{int(wb*100)}"
                              if 0 < wa < 1 and 0 < wb < 1
                              else ("A100" if wa == 1.0 else "B100"),
                "sim_ev":     round(sim_ev, 3),
                "sim_wr":     round(sim_wr, 1),
                "delta_ev":   round(delta_ev, 3),
                "delta_wr":   round(delta_wr, 1),
                "is_current": wa == 1.0 and wb == 0.0,
                "b_data_available": cnt_b >= 10,
            })

        # 최적 비중: sim_ev 최대
        best = max(rows, key=lambda x: x["sim_ev"])

        return {
            "ev_a":       ev_a,
            "ev_b":       ev_b,
            "wr_a":       wr_a,
            "wr_b":       wr_b,
            "cnt_a":      cnt_a,
            "cnt_b":      cnt_b,
            "b_data_available": cnt_b >= 10,
            "mixes":      rows,
            "best_mix":   best,
        }

    # ────────────────────────────────────────────────────────────
    # ④ 전략별 자본배분 시뮬레이션 (최근 50거래)
    # ────────────────────────────────────────────────────────────

    def _simulate_capital_allocation(self, trades50: list[dict]) -> dict:
        """
        최근 50거래 기준 3가지 자본배분 비교:
          - A100%:   A 거래만 사용
          - A50+B50: A/B 각 50% 비율로 혼합
          - B100%:   B 거래만 사용

        각각:
          - 누적 수익률 (복리, 초기자본=1.0)
          - 최대 낙폭 (MDD)
          - 최종 거래수 / 승률 / EV

        B 거래가 없으면 B=A로 추정(경고 표시).
        """
        if not trades50:
            return {"status": "no_data", "scenarios": []}

        a_trades = [t for t in trades50 if (t.get("strategy") or "A") == "A"]
        b_trades = [t for t in trades50 if (t.get("strategy") or "A") == "B"]

        # B 거래 없으면 A 동일 데이터로 추정
        b_estimated = len(b_trades) == 0
        if b_estimated:
            b_trades = a_trades  # fallback

        def _simulate_portfolio(
            trades_a: list[dict],
            trades_b: list[dict],
            ratio_a: float,
            ratio_b: float,
        ) -> dict:
            """
            두 전략 거래를 시간순 병합 후 비율 가중하여 자본 시뮬.
            ratio_a + ratio_b == 1.0 가정.
            """
            # 시간순 정렬 (entry_time 기준)
            def _sort_key(t):
                return t.get("entry_time") or ""

            # 두 전략 섞어서 합산 (비율 가중)
            # 단순 모델: A 거래 수익에 ratio_a, B 거래 수익에 ratio_b 적용
            # 복합 모델: 가중 평균 수익률 스트림 생성
            combined: list[float] = []
            if ratio_a > 0 and ratio_b > 0:
                # 둘 다 섞을 때: 시간축 병합 후 비율 적용
                stream_a = sorted(trades_a, key=_sort_key)
                stream_b = sorted(trades_b, key=_sort_key)
                # 각 거래 수익을 비율 가중
                for t in stream_a:
                    combined.append(float(t.get("exit_pct") or 0) * ratio_a)
                for t in stream_b:
                    combined.append(float(t.get("exit_pct") or 0) * ratio_b)
                combined.sort()  # 시간 미분리 시 평균적 순서 유지
            elif ratio_a == 1.0:
                combined = [float(t.get("exit_pct") or 0) for t in
                            sorted(trades_a, key=_sort_key)]
            else:
                combined = [float(t.get("exit_pct") or 0) for t in
                            sorted(trades_b, key=_sort_key)]

            if not combined:
                return {
                    "count": 0, "win_rate": 0,
                    "ev": 0, "cumulative_pct": 0, "mdd": 0,
                }

            # 복리 누적 수익률
            equity = 1.0
            peak   = 1.0
            mdd    = 0.0
            for r in combined:
                equity *= (1 + r / 100)
                if equity > peak:
                    peak = equity
                drawdown = (peak - equity) / peak * 100
                if drawdown > mdd:
                    mdd = drawdown

            cum_pct = (equity - 1.0) * 100
            wins    = [r for r in combined if r > 0]
            wr      = len(wins) / len(combined) * 100 if combined else 0
            ev      = float(np.mean(combined)) if combined else 0

            return {
                "count":          len(combined),
                "win_rate":       round(wr, 1),
                "ev":             round(ev, 3),
                "cumulative_pct": round(cum_pct, 2),
                "mdd":            round(mdd, 2),
                "final_equity":   round(equity, 4),
            }

        scenarios = [
            {
                "label":   "A 100%",
                "ratio_a": 1.0, "ratio_b": 0.0,
                "result":  _simulate_portfolio(a_trades, b_trades, 1.0, 0.0),
            },
            {
                "label":   "A 50% + B 50%",
                "ratio_a": 0.5, "ratio_b": 0.5,
                "result":  _simulate_portfolio(a_trades, b_trades, 0.5, 0.5),
            },
            {
                "label":   "B 100%",
                "ratio_a": 0.0, "ratio_b": 1.0,
                "result":  _simulate_portfolio(a_trades, b_trades, 0.0, 1.0),
            },
        ]

        # 최적 시나리오: 누적 수익률 기준
        best_scenario = max(
            scenarios, key=lambda x: x["result"].get("cumulative_pct", -999)
        )

        return {
            "trade_count":   len(trades50),
            "a_count":       len(a_trades),
            "b_count":       len(b_trades) if not b_estimated else 0,
            "b_estimated":   b_estimated,
            "scenarios":     scenarios,
            "best_scenario": best_scenario["label"],
        }

    # ────────────────────────────────────────────────────────────
    # ⑤ 내일 추천 운영안 생성 — [보수안] / [시험안] / [공격안] 3단계
    # ────────────────────────────────────────────────────────────

    def _generate_recommendation(
        self,
        strategy_perf: dict,
        weight_sim:    dict,
        capital_sim:   dict,
        baseline:      dict,
        proposals:     dict,
        ev_impact:     dict,
    ) -> dict:
        """
        [내일 추천 운영안] — 3단계 찬별 출력용 데이터 생성.

        ┌─────────────────────────────────────────────────────────────┐
        │ [보수안] 현행 유지                                           │
        │ [시험안] EV 개선 가능성 TOP1 적용 (delta_ev >= 0.30%p 시)  │
        │ [공격안] TOP1 + TOP2 동시 적용                              │
        └─────────────────────────────────────────────────────────────┘

        각 안마다: 예상 거래수 / 예상 승률 / 예상 EV / 예상 일손익 / 위험요소

        결정 규칙:
          - 채택안 있으면 → 채택안을 시험안/공격안의 기반 필터로 사용
          - 채택안 없고 ev_impact TOP1 delta_ev >= _TRIAL_THRESHOLD_EV
            → 해당 그룹 "시험적용안"으로 자동 승격
          - B 데이터 10건 이상이면 비중 최적화 추천
        """
        perf_a = strategy_perf.get("A", {})
        perf_b = strategy_perf.get("B", {})
        cnt_a  = perf_a.get("count", 0)
        cnt_b  = perf_b.get("count", 0)
        ev_a   = perf_a.get("ev",       0.0)
        ev_b   = perf_b.get("ev",       0.0)
        wr_a   = perf_a.get("win_rate", 0.0)
        wr_b   = perf_b.get("win_rate", 0.0)
        if cnt_b == 0:
            ev_b, wr_b = ev_a, wr_a

        채택안       = proposals.get("채택안", [])
        b_available  = weight_sim.get("b_data_available", False)
        best_mix     = weight_sim.get("best_mix", {})
        best3_imp    = ev_impact.get("best_improvements", [])
        base_ev      = baseline.get("ev", ev_a)

        # ── 일간 거래수 추정 ─────────────────────────────────
        est_days    = 30
        daily_a     = cnt_a / est_days
        daily_b     = cnt_b / est_days if cnt_b > 0 else 0.0

        def _make_scenario(
            label:     str,
            wa:        int,
            wb:        int,
            ev_boost:  float,       # EV 기대 개선폭 (필터 효과)
            wr_boost:  float,       # 승률 기대 개선폭
            trade_scale: float,     # 거래수 배율 (필터 시 거래 감소)
            filters:   list[str],   # 적용 필터 설명 목록
            risks:     list[str],   # 위험요소 목록
        ) -> dict:
            wa_f  = wa / 100
            wb_f  = wb / 100
            # EV / 승률: 비중 가중 평균 + 필터 부스트
            s_ev  = round(ev_a * wa_f + ev_b * wb_f + ev_boost, 3)
            s_wr  = round(wr_a * wa_f + wr_b * wb_f + wr_boost, 1)
            # 예상 일거래수
            s_cnt = round((daily_a * wa_f + daily_b * wb_f) * trade_scale, 1)
            s_cnt = max(0.5, s_cnt)
            # 예상 일손익 (EV% × 거래수 → 단순 합산, 실제 금액은 포지션크기 미반영)
            # 부호+상대 크기만 의미있음
            daily_ev_pct = round(s_ev * s_cnt, 3)
            return {
                "label":        label,
                "weight_a":     wa,
                "weight_b":     wb,
                "exp_ev":       s_ev,
                "exp_wr":       s_wr,
                "exp_trades":   s_cnt,
                "daily_ev_pct": daily_ev_pct,
                "filters":      filters,
                "risks":        risks,
            }

        # ────────────────────────────────────────────────────
        # [보수안] — 현행 그대로
        # ────────────────────────────────────────────────────
        conservative = _make_scenario(
            label        = "보수안",
            wa           = 100,
            wb           = 0,
            ev_boost     = 0.0,
            wr_boost     = 0.0,
            trade_scale  = 1.0,
            filters      = ["현행 전략 A 유지", "필터 변경 없음"],
            risks        = ["현 EV 수준 지속 시 누적 손실 위험",
                            "돌파봉이탈 손절 패턴 반복 가능성"],
        )

        # ────────────────────────────────────────────────────
        # [시험안] — EV 개선 TOP1 적용
        # ────────────────────────────────────────────────────
        trial_filters  = []
        trial_risks    = []
        trial_ev_boost = 0.0
        trial_wr_boost = 0.0
        trial_scale    = 0.85    # 필터 적용 시 거래 약 15% 감소 추정
        trial_wa       = 90
        trial_wb       = 10 if b_available else 0

        top1 = best3_imp[0] if best3_imp else None
        top2 = best3_imp[1] if len(best3_imp) > 1 else None

        if 채택안:
            # 채택안이 있으면 채택안 필터를 시험안 기반으로
            top1_filter = 채택안[0]
            trial_filters.append(f"[채택안] {top1_filter['label']}")
            trial_ev_boost = top1_filter.get("delta_ev", 0.0) * 0.6  # 현실화 계수
            trial_wr_boost = top1_filter.get("delta_wr", 0.0) * 0.6
            trial_risks.extend([
                f"채택안 필터({top1_filter['label']}) 실 적용 첫날 — 예상과 다를 수 있음",
                "필터 효과는 과거 데이터 기반 — 전방향성 없음",
            ])
        elif top1 and top1.get("delta_ev", 0) >= _TRIAL_THRESHOLD_EV:
            # EV 개선 가능성 TOP1이 임계 이상 → 시험적용안 자동 승격
            g_info = _GROUP_TRIAL_MAP.get(top1["group"], {})
            trial_filters.append(
                g_info.get("short", top1["group"]) + " 실험"
            )
            # 개선폭의 60%를 현실적 기대치로 사용 (백테스트 과적합 할인)
            trial_ev_boost = top1["delta_ev"] * 0.6
            trial_wr_boost = 0.0   # 승률은 불확실 — 0으로 보수적 추정
            trial_risks.extend([
                f"미검증 실험 (백테스트 기반, 실전 미확인)",
                f"거래 건수 감소 가능 (해당 패턴 {top1['count']}건 제거)",
                g_info.get("direction", ""),
            ])
        else:
            # EV 개선 임계 미달 — 최선 후보 소극적 적용
            best_p = self._pick_best_pending(proposals)
            if best_p:
                trial_filters.append(
                    f"[보류→소극적 시험] {best_p['label']}"
                )
                trial_ev_boost = best_p.get("delta_ev", 0.0) * 0.4
            trial_risks.extend([
                "EV 개선 가능성 낮음 — 효과 미미할 수 있음",
                "데이터 추가 누적 후 재평가 권장",
            ])

        if b_available and trial_wb > 0:
            trial_filters.append(f"전략 B {trial_wb}% 배분 추가")
            trial_risks.append(f"B전략 데이터 {cnt_b}건 (소표본 불안정)")

        trial = _make_scenario(
            label        = "시험안",
            wa           = trial_wa,
            wb           = trial_wb,
            ev_boost     = trial_ev_boost,
            wr_boost     = trial_wr_boost,
            trade_scale  = trial_scale,
            filters      = trial_filters if trial_filters else ["변경 없음"],
            risks        = trial_risks   if trial_risks   else ["일반 시장 위험"],
        )

        # ────────────────────────────────────────────────────
        # [공격안] — TOP1 + TOP2 동시 적용
        # ────────────────────────────────────────────────────
        agg_filters  = list(trial_filters)   # 시험안 필터 포함
        agg_risks    = ["복합 필터 상호작용 불확실", "거래 기회 추가 감소"]
        agg_ev_boost = trial_ev_boost
        agg_wr_boost = trial_wr_boost
        agg_scale    = 0.72     # TOP1+TOP2 동시 적용 시 거래 추가 감소
        agg_wa       = max(70, trial_wa - 10)
        agg_wb       = min(30, trial_wb + 10) if b_available else trial_wb

        if 채택안 and len(채택안) >= 2:
            top2_filter = 채택안[1]
            agg_filters.append(f"[채택안 #2] {top2_filter['label']}")
            agg_ev_boost += top2_filter.get("delta_ev", 0.0) * 0.5
            agg_wr_boost += top2_filter.get("delta_wr", 0.0) * 0.5
        elif top2 and top2.get("delta_ev", 0) >= _TRIAL_THRESHOLD_EV * 0.5:
            # TOP2 개선 가능성 0.15%p 이상이면 공격안에 추가
            g2_info = _GROUP_TRIAL_MAP.get(top2["group"], {})
            agg_filters.append(
                g2_info.get("short", top2["group"]) + " 추가 실험"
            )
            agg_ev_boost += top2["delta_ev"] * 0.5
            agg_risks.append(
                f"2개 패턴 동시 제거 시 정상 거래도 차단될 수 있음"
            )
        elif top1 and top1.get("delta_ev", 0) >= _TRIAL_THRESHOLD_EV:
            # TOP2 없어도 시험안 필터 + B비중 확대로 공격안 구성
            if b_available:
                agg_wb  = min(30, agg_wb + 10)
                agg_wa  = max(70, 100 - agg_wb)
                agg_filters.append(f"전략 B 비중 {agg_wb}%로 확대")
                agg_risks.append("B 비중 확대에 따른 소표본 노출")
            else:
                agg_filters.append("약진입_손절 임계 추가 완화 실험")
                agg_ev_boost += 0.05
                agg_risks.append("약진입 임계 이중 완화 — 실전 검증 미완료")

        aggressive = _make_scenario(
            label        = "공격안",
            wa           = agg_wa,
            wb           = agg_wb,
            ev_boost     = agg_ev_boost,
            wr_boost     = agg_wr_boost,
            trade_scale  = agg_scale,
            filters      = agg_filters if agg_filters else ["시험안 필터 확장"],
            risks        = agg_risks,
        )

        # ────────────────────────────────────────────────────
        # 추천 안 선택 (기본 제시 순서: 시험안)
        # ────────────────────────────────────────────────────
        # 채택안 있거나 TOP1 임계 초과 → "시험안" 권장
        # 아니면 → "보수안" 권장
        has_trial_basis = bool(채택안) or (
            top1 is not None and top1.get("delta_ev", 0) >= _TRIAL_THRESHOLD_EV
        )
        recommended = "시험안" if has_trial_basis else "보수안"

        return {
            "conservative":   conservative,
            "trial":          trial,
            "aggressive":     aggressive,
            "recommended":    recommended,
            "has_trial_basis": has_trial_basis,
            "trial_top1":     top1,
            "trial_top2":     top2,
            "b_data_available": b_available,
            "b_count":        cnt_b,
        }

    # ────────────────────────────────────────────────────────────
    # 코드 힌트 생성 (실제 수정 대상 파라미터 제안)
    # ────────────────────────────────────────────────────────────

    def _generate_code_hint(self, filter_id: str, result: dict) -> str:
        """필터 ID로 실제 코드에서 수정해야 할 내용 제안."""
        hints = {
            "rsi_40_60":   "kr_strategy.py: _eval_entry()에 RSI 40~60 조건 추가\n"
                           "  → if not (40 <= iv['rsi'] <= 60): return SKIP",
            "rsi_35_65":   "kr_strategy.py: _eval_entry()에 RSI 35~65 조건 추가",
            "rsi_45_65":   "kr_strategy.py: _eval_entry()에 RSI 45~65 조건 추가",
            "rsi_nonzero": "kr_strategy.py: RSI=0 (미수집) 진입 차단\n"
                           "  → if iv['rsi'] == 0: return SKIP('RSI 미수집')",
            "bs_045":      "kr_strategy.py: BUY_SCORE_EARLY = 0.40 → 0.45 변경",
            "bs_050":      "kr_strategy.py: BUY_SCORE_EARLY = 0.40 → 0.50 변경",
            "bs_055":      "kr_strategy.py: BUY_SCORE_EARLY = 0.40 → 0.55 변경\n"
                           "  ※ EARLY 없이 Full 진입만 허용하는 효과",
            "vol_surge":   "kr_strategy.py: _eval_entry()에 vol_score >= 1 조건 강화\n"
                           "  (현재 vol_increase OR vol_surge → vol_surge 필수로 변경)",
            "vol_vwap":    "kr_strategy.py: 거래량증가 + VWAP 동시 충족 필수화",
            "bb_bonus":    "kr_strategy.py: breakout_bonus > 0 필수 조건 추가\n"
                           "  → if iv['breakout_bonus'] == 0: return SKIP",
            "bb_strong":   "kr_strategy.py: breakout_bonus >= 0.2 (강한돌파 이상만 허용)",
            "no_breakout": "kr_strategy.py: 비돌파(breakout_bonus=0) 진입 허용\n"
                           "  ※ 전략 B 전환 시 적용 권장",
            "hour_09":     "kr_strategy.py: BUY_STOP_TIME은 유지, 09시대 외 진입 차단\n"
                           "  → BUY_START_TIME = dtime(9, 0) 추가\n"
                           "  → BUY_END_EARLY = dtime(9, 59) 추가",
            "hour_09_10":  "kr_strategy.py: 10시대까지만 신규매수 허용\n"
                           "  → BUY_STOP_TIME = dtime(11, 0)으로 단축",
            "hour_09_11":  "kr_strategy.py: 11시대까지 허용 (현재 14:30 → 12:00으로 단축)",
            "hold_min_2":  "position_guard.py: WEAK_ENTRY 최소 보유 2분 이상 설정",
            "hold_min_5":  "position_guard.py: 청산 전 최소 5분 보유 조건 추가",
            "sig_breakout": "kr_strategy.py: signal_type 필터 — '돌파' 포함 신호만 허용",
            "sig_strong":  "kr_strategy.py: signal_type 필터 — '강한돌파'/'폭발돌파'만 허용",
            "combo_vol_bb": "kr_strategy.py: 거래량증가 AND 돌파보너스>0 동시 필수화\n"
                            "  → 두 조건 중 하나라도 미충족 시 SKIP",
            "combo_vol_vwap_bs050": "kr_strategy.py: 거래량+VWAP+BUY_SCORE_EARLY=0.50 동시 강화",
            "combo_09_vol_bb": "kr_strategy.py: 09시대 + 거래량 + 돌파보너스 복합 필터\n"
                               "  → 09시대 이후 진입 시 거래량/돌파 조건 더 엄격하게",
            "b_rsi_40_60": "_STRAT_B_RSI_MIN=40, _STRAT_B_RSI_MAX=60 유지 (현재값)",
            "b_rsi_35_65": "_STRAT_B_RSI_MIN=35, _STRAT_B_RSI_MAX=65 완화",
            "b_low_bs":    "전략 B: buy_score 0.36~0.45 구간 전용 진입 조건 추가",
            "b_vol_only":  "전략 B: 거래량 조건만 유지, RSI/BB 조건 완화",
        }
        return hints.get(filter_id, f"kr_strategy.py 또는 position_guard.py 내 {filter_id} 관련 파라미터 조정")

    # ────────────────────────────────────────────────────────────
    # 로그 출력
    # ────────────────────────────────────────────────────────────

    def _print_evolution_report(
        self,
        market: str,
        strategy_perf: dict,
        features: dict,
        baseline: dict,
        proposals: dict,
        ev_impact: dict,
        weight_sim: dict,
        capital_sim: dict,
        recommendation: dict,
        now_str: str,
    ) -> None:
        SEP  = "═" * 72
        sep2 = "─" * 72
        lines: list[str] = []

        lines.append(SEP)
        lines.append(f"  ⚡ [ADAPTIVE_EVOLUTION] {market}  {now_str[:16]}")
        lines.append(f"     기준선: {baseline['count']}건 | 승률={baseline['win_rate']:.1f}% | EV={baseline['ev']:+.3f}%")
        lines.append(SEP)

        # ── [1] 전략 A/B 성과 비교 ───────────────────────────
        lines.append("  [1] 전략 A/B 성과 비교")
        if strategy_perf:
            lines.append(
                f"      {'전략':<4} {'건수':>5} {'승률':>7} {'평균손익':>9} {'EV':>9} {'합계P&L':>10}"
            )
            lines.append(f"      {'─'*52}")
            for strat, p in strategy_perf.items():
                marker = "◀ 현행" if strat == "A" else "◀ 신규"
                lines.append(
                    f"      {strat:<4} {p['count']:>5}건 "
                    f"{p['win_rate']:>6.1f}% "
                    f"{p['avg_pct']:>+9.3f}% "
                    f"{p['ev']:>+9.3f}% "
                    f"{p['total_pct']:>+10.3f}%  {marker}"
                )
        else:
            lines.append("      데이터 없음")
        lines.append(sep2)

        # ── [2] 손실/수익 공통 특징 ───────────────────────────
        lines.append(
            f"  [2] 최근 {features.get('sample_size', 0)}건 특징 추출 "
            f"(수익 {features.get('win_count', 0)}건 / 손실 {features.get('loss_count', 0)}건)"
        )

        lines.append("      ◆ 손실 공통 특징:")
        loss_traits = features.get("loss_traits", [])
        if loss_traits:
            for trait in loss_traits:
                lines.append(f"        ⚠ {trait}")
        else:
            lines.append("        (패턴 미검출)")

        lines.append("      ◆ 수익 공통 특징:")
        win_traits = features.get("win_traits", [])
        if win_traits:
            for trait in win_traits:
                lines.append(f"        ✅ {trait}")
        else:
            lines.append("        (패턴 미검출)")

        # 수치 비교
        bs  = features.get("buy_score", {})
        rsi = features.get("rsi", {})
        hm  = features.get("hold_min", {})
        bb  = features.get("breakout_bonus", {})
        if bs:
            lines.append(
                f"      BUY_SCORE  수익={bs['win']['mean']:+.3f} "
                f"vs 손실={bs['loss']['mean']:+.3f}"
            )
        if rsi.get("win", {}).get("mean", 0) or rsi.get("loss", {}).get("mean", 0):
            lines.append(
                f"      RSI        수익={rsi['win']['mean']:+.1f} "
                f"vs 손실={rsi['loss']['mean']:+.1f}"
            )
        if hm:
            lines.append(
                f"      보유시간   수익={hm['win']['mean']:.1f}분 "
                f"vs 손실={hm['loss']['mean']:.1f}분"
            )
        if bb:
            lines.append(
                f"      돌파보너스 수익={bb['win']['mean']:.3f} "
                f"vs 손실={bb['loss']['mean']:.3f}"
            )

        # 시간대
        hour_dist = features.get("hour_dist", {})
        if hour_dist:
            lines.append("      시간대별 (KST):")
            for h in sorted(hour_dist.keys()):
                d = hour_dist[h]
                total = d["win"] + d["loss"]
                wr    = d["win"] / total * 100 if total else 0
                bar   = "█" * int(wr / 10)
                lines.append(
                    f"        {h:02d}시 W={d['win']:2d} L={d['loss']:2d} "
                    f"승률={wr:4.0f}% {bar}"
                )
        lines.append(sep2)

        # ── [3] 채택안 ────────────────────────────────────────
        채택안 = proposals["채택안"]
        lines.append(
            f"  [3] 채택안 ({len(채택안)}개)"
            f"  ← 개선폭≥+{_IMPROVEMENT_THRESHOLD_WR}%p(승률) OR +{_IMPROVEMENT_THRESHOLD_EV}%(EV)"
        )
        if 채택안:
            for i, p in enumerate(채택안, 1):
                lines.append(
                    f"\n  ━━ 채택안 #{i}: [{p['filter_id']}] {p['label']} ━━"
                )
                lines.append(
                    f"     성과: 승률={p['win_rate']:.1f}%({p['delta_wr']:+.1f}%p) | "
                    f"EV={p['ev']:+.3f}%({p['delta_ev']:+.3f}%p) | "
                    f"건수={p['count']}건 | 필터율={p['filter_rate']:.0f}%"
                )
                lines.append(f"     판정: {p['reason']}")
                lines.append(f"  [ADAPTIVE_PROPOSAL] 채택 — {p['label']}")
                lines.append(f"     코드 힌트:")
                for hint_line in p["code_hint"].split("\n"):
                    lines.append(f"       {hint_line}")
        else:
            # ① 채택안 없을 때 최선 후보 강제 출력
            best_p = self._pick_best_pending(proposals)
            lines.append("      (채택안 없음 — 아래 최선 후보 참조)")
            if best_p:
                lines.append(
                    f"\n  ── 최선 후보(보류): [{best_p['filter_id']}] {best_p['label']}"
                )
                lines.append(
                    f"     EV={best_p.get('ev', 0):+.3f}%"
                    f"({best_p.get('delta_ev', 0):+.3f}%p) | "
                    f"건수={best_p.get('count', 0)}건 | "
                    f"사유: {best_p.get('reason', '')}"
                )
                hint = best_p.get("code_hint", "")
                if hint:
                    lines.append(f"     코드 힌트:")
                    for hl in hint.split("\n"):
                        lines.append(f"       {hl}")
            else:
                lines.append("      최선 후보도 없음 — 데이터 추가 누적 필요")
        lines.append(sep2)

        # ── [4] 보류안 ────────────────────────────────────────
        lines.append(f"  [4] 보류안 ({len(proposals['보류안'])}개) — 추가 데이터 누적 후 재평가")
        if proposals["보류안"]:
            for p in sorted(proposals["보류안"], key=lambda x: x.get("delta_ev", 0), reverse=True)[:5]:
                if p.get("delta_ev") is not None:
                    lines.append(
                        f"    · [{p['filter_id']}] {p['label']:<28} "
                        f"EV={p.get('ev', 0):+.3f}%({p.get('delta_ev', 0):+.3f}%p) "
                        f"건수={p['count']} | {p['reason'][:50]}"
                    )
                else:
                    lines.append(
                        f"    · [{p['filter_id']}] {p['label']:<28} {p['reason'][:60]}"
                    )
        else:
            lines.append("      (보류안 없음)")
        lines.append(sep2)

        # ── [5] 폐기안 ────────────────────────────────────────
        lines.append(f"  [5] 폐기안 ({len(proposals['폐기안'])}개) — 성과 기여 없음")
        if proposals["폐기안"]:
            for p in sorted(proposals["폐기안"], key=lambda x: x.get("delta_ev", 0))[:5]:
                lines.append(
                    f"    · [{p['filter_id']}] {p['label']:<28} "
                    f"EV={p.get('ev', 0):+.3f}%({p.get('delta_ev', 0):+.3f}%p) | "
                    f"{p['reason'][:50]}"
                )
        else:
            lines.append("      (폐기안 없음)")
        lines.append(sep2)

        # ── [6] EV 최악 원인 TOP3 + 개선 가능성 TOP3 ─────────
        lines.append(
            f"  [6] EV 영향 분석 (최근 {ev_impact.get('total_count', 0)}건) "
            f"기준 EV={ev_impact.get('base_ev', 0):+.3f}%"
        )
        worst3 = ev_impact.get("worst_causes", [])
        lines.append("      ◆ EV 최악 원인 TOP3 (가장 많이 갉아먹는 패턴):")
        if worst3:
            for i, g in enumerate(worst3, 1):
                lines.append(
                    f"        #{i} [{g['group']}] "
                    f"{g['count']}건({g['ratio_pct']:.0f}%) "
                    f"평균={g['avg_pct']:+.3f}% "
                    f"EV기여={g['contribution']:+.4f}%"
                )
        else:
            lines.append("        (데이터 없음)")

        best3_imp = ev_impact.get("best_improvements", [])
        lines.append("      ◆ EV 개선 가능성 TOP3 (제거 시 EV 상승폭 최대):")
        if best3_imp:
            for i, g in enumerate(best3_imp, 1):
                lines.append(
                    f"        #{i} [{g['group']}] 제거 시 EV "
                    f"{g['ev_without']:+.3f}% "
                    f"(+{g['delta_ev']:+.3f}%p 개선)"
                )
        else:
            lines.append("        (데이터 없음)")
        lines.append(sep2)

        # ── [7] 전략 비중 변경 시뮬레이션 ───────────────────
        b_note = (
            ""
            if weight_sim.get("b_data_available")
            else " ※ B데이터 부족 — B=A 추정치 적용"
        )
        lines.append(f"  [7] 전략 비중 변경 시뮬{b_note}")
        lines.append(
            f"      현행 EV: A={weight_sim.get('ev_a', 0):+.3f}% "
            f"({weight_sim.get('cnt_a', 0)}건) | "
            f"B={weight_sim.get('ev_b', 0):+.3f}% "
            f"({weight_sim.get('cnt_b', 0)}건)"
        )
        lines.append(
            f"      {'비중':^14} {'예상EV':>8} {'Δ EV':>8} {'예상승률':>8} {'비고':}"
        )
        lines.append(f"      {'─'*60}")
        for mx in weight_sim.get("mixes", []):
            current_mark = " ◀ 현행" if mx.get("is_current") else ""
            best_mark    = " ★ 최적" if mx["label"] == weight_sim.get("best_mix", {}).get("label") else ""
            lines.append(
                f"      {mx['label']:<14} "
                f"{mx['sim_ev']:>+8.3f}% "
                f"{mx['delta_ev']:>+8.3f}%p "
                f"{mx['sim_wr']:>7.1f}%"
                f"{current_mark}{best_mark}"
            )
        lines.append(sep2)

        # ── [8] 자본배분 시뮬레이션 (최근 50거래) ────────────
        est_note = " ※ B거래 없음→A추정" if capital_sim.get("b_estimated") else ""
        lines.append(
            f"  [8] 자본배분 시뮬레이션 (최근 {capital_sim.get('trade_count', 0)}거래)"
            f"{est_note}"
        )
        lines.append(
            f"      A {capital_sim.get('a_count', 0)}건 | B {capital_sim.get('b_count', 0)}건"
        )
        lines.append(
            f"      {'시나리오':^14} {'누적수익':>9} {'MDD':>7} {'승률':>7} {'EV':>8} {'최종자본':>9}"
        )
        lines.append(f"      {'─'*60}")
        best_label = capital_sim.get("best_scenario", "")
        for sc in capital_sim.get("scenarios", []):
            r    = sc.get("result", {})
            mark = " ★" if sc["label"] == best_label else "  "
            lines.append(
                f"      {sc['label']:<14}{mark}"
                f"  {r.get('cumulative_pct', 0):>+8.2f}% "
                f"-{r.get('mdd', 0):>6.2f}% "
                f"{r.get('win_rate', 0):>6.1f}% "
                f"{r.get('ev', 0):>+7.3f}% "
                f"{r.get('final_equity', 1.0):>8.4f}"
            )
        lines.append(sep2)

        # ── [9] 내일 추천 운영안 — 3단계 ──────────────────────
        rec = recommendation
        SEP9 = "═" * 60

        lines.append(f"\n  {'★'*4} [내일 추천 운영안] {'★'*4}")
        lines.append(f"  {SEP9}")

        # 권장 안 표시
        recommended_label = rec.get("recommended", "보수안")
        lines.append(
            f"  ▶ 권장: [{recommended_label}] "
            + ("← EV 개선 실험 근거 있음" if rec.get("has_trial_basis") else "← 데이터 부족, 현행 유지")
        )
        if rec.get("trial_top1"):
            t1 = rec["trial_top1"]
            lines.append(
                f"  ▶ 시험 근거: [{t1['group']}] 제거 시 EV "
                f"{t1['ev_without']:+.3f}% (+{t1['delta_ev']:+.3f}%p)"
            )
        lines.append(f"  {SEP9}")

        def _print_scenario(tag: str, sc: dict, is_recommended: bool) -> None:
            mark  = " ◀ 권장" if is_recommended else ""
            wa    = sc.get("weight_a", 100)
            wb    = sc.get("weight_b", 0)
            ev    = sc.get("exp_ev",     0.0)
            wr    = sc.get("exp_wr",     0.0)
            cnt   = sc.get("exp_trades", 0.0)
            daily = sc.get("daily_ev_pct", 0.0)
            fts   = sc.get("filters",  [])
            risks = sc.get("risks",    [])

            lines.append(f"\n  ┌── [{tag}]{mark}")
            lines.append(f"  │  전략 A 비중:  {wa:>3}%   전략 B 비중: {wb:>3}%")
            lines.append(f"  │  예상 거래수:  일 ~{cnt:.1f}건")
            lines.append(f"  │  예상 승률:    {wr:.1f}%")
            lines.append(f"  │  예상 EV:      {ev:+.3f}%/거래")
            # 예상 일손익: EV × 거래수 (상대 스케일 — 부호/크기만 참조)
            daily_sign = "+" if daily >= 0 else ""
            lines.append(
                f"  │  예상 일손익:  {daily_sign}{daily:.3f}% (EV×거래수, 참고용)"
            )
            if fts:
                lines.append(f"  │  필터 적용:")
                for f in fts:
                    if f:
                        lines.append(f"  │    · {f}")
            if risks:
                lines.append(f"  │  위험요소:")
                for r in risks:
                    if r:
                        lines.append(f"  │    ⚠ {r}")
            lines.append(f"  └{'─'*56}")

        _print_scenario("보수안", rec["conservative"], recommended_label == "보수안")
        _print_scenario("시험안", rec["trial"],        recommended_label == "시험안")
        _print_scenario("공격안", rec["aggressive"],   recommended_label == "공격안")

        # 시험안 코드 힌트 (TOP1 그룹 매핑)
        t1 = rec.get("trial_top1")
        if t1 and rec.get("has_trial_basis"):
            g_info = _GROUP_TRIAL_MAP.get(t1.get("group", ""), {})
            hint   = g_info.get("hint", "")
            if hint:
                lines.append(f"\n  [시험안 코드 힌트 — {t1['group']}]")
                for hl in hint.split("\n"):
                    lines.append(f"    {hl}")

        if not rec.get("b_data_available"):
            lines.append(
                f"\n  ℹ B전략 데이터 {rec.get('b_count', 0)}건 — "
                f"10건 이상 누적 시 비중 자동 재산정"
            )
        lines.append(f"  {SEP9}\n")

        lines.append(SEP)

        for line in lines:
            logger.info(line)

    # ────────────────────────────────────────────────────────────
    # 공격안 즉시 활성화 (TrialPlanManager 연동)
    # ────────────────────────────────────────────────────────────

    def apply_aggressive_plan(
        self,
        max_trades:   int   = 7,
        max_loss_krw: int   = -10_000,
        qty_scale:    float = 0.30,
    ) -> dict:
        """
        가장 최근 run() 결과의 공격안을 TrialPlanManager로 즉시 활성화.

        Args:
            max_trades:   적용 최대 거래수 (기본 7)
            max_loss_krw: 누적 손실 한도 (기본 -10,000원)
            qty_scale:    진입 수량 배율 (기본 0.30 = 30%)

        Returns:
            활성화된 active_plan dict

        사용법:
            engine = StrategyEvolutionEngine()
            engine.run('KR')                    # 분석 후
            engine.apply_aggressive_plan()      # 공격안 자동 활성화
        """
        from adaptive.trial_manager import get_trial_manager

        # 최신 evolution_report 에서 공격안 파라미터 조회
        aggressive_params = {
            "qty_scale":            qty_scale,
            "breakout_tolerance":   0.003,   # 돌파봉저가 0.3% 허용폭
            "weak_entry_cut_pct":  -1.0,     # WEAK_ENTRY 기준 -0.7% → -1.0% 완화
            "weak_entry_max_min":   7.0,     # WEAK_ENTRY 적용 시간 5분 → 7분 완화
        }

        plan_dict = {
            "plan_name":    "공격안",
            "source":       "AUTO_APPLIED",
            "max_trades":   max_trades,
            "max_loss_krw": max_loss_krw,
            "params":       aggressive_params,
        }

        tm = get_trial_manager()
        result = tm.activate(plan_dict)

        logger.info(
            f"[ACTIVE_PLAN] 공격안 AUTO_APPLIED 완료 | "
            f"qty_scale={qty_scale:.0%} | "
            f"적용기간={max_trades}거래 | "
            f"손실한도={max_loss_krw:,}원"
        )
        return result

    # ────────────────────────────────────────────────────────────
    # DB 조회
    # ────────────────────────────────────────────────────────────

    def _fetch_closed(self, market: str, limit: int) -> list[dict]:
        try:
            conn = self._conn()
            rows = conn.execute("""
                SELECT code, name, strategy, entry_time, exit_time,
                       exit_pct, exit_reason, hold_min, pnl_krw,
                       buy_score, sell_score, vol_score, vwap_state,
                       rsi, breakout_bonus, strength, signal_type,
                       max_pct, min_pct
                FROM trades
                WHERE status = 'closed'
                  AND market = ?
                  AND exit_pct IS NOT NULL
                ORDER BY exit_time DESC
                LIMIT ?
            """, (market, limit)).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[EVOLUTION] DB 조회 실패: {e}")
            return []

    # ────────────────────────────────────────────────────────────
    # 파일 저장
    # ────────────────────────────────────────────────────────────

    def _save_proposals(self, result: dict) -> None:
        try:
            # ── evolution_proposals.json 누적 저장 ──────────
            history: dict = {}
            if os.path.exists(_PROPOSALS_FILE):
                with open(_PROPOSALS_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)

            key = f"{result['generated_at'][:10]}_{result['market']}"
            history[key] = result

            # 최근 90일치만 보존
            if len(history) > 200:
                oldest = sorted(history.keys())[:len(history) - 180]
                for k in oldest:
                    del history[k]

            with open(_PROPOSALS_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)

            # ── evolution_report.json 최신 보고서 저장 ──────
            with open(_EVOLUTION_REPORT, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            logger.info(
                f"[EVOLUTION] 저장 완료 → "
                f"evolution_proposals.json({len(history)}개) + "
                f"evolution_report.json"
            )
        except Exception as e:
            logger.warning(f"[EVOLUTION] 저장 실패: {e}")
