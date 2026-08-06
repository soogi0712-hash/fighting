"""
주식시장 시간대 세션 관리 (국내 + 해외)
=========================================
국내 KRX 매매 가능 전체 시간:
  ① 장전 시간외        08:00 ~ 09:00  → 전일 종가 기준 단일가 (±10%)
  ② 정규장시작         09:00 ~ 09:30  (개장 직후 고변동)
  ③ 정규장             09:30 ~ 14:50
  ④ 정규장마감         14:50 ~ 15:20  ← 신규 매수 허용 마지막 구간
  ⑤ 정규장마감_매도전용 15:20 ~ 15:30  ← 신규 매수 금지, 매도만 허용
  ⑥ 장후 시간외        15:30 ~ 16:00  → 신규 매수 절대 금지, 매도만

★ 신규 매수 허용 시간: KST 09:00 ~ 15:20 (이후 어떤 이유로도 신규 매수 금지)
★ 15:20 이후: 보유 포지션 익절/손절/청산 매도만 허용

미국장 (KST 기준 — 서머타임 미적용 기준):
  ④ 미국 프리마켓  22:30 ~ 23:30 KST (EDT 기준 09:30~10:30)
  ⑤ 미국 정규장   23:30 ~ 06:00 KST 다음날 (EDT 09:30~16:00)
  ⑥ 미국 애프터마켓 06:00 ~ 07:00 KST (EDT 16:00~17:00)
  ※ KIS는 미국 프리/애프터 비지원 → 정규장 시간만 매매 처리

각 세션별:
  - 사용 가능한 KIS 주문 유형 코드 (ORD_DVSN)
  - 신호 점검 주기 (변동성 구간은 짧게)
  - allow_new_buy: 신규 매수 허용 여부 (15:20 이후 False)
  - sell_only:     매도 전용 여부 (15:20 이후 True)
"""
from datetime import datetime, time, timedelta
import pytz

KST = pytz.timezone("Asia/Seoul")
US_EASTERN = pytz.timezone("America/New_York")


# ══════════════════════════════════════════════════════════════
# 미국 거래세션 식별자 (거래일 귀속) — trade_count/PnL/손실한도 스코프
# ══════════════════════════════════════════════════════════════
#
# 정책:
#   - 타임존은 America/New_York 로 계산(DST 자동, 고정 오프셋 금지).
#   - 하나의 "미국 거래일 세션" 은 ET 04:00(프리마켓 개장) ~ 다음날 03:59:59.
#     ET 00:00~03:59 는 '전 거래일 세션' 에 귀속(오버나이트 연속성).
#   - session_id = 그 거래일의 ET 날짜(YYYY-MM-DD).
#   - phase: PRE(04:00~09:30) / REGULAR(09:30~16:00) / AFTER(16:00~20:00) /
#            OVERNIGHT(20:00~다음날 04:00).
#   - KST 자정은 미국 정규장 도중(ET 오전)이므로 session_id 가 바뀌지 않는다
#     → 동일 세션 도중 KST 날짜 변경으로 한도가 초기화되지 않는다.

def _to_et(dt=None) -> datetime:
    """naive(서버 KST) 또는 tz-aware datetime/ISO 문자열 → ET aware."""
    if dt is None:
        return datetime.now(US_EASTERN)
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            return datetime.now(US_EASTERN)
    if dt.tzinfo is None:
        dt = KST.localize(dt)   # 서버 로컬(KST) 로 간주
    return dt.astimezone(US_EASTERN)


def us_trading_session_id(dt=None) -> str:
    """미국 거래일(세션) 식별자 = ET 거래일 YYYY-MM-DD (04:00 ET 경계)."""
    et = _to_et(dt)
    if et.hour < 4:
        et = et - timedelta(days=1)
    return et.strftime("%Y-%m-%d")


def us_session_phase(dt=None) -> str:
    """PRE / REGULAR / AFTER / OVERNIGHT."""
    et = _to_et(dt)
    m = et.hour * 60 + et.minute
    if 4 * 60 <= m < 9 * 60 + 30:
        return "PRE"
    if 9 * 60 + 30 <= m < 16 * 60:
        return "REGULAR"
    if 16 * 60 <= m < 20 * 60:
        return "AFTER"
    return "OVERNIGHT"

# ── 세션 이름 상수 ──────────────────────────────────────────
SESSION_PRE        = "장전시간외"       # 08:00~09:00
SESSION_OPEN       = "정규장시작"       # 09:00~09:30  (개장 직후 고변동)
SESSION_MID        = "정규장"           # 09:30~14:50
SESSION_CLOSE      = "정규장마감"       # 14:50~15:20  (신규 매수 허용 마지막)
SESSION_CLOSE_SELL = "정규장마감_매도전용"  # 15:20~15:30  (매도 전용)
SESSION_POST       = "장후시간외"       # 15:30~16:00  (매도 전용)
SESSION_OFF        = "휴장"

# ── KIS 주문 유형 코드 ──────────────────────────────────────
# 00: 지정가 / 01: 시장가 / 05: 장전시간외 / 06: 장후시간외
ORD_LIMIT       = "00"
ORD_MARKET      = "01"
ORD_PRE_MARKET  = "05"   # 장전 시간외 단일가
ORD_POST_MARKET = "06"   # 장후 시간외 단일가

# ── 신규 매수 차단 기준 시각 ─────────────────────────────────
BUY_CUTOFF_TIME = time(15, 20)   # 15:20 이후 신규 매수 절대 금지


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
    elif time(14, 50) <= t < time(15, 20):return SESSION_CLOSE        # ★ 15:20까지만
    elif time(15, 20) <= t < time(15, 30):return SESSION_CLOSE_SELL   # ★ 15:20~15:30 매도전용
    elif time(15, 30) <= t < time(16,  0):return SESSION_POST
    else:                                  return SESSION_OFF


def allow_new_buy_now() -> bool:
    """현재 시각에 신규 매수가 허용되는지 즉시 판단 (이중 안전망)"""
    now = now_kst()
    if now.weekday() >= 5:
        return False
    return now.time() < BUY_CUTOFF_TIME


def session_info() -> dict:
    """현재 세션의 상세 정보 딕셔너리

    ★ 추가 필드:
      allow_new_buy: True  → 신규 매수 허용 (KST 09:00~15:20)
                    False → 신규 매수 금지 (15:20 이후 / 장전 / 휴장)
      sell_only:    True  → 매도 전용 세션 (15:20 이후)
      buy_block_reason: 신규 매수 차단 사유 (allow_new_buy=False일 때)
    """
    s = get_session()
    _now_t = now_kst().time()

    META = {
        SESSION_PRE: {
            "tradeable":    True,
            "allow_new_buy": False,   # ★ 장전시간외 신규 매수 금지
            "sell_only":    False,
            "buy_block_reason": "장전시간외 — 신규 매수 미허용 (09:00 이후 가능)",
            "order_dvsn":   ORD_PRE_MARKET,
            "order_label":  "장전시간외(단일가)",
            "check_sec":    30,
            "score_cutoff": 0.0,
            "description":  "전일 종가 기준 ±10% 단일가 — 매도만 허용",
            "icon":         "🌅",
        },
        SESSION_OPEN: {
            "tradeable":    True,
            "allow_new_buy": True,    # ✅ 09:00~09:30 신규 매수 허용
            "sell_only":    False,
            "buy_block_reason": "",
            "order_dvsn":   ORD_LIMIT,
            "order_label":  "정규장(개장)",
            "check_sec":    15,
            "score_cutoff": 0.0,
            "description":  "개장 직후 변동성 구간 — 공격 진입",
            "icon":         "🔔",
        },
        SESSION_MID: {
            "tradeable":    True,
            "allow_new_buy": True,    # ✅ 09:30~14:50 신규 매수 허용
            "sell_only":    False,
            "buy_block_reason": "",
            "order_dvsn":   ORD_LIMIT,
            "order_label":  "정규장",
            "check_sec":    30,
            "score_cutoff": 0.0,
            "description":  "정규장 메인 구간 — 전 종목 스캔",
            "icon":         "📈",
        },
        SESSION_CLOSE: {
            "tradeable":    True,
            "allow_new_buy": True,    # ✅ 14:50~15:20 신규 매수 허용 (마지막 구간)
            "sell_only":    False,
            "buy_block_reason": "",
            "order_dvsn":   ORD_LIMIT,
            "order_label":  "정규장마감(매수허용)",
            "check_sec":    15,
            "score_cutoff": 0.0,
            "description":  "마감 전 — 신규 매수 허용 마지막 구간 (15:20까지)",
            "icon":         "⏰",
        },
        SESSION_CLOSE_SELL: {
            "tradeable":    True,
            "allow_new_buy": False,   # ★ 15:20~15:30 신규 매수 금지
            "sell_only":    True,     # ★ 매도 전용
            "buy_block_reason": "15:20 이후 신규 매수 차단 — 매도 전용 구간",
            "order_dvsn":   ORD_LIMIT,
            "order_label":  "정규장마감(매도전용)",
            "check_sec":    15,
            "score_cutoff": 1.0,      # 매수 점수 기준 최대치 → 사실상 매수 불가
            "description":  "15:20~15:30 — 신규 매수 금지, 보유 포지션 매도만 허용",
            "icon":         "🚫",
        },
        SESSION_POST: {
            "tradeable":    True,
            "allow_new_buy": False,   # ★ 장후시간외 신규 매수 절대 금지
            "sell_only":    True,     # ★ 매도 전용
            "buy_block_reason": "장후시간외 — 신규 매수 절대 금지 (다음날 손절 방지)",
            "order_dvsn":   ORD_POST_MARKET,
            "order_label":  "장후시간외(매도전용)",
            "check_sec":    30,
            "score_cutoff": 1.0,      # 매수 점수 기준 최대치 → 사실상 매수 불가
            "description":  "장후시간외 — 기존 보유 포지션 매도만 허용",
            "icon":         "🌇",
        },
        SESSION_OFF: {
            "tradeable":    False,
            "allow_new_buy": False,
            "sell_only":    False,
            "buy_block_reason": "휴장",
            "order_dvsn":   None,
            "order_label":  "휴장",
            "check_sec":    300,
            "score_cutoff": 1.0,
            "description":  "매매 불가 시간",
            "icon":         "😴",
        },
    }
    info = META.get(s, META[SESSION_OFF]).copy()
    info["session"]  = s
    info["time_kst"] = now_kst().strftime("%H:%M:%S")
    info["weekday"]  = now_kst().strftime("%A")

    # ── 이중 안전망: 시각 직접 체크로 allow_new_buy 최종 확정 ──
    # META의 값과 무관하게 15:20 이후이면 무조건 False
    if _now_t >= BUY_CUTOFF_TIME:
        info["allow_new_buy"]    = False
        info["sell_only"]        = True
        if not info["buy_block_reason"]:
            info["buy_block_reason"] = f"15:20 이후 신규 매수 차단 ({_now_t.strftime('%H:%M:%S')} KST)"

    return info


def is_tradeable() -> bool:
    return session_info()["tradeable"]


def get_order_dvsn(force_market: bool = False) -> str:
    """현재 세션에 맞는 주문 유형 코드 반환"""
    if force_market:
        return ORD_MARKET
    return session_info().get("order_dvsn") or ORD_LIMIT


# ══════════════════════════════════════════════════════════════
# 미국장 세션 함수
# ══════════════════════════════════════════════════════════════

def get_us_session() -> str:
    """
    현재 KST 기준 미국장 세션 반환.
    미국 동부시간(ET) 기준으로 판단 — 서머타임 자동 적용.

    Returns:
        "미국프리마켓"  | "미국정규장" | "미국애프터" | "미국휴장"
    """
    now_et = datetime.now(US_EASTERN)
    # 미국 주말 휴장
    if now_et.weekday() >= 5:
        return "미국휴장"
    t = now_et.time()
    if   time(4,  0) <= t < time(9, 30):  return "미국프리마켓"
    elif time(9, 30) <= t < time(16,  0): return "미국정규장"
    elif time(16, 0) <= t < time(20,  0): return "미국애프터"
    else:                                  return "미국휴장"


def us_session_info() -> dict:
    """미국장 세션 상세 정보"""
    s = get_us_session()
    now_et  = datetime.now(US_EASTERN)
    now_kst = datetime.now(KST)
    META = {
        "미국프리마켓": {
            "tradeable":   False,   # KIS는 프리마켓 주문 미지원
            "check_sec":   600,
            "description": "미국 프리마켓 (KIS 주문 불가)",
            "icon":        "🌙",
        },
        "미국정규장": {
            "tradeable":   True,
            "check_sec":   60,     # ★ 초공격: 60초 (120→60)
            "description": "미국 정규장 (09:30~16:00 ET)",
            "icon":        "🇺🇸",
        },
        "미국애프터": {
            "tradeable":   False,
            "check_sec":   600,
            "description": "미국 애프터마켓 (KIS 주문 불가)",
            "icon":        "🌆",
        },
        "미국휴장": {
            "tradeable":   False,
            "check_sec":   1800,
            "description": "미국 휴장",
            "icon":        "😴",
        },
    }
    info = META.get(s, META["미국휴장"]).copy()
    info["session"]     = s
    info["time_et"]     = now_et.strftime("%H:%M:%S ET")
    info["time_kst"]    = now_kst.strftime("%H:%M:%S KST")
    info["weekday"]     = now_et.strftime("%A")
    info["order_dvsn"]  = "00"   # 지정가 (해외)
    return info


def is_us_tradeable() -> bool:
    """미국 정규장 중인지 여부"""
    return get_us_session() == "미국정규장"


# ══════════════════════════════════════════════════════════════
# 미국장 시간대별 운영 단계 (KST 기준)
# ══════════════════════════════════════════════════════════════
# PRIME      : 22:30~00:00 KST (ET 09:30~11:00) — 최우선 집중 매매
# NEUTRAL    : 00:00~01:00 KST (ET 11:00~12:00) — 중립, 신규 진입 조건 강화
# CONSERVATIVE: 01:00 이후  KST (ET 12:00~)     — 보수, 신규 진입 최소화

US_PHASE_PRIME        = "PRIME"         # 22:30~00:00 KST
US_PHASE_NEUTRAL      = "NEUTRAL"       # 00:00~01:00 KST
US_PHASE_CONSERVATIVE = "CONSERVATIVE"  # 01:00 이후 KST


def get_us_trading_phase() -> str:
    """
    현재 미국장 운영 단계 반환 (KST 시각 기준).
    미국 정규장(ET 09:30~16:00) 중일 때만 의미 있음.

    Returns:
        "PRIME"        : 22:30~00:00 KST — 개장 초반 집중 매매
        "NEUTRAL"      : 00:00~01:00 KST — 중립 운영
        "CONSERVATIVE" : 01:00 이후  KST — 보수 운영
    """
    now_kst_dt = datetime.now(KST)
    t = now_kst_dt.time()
    # 22:30 ~ 자정(00:00) → PRIME
    if time(22, 30) <= t <= time(23, 59, 59):
        return US_PHASE_PRIME
    # 00:00 ~ 01:00 → NEUTRAL
    if time(0, 0) <= t < time(1, 0):
        return US_PHASE_NEUTRAL
    # 01:00 이후 (미국장 운영 중인 경우) → CONSERVATIVE
    if time(1, 0) <= t < time(6, 0):
        return US_PHASE_CONSERVATIVE
    # 미국장 시간 외
    return US_PHASE_CONSERVATIVE


def us_phase_info() -> dict:
    """
    현재 미국장 운영 단계 상세 정보.

    반환 필드:
      phase          : PRIME / NEUTRAL / CONSERVATIVE
      allow_new_buy  : 신규 매수 허용 여부
      aggressiveness : 적극성 수준 (3=최고, 2=중간, 1=최저)
      description    : 설명
      buy_score_min  : 최소 BUY_SCORE 가중치 (숫자가 클수록 조건 강화)
      icon           : 표시 아이콘
    """
    phase = get_us_trading_phase()
    now_kst_dt = datetime.now(KST)

    META = {
        US_PHASE_PRIME: {
            "allow_new_buy":  True,
            "aggressiveness": 3,
            "description":    "개장 초반 최우선 집중 매매 (22:30~00:00 KST)",
            "buy_score_min":  0,    # 기본 점수 기준 그대로 사용
            "icon":           "🔥",
        },
        US_PHASE_NEUTRAL: {
            "allow_new_buy":  True,
            "aggressiveness": 2,
            "description":    "중립 운영 — 신규 진입 조건 강화 (00:00~01:00 KST)",
            "buy_score_min":  1,    # BUY_SCORE +1점 이상 추가로 요구
            "icon":           "⚖️",
        },
        US_PHASE_CONSERVATIVE: {
            "allow_new_buy":  False,  # 신규 진입 최소화 → 기본 False
            "aggressiveness": 1,
            "description":    "보수 운영 — 신규 진입 최소화, 익절/손절 관리 중심 (01:00 이후 KST)",
            "buy_score_min":  2,    # BUY_SCORE +2점 이상 추가로 요구 (사실상 차단)
            "icon":           "🛡️",
        },
    }
    info = META.get(phase, META[US_PHASE_CONSERVATIVE]).copy()
    info["phase"]    = phase
    info["time_kst"] = now_kst_dt.strftime("%H:%M:%S KST")
    return info


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
