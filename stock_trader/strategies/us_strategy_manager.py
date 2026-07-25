"""
해외주식 전략 매니저 — 눌림목 재돌파 진입 전략 v3
==========================================================

★ 핵심 철학: "좋은 자리만 골라서, 확신 있을 때만 들어간다"
  "많이 사는 게 목표가 아니라, 이기는 매매만 한다"

  ── 미국장 시간대별 운영 단계 ──
  PRIME      (22:30~00:00 KST): 최우선 집중 매매 — 적극적 스크리닝/신규진입/회전매매
  NEUTRAL    (00:00~01:00 KST): 중립 운영 — 신규 진입 조건 강화, 기존 포지션 관리 우선
  CONSERVATIVE (01:00 이후 KST): 보수 운영 — 신규 진입 최소화, 익절/손절 관리 중심

  ── 개장 직후 초기 모멘텀 스크리닝 (PRIME 단계 내) ──
  22:31 / 22:33 / 22:35 / 22:40 → 집중 스캔 타임

  ── 진입 조건 (5가지 모두 충족 시에만 매수) ──
  1. VWAP 위에서만 진입  (가격이 VWAP 이상 = 오늘 평균 매수자가 수익 중)
  2. EMA 정배열 확인     (EMA9 > EMA21 > EMA50 = 단기·중기 모두 상승 추세)
  3. 눌림 후 재상승      (전봉 대비 고점 갱신 = 힘이 살아있음)
  4. 추격매수 금지       (직전 N분 급등폭이 X% 초과 시 이미 늦음)
  5. 거래량 확인         (90일 평균 대비 1.5배 이상 = 세력 유입)

  ── 필터 ──
  - 스프레드 과도 시 진입 금지 (bid-ask 0.5% 이상)
  - 꼭대기 방지: 오늘 고가 대비 -4% 이상 하락 시 금지
  - 마감 30분 전 신규 진입 금지

  ── 청산 ──
  - 손절: -3% (빠른 손실 컷)
  - 트레일링 스탑: 구간별 차등
  - 거래량+등락률 동시 급락 → 모멘텀 소멸 즉시 청산
  - 구간별 매도 지표 2개 이상 → 단타 청산

  ── 수익 목표 자동 정지 ──
  ★ 미국장 +300,000원 달성 → US_PROFIT_LOCK (신규 매수 차단)
  ★ 미국장 세션(ET 날짜) 기준 관리 — 날짜 변경 무관하게 세션 내 누적

  ── 로그 원칙 ──
  - 매수/매도 시 반드시 "왜" 기록
  - 진입 시점 VWAP/EMA/거래량/추격여부 전부 표시
  - 미충족 조건도 표시
  - [US OPEN SCAN] 개장 정보 로그 출력

★ 포지션 파일: data/us_positions.json
"""

import os
import json
import numpy as np
from datetime import datetime
from utils.logger             import get_logger
from utils.market_session     import (
    us_session_info, is_us_tradeable,
    get_us_trading_phase, us_phase_info,
    US_PHASE_PRIME, US_PHASE_NEUTRAL, US_PHASE_CONSERVATIVE,
)
from strategies.daily_pnl_guard import DailyPnLGuard
from strategies.reentry_guard   import ReentryGuard, _is_stoploss_reason

# ── Trading Journal (선택적 로드 — 실패 시 매매 루프 중단 없음) ──
try:
    import journal.trading_journal as _us_jnl
    _US_JOURNAL_ENABLED = True
except Exception as _uje:
    _US_JOURNAL_ENABLED = False
    import logging as _uj_logging
    _uj_logging.getLogger("USStrategy").warning(
        f"[US Journal] 로드 실패 — 저널 비활성화(매매 영향 없음): {_uje}"
    )

# ── Phoenix OrderLifecycle (Phase 4 US Pipeline) ─────────────────────
try:
    from phoenix.lifecycle import OrderLifecycleManager, make_order_lifecycle_id
    from phoenix.execution_driven import ExecutionDrivenPositionUpdater
    _US_LIFECYCLE_ENABLED = True
except Exception as _us_lce:
    _US_LIFECYCLE_ENABLED = False
    import logging as _us_lc_logging
    _us_lc_logging.getLogger("USStrategy").warning(
        f"[US Lifecycle] import 실패 — lifecycle 비활성화: {_us_lce}"
    )

# ── FillObserver + PendingOrderRegistry (Phase 4 US Pipeline) ────────
try:
    from journal.fill_observer import (
        FillObserver as _USFillObserver,
        PendingOrderRegistry as _USPendingOrderRegistry,
        PendingStatus as _USPendingStatus,
    )
    _US_FILL_OBSERVER_ENABLED = True
except Exception as _us_foe:
    _US_FILL_OBSERVER_ENABLED = False
    import logging as _us_fo_logging
    _us_fo_logging.getLogger("USStrategy").warning(
        f"[US FillObserver] import 실패 — fill observer 비활성화: {_us_foe}"
    )

logger = get_logger("USStrategy")

US_POSITIONS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "us_positions.json"
)

# ── 기본 관심종목 풀 유니버스 ─────────────────────────────────
DEFAULT_US_WATCHLIST = {
    # ══ [1] 레버리지 ETF ══
    "TQQQ":  {"name": "나스닥100 3배레버리지",      "excd": "NASD"},
    "SOXL":  {"name": "반도체 3배레버리지",          "excd": "NASD"},
    "FNGU":  {"name": "빅테크 3배레버리지",          "excd": "NYSE"},
    # "LABU":  KIS 거래불가 — 자동 블랙리스트
    "TNA":   {"name": "소형주 3배레버리지",          "excd": "NYSE"},
    "TECL":  {"name": "기술섹터 3배레버리지",        "excd": "NYSE"},
    "CURE":  {"name": "헬스케어 3배레버리지",        "excd": "NYSE"},
    "DPST":  {"name": "은행섹터 3배레버리지",        "excd": "NYSE"},
    # "NAIL":  KIS 거래불가 — 자동 블랙리스트
    "HIBL":  {"name": "S&P고베타 3배레버리지",       "excd": "NYSE"},

    # ══ [2] 암호화폐/블록체인 ══
    "MSTR":  {"name": "마이크로스트래티지(BTC보유)", "excd": "NASD"},
    "COIN":  {"name": "코인베이스(암호화폐거래소)",  "excd": "NASD"},
    "RIOT":  {"name": "라이엇플랫폼스(BTC채굴)",     "excd": "NASD"},
    "MARA":  {"name": "마라홀딩스(BTC채굴)",         "excd": "NASD"},
    "HUT":   {"name": "허트8(BTC채굴)",              "excd": "NASD"},
    "CLSK":  {"name": "클린스파크(그린채굴)",         "excd": "NASD"},
    "CIFR":  {"name": "사이퍼마이닝(채굴)",           "excd": "NASD"},
    "WULF":  {"name": "테라울프(친환경채굴)",         "excd": "NASD"},
    "IREN":  {"name": "아이렌(AI+채굴)",              "excd": "NASD"},
    "BTBT":  {"name": "비트팜스(채굴)",               "excd": "NASD"},

    # ══ [3] AI/양자컴퓨팅 ══
    "SMCI":  {"name": "슈퍼마이크로(AI서버)",        "excd": "NASD"},
    "IONQ":  {"name": "아이온큐(양자컴퓨팅)",        "excd": "NYSE"},
    "RGTI":  {"name": "리게티컴퓨팅(양자)",          "excd": "NASD"},
    "QUBT":  {"name": "퀀텀컴퓨팅Inc(양자)",         "excd": "NASD"},
    "QBTS":  {"name": "D-Wave퀀텀(양자)",            "excd": "NYSE"},
    "SOUN":  {"name": "사운드하운드AI(음성AI)",       "excd": "NASD"},
    "BBAI":  {"name": "빅베어AI(기업AI)",            "excd": "NYSE"},
    "UPST":  {"name": "업스타트(AI대출)",             "excd": "NASD"},
    "PAYO":  {"name": "페이온어(핀테크AI)",           "excd": "NASD"},

    # ══ [4] 바이오/유전자 ══
    "RXRX":  {"name": "리커전제약(AI신약)",           "excd": "NASD"},
    "CRSP":  {"name": "크리스퍼테라퓨틱스(유전자)",  "excd": "NASD"},
    "BEAM":  {"name": "빔테라퓨틱스(유전자편집)",    "excd": "NASD"},
    "NTLA":  {"name": "인텔리아테라퓨틱스(유전자)",  "excd": "NASD"},
    "EDIT":  {"name": "에디타스메디신(유전자)",       "excd": "NASD"},
    "SAVA":  {"name": "카사바사이언스(알츠하이머)",   "excd": "NASD"},
    "AGEN":  {"name": "에이전우스(면역항암)",         "excd": "NASD"},
    "IOVA":  {"name": "아이오반스(세포치료)",         "excd": "NASD"},
    "NKTR":  {"name": "넥타테라퓨틱스(약물전달)",    "excd": "NASD"},
    "CRIS":  {"name": "큐리스(항암제)",               "excd": "NASD"},
    "ACAD":  {"name": "아카디아제약(신경계)",         "excd": "NASD"},
    "INSM":  {"name": "인스메드(폐질환)",             "excd": "NASD"},

    # ══ [5] EV/청정에너지 ══
    "RIVN":  {"name": "리비안(EV트럭)",               "excd": "NASD"},
    "LCID":  {"name": "루시드모터스(EV럭셔리)",       "excd": "NASD"},
    "CHPT":  {"name": "차지포인트(EV충전)",            "excd": "NYSE"},
    "BLNK":  {"name": "블링크차징(EV충전)",            "excd": "NASD"},
    "PLUG":  {"name": "플러그파워(수소연료전지)",      "excd": "NASD"},
    "FCEL":  {"name": "퓨얼셀에너지(연료전지)",       "excd": "NASD"},
    "BE":    {"name": "블룸에너지(고체연료전지)",      "excd": "NYSE"},
    "NKLA":  {"name": "니콜라(수소트럭)",              "excd": "NASD"},
    "EVGO":  {"name": "이브고(EV급속충전)",            "excd": "NASD"},
    "WKHS":  {"name": "워크호스(EV상용차)",           "excd": "NASD"},

    # ══ [6] 우주/방위/에어택시 ══
    "RKLB":  {"name": "로켓랩(우주발사체)",           "excd": "NASD"},
    "LUNR":  {"name": "인투이티브머신스(달탐사)",     "excd": "NASD"},
    "ASTS":  {"name": "AST스페이스모바일(우주인터넷)","excd": "NASD"},
    "SPIR":  {"name": "스파이어글로벌(우주데이터)",   "excd": "NYSE"},
    "RDDT":  {"name": "레딧(SNS플랫폼)",               "excd": "NYSE"},
    "JOBY":  {"name": "조비에비에이션(에어택시)",     "excd": "NYSE"},
    "ACHR":  {"name": "아처에비에이션(에어택시)",     "excd": "NYSE"},

    # ══ [6B] 방산/사이버보안 (신규 추가) ══
    "KTOS":  {"name": "크라토스방산(무인항공/방산AI)", "excd": "NASD"},
    "BWXT":  {"name": "BWX테크놀로지스(원자로방산)",  "excd": "NYSE"},
    "CACI":  {"name": "CACI인터내셔널(방산IT)",       "excd": "NYSE"},
    "PLTR":  {"name": "팔란티어(AI방산데이터)",       "excd": "NYSE"},
    "LDOS":  {"name": "레이도스홀딩스(방산IT서비스)", "excd": "NYSE"},
    "S":     {"name": "센티넬원(사이버보안AI)",       "excd": "NYSE"},
    "CRWD":  {"name": "크라우드스트라이크(사이버보안)","excd": "NASD"},
    "PANW":  {"name": "팔로알토네트웍스(사이버보안)", "excd": "NASD"},

    # ══ [7] 핀테크/소형금융 ══
    "SOFI":  {"name": "소파이(핀테크뱅크)",           "excd": "NASD"},
    "AFRM":  {"name": "어펌(BNPL)",                   "excd": "NASD"},
    "OPEN":  {"name": "오픈도어(프롭테크)",            "excd": "NASD"},
    "HOOD":  {"name": "로빈후드(MZ증권)",              "excd": "NASD"},
    "DAVE":  {"name": "데이브(소액대출앱)",            "excd": "NASD"},
    "NU":    {"name": "누홀딩스(중남미핀테크)",        "excd": "NYSE"},
    "CUBI":  {"name": "큐비타뚨크(제켴만한퐌테크)",    "excd": "NYSE"},

    # ══ [7B] 원자력/청정에너지 인프라 (신규 추가) ══
    "NNE":   {"name": "나노뉴클리어에너지(SMR)",      "excd": "NASD"},
    "SMR":   {"name": "뉴스케일파워(소형모듈원자로)", "excd": "NYSE"},
    "LEU":   {"name": "센트러스에너지(우라늄농축)",   "excd": "NYSE"},
    "CCJ":   {"name": "카메코(우라늄채굴)",            "excd": "NYSE"},
    "OKLO":  {"name": "오클로(마이크로원자로)",        "excd": "NYSE"},
    "ETR":   {"name": "엔터지(저렵 전력유틸리티)",   "excd": "NYSE"},
    "VST":   {"name": "비스트라에너지(전력/원전)",    "excd": "NYSE"},
    "CEG":   {"name": "콘스텔레이션에너지(원전)",     "excd": "NASD"},

    # ══ [8] 반도체 중형 ══
    "AMD":   {"name": "AMD(중형반도체)",              "excd": "NASD"},
    "MU":    {"name": "마이크론(메모리반도체)",        "excd": "NASD"},
    "WOLF":  {"name": "울프스피드(전력반도체)",       "excd": "NYSE"},
    "NVTS":  {"name": "나비타스세미(GaN반도체)",      "excd": "NASD"},
    "AEHR":  {"name": "에어테스트(반도체테스트)",     "excd": "NASD"},

    # ══ [9] 소형 성장주/테마주 ══
    "CLOV":  {"name": "클로버헬스(헬스테크)",         "excd": "NASD"},
    "SPCE":  {"name": "버진갤럭틱(우주관광)",         "excd": "NYSE"},
    "CAVA":  {"name": "카바(지중해식당체인)",          "excd": "NYSE"},
    "XPEV":  {"name": "샤오펑(중국EV)",               "excd": "NYSE"},
    "NIO":   {"name": "니오(중국EV)",                 "excd": "NYSE"},
    "LI":    {"name": "리오토(중국EV)",               "excd": "NASD"},
    "FUTU":  {"name": "푸투홀딩스(중국핀테크)",       "excd": "NASD"},

    # ══ [10] 인프라/유틸리티/배당성장 (신규 추가) ══
    "NEE":   {"name": "넥스트에라에너지(신재생유틸리티)","excd": "NYSE"},
    "AES":   {"name": "AES코퍼레이션(글로벌전력)",   "excd": "NYSE"},
    "RUN":   {"name": "선런(주거용태양광)",           "excd": "NASD"},
    "ARRY":  {"name": "어레이테크놀로지스(태양광)",   "excd": "NASD"},
    "NOVA":  {"name": "선노바에너지(태양광금융)",     "excd": "NYSE"},

    # ══ [11] 기타 ══
    "BROS":  {"name": "더치브로스(커피체인)",          "excd": "NYSE"},
    "BLZE":  {"name": "백블레이즈(클라우드스토리지)", "excd": "NASD"},
    "CPRX":  {"name": "카탈리스트바이오(희귀질환)",   "excd": "NASD"},
    "APP":   {"name": "앱러빈(모바일AI광고)",         "excd": "NASD"},
    "CELH":  {"name": "셀시어스홀딩스(에너지음료)",   "excd": "NASD"},
}

# ════════════════════════════════════════════════════════════
# ── 핵심 파라미터
# ════════════════════════════════════════════════════════════
# ★ 1회 투자금: 실제 USD 주문가능금액(frcr_ord_psbl_amt1) 대비 비율로 동적 결정
# INVEST_PER_TRADE_USD = 상한 cap, INVEST_RATIO_OF_AVAIL = 가용금액 대비 비율
INVEST_PER_TRADE_USD  = 400.0   # 1회 최대 투자 상한 (USD) — $499 가능금액 기준 안전선
INVEST_RATIO_OF_AVAIL = 0.80    # 가용 USD의 80%까지 사용 (잔액 여유 확보)

# ── 진입 필터 (★ 초공격 단타 — 진입 장벽 최소화) ──
ENTRY_MIN_VOL_RATIO   =  1.0   # 90일 평균 대비 최소 거래량 (사실상 모두 허용)
ENTRY_VWAP_MARGIN_PCT =  0.0   # VWAP 대비 최소 이격 (0 = VWAP 이상이면 OK)
ENTRY_CHASE_BAR_PCT   =  8.0   # 직전 봉 단독 상승폭 추격 금지 (8%)
ENTRY_MIN_INTRADAY    =  0.2   # 전일 종가 대비 최소 등락률 (0.3%→0.2%, 초입 진입 허용)
PEAK_DROP_FILTER      =  8.0   # 오늘 고가 대비 -8% 이상 하락 시만 금지
SPREAD_MAX_PCT        =  1.5   # bid-ask 스프레드 1.5% 초과 시 금지

# ── 추격매수 패널티 임계 ─────────────────────────────────────────────────────
# ① 5분봉 기준 당일 이미 +N% 상승 → 추격매수 감점 (상승 확인 후 추격 억제)
CHASE_SURGE_PENALTY_PCT   =  3.0  # 이미 +3% 이상 상승 시 추격패널티 -1점
CHASE_SURGE_BLOCK_PCT     =  5.5  # 이미 +5.5% 이상 상승 시 진입 차단 (꼭대기)
# ② 당일 저점 대비 현재가 상승률 → 너무 많이 올라간 종목 감점
FROM_LOW_PENALTY_PCT      =  4.0  # 저점 대비 +4% 이상 → -1점
FROM_LOW_BLOCK_PCT        =  8.0  # 저점 대비 +8% 이상 → -2점 (사실상 진입차단급)
# ③ 거래량 급증 초기 구간 (1~3개 캔들) → 가산점
VOL_EARLY_BOOST_MIN_RATIO = 2.5   # 평균 대비 거래량 2.5배 이상 = 초기 급증
VOL_EARLY_CANDLE_MAX      = 3     # 장 시작 후 최대 N개 캔들 이내 = "초기"

# ── 진입 강도 2단계 (EARLY / FULL) ──────────────────────────────────────────
# BUY_SCORE 최대 10점 (기존 8 + 거래량초기+1 + 추격/저점 감점 반영)
# EARLY: 거래량증가+OBV상승+VWAP위 필수 3조건 + 점수 0.38×10 = 3.8 → 4점 이상
# FULL : 기존 모든 필수 조건 + 점수 0.55×10 = 5.5점 이상
# ★ 기존 대비 1~2점 빠르게 진입 (EARLY 0.50×8=4.0 → 3점 기준으로 사실상 당김)
BUY_SCORE_EARLY       =  0.38  # EARLY 진입 점수 임계 (0.50→0.38, 3.8≈4점 — 더 빠른 진입)
BUY_SCORE_FULL        =  0.55  # FULL  진입 점수 임계 (0.65→0.55, 5.5점 — 더 빠른 진입)
BUY_SCORE_MAX         =  10    # buy_score 최대값 (8→10, 추격감점/거래량초기 반영)
EARLY_ENTRY_RATIO     =  0.50  # EARLY 진입 시 투자금 비중
FULL_ENTRY_RATIO      =  1.00  # FULL  진입 시 투자금 비중 (100%)

# ── 청산 파라미터 (★ 초공격 단타 — 손절 타이트 + 익절 빠르게) ──
STOP_LOSS_PCT         =  5.0   # ★ 손절 -5.0% (사용자 기준 -5%, 우선순위 마지막)
# ── 신규: % 기반 트레일링 ──────────────────────────────────────────
TRAIL_START_PCT       =  1.5   # ★ 트레일링 활성화 기준: +1.5% 도달 시 시작
TRAIL_PCT             =  0.5   # ★ 고점 대비 -0.5% 하락 시 즉시 매도
# ── 익절 우선순위 파라미터 (국내장과 동일 기준 적용) ─────────────
PROFIT_SUPER_PCT      =  2.5   # ★ +2.5% 무조건 전량 익절 (최우선)
PROFIT_FULL_PCT       =  2.0   # ★ +2.0% 전량 익절 (SELL_SCORE 무관)
PROFIT_TRAIL_PCT      =  1.5   # ★ +1.5% + SELL_SCORE≥4 → 전량 익절
# ── 시간 기반 청산 ────────────────────────────────────────────────
TIME_EXIT_MIN         = 60     # ★ 매수 후 60분 경과 시 청산 검토
TIME_EXIT_MIN_PCT     =  1.0   # ★ 60분 경과 후 수익률 +1% 미만이면 청산
# ── 기존 구간별 파라미터 (ZONE_HIGH 급등 구간에만 유지) ──────────
ZONE_HIGH             =  7.0   # 급등 홀딩 구간 시작 (+7%): 트레일 완화 적용
TRAIL_ZONE_HIGH       =  2.5   # 급등구간 트레일링 (고점 -2.5%)
SELL_SCORE_FAST       =  1     # 단타구간 매도지표 임계 (1개 이상)
VOL_DROP_EXIT_RATIO   =  0.6   # 거래량 0.6배 미만 급락 시 청산 조건
VOL_DROP_INTRADAY     = -0.1   # 동시에 등락률도 -0.1% 이하면 청산

# ── KRW 금액 기준 익절 (보조: 소수량 종목 대비 안전망) ─────────────
# 환율 1350원/$ 기준 — 정밀한 환율은 런타임에 재계산
PROFIT_PARTIAL_KRW_US = 10_000   # $pnl × 1350 ≥ 1만원 → 절반 익절 (≈$7.4)
PROFIT_FULL_KRW_US    = 30_000   # $pnl × 1350 ≥ 3만원 → 전량 익절 (≈$22.2)
FX_RATE_APPROX        = 1350.0   # USD→KRW 환율 근사값

# ── 추가매수 ──
ADD_BUY_PCT           =  3.0   # +3% 달성 시 추가매수 (5→3%, 더 빠르게)
ADD_BUY_RATIO         =  0.5
MAX_LEVEL             =  3     # 최대 3레벨 (2→3, 더 공격적)


# ════════════════════════════════════════════════════════════
# ── 지표 계산 엔진
# ════════════════════════════════════════════════════════════

def _ema_series(arr: np.ndarray, span: int) -> np.ndarray:
    """EMA 계산 (단순 루프, numpy only)"""
    k = 2.0 / (span + 1)
    result = [float(arr[0])]
    for v in arr[1:]:
        result.append(result[-1] * (1 - k) + float(v) * k)
    return np.array(result)


def _calc_vwap(candles: list[dict]) -> float:
    """
    일봉 기반 VWAP 근사값
    실제 intraday VWAP는 분봉 필요하지만, 일봉 데이터로
    오늘 포함 최근 5일 가격×거래량 가중평균으로 근사
    → "현재가가 이 VWAP 이상이면 평균 매수자가 수익 중" 의미 유지
    """
    if not candles:
        return 0.0
    recent = candles[-5:]  # 최근 5일
    total_vol = sum(c.get("volume", 0) for c in recent)
    if total_vol <= 0:
        return float(candles[-1].get("close", 0))
    vwap = sum(
        ((c["high"] + c["low"] + c["close"]) / 3.0) * c["volume"]
        for c in recent
    ) / total_vol
    return round(vwap, 4)


def _calc_indicators(candles: list[dict], realtime: dict = None) -> dict:
    """
    일봉 캔들 + 실시간 데이터 → 전략 지표 전체 계산

    반환 필드:
      cur_price, prev_close, intraday_pct
      vol_ratio, elapsed_ratio
      vwap, above_vwap
      ema9, ema21, ema50, ema_bull (정배열)
      rsi, macd_above, macd_cross
      last_bar_surge   : 직전 봉 단독 상승폭 (추격 여부 판단)
      pullback_breakout: 눌림 후 재상승 여부
      candle_volatility: 오늘 고저 변동폭 (%)
      buy_score, sell_score
    """
    empty = {
        "cur_price": 0.0, "prev_close": 0.0, "intraday_pct": 0.0,
        "vol_ratio": 1.0, "elapsed_ratio": 0.5,
        "vwap": 0.0, "above_vwap": False,
        "ema9": 0.0, "ema21": 0.0, "ema50": 0.0, "ema_bull": False,
        "rsi": 50.0, "macd_above": False, "macd_cross": False,
        "last_bar_surge": 0.0, "pullback_breakout": False,
        "candle_volatility": 0.0,
        "buy_score": 0, "sell_score": 0,
        "today_high": 0.0, "today_low": 0.0,
        "from_low_pct": 0.0, "chase_surge_pct": 0.0,
        "vol_early_boost": False, "at_today_high": False,
        "obv": 0.0, "obv_rising": False,
    }
    if len(candles) < 10:
        return empty

    closes  = np.array([c["close"]  for c in candles], dtype=float)
    highs   = np.array([c["high"]   for c in candles], dtype=float)
    lows    = np.array([c["low"]    for c in candles], dtype=float)
    volumes = np.array([c["volume"] for c in candles], dtype=float)

    cur = closes[-1]

    # ── ① 현재가 / 등락률 ──────────────────────────────────
    rt = realtime or {}
    prev_close = float(rt.get("previous_close", 0) or 0)
    if prev_close <= 0:
        prev_close = float(closes[-2]) if len(closes) >= 2 else cur
    intraday_pct = (cur - prev_close) / prev_close * 100 if prev_close > 0 else 0.0

    # ── ② 거래량 비율 (90일 평균 + 장 경과시간 보정) ────────
    elapsed = float(rt.get("elapsed_ratio", 0.5))
    elapsed = max(elapsed, 0.05)
    avg_vol_90d = float(rt.get("avg_volume_90d", 0) or 0)
    today_vol   = float(rt.get("today_volume",   0) or 0)
    if avg_vol_90d > 0 and today_vol > 0:
        projected_vol = today_vol / elapsed
        vol_ratio = projected_vol / avg_vol_90d
    else:
        vol_avg5 = float(np.mean(volumes[-6:-1])) if len(volumes) >= 6 else max(float(volumes[-1]), 1)
        vol_ratio = float(volumes[-1]) / vol_avg5 if vol_avg5 > 0 else 1.0

    # ── ③ VWAP ───────────────────────────────────────────
    vwap = _calc_vwap(candles)
    above_vwap = cur >= vwap if vwap > 0 else False

    # ── ④ EMA 정배열 ─────────────────────────────────────
    ema9  = float(_ema_series(closes, 9)[-1])
    ema21 = float(_ema_series(closes, 21)[-1])  if len(closes) >= 21 else ema9
    ema50 = float(_ema_series(closes, 50)[-1])  if len(closes) >= 50 else ema21
    ema_bull = (cur > ema9 > ema21 > ema50)     # 완전 정배열

    # ── ⑤ RSI(14) ────────────────────────────────────────
    if len(closes) >= 15:
        delta = np.diff(closes[-15:])
        gain  = np.where(delta > 0, delta, 0.0)
        loss  = np.where(delta < 0, -delta, 0.0)
        avg_g = float(np.mean(gain));  avg_l = float(np.mean(loss))
        rsi   = 100.0 - (100.0 / (1 + avg_g / avg_l)) if avg_l > 0 else 60.0
    else:
        rsi = 50.0

    # ── ⑥ MACD ───────────────────────────────────────────
    macd_above = False;  macd_cross = False
    if len(closes) >= 26:
        ema12 = _ema_series(closes, 12)
        ema26 = _ema_series(closes, 26)
        macd  = ema12 - ema26
        sig   = _ema_series(macd, 9)
        macd_above = bool(macd[-1] > sig[-1])
        macd_cross = bool(macd[-1] > sig[-1] and macd[-2] <= sig[-2])

    # ── ⑦ 추격매수 감지: 직전 봉 단독 상승폭 ───────────────
    last_bar_surge = 0.0
    if len(closes) >= 3:
        prev2 = closes[-3]
        if prev2 > 0:
            last_bar_surge = float((closes[-2] - prev2) / prev2 * 100)

    # ── ⑧ 눌림 후 재상승 (Pullback Breakout) ─────────────
    # 조건: 직전 봉이 이전 봉보다 낮았다가 (눌림)
    #       현재 봉이 직전 봉 고점을 넘음 (재상승)
    pullback_breakout = False
    if len(closes) >= 3 and len(highs) >= 3:
        prev_low_bar  = closes[-2] < closes[-3]        # 직전 봉이 눌렸음
        curr_breakout = cur >= highs[-2]               # 현재 직전 봉 고점 돌파
        pullback_breakout = bool(prev_low_bar and curr_breakout)

    # ── ⑨ 오늘 캔들 변동성 ──────────────────────────────
    today_high = float(highs[-1])
    today_low  = float(lows[-1])
    candle_volatility = (today_high - today_low) / today_low * 100 if today_low > 0 else 0.0

    # ── ⑩ OBV (On-Balance Volume) ───────────────────────
    # OBV: 종가 상승 시 거래량 +, 하락 시 거래량 -  누적
    # obv_rising = 최근 5봉 OBV가 상승 추세인지 (OBV[-1] > OBV[-5])
    obv_arr = np.zeros(len(closes))
    for _i in range(1, len(closes)):
        if closes[_i] > closes[_i - 1]:
            obv_arr[_i] = obv_arr[_i - 1] + volumes[_i]
        elif closes[_i] < closes[_i - 1]:
            obv_arr[_i] = obv_arr[_i - 1] - volumes[_i]
        else:
            obv_arr[_i] = obv_arr[_i - 1]
    obv_now    = float(obv_arr[-1])
    obv_rising = bool(len(obv_arr) >= 5 and obv_arr[-1] > obv_arr[-5])

    # ── ⑪ 당일 저점 대비 현재 상승률 ─────────────────────
    today_low_for_score = float(lows[-1])  # 일봉 오늘 저점 (근사)
    from_low_pct = 0.0
    if today_low_for_score > 0:
        from_low_pct = (cur - today_low_for_score) / today_low_for_score * 100

    # ── ⑫ 추격 위험도: 직전 N봉 누적 상승폭 ─────────────
    # 5분봉 대신 일봉 intraday_pct 활용 (장중 누적 상승폭 근사)
    chase_surge_pct = max(intraday_pct, last_bar_surge)

    # ── ⑬ 거래량 급증 초기 구간 여부 ─────────────────────
    # vol_ratio가 VOL_EARLY_BOOST_MIN_RATIO 이상이면 "초기 급증" 인정
    # (캔들 수 기반 "장 초반" 판단은 일봉에서 어렵기 때문에 거래량 급증 자체로 대체)
    vol_early_boost = (vol_ratio >= VOL_EARLY_BOOST_MIN_RATIO)

    # ── ⑭ 고점 돌파 추격 여부 ────────────────────────────
    # 현재가가 오늘 고가와 거의 같으면 = 고점 추격 진입
    at_today_high = False
    if today_high > 0:
        at_today_high = (cur >= today_high * 0.995)   # 고가 대비 -0.5% 이내 = 고점권

    # ── ⑮ 진입 점수 계산 (최대 10점) ────────────────────
    buy_score = 0
    if intraday_pct >= ENTRY_MIN_INTRADAY: buy_score += 2  # 핵심: 오늘 상승 중
    if vol_ratio    >= ENTRY_MIN_VOL_RATIO:buy_score += 2  # 핵심: 거래량 폭발
    if above_vwap:                         buy_score += 1  # VWAP 위
    if ema_bull:                           buy_score += 1  # EMA 정배열
    # 추가 가점
    if macd_above or macd_cross:           buy_score += 1  # MACD 우호
    if pullback_breakout:                  buy_score += 1  # 눌림 후 돌파
    # ★ 거래량 급증 초기 구간 가산점 (+1) — 세력 유입 초입 포착
    if vol_early_boost:                    buy_score += 1  # 거래량 급증 초기 가산점
    # ★ 추격매수 패널티 (감점) — 상승 확인 후 추격 억제
    if chase_surge_pct >= CHASE_SURGE_BLOCK_PCT:
        buy_score -= 2   # +5.5% 이상 이미 상승 → -2점 (사실상 진입차단)
    elif chase_surge_pct >= CHASE_SURGE_PENALTY_PCT:
        buy_score -= 1   # +3% 이상 이미 상승 → -1점 추격패널티
    # ★ 저점 대비 상승률 감점 — 너무 많이 올라간 종목 억제
    if from_low_pct >= FROM_LOW_BLOCK_PCT:
        buy_score -= 2   # 저점 대비 +8% 이상 → -2점
    elif from_low_pct >= FROM_LOW_PENALTY_PCT:
        buy_score -= 1   # 저점 대비 +4% 이상 → -1점
    buy_score = max(0, buy_score)  # 0 미만 방지

    # ── 매도 점수 ────────────────────────────────────────
    sell_score = 0
    if intraday_pct <= -0.5: sell_score += 2
    if rsi > 82:             sell_score += 2
    if not macd_above:       sell_score += 1
    if cur < ema9:           sell_score += 1

    return {
        "cur_price":         round(cur, 4),
        "prev_close":        round(prev_close, 4),
        "intraday_pct":      round(intraday_pct, 2),
        "vol_ratio":         round(vol_ratio, 2),
        "elapsed_ratio":     round(elapsed, 3),
        "vwap":              round(vwap, 4),
        "above_vwap":        above_vwap,
        "ema9":              round(ema9,  4),
        "ema21":             round(ema21, 4),
        "ema50":             round(ema50, 4),
        "ema_bull":          ema_bull,
        "rsi":               round(rsi, 1),
        "macd_above":        macd_above,
        "macd_cross":        macd_cross,
        "last_bar_surge":    round(last_bar_surge, 2),
        "pullback_breakout": pullback_breakout,
        "candle_volatility": round(candle_volatility, 2),
        "today_high":        today_high,
        "today_low":         today_low,
        "from_low_pct":      round(from_low_pct, 2),    # ★ 당일 저점 대비 상승률
        "chase_surge_pct":   round(chase_surge_pct, 2), # ★ 추격 위험도
        "vol_early_boost":   vol_early_boost,            # ★ 거래량 급증 초기 여부
        "at_today_high":     at_today_high,              # ★ 오늘 고점권 여부
        "obv":               obv_now,
        "obv_rising":        obv_rising,
        "buy_score":         buy_score,
        "sell_score":        sell_score,
    }


# ════════════════════════════════════════════════════════════
# ── 포지션 관리
# ════════════════════════════════════════════════════════════

class USPosition:
    def __init__(self, symbol, name, excd, qty, avg_price):
        self.symbol        = symbol
        self.name          = name
        self.excd          = excd
        self.qty           = qty
        self.avg_price     = avg_price
        self.highest_price = avg_price
        self.current_level = 1
        self.created_at    = datetime.now().isoformat()
        self.trade_id: str = ""   # ★ journal 연결용 (재시작 후 매수-매도 연결 유지)

    def update_high(self, price: float):
        if price > self.highest_price:
            self.highest_price = price

    def net_pct(self, cur_price: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return round((cur_price - self.avg_price) / self.avg_price * 100 - 0.25, 2)

    def to_dict(self) -> dict:
        return {
            "symbol":        self.symbol,
            "name":          self.name,
            "excd":          self.excd,
            "qty":           self.qty,
            "avg_price":     self.avg_price,
            "highest_price": self.highest_price,
            "current_level": self.current_level,
            "created_at":    self.created_at,
            "is_overseas":   True,
            "trade_id":      self.trade_id,   # ★ 재시작 후 journal 연결 유지
        }


class USPositionManager:
    def __init__(self):
        self.positions: dict[str, USPosition] = {}
        os.makedirs(os.path.dirname(US_POSITIONS_FILE), exist_ok=True)
        self._load()

    def _load(self):
        try:
            if os.path.exists(US_POSITIONS_FILE):
                data = json.load(open(US_POSITIONS_FILE, encoding="utf-8"))
                for sym, d in data.items():
                    p = USPosition(sym, d["name"], d["excd"], d["qty"], d["avg_price"])
                    p.highest_price = d.get("highest_price", d["avg_price"])
                    p.current_level = d.get("current_level", 1)
                    p.created_at    = d.get("created_at", "")
                    p.trade_id      = d.get("trade_id", "")  # ★ 하위호환
                    self.positions[sym] = p
                logger.info(f"[US포지션] {len(self.positions)}개 로드")
        except Exception as e:
            logger.warning(f"[US포지션] 로드 실패: {e}")

    def save(self):
        try:
            data = {sym: p.to_dict() for sym, p in self.positions.items()}
            with open(US_POSITIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[US포지션] 저장 실패: {e}")

    def add(self, pos: USPosition):
        self.positions[pos.symbol] = pos
        self.save()

    def remove(self, symbol: str):
        self.positions.pop(symbol, None)
        self.save()

    def update(self, symbol: str, qty: int, avg_price: float, level: int):
        if symbol in self.positions:
            p = self.positions[symbol]
            p.qty = qty;  p.avg_price = avg_price;  p.current_level = level
            self.save()


# ════════════════════════════════════════════════════════════
# ── 전략 매니저
# ════════════════════════════════════════════════════════════

class USStrategyManager:
    """
    눌림목 재돌파 진입 전략 v3
    ──────────────────────────
    - VWAP + EMA 정배열 + 눌림 후 재상승 + 거래량 + 추격금지
    - 좋은 자리만 골라서 기대수익률 높은 진입
    - 매수/매도 시 상세 사유 로그 (왜 샀는지, 왜 팔았는지)
    - 시간대별 운영 단계 (PRIME/NEUTRAL/CONSERVATIVE)
    - 목표 수익 +300,000원 달성 시 자동 차단 (미국장 세션 기준)
    """

    # ── KIS 거래불가 영구 블랙리스트 ──────────────────────────
    KIS_NO_TRADE_INITIAL: set = {"LABU", "NAIL"}

    # ── 일일 손익 관리 파라미터 (미국장) ──────────────────────
    # ★ 목표: +300,000원 달성 시 신규 매수 차단 (세션 기준)
    DAILY_TARGET_KRW     = 300_000   # ★ 목표 수익 +30만원
    DAILY_PROFIT_LOCK    = 300_000   # 목표 달성 즉시 차단
    DAILY_LOSS_LIMIT_KRW = -300_000  # 손실 한도

    # ── [US OPEN SCAN] 개장 추적 변수 ────────────────────────
    # 미국장 개장 시 초기화됨
    _open_scan_data: dict = {}   # 개장 스캔 이력 (클래스 레벨 공유)

    def __init__(self, kis_api, max_total_usd: float = 5000.0):
        self.api           = kis_api
        self.max_total_usd = max_total_usd
        self.pos_mgr       = USPositionManager()
        self._rt_cache: dict = {}
        self._kis_no_trade: set = set(self.KIS_NO_TRADE_INITIAL)

        # ★ 일일 손익 관리 (미국장) — use_us_session=True (ET 날짜 기준 리셋)
        self.pnl_guard = DailyPnLGuard(
            target_krw      = self.DAILY_TARGET_KRW,
            profit_lock_krw = self.DAILY_PROFIT_LOCK,
            loss_limit_krw  = self.DAILY_LOSS_LIMIT_KRW,
            name            = "미국장",
            use_us_session  = True,   # ★ 미국장 ET 날짜 기준 리셋 (한국 날짜 변경 무관)
        )

        # ★ 재진입 차단 (국내장/미국장 공통 파일 기반)
        self.reentry = ReentryGuard()

        # ── [US OPEN SCAN] 인스턴스 레벨 추적 ──────────────
        self._us_open_scan: dict = {
            "open_time":       None,   # 개장 감지 시각 (KST)
            "first_scan_time": None,   # 첫 스캔 시각 (KST)
            "first_entry_time": None,  # 첫 진입(매수 신호) 시각 (KST)
            "first_buy_time":  None,   # 첫 실제 매수 체결 시각 (KST)
            "session_key":     "",     # 세션 날짜 키 (ET 날짜)
            "prime_scan_done": set(),  # 완료된 초기 스캔 타임 (22:31, 22:33, 22:35, 22:40)
        }

        # ── Phase 4: OrderLifecycle + FillObserver (US Pipeline) ──────
        self._us_lifecycle_mgr   = None
        self._us_updater         = None
        self._us_pending_registry = None
        self._us_fill_observer   = None
        self._us_pending_buy_meta: dict  = {}
        self._us_pending_sell_meta: dict = {}

        if _US_LIFECYCLE_ENABLED:
            try:
                _jnl_db = os.path.join(
                    os.path.dirname(__file__), "..", "data", "trading_journal.db"
                )
                self._us_lifecycle_mgr = OrderLifecycleManager(_jnl_db)
                self._us_updater = ExecutionDrivenPositionUpdater(
                    on_buy_filled  = self._us_handle_buy_filled,
                    on_sell_filled = self._us_handle_sell_filled,
                )
                logger.info("[US Lifecycle] OrderLifecycleManager 초기화 완료")
            except Exception as _us_le:
                logger.warning(f"[US Lifecycle] 초기화 실패: {_us_le}")
                self._us_lifecycle_mgr = None
                self._us_updater       = None

        if _US_FILL_OBSERVER_ENABLED:
            try:
                self._us_pending_registry = _USPendingOrderRegistry()
                self._us_fill_observer    = _USFillObserver(
                    kis_api         = kis_api,
                    phoenix_db_path = None,
                )
                logger.info("[US FillObserver] PendingOrderRegistry + FillObserver 초기화 완료")
                self._us_restore_pending_meta()
            except Exception as _us_foe2:
                logger.warning(f"[US FillObserver] 초기화 실패: {_us_foe2}")
                self._us_pending_registry = None
                self._us_fill_observer    = None

    def set_realtime_cache(self, cache: dict):
        self._rt_cache = cache or {}

    # ══════════════════════════════════════════════════════════
    # Phase 4: US Pipeline — lifecycle 콜백 + PendingRegistry
    # ══════════════════════════════════════════════════════════

    def _us_handle_buy_filled(self, lc) -> None:
        """US BUY FILLED 시 포지션 반영 (apply는 이미 _do_buy에서 즉시 수행됨).

        US는 KR과 달리 매수 즉시 포지션을 add하는 구조이므로,
        FILLED 콜백에서는 포지션 재확인/갱신만 수행한다.
        """
        meta = self._us_pending_buy_meta.pop(lc.order_lifecycle_id, None)
        if meta is None:
            logger.warning(
                "[US BUY FILLED] pending_buy_meta 없음 — 스킵: "
                "order_lifecycle_id=%s code=%s",
                lc.order_lifecycle_id, lc.code,
            )
            return
        symbol = lc.code
        qty    = lc.filled_qty if lc.filled_qty > 0 else meta.get("qty", 0)
        price  = lc.avg_fill_price if lc.avg_fill_price else meta.get("price", 0.0)
        logger.info(
            "[US BUY FILLED] lifecycle 전이 완료: symbol=%s qty=%s @$%.2f "
            "order_lifecycle_id=%s",
            symbol, qty, price, lc.order_lifecycle_id,
        )

    def _us_handle_sell_filled(self, lc) -> None:
        """US SELL FILLED 시 PnL 기록 및 재진입 차단 등록.

        US는 _do_sell에서 즉시 pos_mgr.remove/update를 수행하므로,
        FILLED 콜백에서는 PnL 기록 + 재진입 차단만 추가 수행한다.
        """
        meta = self._us_pending_sell_meta.pop(lc.order_lifecycle_id, None)
        if meta is None:
            logger.warning(
                "[US SELL FILLED] pending_sell_meta 없음 — 스킵: "
                "order_lifecycle_id=%s code=%s",
                lc.order_lifecycle_id, lc.code,
            )
            return
        symbol  = lc.code
        qty     = lc.filled_qty if lc.filled_qty > 0 else meta.get("qty", 0)
        price   = lc.avg_fill_price if lc.avg_fill_price else meta.get("price", 0.0)
        avg_p   = meta.get("avg_price", price)
        pnl_usd = (price - avg_p) * qty
        logger.info(
            "[US SELL FILLED] lifecycle 전이 완료: symbol=%s qty=%s @$%.2f "
            "pnl_usd=$%.2f order_lifecycle_id=%s",
            symbol, qty, price, pnl_usd, lc.order_lifecycle_id,
        )

    def _us_register_pending_order(
        self,
        symbol: str,
        side: str,
        order_qty: int,
        order_response: dict,
        lifecycle_id: str,
        excd: str = "",
        trade_id: str = "",
    ) -> str:
        """US KIS 주문 응답에서 ODNO 추출 + PendingRegistry 등록.

        US 응답 구조: result["output"]["ODNO"]

        Returns:
            odno (str) — 빈 문자열이면 추출 실패
        """
        if self._us_pending_registry is None or self._us_lifecycle_mgr is None:
            return ""

        try:
            output = order_response.get("output", {}) or {}
            odno   = str(output.get("ODNO", "") or "").strip()

            from datetime import datetime as _dt
            submitted_at = _dt.now().isoformat()

            self._us_pending_registry.register(
                market             = "US",
                trade_id           = lifecycle_id,
                code               = symbol,
                side               = side,
                order_qty          = order_qty,
                submitted_at       = submitted_at,
                odno               = odno,
                client_order_id    = trade_id,
                raw_order_response = order_response,
                exchange           = excd or None,
                currency           = "USD",
            )

            lc = self._us_lifecycle_mgr.load(lifecycle_id)
            if lc is not None:
                self._us_lifecycle_mgr.accept(lc, odno=odno)
                logger.info(
                    "[US PendingRegistry] 등록 완료: symbol=%s side=%s "
                    "odno=%r lifecycle_id=%s",
                    symbol, side, odno, lifecycle_id,
                )
            else:
                logger.warning(
                    "[US PendingRegistry] lifecycle 조회 실패 — odno만 등록: "
                    "lifecycle_id=%s odno=%r", lifecycle_id, odno,
                )
            return odno

        except Exception as exc:
            logger.error(
                "[US PendingRegistry] 등록 오류: symbol=%s side=%s error=%s",
                symbol, side, exc,
            )
            return ""

    def us_dispatch_fill(
        self,
        order_lifecycle_id: str,
        filled_qty: int,
        avg_fill_price: float,
        is_full: bool = True,
    ) -> None:
        """US FillObserver → lifecycle 전이 트리거.

        run_us_fill_poll()에서 자동 호출된다.
        """
        if self._us_lifecycle_mgr is None:
            return

        lc = self._us_lifecycle_mgr.load(order_lifecycle_id)
        if lc is None:
            logger.warning(
                "[US dispatch_fill] lifecycle 없음: order_lifecycle_id=%s",
                order_lifecycle_id,
            )
            return

        try:
            if is_full:
                self._us_lifecycle_mgr.full_fill(
                    lc,
                    delta     = filled_qty,
                    avg_price = avg_fill_price,
                    on_filled = self._us_updater,
                )
            else:
                self._us_lifecycle_mgr.partial_fill(lc, filled_qty, avg_fill_price)
        except Exception as exc:
            logger.error(
                "[US dispatch_fill] lifecycle 전이 오류: "
                "order_lifecycle_id=%s is_full=%s error=%s",
                order_lifecycle_id, is_full, exc,
            )
            raise

    def run_us_fill_poll(self) -> dict:
        """US FillObserver.poll_once() → us_dispatch_fill() 자동 연결.

        app.py 의 메인 루프 또는 별도 폴링 스레드에서 주기적으로 호출한다.
        """
        if self._us_fill_observer is None or self._us_lifecycle_mgr is None:
            return {
                "total": 0, "filled": 0, "partial": 0,
                "no_change": 0, "errors": 0, "dispatched": [],
            }

        # ── ACTIVE(ACCEPTED/PARTIALLY_FILLED) 주문이 없으면 KIS API 호출 없이 즉시 반환
        if self._us_pending_registry is not None:
            _us_trackable = self._us_pending_registry.get_trackable()
            if not _us_trackable:
                return {
                    "total": 0, "filled": 0, "partial": 0,
                    "no_change": 0, "errors": 0, "dispatched": [],
                }

        dispatched = []
        try:
            poll_result = self._us_fill_observer.poll_once()
        except Exception as exc:
            logger.error("[US run_fill_poll] poll_once 오류: %s", exc)
            return {
                "total": 0, "filled": 0, "partial": 0,
                "no_change": 0, "errors": 1, "dispatched": [],
            }

        for detail in poll_result.get("details", []):
            lifecycle_id = detail.get("trade_id", "")
            fill_delta   = detail.get("fill_delta", 0)
            status_after = detail.get("status_after", "")
            error        = detail.get("error")

            if error or fill_delta <= 0 or not lifecycle_id:
                continue

            avg_fill_price = 0.0
            try:
                lc = self._us_lifecycle_mgr.load(lifecycle_id)
                if lc and lc.avg_fill_price:
                    avg_fill_price = lc.avg_fill_price
            except Exception:
                pass

            is_full = (status_after == _USPendingStatus.FILLED)
            try:
                self.us_dispatch_fill(
                    order_lifecycle_id = lifecycle_id,
                    filled_qty         = fill_delta,
                    avg_fill_price     = avg_fill_price,
                    is_full            = is_full,
                )
                dispatched.append(lifecycle_id)
                logger.info(
                    "[US run_fill_poll] dispatch 완료: lifecycle_id=%s "
                    "fill_delta=%s is_full=%s",
                    lifecycle_id, fill_delta, is_full,
                )
            except Exception as exc:
                logger.error(
                    "[US run_fill_poll] dispatch 오류: lifecycle_id=%s error=%s",
                    lifecycle_id, exc,
                )

        return {
            "total":     poll_result.get("total",     0),
            "filled":    poll_result.get("filled",    0),
            "partial":   poll_result.get("partial",   0),
            "no_change": poll_result.get("no_change", 0),
            "errors":    poll_result.get("errors",    0),
            "dispatched": dispatched,
        }

    def _us_restore_pending_meta(self) -> None:
        """재시작 후 ACCEPTED 상태의 US OrderLifecycle → pending meta 복원."""
        if self._us_lifecycle_mgr is None:
            return
        try:
            active_lifecycles = self._us_lifecycle_mgr.load_all_active()
        except Exception as exc:
            logger.warning("[US RestoreMeta] load_all_active 실패: %s", exc)
            return

        restored_buy = restored_sell = 0
        for lc in active_lifecycles:
            if (lc.market or "").upper() != "US":
                continue
            lc_id = lc.order_lifecycle_id
            side  = (lc.side or "BUY").upper()
            base_meta = {
                "code":    lc.code,
                "qty":     lc.filled_qty if lc.filled_qty > 0 else (lc.order_qty or 0),
                "price":   lc.avg_fill_price or 0.0,
                "avg_price": lc.avg_fill_price or 0.0,
                "trade_id": lc.trade_id or "",
                "reason":  "us_restored_on_restart",
            }
            if side == "BUY" and lc_id not in self._us_pending_buy_meta:
                self._us_pending_buy_meta[lc_id] = base_meta
                restored_buy += 1
            elif side == "SELL" and lc_id not in self._us_pending_sell_meta:
                self._us_pending_sell_meta[lc_id] = base_meta
                restored_sell += 1

        if restored_buy or restored_sell:
            logger.info(
                "[US RestoreMeta] 재시작 복원 완료: BUY=%d SELL=%d",
                restored_buy, restored_sell,
            )



    @property
    def positions(self) -> dict:
        return {sym: p.to_dict() for sym, p in self.pos_mgr.positions.items()}

    # ── [US OPEN SCAN] 개장 감지 및 초기 스캔 관리 ────────────
    def _check_open_scan(self, sess: dict) -> bool:
        """
        미국 정규장 개장 감지 및 초기 모멘텀 스캔 타임 체크.
        개장 직후 22:31 / 22:33 / 22:35 / 22:40 에 집중 스캔 로그 출력.
        Returns: True이면 현재가 초기 집중 스캔 타임임
        """
        import pytz
        now_kst  = datetime.now(pytz.timezone("Asia/Seoul"))
        now_et   = datetime.now(pytz.timezone("America/New_York"))
        et_date  = now_et.strftime("%Y-%m-%d")
        kst_hhmm = now_kst.strftime("%H:%M")
        kst_time_str = now_kst.strftime("%H:%M:%S KST")

        scan = self._us_open_scan

        # 세션 날짜가 바뀌면 스캔 정보 리셋
        if scan["session_key"] != et_date:
            scan["session_key"]      = et_date
            scan["open_time"]        = None
            scan["first_scan_time"]  = None
            scan["first_entry_time"] = None
            scan["first_buy_time"]   = None
            scan["prime_scan_done"]  = set()

        # 개장 감지 (ET 09:30 이후, 처음 감지 시 1회)
        if scan["open_time"] is None and sess.get("session") == "미국정규장":
            scan["open_time"] = kst_time_str
            logger.info(
                f"[US OPEN SCAN]\n"
                f"  개장=    {kst_time_str}\n"
                f"  첫 스캔= 대기 중\n"
                f"  첫 진입= 대기 중\n"
                f"  첫 매수= 대기 중"
            )

        # 첫 스캔 기록
        if scan["first_scan_time"] is None and scan["open_time"] is not None:
            scan["first_scan_time"] = kst_time_str
            logger.info(
                f"[US OPEN SCAN]\n"
                f"  개장=    {scan['open_time']}\n"
                f"  첫 스캔= {kst_time_str}\n"
                f"  첫 진입= 대기 중\n"
                f"  첫 매수= 대기 중"
            )

        # 초기 집중 스캔 타임 체크 (22:31, 22:33, 22:35, 22:40)
        _prime_scan_times = {"22:31", "22:33", "22:35", "22:40"}
        is_prime_scan_time = kst_hhmm in _prime_scan_times
        if is_prime_scan_time and kst_hhmm not in scan["prime_scan_done"]:
            scan["prime_scan_done"].add(kst_hhmm)
            logger.info(
                f"[US OPEN SCAN] 🔥 초기 모멘텀 스캔 타임: {kst_hhmm} KST\n"
                f"  개장=    {scan.get('open_time', '?')}\n"
                f"  첫 스캔= {scan.get('first_scan_time', '?')}\n"
                f"  첫 진입= {scan.get('first_entry_time', '?') or '대기 중'}\n"
                f"  첫 매수= {scan.get('first_buy_time', '?') or '대기 중'}"
            )
            return True

        return is_prime_scan_time

    def _record_first_entry(self):
        """첫 진입 시각 기록"""
        import pytz
        scan = self._us_open_scan
        if scan["first_entry_time"] is None:
            now_kst = datetime.now(pytz.timezone("Asia/Seoul"))
            scan["first_entry_time"] = now_kst.strftime("%H:%M:%S KST")

    def _record_first_buy(self):
        """첫 매수 체결 시각 기록 + [US OPEN SCAN] 완성 로그"""
        import pytz
        scan = self._us_open_scan
        if scan["first_buy_time"] is None:
            now_kst = datetime.now(pytz.timezone("Asia/Seoul"))
            scan["first_buy_time"] = now_kst.strftime("%H:%M:%S KST")
            logger.info(
                f"[US OPEN SCAN] ✅ 첫 매수 체결!\n"
                f"  개장=    {scan.get('open_time', '?')}\n"
                f"  첫 스캔= {scan.get('first_scan_time', '?')}\n"
                f"  첫 진입= {scan.get('first_entry_time', '?')}\n"
                f"  첫 매수= {scan['first_buy_time']}"
            )

    # ── 메인 실행 루프 ─────────────────────────────────────
    def run(self, stock: dict) -> dict:
        symbol = stock["symbol"]
        name   = stock.get("name", symbol)
        excd   = stock.get("excd", "NASD")
        sess   = us_session_info()

        # ── [US OPEN SCAN] 개장 체크 ──────────────────────
        _is_prime_scan = self._check_open_scan(sess)

        # ── 현재 운영 단계 확인 ────────────────────────────
        phase_info = us_phase_info()
        phase      = phase_info["phase"]
        phase_icon = phase_info["icon"]

        # KIS 거래불가 블랙리스트 즉시 스킵
        if symbol in self._kis_no_trade:
            return {"action": "SKIP", "symbol": symbol, "name": name,
                    "reason": "KIS거래불가종목(블랙리스트)", "session": sess["session"]}

        if not sess["tradeable"]:
            # ★ 보유 포지션은 휴장 중에도 손절/익절 관리 필요
            # → 단, KIS 주문이 실제로 가능한 세션(미국정규장)이 아니면 SELL 불가
            # → 프리마켓/애프터/휴장 시간에는 손익 모니터링만 하고 SELL은 정규장에서만
            _can_sell_now = sess["session"] == "미국정규장"
            pos_held = self.pos_mgr.positions.get(symbol)
            if pos_held:
                if not _can_sell_now:
                    # 프리마켓·애프터·휴장: 현재가 확인 후 손익 로그만 출력 (주문 없음)
                    realtime_off  = self._fetch_realtime(symbol, excd)
                    cur_price_off = float(realtime_off.get("cur_price", 0) or 0)
                    if cur_price_off > 0:
                        net_pct = (cur_price_off - pos_held.avg_price) / pos_held.avg_price * 100 if pos_held.avg_price > 0 else 0.0
                        logger.info(
                            f"[휴장모니터링] {name}({symbol}) 세션={sess['session']} | "
                            f"현재=${cur_price_off:.2f} | 손익={net_pct:+.2f}% | "
                            f"⛔ KIS 주문불가시간 — 정규장(ET 09:30~16:00) 재개 시 자동 관리"
                        )
                    else:
                        logger.info(f"[휴장모니터링] {name}({symbol}) 세션={sess['session']} | 현재가 취득 실패 → SKIP")
                    return {"action": "SKIP", "symbol": symbol, "name": name,
                            "reason": f"KIS주문불가({sess['session']}) 정규장에서 관리예정", "session": sess["session"]}
                # 미국정규장 중이지만 tradeable=False인 경우(이론적으로 없음, 방어코드)
                realtime_off  = self._fetch_realtime(symbol, excd)
                cur_price_off = float(realtime_off.get("cur_price", 0) or 0)
                candles_off   = None
                if cur_price_off <= 0:
                    candles_off = self.api.get_us_ohlcv(symbol, excd, count=60)
                    if candles_off:
                        cur_price_off = float(candles_off[-1]["close"])
                if cur_price_off > 0:
                    # 지표 계산용 캔들 준비
                    if candles_off and len(candles_off) >= 2:
                        live_candles = candles_off[:-1] + [{**candles_off[-1], "close": cur_price_off}]
                    else:
                        live_candles = []
                    iv_off = _calc_indicators(live_candles, realtime_off)
                    logger.info(
                        f"[휴장중포지션] {name}({symbol}) 휴장({sess['session']})이나 "
                        f"보유포지션 존재 → 손절/익절 판단 실행 | 현재가=${cur_price_off:.2f}"
                    )
                    return self._manage_position(
                        pos_held, symbol, name, excd, cur_price_off, iv_off, sess
                    )
                else:
                    logger.warning(
                        f"[휴장중포지션] {name}({symbol}) 현재가 취득 실패 → SKIP"
                    )
            return {"action": "SKIP", "symbol": symbol, "name": name,
                    "reason": f"미국 휴장({sess['session']})", "session": sess["session"]}

        # ── ★ DailyPnLGuard 상태 로그 (매 루프 항상 INFO) ──────
        _g = self.pnl_guard
        _g._check_date_reset()
        _sicon = {"TRADING": "🟢", "PROFIT_LOCK": "🎯",
                  "LOSS_LIMIT": "🚫", "HALTED": "⛔"}.get(_g.state, "❓")
        _bstr  = "매수가능" if _g.state == "TRADING" else "🚫매수차단"

        # ── 운영 단계 로그 ──────────────────────────────────
        logger.info(
            f"[미국장 운영단계] {phase_icon} {phase} | "
            f"{phase_info['description']} | "
            f"신규진입={'허용' if phase_info['allow_new_buy'] else '차단'} | "
            f"적극성={phase_info['aggressiveness']}/3"
        )

        # ── [US PROFIT TARGET] 로그 ─────────────────────────
        logger.info(
            f"[US PROFIT TARGET]\n"
            f"  실현손익= {_g.realized_pnl:+,.0f}원\n"
            f"  목표=    +{_g.target_krw:,}원\n"
            f"  상태=    {'거래중' if _g.state == 'TRADING' else '목표달성-매수차단' if _g.state == 'PROFIT_LOCK' else '손실한도-매수차단'}"
        )

        # 보유 중이면 평가손익 계산 (USD→KRW 근사, 환율 조회 없이 상수 사용)
        _pos_now = self.pos_mgr.positions.get(symbol)
        _unrealized_us = 0.0
        if _pos_now and _pos_now.avg_price > 0:
            # 현재가는 rt_cache에서 우선 취득
            _rt_tmp = self._rt_cache.get(symbol, {})
            _cp_tmp = float(_rt_tmp.get("cur_price", 0) or 0)
            if _cp_tmp > 0:
                _unrealized_us = (_cp_tmp - _pos_now.avg_price) * _pos_now.qty * FX_RATE_APPROX

        # LOSS_LIMIT 판정 근거 한 줄 요약
        if _g.state == "LOSS_LIMIT":
            _us_verdict = (
                f"실현({_g.realized_pnl:+,.0f}원)"
                f" ≤ 한도({_g.loss_limit_krw:,}원) → LOSS_LIMIT"
            )
        elif _g.state == "PROFIT_LOCK":
            _us_verdict = (
                f"peak({_g.peak_pnl:+,.0f}원)≥목표 AND "
                f"실현({_g.realized_pnl:+,.0f}원)≤Lock → PROFIT_LOCK"
            )
        else:
            _us_verdict = (
                f"실현({_g.realized_pnl:+,.0f}원)"
                f" > 한도({_g.loss_limit_krw:,}원) → TRADING"
            )

        logger.info(
            f"[미국장 PnL] {_sicon} {_g.state} | {_bstr} | "
            f"실현손익={_g.realized_pnl:+,.0f}원(★기준) | "
            f"평가손익={_unrealized_us:+,.0f}원(참고,미포함) | "
            f"최고실현={_g.peak_pnl:+,.0f}원 | "
            f"LOSS_LIMIT판정=[{_us_verdict}] | "
            f"종목={name}({symbol})"
            + (f" | ⚠️차단: {_g.block_reason()}" if not _g.can_buy else "")
        )

        # ① 실시간 데이터
        realtime  = self._fetch_realtime(symbol, excd)
        cur_price = realtime.get("cur_price", 0.0)

        # ② 일봉 OHLCV (EMA50 = 최소 60개 필요)
        candles = self.api.get_us_ohlcv(symbol, excd, count=60)
        if not candles or len(candles) < 10:
            return {"action": "SKIP", "symbol": symbol, "name": name,
                    "reason": "OHLCV 부족", "session": sess["session"]}

        if cur_price <= 0:
            cur_price = float(candles[-1]["close"])
        if cur_price <= 0:
            return {"action": "SKIP", "symbol": symbol, "name": name,
                    "reason": "현재가 없음", "session": sess["session"]}

        # 오늘 캔들 현재가로 교체
        candles_live = candles[:-1] + [{**candles[-1], "close": cur_price}]

        # ③ 지표 계산
        iv = _calc_indicators(candles_live, realtime)

        pos = self.pos_mgr.positions.get(symbol)

        # ── 상태 로그 (매 루프) ───────────────────────────
        logger.info(
            f"[🇺🇸] {name}({symbol}) ${cur_price:.2f} "
            f"등락:{iv['intraday_pct']:+.1f}% "
            f"vol:{iv['vol_ratio']:.1f}x "
            f"OBV:{'✅상승' if iv.get('obv_rising') else '❌하락'} "
            f"VWAP:{'✅' if iv['above_vwap'] else '❌'}(${iv['vwap']:.2f}) "
            f"EMA:{'✅정배열' if iv['ema_bull'] else '❌'} "
            f"RSI:{iv['rsi']:.0f} "
            f"BUY:{iv['buy_score']}/8"
            + (f" | 보유{pos.net_pct(cur_price):+.1f}%" if pos else "")
        )

        # ════════════════════════════════════════════════
        # 보유 중 → 청산/추가매수 판단
        # ════════════════════════════════════════════════
        if pos:
            return self._manage_position(pos, symbol, name, excd,
                                         cur_price, iv, sess)

        # ════════════════════════════════════════════════
        # 미보유 → 진입 조건 체크 (전부 충족 시에만 매수)
        # ════════════════════════════════════════════════
        return self._check_entry(symbol, name, excd, cur_price, iv,
                                 candles_live, realtime, sess)

    # ── 포지션 관리 (보유 중) ──────────────────────────────
    def _manage_position(self, pos, symbol, name, excd, cur_price, iv, sess):
        pos.update_high(cur_price)
        net_pct  = pos.net_pct(cur_price)
        high_pct = (cur_price - pos.highest_price) / pos.highest_price * 100 \
                   if pos.highest_price > 0 else 0.0

        # ── USD PnL / KRW 환산 (공통 계산) ──────────────────
        pnl_usd = (cur_price - pos.avg_price) * pos.qty
        try:
            fx = self.api.get_usd_exchange_rate()
            if not fx or fx <= 0:
                fx = FX_RATE_APPROX
        except Exception:
            fx = FX_RATE_APPROX
        pnl_krw_approx = pnl_usd * fx

        # ── 보유 경과 시간 계산 ──────────────────────────────
        try:
            created = datetime.fromisoformat(pos.created_at) if pos.created_at else None
            elapsed_min = (datetime.now() - created).total_seconds() / 60 if created else 0
        except Exception:
            elapsed_min = 0

        # ════════════════════════════════════════════════════
        # [익절판정] 로그 — 포지션 보유 시 매 루프 항상 출력
        # gross_pct = 수수료 미차감 단순 수익률 (미국장 net_pct ≈ gross_pct)
        # ════════════════════════════════════════════════════
        gross_pct = (cur_price - pos.avg_price) / pos.avg_price * 100 \
                    if pos.avg_price > 0 else 0.0
        # 익절 기준 판별 (로그용)
        if net_pct >= PROFIT_SUPER_PCT:
            _us_basis = f"+{PROFIT_SUPER_PCT}%무조건전량"
        elif net_pct >= PROFIT_FULL_PCT:
            _us_basis = f"+{PROFIT_FULL_PCT}%전량"
        elif net_pct >= PROFIT_TRAIL_PCT:
            _us_basis = f"+{PROFIT_TRAIL_PCT}%+SELL_SCORE≥4조건부전량(현재score={iv.get('sell_score',0)})"
        else:
            _us_basis = f"익절기준미달(최소+{PROFIT_TRAIL_PCT}%필요)"

        logger.info(
            f"[익절판정] 종목={name}({symbol}) | "
            f"매수가=${pos.avg_price:.2f} | 현재가=${cur_price:.2f} | "
            f"gross_pct={gross_pct:+.3f}% | net_pct={net_pct:+.3f}% | "
            f"익절기준={_us_basis} | SELL_SCORE={iv.get('sell_score',0)} | "
            f"경과={elapsed_min:.0f}분 | PnL=${pnl_usd:+.2f}≈{pnl_krw_approx:,.0f}원"
        )

        # ════════════════════════════════════════════════════
        # 청산 우선순위: ①+2.5% → ②+2.0% → ③+1.5%+SCORE≥4
        #               → ④KRW → ⑤트레일링 → ⑥시간청산
        #               → ⑦손절(최후)
        # ════════════════════════════════════════════════════

        # ════════════════════════════════════════════════════
        # ① +2.5% 무조건 전량 익절 (최우선, 예외 없음)
        # ════════════════════════════════════════════════════
        if net_pct >= PROFIT_SUPER_PCT:
            logger.info(
                f"[익절판정] 종목={name}({symbol}) | net_pct={net_pct:+.3f}% | "
                f"익절기준=+{PROFIT_SUPER_PCT}%무조건전량 | 결과=SELL_ALL"
            )
            return self._do_sell(
                symbol, name, excd, pos.qty, cur_price,
                f"✅+2.5%무조건전량익절 {net_pct:+.2f}% ≥ +{PROFIT_SUPER_PCT}%"
                f" (${pnl_usd:+.2f}≈{pnl_krw_approx:,.0f}원)",
                sess
            )

        # ════════════════════════════════════════════════════
        # ② +2.0% 전량 익절 (SELL_SCORE 무관, 예외 없음)
        # ════════════════════════════════════════════════════
        if net_pct >= PROFIT_FULL_PCT:
            logger.info(
                f"[익절판정] 종목={name}({symbol}) | net_pct={net_pct:+.3f}% | "
                f"익절기준=+{PROFIT_FULL_PCT}%전량 | 결과=SELL_ALL"
            )
            return self._do_sell(
                symbol, name, excd, pos.qty, cur_price,
                f"✅+2.0%전량익절 {net_pct:+.2f}% ≥ +{PROFIT_FULL_PCT}%"
                f" (${pnl_usd:+.2f}≈{pnl_krw_approx:,.0f}원)",
                sess
            )

        # ════════════════════════════════════════════════════
        # ③ +1.5% + SELL_SCORE≥4 → 전량 익절
        # ════════════════════════════════════════════════════
        _us_sell_score = iv.get("sell_score", 0)
        if net_pct >= PROFIT_TRAIL_PCT and _us_sell_score >= 4:
            logger.info(
                f"[익절판정] 종목={name}({symbol}) | net_pct={net_pct:+.3f}% | "
                f"익절기준=+{PROFIT_TRAIL_PCT}%+SELL_SCORE≥4 | 결과=SELL_ALL"
            )
            return self._do_sell(
                symbol, name, excd, pos.qty, cur_price,
                f"✅+1.5%익절+SELL_SCORE {net_pct:+.2f}%≥{PROFIT_TRAIL_PCT}%"
                f" score={_us_sell_score}≥4"
                f" (${pnl_usd:+.2f}≈{pnl_krw_approx:,.0f}원)",
                sess
            )

        # HOLD 사유 로그 (익절 미발생)
        if net_pct >= PROFIT_TRAIL_PCT:
            _us_hold = f"+1.5%이상이나SELL_SCORE={_us_sell_score}<4(≥4필요)"
        elif net_pct > 0:
            _us_hold = f"수익중이나익절기준미달(net={net_pct:+.2f}%, 최소+{PROFIT_TRAIL_PCT}%필요)"
        else:
            _us_hold = f"손실중(net={net_pct:+.2f}%)"
        logger.info(
            f"[익절판정] 종목={name}({symbol}) | net_pct={net_pct:+.3f}% | "
            f"결과=HOLD | HOLD사유={_us_hold}"
        )

        # ── [US 손절판정] 매 루프 INFO 출력 ────────────────
        _stop_hit = net_pct <= -STOP_LOSS_PCT
        logger.info(
            f"[US 손절판정] 종목={name}({symbol}) | "
            f"현재손익={net_pct:+.2f}%(${pnl_usd:+.2f}≈{pnl_krw_approx:+,.0f}원) | "
            f"손절조건=net({net_pct:+.2f}%) <= -{STOP_LOSS_PCT}% | "
            f"결과={'⚡손절실행' if _stop_hit else f'유지(아직 -{STOP_LOSS_PCT}% 미달)'}"
        )

        # ════════════════════════════════════════════════════
        # ④ KRW 금액 기준 익절 (소수량 종목 안전망)
        #    3만원 → 전량 / 1만원 → 절반
        # ════════════════════════════════════════════════════
        if pnl_krw_approx >= PROFIT_FULL_KRW_US:
            return self._do_sell(
                symbol, name, excd, pos.qty, cur_price,
                f"💰KRW전량익절 ${pnl_usd:+.2f}≈{pnl_krw_approx:,.0f}원"
                f" ≥ {PROFIT_FULL_KRW_US:,}원 (@환율{fx:.0f})",
                sess
            )

        if pnl_krw_approx >= PROFIT_PARTIAL_KRW_US and pos.qty >= 2:
            half_qty = max(1, pos.qty // 2)
            return self._do_sell(
                symbol, name, excd, half_qty, cur_price,
                f"💰KRW부분익절(50%) ${pnl_usd:+.2f}≈{pnl_krw_approx:,.0f}원"
                f" ≥ {PROFIT_PARTIAL_KRW_US:,}원 (@환율{fx:.0f})",
                sess, is_partial=True
            )

        # ════════════════════════════════════════════════════
        # ⑤ 트레일링 스탑 (★ 핵심 개선)
        #    - +1.5%(TRAIL_START_PCT) 도달 시 즉시 활성화
        #    - 고점 대비 -0.5%(TRAIL_PCT) 하락 시 매도
        #    - +7% 초과 급등 구간은 -2.5% 완화 적용
        # ════════════════════════════════════════════════════
        max_net = pos.net_pct(pos.highest_price)  # 고점 기준 최대 수익률

        if max_net >= ZONE_HIGH:
            # 급등 구간(+7% 초과): trail 완화 (-2.5%)
            trail = TRAIL_ZONE_HIGH
            zone  = "급등홀딩"
        elif max_net >= TRAIL_START_PCT:
            # ★ 단타/중간 통합: +1.5% 도달 시 즉시 -0.5% 트레일 적용
            trail = TRAIL_PCT
            zone  = f"트레일링(최고{max_net:+.1f}%)"
        else:
            # 아직 +1.5% 미도달 — 트레일링 비활성
            trail = None
            zone  = "대기"

        if trail is not None and high_pct <= -trail:
            logger.info(
                f"[US 트레일링] 종목={name}({symbol}) | "
                f"구간={zone} | 고점=${pos.highest_price:.2f}({max_net:+.1f}%) | "
                f"현재=${cur_price:.2f}({net_pct:+.2f}%) | "
                f"고점대비={high_pct:.1f}% ≤ 기준=-{trail}% | 결과=⚡청산실행"
            )
            return self._do_sell(symbol, name, excd, pos.qty, cur_price,
                                 f"트레일링[{zone}] 고점${pos.highest_price:.2f}→"
                                 f"현재${cur_price:.2f} ({high_pct:.1f}% ≤ -{trail}%)", sess)

        # ── [US 트레일링 상태] 매 루프 INFO 출력 ─────────────
        logger.info(
            f"[US 트레일링] 종목={name}({symbol}) | "
            f"구간={zone} | 고점=${pos.highest_price:.2f}({max_net:+.1f}%) | "
            f"현재=${cur_price:.2f}({net_pct:+.2f}%) | "
            f"고점대비={high_pct:.1f}% | trail={'비활성' if trail is None else f'-{trail}%'} | "
            f"결과=유지"
        )

        # ════════════════════════════════════════════════════
        # ⑥ 시간 청산: 매수 후 60분 경과 + 수익 +1% 미만
        #    → 기회비용 방지, 자금 순환
        # ════════════════════════════════════════════════════
        _time_exit_hit = elapsed_min >= TIME_EXIT_MIN and net_pct < TIME_EXIT_MIN_PCT
        logger.info(
            f"[US 시간청산] 종목={name}({symbol}) | "
            f"보유시간={elapsed_min:.0f}분(기준{TIME_EXIT_MIN}분) | "
            f"현재수익={net_pct:+.2f}%(기준+{TIME_EXIT_MIN_PCT}%) | "
            f"결과={'⚡청산실행' if _time_exit_hit else f'유지(경과{elapsed_min:.0f}분/{TIME_EXIT_MIN}분 또는 수익{net_pct:+.1f}%≥+{TIME_EXIT_MIN_PCT}%)'}"
        )
        if _time_exit_hit:
            return self._do_sell(
                symbol, name, excd, pos.qty, cur_price,
                f"⏱시간청산 {elapsed_min:.0f}분 경과, 수익{net_pct:+.1f}% < +{TIME_EXIT_MIN_PCT}%"
                f" (기준 {TIME_EXIT_MIN}분)",
                sess
            )

        # ════════════════════════════════════════════════════
        # ⑦ 단타구간: 매도지표 1개 이상 (트레일 비활성 구간만)
        # ════════════════════════════════════════════════════
        if zone == "대기" and iv["sell_score"] >= SELL_SCORE_FAST:
            reasons = []
            if iv["intraday_pct"] <= -0.5: reasons.append(f"등락{iv['intraday_pct']:+.1f}%")
            if iv["rsi"] > 82:             reasons.append(f"RSI과열{iv['rsi']:.0f}")
            if not iv["macd_above"]:       reasons.append("MACD역전")
            if cur_price < iv["ema9"]:     reasons.append("EMA9이탈")
            return self._do_sell(symbol, name, excd, pos.qty, cur_price,
                                 f"단타청산 매도지표{iv['sell_score']}개 [{','.join(reasons)}]", sess)

        # ════════════════════════════════════════════════════
        # ⑧ 거래량+등락률 동시 급락 → 모멘텀 소멸
        # ════════════════════════════════════════════════════
        if iv["vol_ratio"] < VOL_DROP_EXIT_RATIO and iv["intraday_pct"] < VOL_DROP_INTRADAY:
            tag = "수익중" if net_pct > 0 else "손실중"
            return self._do_sell(symbol, name, excd, pos.qty, cur_price,
                                 f"모멘텀소멸({tag}) "
                                 f"vol:{iv['vol_ratio']:.1f}x↓ "
                                 f"등락:{iv['intraday_pct']:+.1f}%↓", sess)

        # ════════════════════════════════════════════════════
        # ⑨ EMA9 이탈 + VWAP 하회 (추세 완전 붕괴, 손실 중일 때만)
        # ════════════════════════════════════════════════════
        if cur_price < iv["ema9"] and not iv["above_vwap"] and net_pct < 0:
            return self._do_sell(symbol, name, excd, pos.qty, cur_price,
                                 f"추세붕괴 EMA9${iv['ema9']:.2f} VWAP${iv['vwap']:.2f} 모두 이탈", sess)

        # ════════════════════════════════════════════════════
        # ⑩ 손절: 실질수익률 ≤ -STOP_LOSS_PCT (우선순위 마지막)
        # ════════════════════════════════════════════════════
        if net_pct <= -STOP_LOSS_PCT:
            return self._do_sell(symbol, name, excd, pos.qty, cur_price,
                                 f"손절 {net_pct:.1f}% (기준 -{STOP_LOSS_PCT}%)", sess)

        # ════════════════════════════════════════════════════
        # ⑪ 추가매수: +3% + 급등 지속
        # ════════════════════════════════════════════════════
        if (pos.current_level < MAX_LEVEL
                and net_pct >= ADD_BUY_PCT
                and iv["intraday_pct"] >= 1.0
                and iv["vol_ratio"]    >= 1.5
                and iv["above_vwap"]):
            return self._do_add_buy(symbol, name, excd, pos, cur_price, sess, iv)

        # ── [US HOLD 최종 상태] INFO 출력 ──────────────────
        trail_disp = f"-{trail}%" if trail is not None else "비활성"
        logger.info(
            f"[US HOLD] 종목={name}({symbol}) | "
            f"보유시간={elapsed_min:.0f}분 | 현재손익={net_pct:+.2f}%(${pnl_usd:+.2f}≈{pnl_krw_approx:+,.0f}원) | "
            f"SELL_SCORE={iv.get('sell_score',0)} | "
            f"구간={zone} | trail={trail_disp} | "
            f"고점=${pos.highest_price:.2f}({max_net:+.1f}%) | "
            f"VWAP:{'위' if iv['above_vwap'] else '아래'} | EMA:{'정배열' if iv['ema_bull'] else '역배열'}"
        )
        return {
            "action":    "HOLD",
            "symbol":    symbol,  "name": name,
            "price":     cur_price, "net_pct": net_pct,
            "buy_score": iv["buy_score"], "sell_score": iv["sell_score"],
            "intraday_pct": iv["intraday_pct"], "vol_ratio": iv["vol_ratio"],
            "rsi": iv["rsi"], "session": sess["session"],
            "pnl_krw_approx": round(pnl_krw_approx, 0),
            "reason": (f"[{zone}] 보유 {net_pct:+.1f}% "
                       f"PnL≈{pnl_krw_approx:+,.0f}원 | "
                       f"고점대비{high_pct:.1f}% | trail={trail_disp} | "
                       f"경과{elapsed_min:.0f}분 | "
                       f"VWAP:{'위' if iv['above_vwap'] else '아래'} "
                       f"EMA:{'정배열' if iv['ema_bull'] else '역배열'}"),
        }

    # ── 진입 조건 체크 ─────────────────────────────────────
    def _check_entry(self, symbol, name, excd, cur_price, iv,
                     candles_live, realtime, sess):
        """
        모든 진입 조건을 순서대로 검사.
        미충족 조건을 명시적으로 로그에 남김.
        ★ 운영 단계(PRIME/NEUTRAL/CONSERVATIVE)에 따라 조건 강화.
        """

        def _hold(reason: str, tag: str = "HOLD") -> dict:
            return {
                "action": tag, "symbol": symbol, "name": name,
                "price": cur_price, "buy_score": iv["buy_score"],
                "sell_score": iv["sell_score"],
                "intraday_pct": iv["intraday_pct"], "vol_ratio": iv["vol_ratio"],
                "rsi": iv["rsi"], "session": sess["session"], "reason": reason,
            }

        # ── 현재 운영 단계 확인 ──────────────────────────────
        phase_info  = us_phase_info()
        phase       = phase_info["phase"]
        phase_icon  = phase_info["icon"]
        extra_score = phase_info["buy_score_min"]   # 단계별 추가 점수 요건

        # ── [US BUY 판정] 매 루프 INFO 출력 (미보유 종목) ───────
        _obv_state   = "상승" if iv.get("obv_rising") else "하락"
        _vwap_state  = f"위(${iv['vwap']:.2f})" if iv["above_vwap"] else f"아래(${iv['vwap']:.2f})"
        _score_norm  = iv["buy_score"] / BUY_SCORE_MAX
        _early_thr   = BUY_SCORE_EARLY * BUY_SCORE_MAX
        _full_thr    = BUY_SCORE_FULL  * BUY_SCORE_MAX
        if iv["buy_score"] >= _full_thr:
            _buy_expect = f"FULL진입예상(점수{iv['buy_score']}≥{_full_thr:.1f})"
        elif iv["buy_score"] >= _early_thr:
            _buy_expect = f"EARLY진입예상(점수{iv['buy_score']}≥{_early_thr:.1f})"
        else:
            _buy_expect = f"점수부족REJECT({iv['buy_score']}<{_early_thr:.1f})"

        # ─── [US ENTRY ANALYSIS] 진입 분석 로그 (매 루프, 미보유 종목) ────
        _today_low_disp  = iv.get("today_low", 0.0)
        _from_low        = iv.get("from_low_pct", 0.0)
        _chase_surge     = iv.get("chase_surge_pct", 0.0)
        _vol_early       = iv.get("vol_early_boost", False)
        _at_high         = iv.get("at_today_high", False)
        # 추격위험 판정
        if _chase_surge >= CHASE_SURGE_BLOCK_PCT or _from_low >= FROM_LOW_BLOCK_PCT:
            _chase_risk = f"🔴HIGH(급등{_chase_surge:+.1f}%/저점대비{_from_low:+.1f}%)"
        elif _chase_surge >= CHASE_SURGE_PENALTY_PCT or _from_low >= FROM_LOW_PENALTY_PCT:
            _chase_risk = f"🟡MED(급등{_chase_surge:+.1f}%/저점대비{_from_low:+.1f}%)"
        else:
            _chase_risk = f"🟢LOW(급등{_chase_surge:+.1f}%/저점대비{_from_low:+.1f}%)"
        # 진입 결정 사유
        if iv["buy_score"] >= _full_thr:
            _entry_reason_disp = f"FULL진입({iv['buy_score']:.0f}pt≥{_full_thr:.1f})"
        elif iv["buy_score"] >= _early_thr:
            _entry_reason_disp = f"EARLY진입({iv['buy_score']:.0f}pt≥{_early_thr:.1f})"
        else:
            _entry_reason_disp = f"진입보류({iv['buy_score']:.0f}pt<{_early_thr:.1f})"
        if _at_high:
            _entry_reason_disp += " ⚠️고점권"
        if _vol_early:
            _entry_reason_disp += " ✅거래량초기급증"
        logger.info(
            f"[US ENTRY ANALYSIS] "
            f"종목={name}({symbol}) | "
            f"당일저점=${_today_low_disp:.2f} | "
            f"현재가=${cur_price:.2f} | "
            f"저점대비상승률={_from_low:+.1f}% | "
            f"BUY_SCORE={iv['buy_score']}/{BUY_SCORE_MAX} | "
            f"추격매수위험={_chase_risk} | "
            f"진입결정사유={_entry_reason_disp}"
        )
        logger.info(
            f"[US BUY 판정] 종목={name}({symbol}) | "
            f"{phase_icon}단계={phase} | "
            f"BUY_SCORE={iv['buy_score']}/{BUY_SCORE_MAX}({_score_norm:.0%}) | "
            f"거래량={iv['vol_ratio']:.1f}x({'초기급증' if _vol_early else '일반'}) | "
            f"OBV={_obv_state} | VWAP={_vwap_state} | 등락={iv['intraday_pct']:+.1f}% | "
            f"RSI={iv['rsi']:.0f} | EMA={'정배열' if iv['ema_bull'] else '역배열'} | "
            f"결과={_buy_expect}"
        )

        # ── ★ 운영 단계: CONSERVATIVE → 신규 진입 최소화 ────────
        if phase == US_PHASE_CONSERVATIVE:
            logger.info(
                f"[US 운영단계] 🛡️ CONSERVATIVE(01:00 이후) — "
                f"신규 진입 차단 | 익절/손절 관리만 허용 | "
                f"종목={name}({symbol})"
            )
            return _hold(
                f"🛡️CONSERVATIVE단계(01:00이후) 신규진입 최소화 — 익절/손절만 허용",
                "SKIP"
            )

        # ── ★ 운영 단계: NEUTRAL → 진입 조건 강화 (extra_score 적용) ──
        if phase == US_PHASE_NEUTRAL and extra_score > 0:
            neutral_full_thr  = _full_thr  + extra_score
            neutral_early_thr = _early_thr + extra_score
            if iv["buy_score"] < neutral_early_thr:
                logger.info(
                    f"[US 운영단계] ⚖️ NEUTRAL(00:00~01:00) — "
                    f"점수 강화 기준 미달: {iv['buy_score']} < {neutral_early_thr:.0f} "
                    f"(원래기준{_early_thr:.0f}+NEUTRAL가중{extra_score}) | "
                    f"종목={name}({symbol})"
                )
                return _hold(
                    f"⚖️NEUTRAL단계 점수부족 {iv['buy_score']}/{BUY_SCORE_MAX} < "
                    f"강화기준{neutral_early_thr:.0f}점",
                    "HOLD"
                )
            # NEUTRAL 단계에서는 강화된 임계값 사용
            _full_thr  = neutral_full_thr
            _early_thr = neutral_early_thr

        # ── ★ DailyPnLGuard: 신규 진입 차단 체크 ────────────
        if not self.pnl_guard.can_buy:
            block_msg = self.pnl_guard.block_reason()
            logger.warning(
                f"🚫 [미국장] 신규 매수 차단 → {symbol}\n"
                f"  원인: ① LOSS_LIMIT/PROFIT_LOCK\n"
                f"  상태: {self.pnl_guard.state}\n"
                f"  실현손익: {self.pnl_guard.realized_pnl:+,.0f}원 (★평가손익 아님)\n"
                f"  차단사유: {block_msg}"
            )
            return _hold(block_msg, "SKIP")

        # ── 진입 조건 체크 전 — 미충족 원인 수집 ──────────
        _fail_reasons: list[str] = []

        # ── [필수 1] 오늘 등락률 최소 기준 ────────────────
        if iv["intraday_pct"] < ENTRY_MIN_INTRADAY:
            _fail_reasons.append(f"❌등락미충족 {iv['intraday_pct']:+.1f}%<+{ENTRY_MIN_INTRADAY}%")
            if _fail_reasons:
                logger.debug(
                    f"[미국장 매수불발] {symbol} | ② 진입조건미충족 | "
                    + " / ".join(_fail_reasons)
                )
            return _hold(f"❌등락미충족 {iv['intraday_pct']:+.1f}% < +{ENTRY_MIN_INTRADAY}%")

        # ── [필수 2] 거래량 최소 기준 ─────────────────────
        if iv["vol_ratio"] < ENTRY_MIN_VOL_RATIO:
            _fail_reasons.append(f"❌거래량부족 {iv['vol_ratio']:.1f}x<{ENTRY_MIN_VOL_RATIO}x")
            logger.debug(
                f"[미국장 매수불발] {symbol} | ② 진입조건미충족 | "
                + " / ".join(_fail_reasons)
            )
            return _hold(f"❌거래량부족 {iv['vol_ratio']:.1f}x < {ENTRY_MIN_VOL_RATIO}x")

        # ── [필수 3] VWAP 위에서만 진입 ──────────────────
        if not iv["above_vwap"]:
            _fail_reasons.append(f"❌VWAP미달 ${cur_price:.2f}<${iv['vwap']:.2f}")
            logger.debug(
                f"[미국장 매수불발] {symbol} | ② 진입조건미충족 | "
                + " / ".join(_fail_reasons)
            )
            return _hold(
                f"❌VWAP미달 현재${cur_price:.2f} < VWAP${iv['vwap']:.2f} "
                f"(평균매수자 손실 구간)"
            )

        # ── [필수 4] EMA 정배열 (완화: EMA9>EMA21 이면 OK) ───────
        # 완전 정배열(EMA9>EMA21>EMA50)이 아니어도 단기 상승 추세면 진입 허용
        ema_partial_bull = iv["ema9"] > iv["ema21"]   # 단기 정배열
        if not ema_partial_bull:
            ema_state = (f"EMA9${iv['ema9']:.2f} EMA21${iv['ema21']:.2f} "
                         f"EMA50${iv['ema50']:.2f}")
            _fail_reasons.append(f"❌EMA역배열")
            logger.debug(
                f"[미국장 매수불발] {symbol} | ② 진입조건미충족 | "
                + " / ".join(_fail_reasons)
            )
            return _hold(f"❌EMA역배열 {ema_state}")
        # 완전 정배열(EMA9>EMA21>EMA50)이면 가산점 부여 (로그에 표시)
        ema_note = "✅EMA완전정배열" if iv["ema_bull"] else "✅EMA단기정배열"

        # ── [필수 5] 추격매수 금지 ─────────────────────────────────
        # 5a. 직전 봉 단독 급등 (기존 로직)
        if iv["last_bar_surge"] >= ENTRY_CHASE_BAR_PCT:
            _fail_reasons.append(f"❌추격금지 직전봉+{iv['last_bar_surge']:.1f}%")
            logger.debug(
                f"[미국장 매수불발] {symbol} | ② 진입조건미충족 | "
                + " / ".join(_fail_reasons)
            )
            return _hold(
                f"❌추격매수금지 직전봉+{iv['last_bar_surge']:.1f}% "
                f"(기준 +{ENTRY_CHASE_BAR_PCT}%) — 이미 급등 후"
            )
        # 5b. 당일 누적 상승폭이 이미 CHASE_SURGE_BLOCK_PCT 이상 → 고점 추격 차단
        _chase_surge_now = iv.get("chase_surge_pct", 0.0)
        if _chase_surge_now >= CHASE_SURGE_BLOCK_PCT:
            _fail_reasons.append(f"❌당일급등추격 +{_chase_surge_now:.1f}%≥+{CHASE_SURGE_BLOCK_PCT}%")
            logger.info(
                f"[미국장 매수불발] {symbol} | ⚠️ 당일 이미 +{_chase_surge_now:.1f}% 급등 "
                f"(기준 +{CHASE_SURGE_BLOCK_PCT}%) → 고점 추격 차단"
            )
            return _hold(
                f"❌당일급등추격차단 +{_chase_surge_now:.1f}% ≥ +{CHASE_SURGE_BLOCK_PCT}% "
                f"— 상승 초입 아님"
            )
        # 5c. 저점 대비 FROM_LOW_BLOCK_PCT 이상 → 이미 너무 많이 올라간 종목 차단
        _from_low_now = iv.get("from_low_pct", 0.0)
        if _from_low_now >= FROM_LOW_BLOCK_PCT:
            _fail_reasons.append(f"❌저점급등 저점대비+{_from_low_now:.1f}%≥+{FROM_LOW_BLOCK_PCT}%")
            logger.info(
                f"[미국장 매수불발] {symbol} | ⚠️ 당일저점 대비 +{_from_low_now:.1f}% 상승 "
                f"(기준 +{FROM_LOW_BLOCK_PCT}%) → 이미 고점권, 진입 차단"
            )
            return _hold(
                f"❌저점대비고점차단 +{_from_low_now:.1f}% ≥ +{FROM_LOW_BLOCK_PCT}% "
                f"— 상승 초입 아님"
            )

        # ── [필수 6] 꼭대기 매수 방지 ────────────────────
        today_high = float(candles_live[-1].get("high", cur_price) or cur_price)
        if today_high > 0:
            drop_from_high = (today_high - cur_price) / today_high * 100
            if drop_from_high > PEAK_DROP_FILTER:
                _fail_reasons.append(f"❌꼭대기방지 -{drop_from_high:.1f}%")
                logger.debug(
                    f"[미국장 매수불발] {symbol} | ② 진입조건미충족 | "
                    + " / ".join(_fail_reasons)
                )
                return _hold(
                    f"❌꼭대기방지 고가${today_high:.2f}→현재${cur_price:.2f} "
                    f"(-{drop_from_high:.1f}% 하락, 기준 -{PEAK_DROP_FILTER}%)"
                )

        # ── [필수 7] 마감 30분 전 신규 진입 금지 ─────────
        if realtime.get("elapsed_ratio", 0) > 0.92:
            logger.debug(f"[미국장 매수불발] {symbol} | ② 진입조건미충족 | ❌마감30분전")
            return _hold("❌마감30분전 신규 진입 금지")

        # ── [필수 8] RSI 과열 제외 (완화: 85로 상향) ────────
        if iv["rsi"] >= 85:
            logger.debug(f"[미국장 매수불발] {symbol} | ② 진입조건미충족 | ❌RSI과열 {iv['rsi']:.0f}")
            return _hold(f"❌RSI과열 {iv['rsi']:.0f} ≥ 85")

        # ── [가산점 조건] 눌림 후 재상승 여부 로그 ────────
        pullback_note = "✅눌림후재돌파 " if iv["pullback_breakout"] else "⚠️눌림없이진입 "

        # ── [가산점] MACD 상태 로그 ───────────────────────
        if iv["macd_cross"]:
            macd_note = "✅MACD골든크로스 "
        elif iv["macd_above"]:
            macd_note = "✅MACD상향 "
        else:
            macd_note = "⚠️MACD하향(진입주의) "

        # ── OBV 상태 ──────────────────────────────────────
        obv_note  = "✅OBV상승" if iv.get("obv_rising") else "⚠️OBV하락"
        obv_rising = iv.get("obv_rising", False)

        # ── 점수 기반 2단계 진입 결정 ─────────────────────
        # 최대 점수 기준으로 정규화
        # ★ NEUTRAL 단계: extra_score 만큼 임계값 상향
        score_norm   = iv["buy_score"] / BUY_SCORE_MAX      # 0.0~1.0
        _neutral_add = extra_score if phase == US_PHASE_NEUTRAL else 0
        early_thresh = BUY_SCORE_EARLY * BUY_SCORE_MAX + _neutral_add  # PRIME: 3.8 / NEUTRAL: 4.8
        full_thresh  = BUY_SCORE_FULL  * BUY_SCORE_MAX + _neutral_add  # PRIME: 5.5 / NEUTRAL: 6.5

        # ── EARLY ENTRY 필수 3조건: 거래량증가 + OBV상승 + VWAP위 ──
        early_must_ok  = (
            iv["vol_ratio"] >= ENTRY_MIN_VOL_RATIO   # 거래량 증가
            and obv_rising                            # OBV 상승
            and iv["above_vwap"]                     # VWAP 위
        )

        def _early_must_desc() -> str:
            parts = []
            if iv["vol_ratio"] < ENTRY_MIN_VOL_RATIO:
                parts.append(f"거래량{iv['vol_ratio']:.1f}x<{ENTRY_MIN_VOL_RATIO}x")
            if not obv_rising:
                parts.append("OBV하락")
            if not iv["above_vwap"]:
                parts.append(f"VWAP미달${cur_price:.2f}<${iv['vwap']:.2f}")
            return " / ".join(parts) if parts else "OK"

        # ── [ENTRY REJECT] 로그 공통 출력 함수 ───────────
        def _log_reject(reason: str) -> dict:
            logger.info(
                f"[ENTRY REJECT] 종목={name}({symbol}) | "
                f"BUY_SCORE={iv['buy_score']}/{BUY_SCORE_MAX}({score_norm:.0%}) | "
                f"거래량={iv['vol_ratio']:.1f}x | "
                f"OBV={'상승' if obv_rising else '하락'} | "
                f"VWAP={'위' if iv['above_vwap'] else '아래'}(${iv['vwap']:.2f}) | "
                f"탈락사유={reason}"
            )
            return _hold(reason)

        # ── FULL ENTRY: 점수 ≥ 0.65×8 → 100% 진입 ────────
        if iv["buy_score"] >= full_thresh:
            entry_reason = (
                f"[FULL진입] BUY_SCORE={iv['buy_score']}/{BUY_SCORE_MAX} "
                f"등락{iv['intraday_pct']:+.1f}% "
                f"vol{iv['vol_ratio']:.1f}x {obv_note} "
                f"VWAP위(${iv['vwap']:.2f}) "
                f"{ema_note} {pullback_note}{macd_note}"
                f"RSI{iv['rsi']:.0f} 직전봉{iv['last_bar_surge']:+.1f}%"
                f" [{phase_icon}{phase}]"
            )
            logger.info(f"[🎯진입결정] {name}({symbol}) | {entry_reason}")
            # ── [US OPEN SCAN] 첫 진입 기록 ──────────────────
            self._record_first_entry()
            return self._do_buy(symbol, name, excd, cur_price, sess, iv,
                                entry_reason, entry_ratio=FULL_ENTRY_RATIO)

        # ── EARLY ENTRY: 점수 ≥ 0.50×8 + 필수 3조건 → 50% 진입 ──
        if iv["buy_score"] >= early_thresh and early_must_ok:
            early_reason = (
                f"[EARLY진입50%] BUY_SCORE={iv['buy_score']}/{BUY_SCORE_MAX} "
                f"등락{iv['intraday_pct']:+.1f}% "
                f"vol{iv['vol_ratio']:.1f}x {obv_note} "
                f"VWAP위(${iv['vwap']:.2f}) "
                f"{ema_note} {pullback_note}{macd_note}"
                f"RSI{iv['rsi']:.0f} 직전봉{iv['last_bar_surge']:+.1f}%"
                f" [{phase_icon}{phase}]"
            )
            logger.info(
                f"[EARLY ENTRY] 종목={name}({symbol}) | "
                f"BUY_SCORE={iv['buy_score']}/{BUY_SCORE_MAX}({score_norm:.0%}) | "
                f"거래량={iv['vol_ratio']:.1f}x | "
                f"OBV={'상승' if obv_rising else '하락'} | "
                f"VWAP={'위' if iv['above_vwap'] else '아래'}(${iv['vwap']:.2f}) | "
                f"진입비중={EARLY_ENTRY_RATIO:.0%} | {phase_icon}{phase}"
            )
            # ── [US OPEN SCAN] 첫 진입 기록 ──────────────────
            self._record_first_entry()
            return self._do_buy(symbol, name, excd, cur_price, sess, iv,
                                early_reason, entry_ratio=EARLY_ENTRY_RATIO)

        # ── 점수 부족 → REJECT ────────────────────────────
        if iv["buy_score"] < early_thresh:
            return _log_reject(
                f"점수부족 {iv['buy_score']}/{BUY_SCORE_MAX} < EARLY기준{early_thresh:.0f}점"
            )
        # early_thresh 이상이나 필수 3조건 미충족
        return _log_reject(
            f"EARLY필수3조건미충족({_early_must_desc()}) "
            f"점수={iv['buy_score']}/{BUY_SCORE_MAX} FULL기준{full_thresh:.0f}점미달"
        )

    # ── 실시간 데이터 수집 ─────────────────────────────────
    def _fetch_realtime(self, symbol: str, excd: str) -> dict:
        """배치 캐시 우선 → yfinance 개별 → KIS 폴백"""
        if symbol in self._rt_cache:
            return self._rt_cache[symbol]

        import pytz
        result = {
            "cur_price": 0.0, "previous_close": 0.0,
            "today_volume": 0, "avg_volume_90d": 0,
            "elapsed_ratio": 0.5, "change_rate": 0.0,
        }

        try:
            et_now      = datetime.now(pytz.timezone("America/New_York"))
            market_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
            elapsed_min = max(0.0, (et_now - market_open).total_seconds() / 60)
            result["elapsed_ratio"] = min(elapsed_min / 390.0, 1.0)
        except Exception:
            pass

        try:
            import yfinance as yf
            fi   = yf.Ticker(symbol).fast_info
            cur  = float(getattr(fi, "last_price",               0) or 0)
            prev = float(getattr(fi, "previous_close",           0) or 0)
            tvol = int(getattr(fi,   "last_volume",              0) or 0)
            avgv = int(getattr(fi,   "three_month_average_volume",0) or 0)
            if cur > 0:
                result.update({
                    "cur_price":      cur,
                    "previous_close": prev,
                    "today_volume":   tvol,
                    "avg_volume_90d": avgv,
                    "change_rate":    round((cur - prev) / prev * 100, 2) if prev > 0 else 0.0,
                })
                return result
        except Exception:
            pass

        # KIS 폴백
        try:
            pd  = self.api.get_us_current_price(symbol, excd)
            cur = float(pd.get("price", 0) or 0)
            if cur > 0:
                result["cur_price"]    = cur
                result["change_rate"]  = float(pd.get("change_rate", 0) or 0)
                result["today_volume"] = int(pd.get("volume", 0) or 0)
        except Exception:
            pass

        return result

    # ── 매수 가능 여부 사전 체크 + 적정 수량 계산 ────────────────
    def _check_buy_capacity(self, symbol: str, cur_price: float, qty: int) -> tuple[bool, str]:
        """
        매수 가능 여부를 실제 USD 주문가능금액(frcr_ord_psbl_amt1) 기준으로 판단.

        KIS TTTS3007R 핵심 필드:
          frcr_ord_psbl_amt1 → usd: 실제 USD 주문가능금액 (원화→환전 포함)
          ovrs_ord_psbl_amt  → krw: 원화결제 한도 (계좌 설정에 따라 0일 수 있음)

        반환: (가능여부: bool, 사유: str)
        """
        try:
            avail = self.api.get_us_available_amounts()
            usd_avail = avail.get("usd", 0.0)   # frcr_ord_psbl_amt1 — 핵심
            krw_avail = avail.get("krw", 0.0)   # ovrs_ord_psbl_amt  — 보조

            need_usd = cur_price * qty

            if usd_avail >= need_usd:
                return True, f"USD가능(${usd_avail:.2f} >= 필요${need_usd:.2f})"

            # USD 부족 → KRW 보조 확인 (ovrs_ord_psbl_amt > 0 인 경우만)
            if krw_avail > 0:
                fx = 1350.0
                try:
                    fx = self.api.get_usd_exchange_rate()
                except Exception:
                    pass
                need_krw = need_usd * fx * 1.005
                if krw_avail >= need_krw:
                    return True, (
                        f"USD부족(${usd_avail:.2f}) => 원화환전 "
                        f"필요{need_krw:,.0f}원 <= 가용{krw_avail:,.0f}원"
                    )

            # 모두 부족
            return False, (
                f"USD${usd_avail:.2f} < 필요${need_usd:.2f} "
                f"(ovrs_ord_psbl={krw_avail:,.0f}원)"
            )
        except Exception as e:
            logger.warning(f"[{symbol}] 주문가능금액 조회 실패(주문 그대로 시도): {e}")
            return True, "가능금액조회실패→주문시도"

    # ── 매수 실행 ──────────────────────────────────────────
    def _do_buy(self, symbol, name, excd, cur_price, sess, iv,
                entry_reason: str = "",
                entry_ratio: float = FULL_ENTRY_RATIO) -> dict:
        """
        entry_ratio: 투자금 비중 (0.0~1.0)
          - FULL  진입: 1.00 (100%)
          - EARLY 진입: 0.50 ( 50%) → INVEST_PER_TRADE_USD의 50%만 집행
        """
        # ── ★ 재진입 차단 체크 (매도 후 24h/72h 쿨다운) ──────
        _re_blocked, _re_info = self.reentry.check("US", symbol, name)
        if _re_blocked:
            ReentryGuard.log_block(_re_info)
            return {
                "action":  "SKIP",
                "symbol":  symbol, "name": name, "excd": excd,
                "reason":  (
                    f"⛔재진입 차단 — {_re_info['block_reason']} "
                    f"(잔여 {_re_info['remaining_hours']:.1f}h)"
                ),
                "session": sess.get("session", ""),
            }

        # ── 실제 USD 주문가능금액 기준 동적 수량 계산 ──────────
        # 매수 종목 기준 TTTS3007R 조회 → 정확한 ovrs_ord_psbl_amt 확보
        try:
            avail = self.api.get_us_available_amounts(symbol=symbol, excd=excd)
            usd_avail = avail.get("usd", 0.0)
            krw_avail = avail.get("krw", 0.0)
        except Exception:
            usd_avail = 0.0
            krw_avail = 0.0
        # 원화 가능금액도 USD로 환산해서 예산 계산
        # ovrs_ord_psbl_amt > 0 이면 원화결제 사용 가능
        if krw_avail > 0:
            try:
                fx = self.api.get_usd_exchange_rate() or 1350.0
            except Exception:
                fx = 1350.0
            usd_from_krw = (krw_avail / fx) * 0.99   # 환전 수수료 1% 감안
            effective_usd = max(usd_avail, usd_from_krw)
            logger.info(
                f"[{symbol}] 원화결제 가능: ovrs_krw={krw_avail:,.0f}원 "
                f"≈ USD${usd_from_krw:.2f} (환율{fx:.0f}) | "
                f"frcr_usd=${usd_avail:.2f} → 실사용예산 max=${effective_usd:.2f}"
            )
        else:
            effective_usd = usd_avail

        # entry_ratio 적용: EARLY는 50%, FULL은 100% 투자
        budget_usd = min(INVEST_PER_TRADE_USD * entry_ratio,
                         effective_usd * INVEST_RATIO_OF_AVAIL * entry_ratio)
        if budget_usd < cur_price:
            # 1주도 못 살 경우 즉시 포기
            logger.warning(
                f"💸 {symbol} 잔고부족: 예산${budget_usd:.2f} < 1주${cur_price:.2f} "
                f"(usd=${usd_avail:.2f}, krw={krw_avail:,.0f}원)"
            )
            return {"action": "BUY_FAIL", "symbol": symbol, "name": name,
                    "reason": f"잔고부족: 예산${budget_usd:.2f} < 1주${cur_price:.2f}",
                    "session": sess["session"]}
        qty = max(1, int(budget_usd / cur_price))
        ratio_label = f"EARLY{entry_ratio:.0%}" if entry_ratio < 1.0 else "FULL100%"
        logger.info(f"[{symbol}] 예산${budget_usd:.2f}({ratio_label}) / ${cur_price:.2f} = {qty}주")

        # ── 사전 잔고 체크 (USD + KRW 통합) ──────────────────
        can_buy, capacity_msg = self._check_buy_capacity(symbol, cur_price, qty)
        logger.info(f"[💰잔고체크] {symbol} {qty}주 ${cur_price:.2f} → {capacity_msg}")
        if not can_buy:
            logger.warning(f"💸 {symbol} 주문가능금액 부족 → 건너뜀: {capacity_msg}")
            return {"action": "BUY_FAIL", "symbol": symbol, "name": name,
                    "reason": f"잔고부족: {capacity_msg}", "session": sess["session"]}

        # allow_krw_order=True → USD 실패 시 KIS 내부에서 원화 자동환전 재시도
        # ── [US 훅 A] SIGNAL_CONFIRMED + trade_id 생성 ──
        _us_trade_id = ""
        if _US_JOURNAL_ENABLED:
            try:
                _us_trade_id = _us_jnl.make_trade_id("US", symbol)
                _us_jnl.record_signal(
                    _us_trade_id, "US", symbol, name,
                    entry_type    = "FULL" if entry_ratio >= 1.0 else "EARLY",
                    signal_price  = cur_price,
                    buy_score     = iv.get("buy_score", 0.0),
                    sell_score    = 0.0,
                    rsi           = iv.get("rsi"),
                    bb_upper      = None, bb_middle = None, bb_lower = None,
                    atr           = None,
                    volume        = None, volume_ratio = iv.get("vol_ratio"),
                    ai_total_score= None, rs_value = None,
                    orderable_cash= None,
                    session       = sess.get("session", ""),
                    entry_reason  = entry_reason,
                    payload       = {"excd": excd, "entry_ratio": entry_ratio,
                                     "budget_usd": budget_usd, "qty": qty},
                )
            except Exception as _uje:
                _us_jnl._inc_error("us_signal", _uje)

        # ── [US 훅 B] ORDER_SUBMITTED — api.buy_us() 직전 ──
        if _US_JOURNAL_ENABLED and _us_trade_id:
            try:
                _us_jnl.record_order_submitted(
                    _us_trade_id, "US", symbol,
                    order_price = cur_price,
                    order_qty   = qty,
                    payload     = {"excd": excd},
                )
            except Exception as _uje:
                _us_jnl._inc_error("us_submitted", _uje)

        result   = self.api.buy_us(symbol, qty, cur_price, excd, allow_krw_order=True)
        order_ok = result.get("rt_cd") == "0"
        fail_msg = result.get("msg1", "")

        if not order_ok:
            # ── [US 훅 C] ORDER_REJECTED ──
            if _US_JOURNAL_ENABLED and _us_trade_id:
                try:
                    _us_jnl.record_order_rejected(
                        _us_trade_id, "US", symbol,
                        rt_cd = result.get("rt_cd", "?"),
                        msg1  = fail_msg,
                    )
                except Exception as _uje:
                    _us_jnl._inc_error("us_rejected", _uje)
            # KIS 거래불가 → 영구 블랙리스트
            if "해당종목" in fail_msg or "종목정보" in fail_msg or "해당 종목" in fail_msg:
                logger.warning(f"🚫 {symbol} KIS거래불가 → 블랙리스트 등록: {fail_msg}")
                self._kis_no_trade.add(symbol)
                return {"action": "BUY_FAIL", "symbol": symbol, "name": name,
                        "reason": f"KIS거래불가(블랙리스트): {fail_msg}",
                        "session": sess["session"]}
            # 잔고 부족 (원화 재시도 후에도 실패)
            if "초과" in fail_msg or "부족" in fail_msg or "금액" in fail_msg:
                logger.warning(f"💸 {symbol} 원화환전 후에도 잔고부족: {fail_msg}")
                return {"action": "BUY_FAIL", "symbol": symbol, "name": name,
                        "reason": f"잔고부족(원화시도후): {fail_msg}", "session": sess["session"]}
            # 기타 오류 → 잔고 재확인
            logger.warning(f"⚠️ {symbol} 주문실패 → 잔고재확인: {fail_msg}")
            try:
                bal   = self.api.get_us_balance()
                h_map = {h["symbol"]: h for h in bal.get("holdings", [])}
                if symbol in h_map:
                    h  = h_map[symbol]
                    aq = int(h.get("qty", qty));  ap = float(h.get("avg_price", cur_price))
                    pos = USPosition(symbol, name, excd, aq, ap)
                    self.pos_mgr.add(pos)
                    logger.info(f"✅ {symbol} 잔고확인 자동등록 {aq}주")
                    return self._buy_result(symbol, name, excd, ap, aq, 1, sess, iv,
                                            entry_reason, "[잔고확인자동등록]")
                return {"action": "BUY_FAIL", "symbol": symbol, "name": name,
                        "reason": fail_msg or "주문실패", "session": sess["session"]}
            except Exception as e2:
                return {"action": "BUY_FAIL", "symbol": symbol, "name": name,
                        "reason": str(e2), "session": sess["session"]}

        pos = USPosition(symbol, name, excd, qty, cur_price)
        self.pos_mgr.add(pos)
        # ── [US 훅 D] ORDER_ACCEPTED (접수 성공, 체결 미확인) ──
        # ★ ORDER_FILLED 는 실체결 확인 훅에만 기록. rt_cd=0 은 접수이지 체결이 아님.
        if _US_JOURNAL_ENABLED and _us_trade_id:
            try:
                _us_jnl.record_order_accepted(
                    _us_trade_id, "US", symbol,
                    rt_cd = result.get("rt_cd", "0"),
                    msg1  = result.get("msg1", "US매수접수성공_체결미확인"),
                )
                # trade_id를 포지션에 저장 (매도 연결용)
                _us_pos_saved = self.pos_mgr.positions.get(symbol)
                if _us_pos_saved and hasattr(_us_pos_saved, "trade_id"):
                    _us_pos_saved.trade_id = _us_trade_id
            except Exception as _uje:
                _us_jnl._inc_error("us_accepted", _uje)
        # ★ Phase 4: US BUY lifecycle 등록 + PendingRegistry 자동 연결
        if _US_LIFECYCLE_ENABLED and self._us_lifecycle_mgr is not None:
            try:
                _us_lc_id = make_order_lifecycle_id("US", "BUY", symbol)
                _us_lc = self._us_lifecycle_mgr.create(
                    trade_id      = _us_trade_id or _us_lc_id,
                    market        = "US",
                    code          = symbol,
                    side          = "BUY",
                    strategy_name = "USStrategyManager",
                    order_qty     = qty,
                )
                self._us_pending_buy_meta[_us_lc.order_lifecycle_id] = {
                    "code": symbol, "name": name, "qty": qty,
                    "price": cur_price, "avg_price": cur_price,
                    "trade_id": _us_trade_id,
                    "reason": entry_reason,
                }
                self._us_register_pending_order(
                    symbol         = symbol,
                    side           = "BUY",
                    order_qty      = qty,
                    order_response = result,
                    lifecycle_id   = _us_lc.order_lifecycle_id,
                    excd           = excd,
                    trade_id       = _us_trade_id,
                )
                logger.info(
                    "[US BUY ACCEPTED] lifecycle+PendingRegistry 등록 완료: "
                    "order_lifecycle_id=%s symbol=%s qty=%s",
                    _us_lc.order_lifecycle_id, symbol, qty,
                )
            except Exception as _us_le:
                logger.warning("[US BUY Lifecycle] 등록 오류: %s", _us_le)
        # ── [US OPEN SCAN] 첫 매수 기록 ──────────────────────
        self._record_first_buy()
        logger.info(
            f"🟢 US매수 {name}({symbol}) ${cur_price:.2f}×{qty}주 [{ratio_label}]\n"
            f"   진입사유: {entry_reason}\n"
            f"   잔고상태: {capacity_msg}"
        )
        return self._buy_result(symbol, name, excd, cur_price, qty, 1, sess, iv,
                                entry_reason, "")

    def _do_add_buy(self, symbol, name, excd, pos, cur_price, sess, iv) -> dict:
        add_qty = max(1, int(INVEST_PER_TRADE_USD * ADD_BUY_RATIO / cur_price))
        # allow_krw_order=True → 추가매수도 원화환전 허용

        # ── [US 훅 E] 추가매수 SIGNAL + SUBMITTED ──
        _us_add_trade_id = ""
        if _US_JOURNAL_ENABLED:
            try:
                _us_add_trade_id = _us_jnl.make_trade_id("US", symbol)
                _us_jnl.record_signal(
                    _us_add_trade_id, "US", symbol, name,
                    entry_type    = "ADD",
                    signal_price  = cur_price,
                    buy_score     = iv.get("buy_score", 0.0),
                    sell_score    = 0.0,
                    rsi           = iv.get("rsi"),
                    bb_upper=None, bb_middle=None, bb_lower=None, atr=None,
                    volume=None, volume_ratio=iv.get("vol_ratio"),
                    ai_total_score=None, rs_value=None, orderable_cash=None,
                    session       = sess.get("session", ""),
                    entry_reason  = "모멘텀추가매수",
                    payload       = {"excd": excd, "qty": add_qty},
                )
                _us_jnl.record_order_submitted(
                    _us_add_trade_id, "US", symbol,
                    order_price = cur_price,
                    order_qty   = add_qty,
                    payload     = {"excd": excd, "add_buy": True},
                )
            except Exception as _uje:
                _us_jnl._inc_error("us_add_signal", _uje)

        result  = self.api.buy_us(symbol, add_qty, cur_price, excd, allow_krw_order=True)
        if result.get("rt_cd") != "0":
            # ── [US 훅 F] 추가매수 ORDER_REJECTED ──
            if _US_JOURNAL_ENABLED and _us_add_trade_id:
                try:
                    _us_jnl.record_order_rejected(
                        _us_add_trade_id, "US", symbol,
                        rt_cd = result.get("rt_cd", "?"),
                        msg1  = result.get("msg1", "추가매수실패"),
                    )
                except Exception as _uje:
                    _us_jnl._inc_error("us_add_rejected", _uje)
            return {"action": "BUY_FAIL", "symbol": symbol, "name": name,
                    "reason": result.get("msg1", "추가매수실패"), "session": sess["session"]}
        new_qty = pos.qty + add_qty
        new_avg = (pos.avg_price * pos.qty + cur_price * add_qty) / new_qty
        self.pos_mgr.update(symbol, new_qty, new_avg, 2)
        reason = (f"모멘텀추가매수: 수익{pos.net_pct(cur_price):+.1f}% "
                  f"vol{iv['vol_ratio']:.1f}x VWAP위 등락{iv['intraday_pct']:+.1f}%")
        # ── [US 훅 G] 추가매수 ORDER_ACCEPTED (접수, 체결 미확인) ──
        # ★ ORDER_FILLED 는 실체결 확인 훅에만 기록. US 체결조회 미연동 → 비워 둔.
        if _US_JOURNAL_ENABLED and _us_add_trade_id:
            try:
                _us_jnl.record_order_accepted(
                    _us_add_trade_id, "US", symbol,
                    rt_cd = result.get("rt_cd", "0"),
                    msg1  = result.get("msg1", "US추가매수접수성공_체결미확인"),
                )
            except Exception as _uje:
                _us_jnl._inc_error("us_add_accepted", _uje)
        # ★ Phase 4: US ADD_BUY lifecycle 등록 + PendingRegistry 자동 연결
        if _US_LIFECYCLE_ENABLED and self._us_lifecycle_mgr is not None:
            try:
                _us_add_lc_id = make_order_lifecycle_id("US", "BUY", symbol)
                _us_add_lc = self._us_lifecycle_mgr.create(
                    trade_id      = _us_add_trade_id or _us_add_lc_id,
                    market        = "US",
                    code          = symbol,
                    side          = "BUY",
                    strategy_name = "USStrategyManager_AddBuy",
                    order_qty     = add_qty,
                )
                self._us_pending_buy_meta[_us_add_lc.order_lifecycle_id] = {
                    "code": symbol, "name": name, "qty": add_qty,
                    "price": cur_price, "avg_price": cur_price,
                    "trade_id": _us_add_trade_id,
                    "reason": reason,
                }
                self._us_register_pending_order(
                    symbol         = symbol,
                    side           = "BUY",
                    order_qty      = add_qty,
                    order_response = result,
                    lifecycle_id   = _us_add_lc.order_lifecycle_id,
                    excd           = excd,
                    trade_id       = _us_add_trade_id,
                )
                logger.info(
                    "[US ADD_BUY ACCEPTED] lifecycle+PendingRegistry 등록 완료: "
                    "order_lifecycle_id=%s symbol=%s qty=%s",
                    _us_add_lc.order_lifecycle_id, symbol, add_qty,
                )
            except Exception as _us_add_le:
                logger.warning("[US ADD_BUY Lifecycle] 등록 오류: %s", _us_add_le)
        logger.info(f"🟢 US추가매수 {symbol} {add_qty}주 ${cur_price:.2f} | {reason}")
        return self._buy_result(symbol, name, excd, cur_price, add_qty, 2, sess, iv,
                                reason, "[모멘텀추가]")

    def _do_sell(self, symbol, name, excd, qty, cur_price, reason, sess,
                 is_partial: bool = False) -> dict:
        # ── [US 훅 H] SELL_SIGNAL_CONFIRMED — 포지션에서 trade_id 조회 ──
        _us_sell_trade_id = ""
        if _US_JOURNAL_ENABLED:
            try:
                _us_sell_pos = self.pos_mgr.positions.get(symbol)
                if _us_sell_pos and hasattr(_us_sell_pos, "trade_id"):
                    _us_sell_trade_id = _us_sell_pos.trade_id or ""
                _us_jnl.record_sell_signal(
                    _us_sell_trade_id, "US", symbol,
                    sell_price  = cur_price,
                    sell_score  = 0.0,
                    exit_reason = reason,
                    payload     = {"is_partial": is_partial, "qty": qty,
                                   "session": sess.get("session", "")},
                )
            except Exception as _uje:
                _us_jnl._inc_error("us_sell_signal", _uje)

        # ── [US 훅 I] SELL_ORDER_SUBMITTED ──
        if _US_JOURNAL_ENABLED:
            try:
                _us_jnl.record_sell_order_submitted(
                    _us_sell_trade_id, "US", symbol,
                    sell_price = cur_price,
                    sell_qty   = qty,
                    payload    = {"excd": excd},
                )
            except Exception as _uje:
                _us_jnl._inc_error("us_sell_submitted", _uje)

        result  = self.api.sell_us(symbol, qty, cur_price, excd)
        if result.get("rt_cd") == "0":
            # ── [US 훅 J] SELL_ORDER_ACCEPTED ──
            if _US_JOURNAL_ENABLED:
                try:
                    _us_jnl.record_sell_order_accepted(
                        _us_sell_trade_id, "US", symbol,
                        rt_cd = result.get("rt_cd", "0"),
                        msg1  = result.get("msg1", "US매도주문접수성공"),
                    )
                except Exception as _uje:
                    _us_jnl._inc_error("us_sell_accepted", _uje)

            pos     = self.pos_mgr.positions.get(symbol)
            avg_p   = pos.avg_price if pos else cur_price
            pnl_usd = (cur_price - avg_p) * qty
            pnl_pct = (cur_price - avg_p) / avg_p * 100 if avg_p > 0 else 0.0

            if is_partial and pos and pos.qty > qty:
                # 부분 익절: 수량만 줄이고 포지션 유지
                new_qty = pos.qty - qty
                self.pos_mgr.update(symbol, new_qty, avg_p, pos.current_level)
                action_tag = "SELL_PARTIAL"
                logger.info(
                    f"💰 US부분익절 {name}({symbol}) ${cur_price:.2f}×{qty}주"
                    f"→ 잔여{new_qty}주 PnL ${pnl_usd:+.2f} ({pnl_pct:+.1f}%)\n"
                    f"   매도사유: {reason}"
                )
                logger.info(
                    f"[US SELL] 종목={name}({symbol}) | "
                    f"매수가=${avg_p:.2f} | 매도가=${cur_price:.2f} | "
                    f"수량={qty}주(부분) | "
                    f"실현손익=${pnl_usd:+.2f}({pnl_pct:+.1f}%) | "
                    f"매도사유={reason}"
                )
            else:
                # 전량 매도
                self.pos_mgr.remove(symbol)
                action_tag = "SELL"
                emoji = "💰" if pnl_usd >= 0 else "🔴"
                logger.info(
                    f"{emoji} US매도 {name}({symbol}) ${cur_price:.2f}×{qty}주 "
                    f"PnL ${pnl_usd:+.2f} ({pnl_pct:+.1f}%)\n"
                    f"   매도사유: {reason}"
                )
                logger.info(
                    f"[US SELL] 종목={name}({symbol}) | "
                    f"매수가=${avg_p:.2f} | 매도가=${cur_price:.2f} | "
                    f"수량={qty}주(전량) | "
                    f"실현손익=${pnl_usd:+.2f}({pnl_pct:+.1f}%) | "
                    f"매도사유={reason}"
                )

            # ★ DailyPnLGuard에 실현 손익 기록 (USD → KRW 환산)
            try:
                fx = self.api.get_usd_exchange_rate() or 1350.0
            except Exception:
                fx = 1350.0
            pnl_krw = pnl_usd * fx
            self.pnl_guard.record(pnl_krw)
            pnl_st = self.pnl_guard.status_dict()
            logger.info(
                f"[미국장 PnL] 💱 ${pnl_usd:+.2f} × {fx:.0f} = {pnl_krw:+,.0f}원 | "
                f"일일실현={pnl_st['realized_pnl']:+,.0f}원 | "
                f"최고={pnl_st['peak_pnl']:+,.0f}원 | "
                f"상태={pnl_st['state']}"
            )

            # ── ★ 재진입 차단 등록 (SELL 체결 완료 직후) ──────
            # 부분 익절은 포지션 유지이므로 전량 매도일 때만 등록
            if action_tag == "SELL":
                _is_sl = _is_stoploss_reason(reason)
                self.reentry.record_sell(
                    market     = "US",
                    code       = symbol,
                    name       = name,
                    reason     = reason,
                    is_stoploss= _is_sl,
                )

            # ── [US 훅 K] SELL lifecycle + PendingRegistry (Phase 4 US Pipeline) ──
            # ★ rt_cd=0 은 접수 성공. SELL lifecycle을 ACCEPTED로 등록하고
            # FillObserver 폴링 대상으로 PendingRegistry에 추가한다.
            if _US_LIFECYCLE_ENABLED and self._us_lifecycle_mgr is not None:
                try:
                    _us_sell_lc_id = make_order_lifecycle_id("US", "SELL", symbol)
                    _us_sell_lc = self._us_lifecycle_mgr.create(
                        trade_id      = _us_sell_trade_id or _us_sell_lc_id,
                        market        = "US",
                        code          = symbol,
                        side          = "SELL",
                        strategy_name = "USStrategyManager",
                        order_qty     = qty,
                    )
                    self._us_pending_sell_meta[_us_sell_lc.order_lifecycle_id] = {
                        "code":      symbol,
                        "name":      name,
                        "qty":       qty,
                        "price":     cur_price,
                        "avg_price": avg_p,
                        "trade_id":  _us_sell_trade_id,
                        "reason":    reason,
                        "is_full":   not is_partial,
                    }
                    self._us_register_pending_order(
                        symbol         = symbol,
                        side           = "SELL",
                        order_qty      = qty,
                        order_response = result,
                        lifecycle_id   = _us_sell_lc.order_lifecycle_id,
                        excd           = excd,
                        trade_id       = _us_sell_trade_id,
                    )
                    logger.info(
                        "[US SELL ACCEPTED] lifecycle+PendingRegistry 등록 완료: "
                        "order_lifecycle_id=%s symbol=%s qty=%s",
                        _us_sell_lc.order_lifecycle_id, symbol, qty,
                    )
                except Exception as _us_sell_le:
                    logger.warning("[US SELL Lifecycle] 등록 오류: %s", _us_sell_le)

            return {
                "action":      action_tag,
                "symbol":      symbol, "name": name, "excd": excd,
                "price":       cur_price, "qty": qty,
                "pnl_usd":     round(pnl_usd, 2), "pnl_pct": round(pnl_pct, 2),
                "pnl_krw":     round(pnl_krw, 0),
                "reason":      reason, "session": sess["session"], "currency": "USD",
                "realized_pnl": pnl_st["realized_pnl"],
                "peak_pnl":    pnl_st["peak_pnl"],
                "pnl_state":   pnl_st["state"],
            }
        # ★ 매도 실패 시 — '가능수량보다 큽니다' 오류 = KIS에 실제 잔고 없음
        # → 유령 포지션으로 판단하고 봇 포지션에서도 제거
        fail_msg = result.get('msg1', '매도실패')
        # ── [US 훅 L] SELL_ORDER_REJECTED ──
        if _US_JOURNAL_ENABLED:
            try:
                _us_jnl.record_sell_order_rejected(
                    _us_sell_trade_id, "US", symbol,
                    rt_cd = result.get("rt_cd", "?"),
                    msg1  = fail_msg,
                )
            except Exception as _uje:
                _us_jnl._inc_error("us_sell_rejected", _uje)
        if '가능수량' in fail_msg or '수량' in fail_msg:
            if symbol in self.pos_mgr.positions:
                self.pos_mgr.remove(symbol)
                logger.warning(
                    f"[유령포지션제거] {name}({symbol}) KIS잔고 없음({fail_msg}) "
                    f"→ 봇 포지션 자동 삭제"
                )
        return {"action": "SELL_FAIL", "symbol": symbol, "name": name,
                "reason": fail_msg, "session": sess["session"]}

    def _buy_result(self, symbol, name, excd, price, qty, level,
                    sess, iv, entry_reason, tag) -> dict:
        return {
            "action":       "BUY",
            "symbol":       symbol, "name": name, "excd": excd,
            "price":        price,  "qty": qty,
            "amount_usd":   round(price * qty, 2),
            "level":        level,
            "buy_score":    iv["buy_score"],
            "intraday_pct": iv["intraday_pct"],
            "vol_ratio":    iv["vol_ratio"],
            "vwap":         iv["vwap"],
            "above_vwap":   iv["above_vwap"],
            "ema_bull":     iv["ema_bull"],
            "rsi":          iv["rsi"],
            "pullback":     iv["pullback_breakout"],
            "reason":       f"{entry_reason} {tag}".strip(),
            "session":      sess["session"],
            "currency":     "USD",
        }

    def sync_from_balance(self):
        """서버 시작 시 KIS 잔고 기반 포지션 복원"""
        try:
            bal = self.api.get_us_balance()
            for h in bal.get("holdings", []):
                sym = h["symbol"]
                if sym not in self.pos_mgr.positions:
                    pos = USPosition(sym, h.get("name", sym), h.get("excd", "NASD"),
                                     h["qty"], h["avg_price"])
                    self.pos_mgr.add(pos)
                    logger.info(f"🔄 US포지션복원: {sym} {h['qty']}주 ${h['avg_price']:.2f}")
        except Exception as e:
            logger.warning(f"US포지션 동기화 실패: {e}")
