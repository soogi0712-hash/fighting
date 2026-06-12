"""
매수/매도/손절/추가매수 결정 엔진
===================================

★ 장기 복리수익률 극대화 설계 철학 ★
  1. 계좌 생존     — 손절은 즉시·무조건 실행 (실질수익률 기준)
  2. 손실 제한     — 약한 종목 빠르게 제거, 손절금 즉시 재배분
  3. 강한 종목 집중 — AI 점수·RS·추세강도가 높은 종목에 자본 집중
  4. 복리 성장     — 수익 종목 추가매수 구간 실질수익률 기준으로 판단
  5. 수익률 극대화  — 추세가 유지되는 한 홀드. MA20 이탈·트레일링 때만 청산

★ 핵심 규칙 ★
  - 모든 % 판단 → 실질수익률 (수수료·세금 차감 후)
  - avg_price     → 수수료 포함 주당 취득원가 (total_cost / qty)
  - stop-loss     → net_profit_pct_from_cost() ≤ STOP_LOSS_PCT
  - 추가매수 트리거 → net_profit_pct_from_cost() 기준 구간
  - 포지션 한도    → AI 점수·RS에 따라 동적으로 증가 (최대 25%)
  - 손절 후 회수금 → 즉시 최고 강도 종목에 재배분
"""

import os
import json
from datetime import datetime
from utils.logger import get_logger
from screener.transaction_cost import (
    net_profit_pct_from_cost,
    price_for_net_pct_from_cost,
    calc_buy_qty,
    calc_buy_cost,
    BUY_COMMISSION_RATE,
    SELL_COMMISSION_RATE,
    TRANSACTION_TAX_RATE,
)

logger = get_logger("TradeDecision")

# ── 손절·익절 기준 (실질수익률 %) ────────────────────────────
STOP_LOSS_PCT     = -10.0   # 손절: 실질수익률 -10% 이하
TRAILING_STOP_PCT = -12.0   # 트레일링 스탑: 고점 대비 주가 -12%
TRAILING_ACTIVATE_NET_PCT = 5.0  # 트레일링 활성화: 실질수익률 +5% 이상일 때
MA20_EXIT_BUFFER  =  -1.0   # MA20 이탈 허용 버퍼 (%)

# 추가매수 구간 (실질수익률 % 기준)
ADD_BUY_LEVELS    = [10, 20, 35]   # +10% / +20% / +35% 실질수익률
ADD_BUY_RATIO     = 0.33           # 추가매수 시 총자산 대비 비율

# ── 동적 집중도 상한 (AI 점수 기반) ─────────────────────────
# 기본 10%, 고점수(80↑)는 최대 20%, 최고점수(90↑)+강RS는 최대 25%
WEIGHT_BASE       = 10.0    # 기본 종목당 비중 상한 (%)
WEIGHT_HIGH_SCORE = 20.0    # AI ≥ 80점 종목 비중 상한 (%)
WEIGHT_ELITE      = 25.0    # AI ≥ 90점 + RS ≥ +5% 엘리트 종목 비중 상한 (%)
SCORE_HIGH_THRESH = 80.0    # 고점수 기준
SCORE_ELITE_THRESH= 90.0    # 엘리트 기준
RS_ELITE_THRESH   =  5.0    # 엘리트 RS 기준 (%)

# 재배분 우선순위 선정 기준
REALLOC_MIN_SCORE = 75.0    # 재배분 대상 최소 AI 점수
REALLOC_TOP_N     = 5       # 상위 N개 종목에 재배분


def calc_dynamic_weight(total_score: float, rs_value: float) -> float:
    """
    AI 점수 + RS 값으로 동적 종목 비중 상한 계산.

    Args:
        total_score: AIScorer 총점 (0~100)
        rs_value:    상대강도 (%) — 양수=시장 대비 강세

    Returns:
        허용 최대 종목 비중 (%) — WEIGHT_BASE ~ WEIGHT_ELITE
    """
    if total_score >= SCORE_ELITE_THRESH and rs_value >= RS_ELITE_THRESH:
        return WEIGHT_ELITE       # 25%: 최상위 집중
    if total_score >= SCORE_HIGH_THRESH:
        return WEIGHT_HIGH_SCORE  # 20%: 고점수 집중
    return WEIGHT_BASE            # 10%: 기본 분산


class TradeDecisionEngine:
    """
    AI 점수 + 포지션 상태를 결합해 최종 매매 결정 반환.

    포지션 데이터:
        {code, name, entry_price, avg_price,    ← avg_price = 수수료 포함 취득원가
         qty, highest_price,
         added_levels: [10, 20],                ← 이미 추가매수한 실질수익률 구간
         entry_score, entry_date}
    """

    # ══════════════════════════════════════════════════════════
    # 매수 결정
    # ══════════════════════════════════════════════════════════
    def decide_buy(self, score_result: dict, account_info: dict) -> dict:
        """
        신규 매수 결정.

        score_result: AIScorer.score() 반환값
            {code, name, total_score, rs_value, grade, buy_eligible,
             buy_reason, cur_price, sector, trend_score}
        account_info:
            {cash, total_assets, positions: [...],
             daily_loss_pct, total_loss_pct, sector_exposure: {업종: 비중}}
        """
        code  = score_result["code"]
        name  = score_result["name"]
        total = score_result["total_score"]
        rs    = score_result.get("rs_value", 0.0)
        grade = score_result["grade"]

        # AIScorer 적격 여부
        if not score_result.get("buy_eligible"):
            return self._skip(code, name, score_result["buy_reason"])

        cash         = account_info.get("cash", 0)
        total_assets = account_info.get("total_assets", 1)

        # 계좌 생존 규칙 #1: 일일 손실 한도
        if account_info.get("daily_loss_pct", 0) <= -3.0:
            return self._skip(code, name,
                f"일손실한도초과({account_info['daily_loss_pct']:.1f}%)")

        # 계좌 생존 규칙 #2: 전체 계좌 손실 한도
        if account_info.get("total_loss_pct", 0) <= -15.0:
            return self._skip(code, name, "계좌전체손실한도초과")

        # 이미 보유 중 → 추가매수 경로
        existing = next(
            (p for p in account_info.get("positions", []) if p["code"] == code),
            None
        )
        if existing:
            return self._skip(code, name, "이미보유(추가매수경로사용)")

        # 업종 집중도 체크 (sector_exposure는 RiskGuard에서 계산)
        sector     = score_result.get("sector", "기타")
        sector_exp = account_info.get("sector_exposure", {})
        if sector_exp.get(sector, 0) >= 25.0:
            return self._skip(code, name,
                f"업종집중도초과({sector} {sector_exp.get(sector, 0):.0f}%)")

        cur_price = score_result.get("cur_price", 0)
        if not cur_price:
            return self._skip(code, name, "현재가 없음")

        # ★ 동적 종목 비중 상한 (강한 종목 집중 철학)
        max_weight_pct = calc_dynamic_weight(total, rs)
        max_invest     = total_assets * max_weight_pct / 100
        invest_amt     = min(max_invest, cash * 0.90)

        # ★ 수수료 포함 매수 수량 계산
        qty = calc_buy_qty(invest_amt, cur_price, invest_ratio=1.0)
        if qty < 1:
            return self._skip(code, name,
                f"매수수량0 (가용:{invest_amt:,.0f}원/{cur_price:,}원)")

        bc = calc_buy_cost(cur_price, qty)

        return {
            "action":       "BUY",
            "code":         code,
            "name":         name,
            "price":        cur_price,
            "qty":          qty,
            "amount":       bc.buy_amount,      # 순 매수금액 (수수료 전)
            "total_cost":   bc.total_cost,       # 총 취득원가 (수수료 포함)
            "buy_commission": round(bc.commission, 0),
            "grade":        grade,
            "score":        total,
            "rs":           rs,
            "max_weight_pct": max_weight_pct,   # 적용된 비중 상한
            "reason":       (f"AI점수={total:.0f}pts RS={rs:+.1f}% "
                             f"[{grade}] 비중한도={max_weight_pct:.0f}%"),
        }

    # ══════════════════════════════════════════════════════════
    # 추가매수 결정 (+10/+20/+35% 실질수익률 분할)
    # ══════════════════════════════════════════════════════════
    def decide_add_buy(self, position: dict, score_result: dict,
                       account_info: dict) -> dict:
        """
        수익 중인 종목에만 추가매수.

        ★ gain_pct 판단을 simple price % → net_profit_pct_from_cost() 로 교체.
          position["avg_price"] = 수수료 포함 주당 취득원가.

        ADD_BUY_LEVELS = [10, 20, 35] 은 실질수익률(%) 기준.
        """
        code      = position["code"]
        name      = position["name"]
        avg_price = position["avg_price"]   # ★ 수수료 포함 취득원가
        cur_price = score_result.get("cur_price", position.get("cur_price", 0))
        added_lvls= position.get("added_levels", [])

        if not cur_price:
            return self._skip(code, name, "현재가 없음")

        # ★ 실질 수익률 기준 (수수료·세금 차감 후)
        net_pct = net_profit_pct_from_cost(avg_price, cur_price)

        # 손실 중: 추가매수 금지 (약한 종목 추가 투입 방지)
        if net_pct <= 0:
            return self._skip(code, name,
                f"손실중(실질{net_pct:.2f}%) 추가매수불가")

        # 일일 손실 한도 체크
        if account_info.get("daily_loss_pct", 0) <= -3.0:
            return self._skip(code, name, "일손실한도초과")

        # ★ 실질수익률 기준으로 구간 확인
        target_lvl = None
        for lvl in ADD_BUY_LEVELS:
            if lvl not in added_lvls and net_pct >= lvl:
                target_lvl = lvl
                break  # 가장 낮은 미진입 구간 우선

        if target_lvl is None:
            return self._skip(code, name,
                f"추가매수구간미도달(실질{net_pct:.2f}%)")

        # AI 점수 기준 (추가매수는 더 엄격)
        score = score_result.get("total_score", 0)
        if score < 70:
            return self._skip(code, name,
                f"추가매수점수부족({score:.0f}/70)")

        cash         = account_info.get("cash", 0)
        total_assets = account_info.get("total_assets", 1)
        rs           = score_result.get("rs_value", 0.0)

        # ★ 동적 비중 상한 적용 (강한 종목에 더 많이)
        max_weight_pct = calc_dynamic_weight(score, rs)
        invest_amt     = min(total_assets * max_weight_pct / 100 * 0.5,
                             cash * 0.5)   # 추가매수: 상한의 50%

        qty = calc_buy_qty(invest_amt, cur_price, invest_ratio=1.0)
        if qty < 1:
            return self._skip(code, name, "추가매수금액부족")

        bc = calc_buy_cost(cur_price, qty)

        # 트레일링 활성화 가격 역산 (참고용 포함)
        trailing_trigger = price_for_net_pct_from_cost(
            avg_price, TRAILING_ACTIVATE_NET_PCT
        )

        return {
            "action":          "ADD_BUY",
            "code":            code,
            "name":            name,
            "price":           cur_price,
            "qty":             qty,
            "amount":          bc.buy_amount,
            "total_cost":      bc.total_cost,
            "buy_commission":  round(bc.commission, 0),
            "add_level":       target_lvl,
            "net_pct":         round(net_pct, 2),     # ★ 실질수익률
            "score":           score,
            "max_weight_pct":  max_weight_pct,
            "trailing_trigger":round(trailing_trigger, 0),  # 트레일링 활성화 주가
            "reason":          (f"추가매수+{target_lvl}%(실질{net_pct:.2f}%) "
                                f"AI={score:.0f}pts 비중한도={max_weight_pct:.0f}%"),
        }

    # ══════════════════════════════════════════════════════════
    # 매도 / 손절 결정
    # ══════════════════════════════════════════════════════════
    def decide_sell(self, position: dict, score_result: dict,
                    market_info: dict = None) -> dict:
        """
        손절·트레일링스탑·MA20이탈·AI점수급락 중 하나라도 해당하면 SELL.

        ★ 모든 % 판단 → net_profit_pct_from_cost() (실질수익률, 수수료·세금 차감).
          position["avg_price"] = 수수료 포함 주당 취득원가.

        트레일링 활성화 기준 = TRAILING_ACTIVATE_NET_PCT (실질수익률 +5% 이상).
        """
        code          = position["code"]
        name          = position["name"]
        avg_price     = position["avg_price"]       # ★ 수수료 포함 취득원가
        highest_price = position.get("highest_price", avg_price)
        qty           = position["qty"]
        cur_price     = score_result.get("cur_price",
                         position.get("cur_price", avg_price))

        if not cur_price:
            return self._hold(code, name, "현재가없음")

        # ★ 실질 수익률 (수수료·세금 차감 후)
        net_pct   = net_profit_pct_from_cost(avg_price, cur_price)
        trail_pct = ((cur_price - highest_price) / highest_price * 100
                     if highest_price else 0.0)
        ma20      = score_result.get("price_ma20", 0)
        ma20_pct  = (cur_price - ma20) / ma20 * 100 if ma20 else 0.0

        # ── ① 전량 손절: 실질수익률 ≤ -10% ─────────────────
        # 계좌 생존 최우선: 즉시·무조건 실행
        if net_pct <= STOP_LOSS_PCT:
            return {
                "action":    "SELL",
                "code":      code, "name": name,
                "qty":       qty, "price": cur_price,
                "reason":    f"손절(실질{net_pct:.2f}% ≤ {STOP_LOSS_PCT}%)",
                "sell_type": "STOP_LOSS",
                "net_pct":   round(net_pct, 2),  # ★ 실질수익률
                "is_forced": True,               # 지표 우선순위 무시
            }

        # ── ② 트레일링 스탑 ──────────────────────────────────
        # 활성화 조건: 실질수익률 ≥ TRAILING_ACTIVATE_NET_PCT
        activate_price = price_for_net_pct_from_cost(
            avg_price, TRAILING_ACTIVATE_NET_PCT
        )
        trailing_active = (highest_price >= activate_price)
        if trailing_active and trail_pct <= TRAILING_STOP_PCT:
            return {
                "action":    "SELL",
                "code":      code, "name": name,
                "qty":       qty, "price": cur_price,
                "reason":    (f"트레일링스탑(고점대비{trail_pct:.2f}% ≤ "
                              f"{TRAILING_STOP_PCT}%, 실질{net_pct:.2f}%)"),
                "sell_type": "TRAILING_STOP",
                "net_pct":   round(net_pct, 2),
                "is_forced": True,  # 추세 종료 = 강제 매도
            }

        # ── ③ MA20 이탈 (수익권에서 추세 이탈) ──────────────
        # 수익권 보유 + MA20 이탈 → 추세 종료
        if ma20 and net_pct > 0 and ma20_pct <= MA20_EXIT_BUFFER:
            return {
                "action":    "SELL",
                "code":      code, "name": name,
                "qty":       qty, "price": cur_price,
                "reason":    (f"MA20추세이탈(실질{net_pct:.2f}%, "
                              f"MA20괴리{ma20_pct:.2f}%)"),
                "sell_type": "MA20_EXIT",
                "net_pct":   round(net_pct, 2),
                "is_forced": False,  # 지표 확인 후 실행
            }

        # ── ④ AI 점수 급락 (EXCLUDE 등급) ───────────────────
        grade = score_result.get("grade", "")
        score = score_result.get("total_score", 100)
        if grade == "EXCLUDE" and score < 40:
            return {
                "action":    "SELL",
                "code":      code, "name": name,
                "qty":       qty, "price": cur_price,
                "reason":    f"AI점수급락({score:.0f}pts/{grade})",
                "sell_type": "SCORE_DROP",
                "net_pct":   round(net_pct, 2),
                "is_forced": False,
            }

        # 추세 지속 → 홀드
        return self._hold(code, name,
            f"추세유지(실질{net_pct:+.2f}% 고점대비{trail_pct:+.2f}% "
            f"MA20={ma20_pct:+.2f}% 트레일활성={'ON' if trailing_active else 'OFF'})")

    # ══════════════════════════════════════════════════════════
    # 손절 회수금 재배분 우선순위
    # ══════════════════════════════════════════════════════════
    def prioritize_reallocation(self, scored_positions: list[dict],
                                 recycled_cash: float = 0.0) -> list[dict]:
        """
        손절 후 회수된 현금을 강한 종목에 우선 배분.

        ★ 장기 복리수익률 극대화: 손절금을 즉시 최강 종목으로 재투입.

        Args:
            scored_positions: [
                {code, name, avg_price, cur_price,
                 total_score, rs_value, trend_score,
                 added_levels, qty}
            ]
            recycled_cash: 회수된 손절 현금 (원)

        Returns:
            우선순위 배분 목록 [
                {code, name, priority_rank, alloc_ratio,
                 alloc_amount, reason, ...}
            ]
        """
        # ① 수익 중 + 점수 높은 종목 필터
        candidates = [
            p for p in scored_positions
            if (net_profit_pct_from_cost(
                    p.get("avg_price", 0),
                    p.get("cur_price", 0)
                ) > 0)
            and p.get("total_score", 0) >= REALLOC_MIN_SCORE
        ]

        if not candidates:
            logger.info("재배분 대상 없음 (수익+고점수 종목 없음)")
            return []

        # ② 종합 강도 점수로 정렬
        #    = AI 점수 (0~100) × 0.5 + RS × 0.3 + 추세점수 × 0.2
        def strength(p: dict) -> float:
            ai  = p.get("total_score", 0)
            rs  = p.get("rs_value",    0.0)
            tr  = p.get("trend_score", 0)     # IndicatorValidator.trend_score
            # 실질 미실현 수익률도 강도 지표로 포함 (보너스 가중)
            net = net_profit_pct_from_cost(
                p.get("avg_price", 0), p.get("cur_price", 0)
            )
            return ai * 0.5 + rs * 0.3 + tr * 0.2 + min(net * 0.1, 5.0)

        candidates.sort(key=strength, reverse=True)
        top_n = candidates[:REALLOC_TOP_N]

        # ③ 강도 비율로 배분 금액 계산
        total_strength = sum(strength(p) for p in top_n) or 1.0
        result = []
        for rank, p in enumerate(top_n, start=1):
            s      = strength(p)
            ratio  = s / total_strength
            amount = round(recycled_cash * ratio, 0) if recycled_cash else 0.0
            net    = net_profit_pct_from_cost(
                p.get("avg_price", 0), p.get("cur_price", 0)
            )
            result.append({
                "code":          p["code"],
                "name":          p["name"],
                "priority_rank": rank,
                "alloc_ratio":   round(ratio, 4),
                "alloc_amount":  amount,
                "total_score":   p.get("total_score", 0),
                "rs_value":      p.get("rs_value", 0.0),
                "trend_score":   p.get("trend_score", 0),
                "net_pct":       round(net, 2),
                "reason":        (f"강도점수={s:.1f} "
                                  f"AI={p.get('total_score',0):.0f} "
                                  f"RS={p.get('rs_value',0.0):+.1f}% "
                                  f"실질{net:+.2f}%"),
            })

        logger.info(
            f"💡 재배분 대상 {len(result)}종목 "
            f"(회수금={recycled_cash:,.0f}원): "
            + " | ".join(f"{r['name']}({r['alloc_ratio']*100:.0f}%)"
                         for r in result)
        )
        return result

    # ══════════════════════════════════════════════════════════
    # 추세 지속 판단 (홀드 결정 보조)
    # ══════════════════════════════════════════════════════════
    def is_trend_intact(self, position: dict, score_result: dict) -> bool:
        """
        수익 종목 추세가 아직 유효한지 판단.

        추세 유효 조건:
          1. MA20 위에 현재가 있음 (MA20_EXIT_BUFFER 이내)
          2. AI 점수 ≥ 60
          3. grade ≠ EXCLUDE

        Returns:
            True  = 추세 유지 → 홀드
            False = 추세 이탈 → 매도 검토
        """
        avg_price = position.get("avg_price", 0)
        cur_price = score_result.get("cur_price", 0)
        if not cur_price:
            return False

        net_pct  = net_profit_pct_from_cost(avg_price, cur_price)
        ma20     = score_result.get("price_ma20", 0)
        ma20_pct = (cur_price - ma20) / ma20 * 100 if ma20 else 0.0
        score    = score_result.get("total_score", 0)
        grade    = score_result.get("grade", "")

        if ma20 and ma20_pct <= MA20_EXIT_BUFFER:
            return False
        if grade == "EXCLUDE" or score < 60:
            return False
        return True

    # ── 내부 헬퍼 ─────────────────────────────────────────────
    @staticmethod
    def _skip(code, name, reason):
        return {"action": "SKIP", "code": code, "name": name, "reason": reason}

    @staticmethod
    def _hold(code, name, reason):
        return {"action": "HOLD", "code": code, "name": name, "reason": reason}
