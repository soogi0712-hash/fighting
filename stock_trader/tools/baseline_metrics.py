#!/usr/bin/env python3
"""
baseline_metrics.py — 전략 수정 '전' baseline 측정 (읽기전용, 실데이터만)

★ 데이터 출처와 한계 (반드시 함께 보고):
  - 실거래 원장(ledger.db)·trade_log.json 이 없음 → '실거래' baseline 은 측정 불가.
  - 유일한 정량 데이터 = strategy_lab 의 lab.db(=ShadowPortfolio '가상거래', 시뮬).
    · 표본: 매도 32건 / 3종목(NAVER·SK하이닉스·삼성전자) / 2026-04~05 약 1개월.
    · lab_trades.profit_pct 는 과거 데이터에서 0 (저장 당시 미기록) → profit(원, net) 사용.
    · 전략 '변형'(S=손절/T=트레일링/P=피라미딩 변형) 탐색용이라, 현재 라이브
      pyramid_strategy.evaluate 의 정확한 상수 조합과 1:1 일치하지 않음.
  - 따라서 아래 수치는 'SIMULATED / 참고용'이며 실거래 성과가 아니다.

사용: python3 tools/baseline_metrics.py <lab.db 복사본 경로>
"""
import sqlite3, sys, math
from collections import defaultdict


def mdd_from_equity(cur, strategy_id=None):
    q = "SELECT equity FROM lab_equity"
    args = ()
    if strategy_id:
        q += " WHERE strategy_id=?"; args = (strategy_id,)
    q += " ORDER BY snap_date"
    eq = [r[0] for r in cur.execute(q, args)]
    if len(eq) < 2:
        return None
    peak = eq[0]; mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100)
    return round(mdd, 2)


def block(title): print("\n" + "=" * 66 + f"\n{title}\n" + "=" * 66)


def main(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True); c = con.cursor()

    sells = c.execute("""SELECT profit, hold_days, reason, strategy_id, trade_date
                         FROM lab_trades WHERE action='SELL' AND profit IS NOT NULL""").fetchall()
    n = len(sells)
    profits = [s[0] for s in sells]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]

    block("[전체 baseline] (SIMULATED — lab.db 섀도우, 실거래 아님)")
    wr = len(wins)/n*100 if n else 0
    avg_w = sum(wins)/len(wins) if wins else 0
    avg_l = sum(losses)/len(losses) if losses else 0
    rr = abs(avg_w/avg_l) if avg_l else 0
    pf = abs(sum(wins)/sum(losses)) if losses and sum(losses) else 0
    ev = sum(profits)/n if n else 0
    print(f"  총 거래 수(SELL)   : {n}")
    print(f"  승률               : {wr:.1f}%  (승 {len(wins)} / 패 {len(losses)})")
    print(f"  평균 순이익        : {avg_w:,.0f}원")
    print(f"  평균 순손실        : {avg_l:,.0f}원")
    print(f"  손익비(avgW/|avgL|): {rr:.2f}")
    print(f"  profit factor      : {pf:.2f}  (총이익/총손실)")
    print(f"  총 순손익          : {sum(profits):,.0f}원")
    print(f"  거래당 기대값(EV)  : {ev:,.0f}원")
    # MDD
    mdds = [mdd_from_equity(c, sid) for sid in
            [r[0] for r in c.execute("SELECT DISTINCT strategy_id FROM lab_equity")]]
    mdds = [m for m in mdds if m is not None]
    if mdds:
        print(f"  최대낙폭(MDD)      : 최악 {min(mdds):.2f}% / 평균 {sum(mdds)/len(mdds):.2f}% (전략별 equity)")
    else:
        print("  최대낙폭(MDD)      : 데이터 부족")

    block("[수수료·세금 전후 차이]")
    print("  per-trade gross/net 분리값: lab_trades 에 수수료·세금 컬럼 없음 → 정확 산출 불가.")
    turnover = sum(abs(s[0]) for s in sells)  # placeholder note
    from_amt = c.execute("SELECT SUM(amount) FROM lab_trades WHERE action='SELL'").fetchone()[0] or 0
    est_cost = from_amt * (0.00015 + 0.0018)   # 매도 수수료+거래세 추정(매도측만)
    print(f"  매도측 회전액      : {from_amt:,.0f}원")
    print(f"  추정 매도비용(0.195%): {est_cost:,.0f}원  ← 추정치(참고), 실제 저장값 아님")

    block("[청산 사유별 손익]")
    by = defaultdict(lambda: [0, 0.0])
    for p, hd, reason, sid, td in sells:
        by[reason][0] += 1; by[reason][1] += p
    for reason, (cnt, tot) in sorted(by.items(), key=lambda x: x[1][1]):
        print(f"  {cnt:2d}건 | 합계 {tot:>12,.0f} | 평균 {tot/cnt:>10,.0f} | {reason}")

    block("[보유시간별 손익] (hold_days)")
    hb = defaultdict(lambda: [0, 0.0])
    for p, hd, *_ in sells:
        k = "0일(당일)" if hd == 0 else ("1일" if hd == 1 else ("2-3일" if hd <= 3 else "4일+"))
        hb[k][0] += 1; hb[k][1] += p
    for k in ["0일(당일)", "1일", "2-3일", "4일+"]:
        if k in hb:
            cnt, tot = hb[k]; print(f"  {k:9s} | {cnt:2d}건 | 합계 {tot:>12,.0f} | 평균 {tot/cnt:>10,.0f}")

    block("[당일청산 vs 오버나이트 분리]")
    intraday = [p for p, hd, *_ in sells if hd == 0]
    overnight = [p for p, hd, *_ in sells if hd > 0]
    for label, arr in [("당일청산(hold=0)", intraday), ("오버나이트(hold>0)", overnight)]:
        if arr:
            w = len([x for x in arr if x > 0])
            print(f"  {label:18s}: {len(arr):2d}건 | 승률 {w/len(arr)*100:4.1f}% | "
                  f"합계 {sum(arr):>12,.0f} | EV {sum(arr)/len(arr):>10,.0f}")
        else:
            print(f"  {label:18s}: 0건")

    block("[표본 신뢰성 경고]")
    codes = c.execute("SELECT COUNT(DISTINCT code) FROM lab_trades").fetchone()[0]
    print(f"  표본 n={n} (<100), 종목 {codes}개, 1개월 → 통계적 대표성 없음. 튜닝 근거로 쓰지 말 것.")
    print("  실거래 baseline 은 ledger.db 축적 후에만 가능(현재 미기록).")
    con.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용: python3 tools/baseline_metrics.py <lab.db 복사본>"); sys.exit(1)
    main(sys.argv[1])
