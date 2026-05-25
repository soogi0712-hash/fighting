"""
투자 대상 자산군 정의 (Asset Universe)
=======================================

★ 자산군 분류 ★
  STOCK_KOSPI    — 코스피 개별주식
  STOCK_KOSDAQ   — 코스닥 개별주식
  ETF_GENERAL    — 일반 ETF (시장 추종, 섹터, 채권 등)
  ETF_LEVERAGE   — 레버리지 ETF (2x / 3x)
  ETF_INVERSE    — 인버스 ETF (-1x / -2x)

★ 비중 한도 (계좌 전체 대비) ★
  레버리지 ETF  ≤ 20%
  인버스 ETF    ≤ 40%
  현금 보유     ≥ 10%  (항상)

★ 레버리지 ETF 진입 조건 (AND) ★
  1. 시장 국면 = BULL
  2. AI 점수 상위 (≥ 80점)
  3. 상대강도 양수 (RS > 0)

★ 시장 국면별 자산군 우선순위 ★
  BULL:    개별주식 > 레버리지ETF > 일반ETF > 인버스ETF
  LATERAL: 일반ETF > 개별주식 > 인버스ETF > 레버리지ETF
  BEAR:    인버스ETF > 현금 > 일반ETF > 개별주식
"""

import re
from dataclasses import dataclass, field
from typing import Optional

# ── 자산군 타입 상수 ────────────────────────────────────────
ASSET_STOCK_KOSPI   = "STOCK_KOSPI"
ASSET_STOCK_KOSDAQ  = "STOCK_KOSDAQ"
ASSET_ETF_GENERAL   = "ETF_GENERAL"
ASSET_ETF_LEVERAGE  = "ETF_LEVERAGE"
ASSET_ETF_INVERSE   = "ETF_INVERSE"
ASSET_CASH          = "CASH"

# 주식 자산군 집합
ASSET_STOCKS = {ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ}
# ETF 자산군 집합
ASSET_ETFS   = {ASSET_ETF_GENERAL, ASSET_ETF_LEVERAGE, ASSET_ETF_INVERSE}
# 전체
ALL_ASSET_TYPES = list(ASSET_STOCKS | ASSET_ETFS)

# ── 비중 한도 ────────────────────────────────────────────────
WEIGHT_LIMITS = {
    ASSET_ETF_LEVERAGE: 0.20,   # 레버리지 ETF ≤ 20%
    ASSET_ETF_INVERSE:  0.40,   # 인버스 ETF  ≤ 40%
    ASSET_CASH:         0.10,   # 현금 최소 ≥ 10% (= 투자 최대 90%)
}
MAX_INVEST_RATIO = 1.0 - WEIGHT_LIMITS[ASSET_CASH]   # 투자 가능 최대 90%

# ── 시장 국면별 자산군 우선순위 ─────────────────────────────
# 리스트 앞쪽 = 높은 우선순위
REGIME_PRIORITY = {
    "BULL": [
        ASSET_STOCK_KOSPI,
        ASSET_STOCK_KOSDAQ,
        ASSET_ETF_LEVERAGE,
        ASSET_ETF_GENERAL,
        ASSET_ETF_INVERSE,
    ],
    "LATERAL": [
        ASSET_ETF_GENERAL,
        ASSET_STOCK_KOSPI,
        ASSET_STOCK_KOSDAQ,
        ASSET_ETF_INVERSE,
        ASSET_ETF_LEVERAGE,
    ],
    "BEAR": [
        ASSET_ETF_INVERSE,
        ASSET_CASH,
        ASSET_ETF_GENERAL,
        ASSET_STOCK_KOSPI,
        ASSET_STOCK_KOSDAQ,
        ASSET_ETF_LEVERAGE,
    ],
}

# ── 시장 국면별 타겟 배분 비중 ──────────────────────────────
# sum ≤ MAX_INVEST_RATIO (현금 ≥ 10% 항상 유지)
REGIME_TARGET_WEIGHTS = {
    "BULL": {
        ASSET_STOCK_KOSPI:  0.40,   # 코스피 개별주식 40%
        ASSET_STOCK_KOSDAQ: 0.25,   # 코스닥 개별주식 25%
        ASSET_ETF_LEVERAGE: 0.15,   # 레버리지 ETF 15% (한도 20% 이하)
        ASSET_ETF_GENERAL:  0.10,   # 일반 ETF 10%
        ASSET_ETF_INVERSE:  0.00,   # 인버스 ETF 0%
        # 현금 = 10%
    },
    "LATERAL": {
        ASSET_STOCK_KOSPI:  0.25,
        ASSET_STOCK_KOSDAQ: 0.15,
        ASSET_ETF_GENERAL:  0.35,   # 일반 ETF 비중 상향
        ASSET_ETF_LEVERAGE: 0.00,   # 레버리지 비활성
        ASSET_ETF_INVERSE:  0.10,   # 인버스 ETF 소량 헤지
        # 현금 = 15%
    },
    "BEAR": {
        ASSET_STOCK_KOSPI:  0.05,
        ASSET_STOCK_KOSDAQ: 0.05,
        ASSET_ETF_GENERAL:  0.10,
        ASSET_ETF_LEVERAGE: 0.00,   # 레버리지 완전 제한
        ASSET_ETF_INVERSE:  0.35,   # 인버스 ETF 적극 활용
        # 현금 = 45%
    },
}

# ── 대표 ETF 코드 데이터베이스 ──────────────────────────────
# (코드, 이름, 유형, 기초지수, 레버리지배율)
ETF_DATABASE = [
    # ─── 일반 ETF (KOSPI200 / KOSDAQ150 추종) ───
    ("069500", "KODEX 200",              ASSET_ETF_GENERAL,  "KOSPI200",     1.0),
    ("229200", "KODEX 코스닥150",         ASSET_ETF_GENERAL,  "KOSDAQ150",    1.0),
    ("102110", "TIGER 200",              ASSET_ETF_GENERAL,  "KOSPI200",     1.0),
    ("232080", "TIGER 코스닥150",         ASSET_ETF_GENERAL,  "KOSDAQ150",    1.0),
    ("251340", "KODEX 코스피",            ASSET_ETF_GENERAL,  "KOSPI",        1.0),
    ("261670", "KODEX 코스닥",            ASSET_ETF_GENERAL,  "KOSDAQ",       1.0),
    # ─── 섹터 일반 ETF ───
    ("091160", "KODEX 반도체",            ASSET_ETF_GENERAL,  "KRX반도체",    1.0),
    ("091170", "KODEX 은행",              ASSET_ETF_GENERAL,  "KRX은행",      1.0),
    ("140710", "KODEX 미국S&P500TR",      ASSET_ETF_GENERAL,  "S&P500",       1.0),
    ("195930", "KODEX 선진국MSCI World",  ASSET_ETF_GENERAL,  "MSCI World",   1.0),
    ("133690", "TIGER 미국나스닥100",     ASSET_ETF_GENERAL,  "NASDAQ100",    1.0),
    ("379800", "KODEX 미국채10년",        ASSET_ETF_GENERAL,  "미국채10년",   1.0),
    ("157450", "TIGER 미국달러단기채권",  ASSET_ETF_GENERAL,  "미국달러단기채", 1.0),
    # ─── 레버리지 ETF ───
    ("122630", "KODEX 레버리지",          ASSET_ETF_LEVERAGE, "KOSPI200 2x",  2.0),
    ("233740", "KODEX 코스닥150 레버리지",ASSET_ETF_LEVERAGE, "KOSDAQ150 2x", 2.0),
    ("278530", "KODEX 미국S&P500 레버리지",ASSET_ETF_LEVERAGE,"S&P500 2x",   2.0),
    ("367380", "KODEX 미국나스닥100레버리지",ASSET_ETF_LEVERAGE,"NASDAQ100 2x",2.0),
    ("253150", "TIGER 200선물레버리지",   ASSET_ETF_LEVERAGE, "KOSPI200선물2x",2.0),
    # ─── 인버스 ETF ───
    ("114800", "KODEX 인버스",            ASSET_ETF_INVERSE,  "KOSPI200 -1x", -1.0),
    ("251340", "KODEX KOSPI",             ASSET_ETF_GENERAL,  "KOSPI",         1.0),   # 재정의 (아래서 처리)
    ("251590", "ARIRANG 200선물인버스2X", ASSET_ETF_INVERSE,  "KOSPI200 -2x", -2.0),
    ("223760", "KODEX 코스닥150선물인버스",ASSET_ETF_INVERSE, "KOSDAQ150 -1x",-1.0),
    ("219905", "KODEX 미국달러선물인버스",ASSET_ETF_INVERSE,  "USD -1x",      -1.0),
    ("267490", "KODEX 미국채10년인버스",  ASSET_ETF_INVERSE,  "미국채10년 -1x",-1.0),
    ("261250", "KODEX 200선물인버스2X",   ASSET_ETF_INVERSE,  "KOSPI200 -2x", -2.0),
]

# 코드 → ETF 정보 딕셔너리
ETF_CODE_MAP: dict[str, dict] = {}
for _code, _name, _atype, _idx, _mult in ETF_DATABASE:
    ETF_CODE_MAP[_code] = {
        "code":       _code,
        "name":       _name,
        "asset_type": _atype,
        "index":      _idx,
        "multiplier": _mult,
    }

# ── ETF 판별 키워드 패턴 ────────────────────────────────────
_LEV_KEYWORDS  = re.compile(
    r"레버리지|leverage|2X|3X|2배|3배|LEVERAGE", re.IGNORECASE
)
_INV_KEYWORDS  = re.compile(
    r"인버스|inverse|INVERSE|-1X|-2X|곱버스|인버", re.IGNORECASE
)
_ETF_KEYWORDS  = re.compile(
    r"KODEX|TIGER|ARIRANG|KINDEX|KOSEF|HANARO|PLUS|ACE |TIMEFOLIO|"
    r"ETF|ETN|선물인버스|선물레버리지", re.IGNORECASE
)


# ── 자산군 판별 함수 ─────────────────────────────────────────

def classify_asset_type(code: str, name: str, market: str = "") -> str:
    """
    종목 코드·이름·시장 정보로 자산군 타입을 반환한다.

    우선순위:
      1. ETF_CODE_MAP 에 등록된 코드 → 즉시 반환
      2. 이름 키워드로 레버리지/인버스/일반 ETF 판별
      3. 시장 + 기타 → STOCK_KOSPI / STOCK_KOSDAQ
    """
    # 1. DB 등록 코드
    if code in ETF_CODE_MAP:
        return ETF_CODE_MAP[code]["asset_type"]

    # 2. 키워드 판별
    if _ETF_KEYWORDS.search(name):
        if _LEV_KEYWORDS.search(name):
            return ASSET_ETF_LEVERAGE
        if _INV_KEYWORDS.search(name):
            return ASSET_ETF_INVERSE
        return ASSET_ETF_GENERAL

    # 3. 코스닥 종목
    if market == "KOSDAQ":
        return ASSET_STOCK_KOSDAQ

    # 4. 코스피 또는 알 수 없음
    return ASSET_STOCK_KOSPI


def is_etf(asset_type: str) -> bool:
    return asset_type in ASSET_ETFS


def is_leverage_etf(asset_type: str) -> bool:
    return asset_type == ASSET_ETF_LEVERAGE


def is_inverse_etf(asset_type: str) -> bool:
    return asset_type == ASSET_ETF_INVERSE


def get_etf_info(code: str) -> Optional[dict]:
    """ETF 코드로 메타 정보 반환 (없으면 None)"""
    return ETF_CODE_MAP.get(code)


# ── 자산군 우선순위 조회 ─────────────────────────────────────

def get_priority(regime: str) -> list[str]:
    """시장 국면에 따른 자산군 우선순위 리스트"""
    return REGIME_PRIORITY.get(regime, REGIME_PRIORITY["LATERAL"])


def get_target_weights(regime: str) -> dict[str, float]:
    """시장 국면에 따른 타겟 배분 비중"""
    return REGIME_TARGET_WEIGHTS.get(regime, REGIME_TARGET_WEIGHTS["LATERAL"])


def priority_score(asset_type: str, regime: str) -> int:
    """
    종목의 자산군에 대해 현재 시장 국면 우선순위 점수 반환.
    높을수록 우선순위 높음 (리스트 역순 인덱스).
    """
    priority_list = get_priority(regime)
    try:
        idx = priority_list.index(asset_type)
        return len(priority_list) - idx   # 앞쪽일수록 높은 점수
    except ValueError:
        return 0


# ── 레버리지 ETF 진입 조건 체크 ─────────────────────────────

def can_buy_leverage(
    regime:    str,
    ai_score:  float,
    rs_value:  float,
    lever_ratio: float = 0.0,   # 현재 레버리지 비중
) -> tuple[bool, str]:
    """
    레버리지 ETF 매수 가능 여부.
    Returns (ok: bool, reason: str)
    """
    if regime != "BULL":
        return False, f"시장국면 {regime} (BULL만 허용)"
    if ai_score < 80:
        return False, f"AI점수 {ai_score:.0f}점 (≥80 필요)"
    if rs_value <= 0:
        return False, f"RS {rs_value:+.1f}% (양수 필요)"
    if lever_ratio >= WEIGHT_LIMITS[ASSET_ETF_LEVERAGE]:
        return False, f"레버리지 비중 {lever_ratio*100:.1f}% (한도 {WEIGHT_LIMITS[ASSET_ETF_LEVERAGE]*100:.0f}%)"
    return True, "레버리지 ETF 진입 조건 충족"


def can_buy_inverse(
    regime:      str,
    inv_ratio:   float = 0.0,   # 현재 인버스 비중
) -> tuple[bool, str]:
    """
    인버스 ETF 매수 가능 여부.
    """
    if inv_ratio >= WEIGHT_LIMITS[ASSET_ETF_INVERSE]:
        return False, f"인버스 비중 {inv_ratio*100:.1f}% (한도 {WEIGHT_LIMITS[ASSET_ETF_INVERSE]*100:.0f}%)"
    return True, "인버스 ETF 진입 가능"


# ── 비중 초과 체크 ──────────────────────────────────────────

def check_weight_limit(
    asset_type:    str,
    current_ratio: float,   # 현재 해당 자산군 비중
) -> tuple[bool, str]:
    """
    비중 한도 초과 여부.
    Returns (within_limit: bool, msg: str)
    """
    limit = WEIGHT_LIMITS.get(asset_type)
    if limit is None:
        return True, "한도 없음"
    if current_ratio >= limit:
        return False, f"{asset_type} 비중 {current_ratio*100:.1f}% ≥ 한도 {limit*100:.0f}%"
    return True, f"{asset_type} 비중 {current_ratio*100:.1f}% < 한도 {limit*100:.0f}%"


# ── 데모용 ETF 목록 반환 ─────────────────────────────────────

def get_demo_etf_list() -> list[dict]:
    """
    데모 모드에서 사용할 ETF 종목 목록.
    실제 시장 데이터 대신 고정 샘플 사용.
    """
    import random
    demo_etfs = []
    rng = random.Random(9999)

    # DB에서 대표 ETF 샘플링
    sample_codes = [
        "069500",  # KODEX 200
        "229200",  # KODEX 코스닥150
        "122630",  # KODEX 레버리지
        "233740",  # KODEX 코스닥150 레버리지
        "114800",  # KODEX 인버스
        "261250",  # KODEX 200선물인버스2X
        "223760",  # KODEX 코스닥150선물인버스
        "133690",  # TIGER 미국나스닥100
        "140710",  # KODEX 미국S&P500TR
        "091160",  # KODEX 반도체
        "091170",  # KODEX 은행
        "379800",  # KODEX 미국채10년
    ]

    for code in sample_codes:
        info = ETF_CODE_MAP.get(code)
        if not info:
            continue
        base_price = rng.choice([5000, 10000, 15000, 20000, 25000, 30000])
        demo_etfs.append({
            "code":       code,
            "name":       info["name"],
            "market":     "ETF",
            "sector":     f"ETF({info['index']})",
            "asset_type": info["asset_type"],
            "price":      base_price,
            "market_cap": base_price * rng.randint(10_000, 500_000) * 1000,
            "daily_amount": rng.randint(50, 2000) * 1_000_000_000,
            "is_admin":  False, "is_halt": False, "warn_level": 0,
            "multiplier": info["multiplier"],
            "index":      info["index"],
        })
    return demo_etfs


# ── 자산군별 요약 ────────────────────────────────────────────

def summarize_portfolio_by_asset(
    positions: dict,       # {code: {asset_type, value, ...}}
    total_capital: float,
) -> dict:
    """
    포트폴리오의 자산군별 비중 요약.
    positions: {code: {"asset_type": ..., "value": 평가금액, ...}}
    Returns: {asset_type: {"value": ..., "ratio": ..., "count": ...}}
    """
    summary: dict[str, dict] = {}
    for code, pos in positions.items():
        atype = pos.get("asset_type", ASSET_STOCK_KOSPI)
        val   = pos.get("value", 0)
        if atype not in summary:
            summary[atype] = {"value": 0.0, "ratio": 0.0, "count": 0}
        summary[atype]["value"] += val
        summary[atype]["count"] += 1

    # 비중 계산
    if total_capital > 0:
        for atype in summary:
            summary[atype]["ratio"] = round(
                summary[atype]["value"] / total_capital, 4
            )

    return summary


def get_asset_type_label(asset_type: str) -> str:
    """자산군 한글 레이블"""
    LABELS = {
        ASSET_STOCK_KOSPI:  "코스피 주식",
        ASSET_STOCK_KOSDAQ: "코스닥 주식",
        ASSET_ETF_GENERAL:  "일반 ETF",
        ASSET_ETF_LEVERAGE: "레버리지 ETF",
        ASSET_ETF_INVERSE:  "인버스 ETF",
        ASSET_CASH:         "현금",
    }
    return LABELS.get(asset_type, asset_type)


def get_asset_type_color(asset_type: str) -> str:
    """자산군별 UI 색상 (CSS 변수명)"""
    COLORS = {
        ASSET_STOCK_KOSPI:  "var(--blue)",
        ASSET_STOCK_KOSDAQ: "var(--cyan)",
        ASSET_ETF_GENERAL:  "var(--green)",
        ASSET_ETF_LEVERAGE: "var(--orange)",
        ASSET_ETF_INVERSE:  "var(--red)",
        ASSET_CASH:         "var(--muted)",
    }
    return COLORS.get(asset_type, "var(--text)")
