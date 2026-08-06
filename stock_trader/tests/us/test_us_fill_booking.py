"""US 체결 delta 누적 부킹 + crash-safe outbox 검증.

원칙:
  - event_key = f"{oid}:{cum_qty}" (주문+누적체결) 로 delta event 를 write-ahead 영속화
  - 각 부수효과(pos/pnl/reentry/event/app) 처리 상태를 개별 플래그로 영속 기록
  - 재시작(새 매니저 인스턴스, 동일 DB) 시 미완료 부수효과만 재처리
  - 메모리 rollback 이 아니라 DB 중간상태 + 새 객체로 crash 를 재현

crash 재현: outbox.set_flag 를 특정 플래그에서 예외 발생시켜 "효과는 적용됐으나
플래그 저장 전 강제종료" 상태를 DB 에 남긴 뒤, 매니저 객체를 폐기하고 동일 DB 로
새 매니저를 만들어 replay 한다.
"""
from __future__ import annotations

import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from strategies.us_strategy_manager import (   # noqa: E402
    USStrategyManager, USPosition, _USFillOutbox,
)


class FakePosMgr:
    """파일 저장 없는 경량 포지션 매니저 (재시작 시 동일 인스턴스=durable JSON 모사)."""
    def __init__(self):
        self.positions = {}

    def add(self, pos):
        self.positions[pos.symbol] = pos

    def remove(self, symbol):
        self.positions.pop(symbol, None)

    def update(self, symbol, qty, avg_price, level):
        p = self.positions.get(symbol)
        if p:
            p.qty = qty
            p.avg_price = avg_price
            p.current_level = level


class FakePnl:
    """재시작 시 새로 만들어짐(비영속) — outbox 재구성 대상."""
    def __init__(self):
        self.total = 0.0
        self.calls = []

    def record(self, x):
        self.total += x
        self.calls.append(x)

    def status_dict(self):
        return {"realized_pnl": self.total, "peak_pnl": max(0.0, self.total),
                "state": "NORMAL"}


class FakeReentry:
    """data/reentry_guard.json 처럼 영속(재시작 시 동일 인스턴스 유지) + check 멱등."""
    def __init__(self):
        self.sells = []

    def check(self, market, code, name):
        return (any(s["code"] == code for s in self.sells), {})

    def record_sell(self, **kw):
        self.sells.append(kw)


class FakeLC:
    def __init__(self, oid, code, filled_qty, avg_fill_price,
                 side="BUY", order_qty=10):
        self.order_lifecycle_id = oid
        self.code = code
        self.filled_qty = filled_qty
        self.avg_fill_price = avg_fill_price
        self.side = side
        self.order_qty = order_qty
        self.trade_id = ""


def make_us(db_path, pos_mgr=None, pnl_guard=None, reentry=None):
    us = USStrategyManager.__new__(USStrategyManager)
    us.pos_mgr = pos_mgr if pos_mgr is not None else FakePosMgr()
    us._us_pending_buy_meta = {}
    us._us_pending_sell_meta = {}
    us._us_fill_events = []
    us._us_outbox = _USFillOutbox(db_path)
    us.pnl_guard = pnl_guard if pnl_guard is not None else FakePnl()
    us.reentry = reentry if reentry is not None else MagicMock()
    if isinstance(us.reentry, MagicMock):
        us.reentry.check.return_value = (False, {})
    us.api = MagicMock()
    us.api.get_usd_exchange_rate.return_value = 1000.0
    for m in ("_us_apply_fill_delta", "_us_process_outbox_row", "_us_pos_add",
              "_us_pos_reduce", "_us_replay_outbox", "_us_pending_app_events",
              "us_mark_app_done", "_us_has_active_order", "_do_buy"):
        setattr(us, m, getattr(USStrategyManager, m).__get__(us))
    return us


def crash_on(us, target_flag):
    """outbox.set_flag(target_flag) 에서 예외 → 효과 적용 후 플래그 저장 전 강제종료 모사."""
    orig = us._us_outbox.set_flag

    def failing(ek, flag):
        if flag == target_flag:
            raise RuntimeError(f"crash before {flag}")
        return orig(ek, flag)
    us._us_outbox.set_flag = failing


class TestUSOutbox(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "us.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _buy_meta(self, us, oid, qty=10):
        us._us_pending_buy_meta[oid] = {
            "code": "AAPL", "name": "Apple", "excd": "NASD", "level": 1, "qty": qty}

    def _sell_meta(self, us, oid, qty=10, reason="익절"):
        us._us_pending_sell_meta[oid] = {
            "code": "AAPL", "name": "Apple", "excd": "NASD",
            "reason": reason, "qty": qty}

    # ── 정상 delta 부킹 ─────────────────────────────────────
    def test_01_buy_cumulative_delta(self):
        us = make_us(self.db)
        oid = "US_BUY_AAPL_1"
        self._buy_meta(us, oid)
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 5, 104.0, "BUY", 10))
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 10, 112.0, "BUY", 10))
        pos = us.pos_mgr.positions["AAPL"]
        self.assertEqual(pos.qty, 10)
        self.assertAlmostEqual(pos.avg_price, 112.0)
        deltas = [(e["qty"], e["price"]) for e in us._us_fill_events]
        self.assertEqual(deltas, [(3, 100.0), (2, 110.0), (5, 120.0)])

    def test_02_sell_cumulative_delta_pnl_reentry_once(self):
        us = make_us(self.db, reentry=FakeReentry())
        us.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 10, 100.0))
        oid = "US_SELL_AAPL_1"
        self._sell_meta(us, oid)
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 110.0, "SELL", 10))
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 5, 110.0, "SELL", 10))
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 10, 110.0, "SELL", 10))
        self.assertNotIn("AAPL", us.pos_mgr.positions)
        # delta PnL = (110-100)*qty*fx(1000): 3→30000, 2→20000, 5→50000
        self.assertEqual(us.pnl_guard.calls, [30000.0, 20000.0, 50000.0])
        self.assertEqual(len(us.reentry.sells), 1)   # 청산 1회

    def test_03_duplicate_poll_no_double(self):
        us = make_us(self.db)
        oid = "US_BUY_AAPL_1"
        self._buy_meta(us, oid)
        lc = FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10)
        us._us_apply_fill_delta(lc)
        us._us_apply_fill_delta(lc)   # 동일 누적 → last_cum 이후 없음
        us._us_apply_fill_delta(lc)
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 3)
        self.assertEqual(len(us._us_fill_events), 1)

    def test_04_partial_then_cancel_keeps_filled(self):
        us = make_us(self.db)
        oid = "US_BUY_AAPL_1"
        self._buy_meta(us, oid)
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))
        # 취소는 apply 를 호출하지 않음 → 체결분 3주 유지, outbox 3주 기록
        self.assertEqual(us.pos_mgr.positions["AAPL"].qty, 3)
        self.assertEqual(us._us_outbox.last_cum(oid), (3, 300.0))

    # ── crash point 재시작 안전성 ───────────────────────────
    def test_05_crash_after_pos_before_flag(self):
        """포지션 반영 직후, pos_done 저장 전 강제종료 → 재시작 시 이중부킹 없음."""
        us1 = make_us(self.db)
        oid = "US_BUY_AAPL_1"
        self._buy_meta(us1, oid)
        crash_on(us1, "pos_done")
        with self.assertRaises(RuntimeError):
            us1._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))
        # 이 시점 DB: outbox row 존재(pos_done=0), pos_mgr 에는 3주 반영됨
        self.assertEqual(us1.pos_mgr.positions["AAPL"].qty, 3)
        row = us1._us_outbox.get(_USFillOutbox.event_key(oid, 3))
        self.assertEqual(row["pos_done"], 0)
        # 재시작: 새 매니저, 동일 DB + durable pos_mgr, fresh pnl
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr,
                      pnl_guard=FakePnl(), reentry=FakeReentry())
        us2._us_replay_outbox()
        self.assertEqual(us2.pos_mgr.positions["AAPL"].qty, 3)   # 이중 반영 없음
        self.assertEqual(
            us2._us_outbox.get(_USFillOutbox.event_key(oid, 3))["pos_done"], 1)

    def test_06_crash_after_pnl_before_flag(self):
        """매도 PnL 기록 직후, pnl_done 저장 전 강제종료 → 재시작 시 실현손익 1회."""
        us1 = make_us(self.db)
        us1.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 10, 100.0))
        oid = "US_SELL_AAPL_1"
        self._sell_meta(us1, oid)
        crash_on(us1, "pnl_done")
        with self.assertRaises(RuntimeError):
            us1._us_apply_fill_delta(FakeLC(oid, "AAPL", 10, 110.0, "SELL", 10))
        # DB: pos_done=1, pnl_done=0. 구 guard(us1)는 폐기됨.
        row = us1._us_outbox.get(_USFillOutbox.event_key(oid, 10))
        self.assertEqual((row["pos_done"], row["pnl_done"]), (1, 0))
        # 재시작: fresh pnl guard
        fresh_pnl = FakePnl()
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr,
                      pnl_guard=fresh_pnl, reentry=FakeReentry())
        us2._us_replay_outbox()
        self.assertEqual(fresh_pnl.calls, [100000.0])   # (110-100)*10*1000, 정확히 1회

    def test_07_crash_after_reentry_before_flag(self):
        """전량매도 재진입 기록 직후, reentry_done 저장 전 강제종료 → 재진입 1회."""
        reentry = FakeReentry()   # durable
        us1 = make_us(self.db, reentry=reentry)
        us1.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 10, 100.0))
        oid = "US_SELL_AAPL_1"
        self._sell_meta(us1, oid)
        crash_on(us1, "reentry_done")
        with self.assertRaises(RuntimeError):
            us1._us_apply_fill_delta(FakeLC(oid, "AAPL", 10, 110.0, "SELL", 10))
        self.assertEqual(len(reentry.sells), 1)
        row = us1._us_outbox.get(_USFillOutbox.event_key(oid, 10))
        self.assertEqual(row["reentry_done"], 0)
        # 재시작: 동일(영속) reentry → check 가 이미 등록 감지 → 재등록 안 함
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr,
                      pnl_guard=FakePnl(), reentry=reentry)
        us2._us_replay_outbox()
        self.assertEqual(len(reentry.sells), 1)   # 정확히 1회
        self.assertEqual(
            us2._us_outbox.get(_USFillOutbox.event_key(oid, 10))["reentry_done"], 1)

    def test_08_crash_before_app_effect_redelivered_once(self):
        """outbox event 저장 후 app 부수효과 처리 전 강제종료 → 재시작 시 1회 재전달."""
        us1 = make_us(self.db)
        oid = "US_BUY_AAPL_1"
        self._buy_meta(us1, oid)
        us1._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))
        pend1 = us1._us_pending_app_events()
        self.assertEqual(len(pend1), 1)              # app 처리 대기 1건
        # app 이 처리 전 강제종료(us_mark_app_done 미호출) → 재시작
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr)
        us2._us_replay_outbox()
        pend2 = us2._us_pending_app_events()
        self.assertEqual(len(pend2), 1)              # 여전히 미처리 → 재전달
        ek = pend2[0]["event_key"]
        us2.us_mark_app_done(ek)                     # app 처리 확정
        self.assertEqual(len(us2._us_pending_app_events()), 0)   # 재전달 안 됨

    def test_09_completed_event_not_reprocessed(self):
        """모든 부수효과 완료된 event 는 재시작 replay 에서 재실행되지 않음."""
        us1 = make_us(self.db)
        oid = "US_BUY_AAPL_1"
        self._buy_meta(us1, oid)
        us1._us_apply_fill_delta(FakeLC(oid, "AAPL", 3, 100.0, "BUY", 10))
        for ev in us1._us_pending_app_events():
            us1.us_mark_app_done(ev["event_key"])
        # 재시작: durable pos_mgr (3주 유지), fresh pnl
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr, pnl_guard=FakePnl())
        us2._us_replay_outbox()
        self.assertEqual(us2.pos_mgr.positions["AAPL"].qty, 3)   # 중복 없음
        self.assertEqual(len(us2._us_fill_events), 0)            # 재이벤트 없음

    # ── 주문가능금액 사전검증 (Part 2/4) ────────────────────
    def test_10_prevalidation_failure_blocks(self):
        us = make_us(self.db)
        us.api.get_us_available_amounts.return_value = {"ok": False}
        res = us._do_buy("AAPL", "Apple", "NASD", 100.0,
                         {"session": "정규장"}, {"buy_score": 1.0})
        self.assertEqual(res["action"], "BUY_BLOCKED")
        us.api.buy_us.assert_not_called()

    def test_11_zero_overseas_available_blocks(self):
        us = make_us(self.db)
        us.api.get_us_available_amounts.return_value = {
            "ok": True, "usd": 0.0, "krw": 0.0}
        us.api.get_orderable_cash.return_value = 10_000_000   # 국내 예수금 존재
        res = us._do_buy("AAPL", "Apple", "NASD", 100.0,
                         {"session": "정규장"}, {"buy_score": 1.0})
        self.assertEqual(res["action"], "BUY_BLOCKED")
        us.api.buy_us.assert_not_called()

    def test_12_prevalidation_is_order_specific(self):
        """서로 다른 거래소·가격·종목마다 주문별(계좌·거래소·종목·주문가격) 조회."""
        us = make_us(self.db)
        us.api.get_us_available_amounts.return_value = {"ok": False}
        us._do_buy("TSLA", "Tesla", "NASD", 250.5,
                   {"session": "정규장"}, {"buy_score": 1.0})
        us.api.get_us_available_amounts.assert_called_with(
            symbol="TSLA", excd="NASD", ord_unpr=250.5)

    def test_13_inflight_guard(self):
        us = make_us(self.db)
        us._us_pending_buy_meta["b1"] = {"code": "AAPL"}
        self.assertTrue(us._us_has_active_order("AAPL", "BUY"))
        self.assertFalse(us._us_has_active_order("AAPL", "SELL"))
        self.assertFalse(us._us_has_active_order("TSLA", "BUY"))


if __name__ == "__main__":
    unittest.main()
