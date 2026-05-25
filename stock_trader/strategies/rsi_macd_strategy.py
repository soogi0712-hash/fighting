"""
RSI + MACD 복합 전략
- RSI 과매도/과매수 + MACD 방향 확인
"""
import pandas as pd
import numpy as np
from utils.logger import get_logger
from config import Config

logger = get_logger("RSI_MACD_Strategy")


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs   = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def _macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast   = series.ewm(span=fast,   adjust=False).mean()
    ema_slow   = series.ewm(span=slow,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal_line= macd_line.ewm(span=signal, adjust=False).mean()
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram


class RSIMACDStrategy:
    """
    매수 신호:
      - RSI < 30 (과매도) + MACD 히스토그램이 음→양 전환
      - RSI 30~45 구간 + MACD 상향 크로스

    매도 신호:
      - RSI > 70 (과매수) + MACD 히스토그램이 양→음 전환
      - RSI 55~70 구간 + MACD 하향 크로스
    """

    def __init__(self):
        self.rsi_period    = Config.RSI_PERIOD
        self.rsi_oversold  = Config.RSI_OVERSOLD
        self.rsi_overbought= Config.RSI_OVERBOUGHT
        self.macd_fast     = Config.MACD_FAST
        self.macd_slow     = Config.MACD_SLOW
        self.macd_signal   = Config.MACD_SIGNAL

    def analyze(self, candles: list[dict]) -> dict:
        min_len = self.macd_slow + self.macd_signal + 10
        if len(candles) < min_len:
            return {"signal": "HOLD", "reason": "데이터 부족", "score": 0.0}

        df = pd.DataFrame(candles)
        df["close"] = df["close"].astype(float)

        df["rsi"]                            = _rsi(df["close"], self.rsi_period)
        macd_line, signal_line, histogram    = _macd(
            df["close"], self.macd_fast, self.macd_slow, self.macd_signal
        )
        df["macd"]      = macd_line
        df["macd_sig"]  = signal_line
        df["macd_hist"] = histogram

        cur  = df.iloc[-1]
        prev = df.iloc[-2]

        rsi       = cur["rsi"]
        hist_cur  = cur["macd_hist"]
        hist_prev = prev["macd_hist"]
        macd_cur  = cur["macd"]
        sig_cur   = cur["macd_sig"]
        macd_prev = prev["macd"]
        sig_prev  = prev["macd_sig"]

        macd_golden = (macd_prev <= sig_prev) and (macd_cur > sig_cur)   # MACD 골든크로스
        macd_dead   = (macd_prev >= sig_prev) and (macd_cur < sig_cur)   # MACD 데드크로스
        hist_turn_up  = (hist_prev < 0) and (hist_cur > 0)
        hist_turn_down= (hist_prev > 0) and (hist_cur < 0)

        # ── 점수 계산 ──────────────────────────────────────
        buy_score = 0.0
        if rsi < self.rsi_oversold:         buy_score += 0.35
        elif rsi < 45:                      buy_score += 0.15
        if macd_golden:                     buy_score += 0.30
        elif macd_cur > sig_cur:            buy_score += 0.10
        if hist_turn_up:                    buy_score += 0.20
        if macd_cur > 0:                    buy_score += 0.05
        if hist_cur > hist_prev:            buy_score += 0.10

        sell_score = 0.0
        if rsi > self.rsi_overbought:       sell_score += 0.35
        elif rsi > 55:                      sell_score += 0.15
        if macd_dead:                       sell_score += 0.30
        elif macd_cur < sig_cur:            sell_score += 0.10
        if hist_turn_down:                  sell_score += 0.20
        if macd_cur < 0:                    sell_score += 0.05

        # ── 신호 결정 ──────────────────────────────────────
        if buy_score >= 0.55:
            signal = "BUY"
            reason = (
                f"RSI={rsi:.1f}(과매도)" if rsi < self.rsi_oversold
                else f"RSI={rsi:.1f} + MACD 골든크로스" if macd_golden
                else f"RSI={rsi:.1f} + MACD 상승"
            )
            score = buy_score
        elif sell_score >= 0.55:
            signal = "SELL"
            reason = (
                f"RSI={rsi:.1f}(과매수)" if rsi > self.rsi_overbought
                else f"RSI={rsi:.1f} + MACD 데드크로스" if macd_dead
                else f"RSI={rsi:.1f} + MACD 하락"
            )
            score = sell_score
        else:
            signal = "HOLD"
            reason = f"RSI={rsi:.1f}, MACD={macd_cur:.2f} — 관망"
            score  = max(buy_score, sell_score)

        logger.debug(f"RSI/MACD 신호: {signal} | {reason}")
        return {
            "signal":       signal,
            "reason":       reason,
            "score":        round(score, 3),
            "rsi":          round(rsi, 2),
            "macd":         round(macd_cur, 4),
            "macd_signal":  round(sig_cur, 4),
            "macd_hist":    round(hist_cur, 4),
            "macd_golden":  macd_golden,
            "macd_dead":    macd_dead,
        }
