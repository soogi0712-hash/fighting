"""
AI 점수화 엔진 — 상대강도 중심 100점 스코어링
=============================================

항목별 배점:
  최근 3개월 상승률   15점
  최근 1개월 상승률   10점
  거래량 증가율       15점
  거래대금 증가율     10점
  기관 순매수         10점
  외국인 순매수       10점
  신고가 근접도       10점
  영업이익 증가율     10점
  ROE                  5점
  상대강도(RS)        15점
  ──────────────────────
  기본합계           100점

시장 위험 감점 (별도):
  지수 20일 -5% 이하         -5점
  지수 20일 -10% 이하       -10점
  지수 60MA 아래             -5점
  지수 120MA 아래           -10점

강한 종목 가산점:
  지수 하락일 상승마감       +5점
  20일 시장대비 +10% 이상    +5점
  60일 신고가 돌파           +5점
  거래대금 2배 이상          +5점
  기관·외인 연속 순매수      +5점
"""

from utils.logger import get_logger

logger = get_logger("AIScorer")

# ── 배점표 ──────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "return_3m":    15,   # 3개월 수익률
    "return_1m":    10,   # 1개월 수익률
    "volume_ratio": 15,   # 거래량 증가율
    "amount_ratio": 10,   # 거래대금 증가율
    "inst_net":     10,   # 기관 순매수
    "foreign_net":  10,   # 외국인 순매수
    "high52w":      10,   # 신고가 근접도
    "op_growth":    10,   # 영업이익 증가율
    "roe":           5,   # ROE
    "rs":           15,   # 상대강도(RS)
}

# ── 최종 분류 기준 ───────────────────────────────────────────
GRADE_MAP = [
    (90, "BUY_CANDIDATE"),
    (80, "WATCH_HIGH"),
    (70, "WATCH"),
    (60, "HOLD_ONLY"),
    ( 0, "EXCLUDE"),
]


class AIScorer:
    """
    종목 데이터를 받아 AI 점수(0~100+bonus)를 산출하고 등급을 분류한다.

    입력 stock_data 구조:
      {
        code, name,
        price_now, price_1m, price_3m,        # 현재가, 1달전가, 3달전가
        volume_avg_20, volume_now,             # 평균거래량, 최근거래량
        amount_avg_20, amount_now,             # 평균거래대금, 최근거래대금
        inst_net_20,                           # 20일 기관 순매수(주)
        foreign_net_20,                        # 20일 외인 순매수(주)
        high_60d,                              # 60일 최고가
        op_profit_now, op_profit_prev,         # 영업이익 현재/전년
        roe,                                   # ROE(%)
        market_return_20,                      # 시장지수 20일 수익률(%)
        inst_net_streak,                       # 기관 연속순매수일
        foreign_net_streak,                    # 외인 연속순매수일
        market_fell_today,                     # 오늘 지수 하락 여부
        price_change_today,                    # 오늘 종목 등락률(%)
        index_ma60, index_ma120,               # 지수 이평
        index_price,                           # 지수 현재가
        index_return_60d,                      # 지수 60일 수익률
      }
    """

    def score(self, d: dict) -> dict:
        """
        returns:
          {
            total_score, grade,
            base_score, market_penalty, bonus,
            detail: {항목: {raw, score, max}},
            rs_value, buy_eligible, reason
          }
        """
        detail = {}
        base   = 0

        # ── 1. 기본 100점 계산 ────────────────────────────────
        # 3개월 수익률 (15점)
        s, raw = self._score_return_3m(d)
        detail["return_3m"] = {"raw": raw, "score": s, "max": 15, "label": "3개월 수익률"}
        base += s

        # 1개월 수익률 (10점)
        s, raw = self._score_return_1m(d)
        detail["return_1m"] = {"raw": raw, "score": s, "max": 10, "label": "1개월 수익률"}
        base += s

        # 거래량 증가율 (15점)
        s, raw = self._score_volume(d)
        detail["volume_ratio"] = {"raw": raw, "score": s, "max": 15, "label": "거래량 증가율"}
        base += s

        # 거래대금 증가율 (10점)
        s, raw = self._score_amount(d)
        detail["amount_ratio"] = {"raw": raw, "score": s, "max": 10, "label": "거래대금 증가율"}
        base += s

        # 기관 순매수 (10점)
        s, raw = self._score_inst(d)
        detail["inst_net"] = {"raw": raw, "score": s, "max": 10, "label": "기관 순매수"}
        base += s

        # 외국인 순매수 (10점)
        s, raw = self._score_foreign(d)
        detail["foreign_net"] = {"raw": raw, "score": s, "max": 10, "label": "외국인 순매수"}
        base += s

        # 신고가 근접도 (10점)
        s, raw = self._score_high52w(d)
        detail["high52w"] = {"raw": raw, "score": s, "max": 10, "label": "신고가 근접도"}
        base += s

        # 영업이익 증가율 (10점)
        s, raw = self._score_op_growth(d)
        detail["op_growth"] = {"raw": raw, "score": s, "max": 10, "label": "영업이익 증가율"}
        base += s

        # ROE (5점)
        s, raw = self._score_roe(d)
        detail["roe"] = {"raw": raw, "score": s, "max": 5, "label": "ROE"}
        base += s

        # 상대강도 RS (15점)
        s, raw, rs_val = self._score_rs(d)
        detail["rs"] = {"raw": raw, "score": s, "max": 15, "label": "상대강도(RS)"}
        base += s

        # ── 2. 시장 위험 감점 ─────────────────────────────────
        penalty, pen_detail = self._market_penalty(d)
        detail["market_penalty"] = {
            "raw": pen_detail, "score": penalty, "max": 0, "label": "시장위험 감점"
        }

        # ── 3. 강한 종목 가산점 ───────────────────────────────
        bonus, bonus_detail = self._strong_bonus(d, rs_val)
        detail["bonus"] = {
            "raw": bonus_detail, "score": bonus, "max": 25, "label": "강종목 가산점"
        }

        # ── 4. 최종 점수 & 등급 ───────────────────────────────
        total = base + penalty + bonus        # penalty 는 음수
        total = max(0, min(120, total))       # 0~120 범위 클램프
        grade = self._grade(total)

        # 매수 적격 여부
        buy_ok, buy_reason = self._buy_eligible(d, total, rs_val)

        logger.debug(
            f"{d.get('name','?')} | 기본={base} 감점={penalty:+} 보너스={bonus:+} "
            f"총점={total} [{grade}] RS={rs_val:+.1f}%"
        )

        return {
            "code":           d.get("code"),
            "name":           d.get("name"),
            "total_score":    round(total, 1),
            "base_score":     round(base, 1),
            "market_penalty": penalty,
            "bonus":          bonus,
            "grade":          grade,
            "rs_value":       round(rs_val, 2),
            "buy_eligible":   buy_ok,
            "buy_reason":     buy_reason,
            "detail":         detail,
        }

    # ══════════════════════════════════════════════════════════
    # 기본 점수 계산 메서드
    # ══════════════════════════════════════════════════════════

    def _score_return_3m(self, d):
        """3개월 수익률 → 15점"""
        p0 = d.get("price_3m", 0)
        p1 = d.get("price_now", 0)
        if not p0 or not p1:
            return 0, None
        ret = (p1 - p0) / p0 * 100
        # +30%↑=15, +20%=12, +10%=9, +5%=6, 0%=3, 음수=0
        if   ret >= 30: s = 15
        elif ret >= 20: s = 12
        elif ret >= 10: s = 9
        elif ret >=  5: s = 6
        elif ret >=  0: s = 3
        else:           s = max(0, 3 + ret * 0.3)  # 음수: 선형 감소
        return round(s, 1), round(ret, 1)

    def _score_return_1m(self, d):
        """1개월 수익률 → 10점"""
        p0 = d.get("price_1m", 0)
        p1 = d.get("price_now", 0)
        if not p0 or not p1:
            return 0, None
        ret = (p1 - p0) / p0 * 100
        if   ret >= 15: s = 10
        elif ret >= 10: s = 8
        elif ret >=  5: s = 6
        elif ret >=  2: s = 4
        elif ret >=  0: s = 2
        else:           s = max(0, 2 + ret * 0.4)
        return round(s, 1), round(ret, 1)

    def _score_volume(self, d):
        """거래량 증가율 → 15점"""
        avg = d.get("volume_avg_20", 0)
        cur = d.get("volume_now", 0)
        if not avg:
            return 0, None
        ratio = cur / avg
        if   ratio >= 3.0: s = 15
        elif ratio >= 2.0: s = 12
        elif ratio >= 1.5: s = 9
        elif ratio >= 1.2: s = 6
        elif ratio >= 1.0: s = 3
        else:              s = 0
        return round(s, 1), round(ratio, 2)

    def _score_amount(self, d):
        """거래대금 증가율 → 10점"""
        avg = d.get("amount_avg_20", 0)
        cur = d.get("amount_now", 0)
        if not avg:
            return 0, None
        ratio = cur / avg
        if   ratio >= 3.0: s = 10
        elif ratio >= 2.0: s = 8
        elif ratio >= 1.5: s = 6
        elif ratio >= 1.2: s = 4
        elif ratio >= 1.0: s = 2
        else:              s = 0
        return round(s, 1), round(ratio, 2)

    def _score_inst(self, d):
        """기관 순매수 → 10점"""
        net = d.get("inst_net_20", 0)
        if   net >  500_000: s = 10
        elif net >  200_000: s = 8
        elif net >   50_000: s = 6
        elif net >        0: s = 4
        elif net >  -50_000: s = 2
        else:                s = 0
        return round(s, 1), net

    def _score_foreign(self, d):
        """외국인 순매수 → 10점"""
        net = d.get("foreign_net_20", 0)
        if   net >  500_000: s = 10
        elif net >  200_000: s = 8
        elif net >   50_000: s = 6
        elif net >        0: s = 4
        elif net >  -50_000: s = 2
        else:                s = 0
        return round(s, 1), net

    def _score_high52w(self, d):
        """신고가 근접도(60일 고가 기준) → 10점"""
        high = d.get("high_60d", 0)
        cur  = d.get("price_now", 0)
        if not high or not cur:
            return 0, None
        pct  = cur / high * 100         # 고가 대비 현재가 비율
        if   pct >= 100: s = 10         # 신고가 돌파
        elif pct >=  97: s = 8          # 3% 이내
        elif pct >=  93: s = 5          # 7% 이내
        elif pct >=  85: s = 2          # 15% 이내
        else:            s = 0
        return round(s, 1), round(pct, 1)

    def _score_op_growth(self, d):
        """영업이익 증가율 → 10점"""
        prev = d.get("op_profit_prev", None)
        cur  = d.get("op_profit_now", None)
        if cur is None or prev is None:
            return 0, None
        if prev == 0:
            if cur > 0: return 8, "흑전"
            return 0, "영업손실"
        if prev < 0 and cur > 0:
            return 10, "흑자전환"
        growth = (cur - prev) / abs(prev) * 100
        if   growth >= 50: s = 10
        elif growth >= 30: s = 8
        elif growth >= 15: s = 6
        elif growth >=  0: s = 4
        elif growth >= -20:s = 2
        else:              s = 0
        return round(s, 1), round(growth, 1)

    def _score_roe(self, d):
        """ROE → 5점"""
        roe = d.get("roe", None)
        if roe is None:
            return 0, None
        if   roe >= 25: s = 5
        elif roe >= 15: s = 4
        elif roe >= 10: s = 3
        elif roe >=  5: s = 2
        elif roe >=  0: s = 1
        else:           s = 0
        return round(s, 1), round(roe, 1)

    def _score_rs(self, d):
        """
        상대강도(RS) → 15점
        RS = 종목 20일 수익률 - 시장 20일 수익률
        """
        p0  = d.get("price_1m", d.get("price_now", 0))  # 20일 전 근사값
        p1  = d.get("price_now", 0)
        mkt = d.get("market_return_20", 0)               # 시장 20일 수익률(%)

        if not p0 or not p1:
            return 0, None, 0.0
        stock_ret = (p1 - p0) / p0 * 100
        rs        = stock_ret - mkt                       # RS 값

        if   rs >= 20: s = 15
        elif rs >= 15: s = 13
        elif rs >= 10: s = 11
        elif rs >=  5: s = 8
        elif rs >=  0: s = 5
        elif rs >= -5: s = 2
        else:          s = 0
        return round(s, 1), round(rs, 2), rs

    # ══════════════════════════════════════════════════════════
    # 시장 위험 감점
    # ══════════════════════════════════════════════════════════

    def _market_penalty(self, d) -> tuple[float, list]:
        """시장 위험을 감점으로 반영 (음수 반환)"""
        penalty = 0
        reasons = []

        mkt20 = d.get("market_return_20", 0)
        idx_p = d.get("index_price", 0)
        ma60  = d.get("index_ma60", 0)
        ma120 = d.get("index_ma120", 0)
        today_chg = d.get("price_change_today", 0)
        mkt_fell  = d.get("market_fell_today", False)

        # 지수 20일 수익률 감점
        if mkt20 <= -10:
            penalty -= 10
            reasons.append(f"지수20일{mkt20:.1f}%(-10)")
        elif mkt20 <= -5:
            penalty -= 5
            reasons.append(f"지수20일{mkt20:.1f}%(-5)")

        # 이동평균선 아래
        if idx_p and ma120 and idx_p < ma120:
            penalty -= 10
            reasons.append("지수<MA120(-10)")
        elif idx_p and ma60 and idx_p < ma60:
            penalty -= 5
            reasons.append("지수<MA60(-5)")

        # 강한 종목 완화: 지수 하락일에 종목 상승/강보합
        if mkt_fell and today_chg >= 0:
            relief = min(abs(penalty), 5)
            penalty += relief
            reasons.append(f"지수하락 종목강보합+{relief}")

        return penalty, reasons

    # ══════════════════════════════════════════════════════════
    # 강한 종목 가산점
    # ══════════════════════════════════════════════════════════

    def _strong_bonus(self, d, rs_val: float) -> tuple[float, list]:
        """강한 종목에 가산점 부여"""
        bonus   = 0
        reasons = []

        # 지수 하락일 상승마감
        if d.get("market_fell_today") and d.get("price_change_today", 0) > 0:
            bonus += 5
            reasons.append("지수하락日상승(+5)")

        # 20일 시장대비 10% 이상 강함
        if rs_val >= 10:
            bonus += 5
            reasons.append(f"RS+{rs_val:.1f}%우세(+5)")

        # 60일 신고가 돌파
        high60 = d.get("high_60d", 0)
        cur    = d.get("price_now", 0)
        if high60 and cur and cur >= high60:
            bonus += 5
            reasons.append("60일신고가돌파(+5)")

        # 거래대금 2배 이상
        avg_amt = d.get("amount_avg_20", 0)
        cur_amt = d.get("amount_now", 0)
        if avg_amt and cur_amt >= avg_amt * 2:
            bonus += 5
            reasons.append("거래대금2배(+5)")

        # 기관 또는 외인 연속 순매수
        inst_s = d.get("inst_net_streak", 0)
        frn_s  = d.get("foreign_net_streak", 0)
        if inst_s >= 3 or frn_s >= 3:
            bonus += 5
            reasons.append(f"연속순매수기관{inst_s}일/외인{frn_s}일(+5)")

        return bonus, reasons

    # ══════════════════════════════════════════════════════════
    # 등급 및 매수 적격 판단
    # ══════════════════════════════════════════════════════════

    def _grade(self, score: float) -> str:
        for threshold, grade in GRADE_MAP:
            if score >= threshold:
                return grade
        return "EXCLUDE"

    def _buy_eligible(self, d: dict, total: float, rs: float) -> tuple[bool, str]:
        """
        실제 매수 가능 조건 (모두 충족해야 True):
          1. 총점 85점 이상
          2. RS 양수
          3. 일평균 거래대금 30억 이상
          4. 최근 거래량 증가 (평균 이상)
          5. 20일 이동평균선 위
          6. 60일 신고가 근접(95%) 또는 돌파
        """
        reasons = []

        if total < 85:
            reasons.append(f"총점부족({total:.0f}/85)")

        if rs <= 0:
            reasons.append(f"RS비양수({rs:+.1f}%)")

        daily_amt = d.get("amount_avg_20", 0)
        if daily_amt < 3_000_000_000:
            reasons.append(f"거래대금미달({daily_amt/1e8:.0f}억)")

        vol_avg = d.get("volume_avg_20", 1)
        vol_now = d.get("volume_now", 0)
        if vol_now < vol_avg * 0.8:
            reasons.append("거래량감소")

        cur  = d.get("price_now", 0)
        ma20 = d.get("price_ma20", 0)
        if ma20 and cur < ma20:
            reasons.append(f"MA20이탈({cur:,}<{ma20:,})")

        high60 = d.get("high_60d", 0)
        if high60 and cur < high60 * 0.95:
            reasons.append(f"신고가원거리({cur/high60*100:.0f}%)")

        if reasons:
            return False, " | ".join(reasons)
        return True, "매수조건충족"

    def batch_score(self, stock_list: list[dict]) -> list[dict]:
        """여러 종목을 한번에 점수화"""
        results = []
        for d in stock_list:
            try:
                result = self.score(d)
                results.append(result)
            except Exception as e:
                logger.error(f"점수화 오류 {d.get('code','?')}: {e}")
        # 점수 내림차순 정렬
        results.sort(key=lambda x: x["total_score"], reverse=True)
        return results
