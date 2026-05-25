"""
이동평균 돌파 전략 (Golden Cross / Death Cross)
- MA5 / MA20 / MA60 크로스 신호
- 거래량 확인 조건 포함
"""
import pandas as pd
import numpy as np
from utils.logger import get_logger
from config import Config

logger = get_logger("MA_Strategy")


class MAStrategy:
    """
    이동평균 돌파 전략

    매수 신호:
      - 단기MA(5)가 중기MA(20)를 상향 돌파 (골든크로스)
      - 장기MA(60) 위에서 발생
      - 거래량이 20일 평균 거래량의 1.5배 이상

    매도 신호:
      - 단기MA(5)가 중기MA(20)를 하향 돌파 (데드크로스)
      - 또는 현재가가 중기MA(20) 아래로 2% 이상 이탈
    """

    def __init__(self):
        self.short = Config.MA_SHORT    # 5
        self.mid   = Config.MA_MID      # 20
        self.long_ = Config.MA_LONG     # 60

    def analyze(self, candles: list[dict]) -> dict:
        """
        candles: [{"date", "open","high","low","close","volume"}, ...]
        returns: {"signal": "BUY"|"SELL"|"HOLD", "reason": str, "score": float}
        """
        if len(candles) < self.long_ + 5:
            return {"signal": "HOLD", "reason": "데이터 부족", "score": 0.0}

        df = pd.DataFrame(candles)
        df["close"]  = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)

        # 이동평균 계산
        df["ma_short"] = df["close"].rolling(self.short).mean()
        df["ma_mid"]   = df["close"].rolling(self.mid).mean()
        df["ma_long"]  = df["close"].rolling(self.long_).mean()
        df["vol_ma"]   = df["volume"].rolling(self.mid).mean()

        cur   = df.iloc[-1]
        prev  = df.iloc[-2]

        ma_s  = cur["ma_short"]
        ma_m  = cur["ma_mid"]
        ma_l  = cur["ma_long"]
        price = cur["close"]
        vol   = cur["volume"]
        vol_avg = cur["vol_ma"]

        prev_ma_s = prev["ma_short"]
        prev_ma_m = prev["ma_mid"]

        # ── 골든크로스 체크 ────────────────────────────────
        golden_cross = (prev_ma_s <= prev_ma_m) and (ma_s > ma_m)
        above_long   = price > ma_l
        vol_surge    = vol >= vol_avg * 1.5

        # ── 데드크로스 체크 ────────────────────────────────
        dead_cross    = (prev_ma_s >= prev_ma_m) and (ma_s < ma_m)
        below_mid_2pct= price < ma_m * 0.98

        # ── 점수 계산 (0 ~ 1) ─────────────────────────────
        buy_score = 0.0
        if ma_s > ma_m:      buy_score += 0.3
        if ma_m > ma_l:      buy_score += 0.2
        if price > ma_s:     buy_score += 0.2
        if golden_cross:     buy_score += 0.2
        if vol_surge:        buy_score += 0.1

        sell_score = 0.0
        if ma_s < ma_m:      sell_score += 0.3
        if dead_cross:       sell_score += 0.3
        if below_mid_2pct:   sell_score += 0.2
        if price < ma_l:     sell_score += 0.2

        # ── 신호 결정 ────────────────────────────────────
        if golden_cross and above_long:
            signal = "BUY"
            reason = (
                f"골든크로스 발생 (MA{self.short}={ma_s:.0f} > MA{self.mid}={ma_m:.0f})"
                + (f", 거래량 급증({vol/vol_avg:.1f}배)" if vol_surge else "")
            )
            score = buy_score
        elif dead_cross or below_mid_2pct:
            signal = "SELL"
            reason = (
                "데드크로스 발생" if dead_cross
                else f"MA{self.mid} 2% 이탈 (현재가 {price:.0f} / MA20 {ma_m:.0f})"
            )
            score = sell_score
        else:
            signal = "HOLD"
            reason = (
                f"MA{self.short}={ma_s:.0f}, MA{self.mid}={ma_m:.0f}, "
                f"MA{self.long_}={ma_l:.0f} — 관망"
            )
            score = max(buy_score, sell_score)

        logger.debug(f"MA 신호: {signal} | {reason} | 점수={score:.2f}")
        return {
            "signal":    signal,
            "reason":    reason,
            "score":     round(score, 3),
            "ma_short":  round(ma_s, 2),
            "ma_mid":    round(ma_m, 2),
            "ma_long":   round(ma_l, 2),
            "golden_cross": golden_cross,
            "dead_cross":   dead_cross,
        }
