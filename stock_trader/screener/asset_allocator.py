"""
자산군 배분 엔진 (Asset Allocator)
=====================================

★ 역할 ★
  1. 시장 국면(BULL/BEAR/LATERAL) 입력 → 자산군별 타겟 비중 계산
  2. 종목 스크리닝 결과 + 자산군 분류 → 매수 우선순위 리스트 생성
  3. 비중 제한 검사 (레버리지 ≤20%, 인버스 ≤40%, 현금 ≥10%)
  4. 포트폴리오 리밸런싱 권고 생성

★ 핵심 규칙 ★
  레버리지 ETF: BULL + AI≥80 + RS>0 일 때만 매수 허용
  인버스 ETF  : BEAR 우선, 비중 ≤40%
  현금        : 항상 ≥10% 유지
  강세장      : 개별주식 우선
  횡보장      : ETF 우선
  약세장      : 인버스 ETF 또는 현금 우선
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dataclasses import dataclass, field
from typing import Optional
from utils.logger import get_logger

from screener.asset_universe import (
    ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ,
    ASSET_ETF_GENERAL, ASSET_ETF_LEVERAGE, ASSET_ETF_INVERSE, ASSET_CASH,
    WEIGHT_LIMITS, MAX_INVEST_RATIO,
    REGIME_PRIORITY, REGIME_TARGET_WEIGHTS,
    classify_asset_type, can_buy_leverage, can_buy_inverse,
    check_weight_limit, priority_score, get_target_weights,
    get_asset_type_label, summarize_portfolio_by_asset,
)

logger = get_logger("AssetAllocator")


# ═══════════════════════════════════════════════════════════════
# 데이터 구조
# ═══════════════════════════════════════════════════════════════

@dataclass
class AllocationContext:
    """
    배분 결정에 필요한 전체 컨텍스트.
    매 스크리닝/매수 결정 시 생성한다.
    """
    regime:        str   = "LATERAL"   # BULL / BEAR / LATERAL
    total_capital: float = 10_000_000  # 총 계좌 자산 (원)
    cash:          float = 10_000_000  # 현금 잔고
    # 현재 보유 포지션 요약 {asset_type: {"value": float, "count": int}}
    position_summary: dict = field(default_factory=dict)

    @property
    def invested(self) -> float:
        return self.total_capital - self.cash

    @property
    def cash_ratio(self) -> float:
        if not self.total_capital:
            return 1.0
        return self.cash / self.total_capital

    @property
    def invest_ratio(self) -> float:
        return 1.0 - self.cash_ratio

    def asset_ratio(self, asset_type: str) -> float:
        """특정 자산군의 현재 비중"""
        if not self.total_capital:
            return 0.0
        val = self.position_summary.get(asset_type, {}).get("value", 0.0)
        return val / self.total_capital


@dataclass
class BuyDecision:
    """
    단일 종목 매수 결정 결과.
    """
    code:           str
    name:           str
    asset_type:     str
    ai_score:       float
    rs_value:       float
    priority_rank:  int     = 0      # 낮을수록 우선 (1위 = 최우선)
    can_buy:        bool    = False
    reason:         str     = ""
    suggested_ratio: float  = 0.0   # 권장 투자비중 (계좌 대비)
    max_amount:     float   = 0.0   # 최대 투자 가능 금액


@dataclass
class AllocationPlan:
    """
    전체 배분 계획 (스크리닝 결과 → 매수 우선순위 + 비중)
    """
    regime:           str
    target_weights:   dict[str, float]    # 자산군별 타겟 비중
    current_weights:  dict[str, float]    # 현재 실제 비중
    rebalance_needed: bool = False        # 리밸런싱 필요 여부
    buy_list:         list[BuyDecision] = field(default_factory=list)
    warnings:         list[str]          = field(default_factory=list)
    summary:          dict               = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# 핵심 엔진
# ═══════════════════════════════════════════════════════════════

class AssetAllocator:
    """
    시장 국면 인식 자산군 동적 배분 엔진.

    사용 흐름:
      ctx = AllocationContext(regime="BULL", total_capital=..., cash=...,
                              position_summary=...)
      plan = allocator.plan(screened_stocks, ctx)
      → plan.buy_list: 우선순위 정렬된 매수 후보
      → plan.warnings: 비중 초과 경고
    """

    # 진입 허용 최소 AI 점수 (자산군별)
    MIN_SCORE: dict[str, float] = {
        ASSET_STOCK_KOSPI:  75.0,
        ASSET_STOCK_KOSDAQ: 75.0,
        ASSET_ETF_GENERAL:  60.0,   # ETF는 스코어 기준 완화
        ASSET_ETF_LEVERAGE: 80.0,   # 레버리지는 엄격
        ASSET_ETF_INVERSE:  55.0,   # 인버스는 국면 판단이 핵심
    }

    # 자산군별 단일 종목 최대 비중 (포트폴리오 집중도 제한)
    MAX_SINGLE_RATIO: dict[str, float] = {
        ASSET_STOCK_KOSPI:  0.15,   # 개별주식 1종목 최대 15%
        ASSET_STOCK_KOSDAQ: 0.12,   # 코스닥은 더 제한
        ASSET_ETF_GENERAL:  0.20,   # 일반 ETF 20%
        ASSET_ETF_LEVERAGE: 0.10,   # 레버리지 단일 10% (총한도 20%)
        ASSET_ETF_INVERSE:  0.20,   # 인버스 단일 20% (총한도 40%)
    }

    def plan(
        self,
        screened: list[dict],   # AIScorer.score() 결과 리스트
        ctx: AllocationContext,
    ) -> AllocationPlan:
        """
        스크리닝된 종목 + 현재 포트폴리오 컨텍스트 →
        자산군별 배분 계획 + 매수 우선순위 리스트 반환.

        screened 각 항목 필수 키:
          code, name, total_score, rs_value,
          asset_type (없으면 classify_asset_type 으로 추론),
          market (KOSPI/KOSDAQ/ETF)
        """
        regime          = ctx.regime
        target_weights  = get_target_weights(regime)
        current_weights = self._current_weights(ctx)
        warnings        = self._check_limits(ctx, current_weights)

        buy_decisions = []
        for item in screened:
            dec = self._evaluate(item, ctx, current_weights)
            buy_decisions.append(dec)

        # 우선순위 정렬:
        #   1. 매수 가능 여부 (can_buy=True 우선)
        #   2. 자산군 우선순위 점수 (현재 국면 기준)
        #   3. AI 점수 내림차순
        buy_decisions.sort(key=lambda d: (
            0 if d.can_buy else 1,
            -priority_score(d.asset_type, regime),
            -d.ai_score,
        ))
        for i, d in enumerate(buy_decisions, 1):
            d.priority_rank = i

        plan = AllocationPlan(
            regime           = regime,
            target_weights   = target_weights,
            current_weights  = current_weights,
            rebalance_needed = self._needs_rebalance(
                target_weights, current_weights
            ),
            buy_list  = buy_decisions,
            warnings  = warnings,
            summary   = self._build_summary(ctx, target_weights,
                                             current_weights, buy_decisions),
        )
        logger.info(
            f"[AssetAllocator] {regime} 배분 계획: "
            f"매수가능={sum(1 for d in buy_decisions if d.can_buy)}/"
            f"{len(buy_decisions)} 종목 "
            f"경고={len(warnings)}건"
        )
        return plan

    # ── 단일 종목 평가 ──────────────────────────────────────

    def _evaluate(
        self,
        item:    dict,
        ctx:     AllocationContext,
        cur_wt:  dict[str, float],
    ) -> BuyDecision:
        """
        종목 1개에 대한 매수 가능 여부 + 권장 비중 결정.
        """
        code       = item.get("code", "")
        name       = item.get("name", "")
        ai_score   = float(item.get("total_score", item.get("score", 0)))
        rs_value   = float(item.get("rs_value", 0))
        market     = item.get("market", "KOSPI")

        # 자산군 분류
        asset_type = item.get("asset_type") or classify_asset_type(
            code, name, market
        )

        can_buy, reason = self._can_buy(
            asset_type, ai_score, rs_value, ctx, cur_wt
        )

        # 권장 투자 비중 및 금액
        suggested_ratio = self._suggested_ratio(asset_type, ctx)
        max_amount      = ctx.total_capital * suggested_ratio

        return BuyDecision(
            code            = code,
            name            = name,
            asset_type      = asset_type,
            ai_score        = ai_score,
            rs_value        = rs_value,
            can_buy         = can_buy,
            reason          = reason,
            suggested_ratio = round(suggested_ratio, 4),
            max_amount      = round(max_amount, 0),
        )

    def _can_buy(
        self,
        asset_type: str,
        ai_score:   float,
        rs_value:   float,
        ctx:        AllocationContext,
        cur_wt:     dict[str, float],
    ) -> tuple[bool, str]:
        """
        매수 가능 여부 순차 검사.
        """
        regime = ctx.regime

        # 1. 현금 10% 유지 가능한지 (투자 여력)
        if ctx.cash_ratio < WEIGHT_LIMITS[ASSET_CASH] + 0.01:
            return False, f"현금 부족 ({ctx.cash_ratio*100:.1f}% < 최소 {WEIGHT_LIMITS[ASSET_CASH]*100:.0f}%+1%)"

        # 2. 약세장에서 개별주식·레버리지 제한
        if regime == "BEAR":
            if asset_type in (ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ):
                return False, "약세장: 개별주식 신규매수 제한"
            if asset_type == ASSET_ETF_LEVERAGE:
                return False, "약세장: 레버리지 ETF 완전 제한"

        # 3. 레버리지 ETF 특별 조건
        if asset_type == ASSET_ETF_LEVERAGE:
            ok, msg = can_buy_leverage(
                regime    = regime,
                ai_score  = ai_score,
                rs_value  = rs_value,
                lever_ratio = cur_wt.get(ASSET_ETF_LEVERAGE, 0.0),
            )
            if not ok:
                return False, f"레버리지조건미달: {msg}"

        # 4. 인버스 ETF 비중 한도
        if asset_type == ASSET_ETF_INVERSE:
            ok, msg = can_buy_inverse(
                regime    = regime,
                inv_ratio = cur_wt.get(ASSET_ETF_INVERSE, 0.0),
            )
            if not ok:
                return False, f"인버스한도초과: {msg}"

        # 5. 자산군별 총 비중 한도
        atype_limit = WEIGHT_LIMITS.get(asset_type)
        if atype_limit is not None:
            ok, msg = check_weight_limit(
                asset_type    = asset_type,
                current_ratio = cur_wt.get(asset_type, 0.0),
            )
            if not ok:
                return False, f"비중한도초과: {msg}"

        # 6. 최소 AI 점수
        min_score = self.MIN_SCORE.get(asset_type, 70.0)
        if ai_score < min_score:
            return False, f"AI점수미달 ({ai_score:.0f} < {min_score:.0f})"

        # 7. 개별주식 RS 조건
        if asset_type in (ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ):
            if rs_value <= 0:
                return False, f"RS비양수 ({rs_value:+.1f}%)"

        # 8. 횡보장에서 레버리지 ETF
        if regime == "LATERAL" and asset_type == ASSET_ETF_LEVERAGE:
            return False, "횡보장: 레버리지 ETF 비활성"

        return True, "매수 가능"

    # ── 권장 비중 계산 ───────────────────────────────────────

    def _suggested_ratio(
        self,
        asset_type: str,
        ctx:        AllocationContext,
    ) -> float:
        """
        종목 1개에 대한 권장 투자 비중 (계좌 대비).
        - 자산군 타겟 비중 / 해당 자산군 내 예상 종목 수
        - 단일 종목 최대 비중 초과 금지
        """
        target   = get_target_weights(ctx.regime)
        tgt_wt   = target.get(asset_type, 0.05)
        max_sngl = self.MAX_SINGLE_RATIO.get(asset_type, 0.10)

        # 현재 보유 수 기반으로 추가 여력 계산
        cur_cnt  = ctx.position_summary.get(asset_type, {}).get("count", 0)
        cur_val  = ctx.position_summary.get(asset_type, {}).get("value", 0.0)
        cur_wt   = cur_val / ctx.total_capital if ctx.total_capital else 0.0
        remaining = max(0.0, tgt_wt - cur_wt)

        # 단일 종목 비중 = min(잔여 비중, 최대 단일 비중)
        single = min(remaining, max_sngl)

        # 현금 안전마진 보장
        if ctx.cash_ratio - single < WEIGHT_LIMITS[ASSET_CASH]:
            single = max(0.0, ctx.cash_ratio - WEIGHT_LIMITS[ASSET_CASH])

        return round(single, 4)

    # ── 현재 비중 계산 ───────────────────────────────────────

    def _current_weights(self, ctx: AllocationContext) -> dict[str, float]:
        """
        AllocationContext.position_summary 기반으로
        현재 자산군별 비중 딕셔너리 반환.
        """
        weights: dict[str, float] = {}
        if not ctx.total_capital:
            return weights
        for atype, info in ctx.position_summary.items():
            weights[atype] = round(
                info.get("value", 0.0) / ctx.total_capital, 4
            )
        # 현금 비중
        weights[ASSET_CASH] = round(ctx.cash_ratio, 4)
        return weights

    # ── 리밸런싱 필요 여부 ───────────────────────────────────

    def _needs_rebalance(
        self,
        target:  dict[str, float],
        current: dict[str, float],
        threshold: float = 0.05,   # 5%p 이상 이탈 시 리밸런싱
    ) -> bool:
        for atype, tgt in target.items():
            cur = current.get(atype, 0.0)
            if abs(cur - tgt) >= threshold:
                return True
        return False

    # ── 비중 한도 경고 ───────────────────────────────────────

    def _check_limits(
        self,
        ctx:     AllocationContext,
        cur_wt:  dict[str, float],
    ) -> list[str]:
        warnings = []

        # 현금 최소 비중
        if ctx.cash_ratio < WEIGHT_LIMITS[ASSET_CASH]:
            warnings.append(
                f"⚠️ 현금 비중 부족: {ctx.cash_ratio*100:.1f}% "
                f"(최소 {WEIGHT_LIMITS[ASSET_CASH]*100:.0f}% 필요)"
            )

        # 레버리지 한도
        lev = cur_wt.get(ASSET_ETF_LEVERAGE, 0.0)
        if lev > WEIGHT_LIMITS[ASSET_ETF_LEVERAGE]:
            warnings.append(
                f"⚠️ 레버리지 ETF 초과: {lev*100:.1f}% "
                f"(한도 {WEIGHT_LIMITS[ASSET_ETF_LEVERAGE]*100:.0f}%)"
            )

        # 인버스 한도
        inv = cur_wt.get(ASSET_ETF_INVERSE, 0.0)
        if inv > WEIGHT_LIMITS[ASSET_ETF_INVERSE]:
            warnings.append(
                f"⚠️ 인버스 ETF 초과: {inv*100:.1f}% "
                f"(한도 {WEIGHT_LIMITS[ASSET_ETF_INVERSE]*100:.0f}%)"
            )

        # 약세장인데 개별주식 20% 초과
        if ctx.regime == "BEAR":
            stocks = (
                cur_wt.get(ASSET_STOCK_KOSPI,  0.0)
                + cur_wt.get(ASSET_STOCK_KOSDAQ, 0.0)
            )
            if stocks > 0.20:
                warnings.append(
                    f"⚠️ 약세장 개별주식 비중 높음: {stocks*100:.1f}% "
                    f"(축소 권장)"
                )

        return warnings

    # ── 요약 생성 ────────────────────────────────────────────

    def _build_summary(
        self,
        ctx:             AllocationContext,
        target_weights:  dict[str, float],
        current_weights: dict[str, float],
        buy_decisions:   list[BuyDecision],
    ) -> dict:
        """
        AllocationPlan.summary 용 딕셔너리 생성.
        """
        regime = ctx.regime
        priority_order = REGIME_PRIORITY.get(regime, [])

        buyable = [d for d in buy_decisions if d.can_buy]
        asset_breakdown = {}
        for atype in [ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ,
                      ASSET_ETF_GENERAL, ASSET_ETF_LEVERAGE,
                      ASSET_ETF_INVERSE]:
            asset_breakdown[atype] = {
                "label":   get_asset_type_label(atype),
                "target":  round(target_weights.get(atype, 0.0) * 100, 1),
                "current": round(current_weights.get(atype, 0.0) * 100, 1),
                "buyable_count": sum(
                    1 for d in buyable if d.asset_type == atype
                ),
                "priority": priority_order.index(atype) + 1
                            if atype in priority_order else 99,
            }

        return {
            "regime":           regime,
            "total_capital":    ctx.total_capital,
            "cash":             ctx.cash,
            "cash_ratio_pct":   round(ctx.cash_ratio * 100, 1),
            "invest_ratio_pct": round(ctx.invest_ratio * 100, 1),
            "total_buyable":    len(buyable),
            "total_screened":   len(buy_decisions),
            "asset_breakdown":  asset_breakdown,
            "priority_order":   [get_asset_type_label(a) for a in priority_order
                                  if a != ASSET_CASH],
        }


# ═══════════════════════════════════════════════════════════════
# 리밸런싱 권고 생성기
# ═══════════════════════════════════════════════════════════════

class RebalanceAdvisor:
    """
    현재 포트폴리오와 타겟 비중의 차이를 분석하여
    구체적인 리밸런싱 액션을 권고한다.
    """

    def advise(
        self,
        ctx:     AllocationContext,
        plan:    AllocationPlan,
    ) -> list[dict]:
        """
        Returns list of:
        {
          asset_type, label, action (BUY/SELL/HOLD),
          current_pct, target_pct, diff_pct, amount, reason
        }
        """
        advice = []
        regime = ctx.regime
        target = get_target_weights(regime)

        for atype in [ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ,
                      ASSET_ETF_GENERAL, ASSET_ETF_LEVERAGE,
                      ASSET_ETF_INVERSE]:
            cur_pct = plan.current_weights.get(atype, 0.0) * 100
            tgt_pct = target.get(atype, 0.0) * 100
            diff    = tgt_pct - cur_pct

            if diff > 2:
                action = "BUY"
                reason = f"타겟({tgt_pct:.0f}%) 대비 부족 ({cur_pct:.0f}%)"
            elif diff < -2:
                action = "SELL"
                reason = f"타겟({tgt_pct:.0f}%) 대비 초과 ({cur_pct:.0f}%)"
            else:
                action = "HOLD"
                reason = "타겟 범위 이내"

            amount = abs(diff / 100) * ctx.total_capital

            # 레버리지는 BULL에서만 BUY 권고
            if atype == ASSET_ETF_LEVERAGE and action == "BUY":
                if regime != "BULL":
                    action = "HOLD"
                    reason = f"{regime} 국면: 레버리지 BUY 보류"

            # 약세장 개별주식 BUY 억제
            if regime == "BEAR" and atype in (ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ):
                if action == "BUY":
                    action = "HOLD"
                    reason = "약세장: 개별주식 신규매수 억제"

            advice.append({
                "asset_type":  atype,
                "label":       get_asset_type_label(atype),
                "action":      action,
                "current_pct": round(cur_pct, 1),
                "target_pct":  round(tgt_pct, 1),
                "diff_pct":    round(diff, 1),
                "amount":      round(amount, 0),
                "reason":      reason,
            })

        return sorted(advice, key=lambda x: abs(x["diff_pct"]), reverse=True)


# ═══════════════════════════════════════════════════════════════
# ETF 스코어링 어댑터
# ═══════════════════════════════════════════════════════════════

class ETFScorer:
    """
    ETF 전용 간소화 스코어링.
    개별주식과 달리 재무·기관 데이터 없이 추세·모멘텀 기반 평가.

    점수 구성 (100점):
      추세 점수   35점  — 단기(20일) + 중기(60일) MA 위/아래
      모멘텀 점수 30점  — 1개월/3개월 수익률
      거래대금    20점  — 평균 대비 증가율
      RS(상대강도) 15점  — vs 기초지수 또는 코스피200

    국면 보정:
      인버스 ETF: BEAR 국면에서 RS 의미 반전 (지수 하락 = 인버스 상승)
      레버리지 ETF: BULL 국면에서만 진입 허용 (추가 조건 검사)
    """

    def score_etf(
        self,
        d:          dict,
        asset_type: str,
        regime:     str = "LATERAL",
    ) -> dict:
        """
        ETF 점수화.
        d: get_stock_detail() 반환 구조와 호환
        """
        base   = 0
        detail = {}

        # ── 추세 점수 (35점) ──────────────────────────────
        trend, t_detail = self._score_trend(d, asset_type, regime)
        detail["trend"] = {
            "score": trend, "max": 35,
            "label": "추세점수",
            "detail": t_detail,
        }
        base += trend

        # ── 모멘텀 점수 (30점) ────────────────────────────
        mom, m_detail = self._score_momentum(d, asset_type, regime)
        detail["momentum"] = {
            "score": mom, "max": 30,
            "label": "모멘텀",
            "detail": m_detail,
        }
        base += mom

        # ── 거래대금 점수 (20점) ──────────────────────────
        amt, a_detail = self._score_amount(d)
        detail["amount"] = {
            "score": amt, "max": 20,
            "label": "거래대금",
            "detail": a_detail,
        }
        base += amt

        # ── RS 점수 (15점) ────────────────────────────────
        rs_s, rs_val = self._score_rs_etf(d, asset_type, regime)
        detail["rs"] = {
            "score": rs_s, "max": 15,
            "label": "상대강도",
            "raw":   rs_val,
        }
        base += rs_s

        # 점수 클램프
        total = max(0, min(110, base))
        grade = self._grade(total)

        return {
            "code":        d.get("code"),
            "name":        d.get("name"),
            "asset_type":  asset_type,
            "total_score": round(total, 1),
            "grade":       grade,
            "rs_value":    round(rs_val, 2),
            "detail":      detail,
            "regime":      regime,
        }

    def _score_trend(self, d: dict, atype: str, regime: str):
        """추세 점수 — MA20/MA60 위치 기반"""
        cur    = d.get("price_now", 0)
        ma20   = d.get("price_ma20", 0)
        ma60   = d.get("price_ma60", 0)
        score  = 0
        detail = []

        if not cur:
            return 0, detail

        # 인버스 ETF는 하락 추세에서 점수 부여 (반전)
        is_inv = (atype == ASSET_ETF_INVERSE)

        if ma20:
            above_ma20 = cur > ma20
            if (above_ma20 and not is_inv) or (not above_ma20 and is_inv):
                score += 18
                detail.append(f"{'MA20 위' if not is_inv else 'MA20 아래(인버스유리)'}: +18")
            else:
                detail.append(f"{'MA20 아래' if not is_inv else 'MA20 위(인버스불리)'}: +0")

        if ma60:
            above_ma60 = cur > ma60
            if (above_ma60 and not is_inv) or (not above_ma60 and is_inv):
                score += 17
                detail.append(f"+17")

        return min(35, score), detail

    def _score_momentum(self, d: dict, atype: str, regime: str):
        """모멘텀 점수 — 1M/3M 수익률"""
        p0_1m = d.get("price_1m", 0)
        p0_3m = d.get("price_3m", 0)
        cur   = d.get("price_now", 0)
        if not cur:
            return 0, {}

        is_inv = (atype == ASSET_ETF_INVERSE)
        score  = 0

        # 1개월 수익률 (15점)
        if p0_1m:
            ret1m = (cur - p0_1m) / p0_1m * 100
            if is_inv: ret1m = -ret1m   # 인버스: 하락장에서 상승
            if   ret1m >= 10: s = 15
            elif ret1m >=  5: s = 10
            elif ret1m >=  2: s = 6
            elif ret1m >=  0: s = 3
            else:             s = 0
            score += s

        # 3개월 수익률 (15점)
        if p0_3m:
            ret3m = (cur - p0_3m) / p0_3m * 100
            if is_inv: ret3m = -ret3m
            if   ret3m >= 20: s = 15
            elif ret3m >= 10: s = 10
            elif ret3m >=  5: s = 6
            elif ret3m >=  0: s = 3
            else:             s = 0
            score += s

        return min(30, score), {"ret1m": ret1m if p0_1m else None,
                                 "ret3m": ret3m if p0_3m else None}

    def _score_amount(self, d: dict):
        """거래대금 점수 (20점)"""
        avg = d.get("amount_avg_20", 0)
        cur = d.get("amount_now", 0)
        if not avg:
            return 5, {"ratio": None}  # 데이터 없을 시 중립 점수
        ratio = cur / avg
        if   ratio >= 3.0: s = 20
        elif ratio >= 2.0: s = 15
        elif ratio >= 1.5: s = 10
        elif ratio >= 1.0: s = 5
        else:              s = 2
        return s, {"ratio": round(ratio, 2)}

    def _score_rs_etf(self, d: dict, atype: str, regime: str):
        """
        ETF 상대강도 (15점).
        - 일반/레버리지: KOSPI200 대비 수익률
        - 인버스: 음의 상관관계 활용 (지수 하락 시 RS 양수)
        """
        p0  = d.get("price_1m", d.get("price_now", 0))
        p1  = d.get("price_now", 0)
        mkt = d.get("market_return_20", 0)

        if not p0 or not p1:
            return 5, 0.0   # 중립

        etf_ret = (p1 - p0) / p0 * 100

        if atype == ASSET_ETF_INVERSE:
            # 인버스 RS: 지수가 하락할수록 우수
            rs = -mkt - etf_ret   # 지수 하락 크기 반영
        else:
            rs = etf_ret - mkt

        if   rs >= 15: s = 15
        elif rs >= 10: s = 12
        elif rs >=  5: s = 8
        elif rs >=  0: s = 5
        elif rs >= -5: s = 2
        else:          s = 0
        return s, rs

    def _grade(self, score: float) -> str:
        if   score >= 85: return "BUY_CANDIDATE"
        elif score >= 75: return "WATCH_HIGH"
        elif score >= 60: return "WATCH"
        elif score >= 50: return "HOLD_ONLY"
        else:             return "EXCLUDE"


# ── 편의 함수 ────────────────────────────────────────────────

_allocator = AssetAllocator()
_etf_scorer = ETFScorer()
_rebalance_advisor = RebalanceAdvisor()


def make_context(
    regime:         str,
    total_capital:  float,
    cash:           float,
    positions:      dict,   # {code: {"asset_type", "value", ...}}
) -> AllocationContext:
    """AllocationContext 편의 생성자"""
    from screener.asset_universe import summarize_portfolio_by_asset
    pos_summary = summarize_portfolio_by_asset(positions, total_capital)
    return AllocationContext(
        regime           = regime,
        total_capital    = total_capital,
        cash             = cash,
        position_summary = pos_summary,
    )


def get_allocation_plan(
    screened:      list[dict],
    regime:        str,
    total_capital: float,
    cash:          float,
    positions:     dict = None,
) -> AllocationPlan:
    """원-샷 배분 계획 생성"""
    ctx = make_context(regime, total_capital, cash, positions or {})
    return _allocator.plan(screened, ctx)


def score_etf(d: dict, asset_type: str, regime: str = "LATERAL") -> dict:
    """ETF 1종목 점수화"""
    return _etf_scorer.score_etf(d, asset_type, regime)


def get_rebalance_advice(ctx: AllocationContext, plan: AllocationPlan) -> list[dict]:
    """리밸런싱 권고 생성"""
    return _rebalance_advisor.advise(ctx, plan)
