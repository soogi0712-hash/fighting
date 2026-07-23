"""
보조지표 검증 레이어 v2 — 복리형 초회전 단타 시스템
=====================================================

★ 핵심 철학: 1~2% 수익 반복 실현 + 수익 반납 최소화
★ 진입 철학: "확실한 타점보다 조금 빠른 진입" — 거래량 선점 전략

── BUY SCORE (0.0 ~ 1.0, 정규화) ──
  Early Entry  : BUY SCORE ≥ 0.40 → 예정투자금 30% 선진입
  본 진입      : BUY SCORE ≥ 0.55 → 나머지 70% 진입

  구성 지표 (7개):
    1. MA     — 이동평균 배열 (MA5 > MA20 > MA60)
    2. RSI    — 과매도 탈출 (30→50 회복 구간)
    3. MACD   — 골든크로스 / 히스토그램 양전환
    4. BB     — 볼린저밴드 (하단반등 / 중심선 위 + 밴드 확장)
    5. ATR    — 변동성 확대
    6. OBV    — 거래량 선행 상승 (매집 신호)
    7. BB_SQZ — 볼린저 압축(밴드폭 수축 → 돌파 예고)

  거래량 가중치 보너스 (BUY SCORE에 가산):
    일반 거래량 증가 (직전 대비 +20% 이상)  : +2점 → score +0.14
    거래량 폭증    (직전 대비 +100% 이상) : +3점 → score +0.21
    ★ 거래량 증가 없으면 BUY_SCORE ≥ 임계치여도 진입 금지
    ★ VWAP 아래면 진입 금지 (vwap_above 필수)

── SELL SCORE (가중치 합산, ≥ 6점 즉시 매도) ──
  ★ BUY 진입 시 SELL_SCORE ≥ 5이면 매수 금지 (기존 ≥ 6 즉시매도와 별개)
  가중치 항목:
    VWAP 이탈               +4
    OBV 하락전환             +3
    체결강도 급락            +3
    거래량 감소              +2
    매수주체 이탈            +3  (현재가 < MA5 이탈)
    프로그램 매도우위        +2  (ATR 급락)
    볼린저 상단 터치         +3
    볼린저 상단 돌파 실패    +4
    20MA 이탈               +3

── 추세강도 점수 (0~100) ──
  A. RS  (상대강도)
  B. ADX (추세방향 강도)
  C. 모멘텀 가속도
  D. 고점 경신 근접도
"""

import numpy as np
import pandas as pd
from utils.logger import get_logger

logger = get_logger("IndicatorValidator")

# ── 추세강도 기준 ──────────────────────────────────────────────
STRONG_TREND_THRESHOLD = 60
ADX_STRONG_LEVEL       = 25
ADX_VERY_STRONG        = 40

# ── BUY SCORE 임계 ─────────────────────────────────────────────
# "확실한 타점보다 조금 빠른 진입" 철학 반영 → 임계 하향
BUY_SCORE_EARLY  = 0.50   # [TOP1] 0.40→0.50 (라벨 일관성; 실제 게이트는 pyramid_strategy)
BUY_SCORE_FULL   = 0.65   # [TOP1] 0.55→0.65 (라벨 일관성)

# ── 거래량 보너스 (BUY SCORE 가산) ───────────────────────────
# 7개 지표 기반 score에 거래량 가산 → 조기 선점 핵심 트리거
VOL_TOTAL_INDICATORS = 7        # 분모 고정 (기존 지표 수)
BUY_VOL_NORMAL_BONUS  = 2       # 일반 거래량 증가 (+20% 이상) → score +2
BUY_VOL_SURGE_BONUS   = 3       # 거래량 폭증   (+100% 이상) → score +3
VOL_INCREASE_RATIO    = 1.20    # 일반 증가 기준 (직전 대비 ×1.20)
VOL_SURGE_RATIO       = 2.00    # 폭증 기준      (직전 대비 ×2.00)

# ── BUY 진입 금지 임계 ────────────────────────────────────────
BUY_BLOCK_SELL_SCORE  = 5       # SELL_SCORE ≥ 5 이면 신규 매수 금지
                                 # (기존 즉시매도 임계 6과 별개)

# ── SELL SCORE 가중치 (항목별) ────────────────────────────────
SELL_W_VWAP_BREAK       = 4   # VWAP 이탈
SELL_W_OBV_BEAR         = 3   # OBV 하락전환
SELL_W_STRENGTH_DROP    = 3   # 체결강도 급락
SELL_W_VOL_SHRINK       = 2   # 거래량 감소
SELL_W_BUYER_EXIT       = 3   # 매수주체 이탈 (MA5 이탈)
SELL_W_PROGRAM_SELL     = 2   # 프로그램 매도우위 (ATR 급락)
SELL_W_BB_UPPER_TOUCH   = 3   # 볼린저 상단 터치
SELL_W_BB_UPPER_FAIL    = 4   # 볼린저 상단 돌파 실패
SELL_W_MA20_BREAK       = 3   # 20MA 이탈

SELL_SCORE_THRESHOLD    = 6   # 이 이상이면 즉시 매도

# ── 5분봉 돌파 가점 ────────────────────────────────────────────
BREAKOUT_EARLY_BONUS  = 0.10   # 초기 돌파: 직전봉 고가 돌파 + 거래량 1.5배
BREAKOUT_STRONG_BONUS = 0.20   # 강한 돌파: 당일 고가 돌파 + 거래량 2배
BREAKOUT_SURGE_BONUS  = 0.30   # 폭발 돌파: 당일 고가 +0.5% + 거래량 3배 + 체결강도>150

# ── 추격매수 차단 임계 ─────────────────────────────────────────
CHASE_BLOCK_15MIN_PCT = 4.0    # 최근 15분 상승률 > +4% → 추격 차단
CHASE_BLOCK_5MIN_PCT  = 2.0    # 최근 5분 상승률 > +2% → 추격 차단
CHASE_BLOCK_CONSEC    = 3      # 직전 N개 봉 연속 양봉 → 추격 차단


class IndicatorValidator:
    """
    candles 리스트를 받아 BUY/SELL 신호 판단.

    returns: {
        score,          BUY 신호 지표 수 (0~7, 하위 호환)
        buy_score_norm, BUY SCORE 정규화값 (0.0~1.0) ★신규
        sell_score,     SELL SCORE 가중치 합산 (0~27) ★변경
        sell_score_raw, SELL 신호 지표 수 (0~7, 하위 호환)
        sell_urgent,    True if sell_score ≥ SELL_SCORE_THRESHOLD
        signals,        {지표명: True/False}
        detail,         {지표명: {value, signal, reason}}
        sell_detail,    {항목: {weight, triggered, reason}} ★신규
        trend_score,    추세강도 점수 (0~100)
        strong_trend,   True if trend_score ≥ 60
        trend_detail,
        reason,
    }
    """

    def validate(self, candles: list[dict], vwap: float = 0.0,
                 prev_volume: float = 0.0, strength: float = 0.0) -> dict:
        """
        Args:
            candles:     일봉 OHLCV 리스트 (최소 65개)
            vwap:        현재 VWAP (0이면 MA20으로 대체)
            prev_volume: 직전 캔들 거래량 (거래량 감소 판단용)
            strength:    체결강도 (0이면 계산 생략)
        """
        if len(candles) < 65:
            return {
                "score": 0, "buy_score_norm": 0.0,
                "sell_score": 0, "sell_score_raw": 0, "sell_urgent": False,
                "signals": {}, "detail": {}, "sell_detail": {},
                "trend_score": 0, "strong_trend": False, "trend_detail": {},
                "reason": "데이터 부족",
            }

        df = pd.DataFrame(candles)
        c  = df["close"].astype(float)
        h  = df["high"].astype(float)
        lo = df["low"].astype(float)
        v  = df["volume"].astype(float)

        # ── BUY 지표 계산 (7개) ────────────────────────────────
        results = {}
        results["MA"]     = self._check_ma(c)
        results["RSI"]    = self._check_rsi(c)
        results["MACD"]   = self._check_macd(c)
        results["BB"]     = self._check_bb(c)
        results["ATR"]    = self._check_atr(c, h, lo)
        results["OBV"]    = self._check_obv(c, v)
        results["BB_SQZ"] = self._check_bb_squeeze(c)

        buy_count  = sum(1 for r in results.values() if r["signal"] == "BUY")
        sell_raw   = sum(1 for r in results.values() if r["signal"] == "SELL")
        signals    = {k: (r["signal"] == "BUY") for k, r in results.items()}

        # ── SELL SCORE 가중치 시스템 ───────────────────────────
        sell_result = self._calc_sell_score(
            c, h, lo, v, results, vwap, prev_volume, strength
        )
        sell_score  = sell_result["total"]
        sell_urgent = sell_score >= SELL_SCORE_THRESHOLD

        # ── 거래량 보너스 계산 ★ ──────────────────────────────
        # 직전 봉 대비 현재 봉 거래량 비교 (prev_volume: 직전봉, v[-1]: 현재봉)
        cur_vol  = float(v.iloc[-1])
        vol_surge  = (prev_volume > 0) and (cur_vol >= prev_volume * VOL_SURGE_RATIO)
        vol_normal = (prev_volume > 0) and (cur_vol >= prev_volume * VOL_INCREASE_RATIO) and not vol_surge
        vol_any    = vol_surge or vol_normal   # 거래량 증가 여부 (필수 조건 판단용)

        if vol_surge:
            vol_bonus_count = BUY_VOL_SURGE_BONUS    # +3
            vol_label       = f"폭증(×{cur_vol/prev_volume:.1f})+{BUY_VOL_SURGE_BONUS}pt"
        elif vol_normal:
            vol_bonus_count = BUY_VOL_NORMAL_BONUS   # +2
            vol_label       = f"증가(×{cur_vol/prev_volume:.1f})+{BUY_VOL_NORMAL_BONUS}pt"
        else:
            vol_bonus_count = 0
            vol_ratio_str   = f"×{cur_vol/prev_volume:.1f}" if prev_volume > 0 else "N/A"
            vol_label       = f"없음({vol_ratio_str})"

        # ★ BUY SCORE 정규화 (0.0~1.0) — 거래량 보너스 포함
        # 분모를 VOL_TOTAL_INDICATORS(=7) 고정하여 가중치 비율 일관성 유지
        raw_score      = buy_count + vol_bonus_count
        buy_score_norm = round(min(raw_score / VOL_TOTAL_INDICATORS, 1.0), 2)

        # ── VWAP 위/아래 판단 ★ ──────────────────────────────
        price_cur  = float(c.iloc[-1])
        ma20_cur   = float(c.rolling(20).mean().iloc[-1])
        vwap_ref   = vwap if vwap > 0 else ma20_cur
        vwap_above = price_cur >= vwap_ref * 0.999   # VWAP 위 (필수 진입 조건)

        # ── 추세강도 ────────────────────────────────────────────
        trend_result = self._calc_trend_score(c, h, lo, v)
        trend_score  = trend_result["trend_score"]
        strong_trend = trend_score >= STRONG_TREND_THRESHOLD

        # ── 진입 가능 여부 플래그 ★ ──────────────────────────
        # 거래량 증가 없음  → 진입 금지 (buy_score 무관)
        # VWAP 아래        → 진입 금지
        # SELL_SCORE ≥ 5  → 진입 금지 (strategy_manager 단에서도 재확인)
        buy_blocked_vol  = not vol_any        # True: 거래량 증가 없어서 차단
        buy_blocked_vwap = not vwap_above     # True: VWAP 아래라서 차단
        buy_blocked_sell = sell_score >= BUY_BLOCK_SELL_SCORE  # True: SELL_SCORE ≥ 5

        # ── [BUY DETAIL] 로그 출력 ★ ─────────────────────────
        rsi_val  = results["RSI"]["value"].get("RSI", 0)
        obv_sig  = results["OBV"]["signal"]
        bb_sig   = results["BB"]["signal"]
        ema_val  = results["MA"]["value"].get("MA20", 0)   # MA20을 EMA 대용
        buy_names  = [k for k, r in results.items() if r["signal"] == "BUY"]
        sell_names = [k for k, r in results.items() if r["signal"] == "SELL"]

        _block_tags = []
        if buy_blocked_vol:  _block_tags.append("⛔거래량없음")
        if buy_blocked_vwap: _block_tags.append("⛔VWAP아래")
        if buy_blocked_sell: _block_tags.append(f"⛔SELL≥{BUY_BLOCK_SELL_SCORE}")
        _block_str = " ".join(_block_tags) if _block_tags else "✅진입가능"

        entry_tag  = ("🚀본진입" if buy_score_norm >= BUY_SCORE_FULL else
                      "⚡조기진입" if buy_score_norm >= BUY_SCORE_EARLY else "")
        logger.info(
            f"[BUY DETAIL] "
            f"거래량점수={vol_label} | "
            f"VWAP={'위✅' if vwap_above else '아래⛔'}({price_cur:.0f}vs{vwap_ref:.0f}) | "
            f"EMA(MA20)={ema_val:.0f} | "
            f"RSI={rsi_val:.1f} | "
            f"OBV={obv_sig} | "
            f"BB={bb_sig} | "
            f"최종BUY_SCORE={buy_score_norm:.2f}{entry_tag}"
            f"(기본{buy_count}/7+거래량{vol_bonus_count}) | "
            f"SELL_SCORE={sell_score} | {_block_str}"
        )

        reason = (f"BUY={buy_score_norm:.2f}{entry_tag} [{', '.join(buy_names) or '없음'}] | "
                  f"거래량={vol_label} | VWAP={'위' if vwap_above else '아래⛔'} | "
                  f"SELLSCORE={sell_score}{'🚨즉시매도' if sell_urgent else (f'⛔매수금지' if buy_blocked_sell else '')} | "
                  f"추세={trend_score:.0f}/100{'★' if strong_trend else ''}")

        return {
            # ── BUY 관련 ──
            "score":           buy_count,           # 하위 호환 (정수, 거래량 보너스 제외)
            "buy_score_norm":  buy_score_norm,       # 정규화 (거래량 보너스 포함) ★
            "early_entry":     buy_score_norm >= BUY_SCORE_EARLY,
            "full_entry":      buy_score_norm >= BUY_SCORE_FULL,
            # ── 거래량/VWAP 필수 조건 ★ ──
            "vol_any":         vol_any,              # 거래량 증가 여부
            "vol_surge":       vol_surge,            # 거래량 폭증 여부
            "vol_bonus":       vol_bonus_count,      # 가산 점수
            "vol_label":       vol_label,
            "vwap_above":      vwap_above,           # VWAP 위 여부
            "buy_blocked_vol": buy_blocked_vol,      # True→거래량 없어서 진입 차단
            "buy_blocked_vwap":buy_blocked_vwap,     # True→VWAP 아래라서 진입 차단
            "buy_blocked_sell":buy_blocked_sell,     # True→SELL_SCORE≥5 진입 차단
            # ── SELL 관련 ──
            "sell_score":      sell_score,           # 가중치 합산 ★
            "sell_score_raw":  sell_raw,             # 지표 수 (하위 호환)
            "sell_urgent":     sell_urgent,          # True → 즉시 매도
            "sell_detail":     sell_result["detail"],
            # ── 공통 ──
            "signals":         signals,
            "detail":          results,
            "trend_score":     round(trend_score, 1),
            "strong_trend":    strong_trend,
            "trend_detail":    trend_result["detail"],
            "reason":          reason,
        }

    # ══════════════════════════════════════════════════════════
    # ★ SELL SCORE 가중치 시스템 (핵심 신규)
    # ══════════════════════════════════════════════════════════
    def _calc_sell_score(self, c: pd.Series, h: pd.Series, lo: pd.Series,
                          v: pd.Series, buy_results: dict,
                          vwap: float, prev_vol: float, strength: float) -> dict:
        """
        9개 항목 가중치 합산 → sell_score 반환.
        sell_score ≥ 6 → 즉시 매도 신호.
        """
        detail = {}
        total  = 0

        price  = float(c.iloc[-1])
        ma20   = float(c.rolling(20).mean().iloc[-1])
        ma5    = float(c.rolling(5).mean().iloc[-1])
        cur_vol= float(v.iloc[-1])

        # ① VWAP 이탈 (+4)
        # vwap=0이면 MA20으로 대체
        vwap_ref = vwap if vwap > 0 else ma20
        vwap_break = price < vwap_ref * 0.999
        w1 = SELL_W_VWAP_BREAK if vwap_break else 0
        total += w1
        detail["VWAP이탈"] = {
            "weight": SELL_W_VWAP_BREAK, "triggered": vwap_break, "score": w1,
            "reason": f"현재{price:.0f} < VWAP/MA20={vwap_ref:.0f}" if vwap_break else "VWAP 위",
        }

        # ② OBV 하락전환 (+3)
        obv_bear = buy_results.get("OBV", {}).get("signal") == "SELL"
        w2 = SELL_W_OBV_BEAR if obv_bear else 0
        total += w2
        detail["OBV하락"] = {
            "weight": SELL_W_OBV_BEAR, "triggered": obv_bear, "score": w2,
            "reason": "OBV MA5 < MA20 하락" if obv_bear else "OBV 정상",
        }

        # ③ 체결강도 급락 (+3)
        # strength: 100 기준 (100이상=매수우위, 미만=매도우위)
        strength_drop = (strength > 0) and (strength < 90)
        w3 = SELL_W_STRENGTH_DROP if strength_drop else 0
        total += w3
        detail["체결강도급락"] = {
            "weight": SELL_W_STRENGTH_DROP, "triggered": strength_drop, "score": w3,
            "reason": f"체결강도={strength:.1f}(<90)" if strength_drop else
                      (f"체결강도={strength:.1f}" if strength > 0 else "체결강도 미제공"),
        }

        # ④ 거래량 감소 (+2)
        # 현재 거래량이 직전 대비 -30% 이상 감소
        vol_shrink = (prev_vol > 0) and (cur_vol < prev_vol * 0.70)
        w4 = SELL_W_VOL_SHRINK if vol_shrink else 0
        total += w4
        detail["거래량감소"] = {
            "weight": SELL_W_VOL_SHRINK, "triggered": vol_shrink, "score": w4,
            "reason": f"현재거래량{cur_vol:,.0f} < 직전{prev_vol:,.0f}×0.7" if vol_shrink else "거래량 정상",
        }

        # ⑤ 매수주체 이탈 (+3)
        # 현재가가 MA5 아래로 이탈 (주도세력 이탈 신호)
        buyer_exit = price < ma5 * 0.999
        w5 = SELL_W_BUYER_EXIT if buyer_exit else 0
        total += w5
        detail["매수주체이탈"] = {
            "weight": SELL_W_BUYER_EXIT, "triggered": buyer_exit, "score": w5,
            "reason": f"현재{price:.0f} < MA5={ma5:.0f}" if buyer_exit else "MA5 위",
        }

        # ⑥ 프로그램 매도우위 (+2)
        # ATR 급락: 단기 ATR이 장기 ATR보다 현저히 낮음 (변동성 수축 + 하락)
        prev_c  = c.shift(1)
        tr      = pd.concat([h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1).max(axis=1)
        atr5    = float(tr.rolling(5).mean().iloc[-1])
        atr14   = float(tr.rolling(14).mean().iloc[-1])
        prog_sell = (atr5 < atr14 * 0.75) and (price < ma5)
        w6 = SELL_W_PROGRAM_SELL if prog_sell else 0
        total += w6
        detail["프로그램매도"] = {
            "weight": SELL_W_PROGRAM_SELL, "triggered": prog_sell, "score": w6,
            "reason": f"ATR5={atr5:.0f} < ATR14×0.75={atr14*0.75:.0f}" if prog_sell else "ATR 정상",
        }

        # ⑦ 볼린저 상단 터치 (+3)
        std   = c.rolling(20).std()
        upper = c.rolling(20).mean() + 2 * std
        up_cur = float(upper.iloc[-1])
        bb_touch = price >= up_cur * 0.99
        w7 = SELL_W_BB_UPPER_TOUCH if bb_touch else 0
        total += w7
        detail["BB상단터치"] = {
            "weight": SELL_W_BB_UPPER_TOUCH, "triggered": bb_touch, "score": w7,
            "reason": f"현재{price:.0f} ≥ BB상단{up_cur:.0f}×0.99" if bb_touch else "BB 상단 미달",
        }

        # ⑧ 볼린저 상단 돌파 실패 (+4)
        # 전봉이 상단을 넘었으나 현재봉이 상단 아래로 내려온 경우
        prev_price = float(c.iloc[-2])
        up_prev    = float(upper.iloc[-2])
        bb_fail = (prev_price >= up_prev * 0.99) and (price < up_cur * 0.98)
        # 단순 터치와 중복 방지: 터치 없을 때만
        if not bb_touch and bb_fail:
            w8 = SELL_W_BB_UPPER_FAIL
        else:
            bb_fail = False
            w8 = 0
        total += w8
        detail["BB돌파실패"] = {
            "weight": SELL_W_BB_UPPER_FAIL, "triggered": bb_fail, "score": w8,
            "reason": f"전봉{prev_price:.0f}≥상단, 현재{price:.0f}<상단" if bb_fail else "BB 돌파 실패 아님",
        }

        # ⑨ 20MA 이탈 (+3)
        ma20_break = price < ma20 * 0.998
        w9 = SELL_W_MA20_BREAK if ma20_break else 0
        total += w9
        detail["MA20이탈"] = {
            "weight": SELL_W_MA20_BREAK, "triggered": ma20_break, "score": w9,
            "reason": f"현재{price:.0f} < MA20={ma20:.0f}×0.998" if ma20_break else "MA20 위",
        }

        triggered = [k for k, d in detail.items() if d["triggered"]]
        logger.debug(
            f"SELL_SCORE={total} (임계={SELL_SCORE_THRESHOLD}) "
            f"발동항목: {triggered or '없음'}"
        )

        return {"total": total, "detail": detail}

    # ══════════════════════════════════════════════════════════
    # ★ 볼린저 압축 지표 (BB_SQZ) — 돌파 예고 신호
    # ══════════════════════════════════════════════════════════
    def _check_bb_squeeze(self, c: pd.Series) -> dict:
        """
        볼린저 밴드폭(BW)이 최근 20일 최솟값 근처 → 압축(돌파 임박).
        BW < BW_20일_평균 × 0.7 이면 압축 상태 → BUY 신호.
        (상단 터치 직전 '좋은 자리'에서 선점하기 위한 조기 진입 신호)
        """
        ma20 = c.rolling(20).mean()
        std  = c.rolling(20).std()
        upper = ma20 + 2 * std
        lower = ma20 - 2 * std
        bw    = (upper - lower) / ma20 * 100

        bw_cur  = float(bw.iloc[-1])
        bw_avg  = float(bw.rolling(20).mean().iloc[-1])
        bw_min  = float(bw.rolling(20).min().iloc[-1])

        squeezed    = bw_cur <= bw_avg * 0.70         # 압축: 밴드폭 평균의 70% 이하
        near_min    = bw_cur <= bw_min * 1.10          # 20일 최솟값 근처
        bb_squeeze  = squeezed or near_min
        signal      = "BUY" if bb_squeeze else "HOLD"

        return {
            "signal": signal,
            "value":  {"BW현재": round(bw_cur, 1), "BW평균": round(bw_avg, 1),
                       "BW최소": round(bw_min, 1)},
            "reason": (f"밴드폭={bw_cur:.1f}% {'압축(돌파예고)' if bb_squeeze else '정상'}"),
        }

    # ══════════════════════════════════════════════════════════
    # ★ 추세강도 점수 (0~100)
    # ══════════════════════════════════════════════════════════
    def _calc_trend_score(self, c: pd.Series, h: pd.Series,
                          lo: pd.Series, v: pd.Series) -> dict:
        detail = {}
        total  = 0.0

        # A. RS 상대강도
        ret20 = (float(c.iloc[-1]) / float(c.iloc[-20]) - 1) * 100
        ret60 = (float(c.iloc[-1]) / float(c.iloc[-60]) - 1) * 100
        if ret20 > 10:   rs_score = 25.0
        elif ret20 > 5:  rs_score = 20.0
        elif ret20 > 2:  rs_score = 15.0
        elif ret20 > 0:  rs_score = 10.0
        elif ret20 > -3: rs_score = 5.0
        else:            rs_score = 0.0
        if ret20 > ret60:
            rs_score = min(25.0, rs_score + 5.0)
        total += rs_score
        detail["A_RS"] = {
            "score": round(rs_score, 1), "ret20": round(ret20, 2), "ret60": round(ret60, 2),
            "reason": f"20일={ret20:.1f}% 60일={ret60:.1f}% {'가속↑' if ret20 > ret60 else '감속↓'}",
        }

        # B. ADX
        adx_data = self._calc_adx(c, h, lo, period=14)
        adx      = adx_data["adx"]
        bullish  = adx_data["plus_di"] > adx_data["minus_di"]
        if bullish and adx >= ADX_VERY_STRONG:    adx_score = 25.0
        elif bullish and adx >= ADX_STRONG_LEVEL: adx_score = 20.0
        elif bullish and adx >= 15:               adx_score = 10.0
        elif not bullish and adx >= ADX_STRONG_LEVEL: adx_score = 0.0
        else:                                     adx_score = 5.0
        total += adx_score
        detail["B_ADX"] = {
            "score": round(adx_score, 1), "adx": round(adx, 1),
            "plus_di": round(adx_data["plus_di"], 1), "minus_di": round(adx_data["minus_di"], 1),
            "reason": f"ADX={adx:.1f} {'상승↑' if bullish else '하락↓'}",
        }

        # C. 모멘텀 가속도
        daily_ret = c.pct_change()
        mom5  = float(daily_ret.iloc[-5:].mean()) * 100
        mom20 = float(daily_ret.iloc[-20:].mean()) * 100
        if mom5 > 0.5 and mom5 > mom20 * 1.5:   mom_score = 25.0
        elif mom5 > 0.2 and mom5 > mom20:        mom_score = 18.0
        elif mom5 > 0:                           mom_score = 10.0
        elif mom5 > -0.2:                        mom_score = 5.0
        else:                                    mom_score = 0.0
        total += mom_score
        detail["C_Momentum"] = {
            "score": round(mom_score, 1), "mom5": round(mom5, 3), "mom20": round(mom20, 3),
            "reason": f"5일={mom5:.3f}% 20일={mom20:.3f}% {'가속↑' if mom5 > mom20 else '감속↓'}",
        }

        # D. 고점 경신 근접도
        price   = float(c.iloc[-1])
        high20  = float(h.iloc[-20:].max())
        nearness= price / high20
        if nearness >= 1.0:    near_score = 25.0
        elif nearness >= 0.98: near_score = 22.0
        elif nearness >= 0.95: near_score = 15.0
        elif nearness >= 0.90: near_score = 8.0
        else:                  near_score = 0.0
        total += near_score
        detail["D_HighProximity"] = {
            "score": round(near_score, 1), "price": round(price), "high20": round(high20),
            "nearness": round(nearness * 100, 1),
            "reason": f"현재={price:.0f} 20일고점={high20:.0f} {nearness*100:.1f}%{'★신고점' if nearness >= 1.0 else ''}",
        }

        return {"trend_score": round(min(total, 100.0), 1), "detail": detail}

    # ══════════════════════════════════════════════════════════
    # ADX / DMI
    # ══════════════════════════════════════════════════════════
    def _calc_adx(self, c: pd.Series, h: pd.Series,
                  lo: pd.Series, period: int = 14) -> dict:
        prev_c    = c.shift(1)
        tr        = pd.concat([h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1).max(axis=1)
        up_move   = h.diff()
        down_move = (-lo.diff())
        plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        atr_s     = tr.rolling(period).mean()
        plus_di_s = plus_dm.rolling(period).mean()
        minus_di_s= minus_dm.rolling(period).mean()
        plus_di   = float((plus_di_s / atr_s.replace(0, 1e-10) * 100).iloc[-1])
        minus_di  = float((minus_di_s / atr_s.replace(0, 1e-10) * 100).iloc[-1])
        plus_di_series  = plus_di_s / atr_s.replace(0, 1e-10) * 100
        minus_di_series = minus_di_s / atr_s.replace(0, 1e-10) * 100
        di_sum_series   = plus_di_series + minus_di_series
        dx_series = ((plus_di_series - minus_di_series).abs()
                     / di_sum_series.replace(0, 1e-10) * 100)
        adx = float(dx_series.rolling(period).mean().iloc[-1])
        return {
            "adx":      float(adx      if not np.isnan(adx)      else 0.0),
            "plus_di":  float(plus_di  if not np.isnan(plus_di)  else 0.0),
            "minus_di": float(minus_di if not np.isnan(minus_di) else 0.0),
        }

    # ── 1. MA ──────────────────────────────────────────────────
    def _check_ma(self, c: pd.Series) -> dict:
        ma5  = float(c.rolling(5).mean().iloc[-1])
        ma20 = float(c.rolling(20).mean().iloc[-1])
        ma60 = float(c.rolling(60).mean().iloc[-1])
        price= float(c.iloc[-1])
        bull = (ma5 > ma20 > ma60) and (price > ma5)
        bear = (ma5 < ma20 < ma60) and (price < ma5)
        signal = "BUY" if bull else ("SELL" if bear else "HOLD")
        return {
            "signal": signal,
            "value":  {"MA5": round(ma5), "MA20": round(ma20), "MA60": round(ma60)},
            "reason": f"정배열({'✅' if bull else '❌'}) MA5={ma5:.0f} MA20={ma20:.0f} MA60={ma60:.0f}",
        }

    # ── 2. RSI ─────────────────────────────────────────────────
    def _check_rsi(self, c: pd.Series) -> dict:
        delta    = c.diff()
        gain     = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss     = (-delta).clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        rsi      = float(100 - 100 / (1 + gain / loss.replace(0, 1e-10)).iloc[-1])
        prev_rsi = float(100 - 100 / (1 + gain / loss.replace(0, 1e-10)).iloc[-2])
        oversold_cross = (prev_rsi <= 35) and (rsi > 35)
        mid_up         = (rsi > 40) and (rsi < 60) and (rsi > prev_rsi)
        overbought     = rsi >= 70
        signal = "BUY" if (oversold_cross or mid_up) else ("SELL" if overbought else "HOLD")
        return {
            "signal": signal,
            "value":  {"RSI": round(rsi, 1), "prev_RSI": round(prev_rsi, 1)},
            "reason": (f"RSI={rsi:.1f} "
                       f"({'과매도탈출' if oversold_cross else '상승중' if mid_up else '과매수' if overbought else '중립'})"),
        }

    # ── 3. MACD ────────────────────────────────────────────────
    def _check_macd(self, c: pd.Series) -> dict:
        ema12    = c.ewm(span=12, adjust=False).mean()
        ema26    = c.ewm(span=26, adjust=False).mean()
        macd     = ema12 - ema26
        signal_l = macd.ewm(span=9, adjust=False).mean()
        hist     = macd - signal_l
        mc  = float(macd.iloc[-1]);    mp  = float(macd.iloc[-2])
        sc  = float(signal_l.iloc[-1]); sp  = float(signal_l.iloc[-2])
        hc  = float(hist.iloc[-1]);    hp  = float(hist.iloc[-2])
        golden  = (mp <= sp) and (mc > sc)
        hist_up = (hp < 0) and (hc > hp)
        dead    = (mp >= sp) and (mc < sc)
        signal  = "BUY" if (golden or (mc > sc and hist_up)) else ("SELL" if dead else "HOLD")
        return {
            "signal": signal,
            "value":  {"MACD": round(mc, 2), "Signal": round(sc, 2), "Hist": round(hc, 2)},
            "reason": (f"MACD={mc:.2f}/Sig={sc:.2f} "
                       f"({'골든크로스' if golden else '히스토그램↑' if hist_up else '데드크로스' if dead else '중립'})"),
        }

    # ── 4. 볼린저 밴드 ─────────────────────────────────────────
    def _check_bb(self, c: pd.Series) -> dict:
        ma20  = c.rolling(20).mean()
        std   = c.rolling(20).std()
        upper = ma20 + 2 * std
        lower = ma20 - 2 * std
        bw    = (upper - lower) / ma20 * 100
        price   = float(c.iloc[-1]);  prev = float(c.iloc[-2])
        lo_cur  = float(lower.iloc[-1]); lo_prev = float(lower.iloc[-2])
        up_cur  = float(upper.iloc[-1]); ma_cur  = float(ma20.iloc[-1])
        bw_cur  = float(bw.iloc[-1])
        touch_lower      = (prev <= lo_prev) and (price > lo_cur)
        above_mid_expand = (price > ma_cur) and (bw_cur > float(bw.rolling(10).mean().iloc[-1]))
        touch_upper      = price >= up_cur * 0.99
        signal = "BUY" if (touch_lower or above_mid_expand) else ("SELL" if touch_upper else "HOLD")
        return {
            "signal": signal,
            "value":  {"상단": round(up_cur), "중심": round(ma_cur),
                       "하단": round(lo_cur), "밴드폭": round(bw_cur, 1)},
            "reason": (f"현재={price:.0f} 하단={lo_cur:.0f} 상단={up_cur:.0f} "
                       f"({'하단반등' if touch_lower else '추세강화' if above_mid_expand else '상단과매수' if touch_upper else '중립'})"),
        }

    # ── 5. ATR ─────────────────────────────────────────────────
    def _check_atr(self, c: pd.Series, h: pd.Series, lo: pd.Series) -> dict:
        prev_c  = c.shift(1)
        tr      = pd.concat([h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1).max(axis=1)
        atr14   = float(tr.rolling(14).mean().iloc[-1])
        atr5    = float(tr.rolling(5).mean().iloc[-1])
        price   = float(c.iloc[-1])
        atr_pct = atr14 / price * 100
        vol_expand = atr5 > atr14 * 1.1
        high_vol   = atr_pct > 5.0
        signal = "BUY" if (vol_expand and not high_vol) else ("SELL" if high_vol else "HOLD")
        return {
            "signal": signal,
            "value":  {"ATR14": round(atr14), "ATR5": round(atr5), "ATR%": round(atr_pct, 2)},
            "reason": (f"ATR14={atr14:.0f}({atr_pct:.1f}%) "
                       f"{'변동성확대↑' if vol_expand else '고변동위험' if high_vol else '보통'}"),
        }

    # ── 6. OBV ─────────────────────────────────────────────────
    def _check_obv(self, c: pd.Series, v: pd.Series) -> dict:
        direction = np.sign(c.diff()).fillna(0)
        obv       = (direction * v).cumsum()
        obv_ma5   = obv.rolling(5).mean()
        obv_ma20  = obv.rolling(20).mean()
        obv_cur  = float(obv.iloc[-1])
        om5      = float(obv_ma5.iloc[-1]);  om20     = float(obv_ma20.iloc[-1])
        prev_om5 = float(obv_ma5.iloc[-2])
        obv_bull = (om5 > om20) and (om5 > prev_om5)
        obv_bear = (om5 < om20) and (om5 < prev_om5)
        signal = "BUY" if obv_bull else ("SELL" if obv_bear else "HOLD")
        return {
            "signal": signal,
            "value":  {"OBV": round(obv_cur), "OBV_MA5": round(om5), "OBV_MA20": round(om20)},
            "reason": (f"OBV_MA5={om5:,.0f} MA20={om20:,.0f} "
                       f"({'매집↑' if obv_bull else '분산↓' if obv_bear else '중립'})"),
        }

    # ══════════════════════════════════════════════════════════
    # ★ 5분봉 돌파 가점 + 추격매수 차단 (신규)
    # ══════════════════════════════════════════════════════════
    def validate_5min(self, candles_5m: list[dict],
                      today_high: float = 0.0,
                      strength: float = 0.0) -> dict:
        """
        5분봉 리스트(오래된→최신 순)를 받아 돌파 가점과 추격 차단 여부를 반환.

        Args:
            candles_5m : [{time, open, high, low, close, volume}, ...] 최소 5개
            today_high : 당일 고가 (일봉에서 전달)
            strength   : 현재 체결강도

        Returns: {
            breakout_bonus   : float  — BUY_SCORE에 직접 가산할 값 (0/0.10/0.20/0.30)
            breakout_label   : str    — "없음" / "초기돌파" / "강한돌파" / "폭발돌파"
            chase_blocked    : bool   — True → 추격매수 차단
            chase_reason     : str    — 차단 사유
            rise_15m_pct     : float  — 최근 15분 상승률(%)
            rise_5m_pct      : float  — 최근 5분 상승률(%)
            consec_bull      : int    — 직전 연속 양봉 수
            vol_ratio_5m     : float  — 현재봉 거래량 / 직전봉 거래량
            vol_avg4_ratio   : float  — 현재봉 거래량 / 직전4봉 평균 거래량
        }
        """
        _empty = {
            "breakout_bonus": 0.0, "breakout_label": "없음",
            "chase_blocked": False, "chase_reason": "",
            "rise_15m_pct": 0.0, "rise_5m_pct": 0.0,
            "consec_bull": 0, "vol_ratio_5m": 0.0, "vol_avg4_ratio": 0.0,
        }
        if len(candles_5m) < 5:
            return _empty

        cur  = candles_5m[-1]
        prev = candles_5m[-2]

        cur_close  = float(cur["close"])
        cur_high   = float(cur["high"])
        cur_vol    = float(cur["volume"])
        prev_high  = float(prev["high"])
        prev_vol   = float(prev["volume"]) if prev["volume"] > 0 else 1.0

        # ── 거래량 비율 ──────────────────────────────────────
        vol_ratio_5m  = cur_vol / prev_vol if prev_vol > 0 else 0.0
        avg4_vol      = sum(float(candles_5m[-5+i]["volume"]) for i in range(4)) / 4
        vol_avg4_ratio= cur_vol / avg4_vol if avg4_vol > 0 else 0.0

        # ── 최근 상승률 ──────────────────────────────────────
        # 5분 전 종가 = candles_5m[-2].close
        # 15분 전 종가 = candles_5m[-4].close (없으면 -2)
        close_5m_ago  = float(candles_5m[-2]["close"]) if cur_close > 0 else cur_close
        close_15m_ago = float(candles_5m[-4]["close"]) if len(candles_5m) >= 4 else close_5m_ago
        rise_5m_pct   = (cur_close / close_5m_ago  - 1) * 100 if close_5m_ago  > 0 else 0.0
        rise_15m_pct  = (cur_close / close_15m_ago - 1) * 100 if close_15m_ago > 0 else 0.0

        # ── 연속 양봉 수 ────────────────────────────────────
        consec_bull = 0
        for c5 in reversed(candles_5m[:-1]):   # 직전봉부터 역순
            if float(c5["close"]) > float(c5["open"]):
                consec_bull += 1
            else:
                break

        # ── 추격매수 차단 판단 ★ ────────────────────────────
        chase_blocked = False
        chase_reason  = ""
        if rise_15m_pct > CHASE_BLOCK_15MIN_PCT:
            chase_blocked = True
            chase_reason  = (f"15분상승률={rise_15m_pct:+.2f}% > +{CHASE_BLOCK_15MIN_PCT}%")
        elif rise_5m_pct > CHASE_BLOCK_5MIN_PCT:
            chase_blocked = True
            chase_reason  = (f"5분상승률={rise_5m_pct:+.2f}% > +{CHASE_BLOCK_5MIN_PCT}%")
        elif consec_bull >= CHASE_BLOCK_CONSEC:
            chase_blocked = True
            chase_reason  = (f"연속양봉={consec_bull}개 ≥ {CHASE_BLOCK_CONSEC}개")

        if chase_blocked:
            logger.info(
                f"[과열 추격매수 차단] "
                f"15분상승률={rise_15m_pct:+.2f}% | "
                f"5분상승률={rise_5m_pct:+.2f}% | "
                f"연속양봉수={consec_bull} | "
                f"사유={chase_reason}"
            )

        # ── 돌파 가점 판단 ★ ─────────────────────────────────
        # 추격 차단 상태에서도 가점은 계산 (차단은 strategy_manager에서 최종 처리)
        breakout_bonus = 0.0
        breakout_label = "없음"

        # 폭발 돌파 (가장 강한 조건 → 먼저 체크)
        surge_price   = today_high > 0 and cur_high > today_high * 1.005
        surge_vol     = vol_avg4_ratio >= 3.0
        surge_str     = strength > 150
        if surge_price and surge_vol and surge_str:
            breakout_bonus = BREAKOUT_SURGE_BONUS
            breakout_label = (
                f"폭발돌파+{BREAKOUT_SURGE_BONUS}"
                f"(당일고가+0.5%↑,거래량×{vol_avg4_ratio:.1f},체결강도{strength:.0f})"
            )
        # 강한 돌파
        elif today_high > 0 and cur_high > today_high and vol_avg4_ratio >= 2.0:
            breakout_bonus = BREAKOUT_STRONG_BONUS
            breakout_label = (
                f"강한돌파+{BREAKOUT_STRONG_BONUS}"
                f"(당일고가돌파,거래량×{vol_avg4_ratio:.1f})"
            )
        # 초기 돌파
        elif cur_high > prev_high and vol_ratio_5m >= 1.5:
            breakout_bonus = BREAKOUT_EARLY_BONUS
            breakout_label = (
                f"초기돌파+{BREAKOUT_EARLY_BONUS}"
                f"(직전고가돌파,거래량×{vol_ratio_5m:.1f})"
            )

        return {
            "breakout_bonus":  breakout_bonus,
            "breakout_label":  breakout_label,
            "chase_blocked":   chase_blocked,
            "chase_reason":    chase_reason,
            "rise_15m_pct":    round(rise_15m_pct, 2),
            "rise_5m_pct":     round(rise_5m_pct, 2),
            "consec_bull":     consec_bull,
            "vol_ratio_5m":    round(vol_ratio_5m, 2),
            "vol_avg4_ratio":  round(vol_avg4_ratio, 2),
        }
