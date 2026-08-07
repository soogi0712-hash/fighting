"""국내 잔고조회(TTTC8434R) 캐시 + EGW00215 백오프, 주문가능조회 미수수량 무시.

req10: 한 스캔 사이클에서 1회만 조회(짧은 TTL 캐시), EGW00215(초당 조회건수
초과) 발생 시 즉시 반복 호출 금지 + 지수 백오프.
req7 : get_kr_available_amounts 는 미수 포함 수량(max_buy_qty)을 무시하고
       현금 미수없는 수량(nrcvb_buy_qty)만 사용.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import api.kis_api as kmod                      # noqa: E402
from api.kis_api import KISApi                  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _mk_api():
    api = object.__new__(KISApi)
    api.base_url = "https://mock"
    api.account_no = "12345678-01"
    api._headers = lambda *a, **k: {}
    api._rate_limit = lambda: None
    api._on_api_success = lambda: None
    api._on_api_error = lambda *a, **k: None
    api._diagnose_500 = lambda *a, **k: ""
    api._balance_cache = None
    api._balance_cache_ts = 0.0
    api._get_cash_from_psbl_api = lambda: -1
    return api


_OK = {"rt_cd": "0", "output1": [],
       "output2": [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "1000000"}]}
_EGW = {"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "초당 거래건수를 초과하였습니다"}


class TestBalanceCacheAndBackoff(unittest.TestCase):
    def setUp(self):
        self._orig_get = kmod.requests.get

    def tearDown(self):
        kmod.requests.get = self._orig_get

    def test_balance_queried_once_per_scan(self):
        """짧은 TTL 캐시 → 같은 스캔의 반복 호출은 실제 조회 1회만."""
        api = _mk_api()
        calls = []
        kmod.requests.get = lambda *a, **k: (calls.append(1), _Resp(_OK))[1]
        r1 = api.get_balance()
        r2 = api.get_balance()
        r3 = api.get_balance()
        self.assertEqual(len(calls), 1)              # 1회만 실제 조회
        self.assertEqual(r1["cash"], 1000000)
        self.assertEqual(r2["cash"], r3["cash"])

    def test_egw00215_serves_cache_and_backs_off(self):
        """EGW00215 → 캐시 반환 + 백오프 설정, 백오프 중 실제 조회 없음."""
        api = _mk_api()
        seq = [_OK, _EGW]
        calls = []

        def _get(*a, **k):
            calls.append(1)
            return _Resp(seq.pop(0) if seq else _EGW)
        kmod.requests.get = _get

        api.get_balance()                 # 1) 성공 → 캐시 저장
        self.assertEqual(len(calls), 1)
        api._balance_short_ts = 0.0        # TTL 만료 강제 → 다음 호출은 실제 조회 시도

        r2 = api.get_balance()             # 2) EGW00215 → 캐시 반환 + 백오프
        self.assertEqual(len(calls), 2)
        self.assertEqual(r2["cash"], 1000000)          # 캐시값
        self.assertGreater(api._balance_backoff_until, 0.0)

        r3 = api.get_balance()             # 3) 백오프 중 → 실제 조회 없이 캐시
        self.assertEqual(len(calls), 2)                # 추가 조회 없음(반복 호출 금지)
        self.assertEqual(r3["cash"], 1000000)

    def test_backoff_is_exponential(self):
        """연속 EGW00215 → 백오프가 지수적으로 증가."""
        api = _mk_api()
        kmod.requests.get = lambda *a, **k: _Resp(_EGW)
        import time as _t
        api._balance_short_cache = {"cash": 1, "_source": "cache"}
        api._balance_short_ts = 0.0
        api.get_balance()
        b1 = api._balance_backoff_until - _t.time()
        api._balance_backoff_until = 0.0
        api._balance_short_ts = 0.0
        api.get_balance()
        b2 = api._balance_backoff_until - _t.time()
        self.assertGreater(b2, b1)                     # 2회차 백오프 > 1회차


class TestKRAvailableIgnoresMishu(unittest.TestCase):
    def setUp(self):
        self._orig_get = kmod.requests.get

    def tearDown(self):
        kmod.requests.get = self._orig_get

    def test_uses_nrcvb_not_max_buy_qty(self):
        """현금 미수없는 수량(nrcvb_buy_qty)만 사용, 미수 포함(max_buy_qty)은 무시."""
        api = _mk_api()
        payload = {"rt_cd": "0", "output": {
            "ord_psbl_cash": "1000000",
            "nrcvb_buy_qty": "5",       # 현금(미수 없는) 수량
            "max_buy_qty":   "99",      # 미수 포함 — 절대 사용 금지
            "nrcvb_buy_amt": "1000000",
        }}
        kmod.requests.get = lambda *a, **k: _Resp(payload)
        r = api.get_kr_available_amounts("005930", 10000, "00")
        self.assertTrue(r["ok"])
        self.assertEqual(r["qty"], 5)                  # nrcvb_buy_qty, NOT 99
        self.assertEqual(r["amount"], 1000000.0)

    def test_rt_cd_error_blocks(self):
        api = _mk_api()
        kmod.requests.get = lambda *a, **k: _Resp(
            {"rt_cd": "1", "msg1": "조회 오류"})
        r = api.get_kr_available_amounts("005930", 10000, "00")
        self.assertFalse(r["ok"])
        self.assertEqual(r["qty"], 0)


if __name__ == "__main__":
    unittest.main()
