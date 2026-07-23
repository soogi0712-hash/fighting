#!/usr/bin/env python3
"""
analyze_shadow_lab.py — Strategy-Lab(섀도우/시뮬레이션) 거래 통계 재계산 도구

주의:
  - 이 저장소에는 '실계좌 체결 원장'(trade_history.db 등)이 존재하지 않는다.
  - 유일하게 존재하는 거래성 데이터는 strategy_lab 의 lab.db > lab_trades 이며,
    이는 ShadowPortfolio 가 만든 '가상 거래'다 (실주문 아님).
  - 따라서 아래 통계는 전부 SIMULATED(시뮬) 데이터 기준이다.
  - 원본 DB 는 절대 열지 않는다. 반드시 복사본 경로만 인자로 받는다.

사용법:
  python3 analyze_shadow_lab.py <lab.db 복사본 경로>
"""
import sqlite3, sys, math

def wilson_ci(k, n, z=1.96):
    """이항 비율(승률)의 Wilson 95% 신뢰구간."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    half = (z/denom) * math.sqrt(p*(1-p)/n + z*z/(4*n*n))
    return (max(0.0, center-half), min(1.0, center+half))

def mean_ci(xs, z=1.96):
    """평균(EV)의 근사 95% CI (정규 근사, n작으면 신뢰 낮음)."""
    n = len(xs)
    if n == 0:
        return (0.0, 0.0, 0.0)
    m = sum(xs)/n
    if n == 1:
        return (m, m, m)
    var = sum((x-m)**2 for x in xs)/(n-1)
    se = math.sqrt(var/n)
    return (m, m - z*se, m + z*se)

def analyze(dbpath):
    con = sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True)
    c = con.cursor()

    # ── 0. 데이터 가용성 감사 (handoff 요구 컬럼 실재 여부) ──
    cols = [d[1] for d in c.execute("PRAGMA table_info(lab_trades)")]
    required = {
        "MFE(max_pct)": any("max" in x for x in cols),
        "MAE(min_pct)": any("min" in x for x in cols),
        "signal_type":  "signal_type" in cols,
        "exit_reason":  "exit_reason" in cols,   # 실제로는 free-text 'reason'
        "fill_delay":   "fill_delay_sec" in cols,
        "price_gap":    "price_gap_pct" in cols,
        "param_version":"param_version" in cols,
        "entry_price/exit_price 쌍": ("entry_price" in cols and "exit_price" in cols),
    }
    print("="*70)
    print("[데이터 가용성 감사] lab_trades 컬럼:", cols)
    print("-"*70)
    for k, v in required.items():
        print(f"  {k:28s} : {'있음' if v else '없음  ← 분석 불가'}")
    print("="*70)

    # ── 1. 전략별 통계 (profit=KRW 순손익, profit_pct 는 100% 0 이라 미사용) ──
    rows = c.execute("""
        SELECT strategy_id, profit FROM lab_trades
        WHERE action='SELL' AND profit IS NOT NULL
    """).fetchall()
    from collections import defaultdict
    by = defaultdict(list)
    for sid, p in rows:
        by[sid].append(p)

    print(f"\n{'전략':6s} {'n':>3s} {'승':>3s} {'패':>3s} {'승률%':>6s} "
          f"{'승률95%CI':>16s} {'평균이익':>10s} {'평균손실':>10s} "
          f"{'손익비':>6s} {'진짜PF':>7s} {'EV(원)':>10s} {'EV95%CI하단':>12s}")
    all_pnl = []
    for sid in sorted(by):
        xs = by[sid]; all_pnl += xs
        n = len(xs)
        wins = [x for x in xs if x > 0]
        losses = [x for x in xs if x < 0]
        wr = len(wins)/n if n else 0
        lo, hi = wilson_ci(len(wins), n)
        avg_w = sum(wins)/len(wins) if wins else 0.0
        avg_l = sum(losses)/len(losses) if losses else 0.0
        rr = abs(avg_w/avg_l) if avg_l else 0.0           # 손익비 (avg_win/avg_loss)
        pf = abs(sum(wins)/sum(losses)) if losses and sum(losses) else 0.0  # 진짜 PF
        ev, evlo, evhi = mean_ci(xs)
        print(f"{sid:6s} {n:3d} {len(wins):3d} {len(losses):3d} {wr*100:6.1f} "
              f"[{lo*100:5.1f},{hi*100:5.1f}] {avg_w:10.0f} {avg_l:10.0f} "
              f"{rr:6.2f} {pf:7.2f} {ev:10.0f} {evlo:12.0f}")

    # ── 2. 전체 합산 ──
    n = len(all_pnl)
    wins = [x for x in all_pnl if x > 0]; losses = [x for x in all_pnl if x < 0]
    lo, hi = wilson_ci(len(wins), n)
    ev, evlo, evhi = mean_ci(all_pnl)
    print("-"*120)
    print(f"[전체] n={n}  승={len(wins)}  패={len(losses)}  "
          f"승률={100*len(wins)/n:.1f}% (95%CI {lo*100:.1f}~{hi*100:.1f}%)  "
          f"누적손익={sum(all_pnl):,.0f}원  EV={ev:,.0f}원 (95%CI {evlo:,.0f}~{evhi:,.0f})")

    # ── 3. 청산사유(free-text reason) 별 성과 ──
    print("\n[청산사유별 성과]  (reason 은 자유 텍스트 — 구조화 exit_reason 아님)")
    rr = c.execute("""
        SELECT reason, COUNT(*), ROUND(AVG(profit),0), ROUND(SUM(profit),0)
        FROM lab_trades WHERE action='SELL'
        GROUP BY reason ORDER BY COUNT(*) DESC
    """).fetchall()
    for reason, cnt, avgp, sump in rr:
        print(f"  {cnt:2d}건 | 평균 {avgp:>10,.0f} | 합계 {sump:>12,.0f} | {reason}")

    # ── 4. 표본 게이트 (handoff 규칙: n<100 이면 판정불가) ──
    print("\n[표본 게이트] handoff 규칙: n<100 그룹은 노이즈 → 판정 금지")
    print(f"  전체 SELL 표본 n={n}  → {'판정불가 (n<100)' if n < 100 else '판정가능'}")
    codes = c.execute("SELECT COUNT(DISTINCT code) FROM lab_trades").fetchone()[0]
    dr = c.execute("SELECT MIN(trade_date), MAX(trade_date) FROM lab_trades").fetchone()
    print(f"  대상 종목 수={codes}  기간={dr[0]}~{dr[1]}  → 통계적 대표성 없음")
    con.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python3 analyze_shadow_lab.py <lab.db 복사본 경로>")
        sys.exit(1)
    analyze(sys.argv[1])
