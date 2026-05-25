"""
AI 기반 예측 전략
- LSTM + Random Forest 앙상블
- 피처: OHLCV + 기술 지표 (RSI, MACD, Bollinger Bands, OBV)
- 레이블: 다음 N일 후 수익률이 threshold 이상이면 매수
"""
import os
import pickle
import numpy as np
import pandas as pd
from utils.logger import get_logger
from config import Config

logger = get_logger("AI_Strategy")

# TensorFlow는 임포트 시 시간이 걸리므로 lazy import
_tf_loaded = False
_tf = None
def _get_tf():
    global _tf_loaded, _tf
    if not _tf_loaded:
        try:
            import tensorflow as tf
            tf.get_logger().setLevel("ERROR")
            os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
            _tf = tf
            _tf_loaded = True
        except Exception as e:
            logger.warning(f"TensorFlow 로드 실패: {e}")
    return _tf


# ──────────────────────────────────────────────────────────────
# 피처 엔지니어링
# ──────────────────────────────────────────────────────────────
def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = df["volume"].astype(float)

    out = pd.DataFrame(index=df.index)
    out["return_1d"]  = c.pct_change(1)
    out["return_3d"]  = c.pct_change(3)
    out["return_5d"]  = c.pct_change(5)

    # 이동평균 비율
    for w in [5, 10, 20, 60]:
        out[f"ma{w}_ratio"] = c / c.rolling(w).mean() - 1

    # 볼린저 밴드
    ma20  = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    out["bb_upper_pct"] = (c - (ma20 + 2 * std20)) / c
    out["bb_lower_pct"] = (c - (ma20 - 2 * std20)) / c
    out["bb_width"]     = 4 * std20 / ma20

    # RSI
    delta    = c.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    out["rsi"] = 100 - 100 / (1 + rs)

    # MACD
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    out["macd_hist"] = (macd - macd_sig) / c

    # OBV
    obv = (np.sign(c.diff()) * v).fillna(0).cumsum()
    out["obv_ratio"] = obv / obv.rolling(20).mean().replace(0, 1)

    # 고가/저가 대비 위치
    hl_range = (h - l).replace(0, 1)
    out["price_pos"] = (c - l) / hl_range

    # 거래량 비율
    out["vol_ratio"] = v / v.rolling(20).mean().replace(0, 1)

    return out.fillna(0)


# ──────────────────────────────────────────────────────────────
# AI 전략 클래스
# ──────────────────────────────────────────────────────────────
class AIStrategy:
    MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "models")

    def __init__(self):
        os.makedirs(self.MODEL_DIR, exist_ok=True)
        self.lookback  = Config.AI_LOOKBACK    # 20
        self.threshold = Config.AI_THRESHOLD   # 0.55
        self._models   = {}   # stock_code -> {"lstm": ..., "rf": ...}
        self._scalers  = {}

    # ── 훈련 ──────────────────────────────────────────────
    def train(self, stock_code: str, candles: list[dict],
              predict_days: int = 3, label_threshold: float = 0.02):
        """
        candles: 최소 200일 이상 권장
        label_threshold: 3일 후 수익률이 이 값 이상이면 매수(1)
        """
        tf = _get_tf()
        if tf is None:
            logger.error("TensorFlow 없어 AI 훈련 불가")
            return False

        from sklearn.preprocessing import StandardScaler
        from sklearn.ensemble import RandomForestClassifier

        df = pd.DataFrame(candles)
        feats = _build_features(df)
        cols  = feats.columns.tolist()

        # 레이블: predict_days 후 상승 여부
        close  = df["close"].astype(float).values
        labels = np.zeros(len(close))
        for i in range(len(close) - predict_days):
            ret = (close[i + predict_days] - close[i]) / close[i]
            labels[i] = 1 if ret >= label_threshold else 0

        feat_arr = feats.values
        valid    = len(feat_arr) - predict_days

        X_raw = feat_arr[:valid]
        y     = labels[:valid]

        # 스케일러
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X_raw)

        # ── LSTM 시퀀스 생성 ──────────────────────────────
        X_seq, y_seq = [], []
        for i in range(self.lookback, valid):
            X_seq.append(X_scaled[i - self.lookback:i])
            y_seq.append(y[i])
        X_seq = np.array(X_seq)
        y_seq = np.array(y_seq)

        # LSTM 모델
        model = tf.keras.Sequential([
            tf.keras.layers.LSTM(64, return_sequences=True,
                                 input_shape=(self.lookback, len(cols))),
            tf.keras.layers.Dropout(0.2),
            tf.keras.layers.LSTM(32),
            tf.keras.layers.Dropout(0.2),
            tf.keras.layers.Dense(16, activation="relu"),
            tf.keras.layers.Dense(1, activation="sigmoid"),
        ])
        model.compile(optimizer="adam", loss="binary_crossentropy",
                      metrics=["accuracy"])
        model.fit(X_seq, y_seq, epochs=30, batch_size=32,
                  validation_split=0.1, verbose=0)

        # ── RandomForest ─────────────────────────────────
        rf = RandomForestClassifier(n_estimators=100, max_depth=8,
                                    random_state=42)
        rf.fit(X_scaled[self.lookback:], y[self.lookback:])

        # 저장
        model_path  = os.path.join(self.MODEL_DIR, f"lstm_{stock_code}.keras")
        scaler_path = os.path.join(self.MODEL_DIR, f"scaler_{stock_code}.pkl")
        rf_path     = os.path.join(self.MODEL_DIR, f"rf_{stock_code}.pkl")

        model.save(model_path)
        with open(scaler_path, "wb") as f:  pickle.dump(scaler, f)
        with open(rf_path,     "wb") as f:  pickle.dump(rf, f)

        self._models[stock_code]  = {"lstm": model, "rf": rf}
        self._scalers[stock_code] = scaler

        logger.info(f"✅ AI 모델 훈련 완료: {stock_code}")
        return True

    def _load_model(self, stock_code: str) -> bool:
        tf = _get_tf()
        if tf is None:
            return False

        model_path  = os.path.join(self.MODEL_DIR, f"lstm_{stock_code}.keras")
        scaler_path = os.path.join(self.MODEL_DIR, f"scaler_{stock_code}.pkl")
        rf_path     = os.path.join(self.MODEL_DIR, f"rf_{stock_code}.pkl")

        if not (os.path.exists(model_path) and
                os.path.exists(scaler_path) and
                os.path.exists(rf_path)):
            return False

        try:
            self._models[stock_code] = {
                "lstm": tf.keras.models.load_model(model_path),
                "rf":   pickle.load(open(rf_path, "rb")),
            }
            self._scalers[stock_code] = pickle.load(open(scaler_path, "rb"))
            return True
        except Exception as e:
            logger.warning(f"모델 로드 실패 {stock_code}: {e}")
            return False

    # ── 예측 ──────────────────────────────────────────────
    def predict(self, stock_code: str, candles: list[dict]) -> dict:
        """
        returns: {"signal": "BUY"|"SELL"|"HOLD", "prob": float, ...}
        """
        # 모델 확인 / 로드
        if stock_code not in self._models:
            if not self._load_model(stock_code):
                logger.info(f"모델 없음({stock_code}), 훈련 시작...")
                if len(candles) >= 150:
                    ok = self.train(stock_code, candles)
                    if not ok:
                        return {"signal": "HOLD", "reason": "AI 모델 없음",
                                "prob": 0.5, "score": 0.0}
                else:
                    return {"signal": "HOLD", "reason": "데이터 부족(AI)",
                            "prob": 0.5, "score": 0.0}

        scaler  = self._scalers[stock_code]
        lstm    = self._models[stock_code]["lstm"]
        rf      = self._models[stock_code]["rf"]

        df    = pd.DataFrame(candles)
        feats = _build_features(df)
        X_raw = feats.values
        X_sc  = scaler.transform(X_raw)

        if len(X_sc) < self.lookback:
            return {"signal": "HOLD", "reason": "최근 데이터 부족", "prob": 0.5, "score": 0.0}

        # LSTM 예측
        seq     = X_sc[-self.lookback:].reshape(1, self.lookback, -1)
        lstm_prob = float(lstm.predict(seq, verbose=0)[0][0])

        # RF 예측
        rf_prob = float(rf.predict_proba(X_sc[-1:])[0][1])

        # 앙상블 (가중 평균)
        prob = lstm_prob * 0.6 + rf_prob * 0.4

        if prob >= self.threshold:
            signal = "BUY"
            reason = f"AI 매수 신호 (확률={prob:.1%}, LSTM={lstm_prob:.1%}, RF={rf_prob:.1%})"
            score  = prob
        elif prob <= (1 - self.threshold):
            signal = "SELL"
            reason = f"AI 매도 신호 (확률={prob:.1%})"
            score  = 1 - prob
        else:
            signal = "HOLD"
            reason = f"AI 불확실 (확률={prob:.1%})"
            score  = abs(prob - 0.5) * 2

        logger.debug(f"AI 신호: {signal} | {reason}")
        return {
            "signal":    signal,
            "reason":    reason,
            "prob":      round(prob, 4),
            "lstm_prob": round(lstm_prob, 4),
            "rf_prob":   round(rf_prob, 4),
            "score":     round(score, 3),
        }
