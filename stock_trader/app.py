"""
Flask 웹 대시보드 — 세션 인식 자동매매
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import json
import threading
from datetime import datetime

from flask import Flask, render_template, jsonify, request, redirect, url_for, session, send_from_directory
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import Config
from utils.logger import get_logger
from utils.notifier import TelegramNotifier
from utils.market_session import session_info, is_tradeable, SESSION_OFF

logger   = get_logger("Dashboard")
notifier = TelegramNotifier()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = Config.FLASK_SECRET_KEY
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── 글로벌 상태 ────────────────────────────────────────────
_api          = None
_strategy_mgr = None
_scheduler    = None
_bot_running  = False
_watch_list   = list(Config.WATCH_LIST)
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


# ── API 초기화 ────────────────────────────────────────────
def _init_api() -> bool:
    global _api, _strategy_mgr
    from api.kis_api import KISApi
    from strategies.strategy_manager import StrategyManager
    try:
        _api          = KISApi()
        _strategy_mgr = StrategyManager(_api)
        _log("✅ KIS API 초기화 완료")
        return True
    except Exception as e:
        _log(f"❌ API 초기화 실패: {e}", "error")
        return False


# ── 세션 인식 매매 루프 ───────────────────────────────────
def _trading_loop():
    if not _bot_running or _strategy_mgr is None:
        return

    sess = session_info()

    # 휴장이면 로그만 남기고 종료
    if not sess["tradeable"]:
        _log(f"😴 [{sess['time_kst']}] 휴장 — 대기 중", "info")
        return

    _log(
        f"{sess['icon']} [{sess['time_kst']}] {sess['session']} "
        f"({sess['order_label']}) 신호 점검...",
        "info"
    )

    for stock in list(_watch_list):
        try:
            result = _strategy_mgr.run(stock)
            _last_signals[stock["code"]] = result
            action = result.get("action", "HOLD")
            name   = stock["name"]

            if action == "BUY":
                _log(
                    f"🟢 매수 [{result.get('session','')}] "
                    f"{name} {result['price']:,}원 × {result['qty']}주",
                    "buy"
                )
                notifier.notify_buy(
                    name, stock["code"],
                    result["price"], result["qty"],
                    result.get("reason", "")
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
                    name, stock["code"],
                    result["price"], result["qty"],
                    profit, result.get("reason", "")
                )
            elif action == "HOLD":
                _log(
                    f"⏸ {name}  BUY={result.get('buy_score',0):.2f} "
                    f"SELL={result.get('sell_score',0):.2f} "
                    f"(기준 ≥{result.get('cutoff',0.55)})",
                    "info"
                )
        except Exception as e:
            _log(f"❌ {stock['name']} 오류: {e}", "error")

    # 잔고 실시간 업데이트
    try:
        balance = _api.get_balance()
        socketio.emit("balance_update", balance)
    except Exception:
        pass


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


def _daily_screen_job():
    """APScheduler 에서 호출되는 일일 스크리닝 작업"""
    global _last_screen
    try:
        _log("📊 [자동] 일일 종목 스크리닝 시작 (16:05)...", "info")
        sc = _get_screener()
        result = sc.run()
        _last_screen = result
        cnt = result["summary"]["buy_candidate"]
        _log(f"✅ [자동] 스크리닝 완료 — 매수후보 {cnt}개", "info")
        socketio.emit("screen_done", {
            "summary":    result["summary"],
            "candidates": result["candidates_10"][:10],
            "focus":      result["focus_30"][:30],
        })
    except Exception as e:
        _log(f"❌ [자동] 스크리닝 오류: {e}", "error")


# ── Flask 라우트 ──────────────────────────────────────────
@app.route("/")
def index():
    if not session.get("configured"):
        return redirect(url_for("setup"))
    return render_template("dashboard.html",
                           watch_list=_watch_list,
                           is_real=Config.KIS_IS_REAL)


@app.route("/demo")
def demo():
    """KIS API 키 없이 대시보드 UI 확인용 (데모 모드)"""
    session["configured"] = True
    return render_template("dashboard.html",
                           watch_list=_watch_list,
                           is_real=Config.KIS_IS_REAL)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        with open(env_path, "w") as f:
            f.write(f"KIS_APP_KEY={request.form['app_key']}\n")
            f.write(f"KIS_APP_SECRET={request.form['app_secret']}\n")
            f.write(f"KIS_ACCOUNT_NO={request.form['account_no']}\n")
            f.write(f"KIS_IS_REAL={request.form.get('is_real','false')}\n")
            f.write(f"TELEGRAM_BOT_TOKEN={request.form.get('tg_token','')}\n")
            f.write(f"TELEGRAM_CHAT_ID={request.form.get('tg_chat_id','')}\n")
            f.write(f"MAX_INVESTMENT_PER_STOCK={request.form.get('max_per_stock',1000000)}\n")
            f.write(f"MAX_TOTAL_INVESTMENT={request.form.get('max_total',5000000)}\n")
            f.write(f"STOP_LOSS_PERCENT={request.form.get('stop_loss',3.0)}\n")
            f.write(f"TAKE_PROFIT_PERCENT={request.form.get('take_profit',5.0)}\n")
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
    sess         = session_info()
    compound     = _strategy_mgr.pyramid.compound_pool if _strategy_mgr else 0
    return jsonify({
        "bot_running":   _bot_running,
        "api_ready":     _api is not None,
        "watch_list":    _watch_list,
        "is_real":       Config.KIS_IS_REAL,
        "positions":     _strategy_mgr.positions if _strategy_mgr else {},
        "session":       sess,
        "compound_pool": compound,
    })

@app.route("/api/session")
def api_session():
    return jsonify(session_info())

@app.route("/api/balance")
def api_balance():
    if _api is None:
        return jsonify({"error": "API 미초기화"})
    return jsonify(_api.get_balance())

@app.route("/api/signals")
def api_signals():
    return jsonify(_last_signals)

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

    if _scheduler is None or not _scheduler.running:
        _scheduler = BackgroundScheduler(timezone="Asia/Seoul")
        sess = session_info()
        # 매매 루프 — 세션 체크 주기로 시작
        _scheduler.add_job(
            _trading_loop, "interval",
            seconds=sess["check_sec"],
            id="trading_loop",
            replace_existing=True,
        )
        # 세션 감시 — 30초마다
        _scheduler.add_job(
            _session_watcher, "interval",
            seconds=30,
            id="session_watcher",
            replace_existing=True,
        )
        # 일일 스크리닝 — 매일 16:05 KST (장마감 후)
        _scheduler.add_job(
            _daily_screen_job, "cron",
            hour=16, minute=5,
            timezone="Asia/Seoul",
            id="daily_screener",
            replace_existing=True,
        )
        # 전략 실험실 주간 랭킹 — 매주 월요일 09:00 KST
        _scheduler.add_job(
            _weekly_lab_ranking_job, "cron",
            day_of_week="mon", hour=9, minute=0,
            timezone="Asia/Seoul",
            id="weekly_lab_ranking",
            replace_existing=True,
        )
        _scheduler.start()

    _log(f"🚀 자동매매 봇 시작! 현재 세션: {session_info()['session']}", "info")
    notifier.notify_system("자동매매 봇 시작")
    return jsonify({"ok": True})


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    global _bot_running
    _bot_running = False
    _log("⏹ 자동매매 봇 정지", "info")
    notifier.notify_system("자동매매 봇 정지")
    return jsonify({"ok": True})


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


# ══════════════════════════════════════════════════════════
# 스크리너 API 엔드포인트
# ══════════════════════════════════════════════════════════

_screener      = None      # DailyScreener 인스턴스
_last_screen   = {}        # 마지막 스크리닝 결과 캐시


def _get_screener():
    """
    DailyScreener 인스턴스 반환.
    내부 fetcher 는 항상 KISDataFetcher 를 사용한다.
    KISDataFetcher 는 KISApi 의 서브클래스이며 스크리너 전용 메서드를 추가한다.
    _api 가 있어도 KISDataFetcher 로 래핑해서 사용해야 한다.
    """
    global _screener
    from screener.daily_screener import DailyScreener
    from screener.kis_data_fetcher import KISDataFetcher

    # 유효한 실제 KIS 키 여부 판단 (placeholder / 빈 값이면 demo)
    app_key = Config.KIS_APP_KEY or ""
    is_real_key = (
        _api is not None
        and len(app_key) >= 36          # 실제 KIS 앱키는 36자
        and not app_key.startswith("demo")
        and not app_key.startswith("test")
    )
    demo = not is_real_key

    if _screener is None:
        fetcher = KISDataFetcher(demo_mode=demo)
        _screener = DailyScreener(fetcher=fetcher, demo_mode=demo)
    else:
        # demo 모드가 바뀐 경우 (API 초기화 전후) fetcher 교체
        if _screener.demo_mode != demo:
            fetcher = KISDataFetcher(demo_mode=demo)
            _screener.fetcher   = fetcher
            _screener.demo_mode = demo
    return _screener


@app.route("/screener")
def screener_page():
    """스크리너 대시보드 페이지"""
    return render_template("screener.html",
                           watch_list=_watch_list,
                           is_real=Config.KIS_IS_REAL)


@app.route("/api/screener/run", methods=["POST"])
def api_screener_run():
    """즉시 스크리닝 실행 (백그라운드)"""
    global _last_screen
    def _run():
        global _last_screen
        try:
            _log("📊 종목 스크리닝 시작...", "info")
            sc = _get_screener()
            result = sc.run()
            _last_screen = result
            cnt = result["summary"]["buy_candidate"]
            _log(f"✅ 스크리닝 완료 — 매수후보 {cnt}개", "info")
            socketio.emit("screen_done", {
                "summary":    result["summary"],
                "candidates": result["candidates_10"][:10],
                "focus":      result["focus_30"][:30],
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
                           watch_list=_watch_list,
                           is_real=Config.KIS_IS_REAL)


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

    socketio.run(app, host="0.0.0.0", port=Config.DASHBOARD_PORT,
                 debug=False, allow_unsafe_werkzeug=True)
