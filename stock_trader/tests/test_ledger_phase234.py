"""
test_ledger_phase234.py — PHASE 1~4 검증 (오프라인, 실 KIS API 미호출)

검증:
  - 체결 기반 기록 (fill_source 없으면 접수를 체결로 기록하지 않음)
  - 부분체결(요청≠체결) → 실제 체결수량/체결가로 기록
  - 중복체결 방지 (side, order_no) 멱등
  - ledger_health 실패 집계/스냅샷
  - 재시작 정합성 reconcile
  - trade_log↔ledger 정합성 check_consistency
  - LIVE_ORDER_ENABLED=false → 주문 API 가 네트워크 호출 없이 차단
  - 저장된 fixture 기반 리플레이

실행: (stock_trader 에서)  python3 tests/test_ledger_phase234.py
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger.recorder import LedgerRecorder
from ledger.fills import Fill, MockFillSource
from ledger.health import LedgerHealth, check_consistency
from ledger.wiring import record_trade_event, reconcile
from screener.transaction_cost import calc_trade_result

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "replay_trades.json")


def _rec():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    return LedgerRecorder(db_path=tmp.name)


def _row(rec, code, market="KR"):
    return rec.conn.execute(
        "SELECT * FROM trades WHERE market=? AND code=? ORDER BY id DESC LIMIT 1",
        (market, code)).fetchone()


def test_no_fill_source_does_not_record():
    """fill_source 없으면 접수를 체결로 기록하지 않는다(안전)."""
    rec = _rec(); h = LedgerHealth()
    st = record_trade_event(rec, h, {"action": "BUY", "code": "005930",
                                     "price": 70000, "qty": 100}, "KR", None)
    assert st == "no_fill_source"
    assert rec.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    print("✓ no fill_source → 미기록(접수를 체결로 기록하지 않음)")


def test_fill_based_uses_actual_price_qty():
    """요청가/요청수량이 아니라 실제 체결가/체결수량으로 기록."""
    rec = _rec(); h = LedgerHealth()
    fs = MockFillSource()
    # 요청 100@70000 이지만 실제 체결은 100@70120 (슬리피지 반영된 실체결가)
    fs.add("KR", "005930", "BUY", [Fill(order_no="ODNO-1", qty=100, price=70120)])
    st = record_trade_event(rec, h, {"action": "BUY", "code": "005930", "name": "삼성전자",
                                     "price": 70000, "qty": 100, "decision_price": 69900}, "KR", fs)
    assert st == "recorded"
    r = _row(rec, "005930")
    assert r["avg_entry_price"] == 70120 and r["entry_qty_total"] == 100
    assert r["entry_order_no"] == "ODNO-1"       # 실제 주문번호 사용
    assert abs(r["entry_slippage"] - (70120 - 69900)) < 1e-6
    print("✓ 체결 기반: 실제 체결가 70120·실주문번호 기록")


def test_partial_fill_recorded_as_filled_qty():
    """요청 100 이지만 60만 체결 → 60으로 기록(OPEN 유지)."""
    rec = _rec(); h = LedgerHealth()
    fs = MockFillSource()
    fs.add("KR", "005930", "BUY", [Fill(order_no="O1", qty=60, price=70000)])   # 부분체결
    record_trade_event(rec, h, {"action": "BUY", "code": "005930", "price": 70000, "qty": 100}, "KR", fs)
    r = _row(rec, "005930")
    assert r["entry_qty_total"] == 60     # 요청 100 아님
    print("✓ 부분체결: 요청100→체결60 으로 기록")


def test_no_fill_confirmed_counts_health():
    """체결 확인 실패(빈 목록) → 미기록 + health 실패 집계."""
    rec = _rec(); h = LedgerHealth()
    fs = MockFillSource()   # 아무 체결도 넣지 않음 → get_fills=[]
    st = record_trade_event(rec, h, {"action": "SELL", "code": "005930", "price": 71000, "qty": 100}, "KR", fs)
    assert st == "no_fill"
    assert rec.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    snap = h.snapshot()
    assert snap["fail_count"] == 1 and snap["last_error"]["action"] == "SELL"
    print("✓ 체결 미확인 → 미기록 + health.fail_count=1")


def test_duplicate_fill_idempotent():
    """동일 (side, order_no) 체결 두 번 → 한 번만 반영."""
    rec = _rec(); h = LedgerHealth()
    fs = MockFillSource()
    fs.add("KR", "005930", "BUY", [Fill(order_no="DUP", qty=100, price=70000)])
    fs.add("KR", "005930", "BUY", [Fill(order_no="DUP", qty=100, price=70000)])
    record_trade_event(rec, h, {"action": "BUY", "code": "005930", "price": 70000, "qty": 100}, "KR", fs)
    record_trade_event(rec, h, {"action": "BUY", "code": "005930", "price": 70000, "qty": 100}, "KR", fs)
    r = _row(rec, "005930")
    assert r["entry_qty_total"] == 100 and r["entry_fill_count"] == 1
    print("✓ 중복체결(DUP) 멱등 — 1회만 반영")


def test_reconcile_detects_mismatch():
    """ledger OPEN vs 브로커 보유 대조."""
    rec = _rec(); h = LedgerHealth()
    fs = MockFillSource()
    fs.add("KR", "005930", "BUY", [Fill(order_no="A", qty=100, price=70000)])
    record_trade_event(rec, h, {"action": "BUY", "code": "005930", "price": 70000, "qty": 100}, "KR", fs)
    # 브로커에는 005930 90주(수량 불일치) + 000660(미추적) 보유
    rep = reconcile(rec, {"005930": 90, "000660": 10}, "KR")
    assert rep["qty_mismatch"]["005930"] == {"ledger": 100, "broker": 90}
    assert rep["broker_only"] == ["000660"]
    assert rep["consistent"] is False
    print("✓ reconcile: 수량불일치 + 미추적 포지션 탐지")


def test_consistency_check_counts():
    """ledger CLOSED vs trade_log SELL 건수 비교."""
    rec = _rec(); h = LedgerHealth()
    fs = MockFillSource()
    fs.add("KR", "X", "BUY",  [Fill(order_no="b", qty=10, price=100)])
    fs.add("KR", "X", "SELL", [Fill(order_no="s", qty=10, price=110)])
    record_trade_event(rec, h, {"action": "BUY",  "code": "X", "price": 100, "qty": 10}, "KR", fs)
    record_trade_event(rec, h, {"action": "SELL", "code": "X", "price": 110, "qty": 10}, "KR", fs)
    # trade_log 에 SELL 1건이 있는 임시 파일
    tmplog = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False)
    json.dump([{"action": "BUY"}, {"action": "SELL"}], tmplog); tmplog.close()
    res = check_consistency(rec, tmplog.name)
    assert res["ledger_closed"] == 1 and res["log_sell"] == 1 and res["match"] is True
    os.unlink(tmplog.name)
    print("✓ 정합성: ledger CLOSED=1 == trade_log SELL=1")


def test_replay_from_fixture():
    """저장된 fixture(trade_log 형태) + mock 체결 리플레이 → 최종 CLOSED 검증."""
    rec = _rec(); h = LedgerHealth()
    entries = json.load(open(FIX))
    fs = MockFillSource()
    # 각 항목을 '요청=체결'로 가정한 mock 체결로 리플레이
    for i, e in enumerate(entries):
        side = "SELL" if e["action"].startswith("SELL") else "BUY"
        fs.add("KR", e["code"], side, [Fill(order_no=f"F{i}", qty=e["qty"], price=e["price"])])
    for e in entries:
        record_trade_event(rec, h, e, "KR", fs)
    r = _row(rec, "005930")
    avg_exit = (60 * 72000 + 90 * 71000) / 150
    exp = calc_trade_result(70500, 150, avg_exit)   # 평균진입 (100*70000+50*71500)/150=70500
    assert r["status"] == "CLOSED"
    assert abs(r["avg_entry_price"] - 70500) < 1e-6
    assert abs(r["net_pnl"] - exp.net_profit) < 1e-6
    assert h.snapshot()["fail_count"] == 0
    print(f"✓ 리플레이: CLOSED, avg_entry=70500, net_pnl={r['net_pnl']}")


def test_live_order_gate_blocks_without_network():
    """LIVE_ORDER_ENABLED=false → 주문 API 가 네트워크 호출 없이 차단."""
    import api.kis_api as kmod
    from config import Config
    assert Config.LIVE_ORDER_ENABLED is False   # 기본 OFF

    # requests.post/get 이 호출되면 즉시 실패 → '네트워크 미호출' 증명
    class _Boom:
        def __getattr__(self, _):
            raise AssertionError("네트워크 호출 발생! (차단 실패)")
    orig = kmod.requests
    kmod.requests = _Boom()
    try:
        api = kmod.KISApi()
        # 키 사용 방지 위해 즉시 더미로 덮어씀(값 유출/사용 방지)
        api.app_key = api.app_secret = api.account_no = "DUMMY"
        r1 = api.buy("005930", 1, 70000)
        r2 = api.sell("005930", 1, 70000)
        r3 = api.buy_us("AAPL", 1, 200.0)
        r4 = api.sell_us("AAPL", 1, 200.0)
        r5 = api.cancel_order("0", "005930", 1, 70000)
        for r in (r1, r2, r3, r4, r5):
            assert r.get("rt_cd") == "9" and r.get("_blocked") is True
    finally:
        kmod.requests = orig
    print("✓ LIVE_ORDER_ENABLED=false: KR/US 매수·매도·취소 전부 차단(네트워크 미호출)")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n=== {len(fns)}/{len(fns)} passed ===")


if __name__ == "__main__":
    _run_all()
