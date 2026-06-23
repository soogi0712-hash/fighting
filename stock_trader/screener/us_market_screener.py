"""
미국 증시 스크리너 (US Market Screener) v3
==========================================
실행 시점:
  ① 야간 00:00 KST  — 전일 마감 후 종합 분석 + 섹터 강도 기반 신규 종목 발굴
  ② 장중 ET 10:30   — 장 개시 1시간 후 실시간 모멘텀 재선정

개선 내용 (v3 — 고정 관심종목 반복매매 → 매일 신규 발굴):
  1. 섹터 강도 자동 선정: 섹터 ETF 성과 분석으로 당일 강세 섹터 TOP3~5 확정
  2. 강세 섹터 내 신규 종목 우선 발굴: 강세 섹터 종목에 +15점 보너스
  3. 거래량 급증·거래대금 증가·OBV 상승·VWAP 우위 통합 점수
  4. 48h 성과 없는 종목 자동 제거 (us_watchlist_manager 연동)
  5. 7일 손절 페널티 (-20점), 당일 익절 재진입 금지
  6. [US DAILY SCREENER] 로그: 종목수/강세섹터/신규후보/제외종목/감시종목

데이터 소스: yfinance (Yahoo Finance)
"""

import json
import os
from datetime import datetime, date, timedelta
import pytz
from collections import defaultdict

try:
    import yfinance as yf
    import numpy as np
    import pandas as pd
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False

from utils.logger import get_logger

logger = get_logger("USMarketScreener")

KST = pytz.timezone("Asia/Seoul")
US_EASTERN = pytz.timezone("America/New_York")
US_RESULT_FILE     = os.path.join(os.path.dirname(__file__), "..", "data", "us_market.json")
US_INTRADAY_FILE   = os.path.join(os.path.dirname(__file__), "..", "data", "us_intraday.json")
RECENT_LOSS_FILE   = os.path.join(os.path.dirname(__file__), "..", "data", "us_recent_loss.json")
COOLDOWN_FILE      = os.path.join(os.path.dirname(__file__), "..", "data", "us_cooldown.json")

# ── 주요 지수 ─────────────────────────────────────────────────
US_INDICES = {
    "^GSPC":   {"name": "S&P 500",    "emoji": "🇺🇸"},
    "^IXIC":   {"name": "나스닥",      "emoji": "💻"},
    "^DJI":    {"name": "다우존스",    "emoji": "🏦"},
    "^VIX":    {"name": "VIX 공포지수","emoji": "😱"},
    "^TNX":    {"name": "미국10년채",  "emoji": "📈"},
    "DX-Y.NYB":{"name": "달러인덱스", "emoji": "💵"},
    "GC=F":    {"name": "금",          "emoji": "🥇"},
    "CL=F":    {"name": "WTI원유",     "emoji": "🛢️"},
}

# ── 섹터 ETF (11개 섹터 전체) ──────────────────────────────────
US_SECTORS = {
    "XLK":  "기술(Tech)",
    "XLF":  "금융(Finance)",
    "XLV":  "헬스케어",
    "XLE":  "에너지",
    "XLI":  "산업/방산",
    "XLC":  "통신서비스",
    "XLY":  "경기소비재",
    "XLP":  "필수소비재",
    "XLB":  "소재",
    "XLRE": "리츠(부동산)",
    "XLU":  "유틸리티",
    "ITA":  "방위산업ETF",
    "URA":  "원자력ETF",
    "ICLN": "청정에너지ETF",
}

# ════════════════════════════════════════════════════════════════
# ★ 종목 풀 유니버스 — 섹터별 균형 배분 (200+ 종목)
# 섹터 태그는 스크리닝 분산 제어에 사용
# ════════════════════════════════════════════════════════════════
US_STOCKS = {

    # ══════════════════════════════════════════════════════════
    # [SEC-1] AI / 빅데이터 / 클라우드
    # ══════════════════════════════════════════════════════════
    "NVDA":  {"name": "엔비디아(AI반도체)",        "excd": "NASD", "sector": "AI"},
    "PLTR":  {"name": "팔란티어(AI데이터분석)",    "excd": "NYSE", "sector": "AI"},
    "SOUN":  {"name": "사운드하운드AI(음성AI)",    "excd": "NASD", "sector": "AI"},
    "BBAI":  {"name": "빅베어AI(기업AI솔루션)",    "excd": "NYSE", "sector": "AI"},
    "UPST":  {"name": "업스타트(AI대출심사)",      "excd": "NASD", "sector": "AI"},
    "AI":    {"name": "C3.ai(기업AI플랫폼)",       "excd": "NYSE", "sector": "AI"},
    "RXRX":  {"name": "리커전제약(AI신약개발)",    "excd": "NASD", "sector": "AI"},
    "PATH":  {"name": "유아이패스(AI자동화)",      "excd": "NYSE", "sector": "AI"},
    "AMBA":  {"name": "암바렐라(엣지AI반도체)",    "excd": "NASD", "sector": "AI"},
    "SMCI":  {"name": "슈퍼마이크로(AI서버)",      "excd": "NASD", "sector": "AI"},

    # ══════════════════════════════════════════════════════════
    # [SEC-2] 양자컴퓨팅
    # ══════════════════════════════════════════════════════════
    "IONQ":  {"name": "아이온큐(양자컴퓨팅리더)", "excd": "NYSE", "sector": "QUANTUM"},
    "RGTI":  {"name": "리게티컴퓨팅(양자)",       "excd": "NASD", "sector": "QUANTUM"},
    "QUBT":  {"name": "퀀텀컴퓨팅Inc(양자)",      "excd": "NASD", "sector": "QUANTUM"},
    "QBTS":  {"name": "D-Wave퀀텀(양자어닐링)",   "excd": "NYSE", "sector": "QUANTUM"},
    "ARQQ":  {"name": "아쿼이라퀀텀(양자보안)",   "excd": "NASD", "sector": "QUANTUM"},

    # ══════════════════════════════════════════════════════════
    # [SEC-3] 반도체
    # ══════════════════════════════════════════════════════════
    "AMD":   {"name": "AMD(AI/데이터센터반도체)",  "excd": "NASD", "sector": "SEMI"},
    "MU":    {"name": "마이크론(HBM메모리)",       "excd": "NASD", "sector": "SEMI"},
    "WOLF":  {"name": "울프스피드(SiC전력반도체)", "excd": "NYSE", "sector": "SEMI"},
    "NVTS":  {"name": "나비타스세미(GaN반도체)",   "excd": "NASD", "sector": "SEMI"},
    "SOXL":  {"name": "반도체3배레버리지ETF",      "excd": "NASD", "sector": "SEMI"},
    "LSCC":  {"name": "래티스세미(엣지FPGA)",      "excd": "NASD", "sector": "SEMI"},
    "ACLS":  {"name": "액셀리스테크(이온주입장비)","excd": "NASD", "sector": "SEMI"},
    "AEHR":  {"name": "에어테스트(반도체번인)",    "excd": "NASD", "sector": "SEMI"},
    "ARM":   {"name": "ARM홀딩스(CPU IP설계)",    "excd": "NASD", "sector": "SEMI"},

    # ══════════════════════════════════════════════════════════
    # [SEC-4] 암호화폐 / 블록체인 채굴
    # ══════════════════════════════════════════════════════════
    "MSTR":  {"name": "마이크로스트래티지(BTC보유)","excd": "NASD","sector": "CRYPTO"},
    "COIN":  {"name": "코인베이스(암호화폐거래소)", "excd": "NASD","sector": "CRYPTO"},
    "RIOT":  {"name": "라이엇플랫폼스(BTC채굴)",   "excd": "NASD","sector": "CRYPTO"},
    "MARA":  {"name": "마라홀딩스(BTC채굴)",        "excd": "NASD","sector": "CRYPTO"},
    "HUT":   {"name": "허트8(BTC채굴+AI컴퓨팅)",   "excd": "NASD","sector": "CRYPTO"},
    "CLSK":  {"name": "클린스파크(그린채굴)",       "excd": "NASD","sector": "CRYPTO"},
    "CIFR":  {"name": "사이퍼마이닝(채굴)",         "excd": "NASD","sector": "CRYPTO"},
    "WULF":  {"name": "테라울프(친환경채굴)",       "excd": "NASD","sector": "CRYPTO"},
    "IREN":  {"name": "아이렌(AI+채굴복합)",        "excd": "NASD","sector": "CRYPTO"},
    "BTBT":  {"name": "비트팜스(캐나다채굴)",       "excd": "NASD","sector": "CRYPTO"},

    # ══════════════════════════════════════════════════════════
    # [SEC-5] 바이오 / 유전자편집 / 제약
    # ══════════════════════════════════════════════════════════
    "CRSP":  {"name": "크리스퍼테라퓨틱스(유전자편집)","excd":"NASD","sector":"BIO"},
    "BEAM":  {"name": "빔테라퓨틱스(염기편집)",    "excd": "NASD", "sector": "BIO"},
    "NTLA":  {"name": "인텔리아테라퓨틱스(생체편집)","excd":"NASD","sector":"BIO"},
    "EDIT":  {"name": "에디타스메디신(유전자편집)", "excd": "NASD", "sector": "BIO"},
    "IOVA":  {"name": "아이오반스(TIL세포치료)",   "excd": "NASD", "sector": "BIO"},
    "FATE":  {"name": "페이트테라퓨틱스(세포치료)","excd": "NASD", "sector": "BIO"},
    "ACAD":  {"name": "아카디아제약(신경계),",     "excd": "NASD", "sector": "BIO"},
    "INSM":  {"name": "인스메드(희귀폐질환)",      "excd": "NASD", "sector": "BIO"},
    "KYMR":  {"name": "카이메라(단백질분해표적)",  "excd": "NASD", "sector": "BIO"},
    "IMVT":  {"name": "이뮤노반트(자가면역)",      "excd": "NASD", "sector": "BIO"},
    "AUPH":  {"name": "오로비아제약(루푸스신장염)","excd": "NASD", "sector": "BIO"},
    "RLAY":  {"name": "릴레이테라퓨틱스(암치료)", "excd": "NASD", "sector": "BIO"},
    "SAVA":  {"name": "카사바사이언스(알츠하이머)","excd": "NASD", "sector": "BIO"},
    "AGEN":  {"name": "에이전우스(면역항암제)",    "excd": "NASD", "sector": "BIO"},
    "NKTR":  {"name": "넥타테라퓨틱스(약물전달)", "excd": "NASD", "sector": "BIO"},

    # ══════════════════════════════════════════════════════════
    # [SEC-6] EV / 청정에너지
    # ══════════════════════════════════════════════════════════
    "RIVN":  {"name": "리비안(EV픽업트럭)",        "excd": "NASD", "sector": "EV"},
    "LCID":  {"name": "루시드모터스(EV럭셔리)",    "excd": "NASD", "sector": "EV"},
    "CHPT":  {"name": "차지포인트(EV충전인프라)",  "excd": "NYSE", "sector": "EV"},
    "BLNK":  {"name": "블링크차징(EV충전)",        "excd": "NASD", "sector": "EV"},
    "PLUG":  {"name": "플러그파워(수소연료전지)",  "excd": "NASD", "sector": "EV"},
    "FCEL":  {"name": "퓨얼셀에너지(연료전지발전)","excd": "NASD","sector": "EV"},
    "BE":    {"name": "블룸에너지(고체연료전지)",  "excd": "NYSE", "sector": "EV"},
    "EVGO":  {"name": "이브고(EV급속충전네트워크)","excd": "NASD","sector": "EV"},
    "NKLA":  {"name": "니콜라(수소트럭)",          "excd": "NASD", "sector": "EV"},

    # ══════════════════════════════════════════════════════════
    # [SEC-7] 방산 / 우주 / 에어택시
    # ══════════════════════════════════════════════════════════
    "RKLB":  {"name": "로켓랩(소형위성발사체)",    "excd": "NASD", "sector": "DEFENSE"},
    "LUNR":  {"name": "인투이티브머신스(달착륙선)","excd": "NASD","sector": "DEFENSE"},
    "ASTS":  {"name": "AST스페이스(우주직접통신)", "excd": "NASD", "sector": "DEFENSE"},
    "JOBY":  {"name": "조비에비에이션(전기에어택시)","excd":"NYSE","sector":"DEFENSE"},
    "ACHR":  {"name": "아처에비에이션(에어택시)",  "excd": "NYSE", "sector": "DEFENSE"},
    "SPIR":  {"name": "스파이어글로벌(위성데이터)","excd": "NYSE", "sector": "DEFENSE"},
    "RDW":   {"name": "레드와이어(우주구조물제조)","excd": "NYSE", "sector": "DEFENSE"},
    "KTOS":  {"name": "크라토스국방(드론/방산)",   "excd": "NASD", "sector": "DEFENSE"},
    "BWXT":  {"name": "BWX테크놀로지스(원자력방산)","excd":"NYSE","sector":"DEFENSE"},
    "CACI":  {"name": "CACI인터내셔널(방산IT)",    "excd": "NYSE", "sector": "DEFENSE"},

    # ══════════════════════════════════════════════════════════
    # [SEC-8] 원자력 / 에너지 인프라
    # ══════════════════════════════════════════════════════════
    "NNE":   {"name": "나노뉴클리어(마이크로원자로)","excd":"NASD","sector":"NUCLEAR"},
    "SMR":   {"name": "뉴스케일파워(소형원자로)",  "excd": "NYSE", "sector": "NUCLEAR"},
    "LEU":   {"name": "센트러스에너지(우라늄농축)","excd": "NYSE", "sector": "NUCLEAR"},
    "CCJ":   {"name": "카메코(우라늄채굴리더)",    "excd": "NYSE", "sector": "NUCLEAR"},
    "DNN":   {"name": "디니슨마인스(우라늄탐사)", "excd": "NYSE", "sector": "NUCLEAR"},
    "UEC":   {"name": "우라늄에너지코프",          "excd": "NYSE", "sector": "NUCLEAR"},
    "OKLO":  {"name": "오클로(마이크로원자로스타트업)","excd":"NYSE","sector":"NUCLEAR"},

    # ══════════════════════════════════════════════════════════
    # [SEC-9] 핀테크 / 금융
    # ══════════════════════════════════════════════════════════
    "SOFI":  {"name": "소파이(디지털뱅크)",        "excd": "NASD", "sector": "FINTECH"},
    "AFRM":  {"name": "어펌(BNPL후불결제)",        "excd": "NASD", "sector": "FINTECH"},
    "HOOD":  {"name": "로빈후드(MZ주식앱)",        "excd": "NASD", "sector": "FINTECH"},
    "DAVE":  {"name": "데이브(소액대출앱)",         "excd": "NASD", "sector": "FINTECH"},
    "OPEN":  {"name": "오픈도어(AI부동산거래)",    "excd": "NASD", "sector": "FINTECH"},
    "PAYO":  {"name": "페이온어(글로벌B2B결제)",   "excd": "NASD", "sector": "FINTECH"},
    "FLYW":  {"name": "플라이와이어(교육결제솔루션)","excd":"NASD","sector":"FINTECH"},
    "UPST":  {"name": "업스타트(AI대출플랫폼)",    "excd": "NASD", "sector": "FINTECH"},
    "LC":    {"name": "렌딩클럽(P2P대출)",         "excd": "NYSE", "sector": "FINTECH"},
    "LMND":  {"name": "레모네이드(AI보험)",         "excd": "NYSE", "sector": "FINTECH"},

    # ══════════════════════════════════════════════════════════
    # [SEC-10] 소비재 / 헬스테크 / SNS / 기타 성장주
    # ══════════════════════════════════════════════════════════
    "RDDT":  {"name": "레딧(소셜미디어플랫폼)",    "excd": "NYSE", "sector": "GROWTH"},
    "CLOV":  {"name": "클로버헬스(AI건강보험)",    "excd": "NASD", "sector": "GROWTH"},
    "CAVA":  {"name": "카바(지중해패스트푸드체인)","excd": "NYSE", "sector": "GROWTH"},
    "BROS":  {"name": "더치브로스(커피체인급성장)","excd": "NYSE", "sector": "GROWTH"},
    "SPCE":  {"name": "버진갤럭틱(우주관광)",      "excd": "NYSE", "sector": "GROWTH"},
    "XPEV":  {"name": "샤오펑(중국프리미엄EV)",    "excd": "NYSE", "sector": "GROWTH"},
    "NIO":   {"name": "니오(중국EV스타트업)",      "excd": "NYSE", "sector": "GROWTH"},
    "LI":    {"name": "리오토(중국EREV)",           "excd": "NASD", "sector": "GROWTH"},
    "FUTU":  {"name": "푸투홀딩스(중국온라인증권)","excd": "NASD", "sector": "GROWTH"},
    "GME":   {"name": "게임스탑(밈주식)",          "excd": "NYSE", "sector": "GROWTH"},

    # ══════════════════════════════════════════════════════════
    # [SEC-11] 레버리지 ETF (고변동성)
    # ══════════════════════════════════════════════════════════
    "TQQQ":  {"name": "나스닥100 3배레버리지",     "excd": "NASD", "sector": "LEVERAGED"},
    "FNGU":  {"name": "빅테크 3배레버리지",        "excd": "NYSE", "sector": "LEVERAGED"},
    "TNA":   {"name": "소형주 3배레버리지",        "excd": "NYSE", "sector": "LEVERAGED"},
    "TECL":  {"name": "기술섹터 3배레버리지",      "excd": "NYSE", "sector": "LEVERAGED"},
    "CURE":  {"name": "헬스케어 3배레버리지",      "excd": "NYSE", "sector": "LEVERAGED"},
    "DPST":  {"name": "은행섹터 3배레버리지",      "excd": "NYSE", "sector": "LEVERAGED"},
    "HIBL":  {"name": "S&P고베타 3배레버리지",     "excd": "NYSE", "sector": "LEVERAGED"},
    "SPXL":  {"name": "S&P500 3배레버리지",        "excd": "NYSE", "sector": "LEVERAGED"},
    "FAS":   {"name": "금융섹터 3배레버리지",      "excd": "NYSE", "sector": "LEVERAGED"},
    "LABU":  {"name": "바이오 3배레버리지",        "excd": "NASD", "sector": "LEVERAGED"},
}

# ── 섹터 한글 이름 매핑 ────────────────────────────────────────
SECTOR_NAMES = {
    "AI":       "AI/클라우드",
    "QUANTUM":  "양자컴퓨팅",
    "SEMI":     "반도체",
    "CRYPTO":   "암호화폐/채굴",
    "BIO":      "바이오/제약",
    "EV":       "EV/청정에너지",
    "DEFENSE":  "방산/우주",
    "NUCLEAR":  "원자력/에너지",
    "FINTECH":  "핀테크/금융",
    "GROWTH":   "성장/소비/SNS",
    "LEVERAGED":"레버리지ETF",
}

# ── 섹터별 최대 허용 종목 수 (최종 후보 리스트에서) ──────────────
SECTOR_MAX = {
    "AI":       3,
    "QUANTUM":  2,
    "SEMI":     2,
    "CRYPTO":   3,  # 채굴주 집중 방지: 기존 11개→3개로 제한
    "BIO":      3,
    "EV":       2,
    "DEFENSE":  2,
    "NUCLEAR":  2,
    "FINTECH":  2,
    "GROWTH":   2,
    "LEVERAGED":2,
}


# ════════════════════════════════════════════════════════════════
# 유틸리티 함수
# ════════════════════════════════════════════════════════════════

def _safe_pct(cur, prev) -> float:
    try:
        if prev and prev != 0:
            return round((cur - prev) / prev * 100, 2)
    except Exception:
        pass
    return 0.0


def _calc_rsi(series: "pd.Series", period: int = 14) -> float:
    try:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss
        rsi   = 100 - (100 / (1 + rs))
        return round(float(rsi.iloc[-1]), 1)
    except Exception:
        return 50.0


def _calc_macd_signal(series: "pd.Series") -> str:
    try:
        ema12 = series.ewm(span=12).mean()
        ema26 = series.ewm(span=26).mean()
        macd  = ema12 - ema26
        sig   = macd.ewm(span=9).mean()
        if macd.iloc[-1] > sig.iloc[-1] and macd.iloc[-2] <= sig.iloc[-2]:
            return "골든크로스"
        elif macd.iloc[-1] < sig.iloc[-1] and macd.iloc[-2] >= sig.iloc[-2]:
            return "데드크로스"
        elif macd.iloc[-1] > sig.iloc[-1]:
            return "상승"
        else:
            return "하락"
    except Exception:
        return "—"


def _load_recent_loss() -> set:
    """최근 3일 내 손실 청산 종목 로드"""
    try:
        if os.path.exists(RECENT_LOSS_FILE):
            with open(RECENT_LOSS_FILE, "r") as f:
                data = json.load(f)
            cutoff = (datetime.now() - timedelta(days=3)).isoformat()
            return {
                sym for sym, info in data.items()
                if info.get("closed_at", "") >= cutoff and info.get("pnl_usd", 0) < 0
            }
    except Exception:
        pass
    return set()


def _load_cooldown() -> set:
    """익절 후 재매수 억제 종목 로드 (24시간 쿨다운)"""
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE, "r") as f:
                data = json.load(f)
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            return {
                sym for sym, ts in data.items()
                if ts >= cutoff
            }
    except Exception:
        pass
    return set()


def save_cooldown(symbol: str):
    """익절 종목을 24시간 쿨다운에 등록"""
    try:
        data = {}
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE, "r") as f:
                data = json.load(f)
        data[symbol] = datetime.now().isoformat()
        with open(COOLDOWN_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_recent_loss(symbol: str, pnl_usd: float):
    """손실 청산 종목 기록"""
    try:
        data = {}
        if os.path.exists(RECENT_LOSS_FILE):
            with open(RECENT_LOSS_FILE, "r") as f:
                data = json.load(f)
        data[symbol] = {
            "pnl_usd":   pnl_usd,
            "closed_at": datetime.now().isoformat(),
        }
        with open(RECENT_LOSS_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════
# 종목 기술적 분석 (야간용 — 일봉 기준)
# ════════════════════════════════════════════════════════════════

def _score_stock_daily(hist: "pd.DataFrame", symbol: str,
                       recent_loss_syms: set, cooldown_syms: set) -> dict:
    """
    일봉 기반 기술적 점수 산출 (0~100)
    ★ 변동성 중소형주 최적화 + 패널티 시스템
    """
    try:
        c = hist["Close"]
        v = hist["Volume"]

        cur   = float(c.iloc[-1])
        prev  = float(c.iloc[-2])
        pct   = _safe_pct(cur, prev)

        ma5   = float(c.rolling(5).mean().iloc[-1])
        ma20  = float(c.rolling(20).mean().iloc[-1])
        ma60  = float(c.rolling(60).mean().iloc[-1]) if len(c) >= 60 else ma20
        rsi   = _calc_rsi(c)
        macd  = _calc_macd_signal(c)

        pct5d = _safe_pct(cur, float(c.iloc[-6])) if len(c) >= 6 else pct
        pct3d = _safe_pct(cur, float(c.iloc[-4])) if len(c) >= 4 else pct

        high52    = float(c.tail(252).max()) if len(c) >= 252 else float(c.max())
        from_high = _safe_pct(cur, high52)

        vol_avg5  = float(v.rolling(5).mean().iloc[-1])
        vol_ratio = float(v.iloc[-1] / vol_avg5) if vol_avg5 > 0 else 1.0

        # 거래대금 (USD)
        turnover = cur * float(v.iloc[-1])

        # ── 점수 산출 ────────────────────────────────────────────
        score = 40.0

        # ① 거래량 (가장 높은 가중치)
        if   vol_ratio >= 5.0:  score += 20
        elif vol_ratio >= 3.0:  score += 14
        elif vol_ratio >= 2.0:  score += 8
        elif vol_ratio >= 1.5:  score += 4

        # ② 당일 등락률
        if   pct >= 8.0:  score += 18
        elif pct >= 5.0:  score += 13
        elif pct >= 3.0:  score += 8
        elif pct >= 1.0:  score += 4
        elif pct < -3.0:  score -= 12
        elif pct < -1.5:  score -= 5

        # ③ 5일 모멘텀
        if   pct5d >= 15.0: score += 10
        elif pct5d >= 8.0:  score += 6
        elif pct5d >= 3.0:  score += 3
        elif pct5d < -10.0: score -= 8

        # ④ MA 정배열
        if cur > ma5:   score += 4
        if cur > ma20:  score += 5
        if ma5  > ma20: score += 4
        if ma20 > ma60: score += 3

        # ⑤ RSI
        if   45 <= rsi <= 70: score += 8
        elif 35 <= rsi < 45:  score += 3
        elif rsi > 80:        score -= 5
        elif rsi < 25:        score += 2

        # ⑥ MACD
        if "골든" in macd:  score += 10
        elif "상승" in macd: score += 4
        if "데드"  in macd:  score -= 8

        # ⑦ 52주 고점 근접
        if   from_high >= -3.0:  score += 10
        elif from_high >= -10.0: score += 4
        elif from_high < -50.0:  score -= 5

        # ⑧ 거래대금 필터 (유동성 확인: $1M 이상)
        if turnover < 1_000_000:  score -= 10  # 유동성 부족 페널티

        # ── 패널티 ───────────────────────────────────────────────
        # ⑨ 최근 3일 손실 종목 페널티
        if symbol in recent_loss_syms:
            score -= 10
            logger.debug(f"[손실페널티] {symbol} -10점 (3일 내 손실 청산)")

        # ⑩ 익절 후 쿨다운 종목 페널티
        if symbol in cooldown_syms:
            score -= 15
            logger.debug(f"[쿨다운페널티] {symbol} -15점 (24시간 재매수 억제)")

        score = max(0.0, min(100.0, score))

        # 신호 판정
        if   score >= 72: signal = "BUY"
        elif score <= 35: signal = "SELL"
        else:             signal = "HOLD"

        return {
            "cur_price":   round(cur, 2),
            "pct_1d":      pct,
            "pct_3d":      round(pct3d, 2),
            "pct_5d":      round(pct5d, 2),
            "ma5":         round(ma5, 2),
            "ma20":        round(ma20, 2),
            "rsi":         rsi,
            "macd":        macd,
            "from_high52": round(from_high, 1),
            "vol_ratio":   round(vol_ratio, 2),
            "turnover_m":  round(turnover / 1_000_000, 2),  # 백만달러
            "score":       round(score, 1),
            "signal":      signal,
            "penalized":   symbol in recent_loss_syms or symbol in cooldown_syms,
        }
    except Exception as e:
        logger.warning(f"종목 분석 실패 {symbol}: {e}")
        return {"score": 40.0, "signal": "HOLD", "pct_1d": 0.0,
                "vol_ratio": 1.0, "turnover_m": 0.0, "penalized": False}


# ════════════════════════════════════════════════════════════════
# 장중 실시간 스크리닝 (분봉 기반)
# ════════════════════════════════════════════════════════════════

def _score_stock_intraday(ticker_obj, symbol: str,
                          recent_loss_syms: set, cooldown_syms: set) -> dict:
    """
    장중 실시간 점수 산출 — 당일 분봉 + 전일 종가 기반
    ★ 상승률·거래량·거래대금 중심
    """
    try:
        hist_1d = ticker_obj.history(period="2d", interval="1d")
        hist_5m = ticker_obj.history(period="1d", interval="5m")

        if hist_1d.empty or len(hist_1d) < 2 or hist_5m.empty:
            return {"score": 0.0, "signal": "SKIP", "pct_today": 0.0}

        prev_close = float(hist_1d["Close"].iloc[-2])
        cur_price  = float(hist_5m["Close"].iloc[-1])
        pct_today  = _safe_pct(cur_price, prev_close)

        # 당일 누적 거래량 및 거래대금
        total_vol = float(hist_5m["Volume"].sum())
        turnover  = cur_price * total_vol

        # 최근 5분봉 vs 직전 평균 (단기 가속도)
        recent_vol_avg = float(hist_5m["Volume"].tail(3).mean())
        early_vol_avg  = float(hist_5m["Volume"].head(6).mean()) if len(hist_5m) >= 6 else recent_vol_avg
        vol_accel      = recent_vol_avg / early_vol_avg if early_vol_avg > 0 else 1.0

        # 전일 일봉에서 MA, RSI
        hist_90d = ticker_obj.history(period="90d")
        rsi = 50.0
        from_high = 0.0
        if not hist_90d.empty and len(hist_90d) >= 15:
            rsi = _calc_rsi(hist_90d["Close"])
            high52 = float(hist_90d["Close"].tail(252).max()) if len(hist_90d) >= 252 else float(hist_90d["Close"].max())
            from_high = _safe_pct(cur_price, high52)

        score = 35.0

        # ① 당일 상승률 (장중 핵심 지표)
        if   pct_today >= 10.0: score += 25
        elif pct_today >= 7.0:  score += 20
        elif pct_today >= 5.0:  score += 15
        elif pct_today >= 3.0:  score += 10
        elif pct_today >= 1.5:  score += 5
        elif pct_today < -3.0:  score -= 15
        elif pct_today < -1.5:  score -= 6

        # ② 거래대금 ($M)
        if   turnover >= 100_000_000:  score += 15  # $100M+: 초대형 거래
        elif turnover >= 30_000_000:   score += 10
        elif turnover >= 10_000_000:   score += 6
        elif turnover >= 3_000_000:    score += 3
        elif turnover < 500_000:       score -= 10  # 유동성 부족

        # ③ 거래량 가속도 (최근 5분 vs 장 초반)
        if   vol_accel >= 3.0:  score += 10
        elif vol_accel >= 2.0:  score += 6
        elif vol_accel >= 1.5:  score += 3

        # ④ RSI (장중은 과열 허용)
        if   45 <= rsi <= 75: score += 6
        elif rsi > 85:        score -= 4

        # ⑤ 52주 고점 근접
        if   from_high >= -3.0:  score += 8
        elif from_high >= -10.0: score += 3

        # 패널티
        if symbol in recent_loss_syms: score -= 10
        if symbol in cooldown_syms:    score -= 15

        score = max(0.0, min(100.0, score))
        signal = "BUY" if score >= 68 else ("SELL" if score <= 30 else "HOLD")

        return {
            "cur_price":  round(cur_price, 2),
            "pct_today":  round(pct_today, 2),
            "turnover_m": round(turnover / 1_000_000, 2),
            "vol_accel":  round(vol_accel, 2),
            "rsi":        rsi,
            "from_high52":round(from_high, 1),
            "score":      round(score, 1),
            "signal":     signal,
            "penalized":  symbol in recent_loss_syms or symbol in cooldown_syms,
        }
    except Exception as e:
        logger.debug(f"장중 분석 실패 {symbol}: {e}")
        return {"score": 0.0, "signal": "SKIP", "pct_today": 0.0}


# ════════════════════════════════════════════════════════════════
# 섹터 분산 강제 선정 함수
# ════════════════════════════════════════════════════════════════

def _apply_sector_diversity(candidates: list, max_total: int = 25) -> list:
    """
    BUY 후보 리스트에서 섹터 분산 적용하여 최종 후보 선정
    - 각 섹터별 SECTOR_MAX 초과 종목 제외
    - 점수 순서 유지 (높은 점수 우선)
    """
    sector_count = defaultdict(int)
    final = []
    excluded = []

    for c in candidates:
        sym    = c.get("symbol", "")
        sector = US_STOCKS.get(sym, {}).get("sector", "GROWTH")
        limit  = SECTOR_MAX.get(sector, 3)

        if sector_count[sector] < limit:
            final.append(c)
            sector_count[sector] += 1
        else:
            excluded.append(f"{sym}({sector})")

        if len(final) >= max_total:
            break

    if excluded:
        logger.info(f"[US 스크리닝] 섹터초과 제외: {excluded[:10]}")
    return final


# ════════════════════════════════════════════════════════════════
# ★ v3 신규: 섹터 강도 분석 — 당일 강세 섹터 TOP 선정
# ════════════════════════════════════════════════════════════════

# 섹터 코드 → 종목 풀 섹터 태그 매핑
SECTOR_ETF_TO_TAG = {
    "XLK":  ["AI", "SEMI"],
    "XLF":  ["FINTECH"],
    "XLV":  ["BIO"],
    "XLE":  ["NUCLEAR", "EV"],
    "XLI":  ["DEFENSE"],
    "XLC":  ["AI", "GROWTH"],
    "XLY":  ["GROWTH"],
    "XLP":  ["GROWTH"],
    "XLB":  ["GROWTH"],
    "XLRE": ["FINTECH"],
    "XLU":  ["NUCLEAR"],
    "ITA":  ["DEFENSE"],
    "URA":  ["NUCLEAR"],
    "ICLN": ["EV", "NUCLEAR"],
}

# 섹터 한글 이름 → 표시용
SECTOR_ETF_NAMES = {
    "XLK":  "기술(AI/반도체)",
    "XLF":  "금융/핀테크",
    "XLV":  "헬스케어/바이오",
    "XLE":  "에너지",
    "XLI":  "산업/방산",
    "XLC":  "통신/미디어",
    "XLY":  "경기소비재",
    "XLP":  "필수소비재",
    "XLB":  "소재",
    "XLRE": "부동산",
    "XLU":  "유틸리티",
    "ITA":  "방위산업",
    "URA":  "원자력/우라늄",
    "ICLN": "청정에너지",
}


def _analyze_sector_strength(sector_results: list) -> dict:
    """
    섹터 ETF 분석 결과에서 강세/약세 섹터 판정.

    Returns:
        {
            "strong_sector_tags": set,   # 강세 섹터 종목 태그 집합
            "strong_etfs": list,         # 강세 ETF 목록 (pct_1d > 0)
            "strong_names": list,        # 한글 섹터명 목록
            "sector_bonus": dict,        # sector_tag → bonus_score
        }
    """
    # 1일 수익률 기준 정렬
    sorted_sectors = sorted(sector_results, key=lambda x: x.get("pct_1d", 0), reverse=True)

    # 상위 5개 강세 섹터 (1일 수익률 > -0.3% 이상)
    strong_etfs = [
        s for s in sorted_sectors
        if s.get("pct_1d", 0) > -0.3
    ][:5]

    strong_sector_tags = set()
    sector_bonus = {}
    strong_names = []

    for rank, sec in enumerate(strong_etfs):
        etf_sym = sec.get("symbol", "")
        tags    = SECTOR_ETF_TO_TAG.get(etf_sym, [])
        pct_1d  = sec.get("pct_1d", 0)
        name    = SECTOR_ETF_NAMES.get(etf_sym, etf_sym)

        strong_sector_tags.update(tags)
        strong_names.append(name)

        # 보너스 점수: 1위 +20, 2위 +15, 3위 +10, 4위 +7, 5위 +5
        bonus = [20, 15, 10, 7, 5][rank] if rank < 5 else 3
        # 추가: 1일 상승률 비례 보너스
        if pct_1d > 1.5:
            bonus += 5
        elif pct_1d > 0.5:
            bonus += 2

        for tag in tags:
            sector_bonus[tag] = max(sector_bonus.get(tag, 0), bonus)

    return {
        "strong_sector_tags": strong_sector_tags,
        "strong_etfs":        [s["symbol"] for s in strong_etfs],
        "strong_names":       strong_names,
        "sector_bonus":       sector_bonus,
        "all_sorted":         sorted_sectors,
    }


def _calc_obv(hist: "pd.DataFrame") -> bool:
    """OBV (On-Balance Volume) 상승 추세 여부 반환."""
    try:
        close  = hist["Close"]
        volume = hist["Volume"]
        direction = close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        obv = (direction * volume).cumsum()
        obv_ma5 = obv.rolling(5).mean()
        return bool(float(obv.iloc[-1]) > float(obv_ma5.iloc[-1]))
    except Exception:
        return False


def _calc_vwap(hist_intraday: "pd.DataFrame") -> float:
    """당일 VWAP 계산."""
    try:
        typical_price = (hist_intraday["High"] + hist_intraday["Low"] + hist_intraday["Close"]) / 3
        vwap = (typical_price * hist_intraday["Volume"]).sum() / hist_intraday["Volume"].sum()
        return float(vwap)
    except Exception:
        return 0.0


def _score_stock_daily_v3(
    hist: "pd.DataFrame",
    symbol: str,
    recent_loss_syms: set,
    cooldown_syms: set,
    penalty_syms: set,
    today_profit_syms: set,
    sector_bonus: dict,
) -> dict:
    """
    v3 일봉 기반 기술적 점수 산출 (0~100)
    ★ 섹터 강도 보너스 + 7일 손절 페널티 + 당일 익절 차단 추가
    """
    try:
        c = hist["Close"]
        v = hist["Volume"]

        cur   = float(c.iloc[-1])
        prev  = float(c.iloc[-2])
        pct   = _safe_pct(cur, prev)

        ma5   = float(c.rolling(5).mean().iloc[-1])
        ma20  = float(c.rolling(20).mean().iloc[-1])
        ma60  = float(c.rolling(60).mean().iloc[-1]) if len(c) >= 60 else ma20
        rsi   = _calc_rsi(c)
        macd  = _calc_macd_signal(c)

        pct5d = _safe_pct(cur, float(c.iloc[-6])) if len(c) >= 6 else pct
        pct3d = _safe_pct(cur, float(c.iloc[-4])) if len(c) >= 4 else pct

        high52    = float(c.tail(252).max()) if len(c) >= 252 else float(c.max())
        from_high = _safe_pct(cur, high52)

        vol_avg5  = float(v.rolling(5).mean().iloc[-1])
        vol_avg20 = float(v.rolling(20).mean().iloc[-1]) if len(v) >= 20 else vol_avg5
        vol_ratio = float(v.iloc[-1] / vol_avg5) if vol_avg5 > 0 else 1.0

        # 거래대금 (USD)
        turnover = cur * float(v.iloc[-1])

        # OBV
        obv_rising = _calc_obv(hist)

        # ── 점수 산출 ────────────────────────────────────────────
        score = 40.0

        # ① 거래량 급증 (최우선 — 당일 신규 관심 지표)
        if   vol_ratio >= 5.0:  score += 22
        elif vol_ratio >= 3.0:  score += 16
        elif vol_ratio >= 2.0:  score += 10
        elif vol_ratio >= 1.5:  score += 5

        # ② 당일 등락률
        if   pct >= 8.0:  score += 18
        elif pct >= 5.0:  score += 13
        elif pct >= 3.0:  score += 8
        elif pct >= 1.0:  score += 4
        elif pct < -3.0:  score -= 12
        elif pct < -1.5:  score -= 5

        # ③ 5일 모멘텀
        if   pct5d >= 15.0: score += 10
        elif pct5d >= 8.0:  score += 6
        elif pct5d >= 3.0:  score += 3
        elif pct5d < -10.0: score -= 8

        # ④ MA 정배열
        if cur > ma5:   score += 4
        if cur > ma20:  score += 5
        if ma5  > ma20: score += 4
        if ma20 > ma60: score += 3

        # ⑤ RSI
        if   45 <= rsi <= 70: score += 8
        elif 35 <= rsi < 45:  score += 3
        elif rsi > 80:        score -= 5
        elif rsi < 25:        score += 2

        # ⑥ MACD
        if "골든" in macd:  score += 10
        elif "상승" in macd: score += 4
        if "데드"  in macd:  score -= 8

        # ⑦ 52주 고점 근접
        if   from_high >= -3.0:  score += 10
        elif from_high >= -10.0: score += 4
        elif from_high < -50.0:  score -= 5

        # ⑧ 거래대금 필터 ($1M 이상)
        if turnover < 1_000_000:  score -= 10

        # ⑨ OBV 상승 (매수세 확인)
        if obv_rising:  score += 6

        # ★ v3 신규: 섹터 보너스 (강세 섹터 종목 우선)
        sector = US_STOCKS.get(symbol, {}).get("sector", "GROWTH")
        bonus  = sector_bonus.get(sector, 0)
        score += bonus

        # ── 패널티 ───────────────────────────────────────────────
        # ⑩ 최근 3일 손실 종목 (-10점)
        if symbol in recent_loss_syms:
            score -= 10

        # ⑪ 24시간 쿨다운 종목 (-15점)
        if symbol in cooldown_syms:
            score -= 15

        # ⑫ ★ v3 신규: 7일 손절 페널티 (-20점)
        if symbol in penalty_syms:
            score -= 20

        # ⑬ ★ v3 신규: 당일 익절 종목 → 완전 차단 (score 0으로)
        if symbol in today_profit_syms:
            score = 0.0

        score = max(0.0, min(100.0, score))

        # 신호 판정
        if   score >= 72: signal = "BUY"
        elif score <= 35: signal = "SELL"
        else:             signal = "HOLD"

        return {
            "cur_price":    round(cur, 2),
            "pct_1d":       pct,
            "pct_3d":       round(pct3d, 2),
            "pct_5d":       round(pct5d, 2),
            "ma5":          round(ma5, 2),
            "ma20":         round(ma20, 2),
            "rsi":          rsi,
            "macd":         macd,
            "from_high52":  round(from_high, 1),
            "vol_ratio":    round(vol_ratio, 2),
            "turnover_m":   round(turnover / 1_000_000, 2),
            "obv_rising":   obv_rising,
            "sector_bonus": bonus,
            "score":        round(score, 1),
            "signal":       signal,
            "penalized":    (symbol in recent_loss_syms or symbol in cooldown_syms
                             or symbol in penalty_syms),
            "blocked":      symbol in today_profit_syms,
        }
    except Exception as e:
        logger.warning(f"종목 분석 실패 {symbol}: {e}")
        return {"score": 40.0, "signal": "HOLD", "pct_1d": 0.0,
                "vol_ratio": 1.0, "turnover_m": 0.0, "penalized": False, "blocked": False}


def _detect_us_regime(sp500_pct: float, vix: float, nasdaq_pct: float) -> str:
    if vix >= 30:
        return "BEAR"
    elif sp500_pct >= 0.3 and vix < 20:
        return "BULL"
    elif sp500_pct <= -0.5:
        return "BEAR"
    else:
        return "LATERAL"


def _korea_impact(us_regime: str, sp500_pct: float,
                  nasdaq_pct: float, vix: float) -> dict:
    if us_regime == "BULL":
        direction = "상승 예상"; emoji = "🟢"
        reason    = f"S&P500 {sp500_pct:+.2f}%, VIX {vix:.1f} → 코스피 동반 상승"
        kospi_est = round(sp500_pct * 0.7, 2)
    elif us_regime == "BEAR":
        direction = "하락 예상"; emoji = "🔴"
        reason    = f"S&P500 {sp500_pct:+.2f}%, VIX {vix:.1f} → 코스피 하락 주의"
        kospi_est = round(sp500_pct * 0.8, 2)
    else:
        direction = "보합/혼조"; emoji = "🟡"
        reason    = f"S&P500 {sp500_pct:+.2f}%, 방향성 불명확"
        kospi_est = round(sp500_pct * 0.5, 2)

    semicon_note = ""
    if nasdaq_pct >= 1.0:
        semicon_note = "💡 나스닥 강세 → 삼성·하이닉스 수혜"
    elif nasdaq_pct <= -1.0:
        semicon_note = "⚠️ 나스닥 약세 → 반도체주 하락 압력"

    return {"direction": direction, "emoji": emoji, "reason": reason,
            "kospi_est": kospi_est, "semicon_note": semicon_note}


# ════════════════════════════════════════════════════════════════
# 메인 스크리닝 함수 (야간 — 일봉 기반)
# ════════════════════════════════════════════════════════════════

def run_us_screening() -> dict:
    """
    미국 증시 종합 분석 v3 (야간 00:00 KST 실행)
    ★ 섹터 강도 자동 선정 → 강세 섹터 내 신규 종목 발굴 → 페널티 시스템 통합
    """
    if not HAS_YFINANCE:
        logger.error("yfinance 미설치 — pip install yfinance")
        return {"error": "yfinance 미설치"}

    logger.info("🇺🇸 [US DAILY SCREENER] 야간 종합 분석 시작 (v3)...")
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    # ── 0. 페널티/쿨다운 로드 ─────────────────────────────────
    recent_loss_syms  = _load_recent_loss()
    cooldown_syms     = _load_cooldown()

    # ★ v3: watchlist_manager에서 7일 페널티 + 당일 익절 로드
    try:
        from screener.us_watchlist_manager import get_penalty_symbols, get_today_profit_symbols
        penalty_syms     = get_penalty_symbols()
        today_profit_syms = get_today_profit_symbols()
    except Exception:
        penalty_syms      = set()
        today_profit_syms = set()

    logger.info(
        f"[US DAILY SCREENER] 패널티현황 | "
        f"3일손실={len(recent_loss_syms)} | 24h쿨다운={len(cooldown_syms)} | "
        f"7일손절={len(penalty_syms)} | 당일익절재진입금지={len(today_profit_syms)}"
    )

    result = {
        "analyzed_at":  now_kst,
        "date":         date.today().isoformat(),
        "mode":         "overnight_v3",
        "indices":      {},
        "sectors":      {},
        "stocks":       {},
        "regime":       "LATERAL",
        "korea_impact": {},
        "top_buy":      [],
        "summary":      {},
        "sector_strength": {},
    }

    # ── 1. 주요 지수 ──────────────────────────────────────────
    sp500_pct = nasdaq_pct = 0.0
    vix_val   = 20.0
    for sym, meta in US_INDICES.items():
        try:
            hist = yf.Ticker(sym).history(period="5d")
            if hist.empty or len(hist) < 2:
                continue
            cur  = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            pct  = _safe_pct(cur, prev)
            result["indices"][sym] = {
                "name": meta["name"], "emoji": meta["emoji"],
                "price": round(cur, 2), "pct_1d": pct,
            }
            if sym == "^GSPC": sp500_pct  = pct
            if sym == "^IXIC": nasdaq_pct = pct
            if sym == "^VIX":  vix_val    = cur
        except Exception as e:
            logger.warning(f"지수 {sym} 조회 실패: {e}")

    # ── 2. 섹터 분석 + ★ v3: 섹터 강도 판정 ─────────────────
    sector_results = []
    for sym, name in US_SECTORS.items():
        try:
            hist = yf.Ticker(sym).history(period="30d")
            if hist.empty or len(hist) < 5:
                continue
            c     = hist["Close"]
            v     = hist["Volume"]
            cur   = float(c.iloc[-1])
            prev  = float(c.iloc[-2])
            pct   = _safe_pct(cur, prev)
            pct5d = _safe_pct(cur, float(c.iloc[-5])) if len(c) >= 5 else pct
            pct20d = _safe_pct(cur, float(c.iloc[-20])) if len(c) >= 20 else pct5d
            vol_ratio = float(v.iloc[-1] / v.rolling(5).mean().iloc[-1]) if float(v.rolling(5).mean().iloc[-1]) > 0 else 1.0
            entry = {
                "symbol":    sym,
                "name":      SECTOR_ETF_NAMES.get(sym, name),
                "price":     round(cur, 2),
                "pct_1d":    pct,
                "pct_5d":    pct5d,
                "pct_20d":   pct20d,
                "vol_ratio": round(vol_ratio, 2),
            }
            result["sectors"][sym] = entry
            sector_results.append(entry)
        except Exception as e:
            logger.warning(f"섹터 {sym} 조회 실패: {e}")

    sector_results.sort(key=lambda x: x["pct_1d"], reverse=True)

    # ★ v3: 섹터 강도 분석
    sector_strength = _analyze_sector_strength(sector_results)
    sector_bonus    = sector_strength["sector_bonus"]
    strong_names    = sector_strength["strong_names"]
    strong_sector_tags = sector_strength["strong_sector_tags"]

    result["sector_strength"] = {
        "strong_etfs":  sector_strength["strong_etfs"],
        "strong_names": strong_names,
        "sector_bonus": sector_bonus,
    }

    logger.info(
        f"[US DAILY SCREENER] 섹터강도 분석 완료 | "
        f"강세섹터={strong_names[:5]} | "
        f"보너스태그={dict(list(sector_bonus.items())[:5])}"
    )

    # ── 3. ★ v3: 전체 종목 분석 (섹터 보너스 반영) ───────────
    all_stocks     = list(US_STOCKS.items())
    total_count    = len(all_stocks)
    buy_candidates = []
    analyzed_count = 0
    blocked_count  = 0

    for sym, meta in all_stocks:
        try:
            hist = yf.Ticker(sym).history(period="90d")
            if hist.empty or len(hist) < 20:
                continue
            analyzed_count += 1

            # ★ v3 점수 산출 (섹터 보너스 + 7일 페널티 + 당일 익절 차단 포함)
            analysis = _score_stock_daily_v3(
                hist, sym,
                recent_loss_syms, cooldown_syms,
                penalty_syms, today_profit_syms,
                sector_bonus,
            )
            analysis["symbol"] = sym
            analysis["name"]   = meta["name"]
            analysis["sector"] = meta.get("sector", "GROWTH")
            analysis["excd"]   = meta.get("excd", "NASD")
            result["stocks"][sym] = analysis

            if analysis.get("blocked", False):
                blocked_count += 1
                continue  # 당일 익절 → 스킵

            if analysis["signal"] == "BUY":
                buy_candidates.append(analysis)
        except Exception as e:
            logger.warning(f"종목 {sym} 분석 실패: {e}")

    # ── 4. 섹터 분산 적용 → 최종 후보 선정 ───────────────────
    # ★ v3: 강세 섹터 종목 먼저, 나머지는 점수순
    strong_candidates = [c for c in buy_candidates
                         if c.get("sector","") in strong_sector_tags]
    other_candidates  = [c for c in buy_candidates
                         if c.get("sector","") not in strong_sector_tags]

    strong_candidates.sort(key=lambda x: x["score"], reverse=True)
    other_candidates.sort(key=lambda x: x["score"], reverse=True)

    # 강세 섹터 우선으로 병합
    merged = strong_candidates + other_candidates
    final_candidates = _apply_sector_diversity(merged, max_total=25)
    result["top_buy"] = final_candidates

    # ── 5. 섹터 분포 집계 ─────────────────────────────────────
    sector_dist = defaultdict(int)
    for c in final_candidates:
        sector_dist[SECTOR_NAMES.get(c.get("sector",""), c.get("sector",""))] += 1

    # ── 6. [US DAILY SCREENER] 로그 출력 ─────────────────────
    sector_str = " / ".join(strong_names[:5]) if strong_names else "분석중"
    candidate_str = ", ".join(
        f"{c['symbol']}({c.get('sector','?')},{c.get('score',0):.0f}점)"
        for c in final_candidates[:8]
    )
    excluded_parts = []
    if today_profit_syms:
        excluded_parts.append(f"당일익절:{','.join(sorted(today_profit_syms)[:5])}")
    if penalty_syms:
        excluded_parts.append(f"7일패널티:{','.join(sorted(penalty_syms)[:5])}")
    if recent_loss_syms:
        excluded_parts.append(f"3일손실:{','.join(sorted(recent_loss_syms)[:5])}")
    excluded_str = " | ".join(excluded_parts) if excluded_parts else "없음"

    watch_syms = [c["symbol"] for c in final_candidates]
    watch_str  = ", ".join(watch_syms[:15])

    logger.info(f"[US DAILY SCREENER] 시장 분석 종목수={analyzed_count}/{total_count} (당일익절차단={blocked_count})")
    logger.info(f"[US DAILY SCREENER] 강세 섹터={sector_str}")
    logger.info(f"[US DAILY SCREENER] 신규 후보={candidate_str}")
    logger.info(f"[US DAILY SCREENER] 제외 종목={excluded_str}")
    logger.info(f"[US DAILY SCREENER] 최종 감시종목={watch_str} (총{len(final_candidates)}개)")
    logger.info(f"[US DAILY SCREENER] 섹터 분포={dict(sector_dist)}")

    # ── 7. 시장 국면 + 한국 영향 ──────────────────────────────
    regime = _detect_us_regime(sp500_pct, vix_val, nasdaq_pct)
    result["regime"]       = regime
    result["korea_impact"] = _korea_impact(regime, sp500_pct, nasdaq_pct, vix_val)

    # ── 8. 요약 ───────────────────────────────────────────────
    bull_sectors = [s for s in sector_results if s["pct_1d"] > 0]
    result["summary"] = {
        "regime":           regime,
        "sp500_pct":        sp500_pct,
        "nasdaq_pct":       nasdaq_pct,
        "vix":              round(vix_val, 2),
        "bull_sectors":     len(bull_sectors),
        "bear_sectors":     len(sector_results) - len(bull_sectors),
        "top_sector":       sector_results[0]["name"] if sector_results else "—",
        "worst_sector":     sector_results[-1]["name"] if sector_results else "—",
        "strong_sectors":   strong_names[:5],
        "analyzed":         analyzed_count,
        "blocked_today":    blocked_count,
        "buy_raw":          len(buy_candidates),
        "buy_final":        len(final_candidates),
        "sector_dist":      dict(sector_dist),
        "buy_candidates":   len(buy_candidates),
        "analyzed_at":      now_kst,
        "mode":             "overnight_v3",
    }

    # ── 저장 ──────────────────────────────────────────────────
    os.makedirs(os.path.dirname(US_RESULT_FILE), exist_ok=True)
    with open(US_RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(
        f"✅ [US DAILY SCREENER] 야간 완료 — 국면:{regime} | "
        f"S&P500:{sp500_pct:+.2f}% | VIX:{vix_val:.1f} | "
        f"분석:{analyzed_count}종목 | 최종후보:{len(final_candidates)}종목"
    )
    return result


# ════════════════════════════════════════════════════════════════
# 장중 실시간 스크리닝 (ET 10:30 실행 — 장 개시 1시간 후)
# ════════════════════════════════════════════════════════════════

def run_us_intraday_screening() -> dict:
    """
    장중 실시간 스크리닝 — 당일 상승률/거래대금/거래량 급증 상위 선정
    ★ 야간 스크리닝과 병합 → 당일 강세 종목 우선 반영
    """
    if not HAS_YFINANCE:
        return {"error": "yfinance 미설치"}

    logger.info("🇺🇸 [US 스크리닝] 장중 실시간 스크리닝 시작 (ET 10:30)...")
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    recent_loss_syms = _load_recent_loss()
    cooldown_syms    = _load_cooldown()

    all_syms  = list(US_STOCKS.items())
    total     = len(all_syms)
    analyzed  = 0
    candidates = []

    # 배치 다운로드 (yfinance 멀티 티커 — 속도 최적화)
    sym_list = [s for s, _ in all_syms]
    logger.info(f"[US 스크리닝] 장중 분석 대상: {total}종목 배치 다운로드 중...")

    try:
        # 1d 5분봉 일괄 다운로드
        tickers_data = yf.download(
            sym_list, period="2d", interval="5m",
            group_by="ticker", auto_adjust=True, progress=False
        )
    except Exception as e:
        logger.warning(f"배치 다운로드 실패, 개별 조회로 전환: {e}")
        tickers_data = None

    for sym, meta in all_syms:
        try:
            tk = yf.Ticker(sym)
            score_dict = _score_stock_intraday(tk, sym, recent_loss_syms, cooldown_syms)
            if score_dict.get("signal") == "SKIP":
                continue
            analyzed += 1
            score_dict["symbol"] = sym
            score_dict["name"]   = meta["name"]
            score_dict["sector"] = meta.get("sector", "GROWTH")
            score_dict["excd"]   = meta.get("excd", "NASD")

            if score_dict["signal"] == "BUY":
                candidates.append(score_dict)
        except Exception as e:
            logger.debug(f"장중 {sym} 분석 오류: {e}")

    # 정렬: 당일 상승률 + 점수 혼합
    candidates.sort(key=lambda x: (x.get("score", 0) * 0.6 +
                                    x.get("pct_today", 0) * 2.0), reverse=True)

    # 섹터 분산 적용
    final = _apply_sector_diversity(candidates, max_total=25)

    sector_dist = defaultdict(int)
    for c in final:
        sector_dist[SECTOR_NAMES.get(c.get("sector",""), c.get("sector",""))] += 1

    # ★ v3: 당일 익절 차단 + 강세 섹터 우선 정렬
    try:
        from screener.us_watchlist_manager import get_today_profit_symbols, get_penalty_symbols
        today_profit_syms_intra = get_today_profit_symbols()
        penalty_syms_intra      = get_penalty_symbols()
    except Exception:
        today_profit_syms_intra = set()
        penalty_syms_intra      = set()

    # 당일 익절 종목 제거
    candidates = [
        c for c in candidates
        if c.get("symbol","") not in today_profit_syms_intra
    ]

    # 정렬: 당일 상승률 + 점수 혼합
    candidates.sort(key=lambda x: (x.get("score", 0) * 0.6 +
                                    x.get("pct_today", 0) * 2.0), reverse=True)

    # 섹터 분산 적용
    final = _apply_sector_diversity(candidates, max_total=25)

    sector_dist = defaultdict(int)
    for c in final:
        sector_dist[SECTOR_NAMES.get(c.get("sector",""), c.get("sector",""))] += 1

    # ── [US DAILY SCREENER] 장중 로그 출력 ───────────────────
    intraday_top10_str = ", ".join(
        f"{c['symbol']}({c.get('pct_today', 0):+.1f}%,{c.get('score', 0):.0f}점)"
        for c in final[:10]
    )
    blocked_intra = sorted(today_profit_syms_intra)

    logger.info(f"[US DAILY SCREENER] 시장 분석 종목수={analyzed}/{total}")
    logger.info(f"[US DAILY SCREENER] 신규 후보={intraday_top10_str}")
    logger.info(f"[US DAILY SCREENER] 제외 종목=당일익절:{blocked_intra[:5]}")
    logger.info(f"[US DAILY SCREENER] 섹터 분포={dict(sector_dist)}")
    logger.info(
        f"[US DAILY SCREENER] 최종 감시종목="
        + ", ".join(c["symbol"] for c in final[:15])
        + f" (총{len(final)}개)"
    )

    result = {
        "analyzed_at":          now_kst,
        "date":                 date.today().isoformat(),
        "mode":                 "intraday_v3",
        "analyzed":             analyzed,
        "buy_raw":              len(candidates),
        "buy_final":            len(final),
        "sector_dist":          dict(sector_dist),
        "top_buy":              final,
        "cooldown":             sorted(cooldown_syms),
        "recent_loss":          sorted(recent_loss_syms),
        "today_profit_blocked": sorted(today_profit_syms_intra),
    }

    os.makedirs(os.path.dirname(US_INTRADAY_FILE), exist_ok=True)
    with open(US_INTRADAY_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(
        f"✅ [US DAILY SCREENER] 장중 완료 — 분석:{analyzed}종목 | "
        f"최종후보:{len(final)}종목 | 섹터분포:{dict(sector_dist)}"
    )
    return result


def load_us_result() -> dict:
    """저장된 야간 분석 결과 로드"""
    try:
        if os.path.exists(US_RESULT_FILE):
            with open(US_RESULT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"야간 결과 로드 실패: {e}")
    return {}


def load_us_intraday() -> dict:
    """저장된 장중 스크리닝 결과 로드"""
    try:
        if os.path.exists(US_INTRADAY_FILE):
            with open(US_INTRADAY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"장중 결과 로드 실패: {e}")
    return {}


if __name__ == "__main__":
    result = run_us_screening()
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print("\n🔺 매수 TOP10:")
    for s in result["top_buy"][:10]:
        print(
            f"  [{s.get('sector','?'):10s}] {s['name']}({s['symbol']})"
            f" ${s.get('cur_price',0)} | 점수:{s['score']} | "
            f"+{s.get('pct_1d',0):.1f}% | vol:{s.get('vol_ratio',0):.1f}x"
        )
