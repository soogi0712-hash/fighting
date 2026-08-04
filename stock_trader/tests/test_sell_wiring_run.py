"""매도 배선 통합 검증 — 실제 StrategyManager.run() 경로 구동.

이 테스트는 run() 을 재구현하지 않고 실제 run() 을 호출해, 익절·매도 정책이
운영 경로에 실제로 연결됐는지 확인한다.

검증(요구사항 3):
  • 순이익 +2.0% 도달 → 전량 SELL (pyramid ②)
  • 순이익 +1.0% 이상 & sell_score≥6 → SELL (pyramid ④)
  • MA20 이탈 → SELL (decide_sell 안전망 오버레이)
  • 트레일링 → SELL (pyramid ⑥)
  • SELL 결정 후 PendingRegistry → Lifecycle → FillObserver 경로 진입
  • 동일 종목 active SELL 존재 시 추가 SELL 없음
추가:
  • 20분 시간청산이 run() 경로에서 실제 매도 접수까지(요구사항 6)
  • HOLD/SELL 모두 [SELL_DECISION] 로그(요구사항 7)
  • sell_result→result 버그 수정으로 NameError 없이 등록(요구사항 2)
  • 매도정책: 긴급손절 -3.0% 무조건 / 일반손절 -1.2%+5분+sell_score≥7 → SELL 라우팅

경량 하니스: 실제 run()/_decide_sell_for_holding/_register_pending_order/
has_active_sell/has_active_buy 를 그대로 바인딩하고, 지표검증(validator)·현재가/
잔고(api)만 가짜로 주입한다. pyramid/decision/pnl_guard/lifecycle/registry 는 실제.
"""
import os
import sys
import re
import inspect
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
while _ROOT in sys.path:
    sys.path.remove(_ROOT)
sys.path.insert(0, _ROOT)

import journal.fill_observer as fo  # noqa: E402
from journal.fill_observer import PendingOrderRegistry  # noqa: E402
from phoenix.lifecycle import OrderLifecycleManager, LifecycleState  # noqa: E402
import strategies.strategy_manager as sm_mod  # noqa: E402
from strategies.strategy_manager import StrategyManager  # noqa: E402
from strategies.pyramid_strategy import (  # noqa: E402
    PyramidStrategyManager, PyramidPosition,
)
from strategies.daily_pnl_guard import DailyPnLGuard  # noqa: E402
from strategies.reentry_guard import ReentryGuard  # noqa: E402
from screener.trade_decision import TradeDecisionEngine  # noqa: E402
from screener.transaction_cost import price_for_net_pct_from_cost  # noqa: E402
from strategies.buy_guard import (  # noqa: E402
    should_skip_new_buy, DEFAULT_BUY_COOLDOWN_SEC,
)

AVG = 10000 * 1.00015


def price_for_net(net_pct):
    return price_for_net_pct_from_cost(AVG, net_pct)


class FakeValidator:
    """run() 이 필요로 하는 iv/iv5 딕셔너리만 되돌려주는 최소 검증기."""

    def __init__(self, sell_score=0, sell_urgent=False, ma20_mult=1.02):
        self.sell_score = sell_score
        self.sell_urgent = sell_urgent
        self.ma20_mult = ma20_mult   # MA20 = 현재가 × ma20_mult

    def validate_5min(self, candles_5m, today_high=0.0, strength=0.0):
        return {
            "breakout_bonus": 0.0, "breakout_label": "-",
            "chase_blocked": False, "chase_reason": "",
            "vol_ratio_5m": 1.0, "vol_avg4_ratio": 1.0,
            "rise_15m_pct": 0.0, "rise_5m_pct": 0.0, "consec_bull": 0,
        }

    def validate(self, candles, vwap=0.0, prev_volume=0.0, strength=0.0):
        cur = float(candles[-1]["close"])
        ma20 = cur * self.ma20_mult
        return {
            "score": 0, "buy_score_norm": 0.0,
            "sell_score": self.sell_score, "sell_urgent": self.sell_urgent,
            "trend_score": 50, "strong_trend": False, "sell_detail": {},
            "buy_blocked_vol": False, "buy_blocked_vwap": False,
            "buy_blocked_sell": False, "vol_label": "N/A", "vwap_above": True,
            "detail": {
                "MA":  {"value": {"MA20": ma20}},
                "OBV": {"signal": "?"},
                "BB":  {"signal": "?"},
                "BB_SQZ": {"signal": "?"},
            },
        }


class FakeAPI:
    def __init__(self, cur_price):
        self.cur_price = cur_price
        self.sell_calls = []
        self.buy_calls = []

    def get_ohlcv(self, code, period="D", count=200):
        c = {"close": self.cur_price, "high": self.cur_price,
             "low": self.cur_price, "volume": 1000, "vwap": self.cur_price}
        return [dict(c), dict(c), dict(c)]

    def get_current_price(self, code):
        return {"price": self.cur_price, "strength": 0}

    def get_intraday_5min(self, code, count=12):
        return []

    def sell(self, code, qty, price, ord_dvsn="00"):
        self.sell_calls.append((code, qty, price, ord_dvsn))
        return {"rt_cd": "0", "output": {"KNO_ORD_NO": "OD-SELL-1"},
                "msg1": "매도주문접수성공"}

    def buy(self, code, qty, price, ord_dvsn="00"):
        self.buy_calls.append((code, qty, price, ord_dvsn))
        return {"rt_cd": "0", "output": {"KNO_ORD_NO": "OD-BUY-1"},
                "msg1": "매수주문접수성공"}


def _fake_session():
    return {
        "session": "정규장", "icon": "🟢", "time_kst": "10:00:00",
        "tradeable": True, "allow_new_buy": True, "sell_only": False,
        "buy_block_reason": "", "order_dvsn": "01", "order_label": "시장가",
    }


class RunSM:
    run                      = StrategyManager.run
    _decide_sell_for_holding = StrategyManager._decide_sell_for_holding
    _register_pending_order  = StrategyManager._register_pending_order
    has_active_sell          = StrategyManager.has_active_sell
    has_active_buy           = StrategyManager.has_active_buy

    def __init__(self, api, validator, registry, lifecycle_mgr):
        self.api = api
        self.validator = validator
        self.decision = TradeDecisionEngine()
        self.pyramid = PyramidStrategyManager(api, max_per_stock=10_000_000,
                                              max_total=100_000_000)
        self.pyramid.positions = {}
        self.pyramid._save = lambda: None
        self.pnl_guard = DailyPnLGuard(
            target_krw=300_000, profit_lock_krw=300_000,
            loss_limit_krw=-300_000, name="테스트", use_us_session=False)
        self.reentry = ReentryGuard()
        self._lifecycle_mgr = lifecycle_mgr
        self._pending_registry = registry
        self._pending_sell_meta = {}

    def _log_trade(self, *a, **k):
        pass


def seed_position(sm, code, name, net_pct, elapsed_min,
                  highest_net=None, qty=100):
    """pyramid 에 보유 포지션 1건 주입(created_at 을 과거로 설정)."""
    p = PyramidPosition(code, name, AVG)
    p.avg_price = AVG
    p.total_qty = qty
    p.current_level = 1
    hp = price_for_net(highest_net) if highest_net is not None else AVG
    p.highest_price = hp
    p.lowest_price = AVG
    p.created_at = (datetime.now() - timedelta(minutes=elapsed_min)).isoformat()
    p.level_entries = {1: {"price": AVG, "avg_price": AVG,
                           "qty": qty, "remaining": qty}}
    sm.pyramid.positions[code] = p
    return p


class RunWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="runwire-")
        self._orig_db = fo._JOURNAL_DB_PATH
        fo._JOURNAL_DB_PATH = os.path.join(self.tmp, "pending.db")
        self._reset_fo_conn()
        self.reg = PendingOrderRegistry()
        self.lc = OrderLifecycleManager(os.path.join(self.tmp, "lc.db"))
        self._orig_session = sm_mod.session_info
        sm_mod.session_info = _fake_session
        self._orig_journal = sm_mod._JOURNAL_ENABLED
        sm_mod._JOURNAL_ENABLED = False

    def tearDown(self):
        sm_mod.session_info = self._orig_session
        sm_mod._JOURNAL_ENABLED = self._orig_journal
        self._reset_fo_conn()
        fo._JOURNAL_DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _reset_fo_conn():
        if getattr(fo._local, "fo_conn", None) is not None:
            try: fo._local.fo_conn.close()
            except Exception: pass
            fo._local.fo_conn = None

    def _mk(self, cur_net, sell_score=0, sell_urgent=False, ma20_mult=1.02):
        cur = price_for_net(cur_net)
        api = FakeAPI(cur)
        val = FakeValidator(sell_score=sell_score, sell_urgent=sell_urgent,
                            ma20_mult=ma20_mult)
        return RunSM(api, val, self.reg, self.lc), api

    def _assert_pending_lifecycle_path(self, code):
        """SELL 접수 후 PendingRegistry→Lifecycle→FillObserver(get_trackable) 진입 확인."""
        self.assertTrue(self.reg.has_active_sell("KR", code))
        trackable = self.reg.get_trackable("KR")
        entry = next((r for r in trackable if r.get("code") == code), None)
        self.assertIsNotNone(entry, "FillObserver 추적 목록에 SELL 주문 없음")
        self.assertTrue((entry.get("odno") or "").strip())          # odno 확보
        lc = self.lc.load(entry["trade_id"])                        # trade_id=lifecycle_id
        self.assertIsNotNone(lc)
        self.assertEqual(lc.current_state, LifecycleState.ORDER_ACCEPTED)

    # ── 요구사항 3: +2.0% 전량익절이 run() 에서 실제 SELL ──
    def test_full_take_profit_sells_in_run(self):
        sm, api = self._mk(cur_net=2.2)
        seed_position(sm, "005930", "삼성전자", net_pct=2.2, elapsed_min=3)
        res = sm.run({"code": "005930", "name": "삼성전자"}, cached_cash=10_000_000)
        self.assertEqual(res["action"], "SELL", res)
        self.assertIn("전량익절", res["reason"])
        self.assertEqual(len(api.sell_calls), 1)
        self.assertEqual(api.sell_calls[0][1], 100)   # 전량
        self._assert_pending_lifecycle_path("005930")

    # ── 요구사항 3: +1.0% 이상 & sell_score≥6 → SELL ──
    def test_profit_plus_sellscore6_sells_in_run(self):
        sm, api = self._mk(cur_net=1.2, sell_score=6, sell_urgent=True)
        seed_position(sm, "005930", "삼성전자", net_pct=1.2, elapsed_min=3)
        res = sm.run({"code": "005930", "name": "삼성전자"}, cached_cash=10_000_000)
        self.assertEqual(res["action"], "SELL", res)
        self.assertEqual(len(api.sell_calls), 1)
        self._assert_pending_lifecycle_path("005930")

    # ── 요구사항 3: MA20 이탈 → decide_sell 안전망이 SELL 라우팅 ──
    def test_ma20_exit_sells_in_run(self):
        sm, api = self._mk(cur_net=0.2, ma20_mult=1/0.985)   # MA20 -1.5% 이탈
        seed_position(sm, "069500", "KODEX", net_pct=0.2, elapsed_min=5)
        with self.assertLogs("StrategyManager", level="INFO") as cm:
            res = sm.run({"code": "069500", "name": "KODEX"}, cached_cash=10_000_000)
        joined = "\n".join(cm.output)
        self.assertRegex(joined, r"\[SELL_DECISION\].*action=SELL")
        self.assertEqual(res["action"], "SELL", res)
        self.assertIn("MA20", res["reason"])
        self.assertEqual(len(api.sell_calls), 1)
        self._assert_pending_lifecycle_path("069500")

    # ── 요구사항 3: 트레일링 → SELL (pyramid ⑥) ──
    def test_trailing_sells_in_run(self):
        sm, api = self._mk(cur_net=0.3)
        seed_position(sm, "005930", "삼성전자", net_pct=0.3, elapsed_min=5,
                      highest_net=2.0)   # 고점 +2% 활성화 후 하락
        res = sm.run({"code": "005930", "name": "삼성전자"}, cached_cash=10_000_000)
        self.assertEqual(res["action"], "SELL", res)
        self.assertIn("트레일링", res["reason"])
        self.assertEqual(len(api.sell_calls), 1)
        self._assert_pending_lifecycle_path("005930")

    # ── 요구사항 6: 20분 시간청산이 run() 경로에서 실제 매도 접수까지 ──
    def test_time_exit_fires_in_run(self):
        sm, api = self._mk(cur_net=-0.3)
        seed_position(sm, "005930", "삼성전자", net_pct=-0.3, elapsed_min=25)
        res = sm.run({"code": "005930", "name": "삼성전자"}, cached_cash=10_000_000)
        self.assertEqual(res["action"], "SELL", res)
        self.assertIn("시간청산", res["reason"])
        self.assertEqual(len(api.sell_calls), 1)
        self._assert_pending_lifecycle_path("005930")

    # ── 요구사항 3: 동일 종목 active SELL 존재 → pyramid SELL 이어도 추가 SELL 없음 ──
    def test_active_sell_blocks_pyramid_sell(self):
        sm, api = self._mk(cur_net=2.2)
        seed_position(sm, "005930", "삼성전자", net_pct=2.2, elapsed_min=3)
        self.reg.register("KR", "t-existing", "005930", "SELL", 100,
                          "2026-08-04T09:00:00", odno="OD-EXIST")
        res = sm.run({"code": "005930", "name": "삼성전자"}, cached_cash=10_000_000)
        self.assertEqual(len(api.sell_calls), 0)   # in-flight 가드
        self.assertEqual(res["action"], "HOLD", res)

    # ── 요구사항 3: 동일 종목 active SELL 존재 → 오버레이 SELL 도 스킵 ──
    def test_active_sell_blocks_overlay_sell(self):
        sm, api = self._mk(cur_net=0.2, ma20_mult=1/0.985)
        seed_position(sm, "069500", "KODEX", net_pct=0.2, elapsed_min=5)
        self.reg.register("KR", "t-existing", "069500", "SELL", 100,
                          "2026-08-04T09:00:00", odno="OD-EXIST")
        res = sm.run({"code": "069500", "name": "KODEX"}, cached_cash=10_000_000)
        self.assertEqual(len(api.sell_calls), 0)
        self.assertEqual(res["action"], "HOLD", res)

    # ── 요구사항 7: 보유 HOLD 시에도 [SELL_DECISION] action=HOLD 로그 ──
    def test_hold_still_logs_sell_decision(self):
        sm, api = self._mk(cur_net=0.2, ma20_mult=0.99)   # MA20 상단(이탈 없음)
        seed_position(sm, "005930", "삼성전자", net_pct=0.2, elapsed_min=5)
        with self.assertLogs("StrategyManager", level="INFO") as cm:
            res = sm.run({"code": "005930", "name": "삼성전자"}, cached_cash=10_000_000)
        joined = "\n".join(cm.output)
        self.assertRegex(joined, r"\[SELL_DECISION\].*action=HOLD")
        self.assertEqual(res["action"], "HOLD", res)
        self.assertEqual(len(api.sell_calls), 0)

    # ── 매도정책: 긴급손절 -3.0% 는 무조건 SELL 라우팅 (run() 경로) ──
    def test_emergency_stop_routes_in_run(self):
        # net -3.5% (≤ -3.0%), elapsed 짧아도 무조건. pyramid -5% 미도달이라 pyramid HOLD.
        sm, api = self._mk(cur_net=-3.5)
        seed_position(sm, "005930", "삼성전자", net_pct=-3.5, elapsed_min=1)
        res = sm.run({"code": "005930", "name": "삼성전자"}, cached_cash=10_000_000)
        self.assertEqual(res["action"], "SELL", res)
        self.assertIn("긴급손절", res["reason"])
        self.assertEqual(len(api.sell_calls), 1)
        self._assert_pending_lifecycle_path("005930")

    # ── 매도정책: 일반손절 -1.2% + 5분 + sell_score≥7 → SELL ──
    def test_general_stop_routes_in_run(self):
        sm, api = self._mk(cur_net=-1.5, sell_score=7)
        seed_position(sm, "005930", "삼성전자", net_pct=-1.5, elapsed_min=10)
        res = sm.run({"code": "005930", "name": "삼성전자"}, cached_cash=10_000_000)
        self.assertEqual(res["action"], "SELL", res)
        self.assertIn("일반손절", res["reason"])
        self.assertEqual(len(api.sell_calls), 1)
        self._assert_pending_lifecycle_path("005930")

    # ── 매도정책: 일반손절 sell_score 6 → 미달 → HOLD ──
    def test_general_stop_score6_holds_in_run(self):
        sm, api = self._mk(cur_net=-1.5, sell_score=6)
        seed_position(sm, "005930", "삼성전자", net_pct=-1.5, elapsed_min=10)
        res = sm.run({"code": "005930", "name": "삼성전자"}, cached_cash=10_000_000)
        self.assertEqual(res["action"], "HOLD", res)
        self.assertEqual(len(api.sell_calls), 0)

    # ── 매도정책: 일반손절 보유 5분 미만 → 보류 → HOLD ──
    def test_general_stop_under_5min_holds_in_run(self):
        sm, api = self._mk(cur_net=-1.5, sell_score=7)
        seed_position(sm, "005930", "삼성전자", net_pct=-1.5, elapsed_min=3)
        res = sm.run({"code": "005930", "name": "삼성전자"}, cached_cash=10_000_000)
        self.assertEqual(res["action"], "HOLD", res)
        self.assertEqual(len(api.sell_calls), 0)


class SourceWiringTest(unittest.TestCase):
    """소스 검증 — run() 이 배선/버그수정을 반영하는지 정적 확인."""

    def test_run_wires_decide_sell(self):
        src = inspect.getsource(StrategyManager.run)
        self.assertIn("_decide_sell_for_holding", src)
        overlay = inspect.getsource(StrategyManager._decide_sell_for_holding)
        self.assertIn("self.decision.decide_sell", overlay)
        self.assertIn("[SELL_DECISION]", overlay)

    def test_sell_accept_uses_result_not_sell_result(self):
        src = inspect.getsource(StrategyManager.run)
        self.assertNotIn("order_response= sell_result", src)
        self.assertNotIn("order_response=sell_result", src)
        self.assertTrue(re.search(r"order_response=\s*result", src))

    def test_hard_stop_active_in_overlay(self):
        overlay = inspect.getsource(StrategyManager._decide_sell_for_holding)
        # 안전망 + 하드손절(STOP_LOSS) 모두 라우팅 대상
        self.assertIn("_ROUTABLE_SELL_TYPES", overlay)
        self.assertIn("TRAILING_STOP", overlay)
        self.assertIn("MA20_EXIT", overlay)
        self.assertIn("SCORE_DROP", overlay)
        self.assertIn("STOP_LOSS", overlay)


class EtfRepeatBuyTest(unittest.TestCase):
    """요구사항: ETF 동일 종목 반복 신규매수 차단."""

    def test_held_blocks(self):
        skip, r = should_skip_new_buy("069500", is_held=True, has_active_buy=False)
        self.assertTrue(skip); self.assertIn("보유", r)

    def test_active_buy_blocks(self):
        skip, r = should_skip_new_buy("069500", is_held=False, has_active_buy=True)
        self.assertTrue(skip); self.assertIn("매수 주문", r)

    def test_cooldown_blocks(self):
        skip, r = should_skip_new_buy("069500", is_held=False, has_active_buy=False,
                                      recent_buy_ts=1000.0, now_ts=1100.0,
                                      cooldown_sec=DEFAULT_BUY_COOLDOWN_SEC)
        self.assertTrue(skip)

    def test_clean_allows(self):
        skip, r = should_skip_new_buy("069500", is_held=False, has_active_buy=False,
                                      recent_buy_ts=1000.0, now_ts=1400.0,
                                      cooldown_sec=DEFAULT_BUY_COOLDOWN_SEC)
        self.assertFalse(skip)


if __name__ == "__main__":
    unittest.main()
