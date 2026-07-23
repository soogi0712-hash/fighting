"""
session_split.py — 당일(intraday) / 오버나이트(overnight) 통계 분리 (전략 무변경)

분류 기준(단순 24시간 아님, 시장 거래일 기준):
  - KR: 매수/매도 시각을 Asia/Seoul 로 변환한 '거래일(달력일)' 비교.
  - US: America/New_York 로 변환한 거래일 비교.
  - 프리마켓·정규장·애프터마켓은 '같은 시장 달력일'에 속하면 intraday 로 본다.
    (KR: 08:30 프리 ~ 18:00 시간외 = 같은 날짜 / US: 04:00 프리 ~ 20:00 애프터 = 같은 ET 날짜)
  - 같은 날짜라도 매도가 다음 시장 거래일로 넘어가면 overnight.
  - 타임존 정보가 없으면(naive) 임의 분류하지 않고 unknown.

반환: 'intraday' | 'overnight' | 'unknown'
"""
from datetime import datetime
import pytz

_TZ = {"KR": pytz.timezone("Asia/Seoul"), "US": pytz.timezone("America/New_York")}


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt
    except Exception:
        return None


def classify_session(market, entry_ts, exit_ts, assume_market_tz_for_naive=False):
    """
    intraday/overnight/unknown 판정.
    - tz-aware 타임스탬프: 시장 tz 로 변환 후 거래일(date) 비교.
    - naive 타임스탬프: 기본 unknown (assume_market_tz_for_naive=True 면 시장 tz 로 간주).
    """
    tz = _TZ.get(str(market).upper())
    if tz is None:
        return "unknown"
    e, x = _parse(entry_ts), _parse(exit_ts)
    if e is None or x is None:
        return "unknown"

    def to_market_date(dt):
        if dt.tzinfo is None:
            if not assume_market_tz_for_naive:
                return None                      # tz 불명 → unknown
            dt = tz.localize(dt)
        return dt.astimezone(tz).date()

    ed, xd = to_market_date(e), to_market_date(x)
    if ed is None or xd is None:
        return "unknown"
    return "intraday" if ed == xd else "overnight"


def _hold_seconds(t):
    if t.get("hold_seconds") is not None:
        return t["hold_seconds"]
    e, x = _parse(t.get("entry_ts")), _parse(t.get("exit_ts"))
    if e and x and e.tzinfo == x.tzinfo:
        try:
            return (x - e).total_seconds()
        except Exception:
            return None
    return None


def session_report(trades, assume_market_tz_for_naive=False):
    """
    trades: [{market, entry_ts, exit_ts, net_pnl, hold_seconds?}, ...]
    반환: {group: {count, win_rate, total_net, avg_net, profit_factor, avg_hold_sec}}
    """
    groups = {"intraday": [], "overnight": [], "unknown": []}
    for t in trades:
        g = classify_session(t.get("market"), t.get("entry_ts"), t.get("exit_ts"),
                             assume_market_tz_for_naive)
        groups[g].append(t)

    out = {}
    for g, arr in groups.items():
        n = len(arr)
        pnls = [t.get("net_pnl", 0) or 0 for t in arr]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        holds = [h for h in (_hold_seconds(t) for t in arr) if h is not None]
        out[g] = {
            "count": n,
            "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "total_net": round(sum(pnls), 0),
            "avg_net": round(sum(pnls) / n, 0) if n else 0.0,
            "profit_factor": round(abs(sum(wins) / sum(losses)), 2) if losses and sum(losses) else (99.9 if wins else 0.0),
            "avg_hold_sec": round(sum(holds) / len(holds), 0) if holds else None,
        }
    return out


def _load_ledger(db_path):
    import sqlite3, os
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT market, entry_time, exit_time, net_pnl, hold_seconds "
            "FROM trades WHERE status='CLOSED'").fetchall()
    except Exception:
        return []
    finally:
        con.close()
    return [{"market": r["market"], "entry_ts": r["entry_time"], "exit_ts": r["exit_time"],
             "net_pnl": r["net_pnl"], "hold_seconds": r["hold_seconds"]} for r in rows]


if __name__ == "__main__":
    import sys, json, os
    db = sys.argv[1] if len(sys.argv) > 1 else "data/ledger.db"
    trades = _load_ledger(db)
    print(f"[session_split] source={db}  closed trades={len(trades)}")
    if not trades:
        print("  실거래 원장(CLOSED) 데이터 없음 → 분리 대상 없음.")
        print("  (lab.db 는 체결 타임스탬프가 없어 세션 분류 불가 → unknown 처리 대상)")
    rep = session_report(trades)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
