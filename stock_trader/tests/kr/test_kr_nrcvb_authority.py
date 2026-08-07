"""국내 현금 주문가능 권위값 = nrcvb_buy_amt + nrcvb_buy_qty (ord_psbl_cash 아님).

실증(삼성바이오로직스 207940, 주문가 1,509,000원):
  ord_psbl_cash=6,098원, nrcvb_buy_amt≥1,509,000원, nrcvb_buy_qty=1주.
  기존 코드는 ord_psbl_cash(6,098)를 상한으로 써서 BUY 미제출했다.
  → nrcvb_buy_amt/nrcvb_buy_qty 만 권위값. ord_psbl_cash 는 참고 로그용.
  → max_buy_amt/max_buy_qty(미수 포함)는 절대 미사용.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import api.kis_api as kmod                              # noqa: E402
from api.kis_api import KISApi                          # noqa: E402
from strategies.strategy_manager import StrategyManager  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _mk_api():
    api = object.__new__(KISApi)
    api.base_url = "https://mock"
    api.account_no = "12345678-01"
    api._headers = lambda *a, **k: {}
    return api


def _mgr(avail):
    mgr = StrategyManager.__new__(StrategyManager)
    mgr.api = MagicMock()
    mgr.api.get_kr_available_amounts.return_value = avail
    mgr._pending_registry = None
    for m in ("_kr_finalize_buy_qty",):
        setattr(mgr, m, getattr(StrategyManager, m).__get__(mgr))
    return mgr


# 삼성바이오 실증 응답(개인정보 없는 필드만)
_SBIO = {"rt_cd": "0", "output": {
    "ord_psbl_cash": "6098",          # 당장 현금잔고(참고)
    "nrcvb_buy_amt": "1509000",       # ★ 미수 없는 매수가능금액(권위)
    "nrcvb_buy_qty": "1",             # ★ 미수 없는 매수가능수량(권위)
    "max_buy_amt":   "9999999",       # 미수 포함(미사용)
    "max_buy_qty":   "6",             # 미수 포함(미사용)
}}


class TestGetKRAvailableUsesNrcvb(unittest.TestCase):
    def setUp(self):
        self._g = kmod.requests.get

    def tearDown(self):
        kmod.requests.get = self._g

    def test_authority_is_nrcvb_not_ord_psbl_cash(self):
        api = _mk_api()
        kmod.requests.get = lambda *a, **k: _Resp(_SBIO)
        r = api.get_kr_available_amounts("207940", 1_509_000, "00")
        self.assertTrue(r["ok"])
        self.assertEqual(r["amount"], 1_509_000.0)   # nrcvb_buy_amt (NOT 6,098)
        self.assertEqual(r["qty"], 1)                # nrcvb_buy_qty
        self.assertEqual(r["ord_psbl_cash"], 6_098.0)  # 참고용만
        # max_buy_* 는 반환/사용하지 않음
        self.assertNotIn("max_buy_qty", r)
        self.assertNotEqual(r["qty"], 6)             # 미수수량(6) 아님

    def test_nrcvb_amt_zero_blocks(self):
        api = _mk_api()
        kmod.requests.get = lambda *a, **k: _Resp({"rt_cd": "0", "output": {
            "ord_psbl_cash": "6098", "nrcvb_buy_amt": "0", "nrcvb_buy_qty": "0",
            "max_buy_amt": "9999999", "max_buy_qty": "6"}})
        r = api.get_kr_available_amounts("207940", 1_509_000, "00")
        self.assertFalse(r["ok"])       # nrcvb 0 → 미제출(미수수량 있어도 무시)

    def test_nrcvb_missing_blocks(self):
        api = _mk_api()
        kmod.requests.get = lambda *a, **k: _Resp({"rt_cd": "0", "output": {
            "ord_psbl_cash": "6098", "max_buy_qty": "6"}})   # nrcvb 필드 없음
        r = api.get_kr_available_amounts("207940", 1_509_000, "00")
        self.assertFalse(r["ok"])       # 누락 → 0 처리 → 미제출


class TestSamsungBioFinalQty(unittest.TestCase):
    """실증값 기준 전략비중별 최종수량(임의 1주 강제 없음)."""

    def _final(self, ratio, nrcvb_amt, nrcvb_qty=1, price=1_509_000):
        mgr = _mgr({"ok": True, "amount": float(nrcvb_amt), "qty": int(nrcvb_qty),
                    "ord_psbl_cash": 6_098.0})
        return mgr._kr_finalize_buy_qty("207940", price, ratio)[0]

    def test_full_exact_one_share_blocked_by_buffer(self):
        """FULL 100%, nrcvb_amt=정확히 1주가격 → 0.98 버퍼로 0주(정책 판단 필요)."""
        self.assertEqual(self._final(1.0, 1_509_000), 0)
        self.assertEqual(self._final(1.0, 1_509_226), 0)   # 수수료 포함 실제도 0

    def test_full_with_margin_buys_one(self):
        """FULL 100%, nrcvb_amt≥약 1.021×가격 → 1주."""
        self.assertEqual(self._final(1.0, 1_540_000), 1)

    def test_early_30pct_blocked(self):
        """EARLY 30%, 1.5M 종목 → 비중예산(≈452,700)<1주 → 0주(정책상 정상)."""
        self.assertEqual(self._final(0.30, 1_509_000), 0)
        self.assertEqual(self._final(0.30, 5_000_000), 0)   # 30%≈1.5M, 여전히 1주 미만

    def test_ord_psbl_cash_not_a_cap(self):
        """ord_psbl_cash=6,098원이 상한이 아님을 확인: 여유 nrcvb 면 1주 체결."""
        self.assertEqual(self._final(1.0, 1_540_000, nrcvb_qty=1), 1)

    def test_nrcvb_qty_caps_below_amount(self):
        """nrcvb_buy_qty 가 금액환산보다 작으면 그 수량으로 상한."""
        # amount 5,000,000 / price 1,000,000 → 버퍼 floor(4.9)=4, but nrcvb_qty=2
        mgr = _mgr({"ok": True, "amount": 5_000_000.0, "qty": 2,
                    "ord_psbl_cash": 6_098.0})
        self.assertEqual(mgr._kr_finalize_buy_qty("069500", 1_000_000, 1.0)[0], 2)


if __name__ == "__main__":
    unittest.main()
