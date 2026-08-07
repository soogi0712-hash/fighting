"""_order/_submit_kr_order_cash 실계좌 안전성:
- 시장가(ORD_UNPR=0) BUY 미허용(과대수량 방지)
- 접수 불명확(타임아웃/500/파싱실패) → 재제출 0회(UNKNOWN)
- rt_cd=0 또는 ODNO 존재 → 재제출 0회
- 명확한 잔액부족 거절(주문번호 없음)만 재조회 후 축소 1회 재제출
- 동일 수량 반복/무한 재시도 금지
- 계좌별 공유 락으로 복수 인스턴스에서도 동일계좌 BUY 직렬화
- SELL 은 BUY 락을 기다리지 않고 즉시 제출
"""
import os
import sys
import time
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import api.kis_api as kmod                              # noqa: E402
from api.kis_api import KISApi, _account_buy_lock       # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = str(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _mk_submit_api():
    api = object.__new__(KISApi)
    api.base_url = "https://mock"
    api.account_no = "12345678-01"
    api._headers = lambda *a, **k: {}
    api._rate_limit = lambda *a, **k: None
    api._diagnose_500 = lambda *a, **k: ""
    api._on_api_error = lambda *a, **k: None
    api._on_api_success = lambda: None
    api.invalidate_balance_cache = lambda: None
    api._order_cooldown = {}
    api.get_kr_available_amounts = MagicMock()
    return api


def _submit(api, qty, order_type="BUY", price=70000):
    return api._submit_kr_order_cash(
        "005930", order_type, qty, price, "00", "TTTC0012U", "12345678", "01", price)


class TestSubmitRetrySafety(unittest.TestCase):
    def setUp(self):
        self._post = kmod.requests.post
        self.posts = []

    def tearDown(self):
        kmod.requests.post = self._post

    def _mock_post(self, seq):
        it = iter(seq)

        def _p(*a, **k):
            body = k.get("json", {})
            self.posts.append(body.get("ORD_QTY"))
            nxt = next(it)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        kmod.requests.post = _p

    def test_success_no_resubmit(self):
        self._mock_post([_Resp({"rt_cd": "0", "output": {"ODNO": "111"}})])
        api = _mk_submit_api()
        r = _submit(api, 1)
        self.assertEqual(r["rt_cd"], "0")
        self.assertEqual(len(self.posts), 1)

    def test_reject_with_odno_no_resubmit(self):
        """rt_cd!=0 이나 ODNO 존재 → 접수 정황 → 재제출 0회."""
        self._mock_post([_Resp({"rt_cd": "1", "msg1": "금액 부족",
                                "output": {"ODNO": "999"}})])
        api = _mk_submit_api()
        _submit(api, 5)
        self.assertEqual(len(self.posts), 1)
        api.get_kr_available_amounts.assert_not_called()

    def test_timeout_no_resubmit(self):
        """네트워크/타임아웃 → 재제출 0회, UNKNOWN 종료."""
        self._mock_post([ConnectionError("timeout")])
        api = _mk_submit_api()
        r = _submit(api, 5)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(r["rt_cd"], "U")
        self.assertEqual(r["_status"], "ORDER_PENDING_CONFIRMATION")

    def test_http500_no_resubmit(self):
        """HTTP 500(접수 불명확) → 재제출 0회, UNKNOWN."""
        self._mock_post([_Resp({}, status=500)])
        api = _mk_submit_api()
        r = _submit(api, 5)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(r["rt_cd"], "U")

    def test_clear_reject_resize_once(self):
        """주문번호 없는 잔액부족 거절 → 재조회 후 축소 1회만 재제출."""
        self._mock_post([
            _Resp({"rt_cd": "1", "msg1": "주문가능금액 부족"}),       # REJECTED
            _Resp({"rt_cd": "0", "output": {"ODNO": "222"}}),        # 축소 후 성공
        ])
        api = _mk_submit_api()
        # 재조회: nrcvb_qty=2, nrcvb_amt=140000 (price 70000 → 2주)
        api.get_kr_available_amounts.return_value = {
            "ok": True, "amount": 140_000.0, "qty": 2, "ord_psbl_cash": 6098.0}
        r = _submit(api, 5)
        self.assertEqual(r["rt_cd"], "0")
        self.assertEqual(self.posts, ["5", "2"])   # 5주 실패 → 2주 재제출(축소)

    def test_clear_reject_no_reduction_no_resubmit(self):
        """재조회해도 수량이 안 줄면 재제출 금지(동일수량 반복 금지)."""
        self._mock_post([
            _Resp({"rt_cd": "1", "msg1": "주문가능금액 부족"}),
            _Resp({"rt_cd": "0", "output": {"ODNO": "333"}}),
        ])
        api = _mk_submit_api()
        api.get_kr_available_amounts.return_value = {
            "ok": True, "amount": 9_999_999.0, "qty": 99, "ord_psbl_cash": 6098.0}
        r = _submit(api, 5)
        self.assertEqual(self.posts, ["5"])        # 재제출 없음
        self.assertEqual(r.get("rt_cd"), "1")

    def test_non_balance_reject_no_resubmit(self):
        """잔액류 아닌 거절(예: 장운영시간 오류)은 재조회·재제출 없음."""
        self._mock_post([_Resp({"rt_cd": "1", "msg1": "장운영시간이 아닙니다"})])
        api = _mk_submit_api()
        _submit(api, 5)
        self.assertEqual(self.posts, ["5"])
        api.get_kr_available_amounts.assert_not_called()

    def test_sell_reject_no_cash_requery(self):
        """SELL 거절은 현금 재조회·축소 없이 그대로 반환."""
        self._mock_post([_Resp({"rt_cd": "1", "msg1": "수량 부족"})])
        api = _mk_submit_api()
        _submit(api, 10, order_type="SELL", price=80000)
        self.assertEqual(self.posts, ["10"])
        api.get_kr_available_amounts.assert_not_called()


class TestMarketBuyRejected(unittest.TestCase):
    def test_market_price0_buy_blocked(self):
        """시장가/0원 BUY 는 최종검증에서 차단(과대수량 방지)."""
        api = object.__new__(KISApi)
        api.get_kr_available_amounts = MagicMock()
        blk = api._reject_if_nrcvb_insufficient("005930", 10, 0, "01")
        self.assertIsNotNone(blk)
        self.assertEqual(blk["rt_cd"], "9")
        self.assertIn("시장가", blk["msg1"])
        api.get_kr_available_amounts.assert_not_called()   # 조회 자체를 안 함


def _mk_order_api(account="12345678-01", submit_impl=None):
    api = object.__new__(KISApi)
    api.base_url = "https://mock"
    api.account_no = account
    api._live_order_guard = lambda *a, **k: None
    api._pre_validate_kr_order = lambda *a, **k: None
    api._order_cooldown = {}
    api._ORDER_COOLDOWN_SEC = 0.0
    api.tick_size = lambda p: 1
    api.round_to_tick = lambda p, direction=1: int(p)
    api.invalidate_balance_cache = lambda: None
    api._on_api_success = lambda: None
    api.get_kr_available_amounts = MagicMock(return_value={
        "ok": True, "amount": 10_000_000.0, "qty": 100, "ord_psbl_cash": 6098.0})
    api._submit_kr_order_cash = submit_impl or (
        lambda *a, **k: {"rt_cd": "0"})
    return api


class TestCrossInstanceSerialization(unittest.TestCase):
    def test_two_instances_same_account_serialized(self):
        """복수 KISApi 인스턴스여도 동일 계좌 BUY 는 직렬화(공유 락)."""
        active = {"n": 0, "max": 0}
        alk = threading.Lock()

        def _slow(*a, **k):
            with alk:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(0.15)
            with alk:
                active["n"] -= 1
            return {"rt_cd": "0"}

        api1 = _mk_order_api("SAMEACC-01", submit_impl=_slow)
        api2 = _mk_order_api("SAMEACC-01", submit_impl=_slow)
        # 같은 계좌 → 같은 공유 락 객체
        self.assertIs(_account_buy_lock("SAMEACC-01"),
                      _account_buy_lock("SAMEACC-01"))
        t1 = threading.Thread(target=lambda: api1._order("005930", "BUY", 1, 70000, ord_dvsn="00"))
        t2 = threading.Thread(target=lambda: api2._order("005930", "BUY", 1, 70000, ord_dvsn="00"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(active["max"], 1)   # 동시 제출 없음

    def test_sell_not_blocked_by_buy_lock(self):
        """BUY 락 점유 중에도 SELL 은 즉시 제출된다."""
        lk = _account_buy_lock("SELLACC-01")
        submitted = []
        api = _mk_order_api("SELLACC-01",
                            submit_impl=lambda *a, **k: (submitted.append(a[1]), {"rt_cd": "0"})[1])
        lk.acquire()
        try:
            done = threading.Event()

            def _sell():
                api._order("005930", "SELL", 10, 80000, ord_dvsn="00")
                done.set()
            threading.Thread(target=_sell).start()
            # BUY 락을 쥔 상태에서도 SELL 은 0.5초 내 완료
            self.assertTrue(done.wait(0.5))
            self.assertEqual(submitted[-1], "SELL")
        finally:
            lk.release()


if __name__ == "__main__":
    unittest.main()
