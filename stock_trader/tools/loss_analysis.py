#!/usr/bin/env python3
"""
loss_analysis.py — 손실 반복 원인 최소 점검 (D).

실체결 로그(원장) 우선. 없으면 그 사실을 명시하고, 참고용으로 lab.db 의
시뮬레이션 거래(lab_trades)를 '실거래 아님' 명시 하에 집계한다.

주의: 추적 DB 를 직접 열지 않는다(WAL 오염 방지) — 항상 복사본 사용.
사용:  python tools/loss_analysis.py
"""
import os
import sys
import json
import shutil
import sqlite3
import tempfile
from collections import defaultdict

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(BASE, "data")


def _copy_db(name):
    src = os.path.join(DATA, name)
    if not os.path.exists(src):
        return None
    d = tempfile.mkdtemp()
    for ext in ("", "-wal", "-shm"):
        s = src + ext
        if os.path.exists(s):
            shutil.copy(s, os.path.join(d, name + ext))
    return os.path.join(d, name)


def _stats(sells):
    """sells: list of dict with 'profit'(net krw), 'reason', 'code', 'hold'."""
    n = len(sells)
    wins = [s for s in sells if s["profit"] > 0]
    losses = [s for s in sells if s["profit"] < 0]
    gross_win = sum(s["profit"] for s in wins)
    gross_loss = -sum(s["profit"] for s in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    # 최대 연속 손실
    mcl = cur = 0
    for s in sells:
        if s["profit"] < 0:
            cur += 1; mcl = max(mcl, cur)
        else:
            cur = 0
    return {
        "trades": n,
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": pf,
        "max_consecutive_losses": mcl,
        "net": sum(s["profit"] for s in sells),
    }


def _by(sells, keyfn):
    agg = defaultdict(lambda: {"n": 0, "net": 0.0, "wins": 0})
    for s in sells:
        k = keyfn(s)
        agg[k]["n"] += 1
        agg[k]["net"] += s["profit"]
        agg[k]["wins"] += 1 if s["profit"] > 0 else 0
    return agg


def main():
    print("=" * 64)
    print("  loss_analysis — 손실 반복 원인 최소 점검")
    print("=" * 64)

    # 1) 실체결 로그 존재 확인
    real_log = os.path.join(DATA, "trade_log.json")
    ledger_dbs = [f for f in os.listdir(DATA) if f.startswith("ledger_") and f.endswith(".db")] \
        if os.path.isdir(DATA) else []
    print("\n[실거래 데이터 가용성]")
    print(f"  trade_log.json: {'있음' if os.path.exists(real_log) else '❌ 없음'}")
    print(f"  ledger_*.db:    {ledger_dbs if ledger_dbs else '❌ 없음(실체결 원장 미생성)'}")

    real_sells = []
    if os.path.exists(real_log):
        try:
            logs = json.load(open(real_log, encoding="utf-8"))
            for e in logs:
                if str(e.get("action", "")).startswith("SELL"):
                    prof = e.get("profit")
                    if isinstance(prof, dict):
                        prof = prof.get("net_profit", 0)
                    real_sells.append({"profit": float(prof or 0),
                                       "reason": e.get("reason", ""),
                                       "code": e.get("code", ""),
                                       "hold": e.get("elapsed_min", 0)})
        except Exception as ex:
            print(f"  trade_log 파싱 오류: {ex}")

    if real_sells:
        print(f"\n[실거래 매도 {len(real_sells)}건 집계]")
        st = _stats(real_sells)
        for k, v in st.items():
            print(f"  {k}: {v}")
    else:
        print("\n★ 실체결 매도 로그 없음 → 실거래 손익 집계 불가.")
        print("  (실거래 EV/승률 산출은 fill 기반 원장 배선 + 실거래 후에야 가능)")

    # 2) 참고: lab.db 시뮬레이션(실거래 아님)
    labpath = _copy_db("lab.db")
    if labpath:
        print("\n[참고: lab.db lab_trades — 시뮬레이션(실거래 아님)]")
        c = sqlite3.connect(labpath); c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM lab_trades WHERE action LIKE 'SELL%' ORDER BY id")]
        sells = [{"profit": float(r.get("profit") or 0),
                  "reason": r.get("reason", ""), "code": r.get("code", ""),
                  "hold": r.get("hold_days", 0)} for r in rows]
        if not sells:
            print("  SELL 행 없음.")
        else:
            st = _stats(sells)
            print(f"  총 매도 {st['trades']}건 | 승률 {st['win_rate']:.1f}% | "
                  f"평균이익 {st['avg_win']:,.0f} | 평균손실 {st['avg_loss']:,.0f} | "
                  f"PF {st['profit_factor']:.2f} | 최대연속손실 {st['max_consecutive_losses']} | "
                  f"순손익 {st['net']:,.0f}")
            print("  ● 사유별 순손익:")
            for k, v in sorted(_by(sells, lambda s: s["reason"]).items(),
                               key=lambda kv: kv[1]["net"]):
                print(f"     {k[:30]:30} n={v['n']:3} 승={v['wins']:3} net={v['net']:,.0f}")
            print("  ● 종목별 순손익(하위 5):")
            for k, v in sorted(_by(sells, lambda s: s["code"]).items(),
                               key=lambda kv: kv[1]["net"])[:5]:
                print(f"     {k:10} n={v['n']:3} net={v['net']:,.0f}")
        print("\n  ⚠️ 위 수치는 시뮬레이션 데이터이며 실거래 손실의 근거로 사용할 수 없음.")
    else:
        print("\n  lab.db 없음.")

    print("\n" + "=" * 64)
    print("결론: 실체결 로그가 없어 '반복 손실'의 실데이터 원인 집계는 불가.")
    print("      → fill 기반 원장 배선 후 실거래 데이터 수집이 선행돼야 함.")
    print("      Recovery Mode 는 이 데이터 부재 상황에서 손실 노출을 구조적으로 제한한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
