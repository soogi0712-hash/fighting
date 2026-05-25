"""
한국 주식시장 시간대 세션 관리
================================
KRX 매매 가능 전체 시간:

  ① 장전 시간외   08:00 ~ 09:00  → 전일 종가 기준 단일가 (±10%)
  ② 정규장        09:00 ~ 15:30  → 연속 매매 (주 전략 구간)
  ③ 장후 시간외   15:30 ~ 16:00  → 당일 종가 기준 단일가 (±10%)

각 세션별:
  - 사용 가능한 KIS 주문 유형 코드 (ORD_DVSN)
  - 신호 점검 주기 (변동성 구간은 짧게)
  - 전략 강도 조정 (장전/장후는 보수적으로)
  - 휴장일/주말 자동 인식
"""
from datetime import datetime, time
import pytz

KST = pytz.timezone("Asia/Seoul")

# ── 세션 이름 상수 ──────────────────────────────────────────
SESSION_PRE   = "장전시간외"    # 08:00~09:00
SESSION_OPEN  = "정규장시작"    # 09:00~09:30  (개장 직후 고변동)
SESSION_MID   = "정규장"        # 09:30~14:50
SESSION_CLOSE = "정규장마감"    # 14:50~15:30  (마감 고변동)
SESSION_POST  = "장후시간외"    # 15:30~16:00
SESSION_OFF   = "휴장"

# ── KIS 주문 유형 코드 ──────────────────────────────────────
# 00: 지정가 / 01: 시장가 / 05: 장전시간외 / 06: 장후시간외
ORD_LIMIT       = "00"
ORD_MARKET      = "01"
ORD_PRE_MARKET  = "05"   # 장전 시간외 단일가
ORD_POST_MARKET = "06"   # 장후 시간외 단일가


def now_kst() -> datetime:
    return datetime.now(KST)


def get_session() -> str:
    """현재 KST 기준 매매 세션 반환"""
    now = now_kst()
    # 주말 휴장
    if now.weekday() >= 5:
        return SESSION_OFF
    t = now.time()
    if   time(8,  0) <= t < time(9,  0):  return SESSION_PRE
    elif time(9,  0) <= t < time(9, 30):  return SESSION_OPEN
    elif time(9, 30) <= t < time(14, 50): return SESSION_MID
    elif time(14, 50) <= t < time(15, 30):return SESSION_CLOSE
    elif time(15, 30) <= t < time(16,  0):return SESSION_POST
    else:                                  return SESSION_OFF


def session_info() -> dict:
    """현재 세션의 상세 정보 딕셔너리"""
    s = get_session()
    META = {
        SESSION_PRE: {
            "tradeable":    True,
            "order_dvsn":   ORD_PRE_MARKET,
            "order_label":  "장전시간외(단일가)",
            "check_sec":    300,   # 5분
            "score_cutoff": 0.65,  # 진입 기준 점수 높임 (보수적)
            "description":  "전일 종가 기준 ±10% 단일가 매매",
            "icon":         "🌅",
        },
        SESSION_OPEN: {
            "tradeable":    True,
            "order_dvsn":   ORD_LIMIT,
            "order_label":  "정규장(개장)",
            "check_sec":    60,    # 1분 (변동성 크므로 빠르게)
            "score_cutoff": 0.65,
            "description":  "개장 직후 변동성 구간 — 신중 진입",
            "icon":         "🔔",
        },
        SESSION_MID: {
            "tradeable":    True,
            "order_dvsn":   ORD_LIMIT,
            "order_label":  "정규장",
            "check_sec":    300,   # 5분
            "score_cutoff": 0.55,  # 기본 기준
            "description":  "정규장 메인 구간 — 모든 전략 활성",
            "icon":         "📈",
        },
        SESSION_CLOSE: {
            "tradeable":    True,
            "order_dvsn":   ORD_LIMIT,
            "order_label":  "정규장(마감)",
            "check_sec":    60,    # 1분
            "score_cutoff": 0.70,  # 마감 전: 신규 진입 까다롭게
            "description":  "마감 전 — 기존 포지션 정리 우선",
            "icon":         "⏰",
        },
        SESSION_POST: {
            "tradeable":    True,
            "order_dvsn":   ORD_POST_MARKET,
            "order_label":  "장후시간외(단일가)",
            "check_sec":    300,   # 5분
            "score_cutoff": 0.65,
            "description":  "당일 종가 기준 ±10% 단일가 매매",
            "icon":         "🌇",
        },
        SESSION_OFF: {
            "tradeable":    False,
            "order_dvsn":   None,
            "order_label":  "휴장",
            "check_sec":    600,   # 10분 (재확인용)
            "score_cutoff": 1.0,
            "description":  "매매 불가 시간",
            "icon":         "😴",
        },
    }
    info = META.get(s, META[SESSION_OFF]).copy()
    info["session"]  = s
    info["time_kst"] = now_kst().strftime("%H:%M:%S")
    info["weekday"]  = now_kst().strftime("%A")
    return info


def is_tradeable() -> bool:
    return session_info()["tradeable"]


def get_order_dvsn(force_market: bool = False) -> str:
    """현재 세션에 맞는 주문 유형 코드 반환"""
    if force_market:
        return ORD_MARKET
    return session_info().get("order_dvsn") or ORD_LIMIT


def round_price(price: float) -> int:
    """KRX 호가 단위 반올림"""
    p = int(price)
    if   p <   1_000: return max(1, p)
    elif p <   5_000: return round(p / 5) * 5
    elif p <  10_000: return round(p / 10) * 10
    elif p <  50_000: return round(p / 50) * 50
    elif p < 100_000: return round(p / 100) * 100
    elif p < 500_000: return round(p / 500) * 500
    else:             return round(p / 1000) * 1000
