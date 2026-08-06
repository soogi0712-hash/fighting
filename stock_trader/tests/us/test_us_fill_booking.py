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
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from strategies.us_strategy_manager import (   # noqa: E402
    USStrategyManager, USPosition, _USFillOutbox, _USAppEffectLedger,
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
    us._us_app_ledger = _USAppEffectLedger(db_path)
    us.pnl_guard = pnl_guard if pnl_guard is not None else FakePnl()
    us.reentry = reentry if reentry is not None else MagicMock()
    if isinstance(us.reentry, MagicMock):
        us.reentry.check.return_value = (False, {})
    us.api = MagicMock()
    us.api.get_usd_exchange_rate.return_value = 1000.0
    for m in ("_us_apply_fill_delta", "_us_process_outbox_row", "_us_pos_add",
              "_us_pos_reduce", "_us_replay_outbox", "_us_pending_app_events",
              "us_apply_app_effects", "us_mark_app_done",
              "_us_has_active_order", "_do_buy"):
        setattr(us, m, getattr(USStrategyManager, m).__get__(us))
    return us


class DurableEffects:
    """재시작(새 매니저)에도 유지되는 effect 실행 기록. 각 fn 은 event_key 로
    멱등(이미 적용된 (event_key, effect_type) 는 no-op) → '실행 후 flag 저장 전
    crash 재실행' 에도 중복 없음(계약 검증용)."""
    def __init__(self):
        self.applied = {}     # (ek, et) -> 1  (실제 적용 1회)
        self.call_log = []    # 호출 이력(멱등 no-op 포함)

    def fn(self, effect_type):
        def _f(ek, ev):
            self.call_log.append((ek, effect_type))
            if (ek, effect_type) in self.applied:
                return          # 멱등 no-op
            self.applied[(ek, effect_type)] = 1
        return _f

    def applied_count(self, ek, et):
        return self.applied.get((ek, et), 0)


def crash_ledger_on(us, target_effect):
    """_us_app_ledger.mark(target_effect) 에서 예외 → effect 실행 후 flag 저장 전
    강제종료 모사."""
    orig = us._us_app_ledger.mark

    def failing(ek, et, day="", amount=0.0):
        if et == target_effect:
            raise RuntimeError(f"crash before mark {et}")
        return orig(ek, et, day, amount)
    us._us_app_ledger.mark = failing


def seed_fill(us, side="BUY"):
    """outbox 에 체결 1건을 반영해 app 처리 대기 event 를 만든다."""
    oid = f"US_{side}_AAPL_1"
    if side == "BUY":
        us._us_pending_buy_meta[oid] = {
            "code": "AAPL", "name": "Apple", "excd": "NASD", "level": 1, "qty": 10}
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 10, 100.0, "BUY", 10))
    else:
        us.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 10, 100.0))
        us._us_pending_sell_meta[oid] = {
            "code": "AAPL", "name": "Apple", "excd": "NASD",
            "reason": "익절", "qty": 10}
        us._us_apply_fill_delta(FakeLC(oid, "AAPL", 10, 110.0, "SELL", 10))
    return us._us_pending_app_events()[0]


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


class TestUSAppEffectsLedger(unittest.TestCase):
    """집계 effect(trade_count/pnl_stats) — effect 원장에서 결정론적 재구성.
    인메모리 아님 → 재시작해도 동일값(위험통제 유지). 실제 SQLite 상태 검증."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "us.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _day(self, us, ev):
        return ev.get("day", "") or ""

    def test_B1_trade_count_survives_restart(self):
        us1 = make_us(self.db)
        ev = seed_fill(us1, "BUY")
        us1.us_apply_app_effects(ev, _build_fns(us1, ev))
        day = self._day(us1, ev)
        self.assertEqual(us1.us_today_trade_count(day), 1)
        # 재시작: 객체 폐기, 동일 DB 새 매니저
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr)
        self.assertEqual(us2.us_today_trade_count(day), 1)   # 0 아님, 재구성됨

    def test_B2_pnl_survives_restart(self):
        us1 = make_us(self.db)
        # 매도 손실 −5000 (avg 100 → 95, 10주, fx 1000 → (95-100)*10*1000)
        oid = "US_SELL_AAPL_1"
        us1.pos_mgr.add(USPosition("AAPL", "Apple", "NASD", 10, 100.0))
        us1._us_pending_sell_meta[oid] = {
            "code": "AAPL", "name": "Apple", "excd": "NASD",
            "reason": "손절", "qty": 10}
        us1._us_apply_fill_delta(FakeLC(oid, "AAPL", 10, 95.0, "SELL", 10))
        ev = us1._us_pending_app_events()[0]
        us1.us_apply_app_effects(ev, _build_fns(us1, ev))
        day = ev["day"]
        self.assertEqual(us1.us_today_realized_krw(day), -50000.0)
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr)
        self.assertEqual(us2.us_today_realized_krw(day), -50000.0)   # 동일 −X

    def test_B3_redelivery_no_change(self):
        us = make_us(self.db)
        ev = seed_fill(us, "SELL")   # 익절 (110), pnl=+100000
        day = ev["day"]
        us.us_apply_app_effects(ev, _build_fns(us, ev))
        us.us_apply_app_effects(ev, _build_fns(us, ev))   # 재전달
        us.us_apply_app_effects(ev, _build_fns(us, ev))
        self.assertEqual(us.us_today_trade_count(day), 1)         # 불변
        self.assertEqual(us.us_today_realized_krw(day), 100000.0)  # 불변
        # 실제 SQLite 행 UNIQUE 확인
        import sqlite3
        c = sqlite3.connect(self.db)
        n = c.execute("SELECT COUNT(*) FROM us_app_effects "
                      "WHERE event_key=? AND effect_type='trade_count'",
                      (ev["event_key"],)).fetchone()[0]
        c.close()
        self.assertEqual(n, 1)

    def test_B4_repeated_restarts_stable(self):
        us = make_us(self.db)
        ev = seed_fill(us, "SELL")
        day = ev["day"]
        us.us_apply_app_effects(ev, _build_fns(us, ev))
        pos = us.pos_mgr
        for _ in range(3):   # 재시작 반복 + 재전달
            us = make_us(self.db, pos_mgr=pos)
            for e in us._us_pending_app_events():
                us.us_apply_app_effects(e, _build_fns(us, e))
            self.assertEqual(us.us_today_trade_count(day), 1)
            self.assertEqual(us.us_today_realized_krw(day), 100000.0)


class TestUSExternalEffectIdempotency(unittest.TestCase):
    """외부 effect(on_sell_complete/watch_entry) — 실제 JSON 영속 상태로 멱등 검증.
    now() 아닌 결정론적 event_time + event_key 로 재실행에도 만료시각·집계 불변."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "us.db")
        self.jdir = os.path.join(self.tmp, "json")
        os.makedirs(self.jdir, exist_ok=True)
        import screener.us_watchlist_manager as wm
        self.wm = wm
        self._patches = []
        for name in ("WATCH_PERF_FILE", "DAILY_PROFIT_FILE", "PENALTY_FILE",
                     "COOLDOWN_FILE", "RECENT_LOSS_FILE"):
            p = patch.object(wm, name,
                             os.path.join(self.jdir, name.lower() + ".json"))
            p.start()
            self._patches.append(p)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _real_fns(self, ev):
        """실제 us_watchlist_manager 함수를 external_fns 로 구성(app.py 와 동일)."""
        wm = self.wm
        _sym = ev["symbol"]; _price = ev["price"]
        _pnl = ev["pnl_krw"]; _et = ev.get("event_time")

        def _watch(ek, e):
            wm.record_watch_entry(_sym, _price, source="trade",
                                  entered_at=_et, event_key=ek)

        def _osc(ek, e):
            wm.on_sell_complete(_sym, _pnl, event_key=ek, event_time=_et)

        if ev["side"] == "BUY":
            return {"watch_entry": _watch}
        return {"on_sell_complete": _osc} if ev.get("is_full") else {}

    def test_C1_on_sell_complete_crash_before_mark_no_extension(self):
        """on_sell_complete 실행 후 mark 전 종료 → 재시작 후 쿨다운 만료시각 불변,
        중복 집계 없음(실제 JSON 검증)."""
        us1 = make_us(self.db)
        ev = seed_fill(us1, "SELL")   # 익절 → 쿨다운 등록
        ek = ev["event_key"]
        crash_ledger_on(us1, "on_sell_complete")
        with self.assertRaises(RuntimeError):
            us1.us_apply_app_effects(ev, self._real_fns(ev))
        # on_sell_complete 는 실행됨(JSON 기록), ledger 미mark
        cooldown1 = self.wm._load_json(self.wm.COOLDOWN_FILE)
        perf1     = self.wm._load_json(self.wm.WATCH_PERF_FILE)
        self.assertEqual(cooldown1.get("AAPL"), ev["event_time"])   # 결정론적 시각
        self.assertEqual(perf1["AAPL"]["trade_count"], 1)
        self.assertFalse(us1._us_app_ledger.done(ek, "on_sell_complete"))
        # 재시작: 재전달 → on_sell_complete 재실행(같은 event_time/event_key)
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr)
        e2 = us2._us_pending_app_events()[0]
        us2.us_apply_app_effects(e2, self._real_fns(e2))
        cooldown2 = self.wm._load_json(self.wm.COOLDOWN_FILE)
        perf2     = self.wm._load_json(self.wm.WATCH_PERF_FILE)
        self.assertEqual(cooldown2.get("AAPL"), ev["event_time"])   # 만료시각 불변(연장X)
        self.assertEqual(perf2["AAPL"]["trade_count"], 1)           # 중복 집계 없음
        self.assertTrue(us2._us_app_ledger.done(ek, "on_sell_complete"))

    def test_C2_watch_entry_crash_before_mark_no_duplicate(self):
        """watch_entry 실행 후 mark 전 종료 → 재시작 후 중복 항목 없음, 시각 불변."""
        us1 = make_us(self.db)
        ev = seed_fill(us1, "BUY")
        ek = ev["event_key"]
        crash_ledger_on(us1, "watch_entry")
        with self.assertRaises(RuntimeError):
            us1.us_apply_app_effects(ev, self._real_fns(ev))
        perf1 = self.wm._load_json(self.wm.WATCH_PERF_FILE)
        self.assertIn("AAPL", perf1)
        self.assertEqual(perf1["AAPL"]["entered_at"], ev["event_time"])
        # 재시작 재전달
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr)
        e2 = us2._us_pending_app_events()[0]
        us2.us_apply_app_effects(e2, self._real_fns(e2))
        perf2 = self.wm._load_json(self.wm.WATCH_PERF_FILE)
        self.assertEqual(list(perf2.keys()), ["AAPL"])              # 중복 항목 없음
        self.assertEqual(perf2["AAPL"]["entered_at"], ev["event_time"])  # 시각 불변
        self.assertTrue(us2._us_app_ledger.done(ek, "watch_entry"))

    def test_C3_repeated_restart_external_stable(self):
        """재시작을 반복해도 쿨다운/성과 JSON 최종 상태 동일."""
        us = make_us(self.db)
        ev = seed_fill(us, "SELL")
        us.us_apply_app_effects(ev, self._real_fns(ev))
        pos = us.pos_mgr
        for _ in range(3):
            us = make_us(self.db, pos_mgr=pos)
            for e in us._us_pending_app_events():
                us.us_apply_app_effects(e, self._real_fns(e))
        cooldown = self.wm._load_json(self.wm.COOLDOWN_FILE)
        perf     = self.wm._load_json(self.wm.WATCH_PERF_FILE)
        self.assertEqual(cooldown.get("AAPL"), ev["event_time"])
        self.assertEqual(perf["AAPL"]["trade_count"], 1)


class TestUSAppDoneGating(unittest.TestCase):
    """모든 effect 완료 후에만 app_done 확정 + effect 간 crash 재개."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "us.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_D1_crash_between_effects_resume(self):
        """외부 effect 처리 중 종료 → 재시작 시 집계는 재실행 없이 외부만 이어서."""
        eff = DurableEffects()
        us1 = make_us(self.db)
        ev = seed_fill(us1, "BUY")   # effects: trade_count(집계) + watch_entry(외부)
        ek = ev["event_key"]
        day = ev["day"]

        def failing_watch(ekey, evt):
            raise RuntimeError("crash during watch_entry")
        us1.us_apply_app_effects(ev, {"watch_entry": failing_watch})
        self.assertEqual(us1.us_today_trade_count(day), 1)       # 집계는 완료
        self.assertFalse(us1._us_app_ledger.done(ek, "watch_entry"))
        self.assertEqual(us1._us_outbox.get(ek)["app_done"], 0)  # 미확정
        # 재시작: watch_entry 만 이어서, 집계 재실행 없음(원장 UNIQUE)
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr)
        e2 = us2._us_pending_app_events()[0]
        us2.us_apply_app_effects(e2, {"watch_entry": eff.fn("watch_entry")})
        self.assertEqual(us2.us_today_trade_count(day), 1)       # 불변
        self.assertEqual(eff.applied_count(ek, "watch_entry"), 1)
        self.assertEqual(us2._us_outbox.get(ek)["app_done"], 1)  # 이제 확정
        self.assertEqual(len(us2._us_pending_app_events()), 0)

    def test_D2_all_effects_then_app_done_before_flag(self):
        """모든 effect 완료 후 app_done 저장 전 종료 → 재시작 시 재실행 없이 확정."""
        us1 = make_us(self.db)
        ev = seed_fill(us1, "BUY")
        ek = ev["event_key"]
        day = ev["day"]
        orig = us1._us_outbox.set_flag

        def skip_app_done(ekey, flag):
            if flag == "app_done":
                return
            return orig(ekey, flag)
        us1._us_outbox.set_flag = skip_app_done
        us1.us_apply_app_effects(ev, {"watch_entry": lambda a, b: None})
        self.assertEqual(us1._us_outbox.get(ek)["app_done"], 0)
        us2 = make_us(self.db, pos_mgr=us1.pos_mgr)
        us2.us_apply_app_effects(us2._us_pending_app_events()[0],
                                 {"watch_entry": lambda a, b: None})
        self.assertEqual(us2.us_today_trade_count(day), 1)       # 재집계 없음
        self.assertEqual(us2._us_outbox.get(ek)["app_done"], 1)


def _build_fns(us, ev):
    """집계 전용 event 는 external_fns 없이(watch/on_sell 은 no-op) 전달."""
    if ev["side"] == "BUY":
        return {"watch_entry": lambda a, b: None}
    return {"on_sell_complete": (lambda a, b: None)} if ev.get("is_full") else {}


if __name__ == "__main__":
    unittest.main()
