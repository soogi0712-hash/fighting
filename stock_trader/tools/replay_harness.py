"""
replay_harness.py — 라이브 청산 리플레이 하니스 (A·C 후보 비교)

★ 원칙:
  - '현재/A-1/A-2/A-3' 후보는 **실제 pyramid_strategy.evaluate() 를 그대로 호출**한다.
    후보값은 pyramid_strategy 모듈 상수를 '임시' 오버라이드(테스트 후 복원)로만 반영한다.
    → 실제 청산 우선순위·시간청산·trailing high·score 조건·transaction_cost 가 그대로 적용.
  - 'C(청산 단순화)'는 구조 변경 제안이므로 **하니스 내 별도 함수**로 구현(라이브 코드 아님).
  - 동일 가격경로·동일 진입으로 모든 후보를 비교한다.
  - 실제 과거 시계열이 아니라 **synthetic 시나리오**임을 명시(historical replay 와 구분).
  - 라이브 전략에 후보값을 적용하지 않는다(측정 전용).

실행: python3 tools/replay_harness.py
"""
import os, sys
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategies.pyramid_strategy as P
from strategies.pyramid_strategy import PyramidStrategyManager, PyramidPosition
from screener.transaction_cost import calc_sell_proceeds, net_profit_pct_from_cost

CODE, NAME, ENTRY, QTY = "005930", "테스트", 10000, 100

# ── 후보: 실제 evaluate 에 적용할 '모듈 상수 오버라이드' ──────────
CANDIDATES = {
    "현재":  {},
    "A-1":  {"STOP_LOSS_PCT": -1.5},
    "A-2":  {"STOP_LOSS_PCT": -2.0, "PROFIT_FULL_PCT": 4.0, "PROFIT_SUPER_PCT": 4.5},
    "A-3":  {"STOP_LOSS_PCT": -2.0},
}

# ── synthetic 시나리오: (경과분, 현재가, sell_score) 시퀀스 ──────
def px(pct):  # gross % → 가격
    return round(ENTRY * (1 + pct/100), 2)

SCENARIOS = {
    "1.진입후 바로 +2%":       [(1, px(2.6), 0), (2, px(2.6), 0)],
    "2.+1.8%후 +0.7% 하락":    [(1, px(1.8), 0), (3, px(0.9), 0), (25, px(0.7), 0)],
    "3.+1.4%후 횡보":          [(1, px(1.4), 0), (25, px(1.1), 0), (45, px(1.1), 0)],
    "4.-1.5% 직행":            [(1, px(-1.5), 0), (30, px(-1.5), 0)],
    "5.-2%후 반등":            [(1, px(-2.0), 0), (5, px(1.5), 0), (10, px(2.6), 0)],
    "6.20분 시간청산":         [(5, px(0.3), 0), (21, px(0.3), 0)],
    "7.수수료전이익 후손실":    [(2, px(0.1), 0), (30, px(0.1), 0)],
    "8.원화익절+%익절 동시":    [(1, px(4.0), 0)],
    "9.트레일링+SELLSCORE동시": [(1, px(1.8), 7), (3, px(1.6), 7)],
    "10.갭하락 손절선아래":     [(1, px(-7.0), 0)],
}


def _apply(overrides):
    saved = {k: getattr(P, k) for k in overrides}
    for k, v in overrides.items():
        setattr(P, k, v)
    return saved

def _restore(saved):
    for k, v in saved.items():
        setattr(P, k, v)


def _new_pos():
    pos = PyramidPosition(CODE, NAME, ENTRY)
    pos.add_level(1, QTY, ENTRY)     # 수수료 포함 avg_price 세팅
    return pos


def _pnl(pos, exit_price):
    gross = (exit_price - ENTRY) * QTY
    net = calc_sell_proceeds(exit_price, QTY).net_proceeds - pos.avg_price * QTY
    return round(gross, 0), round(net, 0)


def run_real(overrides, path):
    """실제 evaluate 로 시나리오 구동."""
    saved = _apply(overrides)
    try:
        mgr = PyramidStrategyManager(None, 5_000_000, 5_000_000)
        pos = _new_pos(); mgr.positions[CODE] = pos
        base_now = datetime.now()
        mfe = mae = 0.0
        for elapsed, price, score in path:
            mfe = max(mfe, (price - ENTRY)/ENTRY*100)
            mae = min(mae, (price - ENTRY)/ENTRY*100)
            pos.created_at = (base_now - timedelta(minutes=elapsed)).isoformat()
            res = mgr.evaluate(CODE, NAME, price, 0, 0.0, today_high=0.0, sell_score=score)
            if res.get("action") in ("SELL_ALL", "SELL_PARTIAL"):
                g, n = _pnl(pos, price)
                return {"exit_min": elapsed, "action": res["action"],
                        "reason": res["reason"][:42], "gross": g, "net": n,
                        "mfe": round(mfe, 2), "mae": round(mae, 2), "hold_min": elapsed}
        # 미청산 → 경로끝 강제 종가
        last_elapsed, last_price, _ = path[-1]
        g, n = _pnl(pos, last_price)
        return {"exit_min": last_elapsed, "action": "경로끝(미청산)",
                "reason": "path_end", "gross": g, "net": n,
                "mfe": round(mfe, 2), "mae": round(mae, 2), "hold_min": last_elapsed}
    finally:
        _restore(saved)


# ── C 후보(청산 단순화 제안, 라이브 코드 아님) ───────────────────
# R = 최초 손절폭 2%. 3분기: 손절 -1R / +1R 도달후 트레일 0.5R 반납 / 40분 시간청산
C_STOP_R, C_TRAIL_ACT, C_TRAIL_GIVE, C_TIME_MIN, C_TIME_PCT = -2.0, 2.0, 1.0, 40, 1.0

def run_C(path):
    pos = _new_pos()
    base_now = datetime.now(); mfe = mae = 0.0; high_net = -999
    for elapsed, price, score in path:
        mfe = max(mfe, (price - ENTRY)/ENTRY*100)
        mae = min(mae, (price - ENTRY)/ENTRY*100)
        net = net_profit_pct_from_cost(pos.avg_price, price)
        high_net = max(high_net, net)
        reason = None
        if net <= C_STOP_R:
            reason = f"C:손절(-1R, net{net:.2f}%≤{C_STOP_R}%)"
        elif high_net >= C_TRAIL_ACT and net <= high_net - C_TRAIL_GIVE:
            reason = f"C:트레일반납(고점net{high_net:.2f}%→{net:.2f}%)"
        elif elapsed >= C_TIME_MIN and net < C_TIME_PCT:
            reason = f"C:시간청산({elapsed:.0f}분 net{net:.2f}%<{C_TIME_PCT}%)"
        if reason:
            g, n = _pnl(pos, price)
            return {"exit_min": elapsed, "action": "SELL_ALL", "reason": reason,
                    "gross": g, "net": n, "mfe": round(mfe,2), "mae": round(mae,2), "hold_min": elapsed}
    last_elapsed, last_price, _ = path[-1]
    g, n = _pnl(pos, last_price)
    return {"exit_min": last_elapsed, "action": "경로끝(미청산)", "reason": "path_end",
            "gross": g, "net": n, "mfe": round(mfe,2), "mae": round(mae,2), "hold_min": last_elapsed}


def main():
    print("=" * 100)
    print("리플레이 하니스 — synthetic 시나리오 (historical replay 아님) | 실제 evaluate 호출(현재/A-*)")
    print(f"진입 {ENTRY}원 × {QTY}주 (avg_price 수수료포함={_new_pos().avg_price:.2f})")
    print("=" * 100)
    for sname, path in SCENARIOS.items():
        print(f"\n▼ {sname}")
        print(f"  {'후보':5s} {'청산분':>5s} {'action':>14s} {'gross':>9s} {'net':>9s} {'MFE%':>6s} {'MAE%':>6s} | 사유")
        for cname, ov in CANDIDATES.items():
            r = run_real(ov, path)
            print(f"  {cname:5s} {r['exit_min']:>5} {r['action']:>14s} {r['gross']:>9,.0f} {r['net']:>9,.0f} "
                  f"{r['mfe']:>6.2f} {r['mae']:>6.2f} | {r['reason']}")
        rc = run_C(path)
        print(f"  {'C':5s} {rc['exit_min']:>5} {rc['action']:>14s} {rc['gross']:>9,.0f} {rc['net']:>9,.0f} "
              f"{rc['mfe']:>6.2f} {rc['mae']:>6.2f} | {rc['reason']}")
    print("\n※ 후보값은 라이브에 적용하지 않음(측정 전용). synthetic 결과이며 실거래 성과 아님.")


if __name__ == "__main__":
    main()
