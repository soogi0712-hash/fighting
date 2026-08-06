"""미국 매수: KIS 현금 주문가능금액·수량 기반 최종수량 확정 + 단일 축소 재시도.

_do_buy 는 예수금 나눗셈이 아니라
  min(전략수량, KIS주문가능수량, floor(현금가능금액*0.98/주문가))
로 주문수량을 확정하고, 조회실패/수량0 이면 buy_us 를 호출하지 않는다.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import strategies.us_strategy_manager as usm            # noqa: E402
from strategies.us_strategy_manager import USStrategyManager, USPosition  # noqa: E402
from utils.order_sizing import qty_from_cash            # noqa: E402


class FakePosMgr:
    def __init__(self):
        self.positions = {}

    def add(self, pos):
        self.positions[pos.symbol] = pos


def make_us_buy(order_avails, buy_results, fx=1300.0,
                capacity_usd=1e9, active=False):
    """_do_buy 구동용 경량 매니저.

    order_avails: _do_buy 의 주문별 get_us_available_amounts 반환 시퀀스(iter).
    buy_results : api.buy_us 반환 시퀀스(iter).
    """
    us = USStrategyManager.__new__(USStrategyManager)
    us.pos_mgr = FakePosMgr()
    us._us_pending_buy_meta = {}
    us._us_pending_sell_meta = {}
    us._us_outbox = None
    us._us_lifecycle_mgr = None
    us._us_open_scan = {"first_buy_time": None}
    us.reentry = MagicMock()
    us.reentry.check.return_value = (False, {})
    us.api = MagicMock()
    us.api.get_usd_exchange_rate.return_value = fx

    _order_iter = iter(order_avails)

    def _avail(*args, **kwargs):
        # _check_buy_capacity 는 인자 없이 호출 → 넉넉한 용량 반환
        if not kwargs:
            return {"ok": True, "usd": capacity_usd, "krw": 0.0, "qty": 999999}
        # _do_buy 주문별(+재조회) 호출
        return next(_order_iter)

    us.api.get_us_available_amounts.side_effect = _avail
    us.api.buy_us.side_effect = list(buy_results)
    # 성공경로 격리(수량 검증에 집중)
    us._buy_result = lambda *a, **k: {"action": "BUY_ACCEPTED", "qty": a[4]}
    us._record_first_buy = lambda: None
    for m in ("_do_buy", "_us_has_active_order", "_check_buy_capacity"):
        setattr(us, m, getattr(USStrategyManager, m).__get__(us))
    if active:
        us._us_pending_buy_meta["x"] = {"code": "AAPL"}
    return us


SESS = {"session": "정규장"}
IV = {"buy_score": 1.0, "intraday_pct": 0.0, "vol_ratio": 1.0, "vwap": 0.0,
      "above_vwap": True, "ema_bull": True, "rsi": 50.0, "pullback_breakout": False}


@patch.object(usm, "_US_JOURNAL_ENABLED", False)
@patch.object(usm, "_US_LIFECYCLE_ENABLED", False)
class TestUSBuyOrderSizing(unittest.TestCase):

    def test_deposit_large_but_qty_small(self):
        """가능금액 큼 but KIS 주문가능수량 3 → 3주만 주문."""
        us = make_us_buy(
            order_avails=[{"ok": True, "usd": 1e9, "krw": 0.0, "qty": 3}],
            buy_results=[{"rt_cd": "0"}])
        res = us._do_buy("AAPL", "Apple", "NASD", 10.0, SESS, IV)
        self.assertEqual(res["action"], "BUY_ACCEPTED")
        self.assertEqual(us.api.buy_us.call_args[0][1], 3)          # qty=3
        # 버퍼 상한도 준수(현금가능금액*0.98/가 이하)
        self.assertLessEqual(3, qty_from_cash(1e9, 10.0))

    def test_amount_present_but_qty_zero_blocks(self):
        """금액은 있으나 KIS 주문가능수량 0 → BUY_BLOCKED, buy_us 미호출."""
        us = make_us_buy(
            order_avails=[{"ok": True, "usd": 1e9, "krw": 0.0, "qty": 0}],
            buy_results=[{"rt_cd": "0"}])
        res = us._do_buy("AAPL", "Apple", "NASD", 10.0, SESS, IV)
        self.assertEqual(res["action"], "BUY_BLOCKED")
        us.api.buy_us.assert_not_called()

    def test_lookup_failure_no_order(self):
        """주문가능 조회 실패(ok=False) → BUY_BLOCKED, buy_us 미호출."""
        us = make_us_buy(
            order_avails=[{"ok": False}],
            buy_results=[{"rt_cd": "0"}])
        res = us._do_buy("AAPL", "Apple", "NASD", 10.0, SESS, IV)
        self.assertEqual(res["action"], "BUY_BLOCKED")
        us.api.buy_us.assert_not_called()

    def test_inflight_duplicate_blocked(self):
        """동일 종목 미체결 매수 존재 → in-flight SKIP, buy_us 미호출."""
        us = make_us_buy(
            order_avails=[{"ok": True, "usd": 1e9, "krw": 0.0, "qty": 10}],
            buy_results=[{"rt_cd": "0"}], active=True)
        res = us._do_buy("AAPL", "Apple", "NASD", 10.0, SESS, IV)
        self.assertEqual(res["action"], "SKIP")
        us.api.buy_us.assert_not_called()

    def test_insufficient_funds_single_resize_retry(self):
        """부족 오류 → 재조회 후 더 작은 수량으로 1회만 재시도."""
        us = make_us_buy(
            order_avails=[
                {"ok": True, "usd": 1e9, "krw": 0.0, "qty": 50},   # 초기
                {"ok": True, "usd": 1e9, "krw": 0.0, "qty": 2},    # 재조회(축소)
            ],
            buy_results=[
                {"rt_cd": "1", "msg1": "주문가능금액 부족"},        # 1차 실패
                {"rt_cd": "0"},                                    # 축소 후 성공
            ])
        res = us._do_buy("AAPL", "Apple", "NASD", 10.0, SESS, IV)
        self.assertEqual(res["action"], "BUY_ACCEPTED")
        self.assertEqual(us.api.buy_us.call_count, 2)              # 정확히 1회 재시도
        self.assertEqual(us.api.buy_us.call_args_list[1][0][1], 2)  # 축소수량=2

    def test_no_infinite_retry_when_not_smaller(self):
        """재조회해도 수량이 안 줄면(동일수량) 재시도하지 않는다."""
        us = make_us_buy(
            order_avails=[
                {"ok": True, "usd": 1e9, "krw": 0.0, "qty": 50},
                {"ok": True, "usd": 1e9, "krw": 0.0, "qty": 50},   # 그대로
            ],
            buy_results=[
                {"rt_cd": "1", "msg1": "주문가능금액 부족"},
                {"rt_cd": "0"},
            ])
        res = us._do_buy("AAPL", "Apple", "NASD", 10.0, SESS, IV)
        # 동일수량 반복 금지 → buy_us 는 1회만
        self.assertEqual(us.api.buy_us.call_count, 1)
        self.assertNotEqual(res["action"], "BUY_ACCEPTED")


if __name__ == "__main__":
    unittest.main()
