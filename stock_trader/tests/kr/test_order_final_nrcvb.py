"""_order() 최종검증: ord_psbl_cash 기반 [현금초과 차단] 제거 → nrcvb 최종검증.

- BUY 제출 직전 실제 제출가격으로 get_kr_available_amounts 재조회.
- qty<=nrcvb_buy_qty AND qty*order_price<=nrcvb_buy_amt 만 통과. max_buy_*/ord_psbl_cash 미사용.
- SELL 은 현금검증 없이 제출. 전략비중·0.98 재적용 없음. 동일계좌 BUY 직렬화.
"""
import os
import sys
import time
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from api.kis_api import KISApi                          # noqa: E402


def _mk_order_api(nrcvb_seq, submit_impl=None):
    api = object.__new__(KISApi)
    api.base_url = "https://mock"
    api.account_no = "12345678-01"
    api._live_order_guard = lambda *a, **k: None
    api._pre_validate_kr_order = lambda *a, **k: None
    api._order_cooldown = {}
    api._ORDER_COOLDOWN_SEC = 0.0
    api._kr_buy_lock = threading.Lock()
    api.tick_size = lambda p: 1
    api.round_to_tick = lambda p, direction=1: int(p)
    api.invalidate_balance_cache = lambda: None
    api._on_api_success = lambda: None
    _it = iter(nrcvb_seq)
    api.get_kr_available_amounts = MagicMock(side_effect=lambda *a, **k: next(_it))
    api._submitted = []

    def _default_submit(stock_code, order_type, qty, order_price, *a, **k):
        api._submitted.append({"code": stock_code, "side": order_type,
                               "qty": qty, "price": order_price})
        return {"rt_cd": "0", "_submitted_qty": qty}
    api._submit_kr_order_cash = submit_impl or _default_submit
    return api


def _avail(amount, qty, ord_psbl_cash=6098):
    return {"ok": True, "amount": float(amount), "qty": int(qty),
            "ord_psbl_cash": float(ord_psbl_cash)}


class TestOrderFinalNrcvb(unittest.TestCase):

    def test_samsungbio_1share_submits(self):
        """ord_psbl_cash=6,098이지만 nrcvb_amt=2,674,871·qty=1 → 1주 제출."""
        api = _mk_order_api([_avail(2_674_871, 1)])
        r = api._order("207940", "BUY", 1, 1_509_000, ord_dvsn="00")
        self.assertEqual(r["rt_cd"], "0")
        self.assertEqual(api._submitted[-1]["qty"], 1)   # 1주 제출됨

    def test_sk_innovation_6shares(self):
        """SK이노베이션 6주 제출 가능."""
        api = _mk_order_api([_avail(2_000_000, 20)])
        api._order("096770", "BUY", 6, 110_000, ord_dvsn="00")
        self.assertEqual(api._submitted[-1]["qty"], 6)

    def test_hmm_37shares(self):
        """HMM 37주 제출 가능."""
        api = _mk_order_api([_avail(1_000_000, 50)])
        api._order("011200", "BUY", 37, 20_000, ord_dvsn="00")
        self.assertEqual(api._submitted[-1]["qty"], 37)

    def test_qty_exceeds_nrcvb_qty_blocked(self):
        """요청수량 > nrcvb_buy_qty → 차단, 제출 안 됨."""
        api = _mk_order_api([_avail(9_999_999, 1)])
        r = api._order("207940", "BUY", 2, 1_509_000, ord_dvsn="00")
        self.assertEqual(r["rt_cd"], "9")
        self.assertIn("수량초과", r["msg1"])
        self.assertEqual(api._submitted, [])

    def test_amount_exceeds_nrcvb_amt_blocked(self):
        """요청금액 > nrcvb_buy_amt → 차단."""
        api = _mk_order_api([_avail(1_509_000, 5)])   # 금액 1주치, 수량은 5 허용
        r = api._order("207940", "BUY", 2, 1_509_000, ord_dvsn="00")
        self.assertEqual(r["rt_cd"], "9")
        self.assertIn("금액초과", r["msg1"])
        self.assertEqual(api._submitted, [])

    def test_lookup_failure_no_submit(self):
        """최종 조회 실패 → 미제출."""
        api = _mk_order_api([{"ok": False}])
        r = api._order("207940", "BUY", 1, 1_509_000, ord_dvsn="00")
        self.assertEqual(r["rt_cd"], "9")
        self.assertEqual(api._submitted, [])

    def test_sell_no_cash_check(self):
        """SELL 은 현금검증(get_kr_available_amounts) 없이 제출."""
        api = _mk_order_api([])   # nrcvb 시퀀스 비어도 SELL 은 조회 안 함
        r = api._order("207940", "SELL", 10, 800_000, ord_dvsn="00")
        self.assertEqual(r["rt_cd"], "0")
        self.assertEqual(api.get_kr_available_amounts.call_count, 0)
        self.assertEqual(api._submitted[-1]["side"], "SELL")

    def test_no_ratio_or_buffer_reapplied(self):
        """_order 는 전달된 qty 를 그대로 제출(전략비중·0.98 재적용 없음)."""
        api = _mk_order_api([_avail(10_000_000, 100)])
        api._order("005930", "BUY", 50, 70_000, ord_dvsn="00")
        self.assertEqual(api._submitted[-1]["qty"], 50)   # 50 그대로(축소 없음)

    def test_final_check_uses_actual_submit_price(self):
        """최종검증은 전략가격이 아니라 실제 제출가격(호가정렬 후)으로 조회."""
        api = _mk_order_api([_avail(10_000_000, 100)])
        api.round_to_tick = lambda p, direction=1: int(p) + 5   # 제출가 보정 모사
        api._order("005930", "BUY", 1, 70_000, ord_dvsn="00")
        # get_kr_available_amounts 가 보정된 제출가(70005)로 호출됨
        called_price = api.get_kr_available_amounts.call_args[0][1]
        self.assertEqual(called_price, 70_005)


class TestBuySerialization(unittest.TestCase):
    """item10: 동일계좌 BUY 최종조회~제출 직렬화(두 주문 동시 nrcvb 소진 방지)."""

    def test_concurrent_buys_serialized_and_requery(self):
        active = {"n": 0, "max": 0}
        alock = threading.Lock()

        def _slow_submit(stock_code, order_type, qty, order_price, *a, **k):
            with alock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(0.15)
            with alock:
                active["n"] -= 1
            return {"rt_cd": "0"}

        api = _mk_order_api([_avail(10_000_000, 100), _avail(10_000_000, 100)],
                            submit_impl=_slow_submit)

        def _buy():
            api._order("005930", "BUY", 1, 70_000, ord_dvsn="00")
        t1 = threading.Thread(target=_buy)
        t2 = threading.Thread(target=_buy)
        t1.start(); t2.start(); t1.join(); t2.join()
        # 직렬화되어 동시 제출 없음
        self.assertEqual(active["max"], 1)
        # 두 번째 주문도 각자 새로 nrcvb 재조회
        self.assertEqual(api.get_kr_available_amounts.call_count, 2)


if __name__ == "__main__":
    unittest.main()
