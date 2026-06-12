"""
통합 테스트: 장기 복리수익률 극대화 설계 철학 적용 검증
======================================================

테스트 범위:
  1. trade_decision.py
     - decide_sell(): 실질수익률 기반 손절 (-10% net)
     - decide_sell(): 트레일링 활성화 = 실질수익률 +5% 기준 역산
     - decide_add_buy(): ADD_BUY_LEVELS는 실질수익률 기준
     - decide_buy(): AI 점수별 동적 비중 상한 적용
     - calc_dynamic_weight(): 10% / 20% / 25% 분기
     - prioritize_reallocation(): 강도 점수 내림차순 + 금액 비례 배분

  2. risk_guard.py
     - check_buy(): 기본 10% 비중 한도
     - check_buy(): AI ≥ 80 → 20%, AI ≥ 90 + RS ≥ 5 → 25%
     - check_buy(): 업종 25% 한도는 엘리트 종목에도 적용
     - max_buy_amount(): 동적 비중 반영

  3. indicator_validator.py
     - validate(): trend_score 반환 (0~100)
     - validate(): strong_trend = trend_score ≥ 60
     - _calc_trend_score(): A~D 4개 요소 합산
     - trend_detail 키 존재 확인

  4. 복리수익률 일관성
     - 동일 avg_price 로 net_pct = 0% → 손익분기 일치
     - 손절 트리거 가격에서 실질수익률 = -10% 확인
     - 트레일링 활성화 가격에서 실질수익률 = +5% 확인
"""

import sys
import os
import math
import pandas as pd
import numpy as np

# stock_trader 루트를 경로에 추가
# tests/ 의 부모 디렉토리 = stock_trader/
BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

from screener.transaction_cost import (
    net_profit_pct_from_cost,
    price_for_net_pct_from_cost,
    calc_buy_cost,
)
from screener.trade_decision import (
    TradeDecisionEngine,
    calc_dynamic_weight,
    STOP_LOSS_PCT,
    TRAILING_STOP_PCT,
    TRAILING_ACTIVATE_NET_PCT,
    ADD_BUY_LEVELS,
    WEIGHT_BASE,
    WEIGHT_HIGH_SCORE,
    WEIGHT_ELITE,
    SCORE_HIGH_THRESH,
    SCORE_ELITE_THRESH,
    RS_ELITE_THRESH,
)
from screener.risk_guard import (
    RiskGuard,
    MAX_SECTOR_WEIGHT,
    DAILY_LOSS_LIMIT,
    TOTAL_LOSS_LIMIT,
)
from strategies.indicator_validator import (
    IndicatorValidator,
    STRONG_TREND_THRESHOLD,
)

# ── 테스트 인프라 ────────────────────────────────────────────
PASS = "✅"; FAIL = "❌"
results = []

def check(label: str, condition: bool, detail: str = ""):
    icon = PASS if condition else FAIL
    results.append((condition, label, detail))
    print(f"  {icon} {label}" + (f"  [{detail}]" if detail else ""))
    return condition


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ─────────────────────────────────────────────────────────────
# 1. calc_dynamic_weight
# ─────────────────────────────────────────────────────────────
section("1. calc_dynamic_weight() — 동적 비중 상한")

check("기본(score=50, rs=0)    → 10%",
      calc_dynamic_weight(50, 0.0) == WEIGHT_BASE,
      f"got {calc_dynamic_weight(50, 0.0)}")

check("고점수(score=80, rs=0)  → 20%",
      calc_dynamic_weight(80, 0.0) == WEIGHT_HIGH_SCORE,
      f"got {calc_dynamic_weight(80, 0.0)}")

check("고점수(score=85, rs=3)  → 20% (RS 기준 미달)",
      calc_dynamic_weight(85, 3.0) == WEIGHT_HIGH_SCORE,
      f"got {calc_dynamic_weight(85, 3.0)}")

check("엘리트(score=90, rs=5)  → 25%",
      calc_dynamic_weight(90, 5.0) == WEIGHT_ELITE,
      f"got {calc_dynamic_weight(90, 5.0)}")

check("엘리트(score=95, rs=10) → 25%",
      calc_dynamic_weight(95, 10.0) == WEIGHT_ELITE,
      f"got {calc_dynamic_weight(95, 10.0)}")

check("경계: score=89, rs=5   → 20% (점수 미달)",
      calc_dynamic_weight(89, 5.0) == WEIGHT_HIGH_SCORE,
      f"got {calc_dynamic_weight(89, 5.0)}")

check("경계: score=90, rs=4.9 → 20% (RS 미달)",
      calc_dynamic_weight(90, 4.9) == WEIGHT_HIGH_SCORE,
      f"got {calc_dynamic_weight(90, 4.9)}")


# ─────────────────────────────────────────────────────────────
# 2. decide_sell() — 실질수익률 기반 손절
# ─────────────────────────────────────────────────────────────
section("2. decide_sell() — 실질수익률 기반 손절")

engine = TradeDecisionEngine()

# avg_price = 수수료 포함 주당 취득원가
avg_price = 10_000 * (1 + 0.00015)   # 매수가 10,000원 + 수수료

# 손절 트리거 주가 역산: 실질수익률 = -10%
stop_trigger = price_for_net_pct_from_cost(avg_price, STOP_LOSS_PCT)
# 해당 가격에서 실질수익률 확인
verify_pct = net_profit_pct_from_cost(avg_price, stop_trigger)
check("손절 트리거 실질수익률 = -10.00%",
      abs(verify_pct - STOP_LOSS_PCT) < 0.01,
      f"verify_pct={verify_pct:.4f}%")

# 손절 가격에서 decide_sell → SELL 반환
pos_stop = {
    "code": "A005930", "name": "삼성전자",
    "avg_price": avg_price,
    "highest_price": avg_price * 1.01,
    "qty": 100,
}
sr_stop = {"cur_price": stop_trigger, "grade": "NORMAL",
           "total_score": 75, "price_ma20": stop_trigger * 1.02}
d_stop = engine.decide_sell(pos_stop, sr_stop)
check("손절 트리거 도달 → action=SELL",
      d_stop["action"] == "SELL",
      f"action={d_stop['action']}")
check("손절 sell_type = STOP_LOSS",
      d_stop.get("sell_type") == "STOP_LOSS",
      f"sell_type={d_stop.get('sell_type')}")
check("손절 is_forced = True",
      d_stop.get("is_forced") == True,
      f"is_forced={d_stop.get('is_forced')}")

# 손절 이전(-5% net)에서는 HOLD
above_stop = price_for_net_pct_from_cost(avg_price, -5.0)
sr_above = {"cur_price": above_stop, "grade": "NORMAL",
            "total_score": 75, "price_ma20": above_stop * 1.02}
d_above = engine.decide_sell(pos_stop, sr_above)
check("손절 이전(-5%) → HOLD",
      d_above["action"] == "HOLD",
      f"action={d_above['action']}")


# ─────────────────────────────────────────────────────────────
# 3. decide_sell() — 트레일링 스탑 활성화 (실질 +5% 기준)
# ─────────────────────────────────────────────────────────────
section("3. decide_sell() — 트레일링 스탑")

# 트레일링 활성화 가격 = 실질수익률 +5% 역산
trailing_activate = price_for_net_pct_from_cost(avg_price, TRAILING_ACTIVATE_NET_PCT)
verify_trail = net_profit_pct_from_cost(avg_price, trailing_activate)
check(f"트레일링 활성화 가격 실질수익률 = +{TRAILING_ACTIVATE_NET_PCT}%",
      abs(verify_trail - TRAILING_ACTIVATE_NET_PCT) < 0.01,
      f"verify={verify_trail:.4f}%")

# highest_price = 활성화 가격 이상이어야 트레일링 동작
# cur_price = 고점 대비 -12% (TRAILING_STOP_PCT)
high = trailing_activate * 1.02   # 활성화 이후 조금 더 오름
cur_trail = high * (1 + TRAILING_STOP_PCT / 100)  # 고점 -12%

pos_trail = {
    "code": "A005930", "name": "삼성전자",
    "avg_price": avg_price,
    "highest_price": high,
    "qty": 100,
}
sr_trail = {"cur_price": cur_trail, "grade": "NORMAL",
            "total_score": 80, "price_ma20": avg_price}
d_trail = engine.decide_sell(pos_trail, sr_trail)
check("고점 -12% + 트레일링 활성화 → action=SELL",
      d_trail["action"] == "SELL",
      f"action={d_trail['action']}")
check("트레일링 sell_type = TRAILING_STOP",
      d_trail.get("sell_type") == "TRAILING_STOP",
      f"sell_type={d_trail.get('sell_type')}")
check("트레일링 is_forced = True",
      d_trail.get("is_forced") == True)

# 활성화 미달(highest_price < activate_price)이면 트레일링 미작동
pos_no_trail = {
    "code": "A005930", "name": "삼성전자",
    "avg_price": avg_price,
    "highest_price": avg_price * 1.01,   # 활성화 전
    "qty": 100,
}
sr_no_trail = {
    "cur_price": avg_price * 1.01 * (1 + TRAILING_STOP_PCT / 100),
    "grade": "NORMAL", "total_score": 80, "price_ma20": avg_price,
}
d_no_trail = engine.decide_sell(pos_no_trail, sr_no_trail)
check("트레일링 활성화 미달 → TRAILING_STOP 미발동",
      d_no_trail.get("sell_type") != "TRAILING_STOP",
      f"sell_type={d_no_trail.get('sell_type')}")


# ─────────────────────────────────────────────────────────────
# 4. decide_add_buy() — 실질수익률 기반 추가매수
# ─────────────────────────────────────────────────────────────
section("4. decide_add_buy() — 실질수익률 기반 추가매수 구간")

# 추가매수 +10% 실질수익률 달성 가격
add10_price = price_for_net_pct_from_cost(avg_price, 10.0)
verify_add10 = net_profit_pct_from_cost(avg_price, add10_price)
check("ADD_BUY_LEVELS[0]=+10% 도달 가격 실질수익률 = +10%",
      abs(verify_add10 - 10.0) < 0.01,
      f"net_pct={verify_add10:.4f}%")

pos_add = {
    "code": "A005930", "name": "삼성전자",
    "avg_price": avg_price,
    "qty": 100,
    "added_levels": [],
}
sr_add = {
    "cur_price": add10_price * 1.001,   # +10% 약간 초과
    "total_score": 85, "grade": "STRONG",
    "rs_value": 3.0,
}
account_add = {"cash": 5_000_000, "total_assets": 30_000_000,
               "daily_loss_pct": 0.0}

d_add = engine.decide_add_buy(pos_add, sr_add, account_add)
check("실질+10% 도달 → ADD_BUY",
      d_add["action"] == "ADD_BUY",
      f"action={d_add['action']}")
check("추가매수 level=10",
      d_add.get("add_level") == 10,
      f"add_level={d_add.get('add_level')}")
check("반환값에 net_pct 포함",
      "net_pct" in d_add,
      f"keys={list(d_add.keys())}")

# 손실 중 추가매수 금지
sr_loss = {"cur_price": avg_price * 0.99, "total_score": 85,
           "grade": "NORMAL", "rs_value": 0.0}
d_loss_add = engine.decide_add_buy(pos_add, sr_loss, account_add)
check("손실 중 추가매수 → SKIP",
      d_loss_add["action"] == "SKIP",
      f"action={d_loss_add['action']}")

# 이미 추가한 구간은 스킵
pos_added = dict(pos_add)
pos_added["added_levels"] = [10, 20]
add35_price = price_for_net_pct_from_cost(avg_price, 35.0)
sr_add35 = {"cur_price": add35_price * 1.001, "total_score": 85,
            "grade": "STRONG", "rs_value": 3.0}
d_add35 = engine.decide_add_buy(pos_added, sr_add35, account_add)
check("10·20 완료, +35% 도달 → ADD_BUY level=35",
      d_add35["action"] == "ADD_BUY" and d_add35.get("add_level") == 35,
      f"action={d_add35['action']} level={d_add35.get('add_level')}")


# ─────────────────────────────────────────────────────────────
# 5. decide_buy() — 동적 비중 적용
# ─────────────────────────────────────────────────────────────
section("5. decide_buy() — 동적 비중 적용")

base_account = {
    "cash": 50_000_000, "total_assets": 100_000_000,
    "positions": [], "daily_loss_pct": 0.0,
    "total_loss_pct": 0.0, "sector_exposure": {},
}

# 기본 종목 (score=60) → 최대 10% = 10M
sr_basic = {"code": "A000660", "name": "SK하이닉스",
            "total_score": 60, "rs_value": 1.0, "grade": "NORMAL",
            "buy_eligible": True, "buy_reason": "", "cur_price": 100_000,
            "sector": "반도체"}
d_basic = engine.decide_buy(sr_basic, base_account)
check("기본 종목 → BUY 반환",
      d_basic["action"] == "BUY", f"action={d_basic['action']}")
check("기본 종목 max_weight_pct = 10",
      d_basic.get("max_weight_pct") == 10.0,
      f"max_weight_pct={d_basic.get('max_weight_pct')}")
check("기본 종목 투자금 ≤ 총자산 10%",
      d_basic.get("total_cost", 0) <= 100_000_000 * 0.10 * 1.001,
      f"total_cost={d_basic.get('total_cost', 0):,.0f}")

# 고점수 종목 (score=82) → 최대 20%
sr_high = dict(sr_basic)
sr_high.update({"total_score": 82, "rs_value": 2.0, "code": "A035720",
                "name": "카카오"})
d_high = engine.decide_buy(sr_high, base_account)
check("고점수 종목(82점) max_weight_pct = 20",
      d_high.get("max_weight_pct") == 20.0,
      f"max_weight_pct={d_high.get('max_weight_pct')}")

# 엘리트 종목 (score=92, rs=+6) → 최대 25%
sr_elite = dict(sr_basic)
sr_elite.update({"total_score": 92, "rs_value": 6.0, "code": "A051910",
                 "name": "LG화학"})
d_elite = engine.decide_buy(sr_elite, base_account)
check("엘리트 종목(92점, RS+6%) max_weight_pct = 25",
      d_elite.get("max_weight_pct") == 25.0,
      f"max_weight_pct={d_elite.get('max_weight_pct')}")


# ─────────────────────────────────────────────────────────────
# 6. prioritize_reallocation() — 강도 점수 정렬 + 금액 배분
# ─────────────────────────────────────────────────────────────
section("6. prioritize_reallocation() — 손절금 재배분")

# avg_price 계산: 10000원 매수 + 수수료
def make_pos(code, score, rs, ts, cur_price, buy_price=10000):
    avg = calc_buy_cost(buy_price, 1).total_cost
    return {
        "code": code, "name": f"종목{code}",
        "avg_price": avg, "cur_price": cur_price,
        "total_score": score, "rs_value": rs,
        "trend_score": ts, "added_levels": [], "qty": 100,
    }

scored = [
    make_pos("A", 85, 5.0, 70, 11_500),  # 수익 중, 강함
    make_pos("B", 92, 8.0, 80, 12_000),  # 수익 중, 더 강함
    make_pos("C", 78, 2.0, 50, 10_500),  # 수익 중, 보통
    make_pos("D", 60, 1.0, 30, 10_200),  # 점수 미달
    make_pos("E", 80, 4.0, 65,  9_800),  # 손실 중
]

recycled = 2_000_000
realloc = engine.prioritize_reallocation(scored, recycled)

check("재배분 후보 목록 반환",
      len(realloc) > 0,
      f"len={len(realloc)}")
check("최상위 종목 = B (가장 강함)",
      realloc[0]["code"] == "B" if realloc else False,
      f"top={realloc[0]['code'] if realloc else 'N/A'}")
check("D(점수60) 재배분 대상 제외",
      all(r["code"] != "D" for r in realloc),
      f"codes={[r['code'] for r in realloc]}")
check("E(손실중) 재배분 대상 제외",
      all(r["code"] != "E" for r in realloc),
      f"codes={[r['code'] for r in realloc]}")

# 배분 금액 합이 recycled_cash 와 같아야 함
total_alloc = sum(r["alloc_amount"] for r in realloc)
check("배분 금액 합 ≈ 회수금",
      abs(total_alloc - recycled) < 1.0,
      f"total_alloc={total_alloc:,.0f} recycled={recycled:,.0f}")

# 순위 내림차순 (rank 1 < rank 2 ...)
if len(realloc) >= 2:
    check("배분 비율: rank1 ≥ rank2",
          realloc[0]["alloc_ratio"] >= realloc[1]["alloc_ratio"],
          f"rank1={realloc[0]['alloc_ratio']:.3f} rank2={realloc[1]['alloc_ratio']:.3f}")


# ─────────────────────────────────────────────────────────────
# 7. RiskGuard.check_buy() — 동적 비중 상한
# ─────────────────────────────────────────────────────────────
section("7. RiskGuard.check_buy() — 동적 비중 상한")

rg = RiskGuard()
account_rg = {
    "cash": 50_000_000, "total_assets": 100_000_000,
    "daily_pnl_pct": 0.0, "total_pnl_pct": 0.0,
    "positions": [],
}

# 기본: 10% 상한 → 10M 이하 통과
ok, reason = rg.check_buy("A000660", "SK하이닉스", "반도체",
                            9_999_000, account_rg,
                            total_score=60, rs_value=1.0)
check("기본 종목 9.99% 투자 → 통과",
      ok, reason)

# 기본: 10% 초과 → 실패
ok2, r2 = rg.check_buy("A000660", "SK하이닉스", "반도체",
                         10_100_000, account_rg,
                         total_score=60, rs_value=1.0)
check("기본 종목 10.1% 투자 → 비중초과",
      not ok2, r2)

# 고점수(82점): 20% 상한 → 20M 이하 통과
ok3, r3 = rg.check_buy("A035720", "카카오", "IT",
                         19_999_000, account_rg,
                         total_score=82, rs_value=2.0)
check("고점수(82점) 19.99% 투자 → 통과",
      ok3, r3)

# 고점수 20% 초과 → 실패
ok4, r4 = rg.check_buy("A035720", "카카오", "IT",
                         20_100_000, account_rg,
                         total_score=82, rs_value=2.0)
check("고점수(82점) 20.1% 투자 → 비중초과",
      not ok4, r4)

# 엘리트(92점, RS+6): 25% 상한 → 25M 이하 통과
ok5, r5 = rg.check_buy("A051910", "LG화학", "화학",
                         24_999_000, account_rg,
                         total_score=92, rs_value=6.0)
check("엘리트(92점, RS+6%) 24.99% 투자 → 통과",
      ok5, r5)

# 업종 한도는 엘리트에도 적용
account_with_sector = dict(account_rg)
account_with_sector["positions"] = [
    {"sector": "화학", "cur_value": 24_000_000}
]
ok6, r6 = rg.check_buy("A051910", "LG화학", "화학",
                         5_000_000, account_with_sector,
                         total_score=92, rs_value=6.0)
check("엘리트 종목도 업종 25% 한도 적용",
      not ok6, r6)

# 일손실 한도 초과 → 실패
account_daily = dict(account_rg)
account_daily["daily_pnl_pct"] = DAILY_LOSS_LIMIT
ok7, r7 = rg.check_buy("X", "X종목", "기타", 1_000_000,
                         account_daily, 90, 5.0)
check("일손실 한도 초과 → 매수 금지",
      not ok7, r7)


# ─────────────────────────────────────────────────────────────
# 8. RiskGuard.max_buy_amount() — 동적 비중 반영
# ─────────────────────────────────────────────────────────────
section("8. RiskGuard.max_buy_amount() — 동적 비중")

amt_base  = rg.max_buy_amount("X", "기타", account_rg, 60, 1.0)
amt_high  = rg.max_buy_amount("X", "기타", account_rg, 82, 2.0)
amt_elite = rg.max_buy_amount("X", "기타", account_rg, 92, 6.0)

check("기본 최대투자 = 10M",
      abs(amt_base - 10_000_000) < 1,
      f"amt={amt_base:,.0f}")
check("고점수 최대투자 = 20M",
      abs(amt_high - 20_000_000) < 1,
      f"amt={amt_high:,.0f}")
check("엘리트 최대투자 = 25M",
      abs(amt_elite - 25_000_000) < 1,
      f"amt={amt_elite:,.0f}")


# ─────────────────────────────────────────────────────────────
# 9. IndicatorValidator.validate() — trend_score
# ─────────────────────────────────────────────────────────────
section("9. IndicatorValidator.validate() — trend_score 포함")

iv_validator = IndicatorValidator()

def make_candles(n=200, trend="up"):
    """더미 캔들 데이터 생성"""
    np.random.seed(42)
    prices = [10000.0]
    for i in range(n - 1):
        if trend == "up":
            chg = np.random.normal(0.003, 0.01)  # 강한 상승
        elif trend == "down":
            chg = np.random.normal(-0.003, 0.01) # 하락
        else:
            chg = np.random.normal(0.0, 0.01)    # 횡보
        prices.append(max(100, prices[-1] * (1 + chg)))

    candles = []
    for i, p in enumerate(prices):
        high  = p * (1 + abs(np.random.normal(0, 0.005)))
        low   = p * (1 - abs(np.random.normal(0, 0.005)))
        vol   = int(np.random.uniform(100_000, 500_000))
        candles.append({
            "close": round(p, 0), "open": round(p * 0.999, 0),
            "high": round(high, 0), "low": round(low, 0),
            "volume": vol
        })
    return candles

candles_up   = make_candles(200, "up")
candles_down = make_candles(200, "down")

iv_up   = iv_validator.validate(candles_up)
iv_down = iv_validator.validate(candles_down)

check("validate() 결과에 trend_score 포함",
      "trend_score" in iv_up,
      f"keys={list(iv_up.keys())}")
check("validate() 결과에 strong_trend 포함",
      "strong_trend" in iv_up,
      f"keys={list(iv_up.keys())}")
check("validate() 결과에 trend_detail 포함",
      "trend_detail" in iv_up,
      f"keys={list(iv_up.keys())}")
check("trend_score 범위 0~100",
      0 <= iv_up["trend_score"] <= 100,
      f"trend_score={iv_up['trend_score']}")
check("강한 상승 추세 → trend_score > 하락 추세",
      iv_up["trend_score"] > iv_down["trend_score"],
      f"up={iv_up['trend_score']:.1f} down={iv_down['trend_score']:.1f}")
check("trend_detail에 A~D 키 포함",
      all(k in iv_up["trend_detail"]
          for k in ["A_RS", "B_ADX", "C_Momentum", "D_HighProximity"]),
      f"keys={list(iv_up['trend_detail'].keys())}")
check("strong_trend = (trend_score ≥ 60)",
      iv_up["strong_trend"] == (iv_up["trend_score"] >= STRONG_TREND_THRESHOLD),
      f"score={iv_up['trend_score']:.1f} strong={iv_up['strong_trend']}")


# ─────────────────────────────────────────────────────────────
# 10. 복리수익률 일관성 — 손절 경계값 일치
# ─────────────────────────────────────────────────────────────
section("10. 복리수익률 일관성 검증")

for buy_price in [5_000, 10_000, 50_000, 150_000]:
    avg = calc_buy_cost(buy_price, 1).total_cost   # 수수료 포함 취득원가

    # 손절 경계
    stop_p  = price_for_net_pct_from_cost(avg, STOP_LOSS_PCT)
    stop_v  = net_profit_pct_from_cost(avg, stop_p)
    # 손익분기
    be_p    = price_for_net_pct_from_cost(avg, 0.0)
    be_v    = net_profit_pct_from_cost(avg, be_p)
    # 트레일링 활성화
    tr_p    = price_for_net_pct_from_cost(avg, TRAILING_ACTIVATE_NET_PCT)
    tr_v    = net_profit_pct_from_cost(avg, tr_p)
    # 추가매수 +10%
    add10_p = price_for_net_pct_from_cost(avg, 10.0)
    add10_v = net_profit_pct_from_cost(avg, add10_p)

    check(f"buy={buy_price:,} 손절 역산 정확도 ≤ 0.001%",
          abs(stop_v - STOP_LOSS_PCT) < 0.001,
          f"verify={stop_v:.5f}%")
    check(f"buy={buy_price:,} 손익분기 역산 정확도 ≤ 0.001%",
          abs(be_v) < 0.001,
          f"verify={be_v:.5f}%")
    check(f"buy={buy_price:,} 트레일링 활성화 역산 정확도 ≤ 0.001%",
          abs(tr_v - TRAILING_ACTIVATE_NET_PCT) < 0.001,
          f"verify={tr_v:.5f}%")
    check(f"buy={buy_price:,} 추가매수+10% 역산 정확도 ≤ 0.001%",
          abs(add10_v - 10.0) < 0.001,
          f"verify={add10_v:.5f}%")


# ─────────────────────────────────────────────────────────────
# 결과 요약
# ─────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
passed = sum(1 for r in results if r[0])
total  = len(results)
failed = [(r[1], r[2]) for r in results if not r[0]]

print(f"\n  통합 테스트 결과: {passed}/{total} 통과")
if failed:
    print(f"\n  실패 목록:")
    for label, detail in failed:
        print(f"    {FAIL} {label}  [{detail}]")
else:
    print(f"\n  🎉 전체 통과!")

print(f"{'='*60}\n")

sys.exit(0 if not failed else 1)
