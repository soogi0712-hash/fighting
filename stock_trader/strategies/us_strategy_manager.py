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
from datetime import datetime, timedelta, timezone as _tz
from utils.logger             import get_logger

_UTC = _tz.utc
from utils.market_session     import (
    us_session_info, is_us_tradeable,
    get_us_trading_phase, us_phase_info,
    US_PHASE_PRIME, US_PHASE_NEUTRAL, US_PHASE_CONSERVATIVE,
    us_trading_session_id, us_session_phase,
)
from strategies.daily_pnl_guard import DailyPnLGuard
from strategies.reentry_guard   import ReentryGuard, _is_stoploss_reason
from utils.order_sizing        import finalize_order_qty, qty_from_cash
# ── US 손실회복 트레일링 + 복원 포지션 정합화(순수 로직) + 원자 저장소 ──
import strategies.us_recovery      as USR
import strategies.us_reconcile     as USRC
import strategies.us_bars          as USBARS
from strategies.us_position_store  import AtomicPositionStore, CorruptStoreError

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
    from phoenix.lifecycle import (
        OrderLifecycleManager, make_order_lifecycle_id, LifecycleState,
    )
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
    # ★ 안전-해제(차단 해제 허용) 증거 = '해소가 확실한' 단말만.
    #   FILLED   : 체결 완료(주문 소멸).
    #   CANCELLED: 명시적 취소(외부 취소 감지 또는 운영자 감사기반 수동 해제).
    #   EXPIRED/REJECTED 는 US 체결조회의 한계상 '주문 없음' 을 증명하지 못하므로
    #   (poll 재시도 소진 등 불명확) 자동 해제 증거로 쓰지 않는다 → 차단 유지.
    _US_RESOLVED_PENDING = frozenset({
        _USPendingStatus.FILLED, _USPendingStatus.CANCELLED,
    })
except Exception as _us_foe:
    _US_FILL_OBSERVER_ENABLED = False
    _US_RESOLVED_PENDING = frozenset()
    import logging as _us_fo_logging
    _us_fo_logging.getLogger("USStrategy").warning(
        f"[US FillObserver] import 실패 — fill observer 비활성화: {_us_foe}"
    )

logger = get_logger("USStrategy")

# ── US 주문 등록 결과 상태(단일 계약) ────────────────────────────────
# ★ 이 함수는 KIS rt_cd=0(접수 성공) 이후에만 호출된다. 즉 '외부 증권사에 주문이
#   접수됐을 수 있는' 상태이므로, 로컬 후처리 실패를 이유로 절대 REJECTED·취소·
#   meta 제거(재주문 허용)를 하지 않는다. 모든 결과는 차단(재주문 금지)을 유지한다.
#   REGISTERED      : ODNO 확보 + registry ACCEPTED + lifecycle ACCEPTED (완전 정합).
#   PENDING_CONFIRM : ODNO 존재(외부 주문 존재 가능)하나 로컬 등록/전이 일부 실패.
#                     ODNO/symbol/side/qty/price/submitted_at 을 durable 확인대기
#                     원장(registry ACCEPTED row / lifecycle odno)에 보존. 차단 유지,
#                     절대 종말화 안 함.
#   UNKNOWN_CONFIRM : rt_cd=0 이나 ODNO 미수신·불명확. lifecycle→ORDER_SUBMITTED durable.
#                     차단 유지(재주문 금지).
_US_REG_REGISTERED      = "REGISTERED"
_US_REG_PENDING_CONFIRM = "PENDING_CONFIRM"
_US_REG_UNKNOWN_CONFIRM = "UNKNOWN_CONFIRM"

# ── KIS 주문 응답 분류 결과(제출-의도 finalize) ────────────────────────
#   ACCEPTED        : ODNO 수신 → 외부 접수 확정.
#   UNKNOWN_CONFIRM : rt_cd=0·ODNO 미수신 / rt_cd=9(예외 래핑) / 알 수 없는 코드 /
#                     ODNO 존재 rt_cd≠0 등 '접수 가능하나 불명확' → 확인대기(재주문 금지).
#   REJECTED        : 명확 거절(ODNO 없음 + 정상 응답 + 예외/타임아웃/5xx/parse 아님 +
#                     allowlist 코드/메시지) → 안전 종말화(차단 해제 가능).
#   NOT_SENT        : dry-run/live-disabled 등 실제 미전송 → 차단 불필요(로컬 취소).
_US_OUTCOME_ACCEPTED        = "ACCEPTED"
_US_OUTCOME_UNKNOWN_CONFIRM = "UNKNOWN_CONFIRM"
_US_OUTCOME_REJECTED        = "REJECTED"
_US_OUTCOME_NOT_SENT        = "NOT_SENT"

# 명확 거절로 인정할 KIS msg_cd allowlist(정상 응답·ODNO 없음일 때만 적용).
#   불확실한 코드는 포함하지 않는다(미포함 = UNKNOWN_CONFIRM 로 안전 확인대기).
_US_CLEAR_REJECT_CODES = frozenset({
    "APBK0918",  # 매수가능금액(예수금) 부족
    "APBK0919",  # 매도가능수량 부족
    "APBK1664",  # 주문가능수량 초과
    "APBK0656",  # 해당 종목 거래불가/종목정보 없음
    "IGW00027",  # 장 운영시간 아님
})
# 명확 거절 메시지 substring allowlist(정상 응답·ODNO 없음일 때만 적용).
#   ODNO 가 없고 rt_cd 가 0/9 도 아닌 '정상 거절 응답' 에서만 평가되므로, 잔액/
#   수량 부족·거래불가 등 '주문 미체결이 확정' 되는 문구를 폭넓게 포함한다(안전:
#   해당 응답엔 살아 있는 주문이 없다). 미포함 코드/문구는 UNKNOWN_CONFIRM.
_US_CLEAR_REJECT_MSGS = (
    "매도가능수량", "매수가능금액", "주문가능수량", "가능수량", "주문가능",
    "잔고부족", "예수금", "부족", "초과", "금액", "한도",
    "거래불가", "거래정지", "종목정보", "해당종목", "해당 종목",
    "장운영", "장 운영", "장종료", "장 종료",
)

US_POSITIONS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "us_positions.json"
)

# ★ 관리모드 SELL '명확 거절(clear-reject)' 후 재제출 쿨다운(초). 같은 루프/직후 루프의
#   무한 재시도를 막는다. timeout/500/불명확(UNKNOWN_CONFIRM)은 EXIT_PENDING 유지라
#   쿨다운 대상이 아니다(이미 재제출 차단).
US_SELL_REJECT_COOLDOWN_SEC = 60

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
        "ema9": 0.0, "ema9_prev": 0.0, "ema9_rising": False,
        "ema21": 0.0, "ema50": 0.0, "ema_bull": False, "atr_pct": 0.0,
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
    _ema9_series = _ema_series(closes, 9)
    ema9  = float(_ema9_series[-1])
    ema9_prev = float(_ema9_series[-2]) if len(_ema9_series) >= 2 else ema9
    ema9_rising = bool(ema9 > ema9_prev)        # EMA9 상승 기울기
    ema21 = float(_ema_series(closes, 21)[-1])  if len(closes) >= 21 else ema9
    ema50 = float(_ema_series(closes, 50)[-1])  if len(closes) >= 50 else ema21
    ema_bull = (cur > ema9 > ema21 > ema50)     # 완전 정배열

    # ── ④-b ATR(14) → 현재가 대비 % (동적 트레일 폭 산정용) ──────
    #   TR = max(high-low, |high-prevClose|, |low-prevClose|). ATR = 최근 14 TR 평균.
    atr_pct = 0.0
    if len(closes) >= 15:
        _pc = closes[:-1]
        _h  = highs[1:]
        _l  = lows[1:]
        _tr = np.maximum.reduce([
            _h - _l,
            np.abs(_h - _pc),
            np.abs(_l - _pc),
        ])
        _atr = float(np.mean(_tr[-14:])) if len(_tr) >= 14 else float(np.mean(_tr))
        atr_pct = (_atr / cur * 100.0) if cur > 0 else 0.0

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
        "ema9_prev":         round(ema9_prev, 4),
        "ema9_rising":       ema9_rising,
        "ema21":             round(ema21, 4),
        "ema50":             round(ema50, 4),
        "ema_bull":          ema_bull,
        "atr_pct":           round(atr_pct, 4),
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
    def __init__(self, symbol, name, excd, qty, avg_price, recovered: bool = False):
        self.symbol        = symbol
        self.name          = name
        self.excd          = excd
        self.qty           = qty
        self.avg_price     = avg_price
        self.highest_price = avg_price
        self.current_level = 1
        self.created_at    = datetime.now().isoformat()
        self.trade_id: str = ""   # ★ journal 연결용 (재시작 후 매수-매도 연결 유지)
        # ── 관리 상태(손실회복·복원 트레일링) — us_recovery 스키마(단일 진실원) ──
        #   management_mode / recovered / highest_price / recovery_* / profit_* /
        #   last_evaluated_at / exit_pending_ref 를 보관. 원자 저장으로 영속된다.
        self.mgmt: dict = USR.default_state(
            recovered=recovered, highest_price=avg_price
        )

    # ── 관리 상태 편의 접근자 ────────────────────────────────
    @property
    def recovered(self) -> bool:
        return bool(self.mgmt.get("recovered"))

    @recovered.setter
    def recovered(self, v: bool):
        self.mgmt["recovered"] = bool(v)

    @property
    def management_mode(self) -> str:
        return self.mgmt.get("management_mode", USR.MODE_NORMAL)

    # ── 격리(broker-absent quarantine) ──────────────────────
    @property
    def is_quarantined(self) -> bool:
        return bool(self.mgmt.get("quarantined"))

    def quarantine(self, reason: str, snapshot_id: str, now_iso: str):
        """완전 KIS 스냅샷에서 broker 부재로 판정 → 격리(삭제하지 않고 감사정보만).
        기록: symbol, quarantined_at, reason, snapshot_id (그 외 원문 없음)."""
        self.mgmt["quarantined"] = True
        self.mgmt["quarantine"] = {
            "symbol": self.symbol, "quarantined_at": now_iso,
            "reason": reason, "snapshot_id": snapshot_id,
        }

    def clear_quarantine(self):
        """KIS 잔고에 재등장 → 즉시 정상 복구(격리 해제)."""
        self.mgmt["quarantined"] = False
        self.mgmt["quarantine"] = None

    def sync_mgmt_high(self):
        """highest_price 단일 진실원: pos.highest_price ↔ mgmt['highest_price'] 를
        max 로 양방향 동기화(절대 하락 없음 — 반복 복원·재시작 안전)."""
        h = max(float(self.highest_price or 0.0),
                float(self.mgmt.get("highest_price") or 0.0))
        self.highest_price = h
        self.mgmt["highest_price"] = h

    def update_high(self, price: float):
        if price > self.highest_price:
            self.highest_price = price
        # 관리 상태의 최고가도 함께(절대 낮아지지 않음)
        USR.bump_highest_price(self.mgmt, price)

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
            "mgmt":          dict(self.mgmt),  # ★ 관리 상태 영속(§3)
        }


class USPositionManager:
    def __init__(self):
        self.positions: dict[str, USPosition] = {}
        os.makedirs(os.path.dirname(US_POSITIONS_FILE), exist_ok=True)
        # ★ 원자 저장소(temp+fsync+os.replace, .bak 폴백, 손상안전) — §5/§9D
        self._store = AtomicPositionStore(US_POSITIONS_FILE)
        self._load()

    def _load(self):
        try:
            data = self._store.load()   # 본→.bak 폴백. 없으면 {}. 둘 다 손상이면 예외.
        except CorruptStoreError as e:
            # ★ 본·백업 모두 손상 → 빈 dict 로 덮어써 '전체 삭제'하는 사고를 막는다.
            #   기존 self.positions 를 그대로 유지(비우지 않음).
            logger.error(f"[US포지션] 본·백업 모두 손상 — 로드 보류(전체삭제 방지): {e}")
            return
        for sym, d in data.items():
            try:
                p = USPosition(sym, d["name"], d["excd"], d["qty"], d["avg_price"])
                p.highest_price = d.get("highest_price", d["avg_price"])
                p.current_level = d.get("current_level", 1)
                p.created_at    = d.get("created_at", "")
                p.trade_id      = d.get("trade_id", "")  # ★ 하위호환
                # ★ 관리 상태 병합(구버전 JSON=mgmt 없음 → 안전 기본값). highest 는 하락 금지.
                p.mgmt = USR.merge_state(d.get("mgmt"))
                p.sync_mgmt_high()
                self.positions[sym] = p
            except Exception as e:
                logger.warning(f"[US포지션] {sym} 복원 실패(건너뜀): {e}")
        logger.info(f"[US포지션] {len(self.positions)}개 로드")

    def save(self):
        try:
            # 동시 mutation(정합화 잡) 과 경쟁해도 안전하게 스냅샷 후 직렬화
            data = {sym: p.to_dict() for sym, p in self._snapshot()}
            self._store.save(data)   # 원자적(temp+fsync+os.replace) + .bak 보존
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

    # ── 격리 인지 뷰(매도판정·집계·중복매수 판정은 active 만 사용) ──────────
    def _snapshot(self) -> list:
        """positions.items() 스냅샷 — 저빈도 정합화 잡의 동시 mutation 과 경쟁해도
        'dictionary changed size during iteration' 로 죽지 않도록 방어 복사(재시도)."""
        for _ in range(3):
            try:
                return list(self.positions.items())
            except RuntimeError:
                continue
        return list(dict(self.positions).items())

    def active_positions(self) -> dict:
        """격리(BROKER_ABSENT_QUARANTINED)되지 않은 '실제 보유' 포지션만."""
        return {s: p for s, p in self._snapshot() if not p.is_quarantined}

    def quarantined_positions(self) -> dict:
        """격리된 포지션(매도·판정·집계 제외, 감사·재등장 복구용)."""
        return {s: p for s, p in self._snapshot() if p.is_quarantined}

    def quarantine_audit(self) -> list:
        """격리 감사정보 리스트(symbol/quarantined_at/reason/snapshot_id만; PII 없음)."""
        out = []
        for _s, p in self._snapshot():
            if p.is_quarantined and p.mgmt.get("quarantine"):
                out.append(dict(p.mgmt["quarantine"]))
        return out


# ════════════════════════════════════════════════════════════
# ── 주문별 반영(부킹) 완료 누적 체결 워터마크 영속화
# ════════════════════════════════════════════════════════════

_OUTBOX_COLS = (
    "event_key", "oid", "cum_qty", "cum_cost", "delta", "delta_avg",
    "side", "symbol", "name", "excd", "reason",
    "pos_qty_before", "pnl_krw", "closed", "session_id",
    "pos_done", "pnl_done", "reentry_done", "event_done", "app_done",
    "created_at",
)


class _USFillOutbox:
    """crash-safe 체결 execution ledger (outbox).

    ★ 고유키 event_key = f"{oid}:{cum_qty}" — 주문번호 + 누적체결수량으로
      각 체결 구간(delta)을 1행으로 영속화한다.
    ★ 처리할 delta event 를 부수효과 적용 '전에' 먼저 영속화한다(write-ahead).
    ★ 각 부수효과(pos/pnl/reentry/event/app)의 처리 상태를 개별 플래그로
      영속 기록한다.
    ★ 재시작 시 미완료(플래그 0) 부수효과만 재처리하고, 완료된 것은 다시
      실행하지 않는다. 단일 프로세스 rollback 에 의존하지 않는다.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        try:
            with self._conn() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS us_fill_outbox (
                        event_key      TEXT PRIMARY KEY,
                        oid            TEXT NOT NULL,
                        cum_qty        INTEGER NOT NULL,
                        cum_cost       REAL    NOT NULL,
                        delta          INTEGER NOT NULL,
                        delta_avg      REAL    NOT NULL,
                        side           TEXT NOT NULL,
                        symbol         TEXT NOT NULL,
                        name           TEXT, excd TEXT, reason TEXT,
                        pos_qty_before INTEGER NOT NULL DEFAULT 0,
                        pnl_krw        REAL    NOT NULL DEFAULT 0,
                        closed         INTEGER NOT NULL DEFAULT 0,
                        session_id     TEXT,
                        pos_done       INTEGER NOT NULL DEFAULT 0,
                        pnl_done       INTEGER NOT NULL DEFAULT 0,
                        reentry_done   INTEGER NOT NULL DEFAULT 0,
                        event_done     INTEGER NOT NULL DEFAULT 0,
                        app_done       INTEGER NOT NULL DEFAULT 0,
                        created_at     TEXT
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS ix_outbox_oid "
                          "ON us_fill_outbox(oid)")
        except Exception as exc:
            logger.warning("[US Outbox] 초기화 실패: %s", exc)

    @staticmethod
    def event_key(oid: str, cum_qty: int) -> str:
        return f"{oid}:{int(cum_qty)}"

    def last_cum(self, oid: str):
        """oid 의 마지막 기록 누적(수량, 원가). 없으면 (0, 0.0). delta 계산 기준."""
        with self._conn() as c:
            row = c.execute(
                "SELECT cum_qty, cum_cost FROM us_fill_outbox "
                "WHERE oid=? ORDER BY cum_qty DESC LIMIT 1", (oid,)).fetchone()
        if row:
            return int(row["cum_qty"] or 0), float(row["cum_cost"] or 0.0)
        return 0, 0.0

    def insert_if_absent(self, row: dict) -> None:
        """delta event 를 영속화(write-ahead). 이미 있으면 무시(재처리)."""
        from datetime import datetime as _dt
        row = dict(row)
        row.setdefault("created_at", _dt.now().isoformat())
        cols = ",".join(_OUTBOX_COLS)
        ph   = ",".join([":" + k for k in _OUTBOX_COLS])
        with self._conn() as c:
            c.execute(
                f"INSERT OR IGNORE INTO us_fill_outbox ({cols}) VALUES ({ph})",
                {k: row.get(k) for k in _OUTBOX_COLS})

    def get(self, event_key: str):
        with self._conn() as c:
            row = c.execute("SELECT * FROM us_fill_outbox WHERE event_key=?",
                            (event_key,)).fetchone()
        return dict(row) if row else None

    def set_flag(self, event_key: str, flag: str) -> None:
        assert flag in ("pos_done", "pnl_done", "reentry_done",
                        "event_done", "app_done")
        with self._conn() as c:
            c.execute(f"UPDATE us_fill_outbox SET {flag}=1 WHERE event_key=?",
                      (event_key,))

    def all_rows(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM us_fill_outbox ORDER BY oid, cum_qty").fetchall()
        return [dict(r) for r in rows]

    def pending_app_rows(self):
        """fill_event 생성 완료(event_done=1) & app 미처리(app_done=0) 행."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM us_fill_outbox "
                "WHERE event_done=1 AND app_done=0 ORDER BY oid, cum_qty").fetchall()
        return [dict(r) for r in rows]


class _USAppEffectLedger:
    """app 측 부수효과의 (event_key, effect_type) 단위 완료 상태를 영속화.

    ★ 고유키 = (event_key, effect_type) UNIQUE.
    ★ effect 별로 완료를 개별 기록한다(app_done 하나로 묶지 않음). 한 effect
      성공 후 다음 effect 에서 실패해도 성공한 effect 는 재실행되지 않는다.
    ★ 재전달돼도 done() 인 effect 는 실행하지 않는다.
    ★ 외부 함수 실행과 flag 저장을 단일 트랜잭션으로 묶을 수 없으므로, 각 effect
      함수는 event_key 를 받아 스스로 멱등하게 처리하는 것을 계약으로 한다
      (실행 후 flag 저장 전 crash 시에도 재실행이 무해).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        try:
            with self._conn() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS us_app_effects (
                        event_key   TEXT NOT NULL,
                        effect_type TEXT NOT NULL,
                        session_id  TEXT,
                        amount      REAL NOT NULL DEFAULT 0,
                        done_at     TEXT,
                        PRIMARY KEY (event_key, effect_type)
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS ix_appfx_type_session "
                          "ON us_app_effects(effect_type, session_id)")
        except Exception as exc:
            logger.warning("[US AppEffect] 초기화 실패: %s", exc)

    def done(self, event_key: str, effect_type: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM us_app_effects WHERE event_key=? AND effect_type=?",
                (event_key, effect_type)).fetchone()
        return row is not None

    def mark(self, event_key: str, effect_type: str,
             session_id: str = "", amount: float = 0.0) -> None:
        """(event_key, effect_type) 를 UNIQUE 키로 1회만 기록(INSERT OR IGNORE).
        session_id/amount 는 세션별 집계(거래건수·PnL) 재구성용."""
        from datetime import datetime as _dt
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO us_app_effects "
                "(event_key, effect_type, session_id, amount, done_at) "
                "VALUES (?,?,?,?,?)",
                (event_key, effect_type, session_id, float(amount),
                 _dt.now().isoformat()))

    def count(self, effect_type: str, session_id: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM us_app_effects "
                "WHERE effect_type=? AND session_id=?",
                (effect_type, session_id)).fetchone()
        return int(row[0] or 0) if row else 0

    def sum_amount(self, effect_type: str, session_id: str) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(amount),0) FROM us_app_effects "
                "WHERE effect_type=? AND session_id=?",
                (effect_type, session_id)).fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def done_types(self, event_key: str) -> set:
        with self._conn() as c:
            rows = c.execute(
                "SELECT effect_type FROM us_app_effects WHERE event_key=?",
                (event_key,)).fetchall()
        return {r[0] for r in rows}


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

    # ── ★ 종목별 최대 허용손실(USD) — 신규 매수 위험기반 사이징(§5). 고정 -5/-6%
    #   손절 제거로 손실 위험거리가 넓어졌으므로, 이 한도 ÷ (현재가×구조적 위험거리)
    #   로 신규 수량을 축소한다. 환경변수 US_MAX_LOSS_PER_SYMBOL_USD 로 조정 가능.
    US_MAX_LOSS_PER_SYMBOL_USD = float(
        os.environ.get("US_MAX_LOSS_PER_SYMBOL_USD", "60") or 60)

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

        # ── US 실보유 정합화(§4~§7) 상태 ─────────────────────────
        #   buy_gate_ok=False 면 '이번 스캔의 신규 BUY만' 스킵(기존 SELL/체결감시 계속).
        #   ★ 기본 False: 완전·권위 스냅샷으로 복원+격리를 1회 마치기 전까지 신규 BUY
        #     차단(startup 정합화 실패/미실행 시 첫 BUY 차단 — §7). 성공 시 True 로 승격.
        import threading as _th
        self._us_reconcile_lock   = _th.Lock()   # US 전용 비재진입 락(중복 실행 방지)
        self._us_buy_gate_ok      = False
        self._us_buy_gate_reason  = "awaiting_first_authoritative_complete_reconcile"
        self._us_snapshot_seq     = 0            # snapshot_id 생성용 시퀀스
        self._us_reconcile_health = {
            "ran": False, "last_run_at": None, "authoritative": None,
            "buy_allowed": False, "complete": None, "authoritative_empty": None,
            "restored": 0, "reconciled": 0, "quarantined": 0, "unquarantined": 0,
            "stale_deleted": 0, "quarantined_total": 0, "broker_count": 0,
            "internal_count": 0, "active_internal_count": 0,
            "snapshot_id": None, "reason": "not_run_yet", "error": None,
        }
        # ★ 격리 포지션의 '최종 삭제'는 운영자 승인 + 완전 스냅샷일 때만 허용(기본 비활성).
        #   미승인 시에는 삭제하지 않고 BROKER_ABSENT_QUARANTINED 로 격리만 한다(감사 유지).
        self._us_allow_stale_delete = bool(
            os.environ.get("US_RECONCILE_ALLOW_STALE_DELETE", "").lower()
            in ("1", "true", "yes", "on")
        )

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
        self._us_outbox          = None
        self._us_app_ledger      = None
        self._us_pending_registry = None
        self._us_fill_observer   = None
        self._us_pending_buy_meta: dict  = {}
        self._us_pending_sell_meta: dict = {}
        # ★ 체결(FILLED) 시 app.py 측 부수효과(거래집계/워치엔트리/on_sell_complete)를
        #   구동하기 위한 이벤트 큐. run_us_fill_poll() 반환에 실려 드레인된다.
        self._us_fill_events: list = []

        if _US_LIFECYCLE_ENABLED:
            try:
                _jnl_db = os.path.join(
                    os.path.dirname(__file__), "..", "data", "trading_journal.db"
                )
                self._us_lifecycle_mgr = OrderLifecycleManager(_jnl_db)
                # ★ crash-safe 체결 outbox(execution ledger) + app 부수효과 원장
                self._us_outbox = _USFillOutbox(_jnl_db)
                self._us_app_ledger = _USAppEffectLedger(_jnl_db)
                logger.info("[US Lifecycle] OrderLifecycleManager 초기화 완료")
            except Exception as _us_le:
                logger.warning(f"[US Lifecycle] 초기화 실패: {_us_le}")
                self._us_lifecycle_mgr = None
                self._us_outbox = None
                self._us_app_ledger = None

        if _US_FILL_OBSERVER_ENABLED:
            try:
                self._us_pending_registry = _USPendingOrderRegistry()
                self._us_fill_observer    = _USFillObserver(
                    kis_api         = kis_api,
                    phoenix_db_path = None,
                )
                logger.info("[US FillObserver] PendingOrderRegistry + FillObserver 초기화 완료")
                self._us_restore_pending_meta()
                # ★ 재시작: outbox 의 미완료 부수효과만 재처리(이중부킹 없음)
                self._us_replay_outbox()
            except Exception as _us_foe2:
                logger.warning(f"[US FillObserver] 초기화 실패: {_us_foe2}")
                self._us_pending_registry = None
                self._us_fill_observer    = None

    def set_realtime_cache(self, cache: dict):
        self._rt_cache = cache or {}

    # ══════════════════════════════════════════════════════════
    # Phase 4: US Pipeline — lifecycle 콜백 + PendingRegistry
    # ══════════════════════════════════════════════════════════

    # ── 포지션 저수준 헬퍼 (idempotent 적용에 사용) ──
    def _us_pos_add(self, symbol, name, excd, delta, delta_avg, level=1):
        existing = self.pos_mgr.positions.get(symbol)
        # ★ 격리 포지션은 '실보유 아님' → 매수 체결 시 격리를 해제하고 신규 보유로 취급
        #   (격리 qty 에 더하지 않는다; broker 부재였으므로 0에서 시작).
        if existing is not None and existing.is_quarantined:
            existing.clear_quarantine()
            existing.qty = int(delta)
            existing.avg_price = float(delta_avg)
            existing.current_level = level
            existing.recovered = False   # 신규 매수분 — 기존 익절/손절 정책 적용
            self.pos_mgr.save()
            return
        if existing is not None:
            new_qty = existing.qty + int(delta)
            new_avg = ((existing.avg_price * existing.qty + delta_avg * delta) / new_qty
                       if new_qty > 0 else delta_avg)
            self.pos_mgr.update(symbol, new_qty, new_avg,
                                max(existing.current_level, level))
        else:
            pos = USPosition(symbol, name, excd or "NASD", int(delta), float(delta_avg))
            pos.current_level = level
            self.pos_mgr.add(pos)

    def _us_pos_reduce(self, symbol, delta):
        pos = self.pos_mgr.positions.get(symbol)
        if pos is None:
            return
        remaining = pos.qty - int(delta)
        if remaining > 0:
            self.pos_mgr.update(symbol, remaining, pos.avg_price, pos.current_level)
        else:
            self.pos_mgr.remove(symbol)

    def _us_apply_fill_delta(self, lc) -> None:
        """체결(부분/전량)을 outbox 에 write-ahead 로 기록하고 부수효과를 적용한다.
        (엔트리: us_dispatch_fill 이 lifecycle 전이 후 호출)

        delta     = cumulative_filled_qty − outbox 마지막 누적(last_cum)
        delta_avg = (누적원가 − 이전 누적원가) / delta   ← 단계별 평균체결가
        cum_qty ≤ last_cum 이면 신규 체결분 없음(중복 폴링/재시작 재조회 무시).
        """
        ob = getattr(self, "_us_outbox", None)
        if ob is None:
            return
        oid  = lc.order_lifecycle_id
        side = (getattr(lc, "side", "") or "").upper()
        symbol  = lc.code
        cum_qty = int(lc.filled_qty or 0)
        cum_avg = float(lc.avg_fill_price or 0.0)
        if cum_qty <= 0 or cum_avg <= 0:
            return

        prev_cum, prev_cost = ob.last_cum(oid)
        if cum_qty <= prev_cum:
            return   # 신규 체결분 없음
        delta      = cum_qty - prev_cum
        cum_cost   = cum_qty * cum_avg
        delta_cost = cum_cost - prev_cost
        delta_avg  = (delta_cost / delta) if delta > 0 else cum_avg
        if delta_avg <= 0:
            delta_avg = cum_avg

        meta = (self._us_pending_buy_meta if side == "BUY"
                else self._us_pending_sell_meta).get(oid, {})
        name   = meta.get("name", symbol)
        excd   = meta.get("excd", "NASD")
        reason = meta.get("reason", "")

        pos = self.pos_mgr.positions.get(symbol)
        pos_qty_before = int(pos.qty) if pos is not None else 0

        # 매도 실현손익/청산여부 사전 계산(원가 = 감소 전 평단) — outbox 에 영속
        pnl_krw, closed = 0.0, 0
        if side == "SELL":
            prev_avg = pos.avg_price if pos is not None else delta_avg
            sell_qty = min(int(delta), pos_qty_before) if pos is not None else int(delta)
            try:
                fx = self.api.get_usd_exchange_rate() or 1350.0
            except Exception:
                fx = 1350.0
            pnl_krw = (delta_avg - prev_avg) * sell_qty * fx
            closed  = 1 if (pos is not None and pos_qty_before - sell_qty <= 0) else 0

        # ★ US 거래세션 귀속: 주문 접수시각(lc.accepted_at, 영속) 기준으로 계산해
        #   부분체결이 KST 자정/세션 경계를 넘어도 동일 주문의 모든 delta 가 같은
        #   세션에 귀속되게 한다. 재시작해도 lc 에서 동일하게 재계산된다.
        _anchor = (getattr(lc, "accepted_at", None)
                   or getattr(lc, "submitted_at", None)
                   or getattr(lc, "created_at", None))
        try:
            session_id = us_trading_session_id(_anchor)
        except Exception:
            session_id = us_trading_session_id(None)

        # ── write-ahead: 부수효과 적용 전에 event 를 먼저 영속화 ──
        ek = ob.event_key(oid, cum_qty)
        ob.insert_if_absent({
            "event_key": ek, "oid": oid, "cum_qty": cum_qty, "cum_cost": cum_cost,
            "delta": int(delta), "delta_avg": float(delta_avg), "side": side,
            "symbol": symbol, "name": name, "excd": excd, "reason": reason,
            "pos_qty_before": pos_qty_before, "pnl_krw": float(pnl_krw),
            "closed": int(closed), "session_id": session_id,
            "pos_done": 0, "pnl_done": 0, "reentry_done": 0,
            "event_done": 0, "app_done": 0,
        })
        row = ob.get(ek)
        if row is not None:
            self._us_process_outbox_row(row)

        # 주문 완전 종료(누적 == 주문수량) 시 meta 정리
        if cum_qty >= int(getattr(lc, "order_qty", 0) or 0):
            (self._us_pending_buy_meta if side == "BUY"
             else self._us_pending_sell_meta).pop(oid, None)

    def _us_process_outbox_row(self, row: dict) -> None:
        """outbox 1행의 미완료 부수효과만 idempotent 하게 적용하고, 각 효과 적용
        직후 해당 플래그를 영속 기록한다(effect→flag). 재시작 후 재처리해도
        - 포지션: pos_qty_before 스냅샷 vs 현재 수량 비교로 이중 반영 차단
        - 재진입: reentry.check 로 이미 등록됐으면 재등록 안 함
        """
        ob     = self._us_outbox
        ek     = row["event_key"]
        side   = row["side"]
        symbol = row["symbol"]
        delta  = int(row["delta"])
        davg   = float(row["delta_avg"])
        before = int(row["pos_qty_before"])

        if side == "BUY":
            if not row["pos_done"]:
                cur = self.pos_mgr.positions.get(symbol)
                cur_qty = cur.qty if cur is not None else 0
                if cur is not None and cur_qty == before + delta:
                    pass   # 이미 반영됨(crash after pos, before flag) → 재적용 안 함
                else:
                    self._us_pos_add(symbol, row["name"], row["excd"], delta, davg)
                ob.set_flag(ek, "pos_done")
            if not row["event_done"]:
                self._us_fill_events.append({
                    "side": "BUY", "symbol": symbol, "name": row["name"],
                    "qty": delta, "price": davg, "event_key": ek})
                ob.set_flag(ek, "event_done")
        else:   # SELL
            if not row["pos_done"]:
                cur = self.pos_mgr.positions.get(symbol)
                cur_qty = cur.qty if cur is not None else 0
                expected_after = max(0, before - delta)
                if cur is None and expected_after == 0:
                    pass   # 이미 전량청산됨
                elif cur is not None and cur_qty == expected_after:
                    pass   # 이미 반영됨
                else:
                    self._us_pos_reduce(symbol, delta)
                ob.set_flag(ek, "pos_done")
            if not row["pnl_done"]:
                self.pnl_guard.record(float(row["pnl_krw"]))
                ob.set_flag(ek, "pnl_done")
            if row["closed"] and not row["reentry_done"]:
                already = False
                try:
                    blocked, _info = self.reentry.check("US", symbol, row["name"])
                    already = bool(blocked)
                except Exception:
                    already = False
                if not already:
                    self.reentry.record_sell(
                        market="US", code=symbol, name=row["name"],
                        reason=row["reason"],
                        is_stoploss=_is_stoploss_reason(row["reason"]))
                ob.set_flag(ek, "reentry_done")
            if not row["event_done"]:
                self._us_fill_events.append({
                    "side": "SELL", "symbol": symbol, "name": row["name"],
                    "qty": delta, "price": davg,
                    "pnl_krw": float(row["pnl_krw"]),
                    "is_full": bool(row["closed"]), "event_key": ek})
                ob.set_flag(ek, "event_done")

    def _us_replay_outbox(self) -> None:
        """재시작: outbox 를 권위로 (1) 완료 매도의 실현손익을 (재시작 시 0 이 된)
        pnl_guard 에 재구성하고 (2) 미완료 부수효과 행만 재처리한다.
        이미 완료된 부수효과는 다시 실행하지 않는다."""
        ob = getattr(self, "_us_outbox", None)
        if ob is None:
            return
        try:
            rows = ob.all_rows()
        except Exception as exc:
            logger.warning("[US Outbox] replay 로드 실패: %s", exc)
            return
        # ★ 손실한도(DailyPnLGuard)는 '현재 US 거래세션' 스코프로만 재구성한다.
        #   KST 자정이 아니라 세션 경계에서만 초기화되며, 재시작해도 현재 세션의
        #   실현손익이 그대로 복원된다(이전 세션 rows 는 합산 제외).
        cur_sess = us_trading_session_id(None)
        rebuilt = 0.0
        for row in rows:
            if (row["side"] == "SELL" and row["pnl_done"]
                    and (row.get("session_id") or "") == cur_sess):
                try:
                    self.pnl_guard.record(float(row["pnl_krw"]))
                    rebuilt += float(row["pnl_krw"])
                except Exception:
                    pass
            # (2) 미완료 부수효과 재처리
            done = (row["pos_done"] and row["event_done"] and
                    (row["side"] == "BUY" or
                     (row["pnl_done"] and (not row["closed"] or row["reentry_done"]))))
            if not done:
                self._us_process_outbox_row(row)
        if rebuilt:
            logger.info("[US Outbox] 재시작 현세션(%s) 실현손익 재구성: ₩%.0f",
                        cur_sess, rebuilt)

    def _us_meta_is_stale(self, oid: str) -> bool:
        """meta 항목(oid=lifecycle_id)이 로컬 durable 증거상 '해소가 확실'한지 판정.

        ★ req5/6/8: has_active_sell 이 '단순 내부 ACTIVE 플래그' 만 보고 영구 차단
          하지 않도록 차단 전에 증거를 대사하되, **해제는 오직 확실한 해소 증거로만**
          허용한다. KIS 미체결조회(US 는 신뢰 불가)를 쓰지 않고 로컬 durable 상태만
          사용하며, 해제 허용 증거는 다음뿐이다:
            (a) OrderLifecycle 이 FILLED 또는 CANCELLED
            (b) PendingRegistry row 가 FILLED 또는 CANCELLED
          FILLED=체결완료(주문 소멸), CANCELLED=명시적 취소(외부 취소 감지 또는
          운영자 감사기반 수동 해제)만 안전 해제로 본다.

        ⚠️ EXPIRED/REJECTED 는 '주문이 없음'을 증명하지 못하므로(US 체결조회 한계·
          poll 재시도 소진 등 불명확) 해제 증거로 쓰지 않는다 → 차단 유지. 조회
          실패·미확정도 False → **차단 유지**(req6/8: 확인 전 중복 제출 금지, 단순
          경과에 의한 자동 해제 금지).
        """
        s = LifecycleState
        # (a) lifecycle 이 FILLED/CANCELLED?
        mgr = getattr(self, "_us_lifecycle_mgr", None)
        if mgr is not None:
            try:
                lc = mgr.load(oid)
                if lc is not None and lc.current_state in (s.FILLED, s.CANCELLED):
                    return True
            except Exception:
                pass
        # (b) registry row 가 FILLED/CANCELLED?
        reg = getattr(self, "_us_pending_registry", None)
        if reg is not None and _US_FILL_OBSERVER_ENABLED:
            try:
                row = reg.get_by_trade_id(oid)
                if row is not None and row.get("status") in _US_RESOLVED_PENDING:
                    return True
            except Exception:
                pass
        return False

    def _us_has_active_order(self, symbol: str, side: str = None) -> bool:
        """동일 종목(선택적으로 동일 방향)에 ACTIVE(접수/부분체결) US 주문이
        있으면 True. 접수 시 pending meta 에 등록되고 FILLED 시 pop 되므로,
        meta 존재 = 미체결 주문 존재. 중복 제출(이중 매수/매도) 차단용.
        재시작 후에도 _us_restore_pending_meta 가 meta 를 복원한다.

        ★ req5: 차단 전에 각 meta 항목을 로컬 durable 증거로 대사한다. 이미
          terminal 로 확정된 ghost 는 meta 에서 제거하고 차단하지 않는다(영구
          차단 방지). 상태가 불명확한 항목은 유지 → 차단(중복 제출 금지).
        """
        _side = (side or "").upper()
        metas = []
        if _side in ("", "BUY"):
            metas.append(self._us_pending_buy_meta)
        if _side in ("", "SELL"):
            metas.append(self._us_pending_sell_meta)
        for m in metas:
            for oid, meta in list(m.items()):
                if meta.get("code") != symbol:
                    continue
                # 로컬 증거로 이미 단말이면 ghost → 제거 후 계속(차단 안 함)
                if self._us_meta_is_stale(oid):
                    m.pop(oid, None)
                    logger.info(
                        "[US in-flight] ghost meta 해소(로컬 단말 증거): "
                        "symbol=%s side=%s lifecycle_id=%s → 차단 해제",
                        symbol, meta.get("code"), oid)
                    continue
                return True
        return False

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
        """US KIS 주문 응답(rt_cd=0)에서 ODNO 추출 + PendingRegistry 등록 + lifecycle 전이.

        ★★ 이 함수는 **KIS rt_cd=0(접수 성공) 이후에만** 호출된다. 즉 외부 증권사에
           주문이 접수됐을 수 있는 상태다. 따라서 **로컬 등록/전이 실패를 이유로
           registry REJECTED·lifecycle 취소·meta 제거(재주문 허용)를 절대 하지 않는다.**
           모든 경로가 차단(재주문 금지)을 유지하고, 실주문 정보(ODNO 등)를 durable
           확인대기 원장에 보존한다.

        US 응답 구조: result["output"]["ODNO"].

        Returns (단일 계약 — 모두 '차단 유지'):
            _US_REG_REGISTERED      — ODNO 확보 + registry ACCEPTED + lifecycle ACCEPTED.
            _US_REG_PENDING_CONFIRM — ODNO 존재(외부 주문 존재 가능)하나 로컬 등록/전이
                                      일부 실패. durable(registry ACCEPTED row 또는
                                      lifecycle odno)에 보존, 절대 종말화 안 함.
            _US_REG_UNKNOWN_CONFIRM — rt_cd=0 이나 ODNO 미수신·불명확. lifecycle→
                                      ORDER_SUBMITTED durable. 차단 유지.
        """
        if self._us_pending_registry is None or self._us_lifecycle_mgr is None:
            # lifecycle/registry 미가용 → 안전하게 차단 유지(확인대기).
            return _US_REG_UNKNOWN_CONFIRM

        output = order_response.get("output", {}) or {}
        if isinstance(output, list):
            output = output[0] if output else {}
        odno = str(output.get("ODNO", "") or output.get("odno", "") or "").strip()

        # ── ODNO 없음(rt_cd=0 이나 미수신 = 불명확 성공) ────────────────
        #   register(odno='') 는 거부되므로 pending row 를 만들지 않는다. 대신
        #   lifecycle 을 ORDER_SUBMITTED 로 전이해 '제출됨·확인대기' 를 durable 로
        #   남긴다(재시작 후에도 미제출과 구분). 즉시 재주문 금지.
        if not odno:
            try:
                lc = self._us_lifecycle_mgr.load(lifecycle_id)
                if lc is not None:
                    s = LifecycleState
                    if lc.current_state == s.UNKNOWN:
                        self._us_lifecycle_mgr.confirm_signal(lc)
                    if lc.current_state == s.SIGNAL_CONFIRMED:
                        self._us_lifecycle_mgr.submit(lc, trade_id)
            except Exception as _hexc:
                logger.error(
                    "[US PendingRegistry] UNKNOWN_CONFIRM 전이 실패(차단 유지): "
                    "lifecycle_id=%s error=%s", lifecycle_id, _hexc)
            logger.error(
                "[US PendingRegistry] ODNO 미수신(불명확 성공) → UNKNOWN_CONFIRM"
                "(확인대기): symbol=%s side=%s lifecycle_id=%s — 즉시 재주문 금지",
                symbol, side, lifecycle_id)
            return _US_REG_UNKNOWN_CONFIRM

        # ── ODNO 존재 = 외부 주문 존재 가능 → 어떤 로컬 실패에도 종말화·해제 금지 ──
        from datetime import datetime as _dt
        submitted_at = _dt.now().isoformat()

        # 1) durable 확인대기 원장(registry ACCEPTED row) 에 실주문 정보 우선 저장.
        #    ODNO/symbol/side/qty/submitted_at/raw(가격 포함) 가 여기 영속된다(req3).
        _registered = False
        try:
            _row = self._us_pending_registry.register(
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
            _registered = bool(_row)
        except Exception as _rexc:
            logger.error(
                "[US PendingRegistry] register 예외(차단 유지, 종말화 안 함): "
                "symbol=%s odno=%r error=%s", symbol, odno, _rexc)

        # 2) lifecycle 에 ODNO 를 durable 로 반영(정상 전이 → ORDER_ACCEPTED).
        #    실패해도 절대 롤백/취소하지 않는다(외부 주문 존재 가능).
        _lc_ok = False
        try:
            lc = self._us_lifecycle_mgr.load(lifecycle_id)
            if lc is not None:
                self._us_lifecycle_mgr.advance_to_accepted(
                    lc, client_order_id=trade_id, odno=odno)
                _lc_ok = (lc.current_state == LifecycleState.ORDER_ACCEPTED)
        except Exception as _lexc:
            logger.error(
                "[US PendingRegistry] lifecycle 전이 예외(차단 유지, 롤백 안 함): "
                "symbol=%s odno=%r lifecycle_id=%s error=%s",
                symbol, odno, lifecycle_id, _lexc)

        if _registered and _lc_ok:
            logger.info(
                "[US PendingRegistry] 등록 완료: symbol=%s side=%s odno=%r "
                "lifecycle_id=%s state=ORDER_ACCEPTED", symbol, side, odno,
                lifecycle_id)
            return _US_REG_REGISTERED

        # 3) 부분 실패 → 외부 주문 보존(PENDING_CONFIRM). durable 근거 최종 확인.
        _durable = _registered
        if not _durable:
            # registry 저장 실패 → lifecycle 에라도 odno 가 영속됐는지 확인(재시작
            #   정합화가 이 odno 로 복원·차단한다). 그것도 없으면 fail-safe:
            #   in-memory meta(호출부 유지)만으로 차단하고 CRITICAL 로 수동확인 요청.
            try:
                _lc2 = self._us_lifecycle_mgr.load(lifecycle_id)
                _durable = bool(_lc2 is not None
                                and str(getattr(_lc2, "odno", "") or "").strip())
            except Exception:
                _durable = False
        if _durable:
            logger.error(
                "[US PendingRegistry] 부분 실패 → PENDING_CONFIRM(외부 주문 보존, "
                "차단 유지): symbol=%s side=%s odno=%r registry=%s lifecycle_odno=%s",
                symbol, side, odno, _registered, (not _registered))
        else:
            logger.critical(
                "[US PendingRegistry] ★durable 저장 전부 실패★ — 외부 주문(odno=%r) "
                "존재 가능. in-memory 차단만 유지되므로 재시작 시 유실 위험. 운영자 "
                "KIS 미체결 확인 필요: symbol=%s side=%s qty=%s lifecycle_id=%s",
                odno, symbol, side, order_qty, lifecycle_id)
        return _US_REG_PENDING_CONFIRM

    # ── 제출-의도(submit-intent) 크래시 안전 파이프라인 ────────────────
    def _us_classify_order_outcome(self, result: dict) -> tuple[str, str]:
        """KIS 주문 응답을 분류한다. 반환: (outcome, odno).

        ★ rt_cd≠0 전체를 명확 거절로 취급하지 않는다. 명확 거절은 (ODNO 없음 +
          정상 응답 수신 + 예외/타임아웃/HTTP5xx/parse 아님 + allowlist 코드·메시지)
          를 모두 충족할 때만 인정한다. rt_cd=9(예외 래핑)·알 수 없는 코드·ODNO
          존재 응답은 모두 UNKNOWN_CONFIRM(확인대기, 재주문 금지)으로 분류한다.
        """
        if not isinstance(result, dict):
            return _US_OUTCOME_UNKNOWN_CONFIRM, ""   # parse 불가 → 확인대기
        # dry-run / live-disabled(실주문 미전송) → 차단 불필요
        if result.get("_dry_run") or result.get("_live_disabled"):
            return _US_OUTCOME_NOT_SENT, ""
        output = result.get("output", {}) or {}
        if isinstance(output, list):
            output = output[0] if output else {}
        odno = str(output.get("ODNO", "") or output.get("odno", "") or "").strip()
        rt_cd = str(result.get("rt_cd", "") or "").strip()

        if odno:
            # ODNO 존재 → rt_cd 와 무관하게 외부 접수 확정
            return _US_OUTCOME_ACCEPTED, odno
        if rt_cd == "0":
            # rt_cd=0 이나 ODNO 미수신 → 불명확
            return _US_OUTCOME_UNKNOWN_CONFIRM, ""
        if rt_cd == "9":
            # 예외 래핑(timeout/network/HTTP5xx/parse) → 불명확
            return _US_OUTCOME_UNKNOWN_CONFIRM, ""
        # rt_cd≠0, ODNO 없음 → 명확 거절 allowlist 인지 검사
        msg_cd = str(result.get("msg_cd", "") or "").strip().upper()
        msg1   = str(result.get("msg1", "") or "")
        if msg_cd in _US_CLEAR_REJECT_CODES or any(
                kw in msg1 for kw in _US_CLEAR_REJECT_MSGS):
            return _US_OUTCOME_REJECTED, ""
        # 알 수 없는 코드 → 보수적으로 확인대기(재주문 금지)
        return _US_OUTCOME_UNKNOWN_CONFIRM, ""

    def _us_begin_submit_intent(
        self, intent_id: str, symbol: str, side: str, order_qty: int,
        price: float, excd: str = "", trade_id: str = "",
        meta_extra: dict = None,
    ) -> bool:
        """★ KIS 주문 API 호출 '전' 에 durable submit-intent 를 저장한다(crash 안전).

        저장 성공 시에만 True 를 반환하며, 호출부는 True 일 때만 KIS 주문을 낸다.
        순서(모두 docker volume ./data:/app/data 의 SQLite 에 영속):
          1) PendingRegistry.register_intent → status=PENDING_SUBMIT(odno='') 저장.
             (이게 실패하면 False → 주문 미제출)
          2) OrderLifecycle create → ORDER_SUBMITTED 로 durable 전이(재시작 정합화용).
          3) in-memory 차단 meta 등록(동일 세션 중복 주문 차단).
        """
        if self._us_lifecycle_mgr is None or self._us_pending_registry is None:
            logger.critical(
                "[US SubmitIntent] lifecycle/registry 미가용 → 주문 미제출(안전): "
                "symbol=%s side=%s", symbol, side)
            return False
        from datetime import datetime as _dt
        submitted_at = _dt.now().isoformat()

        # 1) durable submit-intent (필수) — 실패 시 KIS 호출 금지
        try:
            _row = self._us_pending_registry.register_intent(
                market="US", trade_id=intent_id, code=symbol, side=side,
                order_qty=order_qty, price=price, submitted_at=submitted_at,
                client_order_id=trade_id, exchange=excd or None, currency="USD")
        except Exception as exc:
            logger.critical(
                "[US SubmitIntent] durable 저장 예외 → 주문 미제출(안전): "
                "symbol=%s side=%s error=%s", symbol, side, exc)
            return False
        if not _row:
            logger.critical(
                "[US SubmitIntent] durable 저장 실패(row 0) → 주문 미제출(안전): "
                "symbol=%s side=%s", symbol, side)
            return False

        # 2) lifecycle → ORDER_SUBMITTED (durable, 재시작 정합화 앵커)
        try:
            lc = self._us_lifecycle_mgr.create(
                trade_id=trade_id or intent_id, market="US", code=symbol,
                side=side, strategy_name="USStrategyManager",
                order_qty=order_qty, order_lifecycle_id=intent_id)
            s = LifecycleState
            if lc.current_state == s.UNKNOWN:
                self._us_lifecycle_mgr.confirm_signal(lc)
            if lc.current_state == s.SIGNAL_CONFIRMED:
                self._us_lifecycle_mgr.submit(lc, trade_id)
        except Exception as exc:
            logger.error(
                "[US SubmitIntent] lifecycle 전이 예외(registry intent 로 차단 유지): "
                "symbol=%s error=%s", symbol, exc)

        # 3) in-memory 차단 meta
        meta = {"code": symbol, "name": symbol, "qty": order_qty,
                "price": price, "avg_price": price, "excd": excd,
                "trade_id": trade_id, "reason": "us_submit_intent"}
        if meta_extra:
            meta.update(meta_extra)
        (self._us_pending_buy_meta if side == "BUY"
         else self._us_pending_sell_meta)[intent_id] = meta
        logger.info(
            "[US SubmitIntent] 저장 완료 → KIS 호출 허용: symbol=%s side=%s qty=%s "
            "intent_id=%s", symbol, side, order_qty, intent_id)
        return True

    def _us_finalize_submit_intent(
        self, intent_id: str, symbol: str, side: str, order_qty: int,
        result: dict, excd: str = "", trade_id: str = "",
    ) -> str:
        """KIS 응답 분류 → durable submit-intent 를 확정 전이. 반환: outcome.

        ACCEPTED/UNKNOWN_CONFIRM → 차단 유지(재주문 금지). REJECTED(명확 거절)/
        NOT_SENT(미전송) → 안전하게 차단 해제. 어떤 경로에서도 ODNO 보유분을
        로컬 취소하지 않는다(cancel_local_only 가 self-guard).
        """
        outcome, odno = self._us_classify_order_outcome(result)
        reg = self._us_pending_registry
        mgr = self._us_lifecycle_mgr
        meta_map = (self._us_pending_buy_meta if side == "BUY"
                    else self._us_pending_sell_meta)

        if outcome == _US_OUTCOME_ACCEPTED:
            # PENDING_SUBMIT → ACCEPTED(odno) 승격 + lifecycle accept. 차단 유지.
            try:
                if reg is not None:
                    reg.update_odno(intent_id, odno)
                    reg.set_status(intent_id, _USPendingStatus.ACCEPTED)
            except Exception as exc:
                logger.error("[US SubmitIntent] ACCEPTED 승격 실패(차단 유지): %s", exc)
            try:
                if mgr is not None:
                    lc = mgr.load(intent_id)
                    if lc is not None and not lc.is_terminal:
                        mgr.advance_to_accepted(
                            lc, client_order_id=trade_id, odno=odno)
            except Exception as exc:
                logger.error("[US SubmitIntent] lifecycle accept 실패(차단 유지): %s", exc)
            logger.info(
                "[US SubmitIntent] ACCEPTED: symbol=%s side=%s odno=%r intent_id=%s",
                symbol, side, odno, intent_id)
            return outcome

        if outcome == _US_OUTCOME_UNKNOWN_CONFIRM:
            # 불명확 → UNKNOWN_CONFIRM 영속. lifecycle 은 ORDER_SUBMITTED 유지. 차단 유지.
            try:
                if reg is not None:
                    reg.set_status(intent_id, _USPendingStatus.UNKNOWN_CONFIRM)
            except Exception as exc:
                logger.error("[US SubmitIntent] UNKNOWN_CONFIRM 마킹 실패(차단 유지): %s", exc)
            logger.error(
                "[US SubmitIntent] UNKNOWN_CONFIRM(확인대기·재주문 금지): symbol=%s "
                "side=%s rt_cd=%s msg=%s", symbol, side,
                result.get("rt_cd"), result.get("msg1"))
            return outcome

        if outcome == _US_OUTCOME_REJECTED:
            # 명확 거절(ODNO 없음·정상 응답·allowlist) → 안전 종말화 + 차단 해제.
            try:
                if reg is not None:
                    reg.set_status(intent_id, _USPendingStatus.REJECTED)
            except Exception as exc:
                logger.error("[US SubmitIntent] REJECTED 마킹 실패: %s", exc)
            try:
                if mgr is not None:
                    lc = mgr.load(intent_id)
                    if lc is not None and not lc.is_terminal:
                        mgr.reject(lc, reason=f"KIS 명확거절: {result.get('msg1')}")
            except Exception as exc:
                logger.error("[US SubmitIntent] lifecycle reject 실패: %s", exc)
            meta_map.pop(intent_id, None)
            logger.info(
                "[US SubmitIntent] REJECTED(명확 거절) → 차단 해제: symbol=%s side=%s "
                "msg=%s", symbol, side, result.get("msg1"))
            return outcome

        # NOT_SENT(dry-run/live-disabled): 실제 미전송 → 로컬 취소 + 차단 해제.
        try:
            if reg is not None:
                reg.set_status(intent_id, _USPendingStatus.CANCELLED)
        except Exception as exc:
            logger.error("[US SubmitIntent] NOT_SENT 취소 마킹 실패: %s", exc)
        try:
            if mgr is not None:
                lc = mgr.load(intent_id)
                if lc is not None:
                    mgr.cancel_local_only(
                        lc, reason="not sent (dry-run/live-disabled)")
        except Exception as exc:
            logger.error("[US SubmitIntent] NOT_SENT lifecycle 취소 실패: %s", exc)
        meta_map.pop(intent_id, None)
        logger.warning(
            "[US SubmitIntent] NOT_SENT(미전송) → 차단 해제: symbol=%s side=%s",
            symbol, side)
        return outcome

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
            # 1) lifecycle 상태 전이 (누적 filled_qty/avg_fill_price 갱신).
            #    부킹은 on_filled 콜백이 아니라 delta 기반으로 처리하므로
            #    on_filled 은 전달하지 않는다(부분체결도 반영하기 위함).
            if is_full:
                self._us_lifecycle_mgr.full_fill(
                    lc, delta=filled_qty, avg_price=avg_fill_price)
            else:
                self._us_lifecycle_mgr.partial_fill(lc, filled_qty, avg_fill_price)
        except Exception as exc:
            logger.error(
                "[US dispatch_fill] lifecycle 전이 오류: "
                "order_lifecycle_id=%s is_full=%s error=%s",
                order_lifecycle_id, is_full, exc,
            )
            raise

        # 2) 갱신된 누적 체결 기준으로 신규 체결분(delta)만 포지션·손익에 반영.
        #    PARTIALLY_FILLED / FILLED 모두 여기서 처리된다(멱등·이중부킹 방지).
        lc2 = self._us_lifecycle_mgr.load(order_lifecycle_id) or lc
        self._us_apply_fill_delta(lc2)

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

        # ★ app.py 측 부수효과(거래집계/워치엔트리/on_sell_complete)는 outbox 의
        #   미처리(event_done=1 & app_done=0) 행에서 가져온다. app.py 가 처리 후
        #   us_mark_app_done(event_key) 로 확정 → 재시작해도 미처리분만 재전달(1회).
        fill_events = self._us_pending_app_events()
        # 인메모리 미러는 비운다(로그/단위테스트용; 권위는 outbox)
        self._us_fill_events = []

        return {
            "total":     poll_result.get("total",     0),
            "filled":    poll_result.get("filled",    0),
            "partial":   poll_result.get("partial",   0),
            "no_change": poll_result.get("no_change", 0),
            "errors":    poll_result.get("errors",    0),
            "dispatched": dispatched,
            "fill_events": fill_events,
        }

    def _us_pending_app_events(self) -> list:
        """outbox 의 event_done=1 & app_done=0 행 → app 처리 대기 체결이벤트."""
        ob = getattr(self, "_us_outbox", None)
        if ob is None:
            return []
        try:
            rows = ob.pending_app_rows()
        except Exception:
            return []
        out = []
        for r in rows:
            _ct = r.get("created_at") or ""
            out.append({
                "side": r["side"], "symbol": r["symbol"], "name": r["name"],
                "qty": int(r["delta"]), "price": float(r["delta_avg"]),
                "pnl_krw": float(r["pnl_krw"]), "is_full": bool(r["closed"]),
                "event_key": r["event_key"],
                # ★ 주문 identity(부분체결 delta 무관, 주문별 1회 집계용)
                "order_key": f"{r['oid']}:{r['side']}",
                # ★ US 거래세션 귀속(KST 날짜 아님) — 자정 넘어도 동일 세션
                "session_id": r.get("session_id") or "",
                # ★ 결정론적 event 시각(외부효과 멱등·재구성용, now() 아님)
                "event_time": _ct,
            })
        return out

    def _us_external_effect_types(self, event: dict) -> list:
        """이 event 의 외부(비집계) 부수효과 목록."""
        if event.get("side") == "BUY":
            return ["watch_entry"]
        if event.get("side") == "SELL" and event.get("is_full"):
            return ["on_sell_complete"]
        return []

    def _us_effect_types(self, event: dict) -> list:
        """이 event 의 전체 effect 목록(집계 + 외부)."""
        agg = ["trade_count"] + (["pnl_stats"] if event.get("side") == "SELL" else [])
        return agg + self._us_external_effect_types(event)

    def us_apply_app_effects(self, event: dict, external_fns: dict = None) -> bool:
        """체결이벤트의 app 부수효과를 (event_key, effect_type) UNIQUE 원장으로
        정확히 1회 적용한다.

        - 집계 effect(trade_count/pnl_stats): 원장에 (day, amount) 로 INSERT OR
          IGNORE 만 한다. 별도 인메모리 카운터가 없으므로 crash 창이 없고,
          당일 거래건수·PnL 은 원장에서 COUNT/SUM 으로 '매번 결정론적 재구성'된다.
        - 외부 effect(watch_entry/on_sell_complete): 원장 done 이면 skip, 아니면
          fn(event_key, event) 실행 후 mark. fn 은 event 의 결정론적 시각/키로
          멱등 처리(재실행해도 쿨다운 만료·집계 불변)하는 것을 계약으로 한다.
        - 한 effect 실패 시 그것만 미완료로 남기고 이후 보류 → 다음 폴 재처리.
        - 모든 effect 완료 후에만 outbox app_done 확정.
        반환: 모든 effect 완료면 True.
        """
        ledger = getattr(self, "_us_app_ledger", None)
        ek  = (event or {}).get("event_key", "")
        if ledger is None or not ek:
            return False
        sess = event.get("session_id", "") or ""
        side = event.get("side")
        # ★ 거래건수는 delta(event_key)별이 아니라 '주문(order_key)별 1회'만 기록.
        #   3→5→10 부분체결이라도 주문 1개 = trade_count 1. 미체결 취소·거부는
        #   체결 event 자체가 없어 0. PnL 은 delta 별 SUM 유지(event_key 사용).
        order_key = event.get("order_key") or ek

        # 1) 집계 effect — 원장 자체가 상태(UNIQUE, 세션별 재구성 가능)
        ledger.mark(order_key, "trade_count", sess, 1.0)     # 주문별 1회
        if side == "SELL":
            ledger.mark(ek, "pnl_stats", sess, float(event.get("pnl_krw", 0.0)))

        # 2) 외부 effect — gate + 멱등 fn
        ok = True
        for et in self._us_external_effect_types(event):
            if ledger.done(ek, et):
                continue
            fn = (external_fns or {}).get(et)
            try:
                if fn is not None:
                    fn(ek, event)
            except Exception as exc:
                logger.error("[US app effect] %s/%s 실패 — 다음 폴 재시도: %s",
                             ek, et, exc)
                ok = False
                break
            ledger.mark(ek, et, sess, 0.0)

        # 3) 모든 effect 완료 시에만 app_done 확정 (effect 별 키 사용)
        def _effect_key(et):
            return order_key if et == "trade_count" else ek
        effects = self._us_effect_types(event)
        if ok and all(ledger.done(_effect_key(et), et) for et in effects):
            ob = getattr(self, "_us_outbox", None)
            if ob is not None:
                try:
                    ob.set_flag(ek, "app_done")
                except Exception as exc:
                    logger.warning("[US Outbox] app_done 마킹 실패: %s error=%s",
                                   ek, exc)
            return True
        return False

    def us_current_session_id(self):
        """지금 시각 기준 미국 거래세션 식별자(ET 거래일)."""
        return us_trading_session_id(None)

    def us_session_trade_count(self, session_id: str) -> int:
        """세션(session_id) US 거래건수 — effect 원장에서 결정론적 재구성.
        주문별 1회(order_key) 집계 → 부분체결 과다집계 없음. 재시작 불변."""
        ledger = getattr(self, "_us_app_ledger", None)
        return ledger.count("trade_count", session_id) if ledger else 0

    def us_session_realized_krw(self, session_id: str) -> float:
        """세션(session_id) US 실현손익(KRW) — delta 별 SUM(pnl_stats)."""
        ledger = getattr(self, "_us_app_ledger", None)
        return ledger.sum_amount("pnl_stats", session_id) if ledger else 0.0

    def us_mark_app_done(self, event_key: str) -> None:
        """(호환용) 모든 부수효과 완료를 전제로 outbox app_done 확정."""
        ob = getattr(self, "_us_outbox", None)
        if ob is not None and event_key:
            try:
                ob.set_flag(event_key, "app_done")
            except Exception as exc:
                logger.warning("[US Outbox] app_done 마킹 실패: %s error=%s",
                               event_key, exc)

    def _us_restore_pending_meta(self) -> None:
        """재시작 후 US OrderLifecycle 정합화 + in-flight pending meta 복원(req10).

        load_all_active() 는 비단말 lifecycle(UNKNOWN 포함)을 모두 반환한다. 사고
        residue 는 'PendingRegistry row 는 ACCEPTED(실 ODNO 보유)인데 lifecycle 은
        전이 예외로 UNKNOWN(lc.odno='') 에 멈춘' 이중 불일치이므로, **lifecycle 뿐
        아니라 registry row 도 함께 대사**해 다음과 같이 정합화한다:

          - lifecycle/registry 가 FILLED/CANCELLED/REJECTED(해소 확정): 차단 미복원.
            (REJECTED 은 명확 거절 = 주문 없음. 신규 경로에서 REJECTED 는 오직
             finalize 의 명확거절 분류로만 기록되므로 자동 해제가 안전하다.)
          - registry row 가 PENDING_SUBMIT/UNKNOWN_CONFIRM/EXPIRED: 접수 가능하나
            불명확 → **차단 유지**(복원). odno 없으므로 lifecycle 은 그대로 둔다.
            (PENDING_SUBMIT = KIS 호출 직후 crash 등으로 finalize 미도달한 앵커 →
             동일 symbol/side 재주문 차단.)
          - ORDER_ACCEPTED / PARTIALLY_FILLED, 또는 odno 근거 존재: in-flight/사고
            ghost → odno 로 advance_to_accepted 하여 lifecycle↔registry 일치 후 복원.
          - ORDER_SUBMITTED 이나 odno 근거 無(단, registry PENDING_SUBMIT/UNKNOWN_CONFIRM
            /EXPIRED): 영속 확인대기 → 차단 유지(재주문 금지).
          - UNKNOWN/SIGNAL_CONFIRMED 이고 odno·registry 근거 全無: orphan →
            cancel_local_only(로컬 CANCELLED)·차단 미복원. ※ odno 보유분은
            cancel_local_only 가 자체 거부하므로 종말화되지 않는다.
        """
        if self._us_lifecycle_mgr is None:
            return
        try:
            active_lifecycles = self._us_lifecycle_mgr.load_all_active()
        except Exception as exc:
            logger.warning("[US RestoreMeta] load_all_active 실패: %s", exc)
            return

        s = LifecycleState
        _PRE_ACCEPTED = (s.UNKNOWN, s.SIGNAL_CONFIRMED, s.ORDER_SUBMITTED)
        _ACTIVE_INFLIGHT = (s.ORDER_ACCEPTED, s.PARTIALLY_FILLED)
        reg = getattr(self, "_us_pending_registry", None)

        restored_buy = restored_sell = 0
        advanced = expired = held = 0
        for lc in active_lifecycles:
            if (lc.market or "").upper() != "US":
                continue
            try:
                if lc.current_state in (s.FILLED, s.CANCELLED):
                    continue   # 해소 확정 → 복원 대상 아님
                if lc.is_terminal:
                    # EXPIRED/REJECTED lifecycle: 불명확 → 아래에서 차단 유지 처리
                    pass
            except Exception:
                pass
            lc_id = lc.order_lifecycle_id
            side  = (lc.side or "BUY").upper()
            state = lc.current_state

            # registry row 대사(권위: 실 ODNO·상태). trade_id == lifecycle_id.
            row_status = ""
            row_odno   = ""
            if reg is not None:
                try:
                    row = reg.get_by_trade_id(lc_id)
                    if row is not None:
                        row_status = row.get("status") or ""
                        row_odno   = str(row.get("odno") or "").strip()
                except Exception:
                    pass
            odno = str(getattr(lc, "odno", "") or "").strip() or row_odno

            # registry 가 해소 확정(FILLED/CANCELLED/REJECTED) → 차단 미복원.
            if (row_status in _US_RESOLVED_PENDING
                    or row_status == _USPendingStatus.REJECTED):
                logger.info(
                    "[US RestoreMeta] registry 해소(%s) → 차단 미복원: "
                    "symbol=%s side=%s", row_status, lc.code, side)
                continue
            # registry 불명확(PENDING_SUBMIT/UNKNOWN_CONFIRM/EXPIRED) → 차단 유지.
            _row_blocking = row_status in (
                _USPendingStatus.PENDING_SUBMIT, _USPendingStatus.UNKNOWN_CONFIRM,
                _USPendingStatus.ACCEPTED, _USPendingStatus.PARTIALLY_FILLED,
                _USPendingStatus.EXPIRED)
            if row_status in (_USPendingStatus.PENDING_SUBMIT,
                              _USPendingStatus.UNKNOWN_CONFIRM,
                              _USPendingStatus.EXPIRED):
                logger.warning(
                    "[US RestoreMeta] registry 불명확(%s) → 차단 유지(자동 해제 금지): "
                    "symbol=%s side=%s odno=%r", row_status, lc.code, side, odno)

            # pre-accepted 정합화
            if state in _PRE_ACCEPTED:
                if odno:
                    # odno 근거 존재(사고 ghost 등) → ACCEPTED 로 일치. odno 없는
                    #   PENDING_SUBMIT/UNKNOWN_CONFIRM 은 여기 오지 않는다(아래 held).
                    try:
                        self._us_lifecycle_mgr.advance_to_accepted(lc, odno=odno)
                        advanced += 1
                        logger.info(
                            "[US RestoreMeta] pre-accepted ghost 정합화: "
                            "symbol=%s side=%s odno=%r row=%s %s→%s",
                            lc.code, side, odno, row_status or "-", state.name,
                            lc.current_state.name)
                    except Exception as _adv:
                        logger.error(
                            "[US RestoreMeta] 정합화 전이 실패(차단 유지): "
                            "lifecycle_id=%s error=%s", lc_id, _adv)
                        # 전이 실패해도 차단 meta 는 복원(보수적: 중복 방지)
                elif _row_blocking or state == s.ORDER_SUBMITTED:
                    # PENDING_SUBMIT/UNKNOWN_CONFIRM/EXPIRED 또는 제출됨(odno無) →
                    #   영속 확인대기. lifecycle 그대로, 차단 meta 복원(재주문 금지).
                    held += 1
                    logger.info(
                        "[US RestoreMeta] 확인대기 복원(row=%s state=%s): "
                        "symbol=%s side=%s → 차단 유지(재주문 금지)",
                        row_status or "-", state.name, lc.code, side)
                else:
                    # UNKNOWN/SIGNAL_CONFIRMED + 근거 全無 → 미제출 orphan.
                    #   cancel_local_only 는 odno 보유 시 자체 거부하므로 안전.
                    try:
                        _res = self._us_lifecycle_mgr.cancel_local_only(
                            lc, reason="restart: unsubmitted orphan, no odno")
                    except Exception:
                        _res = None
                    if _res == s.CANCELLED:
                        expired += 1
                        logger.info(
                            "[US RestoreMeta] 미제출 orphan(근거 全無) 정리: "
                            "symbol=%s side=%s state=%s → CANCELLED(차단 미복원)",
                            lc.code, side, state.name)
                        continue
                    # 취소가 거부됨(예: odno 보유) → 안전측: 차단 유지(복원)
                    logger.warning(
                        "[US RestoreMeta] orphan 취소 거부 → 차단 유지: "
                        "symbol=%s side=%s state=%s", lc.code, side, state.name)
            elif state not in _ACTIVE_INFLIGHT:
                # 예상 밖 상태 → 보수적으로 차단 유지(복원)하되 로그
                logger.warning(
                    "[US RestoreMeta] 예상 밖 상태(차단 복원): "
                    "symbol=%s side=%s state=%s", lc.code, side, state.name)

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

        if restored_buy or restored_sell or advanced or expired or held:
            logger.info(
                "[US RestoreMeta] 재시작 정합화 완료: BUY=%d SELL=%d "
                "정합화(advanced)=%d HOLD복원(held)=%d orphan정리(cancelled)=%d",
                restored_buy, restored_sell, advanced, held, expired,
            )

    def us_list_pending_confirm(self) -> list[dict]:
        """운영자 확인용: 현재 '확인대기(차단 중)' US 주문 목록을 반환한다.

        자동 해제가 불가능한(=US 체결조회로 '주문 없음'을 증명 못 하는) 주문들을
        운영자가 KIS 앱에서 실제 미체결 여부를 확인한 뒤 us_manual_release_pending
        으로 수동 해제할 수 있도록, 차단 근거를 종목·side·odno·상태와 함께 제공한다.
        """
        out: list[dict] = []
        mgr = getattr(self, "_us_lifecycle_mgr", None)
        reg = getattr(self, "_us_pending_registry", None)
        for side, meta_map in (("BUY", self._us_pending_buy_meta),
                               ("SELL", self._us_pending_sell_meta)):
            for lc_id, meta in list(meta_map.items()):
                odno = ""
                lc_state = ""
                row_status = ""
                if mgr is not None:
                    try:
                        lc = mgr.load(lc_id)
                        if lc is not None:
                            odno = str(getattr(lc, "odno", "") or "").strip()
                            lc_state = lc.current_state.name
                    except Exception:
                        pass
                if reg is not None and _US_FILL_OBSERVER_ENABLED:
                    try:
                        row = reg.get_by_trade_id(lc_id)
                        if row is not None:
                            row_status = row.get("status") or ""
                            odno = odno or str(row.get("odno") or "").strip()
                    except Exception:
                        pass
                out.append({
                    "lifecycle_id": lc_id, "symbol": meta.get("code"),
                    "side": side, "qty": meta.get("qty"),
                    "price": meta.get("price"), "odno": odno,
                    "lifecycle_state": lc_state, "registry_status": row_status,
                })
        return out

    def us_manual_release_pending(
        self, lifecycle_id: str, operator: str, reason: str,
        verified_no_open_order: bool = False,
    ) -> dict:
        """운영자 수동 해제(안전 절차·감사기록 필수) — req6.

        US 는 KIS 미체결조회로 '주문 없음'을 자동 증명할 수 없으므로, 확인대기로
        차단된 주문은 자동 해제하지 않는다. 운영자가 **KIS 앱에서 해당 주문이
        미체결로 존재하지 않음(취소/소멸)을 직접 확인**한 뒤에만 이 함수로 해제한다.

        요구사항(모두 필수, 미충족 시 거부):
          - operator: 해제 수행자 식별자(비어 있으면 거부).
          - reason:   해제 사유(비어 있으면 거부).
          - verified_no_open_order=True: 운영자가 KIS 에서 미체결 없음을 확인했다는
            명시적 확인 플래그(False 면 거부).

        동작: registry row → CANCELLED(mark_terminal, 감사 사유), lifecycle →
        CANCELLED(직접 cancel; 접수건도 허용), in-memory 차단 meta 제거. 모든 필드와
        함께 [US AUDIT] 감사 로그를 남긴다. 반환: 처리 결과 dict.
        """
        operator = (operator or "").strip()
        reason   = (reason or "").strip()
        if not operator or not reason or not verified_no_open_order:
            logger.error(
                "[US AUDIT] 수동 해제 거부 — 필수조건 미충족: lifecycle_id=%s "
                "operator=%r reason=%r verified=%s",
                lifecycle_id, operator, reason, verified_no_open_order)
            return {"ok": False, "reason": "operator/reason/verified 필수"}

        from datetime import datetime as _dt
        ts = _dt.now().isoformat()
        symbol = side = odno = ""
        mgr = getattr(self, "_us_lifecycle_mgr", None)
        reg = getattr(self, "_us_pending_registry", None)

        if reg is not None and _US_FILL_OBSERVER_ENABLED:
            try:
                row = reg.get_by_trade_id(lifecycle_id)
                if row is not None:
                    symbol = row.get("code") or ""
                    side   = row.get("side") or ""
                    odno   = str(row.get("odno") or "").strip()
                    reg.mark_terminal(
                        lifecycle_id, _USPendingStatus.CANCELLED,
                        reason=f"MANUAL_RELEASE by={operator}: {reason}")
            except Exception as _rexc:
                logger.error("[US AUDIT] registry 수동취소 실패: %s", _rexc)

        if mgr is not None:
            try:
                lc = mgr.load(lifecycle_id)
                if lc is not None:
                    symbol = symbol or lc.code
                    side   = side or (lc.side or "")
                    odno   = odno or str(getattr(lc, "odno", "") or "").strip()
                    if not lc.is_terminal:
                        mgr.cancel(lc, reason=f"MANUAL_RELEASE by={operator}: {reason}")
            except Exception as _lexc:
                logger.error("[US AUDIT] lifecycle 수동취소 실패: %s", _lexc)

        self._us_pending_buy_meta.pop(lifecycle_id, None)
        self._us_pending_sell_meta.pop(lifecycle_id, None)

        logger.warning(
            "[US AUDIT] 수동 해제 완료 — operator=%s reason=%r ts=%s "
            "lifecycle_id=%s symbol=%s side=%s odno=%s (운영자 KIS 미체결없음 확인)",
            operator, reason, ts, lifecycle_id, symbol, side, odno)
        return {
            "ok": True, "lifecycle_id": lifecycle_id, "symbol": symbol,
            "side": side, "odno": odno, "operator": operator,
            "reason": reason, "ts": ts,
        }

    @property
    def positions(self) -> dict:
        # ★ '실제 보유' 뷰 — 격리(BROKER_ABSENT_QUARANTINED)는 실보유가 아니므로 제외.
        #   보유수·평가금액·중복매수 판정·외부 노출 모두 이 뷰를 쓴다(격리 미포함).
        return {sym: p.to_dict()
                for sym, p in self.pos_mgr.active_positions().items()}

    @property
    def quarantined(self) -> dict:
        """격리 포지션 뷰(감사용, 실보유 아님)."""
        return {sym: p.to_dict()
                for sym, p in self.pos_mgr.quarantined_positions().items()}

    def us_quarantine_audit(self) -> list:
        """격리 감사정보(symbol/quarantined_at/reason/snapshot_id) — /api/status 노출."""
        return self.pos_mgr.quarantine_audit()

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
            # ★ 격리 포지션은 실보유가 아니므로 관리 대상에서 제외(active 만)
            pos_held = self.pos_mgr.active_positions().get(symbol)
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

        # 보유 중이면 평가손익 계산 (USD→KRW 근사; 격리 제외 = active 만)
        _pos_now = self.pos_mgr.active_positions().get(symbol)
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

        # ★ 격리 포지션은 실보유가 아니므로 매도판정 대상에서 제외(active 만).
        #   (격리 심볼은 아래 미보유 경로로 흘러가 중복매수 판정에서도 실보유로 취급 안 됨)
        pos = self.pos_mgr.active_positions().get(symbol)

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

    def _us_account_risk_exceeded(self) -> bool:
        """계좌 위험한도 초과 여부 — 손실 매도의 유일한 계좌레벨 허용조건(§4-2).
        기존 DailyPnLGuard 의 손실한도(LOSS_LIMIT)/정지(HALTED) 상태를 그대로 사용한다.
        (미국장 세션 기준 실현손익이 손실한도 이하로 내려간 상태)."""
        try:
            return getattr(self.pnl_guard, "state", "TRADING") in ("LOSS_LIMIT", "HALTED")
        except Exception:
            return False

    # ══════════════════════════════════════════════════════════
    # US 복원/손실회복 관리 (§2/§3/§4/§8) — 기존 매도 로직보다 먼저 개입
    # ══════════════════════════════════════════════════════════
    def _us_apply_management(self, pos, symbol, name, excd, cur_price, net_pct,
                             pnl_usd, pnl_krw_approx, sess, iv=None):
        """복원/손실회복/수익 트레일링 관리 판정을 기존 ①~⑩ 매도 로직보다 '먼저' 적용.

        반환:
          None  → DEFER: 관리 개입 없음 → 호출부가 기존 ①~⑩ 수행(신규·NORMAL·트레일 미활성).
          dict  → HOLD(기존 로직 스킵) 또는 SELL 결과. SELL 은 **오직 _do_sell 경유**
                  (crash-safe submit-intent → KIS SELL → lifecycle/registry). 별도
                  주문 경로 없음. 타임아웃/불명확 시 재제출 없음(EXIT_PENDING 유지).

        지표(iv)에서 ATR%/EMA9/EMA9기울기를 ctx 로 주입해 동적 트레일·종가확정에 사용.
        부수효과: pos.mgmt 갱신 + pos_mgr.save()(원자적 영속). 포지션 제거는 하지 않음.
        """
        now = datetime.now()
        # highest 단일화(하락 금지) 후 순수 판정
        pos.sync_mgmt_high()
        prev_mode = pos.mgmt.get("management_mode", USR.MODE_NORMAL)
        # ★ 격리 포지션은 애초에 여기 도달하지 않지만(active 만 관리), 방어적으로 HOLD
        if pos.is_quarantined:
            return {"action": "HOLD", "symbol": symbol, "name": name, "excd": excd,
                    "reason": "[관리] 격리 포지션 — 매도판정 제외",
                    "session": sess.get("session", "")}
        _iv = iv or {}
        # ── 종가 확인용 '실제 마감 완료 봉'(now.floor 금지) ──
        #   실제 1분봉 OHLCV 를 조회해 UTC 정규화 → 마지막 '마감 완료' 1분봉/5분봉의
        #   (timestamp, close) 를 뽑는다. 진행 중 봉은 제외. 데이터 없음/지연이면
        #   bar_ts/close=None → 확정봉 매도판정 보류(단, 실시간가 하드손절·급락 안전매도는
        #   계속 동작). tz(UTC/ET/KST) 는 정규화기가 동일 봉으로 통일한다.
        bar_ctx = {"last_completed_1m_bar_at": None, "last_completed_1m_close": None,
                   "last_completed_5m_bar_at": None, "last_completed_5m_close": None}
        try:
            _raw_bars = self.api.get_us_intraday_bars(symbol, excd)
            bar_ctx = USBARS.completed_bar_context(_raw_bars, datetime.now(_UTC))
        except Exception as _be:
            logger.debug("[US관리] %s 1분봉 조회 실패 → 확정봉 보류: %s", symbol, _be)
        ctx = {
            "atr_pct":     float(_iv.get("atr_pct") or 0.0),
            "ema9":        _iv.get("ema9"),
            "ema9_rising": bool(_iv.get("ema9_rising")),
            "bar1_ts":     bar_ctx["last_completed_1m_bar_at"],
            "bar1_close":  bar_ctx["last_completed_1m_close"],
            "bar5_ts":     bar_ctx["last_completed_5m_bar_at"],
            "bar5_close":  bar_ctx["last_completed_5m_close"],
            # ★ 계좌 위험한도 초과 여부 — 손실 매도의 유일한 계좌레벨 허용조건(§4-2).
            #   기존 DailyPnLGuard 의 손실한도/정지 상태를 그대로 사용(추가 정책 없음).
            "account_risk_exceeded": self._us_account_risk_exceeded(),
        }
        d = USR.decide_management_action(dict(pos.mgmt), net_pct, cur_price, now, ctx=ctx)
        # 갱신 상태 반영(액션 무관하게 last_evaluated_at/회복상태 저장) + highest 재동기화
        pos.mgmt = d.state
        pos.sync_mgmt_high()

        # ── DEFER 는 더 이상 반환되지 않는다(단일 매도판정 권위). 방어적으로 HOLD 처리.
        #   → _manage_position 이 기존 ①~⑩ 분기로 흘려보내지 않는다(항상 dict 반환).
        if d.action == USR.ACT_DEFER:   # pragma: no cover (도달 불가)
            self.pos_mgr.save()
            return {"action": "HOLD", "symbol": symbol, "name": name, "excd": excd,
                    "reason": "[관리] normal_hold(single_sell_authority)",
                    "session": sess.get("session", ""), "net_pct": net_pct}

        # ── HOLD: 관리모드가 HOLD 강제(복원 면제/회복 유지) → 기존 로직 스킵 ──
        if d.action == USR.ACT_HOLD:
            self.pos_mgr.save()
            logger.info(
                "[US관리] %s(%s) HOLD mode=%s reason=%s net=%+.2f%% "
                "(복원=%s) — 기존 익절/시간/추세매도 스킵",
                name, symbol, d.mode, d.reason, net_pct, pos.recovered,
            )
            return {"action": "HOLD", "symbol": symbol, "name": name, "excd": excd,
                    "reason": f"[관리:{d.mode}] {d.reason}",
                    "session": sess.get("session", ""),
                    "net_pct": net_pct, "mode": d.mode}

        # ── SELL_ALL: EXIT_PENDING 전이·영속 후, 기존 crash-safe 제출경로로만 매도 ──
        if d.action == USR.ACT_SELL_ALL:
            # ★ 명확 거절 쿨다운 중이면 재제출 금지(무한 재시도 방지, item5).
            #   쿨다운은 '명확 거절' 후에만 설정된다. 판정 상태는 그대로 두고 HOLD 반환.
            cd = pos.mgmt.get("sell_cooldown_until")
            if cd:
                try:
                    if now < datetime.fromisoformat(cd):
                        self.pos_mgr.save()
                        logger.info(
                            "[US관리] %s(%s) SELL 판정이나 명확거절 쿨다운(until=%s) → "
                            "재제출 보류(HOLD)", name, symbol, cd)
                        return {"action": "HOLD", "symbol": symbol, "name": name,
                                "excd": excd, "reason": f"[관리] 매도 쿨다운(until={cd})",
                                "session": sess.get("session", ""), "mode": d.mode}
                except (TypeError, ValueError):
                    pass

            # ★ 되돌림용 스냅샷: 직전 판정상태(RECOVERY + started_at/high 포함)를 정확히 보존.
            #   begin_submit_intent 실패로 KIS 미호출 시 이 상태로 원상복구한다(item4).
            pre_exit_mgmt = dict(pos.mgmt)
            # 제출 '전' EXIT_PENDING 전이·영속(재시작/다음루프 재제출 방지, §2/§8)
            pos.mgmt["management_mode"] = USR.MODE_EXIT
            pos.mgmt["exit_pending_ref"] = {
                "reason": d.reason, "submitted_at": now.isoformat(),
                "prev_mode": prev_mode, "lifecycle_id": None, "odno": None,
            }
            self.pos_mgr.save()
            logger.info(
                "[US관리] %s(%s) SELL_ALL → EXIT_PENDING reason=%s net=%+.2f%%",
                name, symbol, d.reason, net_pct,
            )
            result = self._do_sell(
                symbol, name, excd, pos.qty, cur_price,
                f"[관리:{d.reason}] net={net_pct:+.2f}% "
                f"(${pnl_usd:+.2f}≈{pnl_krw_approx:,.0f}원)",
                sess,
            )
            # 유령포지션 제거(체결 아님, KIS 잔고 없음)면 그대로 반환
            if symbol not in self.pos_mgr.positions:
                return result
            live_pos = self.pos_mgr.positions.get(symbol)
            if live_pos is None:
                return result

            _action = str(result.get("action") or "")
            _has_order = self._us_has_active_order(symbol, "SELL")
            if _has_order:
                # 접수/확인대기(ACCEPTED·UNKNOWN_CONFIRM) 또는 in-flight → EXIT_PENDING 유지.
                #   timeout/500/불명확도 여기(재제출 금지, §5). 아무것도 되돌리지 않는다.
                return result

            # ★ 활성 매도주문 없음 = 실제 미전송/명확거절/submit-intent 실패.
            #   직전 판정상태(RECOVERY + started_at/high)를 '정확히' 원상복구(item4).
            live_pos.mgmt = dict(pre_exit_mgmt)
            # 명확 거절(SELL_FAIL)인 경우에만 쿨다운 설정(무한 재시도 방지, item5).
            #   HOLD(submit-intent 실패 등)는 쿨다운 없이 다음 루프 재시도 허용.
            if _action == "SELL_FAIL":
                live_pos.mgmt["sell_cooldown_until"] = (
                    now + timedelta(seconds=US_SELL_REJECT_COOLDOWN_SEC)).isoformat()
                logger.info(
                    "[US관리] %s(%s) 명확거절 → mode=%s 원상복구 + 쿨다운 %ds",
                    name, symbol, pre_exit_mgmt.get("management_mode"),
                    US_SELL_REJECT_COOLDOWN_SEC)
            else:
                logger.info(
                    "[US관리] %s(%s) 미전송(활성주문 없음) → mode=%s 원상복구(다음 루프 재판정)",
                    name, symbol, pre_exit_mgmt.get("management_mode"))
            self.pos_mgr.save()
            return result

        # 방어: 알 수 없는 액션 → 기존 ①~⑩ 로 흘려보내지 않고 HOLD(단일 권위 유지)
        self.pos_mgr.save()
        return {"action": "HOLD", "symbol": symbol, "name": name, "excd": excd,
                "reason": "[관리] unknown_action_hold", "session": sess.get("session", ""),
                "net_pct": net_pct}

    # ══════════════════════════════════════════════════════════
    # US 실보유 정합화(§4~§7) — KIS 잔고 권위 기반 복원/정리
    # ══════════════════════════════════════════════════════════
    def _next_snapshot_id(self) -> str:
        """정합화 스냅샷 식별자(감사용). 시퀀스+타임스탬프(원문·계좌 없음)."""
        self._us_snapshot_seq += 1
        return f"us-snap-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{self._us_snapshot_seq}"

    def us_reconcile_positions(self, snapshot: dict = None,
                               allow_stale_delete: bool = None) -> dict:
        """KIS 해외 잔고를 '보유수량 권위값'으로 내부 원장을 복원/정합/격리한다(§2/§4~§7).

        Args:
          snapshot : {ok, source, complete, holdings, authoritative_empty} —
                     None 이면 api.get_us_balance_full().
          allow_stale_delete : 격리 포지션의 '최종 삭제' 허용 여부(None=인스턴스 기본,
                     기본 False → 삭제 없이 격리만). 삭제는 완전 스냅샷 + 승인 동시 충족 시만.

        동작(순수판정 us_reconcile.reconcile_decision 위임 후 부수효과):
          · 비권위(조회 실패/캐시/파싱실패) → 삭제·격리·복원 안 함, **신규 BUY만 스킵**(§6).
          · 복원(to_restore) → USPosition(recovered=True), highest=max(avg,cur,기존),
            즉시 매도 안 함(HOLD). 격리중 재등장이면 즉시 격리해제(§ 재등장).
          · 정합(to_reconcile) → qty/avg=KIS 권위 갱신, highest/recovery_high 하락 금지.
            손익은 임의 부킹하지 않는다(회계는 체결 파이프라인 전담).
          · broker_absent(완전 스냅샷만) → **격리(BROKER_ABSENT_QUARANTINED)**. 삭제 아님.
            운영자 승인(allow_stale_delete)+완전 스냅샷일 때만 최종 삭제.
          · 불완전 스냅샷 → positive holding 복원만. broker 부재 판정·격리 금지(§1).

        신규 BUY 게이트: 완전·권위 스냅샷 + 복원 완료 + 내부 stale 전부 격리(또는 삭제)
          완료 시에만 허용. 불완전/실패면 이번 스캔 BUY 스킵(SELL·체결감시는 계속).
        비재진입 락으로 중복 실행을 막고, PII 없는 health 를 갱신한다.
        """
        if allow_stale_delete is None:
            allow_stale_delete = self._us_allow_stale_delete
        if not self._us_reconcile_lock.acquire(blocking=False):
            logger.info("[US정합화] 이미 실행 중 — 이번 호출 스킵(비재진입)")
            return dict(self._us_reconcile_health)
        try:
            now_iso = datetime.now().isoformat()
            snap_id = self._next_snapshot_id()
            # ── 브로커 스냅샷 취득 ──
            if snapshot is None:
                try:
                    snapshot = self.api.get_us_balance_full()
                except Exception as e:
                    snapshot = {"ok": False, "source": None,
                                "complete": False, "holdings": []}
                    logger.warning(f"[US정합화] 잔고 완전조회 예외: {e}")
            ok        = bool(snapshot.get("ok"))
            source    = snapshot.get("source")
            complete  = bool(snapshot.get("complete"))
            holdings  = snapshot.get("holdings")
            auth_empty = bool(snapshot.get("authoritative_empty"))

            internal_syms = list(self.pos_mgr.positions.keys())
            quarantined_syms = list(self.pos_mgr.quarantined_positions().keys())
            res = USRC.reconcile_decision(
                ok=ok, source=source, holdings=holdings,
                internal_symbols=internal_syms, complete=complete,
                quarantined_symbols=quarantined_syms,
            )

            # ── 비권위 → 삭제/격리/복원 안 함. 신규 BUY 게이트만 내림(§6) ──
            if not res.authoritative:
                self._us_buy_gate_ok     = False
                self._us_buy_gate_reason = res.reason
                self._us_reconcile_health.update({
                    "ran": True, "last_run_at": now_iso, "snapshot_id": snap_id,
                    "authoritative": False, "buy_allowed": False,
                    "complete": complete, "authoritative_empty": False,
                    "restored": 0, "reconciled": 0, "quarantined": 0,
                    "unquarantined": 0, "stale_deleted": 0,
                    "quarantined_total": len(quarantined_syms),
                    "broker_count": res.broker_count,
                    "internal_count": res.internal_count,
                    "active_internal_count": res.active_internal_count,
                    "reason": res.reason, "error": None,
                })
                logger.warning(
                    "[US정합화] 비권위 잔고(%s) → 삭제·격리·복원 없음, 신규 BUY 스킵. "
                    "기존 SELL/체결감시는 계속.", res.reason,
                )
                return dict(self._us_reconcile_health)

            restored = reconciled = quarantined = unquarantined = stale_deleted = 0

            # (1) 복원/격리해제: broker 有·내부 active 無 → recovered=True HOLD 로 등록.
            #     격리중 재등장이면 격리해제 + 권위 갱신(§ 재등장).
            for rec in res.to_restore:
                sym = rec["symbol"]
                h = max(float(rec["avg_price"] or 0.0),
                        float(rec.get("cur_price") or 0.0),
                        float(rec.get("highest_price") or 0.0))
                existing = self.pos_mgr.positions.get(sym)
                if existing is not None:
                    # 격리중 재등장 → 즉시 정상 복구(격리해제) + 권위 갱신, highest 하락 금지
                    was_q = existing.is_quarantined
                    existing.clear_quarantine()
                    existing.qty       = int(rec["qty"])
                    existing.avg_price = float(rec["avg_price"])
                    existing.highest_price = max(float(existing.highest_price or 0.0), h)
                    existing.recovered = True
                    existing.sync_mgmt_high()
                    if was_q:
                        unquarantined += 1
                        logger.info("♻️[US정합화] 격리해제(재등장) %s %s주 avg=$%.2f",
                                    sym, rec["qty"], rec["avg_price"])
                    else:
                        reconciled += 1
                    continue
                p = USPosition(sym, rec.get("name", sym), rec.get("excd", "NASD"),
                               int(rec["qty"]), float(rec["avg_price"]),
                               recovered=True)
                p.highest_price = h
                p.sync_mgmt_high()
                p.mgmt["management_mode"] = USR.MODE_NORMAL   # 복원 직후 HOLD(즉시매도 금지)
                self.pos_mgr.positions[sym] = p
                restored += 1
                logger.info(
                    "🔄[US정합화] 복원 %s %s주 avg=$%.2f highest=$%.2f "
                    "(recovered=True, 복원직후 HOLD)",
                    sym, rec["qty"], rec["avg_price"], h,
                )

            # (2) 정합: 양쪽 active → qty/avg=KIS 권위 갱신, highest 하락 금지(손익 부킹 없음)
            for rec in res.to_reconcile:
                sym = rec["symbol"]
                p = self.pos_mgr.positions.get(sym)
                if p is None:
                    continue
                p.clear_quarantine()   # active 로 확인됨 — 혹시 남은 격리표식 해제
                p.qty       = int(rec["qty"])
                p.avg_price = float(rec["avg_price"])
                newh = max(float(p.highest_price or 0.0),
                           float(rec["avg_price"] or 0.0),
                           float(rec.get("cur_price") or 0.0))
                p.highest_price = newh
                rhp = p.mgmt.get("recovery_high_price")
                if rhp is not None:   # recovery_high 도 절대 하락 금지
                    p.mgmt["recovery_high_price"] = max(
                        float(rhp), float(rec.get("cur_price") or 0.0))
                p.sync_mgmt_high()
                reconciled += 1

            # (3) broker_absent(완전 스냅샷만) → 격리 or (승인 시)최종 삭제. §5
            #     불완전 스냅샷이면 res.broker_absent==[] 이므로 아무 것도 하지 않는다(§1).
            do_delete = complete and allow_stale_delete
            for sym in res.broker_absent:
                p = self.pos_mgr.positions.get(sym)
                if p is None:
                    continue
                # 활성 매도주문 중이면 격리·삭제 보류(체결경로가 처리)
                if self._us_has_active_order(sym, "SELL"):
                    logger.info("[US정합화] %s 활성 매도주문 존재 → 격리/삭제 보류", sym)
                    continue
                if do_delete:
                    self.pos_mgr.positions.pop(sym, None)
                    stale_deleted += 1
                    logger.warning(
                        "[US정합화] 최종삭제 %s (KIS 미보유·완전스냅샷·운영자승인)", sym)
                else:
                    if not p.is_quarantined:
                        p.quarantine(reason="broker_absent(complete snapshot)",
                                     snapshot_id=snap_id, now_iso=now_iso)
                        quarantined += 1
                        logger.warning(
                            "🚧[US정합화] 격리 %s (KIS 완전스냅샷 미보유 → "
                            "BROKER_ABSENT_QUARANTINED; 매도·판정·집계 제외, 감사 유지)",
                            sym)

            # ── 신규 BUY 게이트: 완전·권위 + 미해결 broker_absent 없음(전부 격리/삭제) ──
            remaining_absent = [s for s in res.broker_absent
                                if s in self.pos_mgr.positions
                                and not self.pos_mgr.positions[s].is_quarantined
                                and not do_delete]
            buy_ok = bool(res.authoritative and complete and not remaining_absent)
            self._us_buy_gate_ok = buy_ok
            self._us_buy_gate_reason = (
                "" if buy_ok else
                ("incomplete_snapshot(buy skipped this scan)" if not complete
                 else "stale_not_yet_quarantined"))

            self.pos_mgr.save()   # 원자 저장(변경 없더라도 last_evaluated 등 안전 반영)

            q_total = len(self.pos_mgr.quarantined_positions())
            self._us_reconcile_health.update({
                "ran": True, "last_run_at": now_iso, "snapshot_id": snap_id,
                "authoritative": True, "buy_allowed": buy_ok,
                "complete": complete, "authoritative_empty": auth_empty,
                "restored": restored, "reconciled": reconciled,
                "quarantined": quarantined, "unquarantined": unquarantined,
                "stale_deleted": stale_deleted, "quarantined_total": q_total,
                "broker_count": res.broker_count,
                "internal_count": res.internal_count,
                "active_internal_count": res.active_internal_count,
                "reason": res.reason, "error": None,
            })
            logger.info(
                "[US정합화] 완료 complete=%s 복원=%d 정합=%d 격리=%d 격리해제=%d "
                "삭제=%d 격리총=%d broker=%d buy_ok=%s",
                complete, restored, reconciled, quarantined, unquarantined,
                stale_deleted, q_total, res.broker_count, buy_ok,
            )
            return dict(self._us_reconcile_health)
        except Exception as e:
            self._us_buy_gate_ok = False   # 예외 시 안전하게 BUY 차단
            self._us_buy_gate_reason = "reconcile_exception"
            self._us_reconcile_health.update({
                "ran": True, "last_run_at": datetime.now().isoformat(),
                "buy_allowed": False, "error": str(e), "reason": "reconcile_exception",
            })
            logger.error(f"[US정합화] 예외 — 삭제·격리 없이 종료(안전): {e}")
            return dict(self._us_reconcile_health)
        finally:
            self._us_reconcile_lock.release()

    def us_reconcile_health(self) -> dict:
        """PII 없는 정합화 health 스냅샷(/api/status 노출용)."""
        return dict(self._us_reconcile_health)

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
        # [관리모드 개입] 복원/손실회복 트레일링 — 기존 ①~⑩보다 '먼저'(§8)
        #   · EXIT_PENDING → HOLD(재제출 금지)
        #   · RECOVERY_TRAILING/진입(net≤-5) → 손실회복 판정 우선(⑧⑨⑩보다 앞)
        #   · recovered=True(복원) → 고정익절·시간청산·MA매도 면제, 수익 트레일링만
        #   · 그 외(신규·NORMAL·net>-5) → None 반환 → 아래 기존 ①~⑩ 그대로 수행
        # ════════════════════════════════════════════════════
        _mgmt_result = self._us_apply_management(
            pos, symbol, name, excd, cur_price, net_pct,
            pnl_usd, pnl_krw_approx, sess, iv=iv
        )
        if _mgmt_result is not None:
            return _mgmt_result

        # ════════════════════════════════════════════════════
        # 청산 우선순위: (①+2.5%·②+2.0% 고정익절 비활성화 — 동적 수익 트레일링이 지배)
        #               → ③+1.5%+SCORE≥4 → ④KRW → ⑤트레일링 → ⑥시간청산 → ⑦손절
        # ════════════════════════════════════════════════════
        # ★ ①/② 고정익절(+2.5%/+2.0% 무조건 전량)은 '동적 수익 트레일링(ATR)' 보다
        #   먼저 실행되지 않도록 비활성화한다. 수익 트레일링 활성(최고 net>=+1.5%) 시
        #   _us_apply_management 가 이미 지배(HOLD/SELL)하므로 이 지점에 도달하지
        #   않는다. 도달했다면 아직 트레일 미활성(최고 net<+1.5%) 이므로 고정익절도
        #   당연히 미충족이다. (기존 ①② 블록 제거 — 파라미터/판정만 수정)

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

        # ── ★ 정합화 fail-safe(§6): 최근 잔고조회가 '비권위'면 신규 BUY만 스킵 ──
        #   (기존 SELL/체결감시/보유관리는 이 게이트와 무관하게 계속된다.)
        if not self._us_buy_gate_ok:
            logger.info(
                "[US정합화] 신규 진입 스킵 %s — 잔고 비권위(%s). "
                "기존 보유 SELL/관리는 계속.",
                symbol, self._us_buy_gate_reason or "unauthoritative",
            )
            return _hold(
                f"정합화 비권위로 신규매수 보류({self._us_buy_gate_reason or 'unauthoritative'})",
                "HOLD",
            )

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
        except Exception as e:
            # ★ 조회 실패 시 '주문 시도'가 아니라 '차단' (사전검증 실패 → 미제출)
            logger.warning(f"[{symbol}] 주문가능금액 조회 실패 → 차단: {e}")
            return False, "주문가능금액 조회 실패 → 차단"

        # ★ ok=False(오류/타임아웃/파싱실패/rt_cd!=0) → 차단
        if not avail.get("ok", False):
            return False, "주문가능금액 사전검증 실패 → 차단"

        usd_avail = float(avail.get("usd", 0.0) or 0.0)   # frcr_ord_psbl_amt1 — 핵심
        krw_avail = float(avail.get("krw", 0.0) or 0.0)   # ovrs_ord_psbl_amt  — 보조
        need_usd  = cur_price * qty

        if usd_avail >= need_usd:
            return True, f"USD가능(${usd_avail:.2f} >= 필요${need_usd:.2f})"

        # USD 부족 → KRW 보조 확인 (ovrs_ord_psbl_amt > 0 인 경우만)
        if krw_avail > 0:
            fx = 1350.0
            try:
                fx = self.api.get_usd_exchange_rate() or 1350.0
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

        # ── ★ in-flight 중복 매수 가드: 동일 종목 미체결 주문 존재 시 스킵 ──
        if self._us_has_active_order(symbol, "BUY"):
            logger.info("[US in-flight] %s 미체결 매수 주문 존재 → 중복 매수 스킵", symbol)
            return {
                "action":  "SKIP", "symbol": symbol, "name": name, "excd": excd,
                "reason":  "미체결 매수 주문 존재 — 중복 매수 스킵(in-flight)",
                "session": sess.get("session", ""),
            }

        # ── ★ 주문 직전 KIS 해외 주문가능금액 사전검증 (확정액만 사용) ──────
        #   조회 오류/타임아웃/파싱 실패/rt_cd!=0 → ok=False → 주문 미제출(BUY_BLOCKED).
        #   국내 원화 예수금 기반 폴백은 제거했다 — KIS 로 확인된 해외 주문가능
        #   금액이 없으면 실주문을 시도하지 않는다.
        try:
            # 실제 주문과 동일 기준(계좌·거래소·종목·주문가격)으로 주문별 검증
            avail = self.api.get_us_available_amounts(
                symbol=symbol, excd=excd, ord_unpr=cur_price)
        except Exception as _ae:
            avail = {"ok": False}
            logger.warning("[%s] 해외 주문가능 조회 예외 → BUY_BLOCKED: %s", symbol, _ae)

        if not avail.get("ok", False):
            logger.warning("[%s] 해외 주문가능 조회 실패/미확인 → 주문 미제출(BUY_BLOCKED)", symbol)
            return {"action": "BUY_BLOCKED", "symbol": symbol, "name": name,
                    "reason": "해외 주문가능금액 사전검증 실패 — 주문 미제출",
                    "session": sess.get("session", "")}

        usd_avail = float(avail.get("usd", 0.0) or 0.0)
        krw_avail = float(avail.get("krw", 0.0) or 0.0)
        if usd_avail <= 0 and krw_avail <= 0:
            logger.warning(
                "[%s] 해외 주문가능금액 0 (usd=%.2f krw=%.0f) → 주문 미제출(BUY_BLOCKED)",
                symbol, usd_avail, krw_avail)
            return {"action": "BUY_BLOCKED", "symbol": symbol, "name": name,
                    "reason": "해외 주문가능금액 0 — 주문 미제출",
                    "session": sess.get("session", "")}

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

        # ── ★ KIS 주문가능금액으로 최종수량 확정 (해외) ─────────────────
        #   해외주식은 미수/신용이 없어 '주문가능금액'(frcr_ord_psbl_amt1/원화환산)이
        #   권위값이다. ovrs_max_ord_psbl_qty 는 장 시작 직후·환율/시세 미확정 시
        #   0 또는 미제공으로 오는 경우가 있어, 그대로 min 상한에 넣으면 금액이
        #   충분해도 전 종목 BUY 가 0주로 차단된다(=미국 거래 전면 중단).
        #   → ovrs_max_ord_psbl_qty 는 '양수일 때만' 안전 상한으로 쓰고, 0/미제공
        #     이면 무시하고 금액기준으로만 확정한다. 차단은 '금액 부족'일 때만.
        _kis_qty = int(avail.get("qty", 0) or 0)          # ovrs_max_ord_psbl_qty
        _amt_qty = qty_from_cash(effective_usd, cur_price)  # floor(가능금액*0.98/가)
        if _kis_qty > 0:
            _final_qty = min(qty, _kis_qty, _amt_qty)
        else:
            _final_qty = min(qty, _amt_qty)   # KIS 수량 미제공/0 → 금액기준만
            logger.info(
                "[%s] ovrs_max_ord_psbl_qty=0/미제공 → 금액기준 사이징 사용", symbol)
        logger.info(
            "[%s] 수량확정 = min(전략%d, KIS가능%s, 금액환산%d) → %d주",
            symbol, qty, (_kis_qty if _kis_qty > 0 else "무시"),
            _amt_qty, _final_qty)
        if _final_qty <= 0:
            logger.warning(
                "[%s] 주문가능금액 부족(금액환산 %d주, usd=%.2f) → 주문 미제출"
                "(BUY_BLOCKED)", symbol, _amt_qty, effective_usd)
            return {"action": "BUY_BLOCKED", "symbol": symbol, "name": name,
                    "reason": "주문가능금액 부족 — 주문 미제출",
                    "session": sess.get("session", "")}
        qty = _final_qty

        # ── ★ 위험기반 수량 축소(§5): 고정 -5%/-6% 손절 제거로 손실 위험거리가 넓어졌다.
        #   종목별 최대 허용손실(US_MAX_LOSS_PER_SYMBOL_USD) ÷ (현재가 × 구조적 위험거리%)
        #   로 상한을 두어, 변동성 큰 종목의 신규 수량을 축소한다(예산 수량을 넘겨 늘리지 않음).
        _atr_pct = float((iv or {}).get("atr_pct") or 0.0)
        _risk_qty = USR.risk_capped_qty(cur_price, _atr_pct, qty,
                                        self.US_MAX_LOSS_PER_SYMBOL_USD)
        if _risk_qty < qty:
            logger.info(
                "[%s] 위험기반 축소: %d → %d주 (최대손실$%.0f, 위험거리%.2f%%=구조적, ATR%.2f%%)",
                symbol, qty, _risk_qty, self.US_MAX_LOSS_PER_SYMBOL_USD,
                USR.struct_stop_pct(_atr_pct), _atr_pct)
        qty = _risk_qty
        if qty <= 0:
            logger.warning(
                "[%s] 위험기반 수량 0주 → 주문 미제출(최대손실$%.0f 대비 위험거리 과대)",
                symbol, self.US_MAX_LOSS_PER_SYMBOL_USD)
            return {"action": "BUY_BLOCKED", "symbol": symbol, "name": name,
                    "reason": "위험기반 사이징 0주 — 주문 미제출",
                    "session": sess.get("session", "")}

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

        # ── 주문 실행 (allow_krw_order=True → USD 실패 시 KIS 내부 원화환전) ──
        #   ★ 축소 재시도는 '주문번호 없는 명확한 잔액부족 거절'에서만 1회.
        #     중복 제출 방지 원칙:
        #       - rt_cd=="9"(예외/타임아웃 래핑) → 접수 여부 불명확 → 재시도 금지
        #       - output.ODNO 존재(접수 정황) → 재시도 금지
        #       - 명시적 잔액/한도 부족 메시지일 때만 축소·재조회 후 재시도
        #     재조회 실패·수량 미축소 시에도 중단(동일수량 반복·무검증 재시도 금지).
        def _eff_usd(_av):
            _u = float(_av.get("usd", 0.0) or 0.0)
            _k = float(_av.get("krw", 0.0) or 0.0)
            if _k > 0:
                try:
                    _fx = self.api.get_usd_exchange_rate() or 1350.0
                except Exception:
                    _fx = 1350.0
                return max(_u, (_k / _fx) * 0.99)
            return _u

        def _clean_balance_reject(_res):
            """주문번호 없이 KIS 가 '명확히 잔액부족으로 거절'한 경우만 True.

            접수됐거나(주문번호 존재) 접수 여부가 불명확한 응답(예외/타임아웃
            래핑 rt_cd=9, 또는 성공 rt_cd=0)에서는 재주문하지 않는다."""
            _rt = str(_res.get("rt_cd", ""))
            if _rt in ("0", "9"):
                return False     # 성공 or 접수불명확(예외 래핑) → 재시도 금지
            _out = _res.get("output") or {}
            if isinstance(_out, list):
                _out = _out[0] if _out else {}
            if str(_out.get("ODNO", "") or "").strip() not in ("", "0"):
                return False     # 주문번호 존재 → 이미 접수 → 재주문 금지
            _msg = _res.get("msg1", "") or ""
            return any(_kw in _msg for _kw in
                       ("부족", "금액", "초과", "주문가능", "한도"))

        _resized_once = False
        _us_lc_id = None
        while True:
            # ★ KIS 호출 '전' durable submit-intent(매 시도마다 신규). 실패 시 미제출.
            _us_lc_id = make_order_lifecycle_id("US", "BUY", symbol)
            if _US_LIFECYCLE_ENABLED and self._us_lifecycle_mgr is not None:
                if not self._us_begin_submit_intent(
                        _us_lc_id, symbol, "BUY", qty, cur_price, excd, _us_trade_id,
                        meta_extra={"name": name, "level": 1,
                                    "reason": entry_reason}):
                    return {"action": "BUY_FAIL", "symbol": symbol, "name": name,
                            "reason": "submit-intent 저장 실패 — 주문 미제출(안전)",
                            "session": sess["session"]}

            result   = self.api.buy_us(symbol, qty, cur_price, excd,
                                       allow_krw_order=True)

            # 응답 분류 → durable intent 확정. REJECTED/NOT_SENT 만 차단 해제(재시도 허용).
            if _US_LIFECYCLE_ENABLED and self._us_lifecycle_mgr is not None:
                _buy_outcome = self._us_finalize_submit_intent(
                    _us_lc_id, symbol, "BUY", qty, result, excd, _us_trade_id)
            else:
                _buy_outcome = (_US_OUTCOME_ACCEPTED if result.get("rt_cd") == "0"
                                else _US_OUTCOME_REJECTED)
            order_ok = _buy_outcome in (_US_OUTCOME_ACCEPTED,
                                        _US_OUTCOME_UNKNOWN_CONFIRM)
            fail_msg = result.get("msg1", "")
            if order_ok or _resized_once:
                break
            # 주문번호 없는 명확한 잔액부족 거절에서만 1회 재산정(중복 제출 방지)
            if not _clean_balance_reject(result):
                break
            try:
                _re = self.api.get_us_available_amounts(
                    symbol=symbol, excd=excd, ord_unpr=cur_price)
            except Exception:
                _re = {"ok": False}
            if not _re.get("ok", False):
                break   # 재조회 실패 → 무검증 재시도 금지
            _re_qty = finalize_order_qty(
                qty, int(_re.get("qty", 0) or 0), _eff_usd(_re), cur_price)
            if _re_qty <= 0 or _re_qty >= qty:
                break   # 더 작아지지 않으면 재시도 안 함(동일수량 반복 금지)
            logger.warning(
                "[%s] 잔액부족 거절(주문번호 없음) → 재조회 후 축소 재시도 "
                "%d→%d주(1회 한정)", symbol, qty, _re_qty)
            qty = _re_qty
            _resized_once = True

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

        # ── [US 훅 D] ORDER_ACCEPTED (접수 성공, 체결 미확인) ──
        # ★ rt_cd=0 은 접수이지 체결이 아님 → 포지션을 만들지 않는다.
        #   포지션 생성은 실체결 후 _us_handle_buy_filled 에서만 수행한다.
        if _US_JOURNAL_ENABLED and _us_trade_id:
            try:
                _us_jnl.record_order_accepted(
                    _us_trade_id, "US", symbol,
                    rt_cd = result.get("rt_cd", "0"),
                    msg1  = result.get("msg1", "US매수접수성공_체결미확인"),
                )
            except Exception as _uje:
                _us_jnl._inc_error("us_accepted", _uje)
        # ★ durable submit-intent 는 이미 저장·확정됨(_us_begin/_us_finalize).
        #   여기서는 별도 등록을 하지 않는다(차단 meta 는 finalize 가 유지).
        logger.info(
            "[US BUY ACCEPTED] %s: order_lifecycle_id=%s symbol=%s qty=%s",
            _buy_outcome, _us_lc_id, symbol, qty)
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

        reason = (f"모멘텀추가매수: 수익{pos.net_pct(cur_price):+.1f}% "
                  f"vol{iv['vol_ratio']:.1f}x VWAP위 등락{iv['intraday_pct']:+.1f}%")
        # ★ KIS 호출 '전' durable submit-intent 저장(crash 안전). 실패 시 미제출.
        _us_add_lc_id = make_order_lifecycle_id("US", "BUY", symbol)
        if _US_LIFECYCLE_ENABLED and self._us_lifecycle_mgr is not None:
            if not self._us_begin_submit_intent(
                    _us_add_lc_id, symbol, "BUY", add_qty, cur_price, excd,
                    _us_add_trade_id,
                    meta_extra={"name": name, "level": 2, "reason": reason}):
                return {"action": "BUY_FAIL", "symbol": symbol, "name": name,
                        "reason": "submit-intent 저장 실패 — 주문 미제출(안전)",
                        "session": sess["session"]}

        result  = self.api.buy_us(symbol, add_qty, cur_price, excd, allow_krw_order=True)

        # 응답 분류(rt_cd=9/알수없는코드/ODNO존재 → 확인대기, 명확거절만 REJECTED)
        if _US_LIFECYCLE_ENABLED and self._us_lifecycle_mgr is not None:
            _add_outcome = self._us_finalize_submit_intent(
                _us_add_lc_id, symbol, "BUY", add_qty, result, excd,
                _us_add_trade_id)
        else:
            _add_outcome = (_US_OUTCOME_ACCEPTED if result.get("rt_cd") == "0"
                            else _US_OUTCOME_REJECTED)

        if _add_outcome in (_US_OUTCOME_REJECTED, _US_OUTCOME_NOT_SENT):
            # ── [US 훅 F] 추가매수 ORDER_REJECTED (명확 거절/미전송 — 주문 없음) ──
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

        # ACCEPTED 또는 UNKNOWN_CONFIRM → 접수/확인대기(차단 유지). 포지션 갱신은 체결시.
        # ── [US 훅 G] 추가매수 ORDER_ACCEPTED (접수, 체결 미확인) ──
        if _US_JOURNAL_ENABLED and _us_add_trade_id:
            try:
                _us_jnl.record_order_accepted(
                    _us_add_trade_id, "US", symbol,
                    rt_cd = result.get("rt_cd", "0"),
                    msg1  = result.get("msg1", "US추가매수접수성공_체결미확인"),
                )
            except Exception as _uje:
                _us_jnl._inc_error("us_add_accepted", _uje)
        logger.info("🟢 US추가매수 %s %d주 $%.2f | %s [%s]",
                    symbol, add_qty, cur_price, reason, _add_outcome)
        return self._buy_result(symbol, name, excd, cur_price, add_qty, 2, sess, iv,
                                reason, "[모멘텀추가]")

    def _do_sell(self, symbol, name, excd, qty, cur_price, reason, sess,
                 is_partial: bool = False) -> dict:
        # ── ★ in-flight 중복 매도 가드: 동일 종목 미체결 매도 존재 시 스킵 ──
        #   apply(감소/삭제)는 FILLED 시점으로 미뤄지므로 포지션이 남아 있어도
        #   여기서 재매도하면 이중 매도가 된다.
        if self._us_has_active_order(symbol, "SELL"):
            logger.info("[US in-flight] %s 미체결 매도 주문 존재 → 중복 매도 스킵", symbol)
            return {
                "action":  "HOLD", "symbol": symbol, "name": name, "excd": excd,
                "reason":  "미체결 매도 주문 존재 — 중복 매도 스킵(in-flight)",
                "session": sess.get("session", ""),
            }
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

        # ★ KIS 호출 '전' durable submit-intent 저장(crash 안전). 실패 시 미제출.
        pos    = self.pos_mgr.positions.get(symbol)
        avg_p  = pos.avg_price if pos else cur_price
        _us_sell_lc_id = make_order_lifecycle_id("US", "SELL", symbol)
        if _US_LIFECYCLE_ENABLED and self._us_lifecycle_mgr is not None:
            if not self._us_begin_submit_intent(
                    _us_sell_lc_id, symbol, "SELL", qty, cur_price, excd,
                    _us_sell_trade_id,
                    meta_extra={"name": name, "avg_price": avg_p,
                                "reason": reason, "is_full": not is_partial}):
                logger.critical(
                    "[US SELL] submit-intent 저장 실패 → 매도 미제출(안전): %s", symbol)
                return {
                    "action":  "HOLD", "symbol": symbol, "name": name, "excd": excd,
                    "reason":  "submit-intent 저장 실패 — 매도 미제출(안전)",
                    "session": sess.get("session", ""),
                }

        result  = self.api.sell_us(symbol, qty, cur_price, excd)

        # 응답 분류(rt_cd=9/알수없는코드/ODNO존재 → 확인대기, 명확거절만 REJECTED)
        if _US_LIFECYCLE_ENABLED and self._us_lifecycle_mgr is not None:
            _sell_outcome = self._us_finalize_submit_intent(
                _us_sell_lc_id, symbol, "SELL", qty, result, excd,
                _us_sell_trade_id)
        else:
            _sell_outcome = (_US_OUTCOME_ACCEPTED if result.get("rt_cd") == "0"
                             else _US_OUTCOME_REJECTED)

        if _sell_outcome in (_US_OUTCOME_ACCEPTED, _US_OUTCOME_UNKNOWN_CONFIRM):
            # 접수/확인대기 — 포지션 감소·실현손익·재진입은 체결시에만(차단 유지).
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
            est_pnl_usd = (cur_price - avg_p) * qty
            est_pnl_pct = (cur_price - avg_p) / avg_p * 100 if avg_p > 0 else 0.0
            logger.info(
                "[US SELL %s] %s(%s) $%.2f×%s주 접수 — 체결 대기 "
                "(%s, 예상손익 $%+.2f) 사유=%s",
                _sell_outcome, name, symbol, cur_price, qty,
                ("전량" if not is_partial else "부분"), est_pnl_usd, reason,
            )
            return {
                "action":      "SELL_ACCEPTED",
                "symbol":      symbol, "name": name, "excd": excd,
                "price":       cur_price, "qty": qty,
                "est_pnl_usd": round(est_pnl_usd, 2),
                "est_pnl_pct": round(est_pnl_pct, 2),
                "reason":      reason, "session": sess["session"], "currency": "USD",
            }

        # REJECTED(명확 거절) 또는 NOT_SENT(미전송) → 매도 실패/미제출(차단 해제됨).
        # '가능수량보다 큽니다' = KIS 잔고 없음 → 유령 포지션 제거.
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
            "action":       "BUY_ACCEPTED",
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
