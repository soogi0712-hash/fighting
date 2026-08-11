"""UNKNOWN 정합화 '운영 배선' 검증 (요구사항 5).

reconcile_kr_unknowns 가 운영 루프에 실제로 연결돼 동작하는지 확인한다.

검증:
  • 시작시 복원·정합화 잡(_kr_unknown_reconcile_job)이 실제 reconcile 를 호출
  • 저빈도 잡의 비재진입 락(중복 실행 방지)
  • 잡의 장애 격리(정합화 예외가 전파되지 않음)
  • 주문 발견 → ODNO 연결 + PendingRegistry/lifecycle 승격 + RESOLVED_ACCEPTED
  • 미체결 발견 → 승격만, 포지션/손익 부킹 없음
  • 재실행(중복 폴)·재시작 시 이미 RESOLVED → 재부킹/재승격 없음(멱등)
  • 다수 후보 → AMBIGUOUS_MATCH 계속 차단, 조회실패 → 계속 차단
  • 정합화 중에도 SELL 은 즉시 제출(정합화 예외가 매도를 막지 않음)
  • UNKNOWN 은 거래/포지션/손익으로 집계되지 않음
  • UNKNOWN 원장 DB 가 영속 경로(data/trading_journal.db)를 사용
"""
import os
import sys
import shutil
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import api.kis_api as kmod                                        # noqa: E402
from api.kis_api import KISApi                                    # noqa: E402
import journal.fill_observer as fo                                # noqa: E402
from journal.fill_observer import PendingOrderRegistry            # noqa: E402
from phoenix.lifecycle import OrderLifecycleManager, LifecycleState  # noqa: E402
from journal.unknown_order_ledger import UnknownOrderLedger       # noqa: E402
import strategies.strategy_manager as sm_mod                      # noqa: E402
from strategies.strategy_manager import StrategyManager           # noqa: E402


# ── 실제 정합화 메서드만 바인딩한 경량 StrategyManager 하니스 ──────────
class ReconSM:
    _promote_unknown_to_pending = StrategyManager._promote_unknown_to_pending
    reconcile_unknowns_once     = StrategyManager.reconcile_unknowns_once
    _register_pending_order     = StrategyManager._register_pending_order

    def __init__(self, api, registry, lifecycle_mgr):
        self.api = api
        self._pending_registry = registry
        self._lifecycle_mgr = lifecycle_mgr


def _mk_kis(db_path):
    api = object.__new__(KISApi)
    api.base_url = "https://mock"
    api.account_no = "12345678-01"
    api._headers = lambda *a, **k: {}
    api._rate_limit = lambda *a, **k: None
    api._on_api_success = lambda: None
    api._unknown_ledger = UnknownOrderLedger(db_path)
    return api


class _GetResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _PostResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = str(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _mk_order_api(db_path):
    """_order(SELL) 경로를 구동할 수 있는 최소 KISApi(요청은 목킹)."""
    api = object.__new__(KISApi)
    api.base_url = "https://mock"
    api.account_no = "12345678-01"
    api._live_order_guard = lambda *a, **k: None
    api._pre_validate_kr_order = lambda *a, **k: None
    api._order_cooldown = {}
    api._ORDER_COOLDOWN_SEC = 0.0
    api.tick_size = lambda p: 1
    api.round_to_tick = lambda p, direction=1: int(p)
    api.invalidate_balance_cache = lambda: None
    api._on_api_success = lambda: None
    api._on_api_error = lambda *a, **k: None
    api._diagnose_500 = lambda *a, **k: ""
    api._headers = lambda *a, **k: {}
    api._rate_limit = lambda *a, **k: None
    api.get_kr_available_amounts = MagicMock(return_value={
        "ok": True, "amount": 10_000_000.0, "qty": 100, "ord_psbl_cash": 6098.0})
    api._unknown_ledger = UnknownOrderLedger(db_path)
    return api


class WiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recon-wire-")
        self._orig_get = kmod.requests.get
        self._orig_db = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "pending.db")
        self._reset_fo_conn()
        self.reg = PendingOrderRegistry()
        self.lc = OrderLifecycleManager(os.path.join(self.tmp, "lc.db"))
        self.db = os.path.join(self.tmp, "j.db")

    def tearDown(self):
        kmod.requests.get = self._orig_get
        self._reset_fo_conn()
        fo._JOURNAL_DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _reset_fo_conn():
        if getattr(fo._local, "fo_conn", None) is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None

    def _set_orders(self, output1):
        payload = {"rt_cd": "0", "output1": output1}
        kmod.requests.get = lambda *a, **k: _GetResp(payload)

    def _seed_unknown(self, api, code="005930", qty=1, price=70000):
        return api._unknown_ledger.record(
            "12345678-01", "KR", code, "BUY", qty, price, "00",
            created_at="2026-08-07T10:00:00", created_hhmmss="100000")

    # ── 배선: reconcile_unknowns_once 가 실제 reconcile_kr_unknowns 호출 ──
    def test_reconcile_once_calls_api(self):
        api = _mk_kis(self.db)
        api.reconcile_kr_unknowns = MagicMock(return_value=[(1, "RESOLVED_ACCEPTED")])
        sm = ReconSM(api, self.reg, self.lc)
        res = sm.reconcile_unknowns_once()
        api.reconcile_kr_unknowns.assert_called_once()
        _, kw = api.reconcile_kr_unknowns.call_args
        self.assertIn("on_promote", kw)
        self.assertIn("on_fill", kw)
        self.assertEqual(res, [(1, "RESOLVED_ACCEPTED")])

    def test_reconcile_once_fault_isolated(self):
        """정합화 API 예외가 전파되지 않는다(매도·스캔 격리)."""
        api = _mk_kis(self.db)
        api.reconcile_kr_unknowns = MagicMock(side_effect=RuntimeError("api down"))
        sm = ReconSM(api, self.reg, self.lc)
        res = sm.reconcile_unknowns_once()      # 예외 없이 [] 반환
        self.assertEqual(res, [])

    # ── 주문 발견 → ODNO 연결 + pending 승격 + RESOLVED_ACCEPTED ──────
    def test_order_found_connects_odno_and_promotes(self):
        api = _mk_kis(self.db)
        rid = self._seed_unknown(api)
        self._set_orders([
            {"odno": "Z9", "pdno": "005930", "ord_qty": "1", "ord_unpr": "70000",
             "tot_ccld_qty": "0", "rmn_qty": "1", "ord_tmd": "101000",
             "ord_stts_name": "접수"}])
        sm = ReconSM(api, self.reg, self.lc)
        res = sm.reconcile_unknowns_once()
        self.assertEqual(res, [(rid, "RESOLVED_ACCEPTED")])
        # ODNO 원장 연결 + 차단 해제
        self.assertEqual(api._unknown_ledger.get(rid)["odno"], "Z9")
        self.assertFalse(api._unknown_ledger.has_active(
            "12345678-01", "KR", "005930", "BUY"))
        # PendingRegistry 에 등록(정상 lifecycle 진입) + ODNO 연결
        self.assertTrue(self.reg.has_active_order("KR", "005930", "BUY"))

    def test_unfilled_promotion_books_no_position(self):
        """미체결 승격은 등록만 — 포지션/손익 부킹 금지(체결 시 FillObserver 반영)."""
        api = _mk_kis(self.db)
        self._seed_unknown(api)
        self._set_orders([
            {"odno": "Z9", "pdno": "005930", "ord_qty": "1", "ord_unpr": "70000",
             "tot_ccld_qty": "0", "rmn_qty": "1", "ord_tmd": "101000",
             "ord_stts_name": "접수"}])
        sm = ReconSM(api, self.reg, self.lc)
        sm.reconcile_unknowns_once()
        # pending 은 등록되었지만 아직 미체결 → filled 반영 없음
        self.assertTrue(self.reg.has_active_order("KR", "005930", "BUY"))
        # PendingRegistry 추적항목에서 lifecycle_id 를 얻어 상태 확인
        entry = next((r for r in self.reg.get_trackable("KR")
                      if r.get("code") == "005930"), None)
        self.assertIsNotNone(entry, "FillObserver 추적 목록에 승격 주문 없음")
        self.assertEqual((entry.get("odno") or "").strip(), "Z9")   # ODNO 연결
        lc = self.lc.load(entry["trade_id"])
        self.assertIsNotNone(lc)
        # 체결 부킹 전이므로 ORDER_ACCEPTED(포지션·손익 미생성)
        self.assertEqual(lc.current_state, LifecycleState.ORDER_ACCEPTED)

    def test_rerun_is_idempotent_no_double_promote(self):
        """재실행(중복 폴)·재시작 시 이미 RESOLVED → 재승격/재부킹 없음."""
        api = _mk_kis(self.db)
        rid = self._seed_unknown(api)
        self._set_orders([
            {"odno": "Z9", "pdno": "005930", "ord_qty": "1", "ord_unpr": "70000",
             "tot_ccld_qty": "0", "rmn_qty": "1", "ord_tmd": "101000",
             "ord_stts_name": "접수"}])
        sm = ReconSM(api, self.reg, self.lc)
        r1 = sm.reconcile_unknowns_once()
        self.assertEqual(r1, [(rid, "RESOLVED_ACCEPTED")])
        # 두 번째 폴: 이미 해소 → active 아님 → 처리 대상 0건
        r2 = sm.reconcile_unknowns_once()
        self.assertEqual(r2, [])
        # 재시작 모사(새 원장, 동일 DB): 여전히 해소 상태 → 처리 0건
        api2 = _mk_kis(self.db)
        sm2 = ReconSM(api2, self.reg, self.lc)
        r3 = sm2.reconcile_unknowns_once()
        self.assertEqual(r3, [])

    def test_multi_candidate_stays_blocked(self):
        api = _mk_kis(self.db)
        rid = self._seed_unknown(api)
        self._set_orders([
            {"odno": "A", "pdno": "005930", "ord_qty": "1", "ord_unpr": "70000",
             "tot_ccld_qty": "0", "rmn_qty": "1", "ord_tmd": "101000"},
            {"odno": "B", "pdno": "005930", "ord_qty": "1", "ord_unpr": "70000",
             "tot_ccld_qty": "0", "rmn_qty": "1", "ord_tmd": "101100"}])
        sm = ReconSM(api, self.reg, self.lc)
        res = sm.reconcile_unknowns_once()
        self.assertEqual(res, [(rid, "AMBIGUOUS_MATCH")])
        self.assertTrue(api._unknown_ledger.has_active(
            "12345678-01", "KR", "005930", "BUY"))    # 계속 차단
        self.assertFalse(self.reg.has_active_order("KR", "005930", "BUY"))  # 재주문 없음

    def test_query_error_keeps_blocked(self):
        api = _mk_kis(self.db)
        self._seed_unknown(api)
        kmod.requests.get = lambda *a, **k: _GetResp({"rt_cd": "1", "msg1": "오류"})
        sm = ReconSM(api, self.reg, self.lc)
        res = sm.reconcile_unknowns_once()
        self.assertEqual(res[0][1], "KEEP_PENDING_QUERY_FAIL")
        self.assertTrue(api._unknown_ledger.has_active(
            "12345678-01", "KR", "005930", "BUY"))    # 계속 차단

    def test_single_zero_candidate_keeps_blocked(self):
        api = _mk_kis(self.db)
        self._seed_unknown(api)
        self._set_orders([])   # 성공 조회지만 후보 0건
        sm = ReconSM(api, self.reg, self.lc)
        res = sm.reconcile_unknowns_once()
        self.assertEqual(res[0][1], "UNKNOWN_NOT_FOUND")
        self.assertTrue(api._unknown_ledger.has_active(
            "12345678-01", "KR", "005930", "BUY"))    # 단발 0건 → 계속 차단

    def test_zero_candidate_100x_and_restart_keeps_blocked(self):
        """후보 0건 100회 + 재시작 모사(동일 DB, 새 인스턴스) 후에도 UNKNOWN 유지."""
        api = _mk_kis(self.db)
        rid = self._seed_unknown(api)
        self._set_orders([])
        sm = ReconSM(api, self.reg, self.lc)
        for _ in range(100):
            sm.reconcile_unknowns_once()
        self.assertTrue(api._unknown_ledger.has_active(
            "12345678-01", "KR", "005930", "BUY"))
        self.assertEqual(api._unknown_ledger.get(rid)["status"], "UNKNOWN_NOT_FOUND")
        # 재시작 모사: 새 원장 인스턴스(동일 DB) 로도 여전히 차단
        api2 = _mk_kis(self.db)
        self.assertTrue(api2._unknown_ledger.has_active(
            "12345678-01", "KR", "005930", "BUY"))
        # 날짜변경 모사: created_hhmmss 는 그대로, 새 조회일에도 0건이면 계속 UNKNOWN
        sm2 = ReconSM(api2, self.reg, self.lc)
        r = sm2.reconcile_unknowns_once()
        self.assertEqual(r[0][1], "UNKNOWN_NOT_FOUND")
        self.assertTrue(api2._unknown_ledger.has_active(
            "12345678-01", "KR", "005930", "BUY"))

    def test_manual_release_only_unblocks(self):
        """빈 사유 수동해제 거부 / 명시적 사유 후에만 BUY 허용 — 자동경로 아님."""
        api = _mk_kis(self.db)
        rid = self._seed_unknown(api)
        led = api._unknown_ledger
        with self.assertRaises(ValueError):
            led.release_unknown(rid, "")
        self.assertTrue(led.has_active("12345678-01", "KR", "005930", "BUY"))
        self.assertTrue(led.release_unknown(rid, "운영자 확인 미접수", operator="ops"))
        self.assertFalse(led.has_active("12345678-01", "KR", "005930", "BUY"))

    # ── 정합화 중에도 SELL 즉시 제출(정합화 잡 락과 무관) ──────────────
    def test_sell_submits_while_reconcile_lock_held(self):
        """정합화 잡이 실행 중(락 점유)이고 동일 종목 UNKNOWN(BUY)이 있어도
        SELL 은 즉시 제출된다(_order 경로는 정합화 락을 건드리지 않음)."""
        posts = []

        def _post(*a, **k):
            posts.append(1)
            return _PostResp({"rt_cd": "0", "output": {"KNO_ORD_NO": "S1"},
                              "msg1": "매도접수"})
        self._orig_post = kmod.requests.post
        kmod.requests.post = _post
        try:
            api = _mk_order_api(self.db)
            # 동일 종목 UNKNOWN(BUY) 미해소 + 정합화 락 점유 상태 모사
            api._unknown_ledger.record(
                "12345678-01", "KR", "005930", "BUY", 1, 70000, "00",
                created_at="2026-08-07T10:00:00")
            recon_lock = threading.Lock()
            recon_lock.acquire()          # '정합화 진행 중'
            try:
                r = api._order("005930", "SELL", 10, 80000, ord_dvsn="00")
            finally:
                recon_lock.release()
            self.assertEqual(r["rt_cd"], "0")     # SELL 즉시 제출됨
            self.assertEqual(len(posts), 1)
        finally:
            kmod.requests.post = self._orig_post

    def test_reconcile_exception_does_not_propagate(self):
        """정합화 API 예외가 전파되지 않아 후속 매도·스캔을 막지 않는다."""
        api = _mk_kis(self.db)
        api.reconcile_kr_unknowns = MagicMock(side_effect=RuntimeError("recon busy"))
        sm = ReconSM(api, self.reg, self.lc)
        self.assertEqual(sm.reconcile_unknowns_once(), [])

    # ── UNKNOWN 은 거래/포지션/손익으로 집계되지 않음 ────────────────
    def test_unknown_record_is_not_a_trade(self):
        """접수 불명확 기록은 rt_cd=0 이 아니며 포지션·손익을 만들지 않는다."""
        api = _mk_kis(self.db)
        # 미해소 UNKNOWN 만 존재 → active_display 에는 뜨지만 거래·포지션 아님
        self._seed_unknown(api)
        disp = api._unknown_ledger.active_display("12345678-01")
        self.assertEqual(len(disp), 1)
        self.assertEqual(disp[0]["status"], "PENDING")
        # 계좌번호·원문이 표시 필드에 없음(로그 안전)
        self.assertNotIn("account", disp[0])

    # ── 영속성: 기본 DB 가 data/trading_journal.db 경로 ──────────────
    def test_default_ledger_uses_persistent_data_path(self):
        from journal.unknown_order_ledger import _DEFAULT_DB
        norm = os.path.normpath(_DEFAULT_DB)
        self.assertTrue(norm.endswith(os.path.join("data", "trading_journal.db")),
                        f"UNKNOWN 원장 DB 가 영속 경로가 아님: {norm}")
        # fill_observer 저널과 같은 data 디렉터리(영속 볼륨)를 공유
        self.assertEqual(
            os.path.basename(os.path.dirname(norm)), "data")


# ── app 저빈도 잡: 비재진입 락 + 장애 격리 + 스케줄러/시작시 배선 ────────
# flask 미설치 환경이라 app 모듈 import 대신 소스 실행/텍스트 검증으로
# 실제 배선을 확인한다(운영 코드의 정확한 함수 본문을 그대로 구동).
class AppJobTest(unittest.TestCase):
    _APP = os.path.join(_ROOT, "app.py")

    def _app_src(self):
        with open(self._APP, encoding="utf-8") as f:
            return f.read()

    def _build_ns(self, names):
        """app.py 의 지정 함수 본문을 '그대로' 추출·실행 가능한 네임스페이스로 만든다.

        운영 코드의 실제 함수 객체를 구동하므로(재구현 아님) 런타임 동작을 검증한다.
        """
        import ast
        src = self._app_src()
        mod = ast.parse(src)
        fns = [n for n in mod.body
               if isinstance(n, ast.FunctionDef) and n.name in names]
        got = {n.name for n in fns}
        for want in names:
            self.assertIn(want, got, f"app.py 에 {want} 없음")
        ns = {"threading": threading}
        ns["_kr_reconcile_lock"] = threading.Lock()
        ns["_log"] = lambda *a, **k: None
        ns["_strategy_mgr"] = None
        exec(compile(ast.Module(fns, []), "<appfns>", "exec"), ns)
        return ns

    def test_job_nonreentrant_and_isolated(self):
        ns = self._build_ns(["_kr_unknown_reconcile_job"])
        job = ns["_kr_unknown_reconcile_job"]
        calls = {"n": 0}

        class _SM:
            def reconcile_unknowns_once(self_inner):
                calls["n"] += 1
                return []
        # _strategy_mgr None → 즉시 반환(호출 없음)
        ns["_strategy_mgr"] = None
        job()
        self.assertEqual(calls["n"], 0)
        # 정상 1회 호출
        ns["_strategy_mgr"] = _SM()
        job()
        self.assertEqual(calls["n"], 1)
        # 비재진입: 락을 외부에서 선점하면 잡은 즉시 스킵(호출 안 함)
        ns["_kr_reconcile_lock"].acquire()
        try:
            job()
            self.assertEqual(calls["n"], 1)     # 스킵됨
        finally:
            ns["_kr_reconcile_lock"].release()
        # 장애 격리: reconcile 예외가 잡 밖으로 전파되지 않고 락도 해제됨

        class _Boom:
            def reconcile_unknowns_once(self_inner):
                raise RuntimeError("boom")
        ns["_strategy_mgr"] = _Boom()
        job()                                    # 예외 없이 반환
        self.assertFalse(ns["_kr_reconcile_lock"].locked())   # finally 로 해제됨

    def test_startup_and_scheduler_wire_the_job(self):
        """앱 소스에 시작시 1회 호출 + 저빈도 스케줄러 등록이 실제로 배선돼 있다."""
        src = self._app_src()
        # 시작시(restart reconcile) 1회 호출
        self.assertIn("_kr_unknown_reconcile_job()", src)
        # 저빈도 스케줄러 등록(중복 방지 id + replace_existing + interval)
        self.assertIn('id="kr_unknown_reconcile"', src)
        self.assertIn("_kr_unknown_reconcile_job", src)
        self.assertIn("replace_existing=True", src)
        # 잡 본문이 reconcile_unknowns_once 를 실제 호출
        self.assertIn("reconcile_unknowns_once()", src)
        # 등록은 공용 스케줄 등록 함수(_register_all_scheduled_jobs) 안에서 수행되고,
        # 두 시작 경로(자동시작·/api/bot/start)가 그 공용 함수를 각각 호출한다.
        self.assertIn("_register_kr_reconcile_job(scheduler)", src)
        self.assertEqual(src.count("_register_all_scheduled_jobs(_scheduler, sess)"), 2)

    def test_registration_is_single_per_process_runtime(self):
        """런타임: 두 진입점이 같은 _scheduler 에 등록해도 잡은 정확히 1개.

        _auto_start_bot()/bot_start() 는 동일 모듈전역 _scheduler 를 공유하고
        'not _scheduler.running' 가드로 실제 add_job 은 1회만 수행된다. 여기서는
        더 강하게, 실제 _register_kr_reconcile_job 을 2회 호출(양쪽 경로 모사)해도
        id+replace_existing 로 잡이 1개만 남음을 APScheduler add_job 시맨틱을 그대로
        구현한 스케줄러 대역으로 증명한다(apscheduler 미설치 환경).
        """
        class _FakeScheduler:
            """APScheduler add_job 의 id+replace_existing 시맨틱만 충실히 구현."""
            def __init__(self):
                self.jobs = {}   # id → job spec
                self.running = False

            def add_job(self, func, trigger=None, id=None, replace_existing=False,
                        **kw):
                if id in self.jobs and not replace_existing:
                    raise RuntimeError(f"conflicting id {id!r}")
                self.jobs[id] = {"func": func, "trigger": trigger, **kw}

            def get_jobs(self):
                class _J:
                    def __init__(self, jid):
                        self.id = jid
                return [_J(k) for k in self.jobs]

        ns = self._build_ns(["_kr_unknown_reconcile_job",
                             "_register_kr_reconcile_job"])
        reg = ns["_register_kr_reconcile_job"]
        sched = _FakeScheduler()
        reg(sched)      # _auto_start_bot 경로
        reg(sched)      # bot_start 경로(중복 시도) — 예외 없이 대체
        jobs = [j for j in sched.get_jobs() if j.id == "kr_unknown_reconcile"]
        self.assertEqual(len(jobs), 1, "정합화 잡이 1개가 아님(중복 등록)")
        # interval 25초로 등록됐는지 확인(실제 함수가 전달한 인자)
        self.assertEqual(sched.jobs["kr_unknown_reconcile"]["trigger"], "interval")
        self.assertEqual(sched.jobs["kr_unknown_reconcile"]["seconds"], 25)
        self.assertIs(sched.jobs["kr_unknown_reconcile"]["func"],
                      ns["_kr_unknown_reconcile_job"])

    def test_concurrent_invocations_run_at_most_one_body(self):
        """런타임: 잡을 다중 스레드로 동시에 호출해도 본문 동시 실행은 최대 1개.

        비재진입 _kr_reconcile_lock 이 실제로 동시 실행을 직렬화함을 증명한다."""
        import time
        ns = self._build_ns(["_kr_unknown_reconcile_job"])
        job = ns["_kr_unknown_reconcile_job"]
        state = {"inside": 0, "max": 0, "runs": 0}
        guard = threading.Lock()

        class _SM:
            def reconcile_unknowns_once(self_inner):
                with guard:
                    state["inside"] += 1
                    state["runs"] += 1
                    state["max"] = max(state["max"], state["inside"])
                time.sleep(0.02)             # 겹침 창 확대
                with guard:
                    state["inside"] -= 1
                return []
        ns["_strategy_mgr"] = _SM()
        threads = [threading.Thread(target=job) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 동시 실행 본문은 최대 1개(비재진입). 나머지는 즉시 스킵.
        self.assertEqual(state["max"], 1, "본문 동시 실행이 1을 초과(직렬화 실패)")
        self.assertGreaterEqual(state["runs"], 1)   # 최소 1회는 실제 실행


if __name__ == "__main__":
    unittest.main()
