"""
adaptive/daily_reporter.py — 일일 학습 보고서 자동 생성
========================================================
국내장 종료(15:30) / 미국장 종료(06:00 KST) 후 자동 생성.

출력:
  로그 파일: logs/daily_report_YYYYMMDD.log
  JSON:      data/daily_report_YYYYMMDD.json

보고서 내용:
  1. 오늘 거래 요약 (KR / US 분리)
  2. 전략별 성과 (signal_type별 승률/EV)
  3. 자동 조정 결과 (가중치 변동)
  4. 누적 복리 성과
"""

import os
import json
from datetime import datetime, date
from typing import Optional

from utils.v2_logger import get_logger
from adaptive.trade_recorder   import TradeRecorder, _DATA_DIR, _get_conn
from adaptive.strategy_analyzer import StrategyAnalyzer, _calc_stats
from adaptive.weight_adjuster   import WeightAdjuster

logger = get_logger("DailyReporter")

_LOG_DIR    = os.path.join(os.path.dirname(__file__), "..", "logs")
_REPORT_DIR = _DATA_DIR


def _fmt(v, prefix="", suffix="", zero="-"):
    if v is None or v == 0:
        return zero
    return f"{prefix}{v:+.1f}{suffix}" if isinstance(v, float) else f"{prefix}{v}{suffix}"


class DailyReporter:
    """
    일일 학습 보고서 생성기.

    사용법:
        reporter = DailyReporter(recorder, analyzer, adjuster, account_sync)
        reporter.generate("KR")    # KR 장 종료 후
        reporter.generate("US")    # US 장 종료 후
    """

    def __init__(self,
                 recorder:  TradeRecorder,
                 analyzer:  StrategyAnalyzer,
                 adjuster:  WeightAdjuster,
                 account_sync=None):
        self.recorder     = recorder
        self.analyzer     = analyzer
        self.adjuster     = adjuster
        self.account_sync = account_sync
        os.makedirs(_LOG_DIR,    exist_ok=True)
        os.makedirs(_REPORT_DIR, exist_ok=True)

    # ── 메인 생성 ────────────────────────────────────────────

    def generate(self, market: str) -> dict:
        """
        market="KR" or "US" 일일 보고서 생성.
        Returns: 보고서 dict
        """
        today     = date.today().isoformat()
        market_up = market.upper()

        # ── 오늘 거래 조회 ──────────────────────────────────
        try:
            conn  = _get_conn()
            rows  = conn.execute("""
                SELECT * FROM trades
                WHERE status='closed'
                  AND market=?
                  AND date(exit_time)=?
                ORDER BY exit_time ASC
            """, (market_up, today)).fetchall()
            conn.close()
            trades = [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[DailyReporter] DB 조회 실패: {e}")
            trades = []

        # ── 전체 통계 ────────────────────────────────────────
        all_stats = _calc_stats(trades)

        # ── 전략별 통계 ──────────────────────────────────────
        signal_stats = {}
        signals = (StrategyAnalyzer.KR_SIGNALS
                   if market_up == "KR"
                   else StrategyAnalyzer.US_SIGNALS)
        for sig in signals:
            sub = [t for t in trades if t.get("signal_type") == sig]
            if sub:
                signal_stats[sig] = _calc_stats(sub)

        # ── 가중치 조정 실행 ─────────────────────────────────
        adj_report = self.adjuster.adjust(market=market_up)

        # ── 누적 계좌 성과 ───────────────────────────────────
        acc_info = {}
        if self.account_sync:
            try:
                s = self.account_sync.status()
                acc_info = {
                    "total_asset":    s.get("total_asset",    0),
                    "initial_asset":  s.get("initial_asset",  0),
                    "compound_ratio": s.get("compound_ratio", 0),
                }
            except Exception:
                pass

        # ── 최고/최저 전략 ────────────────────────────────────
        best_sig  = max(signal_stats.items(),
                        key=lambda x: x[1]["ev"],
                        default=(None, {}))
        worst_sig = min(signal_stats.items(),
                        key=lambda x: x[1]["ev"],
                        default=(None, {}))

        report = {
            "date":         today,
            "market":       market_up,
            "generated_at": datetime.now().isoformat(),
            "summary":      all_stats,
            "by_signal":    signal_stats,
            "weight_adjustments": adj_report,
            "account":      acc_info,
            "best_signal":  best_sig[0],
            "worst_signal": worst_sig[0],
        }

        # ★ 학습 사례 분석
        report["case_analysis"] = self._analyze_cases(trades)

        # ── 월간 통계 계산 ───────────────────────────────────
        report["monthly"] = self._calc_monthly(market_up)

        # ── 파일 저장 ────────────────────────────────────────
        self._save_report(report, market_up, today)

        # ── 로그 출력 ────────────────────────────────────────
        self._print_summary(report, market_up, all_stats,
                            signal_stats, adj_report, acc_info)

        return report

    # ── 월간 통계 ────────────────────────────────────────────

    def _calc_monthly(self, market: str) -> dict:
        """이번 달 전체 거래 기반 월간 통계."""
        try:
            month_start = date.today().replace(day=1).isoformat()
            conn  = _get_conn()
            rows  = conn.execute("""
                SELECT exit_pct, pnl_krw, hold_min, signal_type
                FROM trades
                WHERE status='closed'
                  AND market=?
                  AND date(exit_time) >= ?
                ORDER BY exit_time ASC
            """, (market, month_start)).fetchall()
            conn.close()
            trades = [dict(r) for r in rows]
            if not trades:
                return {}
            stats = _calc_stats(trades)

            # 최고/최저 전략
            sigs = {}
            for t in trades:
                sig = t.get("signal_type", "?")
                sigs.setdefault(sig, []).append(t["exit_pct"] or 0)
            best  = max(sigs.items(), key=lambda x: sum(x[1])/len(x[1]), default=(None, []))
            worst = min(sigs.items(), key=lambda x: sum(x[1])/len(x[1]), default=(None, []))

            return {
                **stats,
                "best_signal":  best[0],
                "worst_signal": worst[0],
                "month_start":  month_start,
            }
        except Exception:
            return {}

    # ── 보고서 파일 저장 ─────────────────────────────────────

    def _save_report(self, report: dict, market: str, today: str):
        fname = os.path.join(
            _REPORT_DIR,
            f"daily_report_{market}_{today.replace('-','')}.json"
        )
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    # ── 콘솔 출력 ────────────────────────────────────────────

    def _print_summary(self, report, market, stats, signal_stats,
                       adj_report, acc_info):
        lines = []
        lines.append("=" * 68)
        lines.append(f"  ★ {market} DAILY LEARNING REPORT  {report['date']}")
        lines.append("=" * 68)

        # ── 1. 거래 요약 ────────────────────────────────────
        lines.append("  [거래 요약]")
        lines.append(f"  거래횟수   = {stats['trade_count']}건")
        lines.append(f"  승  률     = {stats['win_rate']:.1f}%  "
                     f"(익절={stats.get('win_count',0)} / 손절={stats.get('loss_count',0)})")
        lines.append(f"  실현손익   = {stats['total_pnl_krw']:+,.0f}원")
        lines.append(f"  평균익절   = {stats['avg_win_pct']:+.3f}%")
        lines.append(f"  평균손절   = {stats['avg_loss_pct']:+.3f}%")
        lines.append(f"  손  익  비 = {stats['profit_factor']:.2f}")
        lines.append(f"  기대값(EV) = {stats['ev']:+.3f}%")
        lines.append(f"  최대연속손실= {stats['max_consec_loss']}연속")
        lines.append(f"  평균보유시간= {stats['avg_hold_min']:.1f}분")
        lines.append("-" * 68)

        # ── 2. 전략별 성과 ───────────────────────────────────
        if signal_stats:
            lines.append("  [전략별 성과]")
            for sig, s in sorted(signal_stats.items(),
                                  key=lambda x: x[1]["ev"], reverse=True):
                lines.append(
                    f"  {sig:<12} | "
                    f"n={s['trade_count']:3d} | "
                    f"승률={s['win_rate']:5.1f}% | "
                    f"EV={s['ev']:+.3f}% | "
                    f"손익비={s['profit_factor']:.2f}"
                )
        lines.append("-" * 68)

        # ── 3. 학습 분석 사례 ─────────────────────────────────
        cases = report.get("case_analysis", {})
        lines.append("  [학습 분석]")

        # 수익반납 사례
        profit_return = cases.get("profit_return", [])
        if profit_return:
            lines.append(f"  수익반납 사례 ({len(profit_return)}건):")
            for c in profit_return[:3]:
                lines.append(
                    f"    {c['code']} | 최고={c.get('max_pct',0):+.2f}% → "
                    f"청산={c.get('exit_pct',0):+.2f}% | {c.get('exit_reason','')}"
                )
        else:
            lines.append("  수익반납 사례  = 없음")

        # 손절 지연 사례
        stoploss_delay = cases.get("stoploss_delay", [])
        if stoploss_delay:
            lines.append(f"  손절 지연 사례 ({len(stoploss_delay)}건):")
            for c in stoploss_delay[:3]:
                lines.append(
                    f"    {c['code']} | 최저={c.get('min_pct',0):+.2f}% | "
                    f"보유={c.get('hold_min',0):.0f}분 | {c.get('exit_reason','')}"
                )
        else:
            lines.append("  손절 지연 사례 = 없음")

        # 진입 지연 사례
        entry_delay = cases.get("entry_delay", [])
        if entry_delay:
            lines.append(f"  진입 지연 사례 ({len(entry_delay)}건):")
            for c in entry_delay[:3]:
                lines.append(
                    f"    {c['code']} | 신호→주문={c.get('signal_delay_sec',0):.1f}s | "
                    f"가격괴리={c.get('price_gap_pct',0):+.3f}%"
                )
        else:
            lines.append("  진입 지연 사례 = 없음")

        # API/체결 지연 사례
        api_delay = cases.get("api_delay", [])
        if api_delay:
            lines.append(f"  API 지연 사례  ({len(api_delay)}건):")
            for c in api_delay[:3]:
                lines.append(
                    f"    {c['code']} | 주문→체결={c.get('fill_delay_sec',0):.1f}s"
                )
        else:
            lines.append("  API 지연 사례  = 없음")

        lines.append("-" * 68)

        # ── 4. 자동 조정 결과 ────────────────────────────────
        if adj_report:
            lines.append("  [자동 조정 결과]")
            for a in adj_report:
                delta = a["new_weight"] - a["old_weight"]
                sign  = "+" if delta >= 0 else ""
                lines.append(
                    f"  {a['signal_type']:<12} "
                    f"가중치 {a['old_weight']:.2f} → {a['new_weight']:.2f} "
                    f"({sign}{delta*100:.0f}%) | {a['reason']}"
                )
        else:
            lines.append("  [자동 조정 결과] 변동 없음")
        lines.append("-" * 68)

        # ── 5. 내일 개선 포인트 ──────────────────────────────
        lines.append("  [내일 개선 포인트]")
        improve_points = self._gen_improve_points(stats, cases, signal_stats)
        for pt in improve_points:
            lines.append(f"  • {pt}")
        lines.append("-" * 68)

        # ── 6. 계좌 / 월간 현황 ─────────────────────────────
        best  = report.get("best_signal")
        worst = report.get("worst_signal")
        lines.append(f"  오늘 최고 전략  : {best  or '-'}")
        lines.append(f"  오늘 최저 전략  : {worst or '-'}")

        if acc_info:
            lines.append(
                f"  총  자  산      : {acc_info.get('total_asset',0):,.0f}원"
            )
            lines.append(
                f"  복리수익률      : {acc_info.get('compound_ratio',0):+.2f}%"
            )

        m = report.get("monthly", {})
        if m:
            lines.append("-" * 68)
            lines.append("  [이번달 누적]")
            lines.append(f"  월간손익   = {m.get('total_pnl_krw',0):+,.0f}원")
            lines.append(f"  월간승률   = {m.get('win_rate',0):.1f}%")
            lines.append(f"  월간EV     = {m.get('ev',0):+.3f}%")
            lines.append(f"  강화후보   = {m.get('best_signal','-')}")
            lines.append(f"  삭제후보   = {m.get('worst_signal','-')}")

        lines.append("=" * 68)

        summary = "\n".join(lines)
        for line in lines:
            logger.info(line)

        # 파일에도 저장
        log_fname = os.path.join(
            _LOG_DIR,
            f"daily_report_{market}_{report['date'].replace('-','')}.log"
        )
        with open(log_fname, "w", encoding="utf-8") as f:
            f.write(summary + "\n")

    # ── 학습 사례 분석 ─────────────────────────────────────

    def _analyze_cases(self, trades: list) -> dict:
        """거래 목록에서 학습 사례 추출."""
        profit_return  = []  # 수익 반납 (max_pct 높은데 exit_pct 낮음)
        stoploss_delay = []  # 손절 지연 (min_pct 매우 낮은데 늦게 청산)
        entry_delay    = []  # 진입 지연 (signal_delay_sec 큰 것)
        api_delay      = []  # API 지연 (fill_delay_sec 큰 것)

        for t in trades:
            code    = t.get("code", "?")
            ep      = t.get("exit_pct",         0) or 0
            max_p   = t.get("max_pct",          None)
            min_p   = t.get("min_pct",          None)
            hold    = t.get("hold_min",         0)  or 0
            sig_d   = t.get("signal_delay_sec", None)
            fill_d  = t.get("fill_delay_sec",   None)
            gap     = t.get("price_gap_pct",    None)
            reason  = t.get("exit_reason",      "")

            # 수익 반납: max_pct 존재하고, max - exit > 1%
            if max_p is not None and (max_p - ep) >= 1.0 and max_p > 0.3:
                profit_return.append({
                    "code": code, "max_pct": max_p,
                    "exit_pct": ep, "exit_reason": reason,
                })

            # 손절 지연: 손절이고 min_pct < -1.5% 이고 hold > 30분
            if ep < 0 and min_p is not None and min_p < -1.5 and hold > 30:
                stoploss_delay.append({
                    "code": code, "min_pct": min_p,
                    "hold_min": hold, "exit_reason": reason,
                })

            # 진입 지연: signal_delay > 3초
            if sig_d is not None and sig_d > 3.0:
                entry_delay.append({
                    "code": code, "signal_delay_sec": sig_d,
                    "price_gap_pct": gap or 0,
                })

            # API 지연: fill_delay > 5초
            if fill_d is not None and fill_d > 5.0:
                api_delay.append({
                    "code": code, "fill_delay_sec": fill_d,
                })

        # 각 사례 정렬
        profit_return.sort(key=lambda x: x["max_pct"] - x["exit_pct"], reverse=True)
        stoploss_delay.sort(key=lambda x: x["min_pct"])
        entry_delay.sort(key=lambda x: x["signal_delay_sec"], reverse=True)
        api_delay.sort(key=lambda x: x["fill_delay_sec"], reverse=True)

        return {
            "profit_return":  profit_return,
            "stoploss_delay": stoploss_delay,
            "entry_delay":    entry_delay,
            "api_delay":      api_delay,
        }

    def _gen_improve_points(self, stats: dict, cases: dict,
                             signal_stats: dict) -> list:
        """통계 + 사례 → 내일 개선 포인트 자동 생성."""
        points = []

        wr = stats.get("win_rate", 0)
        ev = stats.get("ev", 0)
        mc = stats.get("max_consec_loss", 0)
        pr = cases.get("profit_return",  [])
        sd = cases.get("stoploss_delay", [])
        ed = cases.get("entry_delay",    [])
        ad = cases.get("api_delay",      [])

        if ev < 0:
            points.append(f"기대값(EV) {ev:+.3f}% — 진입 조건 강화 또는 손절선 앞당기기 검토")
        if wr < 45:
            points.append(f"승률 {wr:.1f}% — 진입 점수 임계값 상향 조정 검토")
        if mc >= 3:
            points.append(f"최대연속손절 {mc}회 — 일손절 한도 설정 또는 신호 필터 강화")
        if len(pr) >= 2:
            points.append(
                f"수익반납 {len(pr)}건 — trailing stop 또는 분할익절 도입 검토"
            )
        if len(sd) >= 2:
            points.append(
                f"손절지연 {len(sd)}건 — 최대 보유시간(hold_max) 단축 또는 손절선 상향 검토"
            )
        if len(ed) >= 2:
            avg_delay = sum(c["signal_delay_sec"] for c in ed) / len(ed)
            points.append(
                f"진입지연 평균 {avg_delay:.1f}s — API rate limit 완화 또는 prefetch 개선 검토"
            )
        if len(ad) >= 2:
            avg_fill = sum(c["fill_delay_sec"] for c in ad) / len(ad)
            points.append(
                f"체결지연 평균 {avg_fill:.1f}s — 시장가 주문 비율 높이기 또는 KIS 응답시간 모니터링"
            )

        # 전략별 개선
        for sig, s in signal_stats.items():
            if s["trade_count"] >= 3 and s["ev"] < -0.3:
                points.append(
                    f"'{sig}' 전략 EV {s['ev']:+.3f}% — 비활성화 또는 기준 강화 검토"
                )

        if not points:
            points.append("특이사항 없음 — 현재 전략 유지")

        return points
