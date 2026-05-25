"""
시스템 전체 설정
"""
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # ── KIS API ──────────────────────────────────────────────
    KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
    KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
    KIS_ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")
    KIS_IS_REAL    = os.getenv("KIS_IS_REAL", "false").lower() == "true"

    BASE_URL = (
        "https://openapi.koreainvestment.com:9443"
        if KIS_IS_REAL else
        "https://openapivts.koreainvestment.com:29443"
    )

    # ── 텔레그램 ─────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── 리스크 설정 ───────────────────────────────────────────
    MAX_INVESTMENT_PER_STOCK = float(os.getenv("MAX_INVESTMENT_PER_STOCK", 1_000_000))
    MAX_TOTAL_INVESTMENT     = float(os.getenv("MAX_TOTAL_INVESTMENT",     5_000_000))
    STOP_LOSS_PERCENT        = float(os.getenv("STOP_LOSS_PERCENT",  3.0))
    TAKE_PROFIT_PERCENT      = float(os.getenv("TAKE_PROFIT_PERCENT", 5.0))

    # ── 대시보드 ─────────────────────────────────────────────
    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "changeme_secret")
    DASHBOARD_PORT   = int(os.getenv("DASHBOARD_PORT", 5000))

    # ── 관심 종목 기본값 ──────────────────────────────────────
    WATCH_LIST = [
        {"code": "005930", "name": "삼성전자"},
        {"code": "000660", "name": "SK하이닉스"},
        {"code": "035420", "name": "NAVER"},
        {"code": "035720", "name": "카카오"},
        {"code": "373220", "name": "LG에너지솔루션"},
    ]

    # ── 전략 파라미터 ─────────────────────────────────────────
    MA_SHORT    = 5
    MA_MID      = 20
    MA_LONG     = 60

    RSI_PERIOD     = 14
    RSI_OVERSOLD   = 30
    RSI_OVERBOUGHT = 70

    MACD_FAST   = 12
    MACD_SLOW   = 26
    MACD_SIGNAL = 9

    AI_LOOKBACK  = 20
    AI_THRESHOLD = 0.55

    # ── 세션별 신호 점검 주기 (초) ────────────────────────────
    # market_session.py 의 check_sec 를 우선 사용,
    # 아래는 fallback 기본값
    CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", 300))  # 기본 5분
