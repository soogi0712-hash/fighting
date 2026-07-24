"""
보완 #3-1 검증: fill_source 주입 + order_no 전파
================================================

범위(승인된 것만):
  1) StrategyManager 가 KisFillSource(api) 를 기본 주입한다.
  2) LIVE_ORDER_ENABLED=false 에서 KisFillSource 는 실 API(get_order_history)
     를 절대 호출하지 않고 빈 목록을 반환한다(증명).
     - 대조: LIVE=true 로 두면 게이트가 유일한 차단막임을 확인.
  3) 주문응답에서 order_no(odno) 를 방어적으로 추출한다.
  4) order_no 가 _log_trade → record_trade_event → fill_source.get_fills(order_hint)
     로 전파된다.
  5) fill_source=None 이면 접수를 체결로 기록하지 않는다(안전 기본값 유지).
  6) MockFillSource 주입 시 '실제 체결값'으로 원장에 기록된다(접수값 아님).

원칙 준수:
  - 전략 로직·BUY/SELL 조건·apply_buy/apply_sell 호출 방식 미변경(본 테스트는 배관만 검증).
  - 실주문 없음. LIVE_ORDER_ENABLED 기본 false 유지(대조 테스트만 try/finally 로 임시 토글 후 복원).
"""

import os
import sys
import tempfile

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

from config import Config
from ledger.recorder import LedgerRecorder
from ledger.fills import Fill, MockFillSource, KisFillSource
from ledger.health import LedgerHealth
from ledger.wiring import record_trade_event
import strategies.pyramid_strategy as ps
import strategies.strategy_manager as sm
from strategies.strategy_manager import StrategyManager


# ── 더미/스파이 API ────────────────────────────────────────────
class _DummyApi:
    """StrategyManager 생성용 최소 스텁 (네트워크 없음)."""
    def get_order_history(self, days=1):
        return []


class _SpyApi:
    """get_order_history 호출 시 카운트 + 예외 → '호출됨'을 확실히 드러냄."""
    def __init__(self):
        self.history_calls = 0

    def get_order_history(self, days=1):
        self.history_calls += 1
        return [{"code": "005930", "type": "매수", "qty": 10,
                 "amount": 700_000, "price": 70_000,
                 "order_no": "ODN-1", "time": "0900"}]


class _SpyFillSource:
    """order_hint 전파 확인용. 아무 체결도 반환하지 않음(원장 무기록)."""
    def __init__(self):
        self.last = None

    def get_fills(self, market, code, side, requested_qty=None,
                  requested_price=None, order_hint=None, ts=None):
        self.last = {"market": market, "code": code, "side": side,
                     "order_hint": order_hint,
                     "requested_qty": requested_qty,
                     "requested_price": requested_price}
        return []


def _mgr(tmpdir):
    """데이터 파일을 임시경로로 우회한 StrategyManager."""
    ps.PYRAMID_FILE  = os.path.join(tmpdir, "pyramid.json")
    ps.COMPOUND_FILE = os.path.join(tmpdir, "compound.json")
    sm.TRADE_LOG_FILE = os.path.join(tmpdir, "trade_log.json")
    return StrategyManager(_DummyApi())


# ── 1. KisFillSource 기본 주입 ──────────────────────────────────
def test_kis_fill_source_injected_by_default():
    with tempfile.TemporaryDirectory() as d:
        mgr = _mgr(d)
        assert isinstance(mgr._fill_source, KisFillSource), \
            "StrategyManager 는 KisFillSource 를 기본 주입해야 함"


# ── 2. LIVE=false 에서 실 API 미호출 증명 ───────────────────────
def test_no_real_api_call_when_live_false():
    assert Config.LIVE_ORDER_ENABLED is False, "테스트 전제: 기본 false"
    spy = _SpyApi()
    src = KisFillSource(spy)
    fills = src.get_fills("KR", "005930", "BUY",
                          requested_qty=10, requested_price=70_000)
    assert fills == [], "LIVE=false 면 빈 목록 반환"
    assert spy.history_calls == 0, \
        "LIVE=false 에서 get_order_history 가 호출되면 안 됨(실 API 미호출 증명)"


def test_gate_is_the_only_blocker_when_live_true():
    """대조: LIVE=true 로 두면 게이트가 열려 실제로 API 를 호출함(게이트가 유일 차단막)."""
    spy = _SpyApi()
    src = KisFillSource(spy)
    original = Config.LIVE_ORDER_ENABLED
    try:
        Config.LIVE_ORDER_ENABLED = True
        fills = src.get_fills("KR", "005930", "BUY",
                              requested_qty=10, requested_price=70_000)
        assert spy.history_calls == 1, "LIVE=true 면 실 API 를 호출"
        assert len(fills) == 1 and fills[0].qty == 10
    finally:
        Config.LIVE_ORDER_ENABLED = original   # ★ 반드시 false 복원
    assert Config.LIVE_ORDER_ENABLED is False, "테스트 후 false 복원 확인"


# ── 3. order_no(odno) 방어적 추출 ───────────────────────────────
def test_extract_order_no_variants():
    f = StrategyManager._extract_order_no
    assert f({"output": {"ODNO": "0000117"}}) == "0000117"
    assert f({"output": {"odno": "123"}}) == "123"
    assert f({"ODNO": "999"}) == "999"
    assert f({"order_no": "abc"}) == "abc"
    # 무효값/부재 → None
    assert f({"output": {"ODNO": "0"}}) is None    # "0" 은 무효 취급
    assert f({"output": {"ODNO": ""}}) is None
    assert f({}) is None
    assert f(None) is None
    assert f("not-a-dict") is None


# ── 4. order_no 가 fill_source.get_fills 로 전파 ────────────────
def test_order_no_propagates_to_fill_source():
    with tempfile.TemporaryDirectory() as d:
        mgr = _mgr(d)
        spy = _SpyFillSource()
        mgr.set_fill_source(spy)
        # 지연초기화 원장을 임시 db 로 강제(실데이터 미접근)
        mgr._ledger = LedgerRecorder(db_path=os.path.join(d, "ledger.db"))
        mgr._log_trade("BUY", "005930", "삼성전자", 70_000, 10,
                       "테스트 매수", "정규장",
                       extra={"order_no": "ODN-XYZ"})
        assert spy.last is not None, "fill_source.get_fills 가 호출돼야 함"
        assert spy.last["order_hint"] == "ODN-XYZ", \
            "order_no 가 order_hint 로 전파돼야 함"
        assert spy.last["side"] == "BUY"


# ── 5. fill_source=None → 접수를 체결로 기록하지 않음 ───────────
def test_none_fill_source_records_nothing():
    with tempfile.TemporaryDirectory() as d:
        rec = LedgerRecorder(db_path=os.path.join(d, "l.db"))
        h = LedgerHealth()
        st = record_trade_event(rec, h,
                                {"action": "BUY", "code": "005930",
                                 "price": 70_000, "qty": 10}, "KR", None)
        assert st == "no_fill_source"
        rows = rec.conn.execute("SELECT COUNT(*) c FROM trades").fetchone()
        assert rows["c"] == 0, "체결 확인 소스 없으면 원장 무기록"


# ── 6. MockFillSource → 실제 체결값으로 기록(접수값 아님) ───────
def test_mock_fill_source_records_actual_fill_values():
    with tempfile.TemporaryDirectory() as d:
        rec = LedgerRecorder(db_path=os.path.join(d, "l.db"))
        h = LedgerHealth()
        fs = MockFillSource()
        # 접수는 100주@70000 이지만 실제 체결은 60주@70120(부분체결·다른가)
        fs.add("KR", "005930", "BUY", [Fill(order_no="ODN-9", qty=60, price=70_120)])
        st = record_trade_event(rec, h,
                                {"action": "BUY", "code": "005930", "name": "삼성전자",
                                 "price": 70_000, "qty": 100, "order_no": "ODN-9"},
                                "KR", fs)
        assert st == "recorded"
        row = rec.conn.execute(
            "SELECT entry_qty_total FROM trades WHERE code='005930'").fetchone()
        assert row["entry_qty_total"] == 60, \
            "원장은 접수(100)가 아닌 실제 체결수량(60)으로 기록돼야 함"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
