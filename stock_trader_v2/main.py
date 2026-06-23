"""
main.py — KIS 단타봇 V2 메인 루프 (국내장 + 미국장)
=====================================================
★ 핵심 원칙:
  1. 매 루프: 실계좌 동기화 → 국내/미국장 종목별 전략 실행
  2. 14:30 이후 국내 신규매수 금지 (KRStrategy 내부 체크)
  3. 15:20 국내 미체결 BUY 전량 취소 + 포지션 강제청산
  4. 미국장 마감 30분 전 신규매수 금지 (USBroker.is_buy_allowed())
  5. 봇 시작 시 trade_log 기반 재진입 차단 복원 (KR/US 공통)

실행:
    cd stock_trader_v2
    python main.py

환경변수 (.env):
    KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO
    INITIAL_ASSET        (기준자산, 기본 5000000)
    KR_WATCH_LIST_CODES  (국내 종목코드, 쉼표 구분)
    US_WATCH_LIST_CODES  (미국 종목코드, 쉼표 구분, 예: "AAPL,TSLA,NVDA")
    US_EXCH_CODES        (거래소코드, 쉼표 구분, 기본 NASD, 예: "NASD,NYSE,NASD")
    US_USD_KRW           (USD/KRW 환율, 기본 1350)
    LOOP_INTERVAL_SEC    (루프 주기 초, 기본 30)
    V2_PROFIT_LOCK_KRW   (일일 수익잠금 기준, 기본 300000)
    V2_LOSS_LIMIT_KRW    (일일 손실한도, 기본 -300000)

하위 호환 (기존 WATCH_LIST_CODES → KR_WATCH_LIST_CODES 로 인식):
    WATCH_LIST_CODES     (구형 환경변수, 국내 종목으로 인식)
"""

import os
import sys
import time
import json
import signal
import threading
import atexit
import subprocess as _sp
from datetime import datetime, time as dtime
import pytz

# ── 경로 설정 ─────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from dotenv import load_dotenv
# ★ 로드 순서 (낮은 번호가 먼저, override=False → 이미 설정된 값 유지)
# 1) 루트/.env       (공통 기본값)
# 2) stock_trader/.env (V1 공유 인증정보 폴백)
# 3) stock_trader_v2/.env (V2 전용 — 최우선, override=True로 덮어씀)
load_dotenv(os.path.join(_HERE, "..", ".env"),                    override=False)
load_dotenv(os.path.join(_HERE, "..", "stock_trader", ".env"),    override=False)
load_dotenv(os.path.join(_HERE, ".env"),                          override=True)   # V2 전용 최우선

from broker.kr_broker       import KRBroker, BUY_CLOSE_TIME, BUY_HARD_STOP
from broker.us_broker       import USBroker, EXCH_NASD
from risk.reentry_guard     import ReentryGuard
from risk.pnl_guard         import DailyPnLGuard
from engine.account_sync    import AccountSync
from engine.execution_engine import ExecutionEngine
from strategy.kr_strategy   import KRStrategy
from strategy.us_strategy   import USStrategy, _KIS_ORDER_BLACKLIST as _KIS_ORDER_BLACKLIST_MAIN
from utils.v2_logger        import get_logger
from adaptive.trade_recorder   import TradeRecorder
from adaptive.strategy_analyzer import StrategyAnalyzer
from adaptive.weight_adjuster   import WeightAdjuster
from adaptive.daily_reporter    import DailyReporter
from adaptive.daily_review      import DailyReviewEngine
from adaptive.trade_sync        import TradeSyncEngine
from utils.log_archiver         import LogArchiver

logger = get_logger("V2Main")
KST    = pytz.timezone("Asia/Seoul")

# USD/KRW 폴백 환율 (환경변수 US_USD_KRW 미설정 시)
_DEFAULT_USD_KRW = 1_350.0

# ── Single Instance Lock (V2 LIVE 전용) ───────────────────────
_LOCK_FILE = os.path.join(_HERE, "data", "v2_live.lock")


def _write_lock():
    """
    V2 LIVE lock 파일 생성.
    이 파일이 존재하면 V1 주문루프가 완전 차단됨.
    """
    os.makedirs(os.path.join(_HERE, "data"), exist_ok=True)
    commit = ""
    # ★ git -C 로 repo 루트(stock_trader_v2의 상위)까지 올라가서 HEAD 읽기
    # _HERE = /home/user/webapp/stock_trader_v2  → parent = /home/user/webapp (git repo 루트)
    _repo_root = os.path.dirname(_HERE)
    for _gdir in [_repo_root, _HERE]:
        try:
            _c = _sp.check_output(
                ["git", "-C", _gdir, "rev-parse", "--short", "HEAD"],
                stderr=_sp.DEVNULL, text=True
            ).strip()
            if _c:
                commit = _c
                break
        except Exception:
            continue
    data = {
        "pid":        os.getpid(),
        "start_time": datetime.now(KST).isoformat(),
        "commit":     commit or "unknown",
        "mode":       "v2_live",
    }
    with open(_LOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(
        f"[V2 LOCK] ✅ lock 파일 생성 완료 → V1 주문루프 차단됨 "
        f"(PID={os.getpid()}, commit={commit or 'unknown'})"
    )


def _remove_lock():
    """종료 시 lock 파일 삭제 — V1 주문루프 복원."""
    try:
        if os.path.exists(_LOCK_FILE):
            os.remove(_LOCK_FILE)
            logger.info("[V2 LOCK] 🔓 lock 파일 삭제 완료 → V1 주문루프 복원")
    except Exception as e:
        logger.warning(f"[V2 LOCK] lock 파일 삭제 실패: {e}")


def _build_orderable_candidates(
    watch_list: list,
    reentry,
    account,
    broker,
    blacklist: set,
    usd_krw: float = 0.0,
    market: str = "KR",
) -> tuple[list, dict]:
    """
    재진입차단 / 수량0 / KIS주문불가 종목을 제외하고
    실제 주문 가능한 후보 리스트를 반환한다.

    Returns:
        available : 주문 가능 후보 리스트 (dict 그대로 유지)
        excluded  : {"reentry": [...], "qty0": [...], "blacklist": [...]}
    """
    available: list = []
    excluded: dict  = {"reentry": [], "qty0": [], "blacklist": []}

    for s in watch_list:
        code = s.get("code", "")
        name = s.get("name", code)

        # 1) KIS 주문불가 블랙리스트
        if code in blacklist:
            excluded["blacklist"].append(code)
            continue

        # 2) 재진입 차단
        try:
            blocked, _ = reentry.check(market, code, name)
        except Exception:
            blocked = False
        if blocked:
            excluded["reentry"].append(code)
            continue

        # 3) 수량0 (자금 대비 1주도 못 사는 경우)
        try:
            if market == "KR":
                pd = broker.get_price(code)
                price = pd.get("price", 0)
            else:
                exch_cd = s.get("exch_cd", "NASD")
                pd = broker.get_price(code, exch_cd)
                price = pd.get("cur_price", 0)

            if price and price > 0:
                price_krw = int(price) if market == "KR" else int(price * usd_krw)
                entry = account.calc_entry_amount(price_krw)
                if not entry.get("can_enter", False) or entry.get("max_qty", 0) <= 0:
                    excluded["qty0"].append(code)
                    continue
            # 시세 조회 실패는 후보에 유지 (장 외 시간 등)
        except Exception:
            pass

        available.append(s)

    return available, excluded


def _check_single_instance():
    """
    중복 실행 방지 — 이미 살아있는 V2 프로세스가 있으면 즉시 종료.
    Returns True if safe to start, False if another instance is running.
    """
    if not os.path.exists(_LOCK_FILE):
        return True
    try:
        with open(_LOCK_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
        old_pid = int(existing.get("pid", 0))
        if old_pid and old_pid != os.getpid():
            try:
                os.kill(old_pid, 0)
                # 프로세스가 살아있음 → 중복 실행 차단
                logger.error(
                    f"[V2 SINGLE INSTANCE] ❌ 이미 실행 중인 V2 프로세스 감지 "
                    f"(PID={old_pid}) → 종료. lock 파일: {_LOCK_FILE}"
                )
                return False
            except (ProcessLookupError, OSError):
                # 이전 프로세스 사망 → stale lock 삭제 후 계속
                logger.warning(
                    f"[V2 SINGLE INSTANCE] ⚠️ 이전 lock 파일 발견 "
                    f"(PID={old_pid}, 사망) → stale lock 삭제 후 재기동"
                )
                os.remove(_LOCK_FILE)
    except Exception:
        # lock 파일 파싱 실패 → 삭제 후 계속
        try:
            os.remove(_LOCK_FILE)
        except Exception:
            pass
    return True

# ── 종료 신호 처리 ─────────────────────────────────────────────
_stop_event = threading.Event()

def _handle_signal(sig, frame):
    logger.info(f"[V2] 종료 신호 수신 (sig={sig}) → 루프 종료 중...")
    _stop_event.set()
    _remove_lock()

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ── 환경변수 읽기 ─────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default

def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def main():
    # ── 0. Single Instance 체크 ──────────────────────────────
    if not _check_single_instance():
        sys.exit(1)

    # ── lock 파일 생성 (V1 주문루프 차단 트리거) ─────────────
    _write_lock()
    # 프로그램 종료 시 항상 lock 삭제 (정상/비정상 종료 모두)
    atexit.register(_remove_lock)

    logger.info("=" * 60)
    logger.info("★ KIS 단타봇 V2 시작")
    logger.info("=" * 60)

    # ── 설정 로드 ───────────────────────────────────────────
    app_key    = _env("KIS_APP_KEY")
    app_secret = _env("KIS_APP_SECRET")
    account_no = _env("KIS_ACCOUNT_NO")

    if not all([app_key, app_secret, account_no]):
        logger.error("❌ 필수 환경변수 누락: KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO")
        sys.exit(1)

    initial_asset    = _env_float("INITIAL_ASSET",          5_000_000)
    profit_lock_krw  = _env_float("V2_PROFIT_LOCK_KRW",       300_000)
    loss_limit_krw   = _env_float("V2_LOSS_LIMIT_KRW",       -300_000)
    loop_sec         = _env_int("LOOP_INTERVAL_SEC",               30)

    # 관심 종목 목록 (국내)
    raw_kr = _env("KR_WATCH_LIST_CODES", "") or _env("WATCH_LIST_CODES", "")
    kr_watch_list = [
        {"code": c.strip(), "name": c.strip()}
        for c in raw_kr.split(",") if c.strip()
    ]

    # 관심 종목 목록 (미국)
    raw_us    = _env("US_WATCH_LIST_CODES", "")
    raw_exch  = _env("US_EXCH_CODES", "")
    us_codes  = [c.strip().upper() for c in raw_us.split(",")  if c.strip()]
    us_exchs  = [e.strip().upper() for e in raw_exch.split(",") if e.strip()]
    us_watch_list = []
    for i, code in enumerate(us_codes):
        exch = us_exchs[i] if i < len(us_exchs) else EXCH_NASD
        us_watch_list.append({"code": code, "name": code, "exch_cd": exch})

    if not kr_watch_list and not us_watch_list:
        logger.warning("⚠️ KR_WATCH_LIST_CODES / US_WATCH_LIST_CODES 모두 미설정")

    # ── 원본 감시목록 보존 (WATCHLIST_REFRESH 기준 목록) ─────
    _kr_base_list: list = list(kr_watch_list)   # 환경변수 원본
    _us_base_list: list = list(us_watch_list)   # 환경변수 원본

    # ── [V2 WATCHLIST] 진단 로그 ────────────────────────────
    _dotenv_v2  = os.path.abspath(os.path.join(_HERE, ".env"))
    _dotenv_v1  = os.path.abspath(os.path.join(_HERE, "..", "stock_trader", ".env"))
    _cwd        = os.getcwd()
    logger.info("=" * 60)
    logger.info("[V2 WATCHLIST]")
    logger.info(f"  dotenv_path(V2) = {_dotenv_v2}  존재={os.path.exists(_dotenv_v2)}")
    logger.info(f"  dotenv_path(V1) = {_dotenv_v1}  존재={os.path.exists(_dotenv_v1)}")
    logger.info(f"  cwd             = {_cwd}")
    logger.info(f"  KR 종목수       = {len(kr_watch_list)}")
    logger.info(f"  KR 종목목록     = {','.join(s['code'] for s in kr_watch_list) or '(없음)'}")
    logger.info(f"  US 종목수       = {len(us_watch_list)}")
    logger.info(f"  US 종목목록     = {','.join(s['code'] for s in us_watch_list) or '(없음)'}")
    logger.info("=" * 60)

    # ── 컴포넌트 초기화 ─────────────────────────────────────
    logger.info("🔧 컴포넌트 초기화 중...")

    # ── Adaptive Engine 초기화 ──────────────────────────────
    recorder = TradeRecorder()
    analyzer = StrategyAnalyzer()
    adjuster = WeightAdjuster(analyzer)
    logger.info("[ADAPTIVE] ✅ TradeRecorder / StrategyAnalyzer / WeightAdjuster 초기화")
    logger.info(
        f"[ADAPTIVE] 실전 가중치 범위: "
        f"min={0.80} ~ max={1.20} | WARNING 축소: 50% | DISABLED: 진입 차단"
    )

    # 국내장
    broker_kr = KRBroker(app_key, app_secret, account_no)
    reentry   = ReentryGuard()
    pnl_kr    = DailyPnLGuard(
        market          = "KR",
        profit_lock_krw = profit_lock_krw,
        loss_limit_krw  = loss_limit_krw,
    )
    # 미국장 브로커 먼저 생성 — AccountSync에 주입하여 통합 총자산 계산
    usd_krw   = _env_float("US_USD_KRW", _DEFAULT_USD_KRW)
    broker_us = USBroker(app_key, app_secret, account_no)

    account  = AccountSync(
        broker_kr,
        broker_us     = broker_us,   # ★ US 브로커 주입 → 외화예수금+해외평가 합산
        initial_asset = initial_asset,
        usd_krw       = usd_krw,
    )
    executor = ExecutionEngine(broker_kr, account, reentry)
    strategy_kr = KRStrategy(broker_kr, account, reentry, pnl_kr,
                              recorder=recorder,
                              adjuster=adjuster)   # Adaptive 실전 반영

    # 미국장 (broker_us 이미 생성됨)
    pnl_us    = DailyPnLGuard(
        market          = "US",
        profit_lock_krw = profit_lock_krw,
        loss_limit_krw  = loss_limit_krw,
    )
    strategy_us = USStrategy(broker_us, account, reentry, pnl_us,
                              usd_krw=usd_krw,
                              recorder=recorder,
                              adjuster=adjuster)   # Adaptive 실전 반영

    # DailyReporter (장 종료 시 자동 호출)
    reporter = DailyReporter(recorder, analyzer, adjuster, account)

    # ★ DailyReviewEngine — DAILY_REVIEW 자동 복기 시스템
    review_engine  = DailyReviewEngine()
    _kr_reviewed   = False   # 오늘 KR DAILY_REVIEW 완료 플래그
    _us_reviewed   = False   # 오늘 US DAILY_REVIEW 완료 플래그

    # ★ TradeSyncEngine — KIS 실체결 동기화
    trade_sync = TradeSyncEngine(broker_kr, broker_us, recorder)

    # ★ LogArchiver — 로그 자동 아카이브
    log_archiver  = LogArchiver()
    _archive_done = False   # 오늘 아카이브 완료 플래그

    # ── 서버 시작 시 trade_log 기반 재진입 차단 복원 ─────────
    logger.info("🔒 재진입 차단 복원 중...")
    reentry.restore_from_tradelog()
    reentry.purge_expired()
    blocked = reentry.get_blocked_list()
    if blocked:
        names = ", ".join(
            f"{b['name']}({b['market']},잔{int(b['remaining_hours'])}h)"
            for b in blocked[:5]
        )
        logger.info(f"🔒 현재 차단 종목 {len(blocked)}개: {names}")
    else:
        logger.info("🔓 차단 종목 없음")

    # ── 초기 실계좌 동기화 ──────────────────────────────────
    logger.info("📊 실계좌 동기화 중...")
    acc_status = account.sync(force=True)
    logger.info(
        f"💰 총자산={acc_status['total_asset']:,.0f}원 | "
        f"국내예수금={acc_status['kr_cash']:,.0f}원 | "
        f"외화예수금={acc_status['us_cash_krw']:,.0f}원 | "
        f"국내평가={acc_status['kr_eval']:,.0f}원 | "
        f"해외평가={acc_status['us_eval_krw']:,.0f}원 | "
        f"KR주문가능={acc_status['kr_orderable']:,.0f}원 | "
        f"US주문가능={acc_status['us_orderable_usd']:.2f}USD | "
        f"복리수익={acc_status['compound_ratio']:+.1f}%"
    )
    logger.info(f"📋 국내종목: {len(kr_watch_list)}개 | 미국종목: {len(us_watch_list)}개")
    logger.info(f"⏱ 루프주기: {loop_sec}초 | 수익잠금: +{profit_lock_krw:,.0f}원 | USD/KRW: {usd_krw:.0f}")

    # ── V1 차단 상태 확인 ────────────────────────────────────
    v1_blocked = os.path.exists(_LOCK_FILE)

    # ══════════════════════════════════════════════════════════
    # ★ [V2 LIVE START] — 필수 시작 로그
    # ══════════════════════════════════════════════════════════
    logger.info("=" * 60)
    logger.info("[V2 LIVE START]")
    logger.info(f"  총자산         = {acc_status['total_asset']:,.0f}원")
    logger.info(f"  국내 예수금    = {acc_status['kr_cash']:,.0f}원")
    logger.info(f"  외화 예수금    = {acc_status['us_cash_krw']:,.0f}원")
    logger.info(f"  국내 평가금액  = {acc_status['kr_eval']:,.0f}원")
    logger.info(f"  해외 평가금액  = {acc_status['us_eval_krw']:,.0f}원")
    logger.info(f"  KR 주문가능    = {acc_status['kr_orderable']:,.0f}원")
    logger.info(f"  US 주문가능    = {acc_status['us_orderable_usd']:.2f}USD")
    logger.info(f"  국내장수익목표 = +{profit_lock_krw:,.0f}원")
    logger.info(f"  미국장수익목표 = +{profit_lock_krw:,.0f}원")
    logger.info(f"  V1중지여부     = {'YES (lock 파일 존재)' if v1_blocked else 'NO (lock 파일 없음 — 경고!)'}")
    logger.info(f"  V2실행여부     = YES (PID={os.getpid()})")
    logger.info(f"  KR관심종목     = {len(kr_watch_list)}개")
    logger.info(f"  US관심종목     = {len(us_watch_list)}개")
    logger.info(f"  루프주기       = {loop_sec}초")
    logger.info("=" * 60)

    # ── KIS 실잔고 기반 US 포지션 자동 복구 ─────────────────────
    # V2 재시작 / BUY_FAIL 오판정 등으로 내부 포지션이 비어 있는 경우
    # KIS 실잔고를 읽어 PositionGuard 자동 복원
    _us_sync_cnt = strategy_us.sync_positions_from_kis()
    if _us_sync_cnt == 0:
        logger.info("[POS SYNC] US 포지션 복구: KIS 잔고와 내부 포지션 일치 (복구 불필요)")
    logger.info("✅ V2 봇 루프 시작 (국내 + 미국장)")

    loop_count   = 0
    _kr_reported = False   # 오늘 KR 일일 보고서 생성 여부
    _us_reported = False   # 오늘 US 일일 보고서 생성 여부
    _kr_reviewed = False   # 오늘 KR DAILY_REVIEW 완료 여부
    _us_reviewed = False   # 오늘 US DAILY_REVIEW 완료 여부
    _report_date = None    # 마지막 보고 날짜

    # ── 상태 알림 플래그 (1회 출력 중복 방지) ────────────────
    _notified = {
        # KR
        "kr_prepare": False, "kr_ready": False,
        "kr_open": False,    "kr_scan": False,
        "kr_close": False,   "kr_profit_lock": False,
        # US
        "us_prepare": False, "us_ready": False,
        "us_open": False,    "us_scan": False,
        "us_close": False,   "us_profit_lock": False,
        # 날짜
        "date": None,
        # 마지막 STATUS 출력 시각
        "kr_status_ts": 0.0, "us_status_ts": 0.0,
        # [ENTRY_FLOW] 마지막 출력 시각 (60초 간격)
        "kr_entry_flow_ts": 0.0, "us_entry_flow_ts": 0.0,
    }
    _STATUS_INTERVAL    = 300   # STATUS 로그 출력 간격(초)
    _ENTRY_FLOW_INTERVAL = 60   # [ENTRY_FLOW] 출력 간격(초)

    # CandidatePool — PREPARE 단계에서 필터된 주문가능 후보 풀
    _kr_avail_pool: list = []   # KR 주문가능 후보
    _us_avail_pool: list = []   # US 주문가능 후보
    _POOL_MIN = 5               # PREPARE 시 확보 목표 최솟값
    _POOL_REFILL_THRESHOLD = 3  # 루프 중 보충 트리거 임계값

    # ENTRY_FLOW 누적 집계 (루프간 유지 → 1분 타이머에 출력 후 초기화)
    _ef_kr = {"watch": 0, "avail": 0, "reentry": 0, "qty0": 0, "cond_fail": 0,
              "supplement": 0, "order": 0}
    _ef_us = {"watch": 0, "avail": 0, "reentry": 0, "qty0": 0, "cond_fail": 0,
              "supplement": 0, "order": 0}

    while not _stop_event.is_set():
        loop_count += 1
        _loop_start_ts = time.time()   # ★ 루프 시작 시각 기록 (정확한 wait 계산용)
        now_kst = datetime.now(KST)
        t       = now_kst.time()
        today   = now_kst.date()

        # ── 날짜 변경 시 보고서 + 알림 플래그 전체 리셋 ──────
        if _report_date != today:
            _kr_reported  = False
            _us_reported  = False
            _kr_reviewed  = False   # ★ DAILY_REVIEW 플래그 리셋
            _us_reviewed  = False
            _archive_done = False   # ★ 날짜 변경 시 아카이브 플래그 리셋
            _report_date  = today
            for k in list(_notified.keys()):
                if k != "date":
                    _notified[k] = False if isinstance(_notified[k], bool) else 0.0
            _notified["date"] = today
            # CandidatePool + ENTRY_FLOW 누적 통계도 날짜 변경 시 초기화
            _kr_avail_pool = []
            _us_avail_pool = []
            _ef_kr = {"watch": 0, "avail": 0, "reentry": 0, "qty0": 0,
                      "cond_fail": 0, "supplement": 0, "order": 0}
            _ef_us = {"watch": 0, "avail": 0, "reentry": 0, "qty0": 0,
                      "cond_fail": 0, "supplement": 0, "order": 0}
            # 날짜 변경 시 환경변수 원본으로 감시목록 초기화
            kr_watch_list = list(_kr_base_list)
            us_watch_list = list(_us_base_list)

        try:
            # ── 실계좌 동기화 (매 루프) ───────────────────────
            account.sync()

            # ★ KIS 실체결 동기화 (60초 간격)
            try:
                trade_sync.run_sync()
            except Exception as _tse:
                logger.warning(f"[TradeSync] 동기화 오류: {_tse}")

            # ★ 로그 아카이브 (00:01~00:05, 하루 1회)
            if dtime(0, 1) <= t <= dtime(0, 5) and not _archive_done:
                try:
                    log_archiver.run()
                    # 대용량 실시간 로그 로테이션
                    import os as _os
                    for _lf in ["logs/v2_live_out.log", "logs/v2_live_err.log"]:
                        _lpath = _os.path.join(_os.path.dirname(__file__), _lf)
                        log_archiver.rotate_live_log(_lpath, max_mb=50.0)
                    _archive_done = True
                except Exception as _ae:
                    logger.warning(f"[LogArchiver] 실행 오류: {_ae}")

            # ════════════════════════════════════════════════
            # ★ 상태 알림 로그 — KR
            # ════════════════════════════════════════════════
            # KR PREPARE: 08:55~09:00 (개장 5분 전)
            if dtime(8, 55) <= t < dtime(9, 0) and not _notified["kr_prepare"]:
                # ── 1. KIS 토큰 상태 확인 및 만료 임박 시 자동 재발급 ──
                _kr_tok_ok      = False
                _kr_tok_label   = "❌ 없음"
                _kr_tok_expire  = ""
                _kr_tok_remain  = 0
                _kr_tok_reissue = ""
                try:
                    _expires = broker_kr._token_expires  # datetime | None
                    _now_dt  = datetime.now()
                    if _expires is None:
                        # 토큰 없음 → 즉시 발급
                        broker_kr.token()
                        _expires = broker_kr._token_expires
                        _kr_tok_reissue = " | 🔄 재발급=✅ 성공"
                    _kr_tok_remain = int((_expires - _now_dt).total_seconds() / 60)
                    if _kr_tok_remain < 60:
                        # 잔여 60분 미만 → 선제 재발급
                        broker_kr._issue_token()
                        _expires       = broker_kr._token_expires
                        _kr_tok_remain = int((_expires - _now_dt).total_seconds() / 60)
                        _kr_tok_reissue = " | 🔄 재발급=✅ 성공(만료임박)"
                    _kr_tok_expire = _expires.strftime("%H:%M")
                    _kr_tok_ok     = True
                    _kr_tok_label  = f"✅ 정상(잔여 {_kr_tok_remain}분, 만료 {_kr_tok_expire})"
                except Exception as _e:
                    _kr_tok_label   = f"❌ 오류({_e})"
                    _kr_tok_reissue = " | 🔄 재발급=❌ 실패"
                # ── 2. 계좌 상태 (force 동기화) ──────────────────────
                _kr_bal_ok    = False
                _kr_cash      = 0
                _kr_orderable = 0
                _kr_holdings  = 0
                try:
                    _bal = broker_kr.get_balance(force=True)
                    if _bal.get("cash", -1) >= 0:
                        _kr_cash      = _bal["cash"]
                        _kr_orderable = broker_kr.get_orderable_cash()
                        _kr_holdings  = len(_bal.get("holdings", []))
                        _kr_bal_ok    = True
                except Exception:
                    pass
                _kr_bal_label = "✅ 정상" if _kr_bal_ok else "❌ 오류"
                # ── 3. 시세조회 테스트 (관심종목 첫 번째 종목) ────────
                _kr_price_ok = False
                try:
                    if kr_watch_list:
                        _tp = broker_kr.get_price(kr_watch_list[0]["code"], force=True)
                        _kr_price_ok = bool(_tp.get("price", 0) > 0)
                except Exception:
                    pass
                _kr_price_label = "✅ 정상" if _kr_price_ok else "⚠️ 확인불가"
                # ── 4. 주문준비 (토큰 + 계좌 모두 OK) ────────────────
                _kr_order_label = "✅ 완료" if (_kr_tok_ok and _kr_bal_ok) else "❌ 점검필요"
                # ── 로그 출력 ─────────────────────────────────────────
                logger.info(
                    f"[KR PREPARE] 국내장 개장 5분 전\n"
                    f"  토큰       = {_kr_tok_label}{_kr_tok_reissue}\n"
                    f"  계좌       = {_kr_bal_label}\n"
                    f"  예수금     = {_kr_cash:,.0f}원\n"
                    f"  주문가능   = {_kr_orderable:,.0f}원\n"
                    f"  관심종목   = {len(kr_watch_list)}개\n"
                    f"  시세조회   = {_kr_price_label}\n"
                    f"  주문준비   = {_kr_order_label}"
                )
                # ── 5. CandidatePool 사전 확보 ─────────────────────
                try:
                    _kr_avail_pool, _kr_excl = _build_orderable_candidates(
                        watch_list = kr_watch_list,
                        reentry    = reentry,
                        account    = account,
                        broker     = broker_kr,
                        blacklist  = set(),   # KR은 블랙리스트 없음
                        market     = "KR",
                    )
                    _pool_log = (
                        f"[KR PREPARE] CandidatePool 확보 완료 | "
                        f"주문가능={len(_kr_avail_pool)}개 / 전체={len(kr_watch_list)}개 | "
                        f"재진입차단={len(_kr_excl['reentry'])} | "
                        f"수량0={len(_kr_excl['qty0'])}"
                    )
                    if len(_kr_avail_pool) < _POOL_MIN:
                        logger.warning(
                            _pool_log +
                            f" ⚠️ 목표({_POOL_MIN}개) 미달 — 감시종목 검토 필요"
                        )
                    else:
                        logger.info(_pool_log)
                except Exception as _pe:
                    logger.warning(f"[KR PREPARE] CandidatePool 확보 실패: {_pe}")
                _notified["kr_prepare"] = True

            # KR READY: 09:00~09:01 (개장 직전)
            if dtime(9, 0) <= t < dtime(9, 2) and not _notified["kr_ready"]:
                logger.info(
                    f"[KR READY] 국내장 개장 1분 전 | "
                    f"관심종목={len(kr_watch_list)}개 로드 완료 | 스크리닝 대기 중"
                )
                _notified["kr_ready"] = True

            # KR OPEN: 09:01 (개장 알림)
            if dtime(9, 1) <= t < dtime(9, 3) and not _notified["kr_open"]:
                logger.info(
                    f"🔔 [KR OPEN] 국내장 개장 | PRIME 모드 시작 | "
                    f"관심종목={len(kr_watch_list)}개 | 집중 스크리닝 시작"
                )
                _notified["kr_open"] = True

            # KR SCAN 알림: 09:01~14:30 사이 5분 간격 → STATUS 로그
            if dtime(9, 1) <= t < dtime(14, 30):
                now_ts = time.time()
                if now_ts - _notified["kr_status_ts"] >= _STATUS_INTERVAL:
                    acc_s = account.sync()
                    holdings_kr = [h for h in account.holdings
                                   if not h.get("code", "").startswith("US")]
                    realized_kr = pnl_kr.realized_pnl if hasattr(pnl_kr, "realized_pnl") else 0
                    eval_kr = sum(h.get("eval_profit", 0) for h in holdings_kr)
                    logger.info(
                        f"[KR STATUS] 상태=TRADING | "
                        f"감시종목={len(kr_watch_list)}개 | "
                        f"보유종목={len(holdings_kr)}개 | "
                        f"실현손익={realized_kr:+,.0f}원 | "
                        f"평가손익={eval_kr:+,.0f}원 | "
                        f"루프={loop_count}회"
                    )
                    _notified["kr_status_ts"] = now_ts

            # KR PROFIT_LOCK 알림 (수익잠금 발동 시 1회)
            if not _notified["kr_profit_lock"] and hasattr(pnl_kr, "is_locked") and pnl_kr.is_locked():
                realized_kr = pnl_kr.realized_pnl if hasattr(pnl_kr, "realized_pnl") else 0
                logger.info(
                    f"🎯 [KR PROFIT LOCK] 목표수익 +{profit_lock_krw:,.0f}원 달성 "
                    f"(실현={realized_kr:+,.0f}원) | 신규매수 중단 | 보유종목 관리모드 전환"
                )
                _notified["kr_profit_lock"] = True

            # KR CLOSE: 15:20 강제청산 후 알림
            if dtime(15, 21) <= t < dtime(15, 25) and not _notified["kr_close"]:
                realized_kr = pnl_kr.realized_pnl if hasattr(pnl_kr, "realized_pnl") else 0
                trade_cnt_kr = getattr(pnl_kr, "trade_count", 0)
                logger.info(
                    f"🏁 [KR CLOSE] 국내장 종료 | "
                    f"오늘 거래횟수={trade_cnt_kr}회 | "
                    f"실현손익={realized_kr:+,.0f}원"
                )
                _notified["kr_close"] = True

            # ════════════════════════════════════════════════
            # ★ 상태 알림 로그 — US
            # ════════════════════════════════════════════════
            # US PREPARE: 22:25~22:30 (개장 5분 전, KST 서머타임 EDT 기준)
            if dtime(22, 25) <= t < dtime(22, 30) and not _notified["us_prepare"]:
                # ── 1. KIS 토큰 상태 확인 및 만료 임박 시 자동 재발급 ──
                _us_tok_ok      = False
                _us_tok_label   = "❌ 없음"
                _us_tok_expire  = ""
                _us_tok_remain  = 0
                _us_tok_reissue = ""
                try:
                    _expires = broker_us._token_expires  # datetime | None
                    _now_dt  = datetime.now()
                    if _expires is None:
                        # 토큰 없음 → 즉시 발급
                        broker_us.token()
                        _expires = broker_us._token_expires
                        _us_tok_reissue = " | 🔄 재발급=✅ 성공"
                    _us_tok_remain = int((_expires - _now_dt).total_seconds() / 60)
                    if _us_tok_remain < 60:
                        # 잔여 60분 미만 → 선제 재발급
                        broker_us._issue_token()
                        _expires       = broker_us._token_expires
                        _us_tok_remain = int((_expires - _now_dt).total_seconds() / 60)
                        _us_tok_reissue = " | 🔄 재발급=✅ 성공(만료임박)"
                    _us_tok_expire = _expires.strftime("%H:%M")
                    _us_tok_ok     = True
                    _us_tok_label  = f"✅ 정상(잔여 {_us_tok_remain}분, 만료 {_us_tok_expire})"
                except Exception as _e:
                    _us_tok_label   = f"❌ 오류({_e})"
                    _us_tok_reissue = " | 🔄 재발급=❌ 실패"
                # ── 2. 계좌 상태 (force 동기화) ──────────────────────
                _us_bal_ok    = False
                _us_usd_bal   = 0.0
                _us_orderable = 0.0
                _us_holdings  = 0
                try:
                    _bal = broker_us.get_balance(force=True)
                    if _bal.get("ok", False):
                        _us_usd_bal   = _bal.get("usd_balance", 0.0)
                        _us_orderable = broker_us.get_orderable_usd()
                        _us_holdings  = len(_bal.get("holdings", []))
                        _us_bal_ok    = True
                except Exception:
                    pass
                _us_bal_label = "✅ 정상" if _us_bal_ok else "❌ 오류"
                # ── 3. 시세조회 테스트 (관심종목 첫 번째 종목) ────────
                _us_price_ok = False
                try:
                    if us_watch_list:
                        _tp = broker_us.get_price(
                            us_watch_list[0]["code"],
                            us_watch_list[0].get("exch_cd", "NASD"),
                            force=True,
                        )
                        _us_price_ok = bool(_tp.get("cur_price", 0) > 0)
                except Exception:
                    pass
                _us_price_label = "✅ 정상" if _us_price_ok else "⚠️ 확인불가"
                # ── 4. 주문준비 (토큰 + 계좌 모두 OK) ────────────────
                _us_order_label = "✅ 완료" if (_us_tok_ok and _us_bal_ok) else "❌ 점검필요"
                # ── 로그 출력 ─────────────────────────────────────────
                logger.info(
                    f"[US PREPARE] 미국장 개장 5분 전\n"
                    f"  토큰         = {_us_tok_label}{_us_tok_reissue}\n"
                    f"  계좌         = {_us_bal_label}\n"
                    f"  예수금(USD)  = ${_us_usd_bal:,.2f}\n"
                    f"  주문가능(USD)= ${_us_orderable:,.2f}\n"
                    f"  관심종목     = {len(us_watch_list)}개\n"
                    f"  시세조회     = {_us_price_label}\n"
                    f"  주문준비     = {_us_order_label}"
                )
                # ── 5. CandidatePool 사전 확보 ─────────────────────
                try:
                    _us_avail_pool, _us_excl = _build_orderable_candidates(
                        watch_list = us_watch_list,
                        reentry    = reentry,
                        account    = account,
                        broker     = broker_us,
                        blacklist  = set(_KIS_ORDER_BLACKLIST_MAIN.keys()),
                        usd_krw    = usd_krw,
                        market     = "US",
                    )
                    _us_pool_log = (
                        f"[US PREPARE] CandidatePool 확보 완료 | "
                        f"주문가능={len(_us_avail_pool)}개 / 전체={len(us_watch_list)}개 | "
                        f"재진입차단={len(_us_excl['reentry'])} | "
                        f"수량0={len(_us_excl['qty0'])} | "
                        f"블랙리스트={len(_us_excl['blacklist'])}"
                    )
                    if len(_us_avail_pool) < _POOL_MIN:
                        logger.warning(
                            _us_pool_log +
                            f" ⚠️ 목표({_POOL_MIN}개) 미달 — 감시종목 검토 필요"
                        )
                    else:
                        logger.info(_us_pool_log)
                except Exception as _upe:
                    logger.warning(f"[US PREPARE] CandidatePool 확보 실패: {_upe}")
                _notified["us_prepare"] = True

            # US READY: 22:29~22:31
            if dtime(22, 29) <= t < dtime(22, 32) and not _notified["us_ready"]:
                logger.info(
                    f"[US READY] 미국장 개장 1분 전 | "
                    f"관심종목 {len(us_watch_list)}개 로드 완료 | 스크리닝 대기 중"
                )
                _notified["us_ready"] = True

            # US OPEN: 22:30~22:33
            if dtime(22, 30) <= t < dtime(22, 35) and not _notified["us_open"]:
                logger.info(
                    f"🔔 [US OPEN] 미국장 개장 | PRIME 모드 시작 | "
                    f"관심종목={len(us_watch_list)}개 | 집중 스크리닝 시작"
                )
                _notified["us_open"] = True

            # US STATUS: 22:30~05:00 사이 5분 간격
            if broker_us.is_market_open(now_kst):
                now_ts = time.time()
                if now_ts - _notified["us_status_ts"] >= _STATUS_INTERVAL:
                    # account.sync()가 KR+US 잔고를 모두 통합 조회 (단일 경로)
                    # broker_us.get_balance() 별도 호출 제거 — us_eval_krw를 acc_s에서 직접 사용
                    acc_s = account.sync()
                    realized_us = pnl_us.realized_pnl if hasattr(pnl_us, "realized_pnl") else 0
                    eval_us_krw = int(acc_s.get("us_eval_krw", 0))
                    # 보유종목 수: strategy_us가 관리하는 내부 포지션 수 사용
                    _us_pos_cnt = len(strategy_us._positions) if strategy_us else 0
                    logger.info(
                        f"[US STATUS] 상태=TRADING | "
                        f"감시종목={len(us_watch_list)}개 | "
                        f"보유종목={_us_pos_cnt}개 | "
                        f"실현손익={realized_us:+,.0f}원 | "
                        f"평가손익={eval_us_krw:+,.0f}원 | "
                        f"루프={loop_count}회"
                    )
                    _notified["us_status_ts"] = now_ts

            # US PROFIT_LOCK 알림 (1회)
            if not _notified["us_profit_lock"] and hasattr(pnl_us, "is_locked") and pnl_us.is_locked():
                realized_us = pnl_us.realized_pnl if hasattr(pnl_us, "realized_pnl") else 0
                logger.info(
                    f"🎯 [US PROFIT LOCK] 목표수익 +{profit_lock_krw:,.0f}원 달성 "
                    f"(실현={realized_us:+,.0f}원) | 신규매수 중단 | 보유종목 관리모드 전환"
                )
                _notified["us_profit_lock"] = True

            # US CLOSE: 05:01~05:05 KST (EDT 기준 장 마감 직후)
            if dtime(5, 1) <= t < dtime(5, 6) and not _notified["us_close"]:
                realized_us = pnl_us.realized_pnl if hasattr(pnl_us, "realized_pnl") else 0
                trade_cnt_us = getattr(pnl_us, "trade_count", 0)
                logger.info(
                    f"🏁 [US CLOSE] 미국장 종료 | "
                    f"오늘 거래횟수={trade_cnt_us}회 | "
                    f"실현손익={realized_us:+,.0f}원"
                )
                _notified["us_close"] = True

            # ── 15:20 국내장 강제 청산 + 미체결 BUY 취소 ──────
            if t >= BUY_HARD_STOP:
                _force_close_kr(strategy_kr, account, executor)
                # 미체결 BUY 취소
                n_cancel = executor.cancel_all_pending_buy()
                if n_cancel:
                    logger.info(f"[15:20] 국내 미체결 BUY {n_cancel}건 취소")

            # ── KR 일일 보고서 (15:30 이후 1회) ──────────────
            if dtime(15, 30) <= t <= dtime(15, 45) and not _kr_reported:
                try:
                    logger.info("[ADAPTIVE] KR 장 종료 → 일일 학습 보고서 생성 중...")
                    reporter.generate("KR")
                    _kr_reported = True
                    logger.info("[ADAPTIVE] ✅ KR 일일 보고서 완료")
                except Exception as _re:
                    logger.warning(f"[ADAPTIVE] KR 보고서 실패: {_re}")

            # ── KR DAILY_REVIEW (15:35 이후 1회) ─────────────
            # DailyReporter 완료 후 5분 뒤에 실행하여 DB 반영 보장
            if dtime(15, 35) <= t <= dtime(15, 55) and not _kr_reviewed:
                try:
                    logger.info("[DAILY_REVIEW] KR 장 종료 → 복기 보고서 생성 중...")
                    review_engine.run("KR")
                    _kr_reviewed = True
                    logger.info("[DAILY_REVIEW] ✅ KR 복기 완료")
                except Exception as _rve:
                    logger.warning(f"[DAILY_REVIEW] KR 복기 실패: {_rve}")

            # ── 15:30~22:00 국내장 완전 대기 (미국장 개장 전) ──
            # KR 장 마감 후 ~ 미국장 개장 전 구간: 전략 실행 건너뜀
            kr_closed = dtime(15, 30) <= t <= dtime(21, 59)
            if kr_closed and not us_watch_list:
                # 미국장 종목도 없으면 슬립 후 다음 루프
                logger.debug(f"[{t.strftime('%H:%M')}] 15:30~22:00 — 모든 장 비활성, 대기")
                _stop_event.wait(timeout=loop_sec * 4)
                continue

            # ── 국내장 종목별 전략 실행 ──────────────────────
            # 15:30 이후는 국내장 전략 실행 완전 차단
            if not kr_closed:
                # ── [WATCHLIST_REFRESH] — 재진입차단 종목 슬롯 해제 + 예비 보충 ──
                # 매 루프: 차단 종목을 감시목록에서 제외하고 원본 목록으로 보충
                _wr_before       = len(kr_watch_list)
                _blocked_codes_wr = {b["code"] for b in reentry.get_blocked_list()
                                     if b.get("market", "KR") == "KR"}
                # 차단 종목 제외
                _kr_active = [s for s in kr_watch_list
                              if s["code"] not in _blocked_codes_wr]
                _kr_excluded_cnt = _wr_before - len(_kr_active)
                # 원본에서 미차단 종목으로 빈 슬롯 보충
                _kr_supplement_codes = {s["code"] for s in _kr_active}
                _kr_added = []
                for _bs in _kr_base_list:
                    if _bs["code"] not in _blocked_codes_wr \
                            and _bs["code"] not in _kr_supplement_codes:
                        _kr_active.append(_bs)
                        _kr_supplement_codes.add(_bs["code"])
                        _kr_added.append(_bs["code"])
                kr_watch_list = _kr_active
                if _kr_excluded_cnt > 0 or _kr_added:
                    logger.info(
                        f"[WATCHLIST_REFRESH] 시장=KR | "
                        f"기존감시={_wr_before} | "
                        f"차단제외={_kr_excluded_cnt} | "
                        f"신규보충={len(_kr_added)} | "
                        f"최종감시={len(kr_watch_list)}"
                    )

                # ── [HWM_UPDATE] KR 보유 포지션 max_pct / min_pct 갱신 ──
                # account.holdings(실계좌 캐시)에서 현재가를 가져와 루프마다 기록
                # ★ PositionGuard 내부 HWM(_hwm_pct)도 동기화 → PROFIT_PROTECT/WEAK_ENTRY_EXIT 활용
                try:
                    _kr_holdings_map = {h["code"]: h for h in account.holdings}
                    for _pos_code, _pg in list(strategy_kr._positions.items()):
                        _h = _kr_holdings_map.get(_pos_code)
                        if _h and _h.get("cur_price", 0) > 0 and _pg.avg_price > 0:
                            _cur_p = float(_h["cur_price"])
                            # DB 기록 (trade_sync)
                            trade_sync.update_position_extremes(
                                code        = _pos_code,
                                cur_price   = _cur_p,
                                entry_price = float(_pg.avg_price),
                                market      = "KR",
                            )
                            # PositionGuard 내부 HWM 갱신 (PROFIT_PROTECT 활용)
                            _pg.update_hwm(_pg.net_pct(_cur_p))
                except Exception as _hwm_e:
                    logger.debug(f"[HWM_UPDATE] KR 갱신 오류: {_hwm_e}")

                # ── [ENTRY_SUMMARY] 집계용 카운터 ──────────────
                _es_reentry  = 0   # 재진입 차단
                _es_exclude  = 0   # 수량0 사전 제외
                _es_cond_fail = 0  # 조건 미달
                _es_order    = 0   # 실제 주문 (BUY)

                for stock in kr_watch_list:
                    if _stop_event.is_set():
                        break
                    try:
                        result = strategy_kr.run(stock)
                        action = result.get("action", "SKIP")
                        reason = result.get("reason", "")
                        if action == "BUY":
                            _es_order += 1
                        elif action == "SKIP":
                            if "재진입 차단" in reason or "⛔" in reason:
                                _es_reentry += 1
                            elif "수량0" in reason or "ENTRY_EXCLUDE" in reason or "자금부족" in reason or "주문가능금액 없음" in reason:
                                _es_exclude += 1
                            else:
                                _es_cond_fail += 1
                        if action not in ("SKIP", "HOLD"):
                            logger.info(
                                f"[KR 루프{loop_count}] "
                                f"{stock['name']}({stock['code']}) "
                                f"→ {action} | {reason}"
                            )
                    except Exception as e:
                        logger.error(
                            f"[KR 루프{loop_count}] "
                            f"{stock.get('name','?')}({stock.get('code','?')}) "
                            f"전략 오류: {e}", exc_info=True
                        )

                # ── [ENTRY_SUMMARY] 출력 (KR 장 운영 시간 + 1분마다) ──
                _es_orderable = len(kr_watch_list) - _es_reentry - _es_exclude
                _blocked_cnt  = len(reentry.get_blocked_list())
                logger.info(
                    f"[ENTRY_SUMMARY] 시장=KR | "
                    f"감시종목={len(kr_watch_list)} | "
                    f"재진입차단={_es_reentry}(전체차단={_blocked_cnt}) | "
                    f"자금부족/수량0={_es_exclude} | "
                    f"조건미달={_es_cond_fail} | "
                    f"주문가능≈{max(0, _es_orderable)} | "
                    f"실제주문={_es_order}"
                )

                # ── CandidatePool 동기화 (개장 후 매 루프 갱신) ─────
                # PREPARE 이후에도 재진입/수량0 상태가 변할 수 있으므로 갱신
                try:
                    _kr_avail_pool, _kr_excl_cur = _build_orderable_candidates(
                        watch_list = kr_watch_list,
                        reentry    = reentry,
                        account    = account,
                        broker     = broker_kr,
                        blacklist  = set(),
                        market     = "KR",
                    )
                except Exception:
                    _kr_excl_cur = {"reentry": [], "qty0": [], "blacklist": []}

                # ── 후보 부족 시 보충 + [NO_ENTRY_REASON] ──────────
                _kr_supplement = 0
                _kr_pool_size  = len(_kr_avail_pool)
                if _kr_pool_size < _POOL_REFILL_THRESHOLD:
                    # 현재 차단 목록 제외 후 감시종목 내 미차단 종목으로 보충
                    _blocked_codes = {b["code"] for b in reentry.get_blocked_list()}
                    for _s in kr_watch_list:
                        if _s["code"] not in _blocked_codes:
                            if not any(p["code"] == _s["code"] for p in _kr_avail_pool):
                                _kr_avail_pool.append(_s)
                                _kr_supplement += 1
                    if _kr_supplement > 0:
                        logger.info(
                            f"[NO_ENTRY_REASON] 시장=KR | "
                            f"사유=주문가능후보부족({_kr_pool_size}개<{_POOL_REFILL_THRESHOLD}) | "
                            f"대응=후보보충 | 보충={_kr_supplement}개 | "
                            f"보충후풀={len(_kr_avail_pool)}개"
                        )
                    else:
                        # 보충 불가 → 원인 분류
                        if _es_exclude > 0 and _es_reentry == 0:
                            _nr_reason = "자금부족/수량0"
                            _nr_action = "자금부족"
                        elif _es_reentry >= len(kr_watch_list) * 0.7:
                            _nr_reason = f"감시종목 대부분 재진입차단({_es_reentry}개)"
                            _nr_action = "대기"
                        else:
                            _nr_reason = f"조건미달({_es_cond_fail}개)"
                            _nr_action = "조건미달"
                        logger.info(
                            f"[NO_ENTRY_REASON] 시장=KR | "
                            f"사유={_nr_reason} | "
                            f"대응={_nr_action}"
                        )
                elif _es_order == 0 and dtime(9, 1) <= t < dtime(14, 30):
                    # 후보는 있지만 실제 주문 없을 때 이유 요약
                    if _es_cond_fail > 0:
                        logger.info(
                            f"[NO_ENTRY_REASON] 시장=KR | "
                            f"사유=조건미달({_es_cond_fail}개) | "
                            f"대응=조건미달"
                        )

                # ── ENTRY_FLOW 누적 업데이트 ────────────────────────
                _ef_kr["watch"]      = len(kr_watch_list)
                _ef_kr["avail"]      = max(_ef_kr["avail"], len(_kr_avail_pool))
                _ef_kr["reentry"]    = max(_ef_kr["reentry"], _es_reentry)
                _ef_kr["qty0"]       = max(_ef_kr["qty0"], _es_exclude)
                _ef_kr["cond_fail"]  = max(_ef_kr["cond_fail"], _es_cond_fail)
                _ef_kr["supplement"] += _kr_supplement
                _ef_kr["order"]      += _es_order

                # ── [ENTRY_FLOW] 1분 간격 출력 ──────────────────────
                _now_ts = time.time()
                if _now_ts - _notified["kr_entry_flow_ts"] >= _ENTRY_FLOW_INTERVAL:
                    logger.info(
                        f"[ENTRY_FLOW] 시장=KR | "
                        f"감시종목={_ef_kr['watch']} | "
                        f"주문가능후보={_ef_kr['avail']} | "
                        f"재진입차단={_ef_kr['reentry']} | "
                        f"수량0제외={_ef_kr['qty0']} | "
                        f"조건미달={_ef_kr['cond_fail']} | "
                        f"보충후보={_ef_kr['supplement']} | "
                        f"실제주문={_ef_kr['order']}"
                    )
                    _notified["kr_entry_flow_ts"] = _now_ts
                    # 1분 누적 후 초기화
                    _ef_kr = {"watch": 0, "avail": 0, "reentry": 0, "qty0": 0,
                              "cond_fail": 0, "supplement": 0, "order": 0}

            # ── 미국장 종목별 전략 실행 ───────────────────────
            _us_mkt_open = broker_us.is_market_open(now_kst)
            _sc_cands: list = []   # 스크리너 후보 (매 루프 초기화)

            # ★ 영구차단 종목 집합 (이후 모든 US 로직에서 공용)
            _perm_banned = getattr(broker_us, "_perm_banned", set())

            # ── [WATCHLIST_REFRESH] — US 재진입차단 종목 슬롯 해제 ──────
            if _us_mkt_open:
                _wr_us_before     = len(us_watch_list)
                _blocked_codes_us_wr = {b["code"] for b in reentry.get_blocked_list()
                                        if b.get("market", "US") == "US"}
                _us_active = [s for s in us_watch_list
                              if s["code"] not in _blocked_codes_us_wr
                              and s["code"] not in _perm_banned]
                _us_excl_cnt = _wr_us_before - len(_us_active)
                # 원본에서 보충 (블랙리스트/차단 제외)
                _bl_keys_wr = set(_KIS_ORDER_BLACKLIST_MAIN.keys())
                _us_active_codes = {s["code"] for s in _us_active}
                _us_added = []
                for _bs in _us_base_list:
                    if _bs["code"] in _bl_keys_wr:
                        continue
                    if _bs["code"] in _blocked_codes_us_wr:
                        continue
                    if _bs["code"] not in _us_active_codes:
                        _us_active.append(_bs)
                        _us_active_codes.add(_bs["code"])
                        _us_added.append(_bs["code"])
                us_watch_list = _us_active
                if _us_excl_cnt > 0 or _us_added:
                    logger.info(
                        f"[WATCHLIST_REFRESH] 시장=US | "
                        f"기존감시={_wr_us_before} | "
                        f"차단제외={_us_excl_cnt} | "
                        f"신규보충={len(_us_added)} | "
                        f"최종감시={len(us_watch_list)}"
                    )


            # ★ US 스크리너 결과 파일 읽기 → us_watch_list 갱신 (매 루프 체크)
            # v1-dashboard의 screener가 작성하는 us_intraday.json 공유
            _screener_path = os.path.join(
                os.path.dirname(_HERE), "stock_trader", "data", "us_intraday.json"
            )
            try:
                if os.path.exists(_screener_path):
                    with open(_screener_path, "r", encoding="utf-8") as _sf:
                        _sc_data = json.load(_sf)
                    _sc_ts   = _sc_data.get("analyzed_at", "")
                    _sc_cands = _sc_data.get("top_buy", [])
                    # 새 후보가 있으면 us_watch_list에 반영 (중복 제외)
                    _sc_added = 0
                    _perm_banned_sc = getattr(broker_us, "_perm_banned", set())
                    for _sc in _sc_cands:
                        _sc_code = (_sc.get("symbol", "") or _sc.get("code", "")).upper()
                        if not _sc_code:
                            continue
                        if _sc_code in _perm_banned_sc:
                            continue
                        if _sc_code in _KIS_ORDER_BLACKLIST_MAIN:
                            continue
                        if not any(s["code"] == _sc_code for s in us_watch_list):
                            us_watch_list.append({
                                "code":    _sc_code,
                                "name":    _sc.get("name", _sc_code),
                                "exch_cd": _sc.get("excd", "NASD"),
                            })
                            _sc_added += 1
                    if _sc_added > 0:
                        logger.info(
                            f"[US SCREENER→WATCH] 스크리너 신규후보 {_sc_added}개 감시종목 추가 | "
                            f"총={len(us_watch_list)}개 | 업데이트={_sc_ts}"
                        )
            except Exception as _sce:
                logger.debug(f"[US SCREENER] 파일 읽기 실패(무시): {_sce}")

            if us_watch_list and _us_mkt_open:
                logger.info(
                    f"[US LOOP START] 루프={loop_count} | "
                    f"종목수={len(us_watch_list)} | "
                    f"시각={now_kst.strftime('%H:%M:%S')}"
                )
                # ★ 배치 현재가 프리패치 — 루프 전체 종목을 yfinance 1회 조회로 캐시
                try:
                    _pf_t0 = time.time()
                    _pf_n  = broker_us.prefetch_prices(us_watch_list)
                    _pf_dt = time.time() - _pf_t0
                    logger.info(
                        f"[US PREFETCH] {_pf_n}/{len(us_watch_list)}개 현재가 캐시 완료 "
                        f"({_pf_dt:.1f}초) — 이후 개별 조회 캐시 히트"
                    )
                except Exception as _pfe:
                    logger.warning(f"[US PREFETCH] 배치 실패(개별 조회 진행): {_pfe}")

            # ★ 영구차단 종목 워치리스트에서 제거 (APBK0656/APBK1672 에러 발생 시)
            # _perm_banned는 위 WATCHLIST_REFRESH 블록에서 이미 선언됨
            if _perm_banned:
                _before = len(us_watch_list)
                us_watch_list = [s for s in us_watch_list if s["code"] not in _perm_banned]
                _removed = _before - len(us_watch_list)
                if _removed > 0:
                    logger.warning(
                        f"[US PERM_BAN 제거] 영구차단 종목 {_removed}개 워치리스트에서 삭제: "
                        f"{_perm_banned} → 남은종목={len(us_watch_list)}개"
                    )

            # ── [HWM_UPDATE] US 보유 포지션 max_pct / min_pct 갱신 ──
            # strategy_us._us_holdings_cache(55초 TTL 캐시)에서 현재가 활용
            try:
                _us_hc = getattr(strategy_us, "_us_holdings_cache", {})
                for _pos_code, _pg in list(strategy_us._positions.items()):
                    _uh = _us_hc.get(_pos_code)
                    _cur_p = float(_uh.get("cur_price", 0)) if _uh else 0.0
                    if _cur_p <= 0:
                        # 캐시 미적재 시 price_data 직접 조회 (fallback)
                        try:
                            _pd = broker_us.get_price(_pos_code,
                                                      getattr(strategy_us._positions[_pos_code],
                                                              "exch_cd", "NASD"))
                            _cur_p = float(_pd.get("cur_price", 0)) if _pd.get("ok") else 0.0
                        except Exception:
                            _cur_p = 0.0
                    if _cur_p > 0 and _pg.avg_price > 0:
                        trade_sync.update_position_extremes(
                            code        = _pos_code,
                            cur_price   = _cur_p,
                            entry_price = float(_pg.avg_price),
                            market      = "US",
                        )
                        # PositionGuard 내부 HWM 갱신
                        _pg.update_hwm(_pg.net_pct(_cur_p))
            except Exception as _hwm_e:
                logger.debug(f"[HWM_UPDATE] US 갱신 오류: {_hwm_e}")

            # ── [US_ENTRY_SUMMARY] 집계용 카운터 ───────────────
            _ues_reentry  = 0
            _ues_exclude  = 0
            _ues_cond_fail = 0
            _ues_order    = 0
            _ues_block_reasons: list = []

            for stock in us_watch_list:
                if _stop_event.is_set():
                    break
                code = stock["code"]
                # ★ 영구차단 종목이면 즉시 SKIP (이미 위에서 걸러지지만 이중방어)
                if code in _perm_banned:
                    continue
                try:
                    result = strategy_us.run(stock)
                    action = result.get("action", "SKIP")
                    reason = result.get("reason", "")
                    if action == "BUY":
                        _ues_order += 1
                    elif action == "SKIP":
                        if "재진입 차단" in reason or "⛔" in reason:
                            _ues_reentry += 1
                            _ues_block_reasons.append(f"{code}:재진입차단")
                        elif "수량0" in reason or "ENTRY_EXCLUDE" in reason or "자금부족" in reason or "주문가능금액 없음" in reason:
                            _ues_exclude += 1
                            _ues_block_reasons.append(f"{code}:수량0/자금부족")
                        elif "미국 정규장 외" in reason or "주말" in reason:
                            pass   # 장외 시간 SKIP은 집계 제외
                        else:
                            _ues_cond_fail += 1
                            _ues_block_reasons.append(f"{code}:{reason[:20]}")
                    if action not in ("SKIP", "HOLD"):
                        logger.info(
                            f"[US 루프{loop_count}] "
                            f"{stock['name']}({stock['code']}) "
                            f"→ {action} | {reason}"
                        )
                    else:
                        logger.debug(
                            f"[US 루프{loop_count}] "
                            f"{stock['name']}({stock['code']}) "
                            f"→ {action} | {reason}"
                        )
                except Exception as e:
                    logger.error(
                        f"[US 루프{loop_count}] "
                        f"{stock.get('name','?')}({stock.get('code','?')}) "
                        f"전략 오류: {e}", exc_info=True
                    )

            # ── [US_ENTRY_SUMMARY] 출력 (US 장 운영 시간 또는 포지션 있을 때) ──
            if _us_mkt_open or strategy_us._positions:
                _block_str = " | ".join(_ues_block_reasons[:5]) if _ues_block_reasons else "없음"
                logger.info(
                    f"[US_ENTRY_SUMMARY] 시장=US | "
                    f"스크리너후보={len(_sc_cands)} | "
                    f"감시종목={len(us_watch_list)} | "
                    f"재진입차단={_ues_reentry} | "
                    f"자금부족/수량0={_ues_exclude} | "
                    f"조건미달={_ues_cond_fail} | "
                    f"실제주문={_ues_order} | "
                    f"차단사유={_block_str}"
                )

                # ── CandidatePool 동기화 (US 장 중 매 루프 갱신) ────
                if _us_mkt_open:
                    try:
                        _us_avail_pool, _us_excl_cur = _build_orderable_candidates(
                            watch_list = us_watch_list,
                            reentry    = reentry,
                            account    = account,
                            broker     = broker_us,
                            blacklist  = set(_KIS_ORDER_BLACKLIST_MAIN.keys()),
                            usd_krw    = usd_krw,
                            market     = "US",
                        )
                    except Exception:
                        _us_excl_cur = {"reentry": [], "qty0": [], "blacklist": []}

                    # ── 후보 부족 시 보충 + [NO_ENTRY_REASON] ────────
                    _us_supplement = 0
                    _us_pool_size  = len(_us_avail_pool)
                    if _us_pool_size < _POOL_REFILL_THRESHOLD:
                        _blocked_codes_us = {b["code"] for b in reentry.get_blocked_list()}
                        _bl_keys = set(_KIS_ORDER_BLACKLIST_MAIN.keys())
                        for _s in us_watch_list:
                            if _s["code"] in _bl_keys:
                                continue
                            if _s["code"] not in _blocked_codes_us:
                                if not any(p["code"] == _s["code"] for p in _us_avail_pool):
                                    _us_avail_pool.append(_s)
                                    _us_supplement += 1
                        if _us_supplement > 0:
                            logger.info(
                                f"[NO_ENTRY_REASON] 시장=US | "
                                f"사유=주문가능후보부족({_us_pool_size}개<{_POOL_REFILL_THRESHOLD}) | "
                                f"대응=후보보충 | 보충={_us_supplement}개 | "
                                f"보충후풀={len(_us_avail_pool)}개"
                            )
                        else:
                            if _ues_exclude > 0 and _ues_reentry == 0:
                                _us_nr_reason = "자금부족/수량0"
                                _us_nr_action = "자금부족"
                            elif _ues_reentry >= len(us_watch_list) * 0.7:
                                _us_nr_reason = f"감시종목 대부분 재진입차단({_ues_reentry}개)"
                                _us_nr_action = "대기"
                            else:
                                _us_nr_reason = f"조건미달({_ues_cond_fail}개)"
                                _us_nr_action = "조건미달"
                            logger.info(
                                f"[NO_ENTRY_REASON] 시장=US | "
                                f"사유={_us_nr_reason} | "
                                f"대응={_us_nr_action}"
                            )
                    elif _ues_order == 0:
                        if _ues_cond_fail > 0:
                            logger.info(
                                f"[NO_ENTRY_REASON] 시장=US | "
                                f"사유=조건미달({_ues_cond_fail}개) | "
                                f"대응=조건미달"
                            )
                    else:
                        _us_supplement = 0

                    # ── ENTRY_FLOW 누적 업데이트 ──────────────────────
                    _ef_us["watch"]      = len(us_watch_list)
                    _ef_us["avail"]      = max(_ef_us["avail"], len(_us_avail_pool))
                    _ef_us["reentry"]    = max(_ef_us["reentry"], _ues_reentry)
                    _ef_us["qty0"]       = max(_ef_us["qty0"], _ues_exclude)
                    _ef_us["cond_fail"]  = max(_ef_us["cond_fail"], _ues_cond_fail)
                    _ef_us["supplement"] += _us_supplement
                    _ef_us["order"]      += _ues_order

                    # ── [ENTRY_FLOW] 1분 간격 출력 ────────────────────
                    _now_ts_us = time.time()
                    if _now_ts_us - _notified["us_entry_flow_ts"] >= _ENTRY_FLOW_INTERVAL:
                        logger.info(
                            f"[ENTRY_FLOW] 시장=US | "
                            f"감시종목={_ef_us['watch']} | "
                            f"주문가능후보={_ef_us['avail']} | "
                            f"재진입차단={_ef_us['reentry']} | "
                            f"수량0제외={_ef_us['qty0']} | "
                            f"조건미달={_ef_us['cond_fail']} | "
                            f"보충후보={_ef_us['supplement']} | "
                            f"실제주문={_ef_us['order']}"
                        )
                        _notified["us_entry_flow_ts"] = _now_ts_us
                        _ef_us = {"watch": 0, "avail": 0, "reentry": 0, "qty0": 0,
                                  "cond_fail": 0, "supplement": 0, "order": 0}

        except KeyboardInterrupt:
            logger.info("[V2] KeyboardInterrupt → 종료")
            break
        except Exception as e:
            logger.error(f"[루프{loop_count}] 전체 오류: {e}", exc_info=True)

        # ── US 일일 보고서 (06:30~06:45 KST, 미국장 마감 후) ─
        if dtime(6, 30) <= t <= dtime(6, 45) and not _us_reported:
            try:
                logger.info("[ADAPTIVE] US 장 종료 → 일일 학습 보고서 생성 중...")
                reporter.generate("US")
                _us_reported = True
                logger.info("[ADAPTIVE] ✅ US 일일 보고서 완료")
            except Exception as _re:
                logger.warning(f"[ADAPTIVE] US 보고서 실패: {_re}")

        # ── US DAILY_REVIEW (06:35~06:50 KST, 미국장 마감 후 복기) ─
        if dtime(6, 35) <= t <= dtime(6, 50) and not _us_reviewed:
            try:
                logger.info("[DAILY_REVIEW] US 장 종료 → 복기 보고서 생성 중...")
                review_engine.run("US")
                _us_reviewed = True
                logger.info("[DAILY_REVIEW] ✅ US 복기 완료")
            except Exception as _rve:
                logger.warning(f"[DAILY_REVIEW] US 복기 실패: {_rve}")

        # ★ 루프 처리 소요시간만큼 sleep에서 차감 → 실제 주기 loop_sec 유지
        _elapsed = time.time() - _loop_start_ts
        _remain  = max(0.0, loop_sec - _elapsed)
        if _elapsed > loop_sec * 1.5:   # 루프가 1.5배 이상 초과되면 경고
            logger.warning(
                f"[LOOP SLOW] 루프={loop_count} 소요={_elapsed:.1f}초 "
                f"(목표={loop_sec}초) — API 지연/네트워크 문제 의심"
            )
        _stop_event.wait(timeout=_remain)

    logger.info("★ V2 봇 종료")
    _remove_lock()


def _force_close_kr(strategy_kr: KRStrategy,
                    account:     AccountSync,
                    executor:    ExecutionEngine):
    """15:20 이후 국내 보유 포지션 전량 강제 청산 (시장가)."""
    account.sync(force=True)
    holdings = account.holdings
    if not holdings:
        return
    for h in holdings:
        code = h["code"]
        name = h["name"]
        qty  = h["qty"]
        if qty <= 0:
            continue
        logger.info(f"[KR 15:20강제청산] {name}({code}) {qty}주")
        executor.execute_sell(
            code        = code,
            name        = name,
            qty         = qty,
            price       = 0,
            reason      = "15:20 KR 강제청산 — 오버나이트 방지",
            ord_dvsn    = "01",   # 시장가
            is_stoploss = False,
        )


if __name__ == "__main__":
    main()
