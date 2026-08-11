"""P0: 미국 매매 루프를 국내 루프에서 완전 분리(독립 스케줄러 잡) 검증.

운영 실증 근본원인: 국내 _trading_loop 최초 실행이 장시간 종료되지 않아
_trading_loop_lock 을 계속 점유 → _us_trading_loop() 가 국내 루프 내부에서만
호출되던 구조라 미국 루프가 영구 미실행.

수정 검증:
  - 미국 매매는 독립 잡 _us_trading_job(APScheduler interval 60s)이 담당
  - US 전용 비재진입 락(_us_trading_loop_lock) — KR 락과 절대 공유 안 함
  - 공용 등록 함수 _register_all_scheduled_jobs 가 KR·US 잡을 각각 정확히 1개 등록
  - KR 지연/정지/락점유/예외와 무관하게 US 잡 독립 실행
  - US 예외↔KR 예외 상호 전파 없음(격리)
  - 관찰성(_us_loop_health), tradeable 순회/비-tradeable 스킵

flask 미설치 → app.py import 대신 함수 본문을 AST 추출·실행(운영 코드 그대로 구동).
"""
import os
import sys
import ast
import time
import types
import logging
import threading
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_APP = os.path.join(_ROOT, "app.py")


def _extract(names, extra_globals=None):
    """app.py 의 지정 함수 본문을 그대로 추출·실행 가능한 네임스페이스로 만든다."""
    with open(_APP, encoding="utf-8") as f:
        mod = ast.parse(f.read())
    fns = [n for n in mod.body
           if isinstance(n, ast.FunctionDef) and n.name in names]
    got = {n.name for n in fns}
    for want in names:
        assert want in got, f"app.py 에 {want} 없음"
    ns = {"threading": threading, "time": time, "logging": logging,
          "logger": logging.getLogger("us-loop-test")}
    from datetime import datetime as _dt
    ns["datetime"] = _dt
    ns.update(extra_globals or {})
    exec(compile(ast.Module(fns, []), "<appfns>", "exec"), ns)
    return ns


class _FakeScheduler:
    """APScheduler add_job 의 id+replace_existing 시맨틱만 충실히 구현."""
    def __init__(self):
        self.jobs = {}
        self.running = False

    def add_job(self, func, trigger=None, id=None, replace_existing=False, **kw):
        if id in self.jobs and not replace_existing:
            raise RuntimeError(f"conflicting id {id!r}")
        self.jobs[id] = {"func": func, "trigger": trigger, **kw}


# ══════════════════════════════════════════════════════════════
# 잡 등록: KR·US 각각 정확히 1개 + 중복 없음
# ══════════════════════════════════════════════════════════════
class RegistrationTest(unittest.TestCase):
    _JOB_NAMES = [
        "_register_all_scheduled_jobs", "_register_kr_reconcile_job",
    ]
    _STUB_JOBS = [
        "_trading_loop", "_us_trading_job", "_session_watcher",
        "_daily_screen_job", "_weekly_lab_ranking_job", "_us_market_job",
        "_us_intraday_job", "_cancel_pending_buy_orders",
        "_kr_unknown_reconcile_job",
    ]

    def _ns(self):
        stubs = {n: (lambda *a, **k: None) for n in self._STUB_JOBS}
        return _extract(self._JOB_NAMES, extra_globals=stubs)

    def test_registers_kr_and_us_each_once(self):
        ns = self._ns()
        sched = _FakeScheduler()
        ns["_register_all_scheduled_jobs"](sched, {"check_sec": 30})
        ids = list(sched.jobs.keys())
        self.assertEqual(ids.count("trading_loop"), 1)
        self.assertEqual(ids.count("us_trading_loop"), 1)
        # US 잡 파라미터: 60초 interval, max_instances=1, coalesce=True
        us = sched.jobs["us_trading_loop"]
        self.assertEqual(us["trigger"], "interval")
        self.assertEqual(us["seconds"], 60)
        self.assertEqual(us["max_instances"], 1)
        self.assertTrue(us["coalesce"])
        # US 잡 함수는 독립 잡 래퍼(_us_trading_job)
        self.assertIs(us["func"], ns["_us_trading_job"])

    def test_double_registration_no_duplicates(self):
        """두 시작 경로(자동시작·/api/bot/start) 연속 호출 모사 → 중복 잡 없음."""
        ns = self._ns()
        sched = _FakeScheduler()
        ns["_register_all_scheduled_jobs"](sched, {"check_sec": 30})  # _auto_start_bot
        ns["_register_all_scheduled_jobs"](sched, {"check_sec": 45})  # bot_start
        for jid in ("trading_loop", "us_trading_loop", "session_watcher",
                    "kr_unknown_reconcile"):
            self.assertIn(jid, sched.jobs)
        # id 별 정확히 1개(중복 없음) — jobs 는 dict 라 id 유일성 보장 + 예외 없이 대체
        self.assertEqual(len(sched.jobs), len(set(sched.jobs)))
        self.assertEqual(sched.jobs["us_trading_loop"]["seconds"], 60)

    def test_both_start_paths_use_shared_registrar(self):
        """자동시작·/api/bot/start 소스가 모두 공용 시작 함수(_ensure_scheduler_started)를
        호출하고, 그 함수만 공용 등록 함수를 호출한다(등록 로직 단일화)."""
        with open(_APP, encoding="utf-8") as f:
            src = f.read()
        # 두 진입점이 공용 시작 함수를 호출
        self.assertEqual(src.count("_ensure_scheduler_started(session_info())"), 2)
        # 공용 등록 함수 호출은 시작 함수 안 1곳뿐
        self.assertEqual(src.count("_register_all_scheduled_jobs(_scheduler, sess)"), 1)
        # 인라인 add_job(_trading_loop ...) 중복 블록이 남아있지 않아야 함
        self.assertEqual(src.count("add_job(_trading_loop"), 1)  # 등록 함수 내부 1곳뿐


# ══════════════════════════════════════════════════════════════
# 독립 실행: KR 정지/락점유와 무관, 비재진입, 격리
# ══════════════════════════════════════════════════════════════
class IndependentJobTest(unittest.TestCase):
    def _job_ns(self, us_loop_impl):
        return _extract(
            ["_us_trading_job"],
            extra_globals={
                "_us_trading_loop_lock": threading.Lock(),
                "_us_loop_health": {
                    "last_started_at": None, "last_finished_at": None,
                    "last_result": None, "last_duration_sec": None,
                    "last_skip_reason": None, "run_count": 0},
                "_us_trading_loop": us_loop_impl,
                "_log": lambda *a, **k: None,
            })

    def test_runs_while_kr_loop_lock_held(self):
        """KR 루프가 락을 점유(강제 정지 모사)해도 US 잡은 3회 이상 실행."""
        calls = {"n": 0}
        ns = self._job_ns(lambda: calls.__setitem__("n", calls["n"] + 1))
        # KR 락을 별도로 만들어 계속 점유(=국내 루프 무한 실행 모사)
        kr_lock = threading.Lock()
        kr_lock.acquire()
        try:
            for _ in range(3):
                ns["_us_trading_job"]()
        finally:
            kr_lock.release()
        self.assertEqual(calls["n"], 3)                 # KR 락과 무관하게 실행
        self.assertEqual(ns["_us_loop_health"]["run_count"], 3)
        self.assertEqual(ns["_us_loop_health"]["last_result"], "ok")

    def test_us_lock_is_separate_from_kr_lock(self):
        """_us_trading_job 은 US 전용 락만 참조하고 KR 락(_trading_loop_lock)은
        참조하지 않는다(AST 식별자 정확 매칭 — 부분문자열 오탐 방지)."""
        with open(_APP, encoding="utf-8") as f:
            src = f.read()
        # 두 락이 서로 다른 이름으로 각각 정의됨
        self.assertIn("_us_trading_loop_lock = threading.Lock()", src)
        self.assertIn("_trading_loop_lock = threading.Lock()", src)
        mod = ast.parse(src)
        job = next(n for n in mod.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_us_trading_job")
        names = {n.id for n in ast.walk(job) if isinstance(n, ast.Name)}
        self.assertIn("_us_trading_loop_lock", names)   # US 락 사용
        self.assertNotIn("_trading_loop_lock", names)   # KR 락 미사용(식별자)

    def test_nonreentrant_max_one_body_concurrent(self):
        """US 잡 동시 8회 호출 시 본체 동시 실행 최대 1개(비재진입)."""
        state = {"inside": 0, "max": 0, "runs": 0}
        guard = threading.Lock()

        def _impl():
            with guard:
                state["inside"] += 1
                state["runs"] += 1
                state["max"] = max(state["max"], state["inside"])
            time.sleep(0.02)
            with guard:
                state["inside"] -= 1
        ns = self._job_ns(_impl)
        job = ns["_us_trading_job"]
        threads = [threading.Thread(target=job) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(state["max"], 1, "본체 동시 실행이 1 초과(비재진입 실패)")
        self.assertGreaterEqual(state["runs"], 1)
        # 락 경합으로 스킵된 호출은 skip_reason 기록
        self.assertIsNotNone(ns["_us_loop_health"].get("last_skip_reason"))

    def test_D_running_skips_then_next_call_runs(self):
        """[D] 실행 중(락 점유) 중복 호출은 스킵, 완료(락 해제) 후 다음 호출은 정상."""
        calls = {"n": 0}
        ns = self._job_ns(lambda: calls.__setitem__("n", calls["n"] + 1))
        job = ns["_us_trading_job"]
        # 본체 실행 중 모사: US 락을 외부에서 선점
        ns["_us_trading_loop_lock"].acquire()
        job()                                            # 스킵(본체 미실행)
        self.assertEqual(calls["n"], 0)
        self.assertIn("lock_busy", str(ns["_us_loop_health"]["last_skip_reason"]))
        ns["_us_trading_loop_lock"].release()            # 완료 모사
        job()                                            # 다음 호출 정상 실행
        self.assertEqual(calls["n"], 1)
        self.assertEqual(ns["_us_loop_health"]["last_result"], "ok")

    def test_health_is_json_serializable_and_no_raw_exception(self):
        """[#10/#13] 예외 후에도 _us_loop_health 는 JSON 직렬화 가능 + 예외 원문 미노출."""
        import json
        secret = "ACCT-9999-11 raw-broker-body SECRET"

        def _impl():
            raise RuntimeError(secret)
        ns = self._job_ns(_impl)
        ns["_us_trading_job"]()
        snap = dict(ns["_us_loop_health"])               # /api/status 와 동일한 스냅샷
        s = json.dumps(snap)                             # 직렬화 가능해야 함
        self.assertNotIn(secret, s)                      # 예외 원문(계좌·원문) 미노출
        self.assertEqual(snap["last_result"], "error: RuntimeError")  # 클래스명만

    def test_us_exception_isolated_and_lock_released(self):
        """US 본체 예외가 잡 밖으로 전파되지 않고 락도 해제된다(다음 실행 정상)."""
        seq = {"raise": True}

        def _impl():
            if seq["raise"]:
                raise RuntimeError("US boom")
        ns = self._job_ns(_impl)
        job = ns["_us_trading_job"]
        job()                                            # 예외 없이 반환
        self.assertTrue(str(ns["_us_loop_health"]["last_result"]).startswith("error"))
        self.assertFalse(ns["_us_trading_loop_lock"].locked())   # 해제됨
        # 다음 실행은 정상 동작
        seq["raise"] = False
        job()
        self.assertEqual(ns["_us_loop_health"]["last_result"], "ok")

    def test_kr_exception_does_not_block_us_job(self):
        """KR 래퍼(_trading_loop) 예외가 US 잡 실행을 막지 않는다(락·상태 독립)."""
        # KR 래퍼: impl 예외 시 finally 로 KR 락만 해제(스케줄러 레벨 격리)
        kr_ns = _extract(
            ["_trading_loop"],
            extra_globals={
                "_trading_loop_lock": threading.Lock(),
                "_trading_loop_impl": (lambda: (_ for _ in ()).throw(
                    RuntimeError("KR boom"))),
            })
        with self.assertRaises(RuntimeError):
            kr_ns["_trading_loop"]()                     # KR 잡은 예외 전파(스케줄러가 격리)
        self.assertFalse(kr_ns["_trading_loop_lock"].locked())  # KR 락은 해제됨
        # US 잡은 KR 상태와 무관하게 정상 실행
        calls = {"n": 0}
        us_ns = self._job_ns(lambda: calls.__setitem__("n", calls["n"] + 1))
        us_ns["_us_trading_job"]()
        self.assertEqual(calls["n"], 1)
        self.assertEqual(us_ns["_us_loop_health"]["last_result"], "ok")


# ══════════════════════════════════════════════════════════════
# _us_trading_loop 본체: tradeable 순회 / 비-tradeable 신규매수 없음
# ══════════════════════════════════════════════════════════════
class _FakeUsStrategy:
    def __init__(self):
        self.run_calls = []
        self.pos_mgr = types.SimpleNamespace(positions={})

    def set_realtime_cache(self, c):
        pass

    def run(self, stock):
        self.run_calls.append(stock.get("symbol"))
        return {"action": "HOLD"}

    def run_us_fill_poll(self):
        return {"dispatched": 0, "filled": 0, "partial": 0, "fill_events": []}


class UsLoopBodyTest(unittest.TestCase):
    def _loop_ns(self, tradeable, watch, positions=None, api=None,
                 bot_running=True):
        us_strat = _FakeUsStrategy()
        us_strat.pos_mgr.positions = positions or {}
        health = {"last_started_at": None, "last_finished_at": None,
                  "last_result": "running", "last_duration_sec": None,
                  "last_skip_reason": None, "run_count": 0}
        ns = _extract(
            ["_us_trading_loop"],
            extra_globals={
                "_bot_running": bot_running,
                "_us_strategy": us_strat,
                "_us_watch_list": watch,
                "_us_last_signals": {},
                "_us_loop_health": health,
                "_api": api,
                "us_session_info": lambda: {
                    "tradeable": tradeable, "time_et": "10:00", "session":
                    ("정규장" if tradeable else "애프터마켓")},
                "_us_prefetch_realtime": lambda syms: {},
                "_us_realbalance_force_sell_check": lambda: None,
                "_build_us_effect_fns": lambda ev: {},
                "_log": lambda *a, **k: None,
                "socketio": types.SimpleNamespace(emit=lambda *a, **k: None),
            })
        return ns, us_strat, health

    def test_tradeable_iterates_watchlist_and_calls_run(self):
        """미국 정규장 tradeable=True → watchlist 순회하며 run() 호출."""
        watch = [{"symbol": "AAPL", "name": "Apple"},
                 {"symbol": "MSFT", "name": "MS"},
                 {"symbol": "NVDA", "name": "NVDA"}]
        ns, us_strat, health = self._loop_ns(True, watch)
        ns["_us_trading_loop"]()
        self.assertEqual(us_strat.run_calls, ["AAPL", "MSFT", "NVDA"])

    def test_non_tradeable_no_positions_skips_no_buy(self):
        """프리/애프터/휴장 + 보유0 → 신규매수 실행 안 함(run 미호출·스킵 사유 기록)."""
        watch = [{"symbol": "AAPL", "name": "Apple"}]
        ns, us_strat, health = self._loop_ns(False, watch, positions={})
        ns["_us_trading_loop"]()
        self.assertEqual(us_strat.run_calls, [])         # 신규매수 판정 자체 없음
        self.assertIn("non_tradeable", str(health["last_skip_reason"]))
        self.assertTrue(str(health["last_result"]).startswith("skip"))

    def test_B_bot_stopped_no_scan_no_run(self):
        """[B] bot stop 상태(_bot_running=False)면 US 잡이 호출돼도 run·주문 미실행."""
        watch = [{"symbol": "AAPL", "name": "Apple"}]
        ns, us_strat, health = self._loop_ns(True, watch, bot_running=False)
        ns["_us_trading_loop"]()
        self.assertEqual(us_strat.run_calls, [])         # 스캔·run·주문 전무
        self.assertTrue(str(health["last_result"]).startswith("skip"))
        self.assertIn("bot_running=False", str(health["last_skip_reason"]))

    def test_F_non_tradeable_with_positions_still_protects(self):
        """[F] 휴장·비거래여도 보유 포지션이 있으면 체결감시·매도 보호 처리는 계속.

        tradeable=False + 보유 포지션 존재 → 조기 스킵하지 않고 watch 순회(run)와
        run_us_fill_poll(체결 폴링/보호)이 실행된다."""
        watch = [{"symbol": "AAPL", "name": "Apple"}]
        positions = {"AAPL": object()}   # 보유 1종목
        ns, us_strat, health = self._loop_ns(False, watch, positions=positions)
        # fill poll 호출 관찰용 래핑
        _polled = {"n": 0}
        _orig_poll = us_strat.run_us_fill_poll
        us_strat.run_us_fill_poll = lambda: (_polled.__setitem__("n", 1) or _orig_poll())
        ns["_us_trading_loop"]()
        self.assertEqual(us_strat.run_calls, ["AAPL"])   # 포지션 관리 위해 run 호출
        self.assertEqual(_polled["n"], 1)                # 체결 폴링(보호) 실행
        # 조기 스킵이 아니므로 skip 결과가 아님
        self.assertNotIn("non_tradeable", str(health.get("last_skip_reason")))


# ══════════════════════════════════════════════════════════════
# heartbeat: 정규장인데 180초+ 미완료 시 ERROR
# ══════════════════════════════════════════════════════════════
class SelfHealAndConfigTest(unittest.TestCase):
    """[A] running 중 누락 잡 자동복구 · [H] executor 설정(기아 방지) · [D 보강]."""

    _REG_NAMES = ["_register_all_scheduled_jobs", "_register_kr_reconcile_job"]
    _STUB_JOBS = [
        "_trading_loop", "_us_trading_job", "_session_watcher",
        "_daily_screen_job", "_weekly_lab_ranking_job", "_us_market_job",
        "_us_intraday_job", "_cancel_pending_buy_orders",
        "_kr_unknown_reconcile_job",
    ]

    def _reg_ns(self):
        stubs = {n: (lambda *a, **k: None) for n in self._STUB_JOBS}
        return _extract(self._REG_NAMES, extra_globals=stubs)

    def test_A_missing_us_job_recovered_on_reregister(self):
        """[A] 스케줄러 running 중 us_trading_loop 잡만 누락돼도 재등록으로 복구."""
        ns = self._reg_ns()
        reg = ns["_register_all_scheduled_jobs"]
        sched = _FakeScheduler()
        sched.running = True                     # 이미 running 상태 모사
        reg(sched, {"check_sec": 30})
        self.assertIn("us_trading_loop", sched.jobs)
        # 어떤 이유로 US 잡만 사라진 상태 모사
        del sched.jobs["us_trading_loop"]
        self.assertNotIn("us_trading_loop", sched.jobs)
        # 재등록(running 중) → US 잡 자동 복구, 다른 잡 중복 없음
        reg(sched, {"check_sec": 30})
        self.assertIn("us_trading_loop", sched.jobs)
        self.assertEqual(list(sched.jobs).count("trading_loop"), 1)

    def test_A_ensure_start_calls_register_unconditionally(self):
        """[A] _ensure_scheduler_started 는 running 여부와 무관하게 등록 함수를
        '먼저' 호출한다(등록이 'not running' 가드 안에 갇혀있지 않음)."""
        with open(_APP, encoding="utf-8") as f:
            src = f.read()
        mod = ast.parse(src)
        fn = next(n for n in mod.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_ensure_scheduler_started")
        # 함수 본문 최상위(top-level) 문장 중에 등록 호출이 존재해야 함(중첩 If 아님)
        top_calls = []
        for stmt in fn.body:
            for node in ast.walk(stmt):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "_register_all_scheduled_jobs"):
                    # 이 호출을 감싸는 최상위 문장이 If(running 가드)인지 확인
                    top_calls.append(type(stmt).__name__)
        self.assertIn("Expr", top_calls,
                      "_register_all_scheduled_jobs 가 최상위 문장으로 호출되지 않음"
                      "(running 가드 안에 갇혔을 수 있음)")

    def test_H_executor_config_prevents_starvation(self):
        """[H] _scheduler_kwargs: 워커 20 + job_defaults(max_instances=1/coalesce).

        KR hang 1건은 워커 1개만 점유하고 max_instances=1 로 중복 누적이 없으므로,
        20 워커 풀에서 US·session_watcher·UNKNOWN 정합화 잡이 굶지 않는다.
        (apscheduler 미설치 환경 → ThreadPoolExecutor 는 가짜 모듈로 대체해 값만 검증)"""
        import types as _types
        fake_pool_mod = _types.ModuleType("apscheduler.executors.pool")
        captured = {}

        class _FakePool:
            def __init__(self, max_workers=None):
                captured["max_workers"] = max_workers
        fake_pool_mod.ThreadPoolExecutor = _FakePool
        # import 경로 해결용 부모 패키지도 채움
        for modname in ("apscheduler", "apscheduler.executors",
                        "apscheduler.executors.pool"):
            sys.modules.setdefault(modname, _types.ModuleType(modname))
        sys.modules["apscheduler.executors.pool"] = fake_pool_mod
        try:
            ns = _extract(["_scheduler_kwargs"])
            kw = ns["_scheduler_kwargs"]()
        finally:
            for modname in ("apscheduler.executors.pool",):
                sys.modules.pop(modname, None)
        self.assertEqual(captured.get("max_workers"), 20)
        self.assertIn("default", kw["executors"])
        jd = kw["job_defaults"]
        self.assertEqual(jd["max_instances"], 1)
        self.assertTrue(jd["coalesce"])
        self.assertGreaterEqual(jd["misfire_grace_time"], 1)


class HeartbeatTest(unittest.TestCase):
    def _hb_ns(self, tradeable, health, lock=None, registered_ts=None):
        import time as _t
        return _extract(
            ["_us_heartbeat_check"],
            extra_globals={
                "_us_loop_health": health,
                "us_session_info": lambda: {"tradeable": tradeable},
                "_us_trading_loop_lock": lock or threading.Lock(),
                # 기본: 등록 후 충분히 경과(유예 지남) → fin=None 미실행 경고 판정 가능
                "_us_registered_ts": (registered_ts if registered_ts is not None
                                      else _t.time() - 9999),
            })

    def test_error_when_stale_over_180s(self):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(seconds=200)).isoformat()
        health = {"last_finished_at": old, "last_result": "ok"}
        ns = self._hb_ns(True, health)
        with self.assertLogs("us-loop-test", level="ERROR") as cm:
            ns["_us_heartbeat_check"]()
        self.assertTrue(any("US heartbeat" in m for m in cm.output))

    def test_no_error_when_recent(self):
        from datetime import datetime
        health = {"last_finished_at": datetime.now().isoformat(), "last_result": "ok"}
        ns = self._hb_ns(True, health)
        # 최근 완료 → ERROR 로그 없음
        logger = logging.getLogger("us-loop-test")
        with self.assertRaises(AssertionError):
            with self.assertLogs("us-loop-test", level="ERROR"):
                ns["_us_heartbeat_check"]()

    def test_no_error_when_not_tradeable(self):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(seconds=9999)).isoformat()
        health = {"last_finished_at": old, "last_result": "ok"}
        ns = self._hb_ns(False, health)   # 휴장 → heartbeat 대상 아님
        with self.assertRaises(AssertionError):
            with self.assertLogs("us-loop-test", level="ERROR"):
                ns["_us_heartbeat_check"]()

    def _assert_no_error(self, ns):
        with self.assertRaises(AssertionError):
            with self.assertLogs("us-loop-test", level="ERROR"):
                ns["_us_heartbeat_check"]()

    def test_no_error_at_startup_within_grace(self):
        """[#9] 앱 시작 직후(완료기록 없음 + 등록 유예 내) → 오탐 없음."""
        import time as _t
        health = {"last_finished_at": None, "last_started_at": None,
                  "last_result": None}
        ns = self._hb_ns(True, health, registered_ts=_t.time() - 10)  # 유예 내
        self._assert_no_error(ns)

    def test_error_when_never_ran_after_grace(self):
        """[#9] 정규장 + 완료기록 없음 + 등록 유예(180s) 경과 → 미실행 경고."""
        import time as _t
        health = {"last_finished_at": None, "last_started_at": None,
                  "last_result": None}
        ns = self._hb_ns(True, health, registered_ts=_t.time() - 300)
        with self.assertLogs("us-loop-test", level="ERROR") as cm:
            ns["_us_heartbeat_check"]()
        self.assertTrue(any("US heartbeat" in m for m in cm.output))

    def test_no_error_while_running_long_scan(self):
        """[#9] 본체 정상 실행 중(락 점유 + 시작 300s 이내)인 장시간 스캔 → 오탐 없음."""
        from datetime import datetime, timedelta
        lock = threading.Lock()
        lock.acquire()   # 실행 중 모사
        try:
            health = {"last_finished_at": (datetime.now() - timedelta(seconds=9999)).isoformat(),
                      "last_started_at": (datetime.now() - timedelta(seconds=90)).isoformat(),
                      "last_result": "running"}
            ns = self._hb_ns(True, health, lock=lock)
            self._assert_no_error(ns)   # 완료가 오래 전이어도 '실행 중'이면 정상
        finally:
            lock.release()

    def test_error_when_body_stuck_over_300s(self):
        """[#9] 본체가 300s+ 실행 중(락 점유, 시작 300s 초과) → 정지 의심 경고."""
        from datetime import datetime, timedelta
        lock = threading.Lock()
        lock.acquire()
        try:
            health = {"last_finished_at": None,
                      "last_started_at": (datetime.now() - timedelta(seconds=400)).isoformat(),
                      "last_result": "running"}
            ns = self._hb_ns(True, health, lock=lock)
            with self.assertLogs("us-loop-test", level="ERROR") as cm:
                ns["_us_heartbeat_check"]()
            self.assertTrue(any("실행 중" in m for m in cm.output))
        finally:
            lock.release()


if __name__ == "__main__":
    unittest.main()
