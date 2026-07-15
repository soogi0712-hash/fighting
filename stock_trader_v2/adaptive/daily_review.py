"""
adaptive/daily_review.py — DAILY_REVIEW 자동 복기 시스템
=========================================================
역할:
  매일 KR 장 종료(15:30) 후 자동 집계 → [DAILY_REVIEW] 로그 출력 +
  data/daily_review_history.json 누적 저장.

집계 항목:
  1.  총 거래수 / 승률 / 평균수익 / 평균손실
  2.  평균 보유시간 / 최장·최단 보유 종목
  3.  BUY_SCORE 분포 (0.40대 / 0.50대 / 0.60+)
  4.  손절사유 통계 (돌파봉이탈 / WEAK_ENTRY / TIME_EXIT /
                     돌파실패 / 에어백 / 기타)
  5.  TIME_EXIT 건수 / 절약 P&L 추정
  6.  PROFIT_PROTECT 건수 / 평균 보호 수익
  7.  WEAK_ENTRY_EXIT 건수
  8.  max_pct 상위 5 거래 (수익 회수 기회 분석용)
  9.  손실 상위 5 거래
  10. 신규 기능 발동 요약 (금일 첫 발동 여부 하이라이트)

설계 원칙:
  - 학습 가중치 자동 변경 없음 (복기 데이터 누적만)
  - 최근 100거래 이상 누적 후 Adaptive 학습 기능 별도 검토 예정
  - 기존 DailyReporter 와 독립 실행 (중복 없음)
  - DB 직접 조회 (trade_history.db)

변경 이력:
  2026-06-18: 초기 작성
"""

from __future__ import annotations

import os
import json
import re
import sqlite3
from datetime import date, datetime
from typing import Any

import pytz

from utils.v2_logger import get_logger
from adaptive.strategy_evolution import StrategyEvolutionEngine

logger  = get_logger("DailyReview")
KST     = pytz.timezone("Asia/Seoul")

_DATA_DIR    = os.path.join(os.path.dirname(__file__), "..", "data")
_HISTORY_FILE = os.path.join(_DATA_DIR, "daily_review_history.json")
_DB_PATH      = os.path.join(_DATA_DIR, "trade_history.db")

# ── 집계에 쓸 exit_reason 패턴 ───────────────────────────────────
_REASON_PATTERNS = {
    "WEAK_ENTRY_EXIT":   r"WEAK_ENTRY_EXIT",
    "TIME_EXIT":         r"TIME_EXIT",
    "PROFIT_PROTECT":    r"PROFIT_PROTECT",
    "돌파봉이탈":        r"돌파봉저가이탈",
    "돌파실패":          r"돌파실패",
    "에어백손절":        r"에어백손절",
    "15:20강제청산":     r"15:20 강제청산",
    "오버나이트방지":    r"오버나이트방지",
    "전량익절":          r"전량익절",
    "익절+SELL신호":     r"익절\+SELL신호|익절.*SELL",
    "기타":              None,          # 위 패턴에 안 걸리면
}

# ── BUY_SCORE 구간 정의 ─────────────────────────────────────────
_BS_BUCKETS = [
    (0.40, 0.50, "0.40~0.49"),
    (0.50, 0.55, "0.50~0.54"),
    (0.55, 0.60, "0.55~0.59"),
    (0.60, 0.70, "0.60~0.69"),
    (0.70, 9.99, "0.70+"),
]


# ════════════════════════════════════════════════════════════════
class DailyReviewEngine:
    """
    하루치 거래를 집계하여 DAILY_REVIEW 보고서를 생성한다.

    사용법:
        engine = DailyReviewEngine()
        report = engine.run("KR")          # → dict 반환 + 로그 출력 + 파일 저장
    """

    # ── 내부 DB 접속 ─────────────────────────────────────────────

    @staticmethod
    def _conn() -> sqlite3.Connection:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 메인 실행 ────────────────────────────────────────────────

    def run(self, market: str = "KR",
            target_date: str | None = None,
            run_evolution: bool = True) -> dict:
        """
        market: "KR" 또는 "US"
        target_date: "YYYY-MM-DD" (기본 = 오늘)
        run_evolution: True이면 DAILY_REVIEW 후 전략 진화 엔진 자동 실행
        Returns: 보고서 dict
        """
        market_up   = market.upper()
        today       = target_date or date.today().isoformat()
        generated   = datetime.now(KST).isoformat()

        # ── 거래 조회 ─────────────────────────────────────────
        trades = self._fetch_trades(market_up, today)

        if not trades:
            logger.info(
                f"[DAILY_REVIEW] {market_up} {today} — 종료 거래 없음 (건너뜀)"
            )
            # 거래 없어도 진화 엔진은 누적 DB 기준으로 실행
            if run_evolution:
                self._run_evolution(market_up)
            return {"date": today, "market": market_up,
                    "generated_at": generated, "trade_count": 0}

        # ── 집계 ─────────────────────────────────────────────
        report = self._build_report(market_up, today, generated, trades)

        # ── 로그 출력 ─────────────────────────────────────────
        self._print_report(report)

        # ── 누적 저장 ─────────────────────────────────────────
        self._save_history(report)

        # ── 전략 진화 엔진 실행 ──────────────────────────────
        if run_evolution:
            evolution_result = self._run_evolution(market_up)
            report["evolution"] = evolution_result

        return report

    # ── 거래 조회 ────────────────────────────────────────────────

    def _fetch_trades(self, market: str, today: str) -> list[dict]:
        try:
            conn  = self._conn()
            rows  = conn.execute("""
                SELECT code, name, entry_price, exit_price,
                       exit_reason, exit_pct, hold_min,
                       pnl_krw, buy_score, signal_type,
                       max_pct, min_pct, entry_time, exit_time,
                       exit_category, outcome,
                       strategy, rsi, vol_score, vwap_state,
                       breakout_bonus
                FROM trades
                WHERE status='closed'
                  AND market=?
                  AND date(exit_time)=?
                ORDER BY exit_time ASC
            """, (market, today)).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[DAILY_REVIEW] DB 조회 실패: {e}")
            return []

    # ── 보고서 빌드 ──────────────────────────────────────────────

    def _build_report(self, market: str, today: str,
                      generated: str, trades: list[dict]) -> dict:

        N = len(trades)
        wins   = [t for t in trades if (t["exit_pct"] or 0) > 0]
        losses = [t for t in trades if (t["exit_pct"] or 0) <= 0]

        win_pcts  = [(t["exit_pct"] or 0) for t in wins]
        loss_pcts = [(t["exit_pct"] or 0) for t in losses]
        all_pcts  = [(t["exit_pct"] or 0) for t in trades]

        # ── [1] 기본 통계 ────────────────────────────────────
        win_rate    = len(wins) / N * 100
        avg_win     = sum(win_pcts)  / len(win_pcts)  if win_pcts  else 0.0
        avg_loss    = sum(loss_pcts) / len(loss_pcts) if loss_pcts else 0.0
        avg_pct     = sum(all_pcts)  / N
        total_pnl   = sum((t["pnl_krw"] or 0) for t in trades)
        hold_mins   = [(t["hold_min"] or 0) for t in trades]
        avg_hold    = sum(hold_mins) / N

        # 손익비
        pf = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0
        # EV
        ev = (win_rate / 100) * avg_win + (1 - win_rate / 100) * avg_loss

        # ── [2] 보유시간 극단치 ──────────────────────────────
        max_hold_t = max(trades, key=lambda t: t["hold_min"] or 0, default=None)
        min_hold_t = min(trades, key=lambda t: t["hold_min"] or 0, default=None)

        # ── [3] BUY_SCORE 분포 ───────────────────────────────
        bs_dist: dict[str, dict] = {}
        for lo, hi, label in _BS_BUCKETS:
            bucket = [t for t in trades
                      if lo <= (t["buy_score"] or 0) < hi]
            if bucket:
                b_pcts = [(t["exit_pct"] or 0) for t in bucket]
                b_wins = sum(1 for x in b_pcts if x > 0)
                bs_dist[label] = {
                    "count":    len(bucket),
                    "win_rate": b_wins / len(bucket) * 100,
                    "avg_pct":  sum(b_pcts) / len(b_pcts),
                }

        # ── [4] 손절사유 통계 ────────────────────────────────
        reason_stats: dict[str, dict] = {}
        for label, pattern in _REASON_PATTERNS.items():
            if pattern is None:
                continue
            matched = [t for t in trades
                       if re.search(pattern, t.get("exit_reason") or "")]
            if matched:
                m_pcts = [(t["exit_pct"] or 0) for t in matched]
                reason_stats[label] = {
                    "count":   len(matched),
                    "avg_pct": sum(m_pcts) / len(m_pcts),
                    "total":   sum(m_pcts),
                }

        # 기타 (패턴 미매칭)
        matched_all = set()
        for label, pattern in _REASON_PATTERNS.items():
            if pattern:
                for t in trades:
                    if re.search(pattern, t.get("exit_reason") or ""):
                        matched_all.add(id(t))
        others = [t for t in trades if id(t) not in matched_all]
        if others:
            o_pcts = [(t["exit_pct"] or 0) for t in others]
            reason_stats["기타"] = {
                "count":   len(others),
                "avg_pct": sum(o_pcts) / len(o_pcts),
                "total":   sum(o_pcts),
            }

        # ── [5] TIME_EXIT 건수 / 절약 P&L ───────────────────
        time_exits = [t for t in trades
                      if re.search(r"TIME_EXIT", t.get("exit_reason") or "")]
        # 절약 P&L: 손절로 끝났지만 TIME_EXIT로 0%에서 청산됐을 거래의 원손실 합계
        # → 단, 현 DB에선 TIME_EXIT 발동 거래가 0%로 찍힌 게 아니라
        #   실제로 청산된 결과. 그냥 avg_pct 집계로 충분
        time_exit_pcts = [(t["exit_pct"] or 0) for t in time_exits]
        time_exit_summary = {
            "count":       len(time_exits),
            "avg_pct":     sum(time_exit_pcts) / len(time_exit_pcts) if time_exit_pcts else 0,
            "total_pct":   sum(time_exit_pcts),
            "codes":       [t["code"] for t in time_exits],
        }

        # ── [6] PROFIT_PROTECT 건수 ──────────────────────────
        pp_exits = [t for t in trades
                    if re.search(r"PROFIT_PROTECT", t.get("exit_reason") or "")]
        pp_pcts = [(t["exit_pct"] or 0) for t in pp_exits]
        pp_summary = {
            "count":     len(pp_exits),
            "avg_pct":   sum(pp_pcts) / len(pp_pcts) if pp_pcts else 0,
            "total_pct": sum(pp_pcts),
            "codes":     [t["code"] for t in pp_exits],
        }

        # ── [7] WEAK_ENTRY_EXIT 건수 ─────────────────────────
        weak_exits = [t for t in trades
                      if re.search(r"WEAK_ENTRY_EXIT", t.get("exit_reason") or "")]
        weak_pcts = [(t["exit_pct"] or 0) for t in weak_exits]
        weak_summary = {
            "count":     len(weak_exits),
            "avg_pct":   sum(weak_pcts) / len(weak_pcts) if weak_pcts else 0,
            "total_pct": sum(weak_pcts),
            "codes":     [t["code"] for t in weak_exits],
        }

        # ── [8] max_pct 상위 5건 ─────────────────────────────
        has_maxpct = [t for t in trades if t.get("max_pct") is not None]
        top_maxpct = sorted(has_maxpct,
                            key=lambda t: t["max_pct"] or 0,
                            reverse=True)[:5]
        top_maxpct_list = [
            {
                "code":      t["code"],
                "name":      t["name"],
                "max_pct":   t["max_pct"],
                "exit_pct":  t["exit_pct"],
                "hold_min":  round(t["hold_min"] or 0, 1),
                "exit_reason": (t["exit_reason"] or "")[:50],
            }
            for t in top_maxpct
        ]

        # ── [9] 손실 상위 5건 ────────────────────────────────
        top_losses = sorted(
            [t for t in trades if (t["exit_pct"] or 0) < 0],
            key=lambda t: t["exit_pct"] or 0
        )[:5]
        top_losses_list = [
            {
                "code":      t["code"],
                "name":      t["name"],
                "exit_pct":  t["exit_pct"],
                "hold_min":  round(t["hold_min"] or 0, 1),
                "exit_reason": (t["exit_reason"] or "")[:60],
            }
            for t in top_losses
        ]

        # ── [10] 누적 통계 (최근 100건 기준) ────────────────
        cumulative = self._calc_cumulative(market)

        # ── [11] 전략 A/B 일간 성과 집계 ────────────────────
        strategy_daily = self._calc_strategy_daily(trades)

        # ── 조립 ─────────────────────────────────────────────
        return {
            "date":         today,
            "market":       market,
            "generated_at": generated,

            # 기본 통계
            "trade_count":  N,
            "win_count":    len(wins),
            "loss_count":   len(losses),
            "win_rate":     round(win_rate, 1),
            "avg_win_pct":  round(avg_win,  3),
            "avg_loss_pct": round(avg_loss, 3),
            "avg_pct":      round(avg_pct,  3),
            "total_pnl_krw": total_pnl,
            "profit_factor": round(pf, 2),
            "ev":           round(ev, 3),
            "avg_hold_min": round(avg_hold, 1),

            # 보유시간 극단
            "longest_hold":  {
                "code": max_hold_t["code"] if max_hold_t else "",
                "name": max_hold_t["name"] if max_hold_t else "",
                "hold_min": round(max_hold_t["hold_min"] or 0, 1) if max_hold_t else 0,
                "exit_pct": max_hold_t["exit_pct"] if max_hold_t else 0,
            },
            "shortest_hold": {
                "code": min_hold_t["code"] if min_hold_t else "",
                "name": min_hold_t["name"] if min_hold_t else "",
                "hold_min": round(min_hold_t["hold_min"] or 0, 1) if min_hold_t else 0,
                "exit_pct": min_hold_t["exit_pct"] if min_hold_t else 0,
            },

            # BUY_SCORE 분포
            "buy_score_dist": bs_dist,

            # 손절사유 통계
            "reason_stats": reason_stats,

            # 신규 기능
            "time_exit":       time_exit_summary,
            "profit_protect":  pp_summary,
            "weak_entry_exit": weak_summary,

            # 상위 거래
            "top_maxpct":  top_maxpct_list,
            "top_losses":  top_losses_list,

            # 누적
            "cumulative":  cumulative,

            # 전략 A/B 일간 성과
            "strategy_daily": strategy_daily,
        }

    # ── 누적 통계 ────────────────────────────────────────────────

    def _calc_cumulative(self, market: str,
                         window: int = 100) -> dict:
        """최근 window 건 기준 누적 통계."""
        try:
            conn = self._conn()
            rows = conn.execute("""
                SELECT exit_pct, hold_min, buy_score,
                       exit_reason, max_pct, pnl_krw
                FROM trades
                WHERE status='closed' AND market=?
                ORDER BY exit_time DESC
                LIMIT ?
            """, (market, window)).fetchall()
            conn.close()
            trades = [dict(r) for r in rows]
        except Exception:
            return {}

        if not trades:
            return {}

        n = len(trades)
        pcts  = [(t["exit_pct"] or 0) for t in trades]
        wins  = [x for x in pcts if x > 0]
        losses= [x for x in pcts if x <= 0]
        total_pnl = sum((t["pnl_krw"] or 0) for t in trades)

        # TIME_EXIT / PROFIT_PROTECT / WEAK_ENTRY_EXIT 누적 건수
        te_cnt  = sum(1 for t in trades
                      if re.search(r"TIME_EXIT",       t.get("exit_reason") or ""))
        pp_cnt  = sum(1 for t in trades
                      if re.search(r"PROFIT_PROTECT",  t.get("exit_reason") or ""))
        we_cnt  = sum(1 for t in trades
                      if re.search(r"WEAK_ENTRY_EXIT", t.get("exit_reason") or ""))

        return {
            "window":       n,
            "win_rate":     round(len(wins)  / n * 100, 1),
            "avg_pct":      round(sum(pcts) / n, 3),
            "avg_win":      round(sum(wins)  / len(wins)   if wins   else 0, 3),
            "avg_loss":     round(sum(losses)/ len(losses) if losses else 0, 3),
            "total_pnl_krw": total_pnl,
            "time_exit_cnt":        te_cnt,
            "profit_protect_cnt":   pp_cnt,
            "weak_entry_exit_cnt":  we_cnt,
            "ready_for_adaptive":   n >= 100,   # 학습 준비 완료 플래그
        }

    # ── 로그 출력 ────────────────────────────────────────────────

    def _print_report(self, r: dict) -> None:
        SEP  = "═" * 68
        sep2 = "─" * 68
        lines: list[str] = []

        lines.append(SEP)
        lines.append(
            f"  ★ [DAILY_REVIEW] {r['market']}  {r['date']}  "
            f"({r['trade_count']}건)"
        )
        lines.append(SEP)

        # ── 1. 기본 통계 ─────────────────────────────────────
        lines.append("  [1] 거래 요약")
        lines.append(
            f"      거래수  : {r['trade_count']}건 "
            f"(익절 {r['win_count']} / 손절 {r['loss_count']})"
        )
        lines.append(
            f"      승 률   : {r['win_rate']:.1f}%  |  "
            f"EV = {r['ev']:+.3f}%"
        )
        lines.append(
            f"      평균수익: {r['avg_win_pct']:+.3f}%  |  "
            f"평균손실: {r['avg_loss_pct']:+.3f}%"
        )
        lines.append(
            f"      손익비  : {r['profit_factor']:.2f}  |  "
            f"합계P&L: {r['avg_pct'] * r['trade_count']:+.2f}%  "
            f"({r['total_pnl_krw']:+,.0f}원)"
        )
        lines.append(
            f"      평균보유: {r['avg_hold_min']:.1f}분  |  "
            f"최장: {r['longest_hold']['code']}({r['longest_hold']['hold_min']:.0f}분 "
            f"{r['longest_hold']['exit_pct']:+.2f}%)  |  "
            f"최단: {r['shortest_hold']['code']}({r['shortest_hold']['hold_min']:.0f}분 "
            f"{r['shortest_hold']['exit_pct']:+.2f}%)"
        )
        lines.append(sep2)

        # ── 2. BUY_SCORE 분포 ────────────────────────────────
        lines.append("  [2] BUY_SCORE 분포")
        if r.get("buy_score_dist"):
            for label, s in r["buy_score_dist"].items():
                bar = "█" * int(s["win_rate"] / 10)
                lines.append(
                    f"      {label:<10} n={s['count']:2d}  "
                    f"승률={s['win_rate']:5.1f}%  {bar:<10}  "
                    f"avg={s['avg_pct']:+.3f}%"
                )
        else:
            lines.append("      데이터 없음")
        lines.append(sep2)

        # ── 3. 손절사유 통계 ─────────────────────────────────
        lines.append("  [3] 청산 사유 통계")
        if r.get("reason_stats"):
            for label, s in sorted(r["reason_stats"].items(),
                                   key=lambda x: -x[1]["count"]):
                lines.append(
                    f"      {label:<16} "
                    f"{s['count']:2d}건  "
                    f"avg={s['avg_pct']:+.3f}%  "
                    f"합계={s['total']:+.2f}%"
                )
        else:
            lines.append("      데이터 없음")
        lines.append(sep2)

        # ── 4. 신규 기능 발동 요약 ───────────────────────────
        lines.append("  [4] 신규 기능 발동 요약 (2026-06-18 추가)")
        te = r.get("time_exit", {})
        pp = r.get("profit_protect", {})
        we = r.get("weak_entry_exit", {})

        lines.append(
            f"      [WEAK_ENTRY_EXIT]  {we.get('count', 0):2d}건  "
            f"avg={we.get('avg_pct', 0):+.3f}%  "
            f"합계={we.get('total_pct', 0):+.2f}%"
        )
        if we.get("codes"):
            lines.append(f"        → {', '.join(we['codes'][:10])}")

        lines.append(
            f"      [TIME_EXIT]        {te.get('count', 0):2d}건  "
            f"avg={te.get('avg_pct', 0):+.3f}%  "
            f"합계={te.get('total_pct', 0):+.2f}%"
        )
        if te.get("codes"):
            lines.append(f"        → {', '.join(te['codes'][:10])}")

        lines.append(
            f"      [PROFIT_PROTECT]   {pp.get('count', 0):2d}건  "
            f"avg={pp.get('avg_pct', 0):+.3f}%  "
            f"합계={pp.get('total_pct', 0):+.2f}%"
        )
        if pp.get("codes"):
            lines.append(f"        → {', '.join(pp['codes'][:10])}")
        lines.append(sep2)

        # ── 5. max_pct 상위 거래 ─────────────────────────────
        lines.append("  [5] max_pct 상위 거래 (수익 회수 기회)")
        if r.get("top_maxpct"):
            for i, t in enumerate(r["top_maxpct"], 1):
                lines.append(
                    f"      {i}. {t['code']} {(t['name'] or '')[:6]:<6}  "
                    f"max={t['max_pct']:+.2f}%  "
                    f"exit={t['exit_pct']:+.2f}%  "
                    f"반납={t['max_pct'] - t['exit_pct']:+.2f}%  "
                    f"hold={t['hold_min']:.0f}분"
                )
        else:
            lines.append("      max_pct 기록 없음 (오늘부터 수집 시작)")
        lines.append(sep2)

        # ── 6. 손실 상위 거래 ────────────────────────────────
        lines.append("  [6] 손실 상위 거래")
        if r.get("top_losses"):
            for i, t in enumerate(r["top_losses"], 1):
                lines.append(
                    f"      {i}. {t['code']} {(t['name'] or '')[:6]:<6}  "
                    f"{t['exit_pct']:+.3f}%  "
                    f"hold={t['hold_min']:.0f}분  "
                    f"사유={t['exit_reason'][:40]}"
                )
        else:
            lines.append("      손실 거래 없음")
        lines.append(sep2)

        # ── 7. 누적 통계 ─────────────────────────────────────
        cum = r.get("cumulative", {})
        if cum:
            lines.append(
                f"  [7] 누적 ({cum.get('window', 0)}건) "
                f"| 준비: {'✅ Adaptive 학습 가능' if cum.get('ready_for_adaptive') else f'⏳ {100 - cum.get(chr(119), 0)}건 더 필요'}"
            )
            lines.append(
                f"      승률={cum.get('win_rate', 0):.1f}%  "
                f"avg={cum.get('avg_pct', 0):+.3f}%  "
                f"손익={cum.get('avg_win', 0):+.3f}%/{cum.get('avg_loss', 0):+.3f}%"
            )
            lines.append(
                f"      누적 WEAK_ENTRY={cum.get('weak_entry_exit_cnt', 0)}건  "
                f"TIME_EXIT={cum.get('time_exit_cnt', 0)}건  "
                f"PROFIT_PROTECT={cum.get('profit_protect_cnt', 0)}건"
            )

        # ── 8. 전략 A/B 일간 성과 ───────────────────────────
        sd = r.get("strategy_daily", {})
        if sd:
            lines.append(sep2)
            lines.append("  [8] 전략 A/B 일간 성과")
            lines.append(
                f"      {'전략':<4} {'건수':>5} {'승률':>7} {'평균손익':>9} {'합계P&L':>10}"
            )
            lines.append(f"      {'─'*45}")
            for strat in ("A", "B"):
                if strat not in sd:
                    continue
                p = sd[strat]
                marker = "(모멘텀돌파)" if strat == "A" else "(BB회귀)"
                lines.append(
                    f"      {strat:<4} {p['count']:>5}건 "
                    f"{p['win_rate']:>6.1f}% "
                    f"{p['avg_pct']:>+9.3f}% "
                    f"{p['total_pct']:>+10.3f}%  {marker}"
                )

        lines.append(SEP)
        lines.append(
            "  ⚡ DAILY_REVIEW 완료 → 전략 진화 엔진 실행 중 "
            "(data/evolution_report.json 저장)"
        )
        lines.append(SEP)

        # ── 출력 ─────────────────────────────────────────────
        for line in lines:
            logger.info(line)

    # ── 전략 A/B 일간 성과 집계 ─────────────────────────────────

    @staticmethod
    def _calc_strategy_daily(trades: list[dict]) -> dict:
        """당일 거래를 strategy별로 분리 집계."""
        result: dict[str, dict] = {}
        for strat in ("A", "B"):
            sub = [t for t in trades if (t.get("strategy") or "A") == strat]
            if not sub:
                continue
            pcts = [(t["exit_pct"] or 0) for t in sub]
            wins = [p for p in pcts if p > 0]
            wr   = len(wins) / len(pcts) * 100 if pcts else 0
            avg  = sum(pcts) / len(pcts) if pcts else 0
            result[strat] = {
                "count":     len(sub),
                "win_rate":  round(wr, 1),
                "avg_pct":   round(avg, 3),
                "ev":        round(avg, 3),
                "total_pct": round(sum(pcts), 3),
            }
        return result

    # ── 전략 진화 엔진 실행 ──────────────────────────────────────

    def _run_evolution(self, market: str) -> dict:
        """StrategyEvolutionEngine 호출 — DAILY_REVIEW 직후 자동 실행."""
        try:
            engine = StrategyEvolutionEngine()
            return engine.run(market=market)
        except Exception as e:
            logger.warning(f"[DAILY_REVIEW] 진화 엔진 실행 실패: {e}")
            return {"status": "error", "error": str(e)}

    # ── 누적 파일 저장 ───────────────────────────────────────────

    def _save_history(self, report: dict) -> None:
        """daily_review_history.json 에 날짜별로 누적 저장."""
        try:
            if os.path.exists(_HISTORY_FILE):
                with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                    history: dict[str, Any] = json.load(f)
            else:
                history = {}

            # 키 = "YYYY-MM-DD_KR" 형식
            key = f"{report['date']}_{report['market']}"
            history[key] = report

            # 최근 180일치만 유지 (KR+US 각각이면 최대 360개)
            if len(history) > 400:
                oldest = sorted(history.keys())[:len(history) - 360]
                for k in oldest:
                    del history[k]

            with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)

            logger.info(
                f"[DAILY_REVIEW] 누적 저장 완료 → "
                f"{os.path.basename(_HISTORY_FILE)} "
                f"({len(history)}개 레코드)"
            )
        except Exception as e:
            logger.warning(f"[DAILY_REVIEW] 저장 실패: {e}")
