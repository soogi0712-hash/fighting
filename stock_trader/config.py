"""
시스템 전체 설정
"""
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # ── KIS API (실전 전용) ───────────────────────────────────
    KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
    KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
    KIS_ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")

    # ★ 실전 투자 전용 — 모의투자 없음
    KIS_IS_REAL = True
    BASE_URL    = "https://openapi.koreainvestment.com:9443"

    # ── 실주문 마스터 게이트 (기본 OFF) ───────────────────────
    # False 이면 KIS 주문/취소 API 가 네트워크 호출 없이 차단된다.
    # 실환경 검증(PHASE 5) 시에만 .env 에 LIVE_ORDER_ENABLED=true 로 명시 활성화.
    LIVE_ORDER_ENABLED = os.getenv("LIVE_ORDER_ENABLED", "false").lower() == "true"

    # ── 텔레그램 ─────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── 리스크 설정 ───────────────────────────────────────────
    # ★ 총 투자금 500만원 — 복리로 불어나는 기준 금액
    # ★ 종목당 한도 없음 — AI 강도 점수에 따라 자동 배분
    #   (기본 10%, 고점수 20%, 엘리트 25% — trade_decision.py 기준)
    MAX_INVESTMENT_PER_STOCK = float(os.getenv("MAX_INVESTMENT_PER_STOCK", 5_000_000))  # 총자산과 동일 = 한도 없음
    MAX_TOTAL_INVESTMENT     = float(os.getenv("MAX_TOTAL_INVESTMENT",     5_000_000))  # 총 투자금 500만원

    # ── 초기기준자산 (누적복리수익률 분모) ──────────────────────
    # .env 에서 INITIAL_ASSET=5000000 으로 지정하면 실제 시작 시점 총자산으로 계산
    # 미설정 시 MAX_TOTAL_INVESTMENT 와 동일값 사용
    INITIAL_ASSET = float(os.getenv("INITIAL_ASSET", os.getenv("MAX_TOTAL_INVESTMENT", 5_000_000)))

    # ── 손절/트레일링스탑 (실질수익률 기준) ────────────────────
    STOP_LOSS_PERCENT        = float(os.getenv("STOP_LOSS_PERCENT",       10.0))  # 실질손익 -10% 손절
    USE_TAKE_PROFIT          = os.getenv("USE_TAKE_PROFIT", "false").lower() == "true"  # 고정 익절 OFF 권장
    TAKE_PROFIT_PERCENT      = float(os.getenv("TAKE_PROFIT_PERCENT",     15.0))  # 고정 익절 % (USE_TAKE_PROFIT=true 시)
    TRAILING_STOP_PCT        = float(os.getenv("TRAILING_STOP_PCT",       15.0))  # 트레일링 하락률 %
    TRAILING_ACTIVATE_PCT    = float(os.getenv("TRAILING_ACTIVATE_PCT",    5.0))  # 트레일링 활성화 수익률 %

    # ── 피라미딩 추가매수 단계 ────────────────────────────────
    ADD_BUY_PROFIT           = os.getenv("ADD_BUY_PROFIT", "true").lower() == "true"   # 수익 종목 추가매수
    ADD_BUY_LOSS             = os.getenv("ADD_BUY_LOSS",  "false").lower() == "true"   # 손실 종목 추가매수 (물타기 금지)
    PYRAMID_STEP_1           = float(os.getenv("PYRAMID_STEP_1", 10.0))  # 1차 추가매수 수익률 %
    PYRAMID_STEP_2           = float(os.getenv("PYRAMID_STEP_2", 20.0))  # 2차 추가매수 수익률 %
    PYRAMID_STEP_3           = float(os.getenv("PYRAMID_STEP_3", 35.0))  # 3차 추가매수 수익률 %

    # ── 실험 전략 (Strategy Lab) ──────────────────────────────
    USE_LAB                  = os.getenv("USE_LAB", "true").lower() == "true"
    LAB_TRAILING_LIST        = os.getenv("LAB_TRAILING_LIST", "10,12,15,18,20,25")   # 트레일링 실험값
    LAB_STOPLOSS_LIST        = os.getenv("LAB_STOPLOSS_LIST", "5,7,10,12")            # 손절 실험값
    LAB_PYRAMID_LIST         = os.getenv("LAB_PYRAMID_LIST",  "10-20-35,15-30-50,20-40-60")  # 피라미딩 실험값

    # ── 전략 평가 기준 우선순위 (승률 마지막) ────────────────────
    # 1. CAGR(연복리수익률)  2. MDD(최대낙폭)  3. 샤프비율
    # 4. 총수익률            5. 승률 (마지막)
    EVAL_PRIORITY = ["cagr", "mdd", "sharpe", "total_return", "win_rate"]

    # ── 대시보드 ─────────────────────────────────────────────
    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "changeme_secret")
    DASHBOARD_PORT   = int(os.getenv("DASHBOARD_PORT", 5000))

    # ── 관심 종목 기본값 ──────────────────────────────────────
    # ★ 기본 빈 리스트 — 스크리너가 자동으로 종목을 발굴합니다
    # 수동으로 추가하려면 대시보드 우측 '관심종목 추가' 기능을 사용하거나
    # .env 에 WATCH_LIST_CODES=005930,000660 형식으로 지정하세요
    _watch_codes = [c.strip() for c in os.getenv("WATCH_LIST_CODES", "").split(",") if c.strip()]
    WATCH_LIST: list = []  # 런타임에 app.py 에서 채워짐 (아래 참조)

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
