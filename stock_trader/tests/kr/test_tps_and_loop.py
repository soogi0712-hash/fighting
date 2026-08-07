"""TPS 공용 rate limiter(초당 15건·스레드안전·선택적 backoff) + 잔고캐시 중복차단
+ 국내 루프 중복실행 방지 배선 검증.

items 11~17.
"""
import os
import re
import sys
import time
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import api.kis_api as kmod                              # noqa: E402
from api.kis_api import KISApi                          # noqa: E402


def _rate_api(min_interval=0.0, max_per_sec=15):
    api = object.__new__(KISApi)
    api._last_api_call_ts = 0.0
    api._API_MIN_INTERVAL = min_interval
    api._RATE_MAX_PER_SEC = max_per_sec
    api._call_times = []
    api._rate_lock = threading.Lock()
    api._backoff_until = 0.0
    api._consecutive_errors = 0
    return api


class TestSharedRateLimiter(unittest.TestCase):
    def test_caps_15_per_sec_threadsafe(self):
        """여러 스레드가 동시에 호출해도 어떤 1초 창에도 15건을 넘지 않는다."""
        api = _rate_api(min_interval=0.0, max_per_sec=15)
        stamps = []
        slock = threading.Lock()

        def worker():
            for _ in range(10):
                api._rate_limit()
                with slock:
                    stamps.append(time.time())
        threads = [threading.Thread(target=worker) for _ in range(8)]  # 80건
        [t.start() for t in threads]
        [t.join() for t in threads]
        stamps.sort()
        for i in range(len(stamps)):
            j = i
            while j < len(stamps) and stamps[j] - stamps[i] < 1.0:
                j += 1
            self.assertLessEqual(j - i, 15, "1초 창에 15건 초과")

    def test_min_interval_enforced(self):
        api = _rate_api(min_interval=0.05, max_per_sec=15)
        t0 = time.time()
        for _ in range(5):
            api._rate_limit()
        self.assertGreaterEqual(time.time() - t0, 0.05 * 4 * 0.9)

    def test_critical_bypasses_backoff(self):
        """backoff 중이어도 critical(주문·매도·체결감시)은 대기하지 않는다(item15)."""
        api = _rate_api(min_interval=0.0)
        api._backoff_until = time.time() + 100.0   # 긴 backoff
        t0 = time.time()
        api._rate_limit(critical=True)
        self.assertLess(time.time() - t0, 0.5)      # 즉시 통과

    def test_non_critical_waits_backoff(self):
        api = _rate_api(min_interval=0.0)
        api._backoff_until = time.time() + 0.5
        t0 = time.time()
        api._rate_limit(critical=False)
        self.assertGreaterEqual(time.time() - t0, 0.4)  # backoff 대기


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


_OK = {"rt_cd": "0", "output1": [],
       "output2": [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "1000000"}]}


class TestBalanceCacheOperational(unittest.TestCase):
    """item16: 한 스캔에서 잔고조회를 여러 번 불러도 실제 HTTP는 1회."""

    def setUp(self):
        self._g = kmod.requests.get

    def tearDown(self):
        kmod.requests.get = self._g

    def test_scan_pattern_single_http(self):
        api = object.__new__(KISApi)
        api.base_url = "https://mock"
        api.account_no = "12345678-01"
        api._headers = lambda *a, **k: {}
        api._rate_limit = lambda *a, **k: None
        api._on_api_success = lambda: None
        api._on_api_error = lambda *a, **k: None
        api._diagnose_500 = lambda *a, **k: ""
        api._balance_cache = None
        api._balance_cache_ts = 0.0
        api._get_cash_from_psbl_api = lambda: -1
        calls = []
        kmod.requests.get = lambda *a, **k: (calls.append(1), _Resp(_OK))[1]
        # 30종목 스캔이 각자 잔고를 참조해도 TTL 캐시로 1회만
        for _ in range(30):
            api.get_balance()
        self.assertEqual(len(calls), 1)


class TestLoopGuardWiring(unittest.TestCase):
    """items 10/11/12: 국내 루프 중복실행 방지·단일 스케줄러·리로더 없음(정적 검증).
    app.py 는 flask 의존이라 import 대신 소스 배선을 검증한다."""

    def setUp(self):
        p = os.path.join(os.path.dirname(__file__), "..", "..", "app.py")
        self.src = open(p, encoding="utf-8").read()

    def test_trading_loop_reentrancy_lock(self):
        self.assertIn("_trading_loop_lock = threading.Lock()", self.src)
        self.assertIn("_trading_loop_lock.acquire(blocking=False)", self.src)
        self.assertIn("def _trading_loop_impl(", self.src)

    def test_scheduler_jobs_replace_existing(self):
        # trading_loop 잡은 id + replace_existing 으로 중복 등록 방지
        self.assertRegex(
            self.src,
            r'id="trading_loop",\s*replace_existing=True')

    def test_no_flask_reloader(self):
        # debug=False & use_reloader=True 부재 → 리로더 이중 프로세스 없음
        self.assertIn("debug=False", self.src)
        self.assertNotIn("use_reloader=True", self.src)


if __name__ == "__main__":
    unittest.main()
