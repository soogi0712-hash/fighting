"""UNKNOWN 주문 영속 원장 + 재시작 후에도 유지되는 BUY 차단.

- ambiguous(타임아웃/500) POST → 즉시 영속 원장 기록 + 같은 종목 BUY 재제출 0회
- 다른 KISApi 인스턴스·재시작 모사(동일 DB)에서도 재제출 0회
- SELL 은 UNKNOWN 이어도 즉시 제출
- UNKNOWN/차단 반환은 rt_cd=0 아님(BUY 로 집계되지 않음)
"""
import os
import sys
import tempfile
import shutil
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import api.kis_api as kmod                              # noqa: E402
from api.kis_api import KISApi                          # noqa: E402
from journal.unknown_order_ledger import UnknownOrderLedger  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = str(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _mk_api(db_path):
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


class TestUnknownBlocking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "j.db")
        self._post = kmod.requests.post
        self.posts = []

    def tearDown(self):
        kmod.requests.post = self._post
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_post(self, behavior):
        def _p(*a, **k):
            self.posts.append(1)
            if isinstance(behavior, Exception):
                raise behavior
            return behavior
        kmod.requests.post = _p

    def test_timeout_records_and_blocks_next_loop(self):
        """타임아웃 → 원장 기록 + 같은 프로세스 다음 루프 재제출 0회."""
        api = _mk_api(self.db)
        self._set_post(ConnectionError("timeout"))
        r1 = api._order("005930", "BUY", 1, 70000, ord_dvsn="00")
        self.assertEqual(r1["rt_cd"], "U")
        self.assertEqual(r1["_status"], "ORDER_PENDING_CONFIRMATION")
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(api._unknown_ledger.has_active("12345678-01", "KR", "005930", "BUY"))
        # 다음 루프: 같은 종목 BUY → 차단, POST 재호출 0회
        r2 = api._order("005930", "BUY", 1, 70000, ord_dvsn="00")
        self.assertEqual(r2["_status"], "BUY_BLOCKED_UNKNOWN")
        self.assertNotEqual(r2["rt_cd"], "0")           # BUY 로 집계 안 됨
        self.assertEqual(len(self.posts), 1)            # 재제출 없음

    def test_http500_blocks_other_instance(self):
        """HTTP 500 후 다른 KISApi 인스턴스(동일 DB)에서도 재제출 0회."""
        api1 = _mk_api(self.db)
        self._set_post(_Resp({}, status=500))
        api1._order("005930", "BUY", 1, 70000, ord_dvsn="00")
        self.assertEqual(len(self.posts), 1)
        # 다른 인스턴스(동일 계좌·DB)
        api2 = _mk_api(self.db)
        r = api2._order("005930", "BUY", 1, 70000, ord_dvsn="00")
        self.assertEqual(r["_status"], "BUY_BLOCKED_UNKNOWN")
        self.assertEqual(len(self.posts), 1)

    def test_restart_persists_block(self):
        """프로세스 재시작 모사(새 원장 인스턴스, 동일 DB)에서도 재제출 0회."""
        api = _mk_api(self.db)
        self._set_post(ConnectionError("timeout"))
        api._order("005930", "BUY", 1, 70000, ord_dvsn="00")
        self.assertEqual(len(self.posts), 1)
        # 재시작: 새 원장 + 새 api 인스턴스, 동일 DB
        api_restart = _mk_api(self.db)
        r = api_restart._order("005930", "BUY", 1, 70000, ord_dvsn="00")
        self.assertEqual(r["_status"], "BUY_BLOCKED_UNKNOWN")
        self.assertEqual(len(self.posts), 1)

    def test_sell_not_blocked_by_unknown(self):
        """UNKNOWN(BUY) 미해소 상태여도 SELL 은 즉시 제출."""
        api = _mk_api(self.db)
        # 먼저 UNKNOWN 기록
        api._unknown_ledger.record("12345678-01", "KR", "005930", "BUY",
                                   1, 70000, "00", created_at="2026-08-07T10:00:00")
        self._set_post(_Resp({"rt_cd": "0", "output": {"ODNO": "1"}}))
        r = api._order("005930", "SELL", 10, 80000, ord_dvsn="00")
        self.assertEqual(r["rt_cd"], "0")               # SELL 제출됨
        self.assertEqual(len(self.posts), 1)

    def test_other_symbol_not_blocked(self):
        """UNKNOWN 은 해당 종목만 차단, 다른 종목 BUY 는 정상."""
        api = _mk_api(self.db)
        api._unknown_ledger.record("12345678-01", "KR", "005930", "BUY",
                                   1, 70000, "00", created_at="2026-08-07T10:00:00")
        self._set_post(_Resp({"rt_cd": "0", "output": {"ODNO": "2"}}))
        r = api._order("000660", "BUY", 1, 90000, ord_dvsn="00")  # 다른 종목
        self.assertEqual(r["rt_cd"], "0")
        self.assertEqual(len(self.posts), 1)

    def test_toctou_recheck_inside_lock(self):
        """외부 검사 통과 후 락 획득 시점에 UNKNOWN 이 생겼으면 락 내 재확인이 차단."""
        api = _mk_api(self.db)
        self._set_post(_Resp({"rt_cd": "0", "output": {"ODNO": "1"}}))
        calls = {"n": 0}

        class _FlipLedger:
            def has_active(self, *a):
                calls["n"] += 1
                return calls["n"] >= 2   # 1차(외부) False, 2차(락내) True
        api._unknown_ledger = _FlipLedger()
        r = api._order("005930", "BUY", 1, 70000, ord_dvsn="00")
        self.assertEqual(r["_status"], "BUY_BLOCKED_UNKNOWN")
        self.assertEqual(len(self.posts), 0)          # 제출되지 않음
        self.assertGreaterEqual(calls["n"], 2)        # 락 내 재확인 수행됨

    def test_resolved_not_accepted_unblocks(self):
        """RESOLVED_NOT_ACCEPTED 로 해소되면 차단 해제(재주문 가능)."""
        api = _mk_api(self.db)
        rid = api._unknown_ledger.record("12345678-01", "KR", "005930", "BUY",
                                         1, 70000, "00", created_at="t")
        self.assertTrue(api._unknown_ledger.has_active("12345678-01", "KR", "005930", "BUY"))
        api._unknown_ledger.resolve(rid, "RESOLVED_NOT_ACCEPTED", note="미접수 확인", ts="t2")
        self.assertFalse(api._unknown_ledger.has_active("12345678-01", "KR", "005930", "BUY"))


if __name__ == "__main__":
    unittest.main()
