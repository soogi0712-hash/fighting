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

설계 원칙:
  - 실제 코드 수정 절대 안 함 (제안만)
  - PROPOSAL은 data/evolution_proposals.json 에 누적 저장
  - 최소 30건 이상 있어야 실행 (통계 신뢰성)
  - 각 필터는 독립 백테스트 → 복합 필터도 평가

변경 이력:
  2026-06-23: 초기 작성
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
_IMPROVEMENT_THRESHOLD_WR  = 10.0   # 승률 개선 +10%p 이상
_IMPROVEMENT_THRESHOLD_EV  = 0.10   # EV 개선 +0.10% 이상
_MIN_TRADES_FOR_ANALYSIS   = 30     # 최소 거래 건수

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

        # ── [7] 로그 출력 ────────────────────────────────────
        self._print_evolution_report(
            market_up, strategy_perf, features, baseline, proposals, now_str
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
        lines.append(
            f"  [3] 채택안 ({len(proposals['채택안'])}개)"
            f"  ← 개선폭≥+{_IMPROVEMENT_THRESHOLD_WR}%p(승률) OR +{_IMPROVEMENT_THRESHOLD_EV}%(EV)"
        )
        if proposals["채택안"]:
            for i, p in enumerate(proposals["채택안"], 1):
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
            lines.append("      (채택안 없음)")
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

        lines.append(SEP)

        for line in lines:
            logger.info(line)

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
