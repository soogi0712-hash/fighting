"""
보조지표 검증 레이어
=====================
피라미딩 진입 전 보조지표로 신호를 검증합니다.
각 지표가 "매수" 신호를 보낼 때 +1점 카운트 → indicator_score 반환

사용 지표 (총 6개):
  1. MA   — 이동평균 배열 (MA5 > MA20 > MA60)
  2. RSI  — 과매도 탈출 (30→50 회복 구간)
  3. MACD — MACD > Signal, 히스토그램 양전환
  4. BB   — 볼린저밴드 하단 터치 후 반등
  5. ATR  — 변동성 확대 (추세 출현 신호)
  6. OBV  — 거래량 증가 동반 (매집 신호)
"""

import numpy as np
import pandas as pd
from utils.logger import get_logger

logger = get_logger("IndicatorValidator")


class IndicatorValidator:
    """
    candles 리스트를 받아 각 보조지표 매수 신호 여부 판단
    returns: score(0~6), detail dict
    """

    def validate(self, candles: list[dict]) -> dict:
        """
        returns:
          score      : 매수 신호 보조지표 개수 (0~6)
          signals    : {지표명: True/False}
          detail     : {지표명: {value, signal, reason}}
          sell_score : 매도 신호 보조지표 개수
        """
        if len(candles) < 65:
            return {"score": 0, "sell_score": 0, "signals": {},
                    "detail": {}, "reason": "데이터 부족"}

        df = pd.DataFrame(candles)
        c  = df["close"].astype(float)
        h  = df["high"].astype(float)
        lo = df["low"].astype(float)
        v  = df["volume"].astype(float)

        results = {}
        results["MA"]   = self._check_ma(c)
        results["RSI"]  = self._check_rsi(c)
        results["MACD"] = self._check_macd(c)
        results["BB"]   = self._check_bb(c)
        results["ATR"]  = self._check_atr(c, h, lo)
        results["OBV"]  = self._check_obv(c, v)

        buy_score  = sum(1 for r in results.values() if r["signal"] == "BUY")
        sell_score = sum(1 for r in results.values() if r["signal"] == "SELL")
        signals    = {k: (v["signal"] == "BUY") for k, v in results.items()}

        # 종합 사유
        buy_names  = [k for k, r in results.items() if r["signal"] == "BUY"]
        sell_names = [k for k, r in results.items() if r["signal"] == "SELL"]
        reason     = (f"매수지표: {', '.join(buy_names) or '없음'} | "
                      f"매도지표: {', '.join(sell_names) or '없음'}")

        logger.debug(f"지표검증 BUY={buy_score} SELL={sell_score} | {reason}")
        return {
            "score":      buy_score,
            "sell_score": sell_score,
            "signals":    signals,
            "detail":     results,
            "reason":     reason,
        }

    # ── 1. 이동평균 ────────────────────────────────────────
    def _check_ma(self, c: pd.Series) -> dict:
        ma5  = float(c.rolling(5).mean().iloc[-1])
        ma20 = float(c.rolling(20).mean().iloc[-1])
        ma60 = float(c.rolling(60).mean().iloc[-1])
        price= float(c.iloc[-1])

        # 정배열: MA5 > MA20 > MA60 + 현재가가 MA5 위
        bull = (ma5 > ma20 > ma60) and (price > ma5)
        # 역배열: 매도
        bear = (ma5 < ma20 < ma60) and (price < ma5)

        signal = "BUY" if bull else ("SELL" if bear else "HOLD")
        return {
            "signal": signal,
            "value":  {"MA5": round(ma5), "MA20": round(ma20), "MA60": round(ma60)},
            "reason": (f"정배열({'✅' if bull else '❌'}) "
                       f"MA5={ma5:.0f} MA20={ma20:.0f} MA60={ma60:.0f}"),
        }

    # ── 2. RSI ─────────────────────────────────────────────
    def _check_rsi(self, c: pd.Series) -> dict:
        delta    = c.diff()
        gain     = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss     = (-delta).clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        rsi      = float(100 - 100 / (1 + gain / loss.replace(0, 1e-10)).iloc[-1])
        prev_rsi = float(100 - 100 / (1 + gain / loss.replace(0, 1e-10)).iloc[-2])

        # 과매도(30↓) 탈출: RSI가 30 이하에서 위로 돌아서는 순간 강력 매수
        # 50 이하에서 상승: 일반 매수
        oversold_cross = (prev_rsi <= 35) and (rsi > 35)
        mid_up         = (rsi > 40) and (rsi < 60) and (rsi > prev_rsi)
        overbought     = rsi >= 70

        signal = ("BUY"  if (oversold_cross or mid_up) else
                  "SELL" if overbought else "HOLD")
        return {
            "signal": signal,
            "value":  {"RSI": round(rsi, 1), "prev_RSI": round(prev_rsi, 1)},
            "reason": (f"RSI={rsi:.1f} "
                       f"({'과매도탈출' if oversold_cross else '상승중' if mid_up else '과매수' if overbought else '중립'})"),
        }

    # ── 3. MACD ────────────────────────────────────────────
    def _check_macd(self, c: pd.Series) -> dict:
        ema12    = c.ewm(span=12, adjust=False).mean()
        ema26    = c.ewm(span=26, adjust=False).mean()
        macd     = ema12 - ema26
        signal_l = macd.ewm(span=9, adjust=False).mean()
        hist     = macd - signal_l

        mc  = float(macd.iloc[-1]);    mp  = float(macd.iloc[-2])
        sc  = float(signal_l.iloc[-1]); sp  = float(signal_l.iloc[-2])
        hc  = float(hist.iloc[-1]);    hp  = float(hist.iloc[-2])

        golden = (mp <= sp) and (mc > sc)            # 골든크로스
        hist_up= (hp < 0) and (hc > hp)              # 히스토그램 상승전환
        dead   = (mp >= sp) and (mc < sc)            # 데드크로스

        signal = ("BUY"  if (golden or (mc > sc and hist_up)) else
                  "SELL" if dead else "HOLD")
        return {
            "signal": signal,
            "value":  {"MACD": round(mc, 2), "Signal": round(sc, 2),
                       "Hist": round(hc, 2)},
            "reason": (f"MACD={mc:.2f}/Sig={sc:.2f} "
                       f"({'골든크로스' if golden else '히스토그램↑' if hist_up else '데드크로스' if dead else '중립'})"),
        }

    # ── 4. 볼린저 밴드 ─────────────────────────────────────
    def _check_bb(self, c: pd.Series) -> dict:
        ma20 = c.rolling(20).mean()
        std  = c.rolling(20).std()
        upper= ma20 + 2 * std
        lower= ma20 - 2 * std
        bw   = (upper - lower) / ma20 * 100   # 밴드 폭 %

        price  = float(c.iloc[-1])
        prev   = float(c.iloc[-2])
        lo_cur = float(lower.iloc[-1])
        lo_prev= float(lower.iloc[-2])
        up_cur = float(upper.iloc[-1])
        ma_cur = float(ma20.iloc[-1])
        bw_cur = float(bw.iloc[-1])

        # 하단 터치 후 반등 (저점 매수)
        touch_lower  = (prev <= lo_prev) and (price > lo_cur)
        # 중심선 위 + 밴드 확장 (추세 강화)
        above_mid_expand = (price > ma_cur) and (bw_cur > float(bw.rolling(10).mean().iloc[-1]))
        # 상단 돌파 = 과매수
        touch_upper  = price >= up_cur * 0.99

        signal = ("BUY"  if (touch_lower or above_mid_expand) else
                  "SELL" if touch_upper else "HOLD")
        return {
            "signal": signal,
            "value":  {"상단": round(up_cur), "중심": round(ma_cur),
                       "하단": round(lo_cur), "밴드폭": round(bw_cur, 1)},
            "reason": (f"현재={price:.0f} 하단={lo_cur:.0f} 상단={up_cur:.0f} "
                       f"({'하단반등' if touch_lower else '추세강화' if above_mid_expand else '상단과매수' if touch_upper else '중립'})"),
        }

    # ── 5. ATR (변동성) ────────────────────────────────────
    def _check_atr(self, c: pd.Series, h: pd.Series, lo: pd.Series) -> dict:
        prev_c  = c.shift(1)
        tr      = pd.concat([h - lo,
                             (h - prev_c).abs(),
                             (lo - prev_c).abs()], axis=1).max(axis=1)
        atr14   = float(tr.rolling(14).mean().iloc[-1])
        atr5    = float(tr.rolling(5).mean().iloc[-1])
        price   = float(c.iloc[-1])
        atr_pct = atr14 / price * 100

        # ATR 단기 > 장기: 변동성 확대 = 추세 발생 신호
        vol_expand  = atr5 > atr14 * 1.1
        # 과도한 변동성: 리스크 경고
        high_vol    = atr_pct > 5.0

        signal = ("BUY"  if vol_expand and not high_vol else
                  "SELL" if high_vol else "HOLD")
        return {
            "signal": signal,
            "value":  {"ATR14": round(atr14), "ATR5": round(atr5),
                       "ATR%": round(atr_pct, 2)},
            "reason": (f"ATR14={atr14:.0f}({atr_pct:.1f}%) "
                       f"{'변동성확대↑' if vol_expand else '고변동위험' if high_vol else '보통'}"),
        }

    # ── 6. OBV (거래량 추세) ───────────────────────────────
    def _check_obv(self, c: pd.Series, v: pd.Series) -> dict:
        direction = np.sign(c.diff()).fillna(0)
        obv       = (direction * v).cumsum()
        obv_ma5   = obv.rolling(5).mean()
        obv_ma20  = obv.rolling(20).mean()

        obv_cur  = float(obv.iloc[-1])
        om5      = float(obv_ma5.iloc[-1])
        om20     = float(obv_ma20.iloc[-1])
        prev_om5 = float(obv_ma5.iloc[-2])

        # OBV 단기MA > 장기MA + 상승 추세: 매집 신호
        obv_bull = (om5 > om20) and (om5 > prev_om5)
        # OBV 하락: 분산(매도세)
        obv_bear = (om5 < om20) and (om5 < prev_om5)

        signal = ("BUY"  if obv_bull else
                  "SELL" if obv_bear else "HOLD")
        return {
            "signal": signal,
            "value":  {"OBV": round(obv_cur), "OBV_MA5": round(om5),
                       "OBV_MA20": round(om20)},
            "reason": (f"OBV_MA5={om5:,.0f} MA20={om20:,.0f} "
                       f"({'매집↑' if obv_bull else '분산↓' if obv_bear else '중립'})"),
        }
