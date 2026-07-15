"""
Flask 웹 대시보드 — 세션 인식 자동매매
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import json
import time
import threading
import subprocess as _subp
from datetime import datetime

from flask import Flask, render_template, jsonify, request, redirect, url_for, session, send_from_directory
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import Config
from utils.logger import get_logger
from utils.notifier import TelegramNotifier
from utils.market_session import (
    session_info, is_tradeable, SESSION_OFF,
    us_session_info, is_us_tradeable,
    allow_new_buy_now, BUY_CUTOFF_TIME,
)

logger   = get_logger("Dashboard")
notifier = TelegramNotifier()

# ── 빌드 정보 (프로세스 시작 시 1회 고정) ─────────────────────
_BUILD_PID   = os.getpid()
_BUILD_START = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
_BUILD_COMMIT = "unknown"
try:
    _BUILD_COMMIT = _subp.check_output(
        ["git", "-C", os.path.dirname(os.path.abspath(__file__)),
         "log", "--format=%h", "-1"],
        stderr=_subp.DEVNULL, text=True
    ).strip() or "unknown"
except Exception:
    pass

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = Config.FLASK_SECRET_KEY
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── 글로벌 상태 ────────────────────────────────────────────
_api          = None
_strategy_mgr = None
_us_strategy  = None      # ★ 해외주식 전략 매니저
_scheduler    = None
_bot_running  = False

# ════════════════════════════════════════════════════════════
# ★ V2 LIVE 전환 보호 스위치
#   V2_LIVE_LOCK_FILE 이 존재하면 V1 주문 루프를 완전 차단
#   V2가 실행 중일 때 V1이 주문을 내는 것을 방지
# ════════════════════════════════════════════════════════════
_V2_LOCK_FILE = os.path.join(os.path.dirname(__file__), "..", "stock_trader_v2", "data", "v2_live.lock")

def _is_v2_live() -> bool:
    """V2 LIVE 잠금파일 존재 여부 반환 — True이면 V1 주문 완전 차단."""
    return os.path.exists(_V2_LOCK_FILE)

# ── 루프 공용 잔고 캐시 ──────────────────────────────────────
# 루프 시작 시 1회 조회 → 주문 직후 갱신 → 60초마다 자동 갱신
# ETF/개별주식 모두 이 값 재사용 → KIS API 호출 최소화
_loop_cash: float = 0.0
_loop_balance: dict = {}
_loop_balance_ts: float = 0.0
_LOOP_BALANCE_TTL: float = 60.0   # 60초마다 재조회

# ── 해외주식 관심종목 (티커 기반) ──────────────────────────
_us_watch_list   = []     # [{"symbol":"NVDA","name":"엔비디아","excd":"NASD"}, ...]
_us_last_signals = {}     # symbol → 최근 신호

# ── 일 수익 추적 ────────────────────────────────────────────
# 하루 목표 수익 5만원 달성 모니터링
DAILY_PROFIT_TARGET_KRW = 50_000   # 일 수익 목표 (KRW)
_today_pnl_kr  = 0.0    # 당일 국내 실현손익 합계 (KRW)
_today_pnl_us  = 0.0    # 당일 미국 실현손익 합계 (USD)
_today_trades  = 0      # 당일 매매 횟수 (매수+매도)
_today_date    = ""     # 당일 날짜 문자열 (날짜 바뀌면 리셋)
_daily_goal_reached = False   # 목표 달성 플래그

# ── 봇 상태 영속 파일 (재시작해도 유지) ──────────────────────
_BOT_STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "bot_state.json")

def _save_bot_state(running: bool):
    """봇 실행 상태를 파일에 저장 (supervisord 재시작 후에도 복원용)"""
    try:
        os.makedirs(os.path.dirname(_BOT_STATE_FILE), exist_ok=True)
        import json as _json
        with open(_BOT_STATE_FILE, "w") as f:
            _json.dump({"running": running, "saved_at": datetime.now().isoformat()}, f)
    except Exception as e:
        logger.warning(f"봇 상태 저장 실패: {e}")

def _load_bot_state() -> bool:
    """저장된 봇 실행 상태 로드"""
    try:
        if os.path.exists(_BOT_STATE_FILE):
            import json as _json
            data = _json.load(open(_BOT_STATE_FILE))
            return bool(data.get("running", False))
    except Exception:
        pass
    return False

# ★ 관심종목 목록: .env WATCH_LIST_CODES 우선, 없으면 30개 고정 기본 종목
# ★ 초공격 단타: 항상 충분한 종목 보유 → 하루 10회+ 매매 가능
# ─── 고정 기본 관심종목 (단타 유동성 + 변동성 높은 종목 30개) ───
DEFAULT_KR_WATCHLIST = [
    # ══ 대형 모멘텀 ══
    {"code": "005930", "name": "삼성전자",       "asset_type": "STOCK_KOSPI"},
    {"code": "000660", "name": "SK하이닉스",      "asset_type": "STOCK_KOSPI"},
    {"code": "035420", "name": "NAVER",           "asset_type": "STOCK_KOSPI"},
    {"code": "005380", "name": "현대차",           "asset_type": "STOCK_KOSPI"},
    {"code": "000270", "name": "기아",             "asset_type": "STOCK_KOSPI"},
    {"code": "035720", "name": "카카오",           "asset_type": "STOCK_KOSPI"},
    {"code": "207940", "name": "삼성바이오로직스", "asset_type": "STOCK_KOSPI"},
    {"code": "051910", "name": "LG화학",           "asset_type": "STOCK_KOSPI"},
    {"code": "006400", "name": "삼성SDI",          "asset_type": "STOCK_KOSPI"},
    {"code": "373220", "name": "LG에너지솔루션",   "asset_type": "STOCK_KOSPI"},
    # ══ 고변동 성장주 ══
    {"code": "247540", "name": "에코프로비엠",     "asset_type": "STOCK_KOSDAQ"},
    {"code": "086520", "name": "에코프로",         "asset_type": "STOCK_KOSDAQ"},
    {"code": "091990", "name": "셀트리온헬스케어", "asset_type": "STOCK_KOSPI"},
    {"code": "068270", "name": "셀트리온",         "asset_type": "STOCK_KOSPI"},
    {"code": "035900", "name": "JYP Ent.",         "asset_type": "STOCK_KOSDAQ"},
    {"code": "041510", "name": "에스엠",           "asset_type": "STOCK_KOSDAQ"},
    {"code": "028260", "name": "삼성물산",         "asset_type": "STOCK_KOSPI"},
    {"code": "066570", "name": "LG전자",           "asset_type": "STOCK_KOSPI"},
    {"code": "096770", "name": "SK이노베이션",     "asset_type": "STOCK_KOSPI"},
    {"code": "011200", "name": "HMM",              "asset_type": "STOCK_KOSPI"},
    # ══ 단타 강한 테마주 ══
    {"code": "000990", "name": "DB하이텍",         "asset_type": "STOCK_KOSPI"},
    {"code": "042700", "name": "한미반도체",       "asset_type": "STOCK_KOSDAQ"},
    {"code": "357780", "name": "솔브레인",         "asset_type": "STOCK_KOSDAQ"},
    {"code": "336370", "name": "솔루엠",           "asset_type": "STOCK_KOSDAQ"},
    {"code": "263750", "name": "펄어비스",         "asset_type": "STOCK_KOSDAQ"},
    {"code": "036570", "name": "엔씨소프트",       "asset_type": "STOCK_KOSPI"},
    {"code": "251270", "name": "넷마블",           "asset_type": "STOCK_KOSPI"},
    {"code": "011790", "name": "SKC",              "asset_type": "STOCK_KOSPI"},
    {"code": "009150", "name": "삼성전기",         "asset_type": "STOCK_KOSPI"},
    {"code": "034020", "name": "두산에너빌리티",   "asset_type": "STOCK_KOSPI"},
]

def _build_watch_list() -> list:
    codes_env = os.environ.get("WATCH_LIST_CODES", "").strip()
    if codes_env:
        NAME_MAP = {s["code"]: s["name"] for s in DEFAULT_KR_WATCHLIST}
        return [{"code": c.strip(), "name": NAME_MAP.get(c.strip(), c.strip()),
                 "asset_type": "STOCK_KOSPI"}
                for c in codes_env.split(",") if c.strip()]
    # ★ 기본: 30개 고정 종목으로 시작 → 스크리닝 결과로 보완
    return list(DEFAULT_KR_WATCHLIST)

_watch_list   = _build_watch_list()
_last_signals = {}
_status_log   = []

# ── 전략 실험실 ───────────────────────────────────────────
_lab_engine   = None    # StrategyLabEngine 인스턴스 (지연 초기화)


def _log(msg: str, level: str = "info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    _status_log.append(entry)
    if len(_status_log) > 150:
        _status_log.pop(0)
    socketio.emit("log", entry)


# ── 스크리닝 DB에서 오늘 매수후보 → 관심종목 자동 로드 ──────
def _load_screener_candidates():
    """앱 시작 시 오늘 스크리닝 결과가 있으면 관심종목에 반영"""
    global _watch_list
    try:
        from screener.screener_db import get_buy_candidates
        cands = get_buy_candidates()   # 오늘 날짜 기준
        if not cands:
            return
        existing_codes = {s["code"] for s in _watch_list}
        added = []
        for c in cands:
            code = c.get("code", "")
            name = c.get("name", code)
            if code and code not in existing_codes:
                _watch_list.append({"code": code, "name": name})
                existing_codes.add(code)
                added.append(name)
        if added:
            logger.info(f"[시작] 스크리닝 후보 자동 로드: {', '.join(added)}")
    except Exception as e:
        logger.warning(f"[시작] 스크리닝 후보 로드 실패 (무시): {e}")


# ── API 초기화 ────────────────────────────────────────────
def _init_api() -> bool:
    global _api, _strategy_mgr, _us_strategy, _us_watch_list
    from api.kis_api import KISApi
    from strategies.strategy_manager import StrategyManager
    from strategies.us_strategy_manager import USStrategyManager, DEFAULT_US_WATCHLIST
    try:
        _api          = KISApi()
        _strategy_mgr = StrategyManager(_api)
        _log("✅ KIS API 초기화 완료")

        # ★ 재진입 차단 모듈 — 서버 시작 시 오늘 SELL 이력으로 복원 + 만료 정리
        try:
            from strategies.reentry_guard import ReentryGuard
            _rg = ReentryGuard()
            # ① 오늘 trade_log SELL 이력 → reentry_guard.json 복원 (재시작 후 이력 유지)
            _rg.restore_from_trade_log()
            # ② 이미 만료된 항목 정리
            _rg.purge_expired()
            _blocked = _rg.get_blocked_list()
            if _blocked:
                _names = ", ".join(
                    b["name"] + "(" + b["market"] + ",잔" + str(int(b["remaining_hours"])) + "h)"
                    for b in _blocked[:5]
                )
                _log(f"🔒 [재진입차단] 현재 {len(_blocked)}개 종목 쿨다운 중: {_names}")
            else:
                _log("🔓 [재진입차단] 쿨다운 종목 없음")
        except Exception as _rge:
            _log(f"⚠️ [재진입차단] 초기화 오류: {_rge}", "warning")

        # ★ 해외주식 전략 매니저 초기화
        max_us_usd   = float(os.environ.get("MAX_US_INVESTMENT_USD", 3000.0))
        _us_strategy = USStrategyManager(_api, max_total_usd=max_us_usd)

        # ★ 해외주식 기본 관심종목 로드 — 100개 풀 유니버스 (.env US_WATCH_LIST 우선)
        # NOTE: watchlist 로딩은 예외와 무관하게 항상 완료되어야 함
        _us_watch_list.clear()   # 이전 데이터 초기화
        env_us = os.environ.get("US_WATCH_LIST", "").strip()
        if env_us:
            for item in env_us.split(","):
                parts = item.strip().split(":")
                sym  = parts[0].strip().upper()
                excd = parts[1].strip().upper() if len(parts) > 1 else "NASD"
                name = DEFAULT_US_WATCHLIST.get(sym, {}).get("name", sym)
                if sym and not any(s["symbol"] == sym for s in _us_watch_list):
                    _us_watch_list.append({"symbol": sym, "name": name, "excd": excd})
        else:
            # DEFAULT_US_WATCHLIST 전체 100개 로드
            for sym, info in DEFAULT_US_WATCHLIST.items():
                _us_watch_list.append({"symbol": sym, **info})
        _log(f"✅ 미국 관심종목 로드 완료: {len(_us_watch_list)}개 (풀 유니버스)", "info")

        # ★ 해외주식 잔고 기반 포지션 자동 복원 (실패해도 watchlist는 유지)
        try:
            _us_strategy.sync_from_balance()
        except Exception as e_sync:
            _log(f"⚠️ US 잔고 동기화 스킵 (비정규장 시간): {e_sync}", "warning")
        _log(f"✅ 해외주식 초기화 완료 — 관심종목 {len(_us_watch_list)}개", "info")

        # ★ API 초기화 후 오늘 스크리닝 후보 관심종목에 자동 반영
        _load_screener_candidates()
        # ★ 실제 잔고 기반 피라미딩 포지션 자동 복원
        _sync_positions_from_balance()
        return True
    except Exception as e:
        _log(f"❌ API 초기화 실패: {e}", "error")
        return False


def _sync_positions_from_balance():
    """
    실제 KIS 잔고 ↔ 봇 피라미딩 포지션 완전 동기화.
    1) 누락 종목 → 자동 등록 (500에러 등으로 포지션 미등록된 경우 복원)
    2) avg_price 불일치 → KIS 실제 평균단가로 교정
       (pyramid_positions.json에 잘못된 avg_price가 기록된 경우 수정)
    ★ 손절 기준이 avg_price 기반이므로 이 값이 정확해야 손절이 작동함
    """
    global _strategy_mgr
    if _strategy_mgr is None:
        return
    try:
        balance = _api.get_balance()
        holdings = balance.get("holdings", [])
        if not holdings:
            return
        pyramid = _strategy_mgr.pyramid
        synced  = []
        corrected = []

        for h in holdings:
            code      = h.get("code", "")
            name      = h.get("name", code)
            qty       = int(h.get("qty", 0))
            avg_price = float(h.get("avg_price", 0))   # KIS 실제 평균단가
            if not code or qty <= 0 or avg_price <= 0:
                continue

            if code not in pyramid.positions:
                # ── 미등록 종목 → 1단계로 자동 등록 ──
                pyramid.apply_buy(code, name, 1, qty, avg_price, using_compound=0)
                synced.append(f"{name}({code}) {qty}주 @{avg_price:,.0f}원")
            else:
                # ── 등록된 종목 → avg_price 불일치 교정 ──
                pos = pyramid.positions[code]
                bot_avg = pos.avg_price
                # 차이가 1% 이상이면 KIS 잔고 기준으로 교정
                if bot_avg > 0 and abs(bot_avg - avg_price) / avg_price > 0.01:
                    old_avg = pos.avg_price
                    pos.avg_price    = avg_price
                    pos.entry_price  = avg_price
                    pos.total_qty    = qty
                    # level_entries 도 교정
                    for lvl_key in pos.level_entries:
                        pos.level_entries[lvl_key]["avg_price"] = avg_price
                        pos.level_entries[lvl_key]["price"]     = avg_price
                        pos.level_entries[lvl_key]["qty"]       = qty
                        pos.level_entries[lvl_key]["remaining"] = qty
                    corrected.append(
                        f"{name}({code}) avg: {old_avg:,.0f}→{avg_price:,.0f}원"
                    )

        pyramid._save()

        if synced:
            _log(f"🔄 포지션 자동 복원: {', '.join(synced)}", "info")
        if corrected:
            _log(f"🔧 포지션 avg_price 교정 (KIS 잔고 기준): {', '.join(corrected)}", "info")
        if not synced and not corrected:
            logger.info("[포지션동기화] 모든 보유종목 포지션 일치 — 복원 불필요")
    except Exception as e:
        logger.warning(f"[포지션동기화] 실패 (무시): {e}")


# ── 봇 자동 시작 (앱 기동 시 이전 상태 복원) ─────────────────
def _auto_start_bot():
    """supervisord 재시작 후 이전에 봇이 실행 중이었으면 자동으로 재개"""
    global _bot_running, _scheduler
    if not _load_bot_state():
        logger.info("[자동시작] 이전 봇 상태: 정지 — 자동 시작 안 함")
        return
    logger.info("[자동시작] 이전 봇 상태: 실행 중 → 봇 자동 재개...")
    if _api is None and not _init_api():
        logger.error("[자동시작] API 초기화 실패 — 봇 자동 시작 불가")
        return
    _bot_running = True
    if _scheduler is None or not _scheduler.running:
        _scheduler = BackgroundScheduler(timezone="Asia/Seoul")
        sess = session_info()
        _scheduler.add_job(_trading_loop,  "interval", seconds=sess["check_sec"],
                           id="trading_loop",       replace_existing=True)
        _scheduler.add_job(_session_watcher, "interval", seconds=30,
                           id="session_watcher",    replace_existing=True)
        _scheduler.add_job(_daily_screen_job, "cron", hour=16, minute=5,
                           timezone="Asia/Seoul",   id="daily_screener",   replace_existing=True)
        _scheduler.add_job(_weekly_lab_ranking_job, "cron", day_of_week="mon", hour=9, minute=0,
                           timezone="Asia/Seoul",   id="weekly_lab_ranking", replace_existing=True)
        _scheduler.add_job(_us_market_job, "cron", hour=0, minute=0,
                           timezone="Asia/Seoul",   id="us_market_screener", replace_existing=True)
        _scheduler.add_job(_us_intraday_job, "cron", hour=10, minute=30,
                           timezone="America/New_York",  # ET 10:30 (장 개시 1h 후)
                           id="us_intraday_screener", replace_existing=True)
        # ★ 15:20:30 미체결 매수주문 자동 취소 (장후시간외 체결 방지)
        _scheduler.add_job(_cancel_pending_buy_orders, "cron",
                           hour=15, minute=20, second=30,
                           timezone="Asia/Seoul",
                           id="cancel_pending_buys", replace_existing=True)
        _scheduler.start()
        # ★ 자동 재개 시 토큰 안정화 후 1회 신호 점검 (15초 딜레이)
        def _delayed_loop():
            time.sleep(15)
            _trading_loop()
        threading.Thread(target=_delayed_loop, daemon=True).start()
    logger.info("[자동시작] ✅ 봇 자동 재개 완료")


# ── ETF 포지션 비중 계산 헬퍼 ──────────────────────────────
def _get_etf_position_ratio(asset_type: str, regime: str) -> float:
    """
    국면 + 자산군에 따른 ETF 단일 종목 투자 비중 반환.
    계좌 총 자산 대비 비율.
      BULL + 레버리지 → 10% (레버리지는 보수적으로)
      BULL + 일반ETF  → 10%
      LATERAL + 일반  → 12% (횡보장엔 ETF 비중 높임)
      LATERAL + 인버스→  5% (소량 헤지)
      BEAR + 인버스   → 15% (하락장 방어)
      BEAR + 일반ETF  →  8%
    """
    table = {
        ("BULL",    "ETF_LEVERAGE"): 0.10,
        ("BULL",    "ETF_GENERAL"):  0.10,
        ("BULL",    "ETF_INVERSE"):  0.00,
        ("LATERAL", "ETF_GENERAL"):  0.12,
        ("LATERAL", "ETF_INVERSE"):  0.05,
        ("LATERAL", "ETF_LEVERAGE"): 0.00,
        ("BEAR",    "ETF_INVERSE"):  0.15,
        ("BEAR",    "ETF_GENERAL"):  0.08,
        ("BEAR",    "ETF_LEVERAGE"): 0.00,
    }
    return table.get((regime, asset_type), 0.08)


def _detect_current_regime() -> str:
    """
    마지막 스크리닝 결과 또는 실시간 지수 기반 시장 국면 반환.
    스크리닝 결과가 있으면 그것을 우선 사용.
    """
    if _last_screen and _last_screen.get("regime"):
        return _last_screen["regime"]
    # 기본값 LATERAL (보수적)
    return "LATERAL"


# ── 일 수익 추적 헬퍼 ──────────────────────────────────────
def _reset_daily_if_needed():
    """날짜가 바뀌면 당일 수익/거래 카운터 초기화"""
    global _today_pnl_kr, _today_pnl_us, _today_trades, _today_date, _daily_goal_reached
    today = datetime.now().strftime("%Y-%m-%d")
    if _today_date != today:
        _today_pnl_kr = 0.0
        _today_pnl_us = 0.0
        _today_trades = 0
        _today_date   = today
        _daily_goal_reached = False
        logger.info(f"📅 새 거래일 시작 — 일 수익 카운터 초기화 ({today})")


def _record_trade_pnl(pnl_krw: float = 0.0, pnl_usd: float = 0.0, is_trade: bool = True):
    """
    실현손익 + 거래 횟수 누적 → 목표 달성 시 알림
    pnl_krw: 국내 손익 (원화)
    pnl_usd: 미국 손익 (달러, 원화로 환산해서 합산)
    """
    global _today_pnl_kr, _today_pnl_us, _today_trades, _daily_goal_reached
    _reset_daily_if_needed()

    _today_pnl_kr += pnl_krw
    _today_pnl_us += pnl_usd
    if is_trade:
        _today_trades += 1

    # USD→KRW 환산 합계 (환율 1350 고정 근사)
    total_krw = _today_pnl_kr + _today_pnl_us * 1350.0

    _log(
        f"📊 당일수익: 국내{_today_pnl_kr:+,.0f}원 + 미국${_today_pnl_us:+.2f} "
        f"= 합계{total_krw:+,.0f}원 | 거래{_today_trades}회",
        "info"
    )

    # ★ 목표 달성 알림 (최초 1회만)
    if not _daily_goal_reached and total_krw >= DAILY_PROFIT_TARGET_KRW:
        _daily_goal_reached = True
        msg = (
            f"🎯 일 수익 목표 달성! {total_krw:+,.0f}원 "
            f"(목표 {DAILY_PROFIT_TARGET_KRW:,}원) "
            f"| 거래 {_today_trades}회"
        )
        _log(msg, "buy")
        try:
            socketio.emit("daily_goal_reached", {
                "total_krw":    round(total_krw),
                "target_krw":   DAILY_PROFIT_TARGET_KRW,
                "trades":       _today_trades,
                "pnl_kr":       round(_today_pnl_kr),
                "pnl_us_usd":   round(_today_pnl_us, 2),
            })
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# ★ 15:20 미체결 매수주문 자동 취소
# ══════════════════════════════════════════════════════════════
def _cancel_pending_buy_orders():
    """
    15:20 이후 남아있는 미체결 매수주문을 전량 취소.

    ▸ 호출 시점:
      ① APScheduler cron job: 매일 15:20:30 (30초 여유)
      ② _trading_loop() 내부: 세션이 '정규장마감_매도전용' 또는
         현재 시각이 15:20~15:35 구간일 때 매 루프마다 체크
    ▸ 이미 취소됐거나 체결된 주문은 미체결 조회 API에서 제외되므로 안전.
    """
    global _api
    if _api is None:
        return

    from utils.market_session import allow_new_buy_now
    from datetime import datetime as _dt
    import pytz as _pytz
    _KST = _pytz.timezone("Asia/Seoul")
    _now = _dt.now(_KST)
    _t   = _now.time()

    # 15:20 이전이면 실행하지 않음
    from datetime import time as _time
    if _t < _time(15, 20):
        return

    # 15:35 이후 이미 장 종료 구간에서는 한 번만 실행 (무한 재시도 방지)
    # → 15:20~15:40 구간에서만 동작
    if _t > _time(15, 40):
        return

    try:
        open_buys = _api.get_open_orders(order_type="BUY")
    except Exception as e:
        logger.error(f"[미체결취소] 미체결 조회 실패: {e}")
        _log(f"❌ [미체결취소] 조회 실패: {e}", "error")
        return

    if not open_buys:
        logger.info(f"[미체결취소] {_now.strftime('%H:%M:%S')} — 미체결 매수주문 없음")
        return

    logger.warning(
        f"[미체결취소] ★ 15:20 이후 미체결 매수주문 {len(open_buys)}건 발견 → 전량 취소 시작"
    )
    _log(
        f"⚠️ [미체결취소] 15:20 경과 — 미체결 매수주문 {len(open_buys)}건 자동 취소",
        "warning"
    )

    cancelled, failed = 0, 0
    for order in open_buys:
        code       = order["stock_code"]
        name       = order["stock_name"] or code
        order_no   = order["order_no"]
        unexec_qty = order["unexec_qty"]
        ord_unpr   = order["ord_unpr"]
        ord_dvsn   = order["ord_dvsn"]
        ord_time   = order["ord_time"]   # HHMMSS

        if unexec_qty <= 0:
            continue

        logger.warning(
            f"[미체결취소] 취소 시도: {name}({code}) | 주문번호={order_no} | "
            f"미체결={unexec_qty}주 | ORD_DVSN={ord_dvsn} | 주문시간={ord_time}"
        )
        result = _api.cancel_order(
            order_no=order_no,
            stock_code=code,
            unexec_qty=unexec_qty,
            ord_unpr=ord_unpr,
            ord_dvsn=ord_dvsn,
        )
        rt_cd  = result.get("rt_cd", "?")
        msg_cd = result.get("msg_cd", "")
        msg1   = result.get("msg1", "")

        if rt_cd == "0":
            cancelled += 1
            _log(
                f"✅ [미체결취소] {name}({code}) {unexec_qty}주 취소 완료 "
                f"(주문번호={order_no}, 주문시간={ord_time})",
                "warning"
            )
        else:
            failed += 1
            _log(
                f"❌ [미체결취소] {name}({code}) 취소 실패 | "
                f"rt_cd={rt_cd} msg_cd={msg_cd!r} msg1={msg1!r}",
                "error"
            )

    logger.warning(
        f"[미체결취소] 완료 — 취소성공={cancelled}건 / 실패={failed}건 / "
        f"전체={len(open_buys)}건"
    )
    if cancelled > 0:
        _log(
            f"✅ [미체결취소] 15:20 이후 미체결 매수주문 {cancelled}건 취소 완료 "
            f"(실패={failed}건)",
            "warning"
        )


# ── 세션 인식 매매 루프 ───────────────────────────────────
def _trading_loop():
    # ★ V2 LIVE 전환 보호: V2가 실행 중이면 V1 주문 루프 완전 차단
    if _is_v2_live():
        logger.info("[V1 주문루프] ⛔ V2 LIVE 실행 중 — V1 KR 주문루프 차단")
        return
    if not _bot_running or _strategy_mgr is None:
        return

    sess = session_info()

    # ══════════════════════════════════════════════════════════
    # ★ 15:20 이후 미체결 매수주문 자동 취소 (매 루프 체크)
    # ══════════════════════════════════════════════════════════
    _sess_name = sess.get("session", "")
    if _sess_name in ("정규장마감_매도전용", "장후시간외"):
        try:
            _cancel_pending_buy_orders()
        except Exception as _ce:
            logger.error(f"[미체결취소] 루프 내 취소 오류: {_ce}")

    # ── ★ 국내 휴장이어도 미국장 + 손절 점검은 반드시 실행 ──────
    if not sess["tradeable"]:
        _log(f"😴 [{sess['time_kst']}] 국내 휴장 — 손절 점검 + 미국장 체크...", "info")
        # ★ 장 마감 중에도 포지션 손절 감시 (고아종목 포함)
        try:
            _orphan_loss_cut()
        except Exception as _e:
            _log(f"❌ 손절 점검 오류: {_e}", "error")
        _us_trading_loop()   # 미국 정규장이면 실행, 아니면 내부에서 스킵
        return

    regime = _detect_current_regime()
    _log(
        f"{sess['icon']} [{sess['time_kst']}] {sess['session']} "
        f"({sess['order_label']}) 신호 점검... [국면:{regime}]",
        "info"
    )

    # ── 루프 공용 잔고: 60초 TTL 캐시 ──────────────────────────────
    # 루프 시작 / 60초 경과 시에만 KIS API 호출 (ETF·개별주 모두 재사용)
    global _loop_cash, _loop_balance, _loop_balance_ts
    import time as _time_mod
    _now = _time_mod.time()
    if _now - _loop_balance_ts >= _LOOP_BALANCE_TTL or _loop_cash == 0:
        try:
            _loop_balance    = _strategy_mgr.api.get_balance()
            _loop_cash       = float(_loop_balance.get("cash", 0))
            _loop_balance_ts = _now
        except Exception as _be:
            _log(f"⚠️ 루프 잔고 조회 실패: {_be} — 이전 캐시값 유지", "warning")
    # 캐시값 0이면 경고
    if _loop_cash == 0:
        _log("⚠️ 잔고 0원 확인됨 — 매수는 SKIP됩니다", "warning")

    for stock in list(_watch_list):
        try:
            code       = stock["code"]
            name       = stock["name"]
            asset_type = stock.get("asset_type", "")

            # ── ETF 전용 매매 경로 ─────────────────────────────
            from screener.asset_universe import (
                ASSET_ETF_GENERAL, ASSET_ETF_LEVERAGE, ASSET_ETF_INVERSE,
                is_etf, classify_asset_type, ETF_CODE_MAP,
            )
            # asset_type 없으면 코드/이름으로 자동 판별
            if not asset_type:
                asset_type = ETF_CODE_MAP.get(code, {}).get("asset_type") or \
                             classify_asset_type(code, name, "")
                stock["asset_type"] = asset_type  # 캐싱

            if is_etf(asset_type):
                _handle_etf_trade(
                    stock, asset_type, regime, sess,
                    cached_cash=_loop_cash,
                    cached_balance=_loop_balance,
                )
                continue

            # ── 개별주식 매매 (기존 로직) ──────────────────────
            result = _strategy_mgr.run(stock, cached_cash=_loop_cash)
            _last_signals[code] = result
            action = result.get("action", "HOLD")

            if action == "BUY":
                # 주문 직후 잔고 캐시 무효화 → 다음 루프에서 재조회
                _loop_balance_ts = 0.0
                _log(
                    f"🟢 매수 [{result.get('session','')}] "
                    f"{name} {result['price']:,}원 × {result['qty']}주",
                    "buy"
                )
                notifier.notify_buy(
                    name, code,
                    result["price"], result["qty"],
                    result.get("reason", "")
                )
                # ★ 거래 횟수 카운트 (매수도 포함)
                _record_trade_pnl(pnl_krw=0.0, is_trade=True)
            elif action == "BUY_FAIL":
                # ── BUY_FAIL: KIS 응답 상세 키 추출 (strategy_manager.py와 통일) ─
                _kis_http   = result.get("_http_status", "?")
                _kis_body   = result.get("_response_body", "")
                _kis_msg_cd = result.get("msg_cd", "")
                _kis_msg1   = result.get("msg1", "") or result.get("reason", "")
                _kis_rt_cd  = result.get("rt_cd", "?")
                _ord_dvsn   = result.get("_ord_dvsn", "?")
                _ord_unpr   = result.get("_ord_unpr", "?")
                _order_qty  = result.get("order_qty",  "?")
                _order_amt  = result.get("order_amt",  "?")
                _tr_id      = result.get("_tr_id", "TTTC0802U")
                _account    = result.get("account",    Config.KIS_ACCOUNT_NO)
                _sess_lbl   = result.get("session",    "?")
                _kst_lbl    = result.get("_kst",       datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

                # ── 원인 분류 (5가지) ────────────────────────────────
                _http_int = int(_kis_http) if str(_kis_http).isdigit() else 0
                if _http_int == 500:
                    _cause = "🔴 KIS 서버 오류 (HTTP 500)"
                elif _kis_msg_cd == "EGW00201":
                    _cause = "🟠 TPS 초과 (EGW00201 — 초당 20건 제한)"
                elif _kis_msg_cd in ("APBK0988", "APBK0503"):
                    _cause = f"🟡 주문가능수량 부족 ({_kis_msg_cd})"
                elif _kis_msg_cd and _kis_msg_cd.startswith("IGW"):
                    _cause = f"🟠 ORD_DVSN/ORD_UNPR 오류 ({_kis_msg_cd})"
                elif "잔고" in str(_kis_msg1) or "금액" in str(_kis_msg1) or "부족" in str(_kis_msg1):
                    _cause = "🟡 주문가능금액 부족"
                elif "시간" in str(_kis_msg1) or "session" in str(_kis_msg1).lower() or "시간대" in str(_kis_msg1):
                    _cause = "🔵 세션/시간 오류"
                elif "계좌" in str(_kis_msg1) or "권한" in str(_kis_msg1):
                    _cause = "🔴 계좌/권한 오류"
                elif "backoff" in str(_kis_msg1) or "TPS" in str(_kis_msg_cd):
                    _cause = "🟠 TPS backoff 중"
                else:
                    _cause = f"❓ 원인 불명 (msg_cd={_kis_msg_cd!r}, msg1={str(_kis_msg1)[:40]!r})"

                # ── 주문금액 포맷 ────────────────────────────────────
                try:
                    _amt_str = f"{int(_order_amt):,}원"
                except Exception:
                    _amt_str = str(_order_amt)

                _log(
                    f"❌ BUY_FAIL [{name}] ▶ {_cause} | HTTP={_kis_http} | msg_cd={_kis_msg_cd!r}\n"
                    f"  종목={name}({code}) | KST={_kst_lbl} | 세션={_sess_lbl} | 계좌={_account}\n"
                    f"  ORD_DVSN={_ord_dvsn} | ORD_UNPR={_ord_unpr}원 | 수량={_order_qty}주 | 주문금액={_amt_str}\n"
                    f"  tr_id={_tr_id} | rt_cd={_kis_rt_cd} | msg1={_kis_msg1!r}\n"
                    f"  KIS body: {str(_kis_body)[:300]}",
                    "error"
                )
            elif action == "SELL_FAIL":
                # ── SELL_FAIL: KIS 응답 상세 키 추출 (strategy_manager.py와 통일) ─
                _kis_http   = result.get("_http_status", "?")
                _kis_body   = result.get("_response_body", "")
                _kis_msg_cd = result.get("msg_cd", "")
                _kis_msg1   = result.get("msg1", "") or result.get("reason", "")
                _kis_rt_cd  = result.get("rt_cd", "?")
                _ord_dvsn   = result.get("_ord_dvsn", "?")
                _ord_unpr   = result.get("_ord_unpr", "?")
                _order_qty  = result.get("order_qty",  "?")
                _order_amt  = result.get("order_amt",  "?")
                _tr_id      = result.get("_tr_id", "TTTC0801U")
                _account    = result.get("account",    Config.KIS_ACCOUNT_NO)
                _sess_lbl   = result.get("session",    "?")
                _kst_lbl    = result.get("_kst",       datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

                _http_int = int(_kis_http) if str(_kis_http).isdigit() else 0
                if _http_int == 500:
                    _cause = "🔴 KIS 서버 오류 (HTTP 500)"
                elif _kis_msg_cd == "EGW00201":
                    _cause = "🟠 TPS 초과 (EGW00201)"
                elif _kis_msg_cd in ("APBK0988", "APBK0503"):
                    _cause = f"🟡 주문가능수량 부족 ({_kis_msg_cd})"
                elif _kis_msg_cd and _kis_msg_cd.startswith("IGW"):
                    _cause = f"🟠 ORD_DVSN/ORD_UNPR 오류 ({_kis_msg_cd})"
                elif "잔고" in str(_kis_msg1) or "부족" in str(_kis_msg1):
                    _cause = "🟡 주문가능금액 부족"
                elif "계좌" in str(_kis_msg1) or "권한" in str(_kis_msg1):
                    _cause = "🔴 계좌/권한 오류"
                else:
                    _cause = f"❓ 원인 불명 (msg_cd={_kis_msg_cd!r}, msg1={str(_kis_msg1)[:40]!r})"

                try:
                    _amt_str = f"{int(_order_amt):,}원"
                except Exception:
                    _amt_str = str(_order_amt)

                _log(
                    f"❌ SELL_FAIL [{name}] ▶ {_cause} | HTTP={_kis_http} | msg_cd={_kis_msg_cd!r}\n"
                    f"  종목={name}({code}) | KST={_kst_lbl} | 세션={_sess_lbl} | 계좌={_account}\n"
                    f"  ORD_DVSN={_ord_dvsn} | ORD_UNPR={_ord_unpr}원 | 수량={_order_qty}주 | 주문금액={_amt_str}\n"
                    f"  tr_id={_tr_id} | rt_cd={_kis_rt_cd} | msg1={_kis_msg1!r}\n"
                    f"  KIS body: {str(_kis_body)[:300]}",
                    "error"
                )
                # ★ 매도 실패 → 자동 재시도 큐 등록 (최대 3회, 0s→5s→15s)
                import time as _time_retry
                _fail_qty = result.get("order_qty", 0)
                if _fail_qty and int(_fail_qty) > 0:
                    _sell_retry_q.append({
                        "code":          code,
                        "name":          name,
                        "qty":           int(_fail_qty),
                        "attempt":       1,
                        "first_fail_ts": _time_retry.time(),
                        "reason":        f"{_cause} | msg_cd={_kis_msg_cd}",
                    })
                    _log(
                        f"🔄 [SELL_RETRY 큐 등록] {name}({code}) {_fail_qty}주 — "
                        f"다음 루프에서 자동 재시도 (최대 3회)",
                        "warning"
                    )
            elif action == "SELL":
                profit = result.get("profit", 0)
                emoji  = "💰" if profit >= 0 else "🔴"
                _log(
                    f"{emoji} 매도 [{result.get('session','')}] "
                    f"{name}  손익 {profit:+,.0f}원",
                    "sell" if profit >= 0 else "error"
                )
                notifier.notify_sell(
                    name, code,
                    result["price"], result["qty"],
                    profit, result.get("reason", "")
                )
                # ★ 당일 수익 누적
                _record_trade_pnl(pnl_krw=float(profit), is_trade=True)
            elif action == "HOLD":
                _log(
                    f"⏸ {name}  BUY={result.get('buy_score',0):.2f} "
                    f"SELL={result.get('sell_score',0):.2f} "
                    f"(기준 ≥{result.get('cutoff',0.55)})",
                    "info"
                )
        except Exception as e:
            _log(f"❌ {stock['name']} 오류: {e}", "error")

    # ── 관심종목 미등록 보유 종목 자동 손절 (고아 종목 처리) ─────
    try:
        _orphan_loss_cut()
    except Exception as _e:
        _log(f"❌ 고아 종목 손절 오류: {_e}", "error")

    # ── ★ 해외주식 매매 루프 (미국 정규장 중일 때만) ──────────
    _us_trading_loop()

    # ══════════════════════════════════════════════════════════
    # ★ SELL 재시도 큐 처리 (매도 실패 자동 재시도)
    # ══════════════════════════════════════════════════════════
    try:
        _flush_sell_retry_q()
    except Exception as _rqe:
        _log(f"❌ [SELL 재시도 큐] 처리 오류: {_rqe}", "error")

    # ══════════════════════════════════════════════════════════
    # ★ Watchdog: 규칙 위반 감지 + 자동 복구
    #   W1: 익절조건충족 HOLD → ERROR + 강제매도
    #   W2: 손절조건충족 HOLD → ERROR + 강제매도
    #   W3: 시간청산조건충족 HOLD → ERROR + 강제매도
    #   W4: KIS qty=0 내부있음 → ERROR + 포지션제거
    #   W5: SELL재시도큐 3분+ → CRITICAL
    # ══════════════════════════════════════════════════════════
    try:
        _watchdog()
    except Exception as _wde:
        _log(f"❌ [Watchdog] 오류: {_wde}", "error")

    # 잔고 실시간 업데이트
    try:
        balance = _api.get_balance()
        socketio.emit("balance_update", balance)
    except Exception:
        pass


# ── ★ 해외주식 매매 루프 ─────────────────────────────────────
def _us_get_elapsed_ratio() -> float:
    """미국 정규장 경과 비율 (0.0~1.0) — ET 09:30~16:00 기준"""
    try:
        import pytz
        et_now = datetime.now(pytz.timezone("America/New_York"))
        market_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
        elapsed_min = max(0.0, (et_now - market_open).total_seconds() / 60)
        return min(elapsed_min / 390.0, 1.0)
    except Exception:
        return 0.5


def _us_prefetch_realtime(symbols: list) -> dict:
    """
    yfinance 개별 fast_info 조회 — 종목당 1회 (배치 방식 제거)
    ★ yf.Tickers() 배치는 내부적으로 직렬 처리라 오히려 더 느림
    → 개별 yf.Ticker(sym).fast_info 방식으로 전환, 각 종목 timeout 3s
    반환: {symbol: {cur_price, previous_close, today_volume, avg_volume_90d, elapsed_ratio}}
    """
    import yfinance as yf
    import signal as _signal

    cache: dict = {}
    elapsed_ratio = _us_get_elapsed_ratio()

    for sym in symbols:
        try:
            fi   = yf.Ticker(sym).fast_info
            cur  = float(getattr(fi, "last_price",               0) or 0)
            prev = float(getattr(fi, "previous_close",           0) or 0)
            tvol = int(getattr(fi,   "last_volume",              0) or 0)
            avgv = int(getattr(fi,   "three_month_average_volume",0) or 0)
            if cur > 0:
                cache[sym] = {
                    "cur_price":      cur,
                    "previous_close": prev,
                    "today_volume":   tvol,
                    "avg_volume_90d": avgv,
                    "elapsed_ratio":  elapsed_ratio,
                    "change_rate":    round((cur - prev) / prev * 100, 2) if prev > 0 else 0.0,
                }
        except Exception:
            pass

    _log(f"📡 실시간 프리페치: {len(cache)}/{len(symbols)}개 완료", "info")
    return cache


# ── 미체결 주문 추적 (재주문 30초 대기용) ─────────────────────────────
_us_pending_sell: dict = {}   # {symbol: {"ts": float, "qty": int, "reason": str}}
# ── 매도 성공 후 60초간 중복 재주문 차단 버퍼 ──────────────────────────
_us_sold_buffer: dict = {}    # {symbol: ts(float)}  — 매도 주문 접수 성공 시각


def _us_realbalance_force_sell_check():
    """
    ★ 실제 KIS 해외주식 잔고 기준 강제 손절 청산 (v30 긴급 추가)

    - 시스템 내부 positions가 아닌 KIS 실잔고 기준으로 손익률 계산
    - 실제 손익률 ≤ -5% 이하 → 즉시 시장가(ORD_DVSN=01) 전량 매도
    - LOSS_LIMIT / MANUAL_STOP / 신규매수차단 상태여도 매도는 반드시 실행
    - 매도 실패 시 KIS rt_cd / msg_cd / msg1 / tr_id 로그 출력
    - 미체결 추적: 30초 후 자동 재주문
    - 매 루프마다 [US 실제잔고 청산점검] 로그 출력
    """
    global _us_pending_sell, _us_sold_buffer

    if _api is None:
        return

    from utils.market_session import us_session_info
    sess = us_session_info()
    # ★ 반드시 미국 정규장(ET 09:30~16:00)에서만 매도 주문 가능
    if sess.get("session") != "미국정규장":
        return

    # ── KIS 실잔고 조회 ──────────────────────────────────────────────
    try:
        bal = _api.get_us_balance()
    except Exception as e_bal:
        logger.warning(f"[US 실잔고 청산점검] 잔고조회 실패: {e_bal}")
        return

    holdings = bal.get("holdings", [])
    if not holdings:
        return

    now_ts = time.time()

    # ── 매도 성공 버퍼 만료 항목 정리 (60초 경과 시 제거) ──────────
    _expired = [s for s, t in _us_sold_buffer.items() if now_ts - t >= 60]
    for s in _expired:
        _us_sold_buffer.pop(s, None)

    for h in holdings:
        symbol    = h.get("symbol", "")
        name      = h.get("name", symbol) or symbol
        real_qty  = int(h.get("qty", 0))
        # ★ 실제 매도가능수량 (T+2 결제 중인 수량 제외) — 없으면 보유수량으로 폴백
        sell_qty  = int(h.get("sell_qty", real_qty) or real_qty)
        avg_price = float(h.get("avg_price", 0) or 0)
        cur_price = float(h.get("cur_price", 0) or 0)
        excd      = h.get("excd", "NASD")

        if real_qty <= 0 or avg_price <= 0:
            continue

        # ── 매도 성공 버퍼: 60초 내 중복 재주문 차단 ──────────────
        _sold_at = _us_sold_buffer.get(symbol)
        if _sold_at is not None:
            _remain = 60 - (now_ts - _sold_at)
            logger.info(
                f"[US 실제잔고 청산점검] "
                f"종목={name}({symbol}) | 매도주문접수완료 대기중 "
                f"(체결/정산 대기 {_remain:.0f}초 남음) | KIS잔고 미반영 — 재주문 차단"
            )
            continue

        # ── 현재가 0이면 실시간 재조회 ────────────────────────────
        if cur_price <= 0:
            try:
                rt = _api.get_us_current_price(symbol, excd)
                cur_price = float(rt.get("price", 0) or 0)
            except Exception:
                pass

        # ── 실제 손익률 계산 ──────────────────────────────────────
        if cur_price > 0 and avg_price > 0:
            real_pnl_pct = (cur_price - avg_price) / avg_price * 100
        else:
            real_pnl_pct = 0.0

        FORCE_SELL_THRESHOLD = -5.0   # -5% 이하 강제 손절
        is_force_sell = real_pnl_pct <= FORCE_SELL_THRESHOLD

        # ── 미체결 재주문 확인 (30초 대기) ───────────────────────
        pending = _us_pending_sell.get(symbol)
        if pending:
            elapsed_since_pending = now_ts - pending["ts"]
            if elapsed_since_pending < 30:
                logger.info(
                    f"[US 실제잔고 청산점검] "
                    f"종목={name}({symbol}) | "
                    f"실제보유수량={real_qty}주(매도가능={sell_qty}주) | "
                    f"평균단가=${avg_price:.2f} | "
                    f"현재가=${cur_price:.2f} | "
                    f"실제손익률={real_pnl_pct:+.2f}% | "
                    f"청산조건={'YES(-5%이하)' if is_force_sell else 'NO'} | "
                    f"매도시도여부=대기중(재주문까지 {30 - elapsed_since_pending:.0f}초) | "
                    f"매도결과=미체결대기 | "
                    f"실패사유=없음"
                )
                continue
            # 30초 경과 → 재주문
            logger.warning(
                f"[US 실제잔고 청산점검] ⚠️ {name}({symbol}) 미체결 30초 경과 → 재주문"
            )
            _us_pending_sell.pop(symbol, None)
            # 재주문 시에는 최신 sell_qty 사용 (pending qty 무시)

        # ── 로그 출력 (매 루프) ───────────────────────────────────
        logger.info(
            f"[US 실제잔고 청산점검] "
            f"종목={name}({symbol}) | "
            f"실제보유수량={real_qty}주(매도가능={sell_qty}주) | "
            f"평균단가=${avg_price:.2f} | "
            f"현재가=${cur_price:.2f} | "
            f"실제손익률={real_pnl_pct:+.2f}% | "
            f"청산조건={'YES(-5%이하)' if is_force_sell else f'NO({real_pnl_pct:+.2f}% > -5%)'} | "
            f"매도시도여부={'YES' if is_force_sell else 'NO'} | "
            f"매도결과={'주문시도' if is_force_sell else '유지'} | "
            f"실패사유=없음"
        )

        if not is_force_sell:
            continue

        # ════════════════════════════════════════════════════════
        # ★ 강제 손절 매도
        # ★ KIS 해외주식은 시장가(price=0) 미지원
        #   → 현재가 기준 지정가로 주문 (슬리피지 허용 -1%)
        # ★ 매도수량: sell_qty (T+2 결제완료분만)
        #   sell_qty=0 이면 T+2 미결제 전량 → 즉시 재시도 큐 등록
        # LOSS_LIMIT / MANUAL_STOP / 신규매수차단 상태 완전 무시
        # ════════════════════════════════════════════════════════

        # ── [US ORDER CHECK] 주문 직전 KIS 주문가능금액 실시간 조회 ──────
        # frcr_ord_psbl_amt1 / ovrs_ord_psbl_amt / ovrs_max_ord_psbl_qty
        # 매도 주문 직전마다 신규 조회 (캐시 없이 항상 최신 값 사용)
        _order_check_usd  = 0.0
        _order_check_krw  = 0.0
        _order_check_mq   = "?"
        _order_check_ok   = False
        _order_check_err  = ""
        try:
            _avail = _api.get_us_available_amounts(symbol=symbol, excd=excd)
            _order_check_usd = _avail.get("usd", 0.0)   # frcr_ord_psbl_amt1
            _order_check_krw = _avail.get("krw", 0.0)   # ovrs_ord_psbl_amt
            _raw_avail       = _avail.get("raw", {})
            _order_check_mq  = _raw_avail.get("ovrs_max_ord_psbl_qty", "?")
            _order_check_ok  = True
        except Exception as _oc_e:
            _order_check_err = str(_oc_e)

        _est_amt_usd  = round(cur_price * sell_qty, 2) if sell_qty > 0 else 0.0
        _basis        = "USD(frcr_ord_psbl_amt1)" if _order_check_usd > 0 else \
                        "KRW(ovrs_ord_psbl_amt)"  if _order_check_krw > 0 else "조회실패"
        _check_result = "주문가능" if (
            _order_check_ok and (_order_check_usd > 0 or _order_check_krw > 0)
        ) else ("조회실패" if _order_check_err else "금액부족(0원)")

        logger.info(
            f"[US ORDER CHECK] "
            f"종목={name}({symbol}) | "
            f"현재가=${cur_price:.2f} | "
            f"주문수량={sell_qty}주(T+2결제가능) / 보유={real_qty}주 | "
            f"예상주문금액=${_est_amt_usd:.2f} | "
            f"USD 주문가능금액(frcr_ord_psbl_amt1)=${_order_check_usd:.2f} | "
            f"원화 주문가능금액(ovrs_ord_psbl_amt)={_order_check_krw:,.0f}원 | "
            f"최대주문가능수량(ovrs_max_ord_psbl_qty)={_order_check_mq}주 | "
            f"실제 사용기준={_basis} | "
            f"결과={_check_result}"
            + (f" | 조회오류={_order_check_err}" if _order_check_err else "")
        )

        # ── T+2 미결제: 매도가능수량=0 → 주문 자체 불필요, 5초 후 재확인 ──
        if sell_qty <= 0:
            # pending이 이미 있으면 덮어쓰지 않음 (재확인 타이머 유지)
            if symbol not in _us_pending_sell:
                logger.warning(
                    f"[US 실제잔고 청산점검] ⚠️ T+2미결제 {name}({symbol}) "
                    f"매도가능수량=0 (보유={real_qty}주, 손익={real_pnl_pct:+.2f}%) "
                    f"→ 결제완료 후 자동 재시도 (5초 후 재확인)"
                )
                _us_pending_sell[symbol] = {
                    "ts":     now_ts - 25,   # 5초(30-25) 후 재확인
                    "qty":    real_qty,
                    "reason": f"T+2미결제대기 {real_pnl_pct:+.2f}%",
                }
            continue

        def _try_sell_with_qty(qty_to_sell: int, price_to_use: float,
                               method_label: str) -> dict:
            """
            지정가 매도 시도.
            - APBK0988(수량초과)만 1주 감소 후 재시도 (최대 3회)
            - 그 외 오류는 즉시 반환 (T+2미결제/잔고없음 등)
            - 항상 KIS 원본 응답 로그 출력
            """
            _qty = qty_to_sell
            for attempt in range(3):
                if _qty <= 0:
                    break
                try:
                    _r = _api.sell_us(symbol, _qty, price_to_use, excd, ord_dvsn="00")
                except Exception as _e:
                    _r = {"rt_cd": "9", "msg_cd": "EXC", "msg1": str(_e)}
                _rc = _r.get("rt_cd", "9")
                _mc = _r.get("msg_cd", "")
                _m1 = _r.get("msg1", "")
                # ★ KIS 원본 응답 항상 로그 (오류 원인 추적용)
                logger.info(
                    f"[US 실제잔고 청산점검] KIS응답 | "
                    f"{symbol} {_qty}주 ${price_to_use:.2f} [{method_label}] | "
                    f"rt_cd={_rc} | msg_cd={_mc} | msg1={_m1!r} | attempt={attempt+1}"
                )
                if _rc == "0":
                    return _r
                # ★ APBK0988(가능수량 초과)만 수량 감소 재시도
                if _mc == "APBK0988" or "가능수량보다 큽니다" in _m1:
                    logger.warning(
                        f"[US 실제잔고 청산점검] ⚠️ 수량초과 {_qty}주→{_qty-1}주 재시도 | "
                        f"msg_cd={_mc} | {_m1!r}"
                    )
                    _qty -= 1
                    continue
                # T+2미결제(APBK0050등) / 기타 오류 → 수량감소 불필요, 즉시 반환
                logger.error(
                    f"[US 실제잔고 청산점검] ❌ 주문거절(수량무관) {symbol} | "
                    f"rt_cd={_rc} | msg_cd={_mc} | msg1={_m1!r}"
                )
                return _r
            return {"rt_cd": "9", "msg_cd": "QTY_FAIL",
                    "msg1": f"수량조정후실패(최종qty={_qty})"}

        if cur_price <= 0:
            logger.error(
                f"[US 실제잔고 청산점검] ❌ 현재가 0 주문 불가 | "
                f"{name}({symbol}) → 30초 후 재주문 예약"
            )
            _us_pending_sell[symbol] = {
                "ts": now_ts, "qty": sell_qty or real_qty,
                "reason": f"강제손절(현재가0) {real_pnl_pct:+.2f}%",
            }
            continue

        # ── 1차: sell_qty 기준 현재가 지정가 ────────────────────
        order_qty   = sell_qty if sell_qty > 0 else real_qty
        order_price = round(cur_price, 2)   # 현재가 그대로 지정가
        sell_method = f"지정가(현재가${order_price:.2f})"

        sell_result = _try_sell_with_qty(order_qty, order_price, sell_method)
        rt_cd  = sell_result.get("rt_cd",  "?")
        msg_cd = sell_result.get("msg_cd", "?")
        msg1   = sell_result.get("msg1",   "?")
        tr_id  = (sell_result.get("output", {}) or {}).get(
                     "ODNO", sell_result.get("tr_id", "N/A"))

        if rt_cd == "0":
            pnl_usd = (order_price - avg_price) * order_qty
            logger.warning(
                f"[US 실제잔고 청산점검] "
                f"종목={name}({symbol}) | "
                f"실제보유수량={real_qty}주(매도가능={sell_qty}주) | "
                f"평균단가=${avg_price:.2f} | "
                f"현재가=${cur_price:.2f} | "
                f"실제손익률={real_pnl_pct:+.2f}% | "
                f"청산조건=YES(-5%이하) | "
                f"매도시도여부=YES | "
                f"매도결과=✅{sell_method}주문성공({order_qty}주) | "
                f"실패사유=없음 | "
                f"PnL=${pnl_usd:+.2f}"
            )
            try:
                if _us_strategy and symbol in _us_strategy.pos_mgr.positions:
                    _us_strategy.pos_mgr.remove(symbol)
                    logger.info(f"[US 실제잔고 청산점검] 내부포지션 제거: {symbol}")
            except Exception:
                pass
            try:
                from screener.us_watchlist_manager import on_sell_complete
                on_sell_complete(symbol, pnl_usd)
            except Exception:
                pass
            _us_pending_sell.pop(symbol, None)
            # ★ 매도 주문 접수 성공 → 60초간 중복 재주문 차단
            _us_sold_buffer[symbol] = now_ts
            logger.info(
                f"[US 실제잔고 청산점검] ✅ 매도버퍼 등록 {symbol} → 60초간 재주문 차단"
            )

        else:
            # ── 1차 실패 로그 ────────────────────────────────────
            logger.error(
                f"[US 실제잔고 청산점검] ❌ {sell_method} 실패 | "
                f"종목={name}({symbol}) | "
                f"rt_cd={rt_cd} | msg_cd={msg_cd} | msg1={msg1!r} | tr_id={tr_id}"
            )

            # ── 2차: 슬리피지 허용 현재가×0.99 지정가 재시도 ──────
            limit_price2 = round(cur_price * 0.99, 2)
            sell_method2 = f"지정가슬리피지(${limit_price2:.2f})"
            sell_result2 = _try_sell_with_qty(order_qty, limit_price2, sell_method2)

            rt_cd2  = sell_result2.get("rt_cd",  "?")
            msg_cd2 = sell_result2.get("msg_cd", "?")
            msg1_2  = sell_result2.get("msg1",   "?")

            if rt_cd2 == "0":
                pnl_usd = (limit_price2 - avg_price) * order_qty
                logger.warning(
                    f"[US 실제잔고 청산점검] "
                    f"종목={name}({symbol}) | "
                    f"실제보유수량={real_qty}주(매도가능={sell_qty}주) | "
                    f"평균단가=${avg_price:.2f} | "
                    f"현재가=${cur_price:.2f} | "
                    f"실제손익률={real_pnl_pct:+.2f}% | "
                    f"청산조건=YES(-5%이하) | "
                    f"매도시도여부=YES | "
                    f"매도결과=✅{sell_method2}성공({order_qty}주) | "
                    f"실패사유=없음 | "
                    f"PnL=${pnl_usd:+.2f}"
                )
                try:
                    if _us_strategy and symbol in _us_strategy.pos_mgr.positions:
                        _us_strategy.pos_mgr.remove(symbol)
                except Exception:
                    pass
                try:
                    from screener.us_watchlist_manager import on_sell_complete
                    on_sell_complete(symbol, pnl_usd)
                except Exception:
                    pass
                _us_pending_sell.pop(symbol, None)
                # ★ 2차 매도 주문 접수 성공 → 60초간 중복 재주문 차단
                _us_sold_buffer[symbol] = now_ts
                logger.info(
                    f"[US 실제잔고 청산점검] ✅ 매도버퍼 등록(2차) {symbol} → 60초간 재주문 차단"
                )
            else:
                # 2차도 실패 → 30초 후 재주문 큐
                logger.error(
                    f"[US 실제잔고 청산점검] ❌ {sell_method2} 도 실패 | "
                    f"종목={name}({symbol}) | "
                    f"rt_cd={rt_cd2} | msg_cd={msg_cd2} | msg1={msg1_2!r} | "
                    f"→ 30초 후 재주문 예약"
                )
                _us_pending_sell[symbol] = {
                    "ts":     now_ts,
                    "qty":    order_qty,
                    "reason": f"강제손절재시도 {real_pnl_pct:+.2f}%",
                }


def _us_trading_loop():
    """
    미국 정규장(ET 09:30~16:00) 중 호출.
    _us_watch_list 종목들을 순회하며 USStrategyManager.run() 실행.
    ★ 루프 시작 시 yfinance 배치 프리페치 → 종목별 개별 API 호출 최소화
    """
    global _us_last_signals
    # ★ V2 LIVE 전환 보호: V2가 실행 중이면 V1 US 주문루프 완전 차단
    if _is_v2_live():
        logger.info("[V1 주문루프] ⛔ V2 LIVE 실행 중 — V1 US 주문루프 차단")
        return
    if not _bot_running or _us_strategy is None:
        return

    us_sess = us_session_info()
    # ★ tradeable=False(미국 휴장)이어도 보유 포지션이 있으면 루프 실행
    #   → _manage_position 내 tradeable gate가 포지션 있을 때 손절/익절 허용
    has_positions = (
        _us_strategy is not None
        and len(_us_strategy.pos_mgr.positions) > 0
    )
    # ★ 실제 KIS 잔고 보유 여부도 확인 (내부 positions 미등록 유령 종목 대응)
    _has_real_holdings = False
    if not has_positions and _api is not None:
        try:
            _rb = _api.get_us_balance()
            _has_real_holdings = bool(_rb.get("holdings"))
        except Exception:
            pass

    if not us_sess["tradeable"] and not has_positions and not _has_real_holdings:
        return   # 미국 휴장 + 보유 포지션 없음(내부+실잔고) → 조용히 스킵

    # ★ v30 긴급: 루프 최상단에서 실제 KIS 잔고 기준 -5% 강제 손절 먼저 실행
    # LOSS_LIMIT/MANUAL_STOP 상태와 완전히 독립적으로 동작
    try:
        _us_realbalance_force_sell_check()
    except Exception as _fsc_err:
        _log(f"⚠️ [실잔고청산점검 오류] {_fsc_err}", "error")

    watch = list(_us_watch_list)
    symbols = [s.get("symbol", "") for s in watch if s.get("symbol")]

    if us_sess["tradeable"]:
        _log(f"🇺🇸 [{us_sess['time_et']}] 해외주식 신호 점검 ({len(symbols)}종목)...", "info")
    else:
        _log(f"🇺🇸 [{us_sess['time_et']}] 해외주식 휴장중 — 보유{len(_us_strategy.pos_mgr.positions)}종목 포지션 관리 중...", "info")

    # ★ 전체 실시간 데이터 배치 프리페치 (yfinance 1회 묶음 호출)
    rt_cache = _us_prefetch_realtime(symbols)

    # _us_strategy에 캐시 주입 (run() 내부 _fetch_realtime이 캐시 우선 사용)
    _us_strategy.set_realtime_cache(rt_cache)

    for stock in watch:
        symbol = stock.get("symbol", "")
        name   = stock.get("name", symbol)
        try:
            result = _us_strategy.run(stock)
            _us_last_signals[symbol] = result
            action = result.get("action", "HOLD")

            if action == "BUY":
                buy_price = float(result.get("price", 0))
                _log(
                    f"🟢 [US매수] {name}({symbol}) "
                    f"${buy_price:.2f} × {result['qty']}주 "
                    f"≈ ${result.get('amount_usd',0):.0f}",
                    "buy"
                )
                # ★ 거래 횟수 카운트 (매수)
                _record_trade_pnl(pnl_usd=0.0, is_trade=True)
                # ★ v3: 매수 진입 시각 기록 (48h 성과 추적용)
                try:
                    from screener.us_watchlist_manager import record_watch_entry
                    record_watch_entry(symbol, buy_price, source="trade")
                except Exception:
                    pass
            elif action == "BUY_FAIL":
                _log(f"❌ [US BUY_FAIL] {name}({symbol}) — {result.get('reason','')}", "error")
            elif action == "SELL":
                pnl_usd_val = float(result.get("pnl_usd", 0))
                pnl_pct_val = float(result.get("pnl_pct", 0))
                emoji = "💰" if pnl_usd_val >= 0 else "🔴"
                _log(
                    f"{emoji} [US매도] {name}({symbol}) "
                    f"손익 ${pnl_usd_val:+.2f} ({pnl_pct_val:+.1f}%)",
                    "sell"
                )
                # ★ 당일 미국 수익 누적
                _record_trade_pnl(pnl_usd=pnl_usd_val, is_trade=True)
                # ★ v3 통합 훅: on_sell_complete (익절→당일재진입금지+24h쿨다운 / 손절→7일페널티+3일손실기록)
                try:
                    from screener.us_watchlist_manager import on_sell_complete
                    on_sell_complete(symbol, pnl_usd_val)
                except Exception as _hook_err:
                    _log(f"⚠️ [매도훅 오류] {symbol}: {_hook_err}", "error")
            # HOLD/SKIP 은 로그 미출력 (노이즈 방지)
        except Exception as e:
            _log(f"❌ [US] {name}({symbol}) 오류: {e}", "error")

    # 해외잔고 실시간 업데이트
    try:
        us_bal = _api.get_us_balance()
        socketio.emit("us_balance_update", us_bal)
    except Exception:
        pass


def _handle_etf_trade(stock: dict, asset_type: str, regime: str, sess: dict,
                      cached_cash: float = None, cached_balance: dict = None):
    """
    ETF 전용 매매 로직.
    국면(BULL/LATERAL/BEAR) + 기술적 점수 기반으로 매수/매도 결정.

    전략:
      - 매수: 가격이 MA20 위에 있고 국면이 해당 ETF에 적합하면 매수
      - 매도: 손절(-7%) 또는 국면 변경으로 부적합해진 경우 매도
      - 포지션 크기: _get_etf_position_ratio() 기준
    cached_cash / cached_balance: 루프에서 미리 조회한 잔고 (KIS TPS 절감)
    """
    from screener.asset_universe import (
        ASSET_ETF_LEVERAGE, ASSET_ETF_INVERSE, ASSET_ETF_GENERAL,
    )
    code = stock["code"]
    name = stock["name"]

    # 레버리지 ETF — BULL 아니면 즉시 매도 (이미 보유한 경우)
    if asset_type == ASSET_ETF_LEVERAGE and regime != "BULL":
        _etf_force_sell(code, name, f"국면전환({regime}) — 레버리지 해제")
        return

    # 인버스 ETF — BULL 국면에서 보유 중이면 즉시 매도
    if asset_type == ASSET_ETF_INVERSE and regime == "BULL":
        _etf_force_sell(code, name, f"BULL 전환 — 인버스 헤지 해제")
        return

    try:
        if _api is None:
            return

        # 현재가 + MA20 조회 (일봉 20개)
        candles = _api.get_ohlcv(code, period="D", count=25)
        if len(candles) < 20:
            _log(f"⚠️ ETF {name} — 캔들 데이터 부족({len(candles)}개)", "info")
            return

        closes  = [c["close"] for c in candles]
        ma20    = sum(closes[-20:]) / 20
        cur     = closes[-1]
        ret_pct = (cur - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0

        # 기보유 여부 확인: 루프 캐시 우선, 없으면 직접 조회
        if cached_balance and cached_balance.get("holdings") is not None:
            bal = cached_balance
        else:
            bal = _api.get_balance()
        holdings    = {h["code"]: h for h in bal.get("holdings", [])}
        total_eval  = bal.get("total_eval", 0) or Config.MAX_TOTAL_INVESTMENT
        is_held     = code in holdings

        # ─ 매도 판단 ───────────────────────────────────────────
        if is_held:
            pos    = holdings[code]
            avg_px = float(pos.get("avg_price", cur))
            pnl    = (cur - avg_px) / avg_px * 100

            stop_loss_pct = -7.0   # ETF 손절선 (개별주식보다 타이트)

            if pnl <= stop_loss_pct:
                qty = pos.get("qty", 1)
                _log(f"🔴 ETF 손절매도 {name} PnL:{pnl:.1f}% (≤{stop_loss_pct}%)", "sell")
                try:
                    _api.sell(code, qty, cur)
                    notifier.notify_sell(name, code, cur, qty, (cur-avg_px)*qty, f"ETF손절{pnl:.1f}%")
                except Exception as e:
                    _log(f"❌ ETF 손절 주문 실패 {name}: {e}", "error")
                return

            # 인버스/일반: MA20 아래로 내려왔고 수익 중이면 익절
            if asset_type in (ASSET_ETF_INVERSE, ASSET_ETF_GENERAL):
                if cur < ma20 * 0.98 and pnl >= 5.0:
                    qty = pos.get("qty", 1)
                    _log(f"💰 ETF 익절매도 {name} PnL:{pnl:.1f}% (MA20 하향 이탈)", "sell")
                    try:
                        _api.sell(code, qty, cur)
                        notifier.notify_sell(name, code, cur, qty, (cur-avg_px)*qty, f"ETF익절{pnl:.1f}%")
                    except Exception as e:
                        _log(f"❌ ETF 익절 주문 실패 {name}: {e}", "error")
                    return

            _log(
                f"📦 ETF 보유 {name} [{asset_type}] "
                f"현재가:{cur:,} MA20:{ma20:,.0f} "
                f"PnL:{pnl:+.1f}%",
                "info"
            )
            _last_signals[code] = {
                "action": "HOLD", "price": cur, "pnl_pct": pnl,
                "asset_type": asset_type, "regime": regime,
                "buy_score": 0, "sell_score": 0,
            }
            return

        # ─ 매수 판단 ───────────────────────────────────────────
        # ★ 15:20 이후 신규 매수 절대 차단 (ETF 포함)
        if not sess.get("allow_new_buy", False) or not allow_new_buy_now():
            _blk_r = sess.get("buy_block_reason") or f"15:20 이후 신규 매수 차단 ({sess.get('time_kst','?')} KST)"
            _log(
                f"🚫 [ETF 매수차단] {name}({code}) — {_blk_r} | "
                f"세션={sess.get('session','?')} | BUY_CUTOFF={BUY_CUTOFF_TIME.strftime('%H:%M')} KST",
                "warning"
            )
            _last_signals[code] = {
                "action": "HOLD", "price": cur,
                "asset_type": asset_type, "regime": regime,
                "buy_score": 0, "sell_score": 0,
                "reason": _blk_r,
            }
            return

        # 조건: 현재가 > MA20 (상승 추세) + 국면 적합
        above_ma20 = cur > ma20

        # 인버스는 하락 추세(가격 < MA20)에서 매수
        if asset_type == ASSET_ETF_INVERSE:
            above_ma20 = cur < ma20  # 인버스: MA20 하회 = 지수 하락 = 인버스 매수 신호

        if not above_ma20:
            _log(
                f"⏸ ETF 대기 {name} [{asset_type}] "
                f"현재가:{cur:,} MA20:{ma20:,.0f} "
                f"({'MA20 상회 대기' if asset_type == ASSET_ETF_INVERSE else 'MA20 하회'})",
                "info"
            )
            _last_signals[code] = {
                "action": "HOLD", "price": cur,
                "asset_type": asset_type, "regime": regime,
                "buy_score": 0, "sell_score": 0,
            }
            return

        # 포지션 크기 계산
        ratio       = _get_etf_position_ratio(asset_type, regime)
        invest_amt  = int(total_eval * ratio)
        invest_amt  = max(invest_amt, 100_000)   # 최소 10만원
        invest_amt  = min(invest_amt, Config.MAX_INVESTMENT_PER_STOCK)
        qty         = max(1, invest_amt // cur)

        # 레이블
        type_label  = {"ETF_LEVERAGE": "🔶 레버리지", "ETF_INVERSE": "🔴 인버스",
                       "ETF_GENERAL":  "📦 일반ETF"}.get(asset_type, "ETF")

        _log(
            f"🟢 ETF 매수 {type_label} {name} "
            f"{cur:,}원 × {qty}주 "
            f"({ratio*100:.0f}% / {invest_amt:,}원) [국면:{regime}]",
            "buy"
        )
        try:
            _api.buy(code, qty, cur)
            notifier.notify_buy(name, code, cur, qty,
                                f"ETF매수({asset_type}/{regime})")
        except Exception as e:
            _log(f"❌ ETF 매수 주문 실패 {name}: {e}", "error")

        _last_signals[code] = {
            "action": "BUY", "price": cur, "qty": qty,
            "asset_type": asset_type, "regime": regime,
            "buy_score": 1, "sell_score": 0,
        }

    except Exception as e:
        _log(f"❌ ETF 처리 오류 {name}: {e}", "error")


def _etf_force_sell(code: str, name: str, reason: str):
    """국면 변경으로 ETF 강제 매도 (보유 중인 경우에만)"""
    if _api is None:
        return
    try:
        # 루프 캐시 우선 사용 (holdings 있으면 재조회 안 함)
        bal = _loop_balance if _loop_balance.get("holdings") is not None else _api.get_balance()
        holdings = {h["code"]: h for h in bal.get("holdings", [])}
        if code not in holdings:
            return
        pos = holdings[code]
        qty = pos.get("qty", 0)
        cur = pos.get("cur_price", 0)
        avg = pos.get("avg_price", cur)
        if qty > 0:
            _api.sell(code, qty, cur)
            pnl = (cur - avg) * qty
            _log(f"🔄 ETF 강제매도 {name} — {reason} | 손익:{pnl:+,.0f}원", "sell")
            notifier.notify_sell(name, code, cur, qty, pnl, reason)
    except Exception as e:
        _log(f"❌ ETF 강제매도 실패 {name}: {e}", "error")


def _orphan_loss_cut():
    """
    보유 종목 전체 손절·트레일링 감시 (관심종목 등록 여부 무관).

    매 루프마다 KIS 실제 잔고의 모든 종목을 순회해서:
      1) 실질수익률 ≤ STOP_LOSS_PCT(-5%)      → 즉시 전량 손절
      2) 트레일링 활성화 후 고점대비 ≤ -2%    → 즉시 전량 매도
    watch_list 에 있는 종목도 포함 (strategy_manager 가 SKIP 한 경우 이중 보장)

    ★ 국내 비정규장(휴장) 중에는 손절 주문 시도 자체를 차단
      (장 마감 후 잔고 조회는 하되 실제 매도 주문은 보내지 않음)
    """
    if _api is None:
        return
    # ── 국내 장 가능 여부 체크 (휴장 중엔 매도 주문 X) ──────────────
    _kr_tradeable = session_info().get("tradeable", False)
    try:
        from strategies.pyramid_strategy import (
            STOP_LOSS_PCT, TRAILING_STOP_PCT, TRAILING_ACTIVATE_PCT,
            price_for_net_pct_from_cost,
        )
        from screener.transaction_cost import net_profit_pct_from_cost

        # 루프 캐시 사용 (holdings 없으면 직접 조회)
        import time as _t
        if _loop_balance.get("holdings") is not None and            (_t.time() - _loop_balance_ts) < 120:
            bal = _loop_balance
        else:
            bal = _api.get_balance()
        holdings = bal.get("holdings", [])
        if not holdings:
            return

        # pyramid 객체에서 직접 PyramidPosition 접근 (avg_price, highest_price)
        pyramid_obj = _strategy_mgr.pyramid if _strategy_mgr else None

        for h in holdings:
            code = h.get("code", "")
            name = h.get("name", code)
            qty  = int(h.get("qty", 0))
            if qty <= 0 or not code:
                continue

            # ── avg_price: pyramid 객체 우선, 없으면 KIS 잔고 ──────────
            pyramid_pos_obj = pyramid_obj.positions.get(code) if pyramid_obj else None
            if pyramid_pos_obj is not None:
                avg_px      = float(pyramid_pos_obj.avg_price)
                highest_px  = float(pyramid_pos_obj.highest_price)
            else:
                avg_px      = float(h.get("avg_price", 0))
                highest_px  = avg_px
            if avg_px <= 0:
                continue

            # ── cur_price: KIS 잔고 우선, 0이면 API 직접 조회 ──────────
            cur_px = float(h.get("cur_price", 0))
            if cur_px <= 0:
                try:
                    cur_data = _api.get_current_price(code)
                    cur_px   = float(cur_data.get("price", 0))
                except Exception:
                    cur_px = 0
            if cur_px <= 0:
                # 오늘 OHLCV 마지막 종가 사용
                try:
                    candles = _api.get_ohlcv(code, period="D", count=1)
                    if candles:
                        cur_px = float(candles[-1].get("close", 0))
                        # 당일 고가도 반영
                        td_high = float(candles[-1].get("high", cur_px))
                        if pyramid_pos_obj and td_high > highest_px:
                            pyramid_pos_obj.update_high(td_high)
                            highest_px = pyramid_pos_obj.highest_price
                except Exception:
                    pass
            if cur_px <= 0:
                _log(f"⚠️ [{name}({code})] 현재가 조회 실패 — 손절 점검 스킵", "info")
                continue

            # highest_price 갱신
            if pyramid_pos_obj and cur_px > highest_px:
                pyramid_pos_obj.update_high(cur_px)
                highest_px = pyramid_pos_obj.highest_price

            # ── 실질수익률 + 트레일링 계산 ────────────────────────────
            net_pct    = net_profit_pct_from_cost(avg_px, cur_px)
            act_price  = price_for_net_pct_from_cost(avg_px, TRAILING_ACTIVATE_PCT)
            trail_pct  = (cur_px - highest_px) / highest_px * 100 if highest_px > 0 else 0
            is_orphan  = code not in {s["code"] for s in _watch_list}

            # ── 판단 ──────────────────────────────────────────────────
            sell_reason = None

            if net_pct <= STOP_LOSS_PCT:
                sell_reason = (
                    f"손절 실질{net_pct:.2f}% ≤ {STOP_LOSS_PCT}% "
                    f"(매입{avg_px:,.0f}원→현재{cur_px:,.0f}원)"
                )
            elif highest_px >= act_price and trail_pct <= TRAILING_STOP_PCT:
                sell_reason = (
                    f"트레일링스탑 고점대비{trail_pct:.2f}% ≤ {TRAILING_STOP_PCT}% "
                    f"(고점{highest_px:,.0f}원→현재{cur_px:,.0f}원, 실질{net_pct:+.2f}%)"
                )

            if sell_reason:
                tag = "고아종목 " if is_orphan else ""
                # ★ 국내 비정규장(휴장) 중에는 매도 주문 차단
                if not _kr_tradeable:
                    _log(
                        f"⏸️ [{tag}매도대기] {name}({code}) {sell_reason} "
                        f"— 국내 휴장 중 주문 보류 (장 개시 후 자동 실행)",
                        "info"
                    )
                else:
                    _log(
                        f"🔴 [{tag}자동매도] {name}({code}) {sell_reason} — 전량 매도",
                        "sell"
                    )
                    try:
                        result = _api.sell(code, qty, 0)  # 시장가 전량 매도
                        if result.get("rt_cd") == "0":
                            if pyramid_obj:
                                pyramid_obj.positions.pop(code, None)
                                pyramid_obj._save()
                            _log(f"✅ 매도 체결 완료: {name}({code}) {qty}주", "sell")
                        else:
                            _log(f"❌ 매도 주문 실패 {name}: {result.get('msg1','?')}", "error")
                    except Exception as e:
                        _log(f"❌ 매도 주문 오류 {name}: {e}", "error")

            elif net_pct <= -2.0 and is_orphan:
                _log(
                    f"⚠️ [고아종목] {name}({code}) 실질{net_pct:.2f}% "
                    f"(손절선:{STOP_LOSS_PCT}%)", "info"
                )
            else:
                tag = "고아" if is_orphan else ""
                _log(
                    f"📋 [{tag}{name}({code})] 실질{net_pct:+.2f}% "
                    f"고점{highest_px:,.0f}원 트레일{trail_pct:+.1f}% — 홀드",
                    "info"
                )

        # 변경된 highest_price 저장
        if pyramid_obj:
            pyramid_obj._save()

    except Exception as e:
        _log(f"❌ 손절/트레일링 감시 오류: {e}", "error")



# ══════════════════════════════════════════════════════════════════
# ★ SELL 재시도 큐 — 매도 실패 시 최대 3회 자동 재시도
# ══════════════════════════════════════════════════════════════════
_sell_retry_q: list = []
# 항목 구조: {"code", "name", "qty", "attempt", "first_fail_ts", "reason"}

def _flush_sell_retry_q():
    """
    매도 실패 재시도 큐 처리 — _trading_loop() 말미에서 호출.
    ① 최대 3회 재시도
    ② 시도 간격: 1회차 즉시, 2회차 +5초, 3회차 +15초
    ③ 3회 모두 실패 시 ERROR 로그 + 큐에서 제거
    ④ 성공 시 pyramid 포지션 정리
    """
    global _sell_retry_q
    if not _sell_retry_q or _api is None:
        return

    import time as _t
    now_ts = _t.time()
    remain = []

    for item in _sell_retry_q:
        code    = item["code"]
        name    = item["name"]
        qty     = item["qty"]
        attempt = item["attempt"]          # 1, 2, 3
        first_ts = item["first_fail_ts"]
        reason  = item["reason"]

        # 재시도 대기시간 체크 (1차: 0s, 2차: 5s, 3차: 15s)
        wait_sec = [0, 5, 15][min(attempt - 1, 2)]
        if now_ts - first_ts < wait_sec:
            remain.append(item)
            continue

        if attempt > 3:
            _log(
                f"🚨 [SELL_RETRY_FAIL] {name}({code}) 매도 3회 재시도 모두 실패 — "
                f"수동 청산 필요! qty={qty}주 | 최초실패사유={reason}",
                "error"
            )
            continue  # 큐에서 제거

        _log(
            f"🔄 [SELL_RETRY {attempt}/3] {name}({code}) {qty}주 시장가 재매도 시도...",
            "warning"
        )
        try:
            result = _api.sell(code, qty, 0)   # 시장가
            if result.get("rt_cd") == "0":
                _log(f"✅ [SELL_RETRY 성공] {name}({code}) {qty}주 매도 완료", "sell")
                # pyramid 포지션 정리
                if _strategy_mgr and hasattr(_strategy_mgr, "pyramid"):
                    _strategy_mgr.pyramid.positions.pop(code, None)
                    _strategy_mgr.pyramid._save()
                # 성공 → 큐에서 제거 (remain에 추가 안 함)
            else:
                msg1 = result.get("msg1", "?")
                _log(
                    f"❌ [SELL_RETRY {attempt}/3 실패] {name}({code}): {msg1}",
                    "error"
                )
                item["attempt"] += 1
                item["first_fail_ts"] = now_ts  # 다음 대기 기산점 갱신
                remain.append(item)
        except Exception as e:
            _log(f"❌ [SELL_RETRY {attempt}/3 오류] {name}({code}): {e}", "error")
            item["attempt"] += 1
            item["first_fail_ts"] = now_ts
            remain.append(item)

    _sell_retry_q = remain


# ══════════════════════════════════════════════════════════════════
# ★ Watchdog — 규칙 위반 감지 + 자동 복구
# ══════════════════════════════════════════════════════════════════
def _watchdog():
    """
    매 루프 말미에서 호출되는 자율운영 감시자.

    감지 항목:
      W1. 익절 조건 충족인데 HOLD 중인 포지션
      W2. 손절 조건 충족인데 HOLD 중인 포지션
      W3. 시간청산 조건 충족인데 HOLD 중인 포지션
      W4. KIS 실제 잔고 ≠ 내부 포지션 (수량 불일치)
      W5. SELL_FAIL 재시도 큐 항목이 3분 이상 미처리

    W1~W3 감지 시: ERROR 로그 출력 + 즉시 강제 매도 실행
    W4 감지 시   : ERROR 로그 출력 + 내부 포지션 자동 제거 (KIS 기준 우선)
    W5 감지 시   : CRITICAL 로그 출력 + 알림
    """
    if _api is None or _strategy_mgr is None:
        return

    from screener.transaction_cost import net_profit_pct_from_cost
    from strategies.pyramid_strategy import (
        STOP_LOSS_PCT, TRAILING_STOP_PCT, TRAILING_ACTIVATE_PCT,
        PROFIT_FULL_PCT, PROFIT_SUPER_PCT,
        TIME_EXIT_20_MIN, TIME_EXIT_20_PCT,
        TIME_EXIT_40_MIN, TIME_EXIT_40_PCT,
        price_for_net_pct_from_cost,
    )

    import time as _t
    pyramid_obj = _strategy_mgr.pyramid
    positions   = pyramid_obj.positions
    now         = datetime.now()
    kr_tradeable = session_info().get("tradeable", False)

    if not positions:
        return

    # ── KIS 실제 잔고 조회 (캐시 120초 이내면 재사용) ────────────
    try:
        import time as _t2
        if _loop_balance.get("holdings") is not None and (_t2.time() - _loop_balance_ts) < 120:
            holdings = _loop_balance.get("holdings", [])
        else:
            bal = _api.get_balance()
            holdings = bal.get("holdings", [])
        kis_qty = {h["code"]: int(h.get("qty", 0)) for h in holdings if h.get("code")}
    except Exception as e:
        _log(f"⚠️ [Watchdog] KIS 잔고 조회 실패: {e}", "warning")
        return

    # ══════════════
    # W4: 포지션 불일치 (내부 있음 + KIS qty=0)
    # ══════════════
    removed_by_watchdog = []
    for code, pos in list(positions.items()):
        kis_q = kis_qty.get(code, 0)
        int_q = pos.total_qty
        if kis_q == 0 and int_q > 0:
            _log(
                f"🚨 [Watchdog W4] 포지션 불일치 감지! "
                f"{pos.name}({code}) 내부={int_q}주 KIS=0주 "
                f"→ 내부 포지션 강제 제거 (수동청산 추정)",
                "error"
            )
            pyramid_obj.positions.pop(code, None)
            removed_by_watchdog.append(code)
        elif kis_q > 0 and int_q == 0:
            _log(
                f"⚠️ [Watchdog W4] 미등록 KIS 보유 감지: "
                f"{code} KIS={kis_q}주 내부=0주 → _sync_positions_from_balance() 호출",
                "warning"
            )
            # 자동 복구는 _sync_positions_from_balance에 위임
            try:
                _sync_positions_from_balance()
            except Exception:
                pass

    if removed_by_watchdog:
        pyramid_obj._save()

    # ══════════════════════════════════════════════════
    # W1/W2/W3: 규칙 위반 포지션 (익절/손절/시간청산 미실행)
    # ══════════════════════════════════════════════════
    for code, pos in list(positions.items()):
        # KIS에 실제로 없으면 스킵 (W4에서 이미 처리)
        if kis_qty.get(code, 0) == 0:
            continue

        avg_px     = float(pos.avg_price)
        high_px    = float(pos.highest_price)
        qty        = pos.total_qty
        if avg_px <= 0 or qty <= 0:
            continue

        # 현재가 조회
        try:
            cur_data = _api.get_current_price(code)
            cur_px   = float(cur_data.get("price", 0))
        except Exception:
            cur_px = 0
        if cur_px <= 0:
            continue

        # 보유시간
        try:
            created_at  = datetime.fromisoformat(pos.created_at)
            elapsed_min = (now - created_at).total_seconds() / 60
        except Exception:
            elapsed_min = 0

        net_pct    = net_profit_pct_from_cost(avg_px, cur_px)
        act_price  = price_for_net_pct_from_cost(avg_px, TRAILING_ACTIVATE_PCT)
        trail_pct  = (cur_px - high_px) / high_px * 100 if high_px > 0 else 0

        violation  = None
        viol_type  = None

        # W1: 익절 미실행
        if net_pct >= PROFIT_SUPER_PCT:
            violation = (f"익절조건충족({net_pct:+.2f}%≥+{PROFIT_SUPER_PCT}%) 인데 HOLD 중 "
                         f"→ 즉시 전량 매도 실행")
            viol_type = "W1_PROFIT"
        elif net_pct >= PROFIT_FULL_PCT:
            violation = (f"익절조건충족({net_pct:+.2f}%≥+{PROFIT_FULL_PCT}%) 인데 HOLD 중 "
                         f"→ 즉시 전량 매도 실행")
            viol_type = "W1_PROFIT"

        # W2: 손절 미실행
        elif net_pct <= STOP_LOSS_PCT:
            violation = (f"손절조건충족({net_pct:+.2f}%≤{STOP_LOSS_PCT}%) 인데 HOLD 중 "
                         f"→ 즉시 전량 매도 실행")
            viol_type = "W2_STOPLOSS"

        # W2b: 트레일링 미실행
        elif high_px >= act_price and trail_pct <= TRAILING_STOP_PCT:
            violation = (f"트레일링조건충족(고점대비{trail_pct:+.2f}%≤{TRAILING_STOP_PCT}%) "
                         f"인데 HOLD 중 → 즉시 전량 매도 실행")
            viol_type = "W2_TRAILING"

        # W3: 시간청산 미실행
        elif elapsed_min >= TIME_EXIT_40_MIN and net_pct < TIME_EXIT_40_PCT:
            violation = (f"시간청산조건충족({elapsed_min:.0f}분≥{TIME_EXIT_40_MIN}분, "
                         f"실질{net_pct:+.2f}%<+{TIME_EXIT_40_PCT}%) 인데 HOLD 중 "
                         f"→ 즉시 전량 매도 실행")
            viol_type = "W3_TIME"
        elif elapsed_min >= TIME_EXIT_20_MIN and net_pct < TIME_EXIT_20_PCT:
            violation = (f"시간청산조건충족({elapsed_min:.0f}분≥{TIME_EXIT_20_MIN}분, "
                         f"실질{net_pct:+.2f}%<+{TIME_EXIT_20_PCT}%) 인데 HOLD 중 "
                         f"→ 즉시 전량 매도 실행")
            viol_type = "W3_TIME"

        if violation:
            _log(
                f"🚨 [Watchdog {viol_type}] 규칙 위반 감지! "
                f"{pos.name}({code}) {violation}",
                "error"
            )
            if kr_tradeable:
                try:
                    result = _api.sell(code, qty, 0)   # 시장가 즉시 매도
                    if result.get("rt_cd") == "0":
                        pyramid_obj.positions.pop(code, None)
                        pyramid_obj._save()
                        _log(
                            f"✅ [Watchdog 강제매도 완료] {pos.name}({code}) "
                            f"{qty}주 | 실질{net_pct:+.2f}%",
                            "sell"
                        )
                    else:
                        msg1 = result.get("msg1", "?")
                        _log(
                            f"❌ [Watchdog 강제매도 실패] {pos.name}({code}): {msg1} "
                            f"→ SELL 재시도 큐 등록",
                            "error"
                        )
                        _sell_retry_q.append({
                            "code": code, "name": pos.name, "qty": qty,
                            "attempt": 1,
                            "first_fail_ts": _t.time(),
                            "reason": f"Watchdog {viol_type}: {violation[:60]}",
                        })
                except Exception as e:
                    _log(
                        f"❌ [Watchdog 강제매도 예외] {pos.name}({code}): {e} "
                        f"→ SELL 재시도 큐 등록",
                        "error"
                    )
                    _sell_retry_q.append({
                        "code": code, "name": pos.name, "qty": qty,
                        "attempt": 1,
                        "first_fail_ts": _t.time(),
                        "reason": f"Watchdog {viol_type} 예외: {str(e)[:60]}",
                    })
            else:
                _log(
                    f"⏸️ [Watchdog {viol_type}] 국내 휴장 중 → "
                    f"장 재개 후 자동 실행 예정",
                    "warning"
                )

    # ══════════════
    # W5: SELL 재시도 큐 장기 미처리
    # ══════════════
    import time as _t3
    for item in _sell_retry_q:
        age_sec = _t3.time() - item["first_fail_ts"]
        if age_sec > 180:   # 3분 이상 미처리
            _log(
                f"🚨 [Watchdog W5] SELL 재시도 큐 {int(age_sec)}초 미처리! "
                f"{item['name']}({item['code']}) {item['qty']}주 "
                f"attempt={item['attempt']}/3 — 수동 확인 필요",
                "error"
            )


def _reschedule(interval_sec: int):
    """스케줄러 주기를 동적으로 변경"""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.reschedule_job("trading_loop",
                                  trigger=IntervalTrigger(seconds=interval_sec))


def _session_watcher():
    """세션 변경 감지 → 스케줄 주기 자동 조정 (30초마다 체크)"""
    if not _bot_running:
        return
    sess = session_info()
    _reschedule(sess["check_sec"])
    socketio.emit("session_update", {
        "session":     sess["session"],
        "icon":        sess["icon"],
        "order_label": sess["order_label"],
        "check_sec":   sess["check_sec"],
        "tradeable":   sess["tradeable"],
        "time_kst":    sess["time_kst"],
    })


def _get_lab() -> "StrategyLabEngine":
    """
    StrategyLabEngine 인스턴스 반환 (지연 초기화).
    처음 호출 시 DB 및 전략 포트폴리오를 초기화한다.
    """
    global _lab_engine
    if _lab_engine is None:
        from strategy_lab.lab_engine import StrategyLabEngine
        _lab_engine = StrategyLabEngine()
        logger.info("[App] StrategyLabEngine 초기화 완료")
    return _lab_engine


def _apply_us_screen_to_watchlist(top_buy: list) -> dict:
    """
    미국 스크리닝 BUY 후보 → _us_watch_list 자동 반영 (v3)
    ─────────────────────────────────────────────────────────
    v3 변경:
      1. us_watchlist_manager.cleanup_watchlist 통합 호출
         - 48h 성과 없는 종목 자동 제거
         - 당일 익절 종목 신규 진입 금지
         - 7일 손절 페널티 종목 후순위
      2. 보유 포지션 종목은 절대 제거 안 함
      3. [US DAILY SCREENER] 형식 로그 출력
    반환: {"added": [...], "removed_stale": [...], "kept_held": [...],
           "blocked_today_profit": [...]}
    """
    global _us_watch_list

    MAX_US_WATCH = 20

    if not top_buy:
        return {"added": [], "removed_stale": [], "kept_held": [], "blocked_today_profit": []}

    # 현재 포지션 보유 심볼 (강제 유지 대상)
    held_syms = set()
    if _us_strategy and _us_strategy.positions:
        held_syms = {sym for sym, p in _us_strategy.positions.items()
                     if (p.get("qty") or p.get("current_level", 0)) > 0}

    # ★ v3: watchlist_manager 통합 정리
    try:
        from screener.us_watchlist_manager import cleanup_watchlist, log_daily_screener_result
        from screener.us_market_screener import US_STOCKS

        # screener의 US_STOCKS에서 analyzed_count 추정
        analyzed_count = len(US_STOCKS)

        cleanup = cleanup_watchlist(
            current_watchlist=_us_watch_list,
            held_syms=held_syms,
            new_candidates=top_buy,
            max_watch=MAX_US_WATCH,
        )
        _us_watch_list = cleanup["final_watchlist"]

        # [US DAILY SCREENER] 로그 출력
        # 강세 섹터는 screener 결과에서 가져오기
        try:
            from screener.us_market_screener import load_us_result
            sr = load_us_result()
            strong_sectors = sr.get("summary", {}).get("strong_sectors", [])
        except Exception:
            strong_sectors = []

        log_daily_screener_result(
            analyzed_count=analyzed_count,
            strong_sectors=strong_sectors,
            new_candidates=top_buy,
            cleanup_result=cleanup,
            final_watchlist=_us_watch_list,
        )

        logger.info(
            f"🇺🇸 [관심종목 갱신v3] "
            f"+{cleanup['added']} | "
            f"48h제거:{cleanup['stale_syms'][:5]} | "
            f"익절차단:{cleanup['blocked_today_profit'][:5]} | "
            f"포지션유지:{cleanup['kept_held']} | "
            f"총{len(_us_watch_list)}개"
        )
        return cleanup

    except Exception as e:
        logger.warning(f"[관심종목 갱신] watchlist_manager 오류, 기본 로직 사용: {e}")

    # ── fallback: 기본 로직 ────────────────────────────────────
    candidate_syms = []
    for c in top_buy:
        sym  = c.get("symbol", "")
        name = c.get("name", sym)
        excd = c.get("excd", "NASD")
        if sym:
            candidate_syms.append({"symbol": sym, "name": name, "excd": excd,
                                    "sector": c.get("sector","GROWTH")})

    new_list = []
    added    = []
    for c in candidate_syms[:MAX_US_WATCH]:
        new_list.append(c)
        if not any(s["symbol"] == c["symbol"] for s in _us_watch_list):
            added.append(c["symbol"])

    for sym in held_syms:
        if not any(s["symbol"] == sym for s in new_list):
            old = next((s for s in _us_watch_list if s["symbol"] == sym), None)
            entry = old or {"symbol": sym, "name": sym, "excd": "NASD"}
            new_list.append(entry)

    old_syms = {s["symbol"] for s in _us_watch_list}
    new_syms = {s["symbol"] for s in new_list}
    removed  = list(old_syms - new_syms)
    _us_watch_list = new_list

    logger.info(f"🇺🇸 [관심종목 갱신] 추가:{added} | 제거:{removed} | 총{len(_us_watch_list)}개")
    return {"added": added, "removed_stale": removed, "kept_held": [], "blocked_today_profit": []}


def _us_market_job():
    """매일 00:00 KST — 미국 증시 야간 분석 v3 + 관심종목 자동 갱신 (섹터 강도 기반)"""
    try:
        from screener.us_market_screener import run_us_screening
        _log("🇺🇸 [US DAILY SCREENER] 야간 종합 분석 시작 (00:00 KST)...", "info")
        result  = run_us_screening()
        s       = result.get("summary", {})
        regime  = s.get("regime", "?")
        sp500   = s.get("sp500_pct", 0)
        vix     = s.get("vix", 0)
        top_buy = result.get("top_buy", [])
        analyzed = s.get("analyzed", 0)
        strong_sectors = s.get("strong_sectors", [])
        sector_dist    = s.get("sector_dist", {})

        _log(
            f"✅ [US DAILY SCREENER] 야간 완료 — 국면:{regime} | "
            f"S&P500:{sp500:+.2f}% | VIX:{vix:.1f} | "
            f"분석:{analyzed}종목 | 최종후보:{len(top_buy)}개",
            "info"
        )
        _log(
            f"[US DAILY SCREENER] 강세섹터={strong_sectors} | "
            f"섹터분포={sector_dist}",
            "info"
        )

        # ★ BEAR 국면이 아닐 때만 watchlist 자동 교체
        wl_diff = {}
        if regime != "BEAR" and top_buy:
            wl_diff = _apply_us_screen_to_watchlist(top_buy)
        elif regime == "BEAR":
            _log("🐻 BEAR 국면 — 미국 관심종목 교체 보류 (기존 유지)", "info")

        socketio.emit("us_screen_done", {
            "summary":        s,
            "korea_impact":   result.get("korea_impact", {}),
            "top_buy":        top_buy[:20],
            "wl_diff":        wl_diff,
            "watch_list":     _us_watch_list,
            "strong_sectors": strong_sectors,
            "sector_strength":result.get("sector_strength", {}),
        })
    except Exception as e:
        _log(f"❌ [US DAILY SCREENER] 야간 분석 오류: {e}", "error")


def _us_intraday_job():
    """매일 ET 10:30 (장 개시 1시간 후) — 5분봉 기반 실시간 모멘텀 재스크리닝 v3"""
    try:
        from screener.us_market_screener import run_us_intraday_screening
        _log("🇺🇸 [US DAILY SCREENER] ET 10:30 장중 실시간 스크리닝 시작...", "info")
        result   = run_us_intraday_screening()
        top_buy  = result.get("top_buy", [])
        analyzed = result.get("analyzed", 0)
        buy_raw  = result.get("buy_raw", 0)
        buy_final= result.get("buy_final", 0)
        sector_d = result.get("sector_dist", {})
        blocked  = result.get("today_profit_blocked", [])

        _log(
            f"✅ [US DAILY SCREENER] 장중 완료 | "
            f"분석={analyzed}종목 | BUY후보={buy_raw} → 최종={buy_final} | "
            f"당일익절차단={blocked[:5]}",
            "info"
        )

        if top_buy:
            wl_diff = _apply_us_screen_to_watchlist(top_buy)
            added   = wl_diff.get("added", [])
            if added:
                _log(
                    f"🇺🇸 [장중 관심종목 갱신] 신규추가:{added} | 총{len(_us_watch_list)}개",
                    "info"
                )

        socketio.emit("us_intraday_screen_done", {
            "top_buy":        top_buy[:20],
            "sector_dist":    sector_d,
            "watch_list":     _us_watch_list,
            "today_blocked":  blocked,
        })
    except Exception as e:
        _log(f"❌ [장중 스크리닝] 오류: {e}", "error")


def _weekly_lab_ranking_job():
    """APScheduler 에서 호출되는 주간 랭킹 계산 작업 (매주 월요일 09:00)"""
    try:
        from strategy_lab.lab_db import save_ranking_snapshot, save_ai_recs_from_ranking
        _log("📊 [자동] 전략 실험실 주간 랭킹 계산 시작...", "info")
        lab = _get_lab()
        ranking = lab.calc_ranking()
        regime  = lab.market_regime
        save_ranking_snapshot(ranking, regime)
        save_ai_recs_from_ranking(ranking, regime)
        top = ranking[0] if ranking else {}
        _log(
            f"✅ [자동] 랭킹 완료 — "
            f"1위: {top.get('name','?')} ({top.get('score',0):.1f}점) "
            f"| 국면: {regime}",
            "info",
        )
        socketio.emit("lab_ranking_done", {
            "ranking": ranking[:5],
            "regime":  regime,
        })
    except Exception as e:
        _log(f"❌ [자동] 랭킹 오류: {e}", "error")


def _apply_screen_result(result: dict):
    """
    스크리닝 결과(result)를 _watch_list에 반영하고 대시보드에 실시간 push.
    _daily_screen_job과 api_screener_run 양쪽에서 공유.
    """
    global _watch_list
    regime  = result.get("regime", "LATERAL")
    cnt     = result["summary"]["buy_candidate"]
    etf_cnt = result["summary"].get("etf_buy_candidate", 0)

    existing_codes = {s["code"] for s in _watch_list}
    added_stocks = []
    added_etfs   = []

    # ── ① 주식 매수후보 → 관심종목 추가 ────────────────────
    for c in result.get("candidates_10", [])[:10]:
        code = c.get("code", "")
        name = c.get("name", code)
        if code and code not in existing_codes:
            _watch_list.append({"code": code, "name": name, "asset_type": "STOCK_KOSPI"})
            existing_codes.add(code)
            added_stocks.append(name)

    # ── ② ETF 매수후보 → 관심종목 추가 (국면별 자동 반영) ──
    for e in result.get("etf_candidates", []):
        code      = e.get("code", "")
        name      = e.get("name", code)
        atype     = e.get("asset_type", "ETF_GENERAL")
        ai_score  = e.get("ai_score", 0)
        if not code:
            continue
        if atype == "ETF_LEVERAGE" and regime != "BULL":
            _log(f"⛔ ETF 제외({name}) — 레버리지는 BULL 국면에서만 허용 (현재:{regime})", "info")
            continue
        if atype == "ETF_LEVERAGE" and ai_score < 80:
            _log(f"⛔ ETF 제외({name}) — 레버리지 AI점수미달({ai_score:.0f}<80)", "info")
            continue
        if code not in existing_codes:
            _watch_list.append({"code": code, "name": name, "asset_type": atype})
            existing_codes.add(code)
            added_etfs.append(f"{name}[{atype}]")

    # ── ③ BEAR 국면: 인버스 ETF 상위 2개 자동 추가 ─────────
    if regime == "BEAR":
        inv_etfs = [e for e in result.get("etf_by_type", {}).get("ETF_INVERSE", [])
                    if e.get("grade") in ("BUY_CANDIDATE", "WATCH_HIGH")][:2]
        for e in inv_etfs:
            code  = e.get("code", "")
            name  = e.get("name", code)
            atype = "ETF_INVERSE"
            if code and code not in existing_codes:
                _watch_list.append({"code": code, "name": name, "asset_type": atype})
                existing_codes.add(code)
                added_etfs.append(f"{name}[인버스]")
                _log(f"🔴 [BEAR 국면] 인버스ETF 자동추가: {name}({code})", "info")

    # ── ④ 국면 변경 시 부적합 ETF 제거 ─────────────────────
    removed = []
    new_watch = []
    for s in _watch_list:
        atype = s.get("asset_type", "")
        code  = s.get("code", "")
        if atype == "ETF_LEVERAGE" and regime != "BULL" and code not in [c.get("code") for c in result.get("candidates_10", [])]:
            removed.append(s["name"])
            continue
        if atype == "ETF_INVERSE" and regime == "BULL":
            removed.append(s["name"])
            continue
        new_watch.append(s)
    if removed:
        _watch_list = new_watch
        _log(f"🔄 [국면변경:{regime}] 관심종목 제거: {', '.join(removed)}", "info")

    # ── ⑤ 주식 관심종목 최소 30개 보완 (초공격 단타: 종목 많을수록 매매 기회↑) ──
    stock_watch_cnt = sum(1 for s in _watch_list if not s.get("asset_type","").startswith("ETF"))
    TARGET_STOCK_WATCH = 30
    if stock_watch_cnt < TARGET_STOCK_WATCH:
        need = TARGET_STOCK_WATCH - stock_watch_cnt
        existing_codes = {s["code"] for s in _watch_list}
        focus_list = sorted(result.get("focus_30", []), key=lambda x: x.get("ai_score", 0), reverse=True)
        filled = []
        for c in focus_list:
            if len(filled) >= need:
                break
            code = c.get("code", "")
            name = c.get("name", code)
            if code and code not in existing_codes:
                atype = c.get("asset_type", "STOCK_KOSPI")
                if not atype.startswith("ETF"):
                    _watch_list.append({"code": code, "name": name, "asset_type": atype, "auto_fill": True})
                    existing_codes.add(code)
                    filled.append(name)
        if filled:
            _log(f"🔄 [자동보완] focus_30에서 {len(filled)}개 추가: {', '.join(filled)}", "info")

        # ★ fallback: DEFAULT_KR_WATCHLIST 30개 전체 사용
        FALLBACK_STOCKS = DEFAULT_KR_WATCHLIST
        existing_codes2  = {s["code"] for s in _watch_list}
        stock_watch_cnt2 = sum(1 for s in _watch_list if not s.get("asset_type","").startswith("ETF"))
        if stock_watch_cnt2 < TARGET_STOCK_WATCH:
            need2 = TARGET_STOCK_WATCH - stock_watch_cnt2
            fb_added = []
            for fb in FALLBACK_STOCKS:
                if len(fb_added) >= need2:
                    break
                if fb["code"] not in existing_codes2:
                    _watch_list.append({**fb, "auto_fill": True, "fallback": True})
                    existing_codes2.add(fb["code"])
                    fb_added.append(fb["name"])
            if fb_added:
                _log(f"🏦 [기본종목] 단타종목 자동추가: {', '.join(fb_added)}", "info")

    total_watch = len(_watch_list)
    stock_final = sum(1 for s in _watch_list if not s.get("asset_type","").startswith("ETF"))
    etf_final   = total_watch - stock_final

    if added_stocks:
        _log(f"📈 주식 관심종목 추가 — {', '.join(added_stocks)} ({len(added_stocks)}개)", "info")
    if added_etfs:
        _log(f"📦 ETF 관심종목 추가 — {', '.join(added_etfs)} ({len(added_etfs)}개)", "info")
    if not added_stocks and not added_etfs:
        _log("📋 관심종목 변동 없음 (이미 포함된 종목)", "info")
    _log(f"📋 [관심종목 현황] 주식:{stock_final}개 / ETF:{etf_final}개 / 합계:{total_watch}개", "info")

    # 대시보드 실시간 반영
    socketio.emit("watchlist_updated", {"watch_list": _watch_list, "regime": regime})


def _daily_screen_job():
    """APScheduler 에서 호출되는 일일 스크리닝 작업"""
    global _last_screen
    try:
        _log("📊 [자동] 일일 종목 스크리닝 시작 (16:05)...", "info")
        sc = _get_screener()
        result = sc.run()
        _last_screen = result
        regime  = result.get("regime", "LATERAL")
        cnt     = result["summary"]["buy_candidate"]
        etf_cnt = result["summary"].get("etf_buy_candidate", 0)
        _log(f"✅ [자동] 스크리닝 완료 — 국면:{regime} | 주식후보 {cnt}개 | ETF후보 {etf_cnt}개", "info")

        # 관심종목 반영 + 대시보드 push (공통 함수)
        _apply_screen_result(result)

        socketio.emit("screen_done", {
            "summary":        result["summary"],
            "candidates":     result["candidates_10"][:10],
            "focus":          result["focus_30"][:30],
            "etf_candidates": result.get("etf_candidates", []),
            "regime":         regime,
        })
    except Exception as e:
        _log(f"❌ [자동] 스크리닝 오류: {e}", "error")


# ── Flask 라우트 ──────────────────────────────────────────
@app.route("/")
def index():
    # ★ .env에 API 키가 있으면 어느 PC/브라우저에서 접속해도 바로 대시보드
    # (세션 쿠키 불필요 — 서버에 키가 있으면 무조건 통과)
    if Config.KIS_APP_KEY and Config.KIS_APP_SECRET and Config.KIS_ACCOUNT_NO:
        session["configured"] = True   # 호환성 유지
        if _api is None:
            _init_api()
        return render_template("dashboard.html",
                               watch_list=_watch_list)
    # API 키가 없을 때만 setup 화면
    return redirect(url_for("setup"))


@app.route("/demo")
def demo():
    """KIS API 키 없이 대시보드 UI 확인용"""
    session["configured"] = True
    return render_template("dashboard.html",
                           watch_list=_watch_list)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        with open(env_path, "w") as f:
            # KIS API (실전 전용 — KIS_IS_REAL 항상 true)
            f.write(f"KIS_APP_KEY={request.form['app_key']}\n")
            f.write(f"KIS_APP_SECRET={request.form['app_secret']}\n")
            f.write(f"KIS_ACCOUNT_NO={request.form['account_no']}\n")
            f.write("KIS_IS_REAL=true\n")
            # 텔레그램
            f.write(f"TELEGRAM_BOT_TOKEN={request.form.get('tg_token','')}\n")
            f.write(f"TELEGRAM_CHAT_ID={request.form.get('tg_chat_id','')}\n")
            # 투자금
            f.write(f"MAX_INVESTMENT_PER_STOCK={request.form.get('max_per_stock',5000000)}\n")
            f.write(f"MAX_TOTAL_INVESTMENT={request.form.get('max_total',5000000)}\n")
            # 손절/트레일링
            f.write(f"STOP_LOSS_PERCENT={request.form.get('stop_loss',10.0)}\n")
            f.write(f"USE_TAKE_PROFIT={request.form.get('use_take_profit','false')}\n")
            f.write(f"TAKE_PROFIT_PERCENT={request.form.get('take_profit',15.0)}\n")
            f.write(f"TRAILING_STOP_PCT={request.form.get('trailing_stop',15.0)}\n")
            f.write(f"TRAILING_ACTIVATE_PCT={request.form.get('trailing_activate',5.0)}\n")
            # 추가매수 (피라미딩)
            f.write(f"ADD_BUY_PROFIT={request.form.get('add_buy_profit','false')}\n")
            f.write(f"ADD_BUY_LOSS={request.form.get('add_buy_loss','false')}\n")
            f.write(f"PYRAMID_STEP_1={request.form.get('pyramid_1',10.0)}\n")
            f.write(f"PYRAMID_STEP_2={request.form.get('pyramid_2',20.0)}\n")
            f.write(f"PYRAMID_STEP_3={request.form.get('pyramid_3',35.0)}\n")
            # 실험 전략 (Strategy Lab)
            f.write(f"USE_LAB={request.form.get('use_lab','false')}\n")
            f.write(f"LAB_TRAILING_LIST={request.form.get('lab_trailing','10,12,15,18,20,25')}\n")
            f.write(f"LAB_STOPLOSS_LIST={request.form.get('lab_stoploss','5,7,10,12')}\n")
            f.write(f"LAB_PYRAMID_LIST={request.form.get('lab_pyramid','10-20-35,15-30-50,20-40-60')}\n")
            # Flask
            f.write(f"FLASK_SECRET_KEY={Config.FLASK_SECRET_KEY}\n")
        from importlib import reload
        import config as cfg_mod
        reload(cfg_mod)
        session["configured"] = True
        _init_api()
        return redirect(url_for("index"))
    return render_template("setup.html")


# ── API 엔드포인트 ────────────────────────────────────────
@app.route("/api/status")
def api_status():
    sess     = session_info()
    compound = _strategy_mgr.pyramid.compound_pool if _strategy_mgr else 0

    # ★ positions = 봇이 직접 관리하는 피라미딩 포지션 (로컬 파일 기반)
    #   실제 KIS 계좌 잔고는 /api/balance 에서 가져옵니다
    bot_positions = _strategy_mgr.positions if _strategy_mgr else {}

    # 시장 국면 + ETF 배분 현황
    regime = _detect_current_regime()
    from screener.asset_universe import (
        REGIME_TARGET_WEIGHTS, get_asset_type_label,
        ASSET_ETF_GENERAL, ASSET_ETF_LEVERAGE, ASSET_ETF_INVERSE,
        ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ,
    )
    target_weights = REGIME_TARGET_WEIGHTS.get(regime, {})
    etf_watch = [s for s in _watch_list if s.get("asset_type","").startswith("ETF")]
    stock_watch = [s for s in _watch_list if not s.get("asset_type","").startswith("ETF")]

    # 해외주식 세션 정보
    us_sess = us_session_info()

    return jsonify({
        "bot_running":   _bot_running,
        "api_ready":     _api is not None,
        "watch_list":    _watch_list,
        "positions":     bot_positions,
        "session":       sess,
        "compound_pool": compound,
        "regime":        regime,
        "target_weights": {k: round(v*100, 1) for k, v in target_weights.items()},
        "etf_watch_count":   len(etf_watch),
        "stock_watch_count": len(stock_watch),
        "etf_watch":         etf_watch,
        "stop_loss_pct":     Config.STOP_LOSS_PERCENT,
        # ★ 해외주식 정보
        "us_session":        us_sess,
        "us_watch_list":     _us_watch_list,
        "us_positions":      _us_strategy.positions if _us_strategy else {},
        "us_tradeable":      us_sess["tradeable"],
    })

@app.route("/api/session")
def api_session():
    return jsonify(session_info())


@app.route("/api/build")
def api_build():
    """운영 인스턴스 빌드 정보 — 헤더 배지용"""
    return jsonify({
        "commit":  _BUILD_COMMIT,
        "pid":     _BUILD_PID,
        "started": _BUILD_START,
        "build_time": _BUILD_START,
    })


@app.route("/api/reentry")
def api_reentry():
    """재진입 차단 중인 종목 목록 조회 (국내장 + 미국장 공통)"""
    try:
        from strategies.reentry_guard import ReentryGuard
        guard = ReentryGuard()
        guard.purge_expired()
        blocked_list = guard.get_blocked_list()
        return jsonify({
            "count":   len(blocked_list),
            "blocked": blocked_list,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── ★ 일일 손익 관리 (DailyPnLGuard) API ──────────────────

@app.route("/api/pnl/status")
def api_pnl_status():
    """
    국내장 + 미국장 DailyPnLGuard 현재 상태 조회.
    보유 포지션 평가손익을 계산하여 log_status(unrealized_pnl=...)로 전달.

    국내장: pyramid.positions → 현재가 합산으로 평가손익 계산
    미국장: pos_mgr.positions + _rt_cache → USD→KRW 환산
    ★ 평가손익은 참고용만 표시 — LOSS_LIMIT 판정에는 절대 미사용
    """
    result = {}

    # ── 국내장 ────────────────────────────────────────────
    if _strategy_mgr:
        g = _strategy_mgr.pnl_guard

        # 보유 포지션 평가손익 계산 (가능한 경우만)
        kr_unrealized = 0.0
        try:
            if _api is not None:
                for code, pos in _strategy_mgr.pyramid.positions.items():
                    if pos.avg_price <= 0 or pos.total_qty <= 0:
                        continue
                    try:
                        cur_data = _api.get_current_price(code)
                        cp = float(cur_data.get("price", 0) or 0)
                        if cp > 0:
                            kr_unrealized += (cp - pos.avg_price) * pos.total_qty
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[PnL API] 국내장 평가손익 계산 실패 (무시): {e}")

        # 로그에 실현/평가 구분하여 즉시 출력
        g.log_status(unrealized_pnl=kr_unrealized if kr_unrealized != 0.0 else None)

        d = g.status_dict()
        d["unrealized_pnl"] = round(kr_unrealized, 0)
        result["kr"] = d
    else:
        result["kr"] = {"error": "국내장 전략 매니저 미초기화"}

    # ── 미국장 ────────────────────────────────────────────
    if _us_strategy:
        g = _us_strategy.pnl_guard

        # 보유 포지션 평가손익 계산 (rt_cache 우선, 없으면 0)
        us_unrealized = 0.0
        try:
            from strategies.us_strategy_manager import FX_RATE_APPROX
            for sym, pos in _us_strategy.pos_mgr.positions.items():
                if pos.avg_price <= 0 or pos.qty <= 0:
                    continue
                rt = _us_strategy._rt_cache.get(sym, {})
                cp = float(rt.get("cur_price", 0) or 0)
                if cp > 0:
                    us_unrealized += (cp - pos.avg_price) * pos.qty * FX_RATE_APPROX
        except Exception as e:
            logger.debug(f"[PnL API] 미국장 평가손익 계산 실패 (무시): {e}")

        # 로그에 실현/평가 구분하여 즉시 출력
        g.log_status(unrealized_pnl=us_unrealized if us_unrealized != 0.0 else None)

        d = g.status_dict()
        d["unrealized_pnl"] = round(us_unrealized, 0)
        result["us"] = d
    else:
        result["us"] = {"error": "미국장 전략 매니저 미초기화"}

    return jsonify(result)


@app.route("/api/pnl/inject", methods=["POST"])
def api_pnl_inject():
    """
    서버 재시작 후 이전 손익을 수동 주입.

    Body (JSON):
        market     : "kr" | "us"           (필수)
        realized_pnl: float  (KRW)         (필수)
        peak_pnl   : float   (KRW, 선택)

    예시:
        POST /api/pnl/inject
        {"market":"kr","realized_pnl":-55066}
    """
    data = request.get_json(force=True, silent=True) or {}
    market       = data.get("market", "kr").lower()
    realized_pnl = data.get("realized_pnl")
    peak_pnl     = data.get("peak_pnl", None)

    if realized_pnl is None:
        return jsonify({"error": "realized_pnl 필드 필수"}), 400

    if market == "kr":
        if not _strategy_mgr:
            return jsonify({"error": "국내장 전략 매니저 미초기화"}), 503
        _strategy_mgr.pnl_guard.inject(realized_pnl, peak_pnl)
        return jsonify({
            "ok": True,
            "market": "kr",
            "status": _strategy_mgr.pnl_guard.status_dict(),
        })
    elif market == "us":
        if not _us_strategy:
            return jsonify({"error": "미국장 전략 매니저 미초기화"}), 503
        _us_strategy.pnl_guard.inject(realized_pnl, peak_pnl)
        return jsonify({
            "ok": True,
            "market": "us",
            "status": _us_strategy.pnl_guard.status_dict(),
        })
    else:
        return jsonify({"error": f"market='{market}' 은 kr 또는 us 만 허용"}), 400

@app.route("/api/balance")
def api_balance():
    """실제 KIS 증권사 계좌 잔고 — holdings 가 진짜 보유 종목"""
    if _api is None:
        return jsonify({"error": "API 미초기화", "holdings": [], "total_eval": 0, "cash": 0})
    return jsonify(_api.get_balance())


@app.route("/api/asset-summary")
def api_asset_summary():
    """
    ★ 총평가금액 / 누적복리수익률 / 실현·평가손익 / 일일손익 통합 조회

    총평가금액 = 보유종목평가 + 예수금 + 미체결정산금 = MTS 총자산(tot_evlu_amt)
    누적복리수익률 = (현재총자산 / 초기기준자산 - 1) × 100
    실현손익 = 당일 매도 완료 기준 (_today_pnl_kr + _today_pnl_us 환산)
    평가손익 = 보유종목 기준 (KIS evlu_pfls_smtl_amt, 절대 혼합 금지)
    """
    if _api is None:
        return jsonify({"error": "API 미초기화"}), 503

    try:
        bal = _api.get_balance()
    except Exception as e:
        logger.error(f"[AssetSummary] 잔고 조회 실패: {e}")
        return jsonify({"error": f"잔고 조회 실패: {e}"}), 503

    # ── 1. 총평가금액 분해 ────────────────────────────────────
    # KIS tot_evlu_amt = 유가증권평가 + 예수금 + 미체결정산금
    # → MTS '총자산'과 동일한 값
    total_eval    = bal.get("total_eval", 0)      # tot_evlu_amt  (MTS 총자산)
    scts_eval     = bal.get("scts_eval", 0)        # 보유종목 평가금액
    cash_amt      = bal.get("cash", 0)             # 예수금(주문가능현금)
    prev_settle   = bal.get("prev_sell_settle", 0) # 전일매도정산금
    next_settle   = bal.get("next_day_settle", 0)  # 익일정산금
    purchase_amt  = bal.get("purchase_amt", 0)     # 매입금액 합계

    # 직접 합산 검증 (디버그용)
    # ★ KIS total_eval(tot_evlu_amt) = scts_eval + cash_amt + 정산금들
    # ★ cash_amt(dnca_tot_amt)가 음수인 경우 = 미수금(매수대금 정산 대기) 상태
    #    → 총자산 계산은 반드시 tot_evlu_amt(total_eval) 직접 사용 (분해합과 일치하지 않을 수 있음)
    calc_total    = scts_eval + cash_amt + prev_settle + next_settle
    diff_vs_kis   = total_eval - calc_total   # KIS값과 합산값의 차이 (참고용)

    # ── 2. 손익 분리 ─────────────────────────────────────────
    # 실현손익: 당일 매도 완료 기준 (절대 평가손익과 혼합 금지)
    _reset_daily_if_needed()
    us_fx = 1350.0  # USD→KRW 근사 환율 (실시간 환율 미조회시 기본값)
    try:
        fx_data = _api.get_exchange_rate() if hasattr(_api, "get_exchange_rate") else None
        if fx_data:
            us_fx = float(fx_data)
    except Exception:
        pass

    realized_pnl_kr  = round(_today_pnl_kr)
    realized_pnl_us  = round(_today_pnl_us * us_fx)
    realized_pnl_tot = realized_pnl_kr + realized_pnl_us

    # 평가손익: KIS API 직접값 (보유종목 미실현 손익)
    unrealized_pnl = bal.get("total_profit", 0)   # evlu_pfls_smtl_amt

    # ── 3. 누적복리수익률 ─────────────────────────────────────
    # 공식: (현재총자산 / 초기기준자산 - 1) × 100
    initial_asset   = Config.INITIAL_ASSET
    cumulative_ret  = 0.0
    total_return    = 0.0
    if initial_asset > 0 and total_eval > 0:
        cumulative_ret = round((total_eval / initial_asset - 1) * 100, 2)
        total_return   = cumulative_ret  # 단순 총수익률 (비복리 동일)

    # ── 4. 진단 로그 ─────────────────────────────────────────
    logger.info(
        f"[자산요약] "
        f"초기자산={initial_asset:,.0f}원 | "
        f"보유평가(scts)={scts_eval:,.0f}원 | "
        f"예수금(dnca)={cash_amt:,.0f}원{'(미수금)' if cash_amt < 0 else ''} | "
        f"전일정산={prev_settle:,.0f}원 | "
        f"익일정산={next_settle:,.0f}원 | "
        f"합산검증={calc_total:,.0f}원(차이{diff_vs_kis:+,}원) | "
        f"총자산(KIS tot_evlu)={total_eval:,.0f}원 | "
        f"누적복리={cumulative_ret:+.2f}%"
    )
    logger.info(
        f"[손익분리] "
        f"실현(당일)={realized_pnl_tot:+,.0f}원 "
        f"[국내{realized_pnl_kr:+,.0f}+미국{realized_pnl_us:+,.0f}] | "
        f"평가손익={unrealized_pnl:+,.0f}원 | "
        f"일일손익합계={realized_pnl_tot + unrealized_pnl:+,.0f}원"
    )

    return jsonify({
        # 총자산 분해
        "total_eval":       total_eval,     # MTS 총자산 = tot_evlu_amt (★기준)
        "scts_eval":        scts_eval,      # 보유종목 평가금액 (scts_evlu_amt)
        "cash":             cash_amt,       # 예수금 (dnca_tot_amt, 음수=미수금)
        "prev_settle":      prev_settle,    # 전일매도정산금
        "next_settle":      next_settle,    # 익일정산금
        "purchase_amt":     purchase_amt,   # 매입금액 합계
        "calc_total":       calc_total,     # 직접합산 검증값 (참고용)
        "diff_vs_kis":      diff_vs_kis,    # KIS값-합산값 차이 (0에 가까울수록 정상)

        # 손익 분리 (절대 혼합 금지)
        "realized_pnl":     realized_pnl_tot,    # 실현손익 (당일, KRW 환산)
        "realized_pnl_kr":  realized_pnl_kr,     # 국내 실현손익
        "realized_pnl_us":  realized_pnl_us,     # 미국 실현손익 (KRW환산)
        "unrealized_pnl":   unrealized_pnl,      # 평가손익 (보유종목)
        "daily_pnl":        realized_pnl_tot,    # 일일손익 = 실현손익 (당일)

        # 수익률
        "initial_asset":    int(initial_asset),  # 초기기준자산
        "cumulative_ret":   cumulative_ret,      # 누적복리수익률 %
        "total_return":     total_return,        # 총수익률 %

        # 보유종목 수
        "hold_cnt":         len(bal.get("holdings", [])),
    })


@app.route("/api/holdings")
def api_holdings():
    """KIS 계좌 실제 보유 종목만 반환 (대시보드 보유현황용)"""
    if _api is None:
        return jsonify({"holdings": [], "error": "API 미초기화"})
    try:
        bal = _api.get_balance()
        return jsonify({"holdings": bal.get("holdings", [])})
    except Exception as e:
        logger.error(f"holdings 조회 실패: {e}")
        return jsonify({"holdings": [], "error": str(e)})

@app.route("/api/signals")
def api_signals():
    return jsonify(_last_signals)

# ── ★ 해외주식 전용 API ───────────────────────────────────────

@app.route("/api/us/signals")
def api_us_signals():
    """해외주식 최신 신호"""
    return jsonify(_us_last_signals)

@app.route("/api/us/positions")
def api_us_positions():
    """해외주식 봇 포지션"""
    if _us_strategy is None:
        return jsonify({})
    return jsonify(_us_strategy.positions)

@app.route("/api/us/balance")
def api_us_balance():
    """해외주식 KIS 계좌 잔고"""
    if _api is None:
        return jsonify({"holdings": [], "error": "API 미초기화"})
    try:
        bal = _api.get_us_balance()
        rate = _api.get_usd_exchange_rate()
        bal["exchange_rate"] = rate
        # 원화 환산 추가
        for h in bal.get("holdings", []):
            h["cur_price_krw"] = round(h.get("cur_price", 0) * rate)
            h["avg_price_krw"] = round(h.get("avg_price", 0) * rate)
            h["pnl_krw"]       = round(h.get("pnl_amt",  0) * rate)
        return jsonify(bal)
    except Exception as e:
        return jsonify({"holdings": [], "error": str(e)})

@app.route("/api/us/session")
def api_us_session():
    """미국장 세션 정보"""
    return jsonify(us_session_info())

@app.route("/api/us/watchlist", methods=["GET"])
def api_us_watchlist_get():
    """해외주식 관심종목 조회"""
    return jsonify({"watch_list": _us_watch_list})

@app.route("/api/us/watchlist/add", methods=["POST"])
def api_us_watchlist_add():
    """해외주식 관심종목 추가"""
    global _us_watch_list
    data   = request.get_json() or {}
    symbol = data.get("symbol", "").strip().upper()
    name   = data.get("name",   symbol).strip()
    excd   = data.get("excd",   "NASD").strip().upper()
    if not symbol:
        return jsonify({"ok": False, "error": "symbol 필요"})
    if any(s["symbol"] == symbol for s in _us_watch_list):
        return jsonify({"ok": False, "error": "이미 추가된 종목"})
    _us_watch_list.append({"symbol": symbol, "name": name, "excd": excd})
    _log(f"🇺🇸 해외 관심종목 추가: {name}({symbol})", "info")
    socketio.emit("us_watchlist_updated", {"watch_list": _us_watch_list})
    return jsonify({"ok": True, "watch_list": _us_watch_list})

@app.route("/api/us/watchlist/remove", methods=["POST"])
def api_us_watchlist_remove():
    """해외주식 관심종목 삭제"""
    global _us_watch_list
    data   = request.get_json() or {}
    symbol = data.get("symbol", "").strip().upper()
    before = len(_us_watch_list)
    _us_watch_list = [s for s in _us_watch_list if s["symbol"] != symbol]
    removed = before - len(_us_watch_list)
    if removed:
        _log(f"🗑️ 해외 관심종목 삭제: {symbol}", "info")
        socketio.emit("us_watchlist_updated", {"watch_list": _us_watch_list})
    return jsonify({"ok": bool(removed), "watch_list": _us_watch_list})

@app.route("/api/us/chart/<symbol>")
def api_us_chart(symbol):
    """해외주식 캔들 데이터"""
    if _api is None:
        return jsonify({"error": "API 미초기화"})
    excd = request.args.get("excd", "NASD")
    try:
        candles = _api.get_us_ohlcv(symbol.upper(), excd, count=60)
        return jsonify({"symbol": symbol, "excd": excd, "candles": candles})
    except Exception as e:
        return jsonify({"error": str(e)})

# ── 미국 증시 분석 API ─────────────────────────────────────
@app.route("/api/us/status")
def api_us_status():
    """저장된 미국 분석 결과 반환 (캐시)"""
    from screener.us_market_screener import load_us_result
    return jsonify(load_us_result())

@app.route("/api/us/run", methods=["POST"])
def api_us_run():
    """미국 분석 즉시 실행 + 관심종목 자동 갱신 (수동 트리거)"""
    try:
        from screener.us_market_screener import run_us_screening
        _log("🇺🇸 [수동] 미국 증시 분석 시작...", "info")
        result  = run_us_screening()
        s       = result.get("summary", {})
        regime  = s.get("regime", "?")
        top_buy = result.get("top_buy", [])
        _log(f"✅ [수동] 미국 분석 완료 — 국면:{regime} | S&P500:{s.get('sp500_pct',0):+.2f}%", "info")

        # ★ 관심종목 자동 갱신 (BEAR 제외)
        wl_diff = {}
        if regime != "BEAR" and top_buy:
            wl_diff = _apply_us_screen_to_watchlist(top_buy)
            added   = wl_diff.get("added", [])
            removed = wl_diff.get("removed", [])
            if added or removed:
                _log(f"🇺🇸 [수동 갱신] +{added} / -{removed} → 총{len(_us_watch_list)}개", "info")
        elif regime == "BEAR":
            _log("🐻 BEAR 국면 — 수동 분석 시 관심종목 교체 보류", "info")

        socketio.emit("us_screen_done", {
            "summary":      s,
            "korea_impact": result.get("korea_impact", {}),
            "top_buy":      top_buy[:20],
            "wl_diff":      wl_diff,
            "watch_list":   _us_watch_list,
        })
        return jsonify({
            "ok":         True,
            "summary":    s,
            "wl_diff":    wl_diff,
            "watch_list": _us_watch_list,
        })
    except Exception as e:
        logger.error(f"미국 분석 실행 오류: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/us/preflight")
def api_us_preflight():
    """
    미국주식 실거래 사전점검 (5단계)
    서버 내부 캐시 토큰 재사용 → EGW00133 토큰 제한 완전 회피
    """
    import time
    steps = []

    def step(num, name, ok, detail="", data=None):
        icon = "✅" if ok else "❌"
        steps.append({
            "step":   num,
            "name":   name,
            "ok":     ok,
            "icon":   icon,
            "detail": detail,
            "data":   data or {},
        })
        logger.info(f"[PREFLIGHT STEP{num}] {icon} {name}: {detail}")

    if _api is None:
        return jsonify({"ok": False, "error": "API 미초기화", "steps": steps})

    # ── STEP 1: 토큰 유효성 확인 (서버 내부 캐시 사용, 신규 발급 없음) ──
    try:
        tok = getattr(_api, "_access_token", None)
        tok_exp = getattr(_api, "_token_expire", None)
        has_token = bool(tok and len(tok) > 10)
        step(1, "KIS 토큰 확인 (캐시)", has_token,
             f"토큰={'있음' if has_token else '없음'} | 만료={tok_exp}",
             {"cached": has_token, "expires": str(tok_exp)})
    except Exception as e:
        step(1, "KIS 토큰 확인 (캐시)", False, str(e))

    # ── STEP 2: 해외주식 잔고 조회 (TTTS3012R) ──
    try:
        bal = _api.get_us_balance()
        holdings = bal.get("holdings", [])
        total_eval = bal.get("total_eval", 0)
        cash_usd   = bal.get("cash_usd", 0)
        ok2 = True  # API 자체 성공 여부는 예외 미발생으로 판단
        step(2, "해외주식 잔고 (TTTS3012R)", ok2,
             f"보유종목={len(holdings)}개 | 평가=${total_eval:.2f} | 예수금=${cash_usd:.2f}",
             {"holdings": holdings, "total_eval": total_eval, "cash_usd": cash_usd})
    except Exception as e:
        step(2, "해외주식 잔고 (TTTS3012R)", False, str(e))

    # ── STEP 3: USD 예수금 잔액 (CTRP6504R) ──
    try:
        usd_cash = _api.get_us_cash_balance()
        ok3 = isinstance(usd_cash, dict)
        step(3, "USD 예수금 (CTRP6504R)", ok3,
             f"USD=${usd_cash.get('cash_usd', 0):.2f} | KRW={usd_cash.get('cash_krw', 0):,.0f}원",
             usd_cash)
    except AttributeError:
        # get_us_cash_balance 미구현 → TTTS3007R 에서 USD 추출
        try:
            amt = _api.get_us_available_amounts()
            ok3 = isinstance(amt, dict)
            usd_avail = amt.get("usd_avail", 0)
            step(3, "USD 주문가능금액 (TTTS3007R)", ok3,
                 f"USD가능=${usd_avail:.2f}",
                 amt)
        except Exception as e2:
            step(3, "USD 예수금/주문가능금액", False, str(e2))
    except Exception as e:
        step(3, "USD 예수금 (CTRP6504R)", False, str(e))

    # ── STEP 4: AAPL 주문가능금액 (TTTS3007R, ITEM_CD=AAPL) ──
    try:
        amt = _api.get_us_available_amounts()
        usd_avail = amt.get("usd_avail", 0)
        krw_avail = amt.get("krw_avail", 0)
        ok4 = isinstance(amt, dict)
        step(4, "주문가능금액 AAPL (TTTS3007R)", ok4,
             f"USD가능=${usd_avail:.2f} | KRW가능={krw_avail:,.0f}원",
             amt)
    except Exception as e:
        step(4, "주문가능금액 AAPL (TTTS3007R)", False, str(e))

    # ── STEP 5: AAPL 현재가 조회 (HHDFS00000300 or yfinance) ──
    try:
        price_info = _api.get_us_current_price("AAPL")
        price = price_info.get("price", 0)
        source = price_info.get("source", "KIS")
        ok5 = price > 0
        step(5, f"AAPL 현재가 ({source})", ok5,
             f"AAPL=${price:.2f} ({source})",
             {"price": price, "source": source})
    except Exception as e:
        step(5, "AAPL 현재가", False, str(e))

    # ── STEP 6: 보유종목 기준 원화주문가능금액 실측 ──────────
    # 서버 내부 get_us_available_amounts() 재사용 — 별도 토큰 발급 없음
    try:
        holdings_list = steps[1]["data"].get("holdings", [])
        test_sym  = holdings_list[0]["symbol"] if holdings_list else "BBAI"
        test_excd = holdings_list[0].get("excd", "NYSE") if holdings_list else "NYSE"

        avail6 = _api.get_us_available_amounts(symbol=test_sym, excd=test_excd)
        frcr6  = avail6.get("usd", 0.0)    # frcr_ord_psbl_amt1
        ovrs6  = avail6.get("krw", 0.0)    # ovrs_ord_psbl_amt
        raw6   = avail6.get("raw", {})
        mqty6  = raw6.get("ovrs_max_ord_psbl_qty", "?")
        exrt6  = raw6.get("exrt", "?")
        ok6    = frcr6 > 0 or ovrs6 > 0

        step(6, f"원화주문가능금액 실측 ({test_sym} 기준)", ok6,
             f"frcr(USD포함)=${frcr6:.2f} | ovrs(원화결제한도)={ovrs6:,.0f}원 | 최대{mqty6}주 | 환율={exrt6}",
             {"frcr_usd": frcr6, "ovrs_krw": ovrs6, "max_qty": mqty6,
              "exrt": exrt6, "symbol": test_sym, "raw": raw6})
    except Exception as e:
        step(6, "원화주문가능금액 실측", False, str(e))

    all_ok = all(s["ok"] for s in steps)
    return jsonify({
        "ok":      all_ok,
        "steps":   steps,
        "summary": f"{'✅ 전체 통과' if all_ok else '⚠️ 일부 실패'} — {sum(1 for s in steps if s['ok'])}/{len(steps)} 성공",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S KST"),
    })


@app.route("/api/trades")
def api_trades():
    if _strategy_mgr is None:
        return jsonify([])
    return jsonify(_strategy_mgr.get_trade_history(50))

@app.route("/api/chart/<code>")
def api_chart(code):
    if _api is None:
        return jsonify([])
    candles = _api.get_ohlcv(code, period="D", count=120)
    return jsonify(candles)


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    from backtesting.backtest_engine import BacktestEngine
    data        = request.json or {}
    code        = data.get("code", "005930")
    strategy    = data.get("strategy", "combined")
    stop_loss   = float(data.get("stop_loss", 3.0))
    take_profit = float(data.get("take_profit", 5.0))
    capital     = float(data.get("capital", 10_000_000))
    if _api is None:
        return jsonify({"error": "API 미초기화"})
    candles = _api.get_ohlcv(code, period="D", count=500)
    if not candles:
        return jsonify({"error": "데이터 없음"})
    engine = BacktestEngine(capital)
    return jsonify(engine.run(candles, strategy, stop_loss, take_profit))

@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    global _bot_running, _scheduler
    if _api is None and not _init_api():
        return jsonify({"ok": False, "msg": "API 초기화 실패"})
    _bot_running = True
    _save_bot_state(True)   # ★ 상태 파일에 저장

    if _scheduler is None or not _scheduler.running:
        _scheduler = BackgroundScheduler(timezone="Asia/Seoul")
        sess = session_info()
        _scheduler.add_job(_trading_loop,  "interval", seconds=sess["check_sec"],
                           id="trading_loop",         replace_existing=True)
        _scheduler.add_job(_session_watcher, "interval", seconds=30,
                           id="session_watcher",      replace_existing=True)
        _scheduler.add_job(_daily_screen_job, "cron", hour=16, minute=5,
                           timezone="Asia/Seoul",     id="daily_screener",     replace_existing=True)
        _scheduler.add_job(_weekly_lab_ranking_job, "cron", day_of_week="mon", hour=9, minute=0,
                           timezone="Asia/Seoul",     id="weekly_lab_ranking", replace_existing=True)
        _scheduler.add_job(_us_market_job, "cron", hour=0, minute=0,
                           timezone="Asia/Seoul",     id="us_market_screener", replace_existing=True)
        _scheduler.add_job(_us_intraday_job, "cron", hour=10, minute=30,
                           timezone="America/New_York",  # ET 10:30 (장 개시 1h 후)
                           id="us_intraday_screener", replace_existing=True)
        # ★ 15:20:30 미체결 매수주문 자동 취소 (장후시간외 체결 방지)
        _scheduler.add_job(_cancel_pending_buy_orders, "cron",
                           hour=15, minute=20, second=30,
                           timezone="Asia/Seoul",
                           id="cancel_pending_buys", replace_existing=True)
        _scheduler.start()
        # ★ 봇 시작 시 토큰 안정화 후 1회 신호 점검 (15초 딜레이)
        def _delayed_loop_start():
            time.sleep(15)
            _trading_loop()
        threading.Thread(target=_delayed_loop_start, daemon=True).start()

    _kr_sess = session_info()
    _us_sess = us_session_info()
    _log(
        f"🚀 자동매매 봇 시작! "
        f"KR={_kr_sess['session']} | "
        f"US={_us_sess['session']} (ET {_us_sess['time_et']}) | "
        f"KST {_kr_sess['time_kst']}",
        "info"
    )
    notifier.notify_system("자동매매 봇 시작")
    return jsonify({"ok": True})


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    global _bot_running
    _bot_running = False
    _save_bot_state(False)   # ★ 상태 파일에 저장
    _log("⏹ 자동매매 봇 정지", "info")
    notifier.notify_system("자동매매 봇 정지")
    return jsonify({"ok": True})


@app.route("/api/bot/run_now", methods=["POST"])
def bot_run_now():
    """
    트레이딩 루프 즉시 강제 실행 (손절·트레일링·매매 신호 즉시 점검).
    세션(장중/휴장) 무관하게 _orphan_loss_cut() 포함 전체 루프 실행.
    """
    if not _bot_running or _strategy_mgr is None:
        return jsonify({"ok": False, "msg": "봇이 실행 중이지 않습니다"})
    def _run():
        try:
            _orphan_loss_cut()   # 손절·트레일링 즉시 점검
            _trading_loop()      # 전체 루프
        except Exception as e:
            _log(f"❌ 강제 실행 오류: {e}", "error")
    threading.Thread(target=_run, daemon=True).start()
    _log("⚡ 트레이딩 루프 즉시 강제 실행", "info")
    return jsonify({"ok": True, "msg": "실행 시작됨"})


@app.route("/api/analyze/<code>")
def api_analyze(code):
    if _strategy_mgr is None:
        return jsonify({"error": "API 미초기화"})
    name   = next((s["name"] for s in _watch_list if s["code"] == code), code)
    result = _strategy_mgr.run({"code": code, "name": name})
    _last_signals[code] = result
    return jsonify(result)


@app.route("/api/watchlist/add", methods=["POST"])
def watchlist_add():
    data = request.json or {}
    code = data.get("code", "").strip()
    name = data.get("name", code)
    if not code:
        return jsonify({"ok": False, "msg": "코드 없음"})
    if any(s["code"] == code for s in _watch_list):
        return jsonify({"ok": False, "msg": "이미 추가됨"})
    _watch_list.append({"code": code, "name": name})
    return jsonify({"ok": True, "watch_list": _watch_list})


@app.route("/api/watchlist/remove", methods=["POST"])
def watchlist_remove():
    global _watch_list
    code = (request.json or {}).get("code", "")
    _watch_list = [s for s in _watch_list if s["code"] != code]
    return jsonify({"ok": True, "watch_list": _watch_list})


@app.route("/api/logs")
def api_logs():
    return jsonify(_status_log[-50:])


# ══════════════════════════════════════════════════════════════
# ★ 통합 로그 API — V1 메모리 로그 + V2 파일 로그 시간순 병합
#   화면에서 /api/logs/merged 를 호출하면 V1+V2 최신 200줄 병합 반환
# ══════════════════════════════════════════════════════════════
_V2_LOG_FILE = os.path.join(os.path.dirname(__file__),
                             "..", "stock_trader_v2", "logs", "v2_live_out.log")

# V2 로그에서 화면에 표시할 레벨/키워드 필터
_V2_SHOW_PREFIXES = (
    "[V2 LIVE START]", "[V2 LOCK]", "[ADAPTIVE]",
    "★ V2", "[V2 PREPARE]", "[V2 READY]",
    "[US PREPARE]", "[US READY]", "[US OPEN]", "[US SCAN]",
    "[US STATUS]", "[US PROFIT LOCK]", "[US CLOSE]", "[US BUY 판정]",
    "[KR PREPARE]", "[KR READY]", "[KR OPEN]", "[KR SCAN]",
    "[KR STATUS]", "[KR PROFIT LOCK]", "[KR CLOSE]", "[KR BUY 판정]",
    "[ADAPTIVE WEIGHT APPLIED]", "[ADAPTIVE SIGNAL FILTER]",
    "BUY_US", "SELL_US", "BUY_KR", "SELL_KR",
    "[US 루프", "[KR 루프",
    # ★ V2 신규 표준 태그 (v2_rebuild 반영)
    "[ASSET_SYNC]",       # KR+US 합산 총자산
    "[BUY_OK]",           # 매수 체결 확인
    "[SELL_OK]",          # 매도 체결 확인
    "[TRADE_REVIEW]",     # 거래 요약 (진입~청산)
    "[EXIT_DECISION]",    # US 통합 청산 판정
    # ★ V2 진입 분석 태그 (이번 세션 추가)
    "[ENTRY_SUMMARY]",    # KR 루프당 진입 요약
    "[US_ENTRY_SUMMARY]", # US 루프당 진입 요약
    "[ENTRY_EXCLUDE]",    # 수량0/자금부족 사전 제외
    "[US SCREENER→WATCH]",# 스크리너→감시종목 반영
    # ★ CandidatePool 로그 태그
    "[ENTRY_FLOW]",       # 1분 간격 진입 흐름 요약
    "[NO_ENTRY_REASON]",  # 거래 없을 때 이유 표시
    # ★ 재진입 정책 로그 태그
    "[REENTRY_POLICY]",   # 매도 후 쿨다운 정책 적용 내역
    "[WATCHLIST_REFRESH]",# 재진입차단 종목 제외 + 감시목록 갱신
    "💰", "✅ KIS", "❌", "⛔", "🔔", "🎯", "🏁",
)
# 노이즈 필터 (이 키워드가 포함된 줄은 화면에서 숨김)
_V2_HIDE_PREFIXES = (
    "OPSQ2001", "EGW00133", "[RateLimit]",
    "[GET] 요청 실패", "[GET] rt_cd=2",
    "[US 재진입 체크]", "토큰 캐시 재사용",
    "[v2.KISBase]",
)


def _read_v2_log_lines(n: int = 400) -> list:
    """
    V2 로그 파일에서 최근 n줄을 읽고,
    화면 표시 대상 로그만 파싱하여 반환.
    반환: [{"time": "HH:MM:SS", "msg": str, "level": str, "src": "V2"}]
    """
    import re
    result = []
    try:
        if not os.path.exists(_V2_LOG_FILE):
            return result
        with open(_V2_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        # 최근 n줄만
        lines = lines[-n:]
        # 파싱: "2026-06-12 14:00:32: [2026-06-12 14:00:32] [v2.XXX] [LEVEL] 메시지"
        pat = re.compile(
            r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):\s+"
            r"\[\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})\]\s+"
            r"\[v2\.\w+\]\s+\[(INFO|WARNING|ERROR|DEBUG)\]\s+(.*)"
        )
        for line in lines:
            m = pat.match(line.strip())
            if not m:
                continue
            dt_str, time_str, level_str, msg = (
                m.group(1), m.group(2), m.group(3), m.group(4)
            )
            # 노이즈 필터
            if any(h in msg for h in _V2_HIDE_PREFIXES):
                continue
            # 표시 필터
            if not any(p in msg for p in _V2_SHOW_PREFIXES):
                # WARNING/ERROR 는 무조건 표시
                if level_str not in ("WARNING", "ERROR"):
                    continue
            level_map = {"INFO": "info", "WARNING": "warning",
                         "ERROR": "error", "DEBUG": "debug"}
            result.append({
                "time":    time_str,
                "dt":      dt_str,          # 정렬용
                "msg":     f"[V2] {msg}",
                "level":   level_map.get(level_str, "info"),
                "src":     "V2",
            })
    except Exception:
        pass
    return result


@app.route("/api/logs/merged")
def api_logs_merged():
    """
    V1 메모리 로그 + V2 파일 로그를 시간순 병합하여 최신 200줄 반환.
    프론트엔드 대시보드에서 이 API를 폴링하면 V1+V2 통합 로그를 볼 수 있음.
    """
    from datetime import date as _date
    today = _date.today().strftime("%Y-%m-%d")

    # V1 로그: 메모리 _status_log (최근 150개)
    v1_entries = []
    for e in _status_log[-150:]:
        v1_entries.append({
            "time":  e.get("time", "00:00:00"),
            "dt":    f"{today} {e.get('time', '00:00:00')}",
            "msg":   e.get("msg", ""),
            "level": e.get("level", "info"),
            "src":   "V1",
        })

    # V2 로그: 파일에서 최근 400줄 파싱
    v2_entries = _read_v2_log_lines(400)

    # 병합 + 시간순 정렬
    merged = v1_entries + v2_entries
    merged.sort(key=lambda x: x.get("dt", ""))

    # 최신 200줄만
    merged = merged[-200:]

    # dt 필드 제거 (응답 정리)
    for e in merged:
        e.pop("dt", None)

    return jsonify(merged)


# ══════════════════════════════════════════════════════════════
# ★ V2 LIVE 상태 API — V2 엔진 상태를 대시보드에 노출
# ══════════════════════════════════════════════════════════════

@app.route("/api/v2/status")
def api_v2_status():
    """V2 LIVE 실행 상태를 반환. 대시보드 상단 패널용."""
    import subprocess as _sp
    v2_data_dir = os.path.join(os.path.dirname(__file__), "..", "stock_trader_v2", "data")
    lock_file   = _V2_LOCK_FILE

    # lock 파일 읽기
    v2_running = os.path.exists(lock_file)
    lock_info  = {}
    if v2_running:
        try:
            import json as _json
            with open(lock_file) as f:
                lock_info = _json.load(f)
        except Exception:
            pass

    # V2 PID 생사 확인
    v2_pid = lock_info.get("pid", 0)
    v2_pid_alive = False
    if v2_pid:
        try:
            os.kill(int(v2_pid), 0)
            v2_pid_alive = True
        except (ProcessLookupError, PermissionError):
            v2_pid_alive = False

    # V2 계좌 상태 파일 읽기
    acc_file = os.path.join(v2_data_dir, "v2_account_state.json")
    acc = {}
    try:
        import json as _json
        if os.path.exists(acc_file):
            with open(acc_file) as f:
                acc = _json.load(f)
    except Exception:
        pass

    # V2 PnL 파일 읽기
    def _read_pnl(market):
        try:
            import json as _json
            p = os.path.join(v2_data_dir, f"v2_pnl_{market.lower()}.json")
            if os.path.exists(p):
                with open(p) as f:
                    return _json.load(f)
        except Exception:
            pass
        return {}

    pnl_kr = _read_pnl("KR")
    pnl_us = _read_pnl("US")

    # V2 trade_log 당일 거래 횟수
    trade_count = 0
    try:
        import json as _json
        from datetime import date as _date
        tl = os.path.join(v2_data_dir, "v2_trade_log.json")
        if os.path.exists(tl):
            with open(tl) as f:
                logs = _json.load(f)
            today = _date.today().isoformat()
            trade_count = sum(
                1 for l in logs
                if str(l.get("timestamp","")).startswith(today)
            )
    except Exception:
        pass

    return jsonify({
        "v1_order_loop":   "STOPPED" if v2_running else "RUNNING",
        "v2_order_loop":   "RUNNING" if (v2_running and v2_pid_alive) else ("LOCK_EXISTS_PID_DEAD" if v2_running else "STOPPED"),
        "v2_live":         v2_running and v2_pid_alive,
        "v2_pid":          lock_info.get("pid", "-"),
        "v2_start_time":   lock_info.get("start_time", "-"),
        "v2_commit":       lock_info.get("commit", "-"),
        "mode":            "V2_LIVE" if (v2_running and v2_pid_alive) else ("V1_ACTIVE" if not v2_running else "V2_LOCK_STALE"),
        # 합산 총자산 (v2_account_state.json 기준)
        "total_asset":     acc.get("total_asset", 0),
        "orderable_cash":  acc.get("orderable_cash", 0),
        # ★ 세부 자산 항목 (KR+US 분리 표시용)
        "kr_cash":         acc.get("kr_cash", 0),
        "kr_eval":         acc.get("kr_eval", 0),
        "us_cash_krw":     acc.get("us_cash_krw", 0),
        "us_eval_krw":     acc.get("us_eval_krw", 0),
        "kr_orderable":    acc.get("kr_orderable", 0),
        "us_orderable_usd": acc.get("us_orderable_usd", 0),
        "last_sync":       acc.get("last_sync", "-"),
        "pnl_kr_today":    pnl_kr.get("realized_pnl", pnl_kr.get("today_pnl", 0)),
        "pnl_us_today":    pnl_us.get("realized_pnl", pnl_us.get("today_pnl", 0)),
        "trade_count_today": trade_count,
        "profit_lock_krw": 300_000,
    })


@app.route("/api/v2/daily-review")
def api_v2_daily_review():
    """
    DAILY_REVIEW 복기 히스토리 조회.
    - market: KR | US | ALL  (쿼리파라미터, 기본 ALL)
    - limit : 최근 N일치 (기본 30)
    반환: { history: [ { key, date, market, ...report } ], total: int }
    """
    import json as _json
    from flask import request as _req

    market = _req.args.get("market", "ALL").upper()
    try:
        limit  = int(_req.args.get("limit", 30))
    except (ValueError, TypeError):
        limit  = 30

    history_path = os.path.join(
        os.path.dirname(__file__), "..", "stock_trader_v2", "data",
        "daily_review_history.json"
    )

    if not os.path.exists(history_path):
        return jsonify({"history": [], "total": 0})

    try:
        with open(history_path, "r", encoding="utf-8") as f:
            raw = _json.load(f)
    except Exception:
        return jsonify({"history": [], "total": 0})

    # 필터링
    entries = []
    for key, val in raw.items():
        mkt = val.get("market", "")
        if market != "ALL" and mkt != market:
            continue
        entries.append({"key": key, **val})

    # 날짜 내림차순 정렬 → limit 개
    entries.sort(key=lambda x: x.get("date", ""), reverse=True)
    entries = entries[:limit]

    return jsonify({"history": entries, "total": len(entries)})


@app.route("/api/v2/daily-review/latest")
def api_v2_daily_review_latest():
    """
    가장 최근 KR / US 복기 보고서 각각 1건씩 반환.
    대시보드 요약 카드용.
    """
    import json as _json

    history_path = os.path.join(
        os.path.dirname(__file__), "..", "stock_trader_v2", "data",
        "daily_review_history.json"
    )

    if not os.path.exists(history_path):
        return jsonify({"KR": None, "US": None})

    try:
        with open(history_path, "r", encoding="utf-8") as f:
            raw = _json.load(f)
    except Exception:
        return jsonify({"KR": None, "US": None})

    latest = {"KR": None, "US": None}
    for mkt in ("KR", "US"):
        candidates = [
            {"key": k, **v}
            for k, v in raw.items()
            if v.get("market") == mkt
        ]
        if candidates:
            candidates.sort(key=lambda x: x.get("date", ""), reverse=True)
            latest[mkt] = candidates[0]

    return jsonify(latest)


# ══════════════════════════════════════════════════════════
# 스크리너 API 엔드포인트
# ══════════════════════════════════════════════════════════

_screener      = None      # DailyScreener 인스턴스
_last_screen   = {}        # 마지막 스크리닝 결과 캐시


def _get_screener():
    """
    DailyScreener 인스턴스 반환.
    실전 KIS API 전용 — KISDataFetcher(demo_mode=False) 고정.
    """
    global _screener
    from screener.daily_screener import DailyScreener
    from screener.kis_data_fetcher import KISDataFetcher

    if _screener is None:
        fetcher = KISDataFetcher(demo_mode=False)
        _screener = DailyScreener(fetcher=fetcher, demo_mode=False)
    return _screener


@app.route("/screener")
def screener_page():
    """스크리너 대시보드 페이지"""
    return render_template("screener.html",
                           watch_list=_watch_list)


@app.route("/api/screener/run", methods=["POST"])
def api_screener_run():
    """즉시 스크리닝 실행 (백그라운드) — 완료 후 관심종목 자동 반영"""
    global _last_screen
    def _run():
        global _last_screen
        try:
            _log("📊 종목 스크리닝 시작...", "info")
            sc = _get_screener()
            result = sc.run()
            _last_screen = result
            cnt     = result["summary"]["buy_candidate"]
            etf_cnt = result["summary"].get("etf_buy_candidate", 0)
            regime  = result.get("regime", "LATERAL")
            _log(f"✅ 스크리닝 완료 — 매수후보 {cnt}개 / ETF {etf_cnt}개", "info")

            # ★ 스크리닝 결과를 _watch_list 에 즉시 반영
            _apply_screen_result(result)

            socketio.emit("screen_done", {
                "summary":        result["summary"],
                "candidates":     result["candidates_10"][:10],
                "focus":          result["focus_30"][:30],
                "etf_candidates": result.get("etf_candidates", []),
                "regime":         regime,
            })
        except Exception as e:
            _log(f"❌ 스크리닝 오류: {e}", "error")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": "스크리닝 시작됨"})


@app.route("/api/screener/results")
def api_screener_results():
    """최신 스크리닝 결과 조회"""
    from screener.screener_db import get_latest_scores, get_buy_candidates, get_screen_summary
    grade   = request.args.get("grade", "")
    limit   = int(request.args.get("limit", 100))
    scores  = get_latest_scores(limit=limit, grade_filter=grade or None)
    cands   = get_buy_candidates()
    summary = get_screen_summary(days=1)
    return jsonify({
        "scores":     scores,
        "candidates": cands,
        "summary":    summary[0] if summary else {},
    })


@app.route("/api/screener/candidates")
def api_screener_candidates():
    """매수 후보 10개"""
    from screener.screener_db import get_buy_candidates
    return jsonify(get_buy_candidates())


@app.route("/api/screener/focus")
def api_screener_focus():
    """집중감시 30개"""
    from screener.screener_db import get_watchlist_db
    return jsonify(get_watchlist_db(focus_only=True))


@app.route("/api/screener/watchlist")
def api_screener_watchlist():
    """전체 감시 200개"""
    from screener.screener_db import get_watchlist_db
    return jsonify(get_watchlist_db(focus_only=False))


@app.route("/api/screener/analyze/<code>")
def api_screener_analyze(code):
    """단일 종목 즉시 분석"""
    name = request.args.get("name", code)
    sc   = _get_screener()
    result = sc.analyze_single(code, name)
    return jsonify(result)


@app.route("/api/screener/history/<code>")
def api_screener_history(code):
    """종목 점수 이력"""
    from screener.screener_db import get_score_history
    return jsonify(get_score_history(code, days=30))


@app.route("/api/screener/summary")
def api_screener_summary():
    """스크리닝 요약 (최근 7일)"""
    from screener.screener_db import get_screen_summary
    return jsonify(get_screen_summary(days=7))


# ══════════════════════════════════════════════════════════
# 전략 실험실 (Strategy Lab) API 엔드포인트
# ══════════════════════════════════════════════════════════

@app.route("/lab")
def lab_page():
    """전략 실험실 대시보드 페이지"""
    return render_template("lab.html",
                           watch_list=_watch_list)


@app.route("/api/lab/status")
def api_lab_status():
    """전략 실험실 전체 현황 (대시보드 카드)"""
    lab = _get_lab()
    return jsonify(lab.get_all_status())


@app.route("/api/lab/ranking")
def api_lab_ranking():
    """최신 전략 랭킹 (캐시 or 파일)"""
    lab = _get_lab()
    ranking = lab.get_ranking()
    return jsonify({
        "ranking": ranking,
        "regime":  lab.market_regime,
        "calc_date": lab._last_rank_date or "",
    })


@app.route("/api/lab/ranking/run", methods=["POST"])
def api_lab_ranking_run():
    """즉시 랭킹 재계산 (수동 트리거)"""
    def _run():
        try:
            from strategy_lab.lab_db import save_ranking_snapshot, save_ai_recs_from_ranking
            lab     = _get_lab()
            ranking = lab.calc_ranking()
            regime  = lab.market_regime
            save_ranking_snapshot(ranking, regime)
            save_ai_recs_from_ranking(ranking, regime)
            socketio.emit("lab_ranking_done", {
                "ranking": ranking[:5],
                "regime":  regime,
            })
        except Exception as e:
            logger.error(f"[Lab] 랭킹 계산 오류: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": "랭킹 계산 시작됨"})


@app.route("/api/lab/strategy/<sid>")
def api_lab_strategy(sid):
    """단일 전략 상세 (메트릭 + 포지션 + 거래내역)"""
    lab = _get_lab()
    detail = lab.get_strategy_detail(sid)
    if not detail:
        return jsonify({"error": f"전략 없음: {sid}"}), 404
    return jsonify(detail)


@app.route("/api/lab/regime")
def api_lab_regime():
    """시장 국면 분석"""
    lab = _get_lab()
    return jsonify(lab.get_regime_analysis())


@app.route("/api/lab/ai")
def api_lab_ai():
    """최신 AI 추천 (전략별)"""
    from strategy_lab.lab_db import get_latest_ai_recs
    return jsonify(get_latest_ai_recs(limit=20))


@app.route("/api/lab/tier/history")
def api_lab_tier_history():
    """계층 변경 이력"""
    from strategy_lab.lab_db import get_tier_history
    sid = request.args.get("strategy_id", None)
    return jsonify(get_tier_history(strategy_id=sid, limit=50))


@app.route("/api/lab/trades")
def api_lab_trades():
    """가상 거래 기록 조회"""
    from strategy_lab.lab_db import get_trades
    sid        = request.args.get("strategy_id", None)
    code       = request.args.get("code", None)
    action     = request.args.get("action", None)
    start_date = request.args.get("start_date", None)
    limit      = int(request.args.get("limit", 100))
    return jsonify(get_trades(
        strategy_id=sid, code=code, action=action,
        start_date=start_date, limit=limit,
    ))


@app.route("/api/lab/equity/<sid>")
def api_lab_equity(sid):
    """전략별 자산 곡선"""
    from strategy_lab.lab_db import get_equity_curve
    days = int(request.args.get("days", 120))
    return jsonify(get_equity_curve(sid, days=days))


@app.route("/api/lab/demo", methods=["POST"])
def api_lab_demo():
    """데모 시뮬레이션 실행 (실계좌 완전 무관)"""
    days = int((request.json or {}).get("days", 60))
    days = max(10, min(days, 365))   # 10~365일 범위 제한

    def _run():
        try:
            from strategy_lab.lab_db import save_ranking_snapshot, save_ai_recs_from_ranking
            lab    = _get_lab()
            result = lab.run_demo_simulation(days=days)
            save_ranking_snapshot(result["ranking"], result["regime"])
            save_ai_recs_from_ranking(result["ranking"], result["regime"])
            socketio.emit("lab_demo_done", result)
        except Exception as e:
            logger.error(f"[Lab] 데모 시뮬레이션 오류: {e}")
            socketio.emit("lab_demo_done", {"error": str(e)})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": f"{days}일 데모 시뮬레이션 시작됨"})


@app.route("/api/lab/db/stats")
def api_lab_db_stats():
    """Lab DB 통계 (모니터링)"""
    from strategy_lab.lab_db import get_db_stats
    return jsonify(get_db_stats())


@app.route("/api/lab/performance")
def api_lab_performance():
    """전략 성과 테이블 (DB 기반)"""
    from strategy_lab.lab_db import get_strategy_performance_table
    return jsonify(get_strategy_performance_table())


@app.route("/api/lab/regime/best")
def api_lab_regime_best():
    """시장 국면별 최고 전략"""
    from strategy_lab.lab_db import get_weekly_best_by_regime
    return jsonify(get_weekly_best_by_regime())


# ══════════════════════════════════════════════════════════
# 자산군 유니버스 API  /api/universe/*
# ══════════════════════════════════════════════════════════

@app.route("/api/universe/status")
def api_universe_status():
    """
    현재 자산군 배분 현황
    — 국면, 자산군별 타겟/실제 비중, 비중 한도 여유분 반환
    """
    try:
        from screener.asset_universe import (
            WEIGHT_LIMITS, REGIME_TARGET_WEIGHTS,
            REGIME_PRIORITY, ALL_ASSET_TYPES, get_asset_type_label,
        )
        from screener.daily_screener import _detect_regime
        sc      = _get_screener()
        fetcher = sc._get_fetcher()
        kospi   = fetcher.get_market_index("KOSPI")
        kosdaq  = fetcher.get_market_index("KOSDAQ")
        regime  = _detect_regime(kospi, kosdaq)

        target_wts = REGIME_TARGET_WEIGHTS.get(regime, {})
        cash_min   = WEIGHT_LIMITS.get("CASH", 0.10)
        lev_max    = WEIGHT_LIMITS.get("ETF_LEVERAGE", 0.20)
        inv_max    = WEIGHT_LIMITS.get("ETF_INVERSE",  0.40)

        return jsonify({
            "ok":     True,
            "regime": regime,
            "target_weights": {
                k: {"weight": round(v, 4), "label": get_asset_type_label(k)}
                for k, v in target_wts.items()
            },
            "regime_priority": REGIME_PRIORITY.get(regime, []),
            "weight_limits": {
                "leverage_max": lev_max,
                "inverse_max":  inv_max,
                "cash_min":     cash_min,
            },
            "market": {
                "kospi_price":    kospi.get("price", 0),
                "kospi_ret20":    kospi.get("return_20d", 0),
                "kosdaq_price":   kosdaq.get("price", 0),
                "kosdaq_ret20":   kosdaq.get("return_20d", 0),
            },
        })
    except Exception as e:
        logger.error(f"/api/universe/status 오류: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/universe/allocation")
def api_universe_allocation():
    """
    현재 국면 기준 배분 계획 (스크리닝 캐시 사용).
    캐시 없을 경우 간이 데모 데이터 반환.
    """
    try:
        from screener.asset_universe import REGIME_TARGET_WEIGHTS, REGIME_PRIORITY, get_asset_type_label
        from screener.daily_screener import _detect_regime
        from screener.asset_allocator import get_allocation_plan

        sc      = _get_screener()
        fetcher = sc._get_fetcher()
        kospi   = fetcher.get_market_index("KOSPI")
        kosdaq  = fetcher.get_market_index("KOSDAQ")
        regime  = _detect_regime(kospi, kosdaq)

        # 최근 스크리닝 결과 캐시에서 후보 가져오기 (없으면 빈 리스트)
        try:
            from screener.screener_db import get_watchlist_db
            candidates = get_watchlist_db(focus_only=True) or []
        except Exception:
            candidates = []

        # ETF 목록 추가 (데모 목록)
        try:
            etf_list = fetcher.get_etf_list()
        except Exception:
            etf_list = []

        all_screened = candidates + etf_list

        plan = get_allocation_plan(
            screened      = all_screened,
            regime        = regime,
            total_capital = 10_000_000,
            cash          = 10_000_000,
        )

        return jsonify({
            "ok":     True,
            "regime": regime,
            "target_weights": {
                k: round(v, 4)
                for k, v in plan.target_weights.items()
            },
            "buy_list": [
                {
                    "code":            bd.code,
                    "name":            bd.name,
                    "asset_type":      bd.asset_type,
                    "asset_type_kr":   get_asset_type_label(bd.asset_type),
                    "ai_score":        round(bd.ai_score, 1),
                    "rs_value":        round(bd.rs_value, 2),
                    "can_buy":         bd.can_buy,
                    "reason":          bd.reason,
                    "suggested_ratio": round(bd.suggested_ratio, 4),
                    "max_amount":      round(bd.max_amount, 0),
                    "priority_rank":   bd.priority_rank,
                }
                for bd in plan.buy_list
                if bd.can_buy
            ],
            "blocked_list": [
                {
                    "code":       bd.code,
                    "name":       bd.name,
                    "asset_type": bd.asset_type,
                    "reason":     bd.reason,
                }
                for bd in plan.buy_list
                if not bd.can_buy
            ],
            "warnings":         plan.warnings,
            "rebalance_needed": plan.rebalance_needed,
            "summary":          plan.summary,
        })
    except Exception as e:
        logger.error(f"/api/universe/allocation 오류: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/universe/plan", methods=["POST"])
def api_universe_plan():
    """
    POST JSON {screened: [...], regime: str, total_capital: float, cash: float}
    → 배분 계획 실행 후 결과 반환
    """
    try:
        from screener.asset_allocator import get_allocation_plan
        from screener.asset_universe import get_asset_type_label

        data          = request.get_json(force=True) or {}
        screened      = data.get("screened", [])
        regime        = data.get("regime", "LATERAL")
        total_capital = float(data.get("total_capital", 10_000_000))
        cash          = float(data.get("cash",          total_capital))
        positions     = data.get("positions", {})

        if not screened:
            return jsonify({"ok": False, "error": "screened 목록이 비어 있습니다"}), 400

        plan = get_allocation_plan(
            screened      = screened,
            regime        = regime,
            total_capital = total_capital,
            cash          = cash,
            positions     = positions,
        )
        return jsonify({
            "ok":     True,
            "regime": plan.regime,
            "target_weights":   plan.target_weights,
            "current_weights":  plan.current_weights,
            "rebalance_needed": plan.rebalance_needed,
            "warnings":         plan.warnings,
            "buyable_count":    sum(1 for d in plan.buy_list if d.can_buy),
            "blocked_count":    sum(1 for d in plan.buy_list if not d.can_buy),
            "buy_list": [
                {
                    "code":            bd.code,
                    "name":            bd.name,
                    "asset_type":      bd.asset_type,
                    "asset_type_kr":   get_asset_type_label(bd.asset_type),
                    "ai_score":        round(bd.ai_score, 1),
                    "rs_value":        round(bd.rs_value, 2),
                    "can_buy":         bd.can_buy,
                    "reason":          bd.reason,
                    "suggested_ratio": round(bd.suggested_ratio, 4),
                    "max_amount":      round(bd.max_amount, 0),
                }
                for bd in plan.buy_list
            ],
            "summary": plan.summary,
        })
    except Exception as e:
        logger.error(f"/api/universe/plan 오류: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/universe/etf/list")
def api_universe_etf_list():
    """
    투자 대상 ETF 유니버스 목록.
    자산군별(일반/레버리지/인버스) 분류 포함.
    """
    try:
        from screener.asset_universe import (
            get_demo_etf_list, classify_asset_type, get_asset_type_label,
            ETF_CODE_MAP,
        )
        sc      = _get_screener()
        fetcher = sc._get_fetcher()
        try:
            raw_list = fetcher.get_etf_list()
        except Exception:
            raw_list = get_demo_etf_list()

        result = []
        for e in raw_list:
            code  = e.get("code", "")
            name  = e.get("name", code)
            atype = e.get("asset_type") or classify_asset_type(code, name, "ETF")
            result.append({
                "code":         code,
                "name":         name,
                "asset_type":   atype,
                "asset_type_kr": get_asset_type_label(atype),
                "market_cap":   e.get("market_cap", 0),
                "daily_amount": e.get("daily_amount", 0),
            })

        # 자산군별 그룹화
        by_type: dict = {}
        for r in result:
            at = r["asset_type"]
            by_type.setdefault(at, []).append(r)

        return jsonify({
            "ok":       True,
            "total":    len(result),
            "by_type":  by_type,
            "etf_list": result,
        })
    except Exception as e:
        logger.error(f"/api/universe/etf/list 오류: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/universe/regime/weights")
def api_universe_regime_weights():
    """
    국면별 타겟 비중 + 비중 한도 테이블 반환.
    UI의 비중 게이지 / 도넛 차트에 사용.
    """
    try:
        from screener.asset_universe import (
            REGIME_TARGET_WEIGHTS, WEIGHT_LIMITS,
            REGIME_PRIORITY, ALL_ASSET_TYPES, get_asset_type_label,
        )
        from screener.daily_screener import _detect_regime
        sc      = _get_screener()
        fetcher = sc._get_fetcher()
        kospi   = fetcher.get_market_index("KOSPI")
        kosdaq  = fetcher.get_market_index("KOSDAQ")
        current_regime = _detect_regime(kospi, kosdaq)

        # 모든 국면의 타겟 비중을 한번에 반환
        all_regime_weights = {}
        for regime, wt_map in REGIME_TARGET_WEIGHTS.items():
            all_regime_weights[regime] = {
                k: {
                    "weight":   round(v, 4),
                    "label":    get_asset_type_label(k),
                    "priority": (REGIME_PRIORITY.get(regime, []).index(k) + 1
                                 if k in REGIME_PRIORITY.get(regime, [])
                                 else 99),
                }
                for k, v in wt_map.items()
            }

        return jsonify({
            "ok":             True,
            "current_regime": current_regime,
            "regime_weights": all_regime_weights,
            "regime_priority": {
                r: REGIME_PRIORITY.get(r, [])
                for r in ["BULL", "LATERAL", "BEAR"]
            },
            "weight_limits": {
                k: {"limit": v, "label": get_asset_type_label(k)}
                for k, v in WEIGHT_LIMITS.items()
            },
            "asset_labels": {
                at: get_asset_type_label(at) for at in ALL_ASSET_TYPES
            },
        })
    except Exception as e:
        logger.error(f"/api/universe/regime/weights 오류: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── SocketIO ─────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    emit("log_history", _status_log[-30:])
    emit("session_update", session_info())


@socketio.on("run_now")
def on_run_now():
    threading.Thread(target=_trading_loop, daemon=True).start()


# ── 진입점 ────────────────────────────────────────────────
@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "static"),
        "favicon.ico", mimetype="image/vnd.microsoft.icon"
    )


if __name__ == "__main__":
    print("=" * 60)
    print("  📈 주식 자동매매 시스템")
    print(f"  🌐 http://0.0.0.0:{Config.DASHBOARD_PORT}")
    print("=" * 60)

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path) and os.path.getsize(env_path) > 10:
        _init_api()

    # 전략 실험실 미리 초기화 (DB 스키마 생성 및 포트폴리오 로드)
    try:
        _get_lab()
    except Exception as _e:
        print(f"[App] 전략 실험실 초기화 실패 (무시): {_e}")

    # ★ 이전에 봇이 실행 중이었으면 자동 재개
    _auto_start_bot()

    socketio.run(app, host="0.0.0.0", port=Config.DASHBOARD_PORT,
                 debug=False, allow_unsafe_werkzeug=True)
