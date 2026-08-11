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
        """자동시작·/api/bot/start 소스가 모두 공용 등록 함수를 호출한다."""
        with open(_APP, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("_register_all_scheduled_jobs(_scheduler, sess)"), 2)
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
    def _loop_ns(self, tradeable, watch, positions=None, api=None):
        us_strat = _FakeUsStrategy()
        us_strat.pos_mgr.positions = positions or {}
        health = {"last_started_at": None, "last_finished_at": None,
                  "last_result": "running", "last_duration_sec": None,
                  "last_skip_reason": None, "run_count": 0}
        ns = _extract(
            ["_us_trading_loop"],
            extra_globals={
                "_bot_running": True,
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


# ══════════════════════════════════════════════════════════════
# heartbeat: 정규장인데 180초+ 미완료 시 ERROR
# ══════════════════════════════════════════════════════════════
class HeartbeatTest(unittest.TestCase):
    def _hb_ns(self, tradeable, health):
        return _extract(
            ["_us_heartbeat_check"],
            extra_globals={
                "_us_loop_health": health,
                "us_session_info": lambda: {"tradeable": tradeable},
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


if __name__ == "__main__":
    unittest.main()
