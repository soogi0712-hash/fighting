"""P0: KIS 잔고 7종목 자동 복원(독립 잡) + ODNO 누락 정합화 검증.

운영 실증: 대시보드는 KIS 7종목 표시하나 pyramid_positions.json={} → 트레일링
미작동. rt_cd=0 직후 ODNO='' 로 PendingRegistry 등록 거부됐는데 '등록 완료 odno=""'
허위 로깅.

근본원인 2건:
  (1) KIS→pyramid 복원(_sync_positions_from_balance)이 앱시작 1회 + _watchdog(국내
      _trading_loop_impl 말미)에서만 실행 → 국내 루프 지연·정지 시 복원 미실행.
      → 독립 저빈도 잡 _kr_position_restore_job 으로 분리(국내 루프 무관 계속 대사).
  (2) _register_pending_order 가 KR 응답에서 KNO_ORD_NO 만 읽어, 실주문 응답(ODNO)
      에서 항상 '' → 등록 거부. 그런데도 accept(odno='')·'등록 완료' 허위 로깅.
      → 견고 추출(ODNO 우선) + odno 없으면 register 미호출·UNKNOWN 라우팅.

검증 A~I.
"""
import os
import sys
import ast
import json
import types
import shutil
import logging
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import strategies.pyramid_strategy as ps                       # noqa: E402
from strategies.pyramid_strategy import (                       # noqa: E402
    PyramidStrategyManager, TRAILING_ACTIVATE_PCT,
)
from strategies.strategy_manager import StrategyManager         # noqa: E402
from screener.transaction_cost import price_for_net_pct_from_cost  # noqa: E402

_APP = os.path.join(_ROOT, "app.py")

# 운영 실증 7종목: (code, name, avg, cur)
SEVEN = [
    ("006400", "삼성SDI",       474500, 486500),   # +2.52%
    ("028260", "삼성물산",       357000, 357000),   #  0.00%
    ("051910", "LG화학",         276000, 278500),   # +0.90%
    ("066570", "LG전자",         197000, 203000),   # +3.04%
    ("086520", "에코프로",        90900,  90800),    # -0.11%
    ("247540", "에코프로비엠",    110100, 111500),   # +1.27%
    ("373220", "LG에너지솔루션",  359750, 366500),   # +1.87%
]


def _holdings(rows):
    return [{"code": c, "name": n, "qty": 10, "avg_price": a, "cur_price": p}
            for (c, n, a, p) in rows]


def _extract(names, extra_globals=None):
    names = list(names)
    if "_sync_positions_from_balance" in names and \
            "_sync_positions_from_balance_locked" not in names:
        names.append("_sync_positions_from_balance_locked")
    with open(_APP, encoding="utf-8") as f:
        mod = ast.parse(f.read())
    fns = [n for n in mod.body
           if isinstance(n, ast.FunctionDef) and n.name in names]
    got = {n.name for n in fns}
    for want in names:
        assert want in got, f"app.py 에 {want} 없음"
    ns = {"threading": threading, "logging": logging,
          "logger": logging.getLogger("kr-restore-test"),
          "_kr_restore_lock": threading.Lock()}
    from datetime import datetime as _dt
    ns["datetime"] = _dt
    ns.update(extra_globals or {})
    exec(compile(ast.Module(fns, []), "<appfns>", "exec"), ns)
    return ns


# ══════════════════════════════════════════════════════════════
# reconcile 레벨: 7종목 복원 + 트레일링 (A/B/C/I)
# ══════════════════════════════════════════════════════════════
class SevenHoldingsRestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p0-restore-")
        self._py, self._cp = ps.PYRAMID_FILE, ps.COMPOUND_FILE
        ps.PYRAMID_FILE = os.path.join(self.tmp, "pyramid_positions.json")
        ps.COMPOUND_FILE = os.path.join(self.tmp, "compound_pool.json")
        self.mgr = PyramidStrategyManager(None, max_per_stock=1e12, max_total=1e12)

    def tearDown(self):
        ps.PYRAMID_FILE, ps.COMPOUND_FILE = self._py, self._cp
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _broker(self):
        return {c: {"qty": 10, "avg_price": a, "name": n, "cur_price": p}
                for (c, n, a, p) in SEVEN}

    def _eval(self, code, cur, sell_score=0):
        return self.mgr.evaluate(code, "x", cur, 0, 0.0, today_high=0.0,
                                 buy_score_norm=0.0, sell_score=sell_score)

    def test_A_seven_holdings_restored_from_empty(self):
        """[A] KIS 7종목 + 빈 원장 → 7종목 모두 복원(recovered=True, 평단·수량·고가)."""
        self.assertEqual(self.mgr.positions, {})
        rep = self.mgr.reconcile_from_broker(self._broker(), active_codes=set())
        self.assertEqual(len(self.mgr.positions), 7)
        self.assertEqual(len(rep["added"]), 7)
        for (c, n, a, p) in SEVEN:
            pos = self.mgr.positions[c]
            self.assertTrue(pos.recovered)
            self.assertEqual(pos.total_qty, 10)
            self.assertEqual(pos.avg_price, a)
            self.assertEqual(pos.highest_price, max(a, p))   # highest=max(평단,현재가)

    def test_B_active_stocks_hold_immediately_after_restore(self):
        """[B] 삼성SDI(+2.52%)/LG전자(+3.04%)/LGES(+1.87%)는 활성이나 복원 즉시 HOLD.

        복원 highest=현재가(신고가)이므로 고점대비 0% → 즉시 매도하지 않는다."""
        self.mgr.reconcile_from_broker(self._broker(), active_codes=set())
        for code, _, avg, cur in SEVEN:
            pos = self.mgr.positions[code]
            # 활성 종목(고가가 +1.5% 이상)인지 확인 후에도 HOLD
            activate = price_for_net_pct_from_cost(avg, TRAILING_ACTIVATE_PCT)
            d = self._eval(code, cur)
            self.assertNotIn(d["action"], ("SELL_ALL", "SELL_PARTIAL"),
                             f"{code} 복원 직후 즉시 매도됨(정책 위반)")
            if code in ("006400", "066570", "373220"):
                self.assertGreaterEqual(pos.highest_price, activate)  # 활성 상태

    def test_C_trailing_099_hold_100_sell(self):
        """[C] 복원 후 고점 대비 -0.99% HOLD, -1.0% SELL_ALL (삼성SDI 기준)."""
        self.mgr.reconcile_from_broker(self._broker(), active_codes=set())
        pos = self.mgr.positions["006400"]
        pos.highest_price = 500000          # 활성가 훨씬 초과 → 활성
        pos.current_level = 4               # 피라미딩 추가매수 분기 배제
        d_hold = self._eval("006400", 495050)   # 고점대비 -0.99%
        self.assertEqual(d_hold["action"], "HOLD")
        d_sell = self._eval("006400", 495000)   # 고점대비 -1.0%
        self.assertEqual(d_sell["action"], "SELL_ALL")
        self.assertIn("트레일링", d_sell["reason"])

    def test_10_repeat_restore_highest_never_lowered(self):
        """[10] 복원 후 30초 잡 10회 반복 → highest 불변/상승만(현재가 하락에도 유지)."""
        self.mgr.reconcile_from_broker(self._broker(), active_codes=set())
        base_high = {c: self.mgr.positions[c].highest_price for c, *_ in SEVEN}
        # 이후 대사에서 현재가가 하락(평단 미변)해도 highest 하향 금지
        for i in range(10):
            drop = {c: {"qty": 10, "avg_price": a, "name": n,
                        "cur_price": max(1, p - 5000 - i * 100)}
                    for (c, n, a, p) in SEVEN}
            self.mgr.reconcile_from_broker(drop, active_codes=set())
            for c, *_ in SEVEN:
                self.assertGreaterEqual(self.mgr.positions[c].highest_price,
                                        base_high[c], f"{c} highest 하향됨")
        # 반대로 현재가 급등 시엔 트레일링 갱신은 evaluate 소관(reconcile 은 유지)
        for c, *_ in SEVEN:
            self.assertEqual(self.mgr.positions[c].highest_price, base_high[c])

    def test_I_restart_preserves_highest_and_recovered(self):
        """[I] 저장→재로드(재시작 모사) 후 highest·recovered 유지, 중복 없음."""
        self.mgr.reconcile_from_broker(self._broker(), active_codes=set())
        highs = {c: self.mgr.positions[c].highest_price for c, *_ in SEVEN}
        mgr2 = PyramidStrategyManager(None, max_per_stock=1e12, max_total=1e12)
        self.assertEqual(len(mgr2.positions), 7)
        for (c, *_rest) in SEVEN:
            self.assertTrue(mgr2.positions[c].recovered)
            self.assertEqual(mgr2.positions[c].highest_price, highs[c])
        rep = mgr2.reconcile_from_broker(self._broker(), active_codes=set())
        self.assertTrue(rep["unchanged"])           # 중복 복원 없음
        self.assertEqual(len(mgr2.positions), 7)


# ══════════════════════════════════════════════════════════════
# 독립 복원 잡 + _sync (D/E) — app.py 함수 AST 추출
# ══════════════════════════════════════════════════════════════
class _FakeStrategyMgr:
    def __init__(self, pyramid):
        self.pyramid = pyramid

    def active_order_codes(self, market):
        return set()


class IndependentRestoreJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p0-job-")
        self._py, self._cp = ps.PYRAMID_FILE, ps.COMPOUND_FILE
        ps.PYRAMID_FILE = os.path.join(self.tmp, "pyramid_positions.json")
        ps.COMPOUND_FILE = os.path.join(self.tmp, "compound_pool.json")
        self.pyr = PyramidStrategyManager(None, max_per_stock=1e12, max_total=1e12)

    def tearDown(self):
        ps.PYRAMID_FILE, ps.COMPOUND_FILE = self._py, self._cp
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _api(self, source, holdings):
        api = MagicMock()
        api.get_balance = MagicMock(
            return_value={"_source": source, "holdings": holdings})
        return api

    def _job_ns(self, api):
        health = {"last_started_at": None, "last_finished_at": None,
                  "last_result": None, "last_source": None,
                  "restored_codes": [], "position_count": 0, "run_count": 0}
        return _extract(
            ["_kr_position_restore_job", "_sync_positions_from_balance"],
            extra_globals={
                "_strategy_mgr": _FakeStrategyMgr(self.pyr),
                "_api": api,
                "_kr_restore_lock": threading.Lock(),
                "_kr_restore_health": health,
                "_log": lambda *a, **k: None,
            }), health

    def test_D_new_fill_auto_restored_next_reconcile(self):
        """[D] 장중 신규 체결 후 다음 대사에서 자동 복원(독립 잡)."""
        # 1차: 6종목만 보유
        api = self._api("api", _holdings(SEVEN[:6]))
        ns, health = self._job_ns(api)
        ns["_kr_position_restore_job"]()
        self.assertEqual(len(self.pyr.positions), 6)
        # 신규 체결로 7번째 종목이 잔고에 등장 → 다음 대사에서 자동 복원
        api.get_balance.return_value = {"_source": "api",
                                        "holdings": _holdings(SEVEN)}
        ns["_kr_position_restore_job"]()
        self.assertEqual(len(self.pyr.positions), 7)
        self.assertIn("373220", self.pyr.positions)
        self.assertEqual(health["last_result"], "ok")
        self.assertEqual(health["position_count"], 7)

    def test_E_non_api_source_never_deletes(self):
        """[E] 조회실패/EGW00215/캐시 응답으로 기존 원장 삭제·{} 저장 금지."""
        # 먼저 정상 복원(7종목)
        api = self._api("api", _holdings(SEVEN))
        ns, health = self._job_ns(api)
        ns["_kr_position_restore_job"]()
        self.assertEqual(len(self.pyr.positions), 7)
        # 이후 잔고 조회가 캐시/오류/빈응답으로 오면 삭제하지 않고 보존
        for src, hold in (("cache", []), ("error", []), ("psbl", []),
                          ("empty", [])):
            api.get_balance.return_value = {"_source": src, "holdings": hold}
            ns["_kr_position_restore_job"]()
            self.assertEqual(len(self.pyr.positions), 7,
                             f"_source={src} 인데 원장 삭제됨(P0 위반)")
            self.assertTrue(str(health["last_result"]).startswith("skip"))

    def test_job_nonreentrant_and_isolated(self):
        """복원 잡: 비재진입 + 예외 격리(다음 실행 정상)."""
        api = self._api("api", _holdings(SEVEN))
        ns, health = self._job_ns(api)
        # 먼저 정상 1회 → 7종목 복원
        ns["_kr_position_restore_job"]()
        self.assertEqual(len(self.pyr.positions), 7)
        # 락 선점(다른 _sync 진행 중 모사) → 직렬화 스킵(본체 미실행, 보존)
        ns["_kr_restore_lock"].acquire()
        _before = len(self.pyr.positions)
        ns["_kr_position_restore_job"]()
        self.assertIn("sync_in_progress", str(health["last_result"]))
        self.assertEqual(len(self.pyr.positions), _before)   # 보존(중복 대사 없음)
        ns["_kr_restore_lock"].release()
        # 예외 격리: get_balance 예외 → _sync 내부에서 보존, 잡은 예외 없이 종료
        api.get_balance.side_effect = RuntimeError("EGW00215 raw")
        ns["_kr_position_restore_job"]()
        self.assertFalse(ns["_kr_restore_lock"].locked())    # 해제됨
        self.assertEqual(len(self.pyr.positions), 7)          # 보존
        # health 에 예외 원문 미노출
        self.assertNotIn("EGW00215 raw", json.dumps(dict(health)))

    def test_9_concurrent_8x_restore_file_intact(self):
        """[9/10] 시작 _sync·30초 잡 동시 8회 호출해도 원자저장·락으로 파일 정상·7종목."""
        api = self._api("api", _holdings(SEVEN))
        ns, health = self._job_ns(api)
        errs = []

        def _run():
            try:
                ns["_kr_position_restore_job"]()
            except Exception as e:   # 어떤 스레드도 예외 전파 없어야
                errs.append(e)
        ts = [threading.Thread(target=_run) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(len(self.pyr.positions), 7)
        # 저장 파일이 손상 없이 7종목 유효 JSON
        with open(ps.PYRAMID_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(len(saved), 7)
        for c, *_ in SEVEN:
            self.assertIn(c, saved)
            self.assertTrue(saved[c]["recovered"])

    def test_10_ten_api_errors_keep_seven(self):
        """[10] 정상 복원 후 잔고 API 오류/EGW 10회 반복에도 7종목 유지."""
        api = self._api("api", _holdings(SEVEN))
        ns, health = self._job_ns(api)
        ns["_kr_position_restore_job"]()
        self.assertEqual(len(self.pyr.positions), 7)
        for i in range(10):
            api.get_balance.return_value = {"_source": "error", "holdings": []}
            ns["_kr_position_restore_job"]()
            self.assertEqual(len(self.pyr.positions), 7)

    def test_health_json_serializable_no_pii(self):
        """[#13] kr_restore_health 는 JSON 직렬화 가능 + 코드만(계좌·이름·원문 없음)."""
        api = self._api("api", _holdings(SEVEN))
        ns, health = self._job_ns(api)
        ns["_kr_position_restore_job"]()
        s = json.dumps(dict(health))            # 직렬화 가능
        self.assertIn("006400", s)              # 코드만 노출
        self.assertNotIn("삼성SDI", s)           # 종목명 미노출
        self.assertNotIn("12345678", s)         # 계좌 미노출


# ══════════════════════════════════════════════════════════════
# ODNO 견고 추출·라우팅·허위로그 방지 (G/H) + KIS 보유 BUY 차단 (F)
# ══════════════════════════════════════════════════════════════
class OdnoAndHeldBuyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p0-odno-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sm_stub(self):
        """_register_pending_order/_is_kr_held_at_broker 만 쓰는 경량 StrategyManager."""
        sm = object.__new__(StrategyManager)
        return sm

    def test_G_odno_field_extracted_not_empty(self):
        """[G 근본원인] 실주문 응답의 ODNO 필드를 견고 추출(기존 KNO_ORD_NO만→'' 버그)."""
        from journal.fill_observer import PendingOrderRegistry
        from phoenix.lifecycle import OrderLifecycleManager
        import journal.fill_observer as fo
        _orig = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "pending.db")
        try:
            if getattr(fo._local, "fo_conn", None) is not None:
                fo._local.fo_conn.close(); fo._local.fo_conn = None
            sm = self._sm_stub()
            sm._pending_registry = PendingOrderRegistry()
            sm._lifecycle_mgr = OrderLifecycleManager(os.path.join(self.tmp, "lc.db"))
            from phoenix.lifecycle import make_order_lifecycle_id
            lc_id = make_order_lifecycle_id("KR", "BUY", "006400")
            lc = sm._lifecycle_mgr.create(trade_id=lc_id, market="KR", code="006400",
                                          side="BUY", strategy_name="t", order_qty=10)
            sm._lifecycle_mgr.confirm_signal(lc); sm._lifecycle_mgr.submit(lc)
            # ★ 실제 KIS order-cash 성공 응답: output.ODNO (KNO_ORD_NO 아님)
            resp = {"rt_cd": "0", "output": {"ODNO": "0001234567"}}
            odno = sm._register_pending_order(
                market="KR", trade_id="cli-1", code="006400", side="BUY",
                order_qty=10, order_response=resp, lifecycle_id=lc_id)
            self.assertEqual(odno, "0001234567")   # ODNO 정상 추출(빈 문자열 아님)
            self.assertTrue(sm._pending_registry.has_active_order("KR", "006400", "BUY"))
        finally:
            if getattr(fo._local, "fo_conn", None) is not None:
                fo._local.fo_conn.close(); fo._local.fo_conn = None
            fo._JOURNAL_DB_PATH = _orig

    def test_H_empty_odno_no_success_log_no_register(self):
        """[H] ODNO 없음 → register 미호출 + '등록 완료' 로그 금지 + '' 반환."""
        sm = self._sm_stub()
        sm._pending_registry = MagicMock()
        sm._lifecycle_mgr = MagicMock()
        resp = {"rt_cd": "0", "output": {}}   # ODNO 없음
        with self.assertLogs("StrategyManager", level="INFO") as cm:
            logging.getLogger("StrategyManager").info("probe")  # 최소 1건 보장
            odno = sm._register_pending_order(
                market="KR", trade_id="c", code="006400", side="BUY",
                order_qty=10, order_response=resp, lifecycle_id="lc-x")
        self.assertEqual(odno, "")
        sm._pending_registry.register.assert_not_called()      # 등록 시도 없음
        sm._lifecycle_mgr.accept.assert_not_called()           # accept 없음
        joined = "\n".join(cm.output)
        self.assertNotIn("등록 완료", joined)                   # 허위 성공 로그 금지

    def test_G_kis_rtcd0_empty_odno_routes_unknown(self):
        """[G] kis_api: rt_cd=0 이나 ODNO 없음(BUY) → UNKNOWN 영속 + rt_cd='U'."""
        import api.kis_api as kmod
        from api.kis_api import KISApi
        from journal.unknown_order_ledger import UnknownOrderLedger

        api = object.__new__(KISApi)
        api.base_url = "https://mock"; api.account_no = "12345678-01"
        api._live_order_guard = lambda *a, **k: None
        api._pre_validate_kr_order = lambda *a, **k: None
        api._order_cooldown = {}; api._ORDER_COOLDOWN_SEC = 0.0
        api.tick_size = lambda p: 1
        api.round_to_tick = lambda p, direction=1: int(p)
        api.invalidate_balance_cache = lambda: None
        api._on_api_success = lambda: None
        api._on_api_error = lambda *a, **k: None
        api._diagnose_500 = lambda *a, **k: ""
        api._headers = lambda *a, **k: {}
        api._rate_limit = lambda *a, **k: None
        api._reject_if_nrcvb_insufficient = lambda *a, **k: None
        api._account_buy_lock = lambda *a, **k: __import__("contextlib").nullcontext()
        api._unknown_ledger = UnknownOrderLedger(os.path.join(self.tmp, "u.db"))

        class _R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):   # rt_cd=0 이나 ODNO 없음
                return {"rt_cd": "0", "output": {}, "msg1": "정상"}
        _orig = kmod.requests.post
        kmod.requests.post = lambda *a, **k: _R()
        try:
            r = api._order("006400", "BUY", 10, 474500, ord_dvsn="00")
        finally:
            kmod.requests.post = _orig
        self.assertEqual(r["rt_cd"], "U")                       # UNKNOWN 라우팅
        self.assertEqual(r["_status"], "ORDER_PENDING_CONFIRMATION")
        self.assertTrue(api._unknown_ledger.has_active(
            "12345678-01", "KR", "006400", "BUY"))              # 영속 차단

    def test_F_is_kr_held_at_broker(self):
        """[F/5] KIS 보유 판정 → 신규 BUY 차단 + 조회실패 시 fail-safe 차단."""
        sm = self._sm_stub()
        sm.api = MagicMock()
        # 실조회(api) + 보유 → True(차단)
        sm.api.get_balance = MagicMock(return_value={
            "_source": "api", "holdings": _holdings(SEVEN)})
        self.assertTrue(sm._is_kr_held_at_broker("006400"))
        self.assertFalse(sm._is_kr_held_at_broker("999999"))   # 데이터 有·미보유→허용
        # 캐시 스냅샷(직전성공) 사용: 보유 → True, 미보유 → False
        sm.api.get_balance.return_value = {"_source": "cache",
                                           "holdings": _holdings(SEVEN)}
        self.assertTrue(sm._is_kr_held_at_broker("006400"))
        self.assertFalse(sm._is_kr_held_at_broker("999999"))
        # 보유데이터 불명(error/empty) → 중복매수 방지 fail-safe 차단(True)
        for src in ("error", "psbl", "empty"):
            sm.api.get_balance.return_value = {"_source": src, "holdings": []}
            self.assertTrue(sm._is_kr_held_at_broker("006400"),
                            f"_source={src} 조회실패 fail-safe 차단 아님")
        # 조회 예외 → fail-safe 차단
        sm.api.get_balance.side_effect = RuntimeError("EGW00215")
        self.assertTrue(sm._is_kr_held_at_broker("006400"))

    def _mk_kr_order_api(self, ledger_db):
        import api.kis_api as kmod
        from api.kis_api import KISApi
        from journal.unknown_order_ledger import UnknownOrderLedger
        api = object.__new__(KISApi)
        api.base_url = "https://mock"; api.account_no = "12345678-01"
        api._live_order_guard = lambda *a, **k: None
        api._pre_validate_kr_order = lambda *a, **k: None
        api._order_cooldown = {}; api._ORDER_COOLDOWN_SEC = 0.0
        api.tick_size = lambda p: 1
        api.round_to_tick = lambda p, direction=1: int(p)
        api.invalidate_balance_cache = lambda: None
        api._on_api_success = lambda: None
        api._on_api_error = lambda *a, **k: None
        api._diagnose_500 = lambda *a, **k: ""
        api._headers = lambda *a, **k: {}
        api._rate_limit = lambda *a, **k: None
        api._reject_if_nrcvb_insufficient = lambda *a, **k: None
        api._account_buy_lock = lambda *a, **k: __import__("contextlib").nullcontext()
        api._unknown_ledger = UnknownOrderLedger(ledger_db)
        return kmod, api

    def test_7_sell_rtcd0_empty_odno_nonblocking_confirm(self):
        """[7] SELL rt_cd=0·ODNO 없음 → 비차단 확인대기 영속 + 후속 SELL 미차단."""
        kmod, api = self._mk_kr_order_api(os.path.join(self.tmp, "s.db"))

        class _R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"rt_cd": "0", "output": {}, "msg1": "정상"}   # ODNO 없음
        _orig = kmod.requests.post
        kmod.requests.post = lambda *a, **k: _R()
        try:
            r = api._order("006400", "SELL", 10, 486500, ord_dvsn="00")
        finally:
            kmod.requests.post = _orig
        # 접수(rt_cd=0) 유지, 단순 성공/실패 아님 — 확인대기 표식
        self.assertEqual(r["rt_cd"], "0")
        self.assertEqual(r["_status"], "SELL_ACCEPTED_UNCONFIRMED")
        # 비차단: BUY·SELL 어느 쪽도 차단하지 않음(has_active=False)
        self.assertFalse(api._unknown_ledger.has_active(
            "12345678-01", "KR", "006400", "SELL"))
        self.assertFalse(api._unknown_ledger.has_active(
            "12345678-01", "KR", "006400", "BUY"))
        # 그러나 '확인대기' 로 영속(단순 OK 로 버리지 않음) — 정합화 대상
        recon = api._unknown_ledger.list_reconcilable()
        self.assertEqual(len(recon), 1)
        self.assertEqual(recon[0]["side"], "SELL")
        self.assertEqual(recon[0]["status"], "PENDING_CONFIRM")

    def test_7_sell_confirm_reconciled_connects_odno(self):
        """[7] 비차단 SELL 확인대기 → 당일주문조회에서 발견 시 ODNO 연결·해소(부킹 없음)."""
        from journal.unknown_order_ledger import UnknownOrderLedger
        from journal.unknown_order_reconciler import reconcile_unknown_orders
        led = UnknownOrderLedger(os.path.join(self.tmp, "sc.db"))
        rid = led.record("12345678-01", "KR", "006400", "SELL", 10, 486500, "00",
                         created_at="t", created_hhmmss="100000", blocking=False)
        booked = []
        # 당일주문조회에서 동일조건 SELL 발견(미체결)
        prov = lambda row: {"query_ok": True, "candidates": [
            {"odno": "S123", "qty": 10, "price": 486500, "cum_filled_qty": 0}]}
        res = reconcile_unknown_orders(
            led, prov, on_promote=lambda r, c: booked.append(c) or True,
            on_fill=lambda r, c: booked.append(c) or True, now_iso="t2")
        self.assertEqual(res, [(rid, "SELL_CONFIRMED")])
        self.assertEqual(led.get(rid)["odno"], "S123")     # ODNO 연결
        self.assertEqual(booked, [])                        # 부킹(promote/fill) 없음
        # 확인대기가 아니게 됨(재점검 목록에서 빠짐)
        self.assertEqual(led.list_reconcilable(), [])

    def test_7_sell_confirm_keeps_when_not_found(self):
        """[7] SELL 확인대기: 당일주문 미발견이면 비차단 유지(계속 확인대기)."""
        from journal.unknown_order_ledger import UnknownOrderLedger
        from journal.unknown_order_reconciler import reconcile_unknown_orders
        led = UnknownOrderLedger(os.path.join(self.tmp, "sk.db"))
        rid = led.record("12345678-01", "KR", "006400", "SELL", 10, 486500, "00",
                         created_at="t", created_hhmmss="100000", blocking=False)
        prov = lambda row: {"query_ok": True, "candidates": []}   # 0건
        res = reconcile_unknown_orders(led, prov, now_iso="t2")
        self.assertEqual(res, [(rid, "SELL_CONFIRM_KEEP")])
        # 여전히 비차단 확인대기(신규 SELL 미차단)
        self.assertFalse(led.has_active("12345678-01", "KR", "006400", "SELL"))
        self.assertEqual(led.get(rid)["status"], "PENDING_CONFIRM")


if __name__ == "__main__":
    unittest.main()
