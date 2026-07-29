import os
import sys
import unittest
from datetime import datetime, timedelta

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

from screener.sell_decision import SellReason, normalize_sell_score
from screener.trade_decision import TradeDecisionEngine


class TestSellDecision(unittest.TestCase):
    def setUp(self):
        self.engine = TradeDecisionEngine()

    def test_normalize_sell_score_keeps_0_1_total_score_distinct_from_sell_score(self):
        self.assertEqual(normalize_sell_score(0.8), 0)
        self.assertEqual(normalize_sell_score(6), 6)
        self.assertEqual(normalize_sell_score(7.4), 7)

    def test_emergency_stop_for_large_loss(self):
        position = {
            "code": "A005930",
            "name": "삼성전자",
            "avg_price": 100.0,
            "highest_price": 101.0,
            "qty": 10,
            "created_at": (datetime.now() - timedelta(minutes=1)).isoformat(),
        }
        score_result = {
            "cur_price": 97.0,
            "sell_score": 0,
            "price_ma20": 100.0,
        }

        decision = self.engine.decide_sell(position, score_result)

        self.assertEqual(decision["action"], "SELL")
        self.assertEqual(decision["sell_type"], "STOP_LOSS")
        self.assertEqual(decision["sell_reason"], SellReason.EMERGENCY_STOP)

    def test_general_stop_loss_requires_hold_time(self):
        position = {
            "code": "A005930",
            "name": "삼성전자",
            "avg_price": 100.0,
            "highest_price": 101.0,
            "qty": 10,
            "created_at": (datetime.now() - timedelta(minutes=3)).isoformat(),
        }
        score_result = {
            "cur_price": 98.0,
            "sell_score": 10,
            "price_ma20": 100.0,
        }

        decision = self.engine.decide_sell(position, score_result)

        self.assertEqual(decision["action"], "HOLD")
        self.assertNotIn("sell_type", decision)

    def test_general_stop_loss_requires_sell_score_threshold(self):
        position = {
            "code": "A005930",
            "name": "삼성전자",
            "avg_price": 100.0,
            "highest_price": 101.0,
            "qty": 10,
            "created_at": (datetime.now() - timedelta(minutes=6)).isoformat(),
        }
        score_result = {
            "cur_price": 98.7,
            "sell_score": 6,
            "price_ma20": 100.0,
        }

        decision = self.engine.decide_sell(position, score_result)

        self.assertEqual(decision["action"], "HOLD")
        self.assertNotIn("sell_type", decision)

    def test_general_stop_loss_sells_when_threshold_and_hold_time_are_met(self):
        position = {
            "code": "A005930",
            "name": "삼성전자",
            "avg_price": 100.0,
            "highest_price": 101.0,
            "qty": 10,
            "created_at": (datetime.now() - timedelta(minutes=6)).isoformat(),
        }
        score_result = {
            "cur_price": 98.7,
            "sell_score": 7,
            "price_ma20": 100.0,
        }

        decision = self.engine.decide_sell(position, score_result)

        self.assertEqual(decision["action"], "SELL")
        self.assertEqual(decision["sell_type"], "STOP_LOSS")
        self.assertEqual(decision["sell_reason"], SellReason.STOP_LOSS)

    def test_loss_only_does_not_trigger_stop_loss_when_pnl_is_above_threshold(self):
        position = {
            "code": "A005930",
            "name": "삼성전자",
            "avg_price": 100.0,
            "highest_price": 101.0,
            "qty": 10,
            "created_at": (datetime.now() - timedelta(minutes=20)).isoformat(),
        }
        score_result = {
            "cur_price": 99.0,
            "sell_score": 10,
            "price_ma20": 100.0,
        }

        decision = self.engine.decide_sell(position, score_result)

        self.assertEqual(decision["action"], "HOLD")
        self.assertNotIn("sell_type", decision)

    def test_total_score_is_not_used_when_sell_score_is_missing(self):
        position = {
            "code": "A005930",
            "name": "삼성전자",
            "avg_price": 100.0,
            "highest_price": 101.0,
            "qty": 10,
            "created_at": (datetime.now() - timedelta(minutes=6)).isoformat(),
        }
        score_result = {
            "cur_price": 98.7,
            "total_score": 100,
            "price_ma20": 100.0,
        }

        decision = self.engine.decide_sell(position, score_result)

        self.assertEqual(decision["action"], "HOLD")
        self.assertNotIn("sell_type", decision)

    def test_trailing_stop_path_still_sells(self):
        position = {
            "code": "A005930",
            "name": "삼성전자",
            "avg_price": 100.0,
            "highest_price": 120.0,
            "qty": 10,
            "created_at": (datetime.now() - timedelta(minutes=20)).isoformat(),
        }
        score_result = {
            "cur_price": 105.0,
            "sell_score": 0,
            "price_ma20": 100.0,
        }

        decision = self.engine.decide_sell(position, score_result)

        self.assertEqual(decision["action"], "SELL")
        self.assertEqual(decision["sell_type"], "TRAILING_STOP")
        self.assertEqual(decision["sell_reason"], SellReason.TRAILING_STOP)

    def test_signal_exit_path_still_sells(self):
        position = {
            "code": "A005930",
            "name": "삼성전자",
            "avg_price": 100.0,
            "highest_price": 100.0,
            "qty": 10,
            "created_at": (datetime.now() - timedelta(minutes=20)).isoformat(),
        }
        score_result = {
            "cur_price": 101.0,
            "sell_score": 0,
            "price_ma20": 103.0,
        }

        decision = self.engine.decide_sell(position, score_result)

        self.assertEqual(decision["action"], "SELL")
        self.assertEqual(decision["sell_type"], "MA20_EXIT")
        self.assertEqual(decision["sell_reason"], SellReason.SIGNAL_EXIT)


if __name__ == "__main__":
    unittest.main()
