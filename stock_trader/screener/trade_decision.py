"""
매수/매도/손절/추가매수 결정 엔진
===================================

★ 매수 조건: 모든 조건 충족 시만 진입
★ 손절: -10% 도달 시 전량 매도
★ 추가매수: +10%/+20%/+35% 구간 분할 (수익 중인 종목만)
★ 트레일링 스탑: 고점 대비 -12% 또는 MA20 이탈
★ 손절 회수금: 수익 중인 강한 종목 우선 배분
"""

import os
import json
from datetime import datetime
from utils.logger import get_logger

logger = get_logger("TradeDecision")

# ── 손절/익절 기준 ───────────────────────────────────────────
STOP_LOSS_PCT     = -10.0          # 손절: 매수가 대비 -10%
TRAILING_STOP_PCT = -12.0          # 트레일링 스탑: 고점 대비 -12%
ADD_BUY_LEVELS    = [10, 20, 35]   # 추가매수 구간 (+%)
ADD_BUY_RATIO     = 0.33           # 추가매수 시 현금 대비 비율
MA20_EXIT_BUFFER  = -1.0           # MA20 이탈 허용 버퍼(%)


class TradeDecisionEngine:
    """
    AI 점수 + 포지션 상태를 결합해 최종 매매 결정 반환
    포지션 데이터: {
        code, name, entry_price, avg_price,
        qty, highest_price, added_levels: [10,20],  # 이미 추가매수한 구간
        entry_score, entry_date
    }
    """

    # ══════════════════════════════════════════════════════════
    # 매수 결정
    # ══════════════════════════════════════════════════════════
    def decide_buy(self, score_result: dict, account_info: dict) -> dict:
        """
        score_result: AIScorer.score() 반환값
        account_info: {
            cash, total_assets, positions: [{code,name,qty,avg_price,cur_price,...}],
            daily_loss_pct, total_loss_pct, sector_exposure: {업종: 비중}
        }
        """
        code  = score_result["code"]
        name  = score_result["name"]
        total = score_result["total_score"]
        rs    = score_result["rs_value"]
        grade = score_result["grade"]

        # 기본 적격 여부 (AIScorer 에서 사전 검증)
        if not score_result.get("buy_eligible"):
            return self._skip(code, name, score_result["buy_reason"])

        # 계좌 리스크 검사 (RiskGuard 에서 별도 처리하지만 여기서도 기본 체크)
        cash   = account_info.get("cash", 0)
        total_assets = account_info.get("total_assets", 1)

        # 하루 손실 -3% 이상이면 신규매수 중단
        if account_info.get("daily_loss_pct", 0) <= -3.0:
            return self._skip(code, name, f"일손실한도초과({account_info['daily_loss_pct']:.1f}%)")

        # 계좌 전체 손실 -15% 이상이면 자동매매 정지
        if account_info.get("total_loss_pct", 0) <= -15.0:
            return self._skip(code, name, "계좌전체손실한도초과")

        # 이미 보유 중인 종목은 추가매수 경로로
        existing = next(
            (p for p in account_info.get("positions", []) if p["code"] == code),
            None
        )
        if existing:
            return self._skip(code, name, "이미보유(추가매수경로사용)")

        # 업종 집중도 체크
        sector = score_result.get("sector", "기타")
        sector_exp = account_info.get("sector_exposure", {})
        if sector_exp.get(sector, 0) >= 25.0:
            return self._skip(code, name, f"업종집중도초과({sector} {sector_exp[sector]:.0f}%)")

        # 투자 금액 결정 (종목당 최대 총자산의 10%)
        cur_price = score_result.get("cur_price", 0)
        if not cur_price:
            return self._skip(code, name, "현재가 없음")

        max_invest = total_assets * 0.10
        invest_amt = min(max_invest, cash * 0.90)
        qty        = int(invest_amt / cur_price)

        if qty < 1:
            return self._skip(code, name, f"매수수량0 (가용:{invest_amt:,.0f}원/{cur_price:,}원)")

        return {
            "action":    "BUY",
            "code":      code,
            "name":      name,
            "price":     cur_price,
            "qty":       qty,
            "amount":    qty * cur_price,
            "grade":     grade,
            "score":     total,
            "rs":        rs,
            "reason":    f"AI점수={total:.0f}pts RS={rs:+.1f}% [{grade}]",
        }

    # ══════════════════════════════════════════════════════════
    # 추가매수 결정 (+10/+20/+35% 분할)
    # ══════════════════════════════════════════════════════════
    def decide_add_buy(self, position: dict, score_result: dict,
                       account_info: dict) -> dict:
        """
        수익 중인 종목에만 추가매수.
        손실 중이면 무조건 SKIP.
        """
        code       = position["code"]
        name       = position["name"]
        avg_price  = position["avg_price"]
        cur_price  = score_result.get("cur_price", position.get("cur_price", 0))
        added_lvls = position.get("added_levels", [])

        if not cur_price:
            return self._skip(code, name, "현재가 없음")

        gain_pct   = (cur_price - avg_price) / avg_price * 100

        # 손실 중: 추가매수 금지
        if gain_pct <= 0:
            return self._skip(code, name, f"손실중({gain_pct:.1f}%) 추가매수불가")

        # 하루 손실 한도 체크
        if account_info.get("daily_loss_pct", 0) <= -3.0:
            return self._skip(code, name, "일손실한도초과")

        # 어떤 추가매수 구간 도달했는지 확인
        target_lvl = None
        for lvl in ADD_BUY_LEVELS:
            if lvl not in added_lvls and gain_pct >= lvl:
                target_lvl = lvl
                break   # 가장 낮은 미진입 구간 우선

        if target_lvl is None:
            return self._skip(code, name, f"추가매수구간미도달({gain_pct:.1f}%)")

        # AI 점수도 일정 이상이어야 추가매수
        score = score_result.get("total_score", 0)
        if score < 70:
            return self._skip(code, name, f"추가매수점수부족({score:.0f}/70)")

        cash       = account_info.get("cash", 0)
        total_assets = account_info.get("total_assets", 1)
        invest_amt = min(total_assets * 0.05, cash * 0.5)  # 추가: 총자산 5%
        qty        = int(invest_amt / cur_price)

        if qty < 1:
            return self._skip(code, name, "추가매수금액부족")

        return {
            "action":    "ADD_BUY",
            "code":      code,
            "name":      name,
            "price":     cur_price,
            "qty":       qty,
            "amount":    qty * cur_price,
            "add_level": target_lvl,
            "gain_pct":  gain_pct,
            "score":     score,
            "reason":    f"추가매수+{target_lvl}%(현재수익{gain_pct:.1f}%)",
        }

    # ══════════════════════════════════════════════════════════
    # 매도 / 손절 결정
    # ══════════════════════════════════════════════════════════
    def decide_sell(self, position: dict, score_result: dict,
                    market_info: dict = None) -> dict:
        """
        손절/트레일링스탑/MA20이탈 중 하나라도 해당하면 SELL 반환
        """
        code         = position["code"]
        name         = position["name"]
        avg_price    = position["avg_price"]
        highest_price= position.get("highest_price", avg_price)
        qty          = position["qty"]
        cur_price    = score_result.get("cur_price", position.get("cur_price", avg_price))

        if not cur_price:
            return self._hold(code, name, "현재가없음")

        loss_pct    = (cur_price - avg_price) / avg_price * 100
        trail_pct   = (cur_price - highest_price) / highest_price * 100 if highest_price else 0
        ma20        = score_result.get("price_ma20", 0)
        ma20_pct    = (cur_price - ma20) / ma20 * 100 if ma20 else 0

        # ① 손절: -10%
        if loss_pct <= STOP_LOSS_PCT:
            return {
                "action":   "SELL",
                "code":     code, "name": name,
                "qty":      qty, "price": cur_price,
                "reason":   f"손절({loss_pct:.1f}%)",
                "sell_type":"STOP_LOSS",
                "profit_pct": loss_pct,
            }

        # ② 트레일링 스탑: 고점 -12% (단, 고점이 매수가 +5% 이상일 때만)
        if (highest_price > avg_price * 1.05 and
                trail_pct <= TRAILING_STOP_PCT):
            return {
                "action":   "SELL",
                "code":     code, "name": name,
                "qty":      qty, "price": cur_price,
                "reason":   f"트레일링스탑(고점{trail_pct:.1f}%)",
                "sell_type":"TRAILING_STOP",
                "profit_pct": loss_pct,
            }

        # ③ MA20 이탈 매도 (수익 중일 때만 — 손절은 위에서 처리)
        if ma20 and loss_pct > 0 and ma20_pct <= MA20_EXIT_BUFFER:
            return {
                "action":   "SELL",
                "code":     code, "name": name,
                "qty":      qty, "price": cur_price,
                "reason":   f"MA20이탈(수익확보 {loss_pct:.1f}%)",
                "sell_type":"MA20_EXIT",
                "profit_pct": loss_pct,
            }

        # ④ AI 점수 급락 (EXCLUDE 등급)
        grade = score_result.get("grade", "")
        score = score_result.get("total_score", 100)
        if grade == "EXCLUDE" and score < 40:
            return {
                "action":   "SELL",
                "code":     code, "name": name,
                "qty":      qty, "price": cur_price,
                "reason":   f"AI점수급락({score:.0f}pts/{grade})",
                "sell_type":"SCORE_DROP",
                "profit_pct": loss_pct,
            }

        return self._hold(code, name,
            f"유지(손익{loss_pct:+.1f}% 고점대비{trail_pct:+.1f}% MA20={ma20_pct:+.1f}%)")

    # ══════════════════════════════════════════════════════════
    # 손절 회수금 재배분 우선순위
    # ══════════════════════════════════════════════════════════
    def prioritize_reallocation(self, scored_positions: list[dict]) -> list[str]:
        """
        손절 후 회수된 현금 → 수익 중인 강한 종목에 우선 배분
        returns: 추가배분 우선순위 코드 목록
        """
        # 수익 중 + 점수 높은 종목 필터
        candidates = [
            p for p in scored_positions
            if p.get("gain_pct", 0) > 0 and p.get("total_score", 0) >= 80
        ]
        # 점수 내림차순
        candidates.sort(key=lambda x: x.get("total_score", 0), reverse=True)
        return [p["code"] for p in candidates[:5]]

    # ── 내부 헬퍼 ─────────────────────────────────────────────
    @staticmethod
    def _skip(code, name, reason):
        return {"action": "SKIP", "code": code, "name": name, "reason": reason}

    @staticmethod
    def _hold(code, name, reason):
        return {"action": "HOLD", "code": code, "name": name, "reason": reason}
