"""
test_ledger_recorder.py — 실거래 원장 오프라인 테스트

- 가짜 체결(fake fill) 데이터만 사용. 실주문/실API 호출 없음.
- 임시 ledger.db 파일 사용. 실제 data/ledger.db·기존 DB 는 건드리지 않음.
- 검증: 평균가 집계, 피라미딩, 부분청산, MFE/MAE, OPEN→PARTIAL→CLOSED,
        net_pnl(=calc_trade_result), 슬리피지 별도저장(net 미반영), 멱등성, 무결성 제약.
실행: (stock_trader 디렉터리에서)  python3 -m pytest tests/test_ledger_recorder.py -v
      또는                          python3 tests/test_ledger_recorder.py
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger.recorder import LedgerRecorder
from screener.transaction_cost import calc_trade_result


def _fresh():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return LedgerRecorder(db_path=tmp.name), tmp.name


def _row(rec, code, market="KR"):
    return rec.conn.execute(
        "SELECT * FROM trades WHERE market=? AND code=? ORDER BY id DESC LIMIT 1",
        (market, code)).fetchone()


def test_simple_roundtrip():
    rec, _ = _fresh()
    rec.on_buy_fill("KR", "005930", "삼성전자", "OB1", 10000, 100, entry_reason="매수신호")
    r = _row(rec, "005930")
    assert r["status"] == "OPEN"
    assert r["avg_entry_price"] == 10000 and r["entry_qty_total"] == 100
    assert r["entry_fill_count"] == 1

    rec.on_sell_fill("KR", "005930", "OS1", 11000, 100, exit_reason="익절")
    r = _row(rec, "005930")
    exp = calc_trade_result(10000, 100, 11000)
    assert r["status"] == "CLOSED"
    assert abs(r["realized_pnl"] - exp.gross_profit) < 1e-6
    assert abs(r["net_pnl"] - exp.net_profit) < 1e-6
    assert abs(r["fees"] - (exp.buy_cost.commission + exp.sell_proceeds.commission)) < 1e-6
    assert abs(r["tax"] - exp.sell_proceeds.transaction_tax) < 1e-6
    assert r["exit_qty_total"] == 100 and r["exit_fill_count"] == 1
    print("✓ simple roundtrip: net_pnl =", r["net_pnl"])


def test_pyramiding_avg():
    rec, _ = _fresh()
    rec.on_buy_fill("KR", "000660", "SK하이닉스", "B1", 10000, 100)
    rec.on_buy_fill("KR", "000660", "SK하이닉스", "B2", 12000, 100)   # 추가매수
    r = _row(rec, "000660")
    assert r["entry_qty_total"] == 200
    assert abs(r["avg_entry_price"] - 11000) < 1e-6   # (100*10000+100*12000)/200
    assert r["entry_fill_count"] == 2

    rec.on_sell_fill("KR", "000660", "S1", 13000, 200, exit_reason="익절")
    r = _row(rec, "000660")
    exp = calc_trade_result(11000, 200, 13000)
    assert r["status"] == "CLOSED"
    assert abs(r["net_pnl"] - exp.net_profit) < 1e-6
    print("✓ pyramiding avg=11000, net_pnl =", r["net_pnl"])


def test_partial_exit_lifecycle():
    rec, _ = _fresh()
    rec.on_buy_fill("KR", "035420", "NAVER", "B1", 10000, 100)
    rec.on_sell_fill("KR", "035420", "S1", 11000, 40, exit_reason="분할익절1")
    r = _row(rec, "035420")
    assert r["status"] == "PARTIAL"
    assert r["exit_qty_total"] == 40 and r["net_pnl"] is None   # 미확정

    rec.on_sell_fill("KR", "035420", "S2", 12000, 60, exit_reason="분할익절2")
    r = _row(rec, "035420")
    avg_exit = (40 * 11000 + 60 * 12000) / 100      # 11600
    exp = calc_trade_result(10000, 100, avg_exit)
    assert r["status"] == "CLOSED"
    assert abs(r["avg_exit_price"] - avg_exit) < 1e-6
    assert abs(r["net_pnl"] - exp.net_profit) < 1e-6
    assert r["exit_fill_count"] == 2
    print("✓ partial exit: OPEN→PARTIAL→CLOSED, avg_exit=11600, net_pnl =", r["net_pnl"])


def test_mfe_mae_ticks():
    rec, _ = _fresh()
    rec.on_buy_fill("KR", "005930", "삼성전자", "B1", 10000, 100)
    rec.on_tick("KR", "005930", 10500)   # +5%
    rec.on_tick("KR", "005930", 9000)    # -10%
    rec.on_tick("KR", "005930", 10000)   # 0
    r = _row(rec, "005930")
    assert abs(r["mfe_pct"] - 5.0) < 1e-6
    assert abs(r["mae_pct"] - (-10.0)) < 1e-6
    print("✓ MFE/MAE: mfe=+5%, mae=-10%")


def test_slippage_separate_from_net():
    rec, _ = _fresh()
    # 진입: 신호가 9900, 체결 10000 (슬리피지 +100) / 청산: 신호가 11100, 체결 11000 (슬리피지 -100)
    rec.on_buy_fill("KR", "005930", "삼성전자", "B1", 10000, 100, decision_price=9900)
    rec.on_sell_fill("KR", "005930", "S1", 11000, 100, decision_price=11100)
    r = _row(rec, "005930")
    exp = calc_trade_result(10000, 100, 11000)     # net 은 실제 체결가 기준
    assert abs(r["net_pnl"] - exp.net_profit) < 1e-6    # 슬리피지 재차감 안 됨
    assert abs(r["entry_slippage"] - 100) < 1e-6        # 10000 - 9900
    assert abs(r["exit_slippage"] - (-100)) < 1e-6      # 11000 - 11100
    print("✓ slippage stored separately, net_pnl unaffected =", r["net_pnl"])


def test_idempotent_duplicate_buy():
    rec, _ = _fresh()
    rec.on_buy_fill("KR", "005930", "삼성전자", "B1", 10000, 100)
    rec.on_buy_fill("KR", "005930", "삼성전자", "B1", 10000, 100)  # 동일 주문 재기록(재시도)
    r = _row(rec, "005930")
    assert r["entry_qty_total"] == 100 and r["entry_fill_count"] == 1
    print("✓ idempotent: duplicate buy order ignored")


def test_us_market_same_api():
    rec, _ = _fresh()
    rec.on_buy_fill("US", "AAPL", "Apple", "UB1", 200, 10)
    rec.on_sell_fill("US", "AAPL", "US1", 210, 10, exit_reason="익절")
    r = _row(rec, "AAPL", market="US")
    exp = calc_trade_result(200, 10, 210)
    assert r["status"] == "CLOSED" and abs(r["net_pnl"] - exp.net_profit) < 1e-6
    print("✓ US uses same API, net_pnl =", r["net_pnl"])


def test_reentry_new_row_after_close():
    rec, _ = _fresh()
    rec.on_buy_fill("KR", "005930", "삼성전자", "B1", 10000, 100)
    rec.on_sell_fill("KR", "005930", "S1", 11000, 100)
    rec.on_buy_fill("KR", "005930", "삼성전자", "B2", 9000, 50)   # 청산 후 재진입 = 새 행
    rows = rec.conn.execute("SELECT status FROM trades WHERE code='005930' ORDER BY id").fetchall()
    assert [x["status"] for x in rows] == ["CLOSED", "OPEN"]
    print("✓ reentry after close creates new row")


def test_integrity_constraints():
    rec, _ = _fresh()
    # qty<=0 → recorder ValueError
    try:
        rec.on_buy_fill("KR", "005930", "삼성전자", "B1", 10000, 0)
        assert False, "expected ValueError"
    except ValueError:
        pass
    # market CHECK 위반 → sqlite IntegrityError
    try:
        rec.conn.execute(
            "INSERT INTO trades(market, code, status, entry_order_no, entry_qty_total) "
            "VALUES ('JP','X','OPEN','Z',10)")
        rec.conn.commit()
        assert False, "expected IntegrityError (market CHECK)"
    except sqlite3.IntegrityError:
        rec.conn.rollback()
    # 중복 entry_order_no (동일 시장) → UNIQUE 위반
    rec.on_buy_fill("KR", "111111", "A", "DUP", 100, 1)
    try:
        rec.conn.execute(
            "INSERT INTO trades(market, code, status, entry_order_no, entry_qty_total, avg_entry_price) "
            "VALUES ('KR','222222','OPEN','DUP',10,100)")
        rec.conn.commit()
        assert False, "expected IntegrityError (unique entry_order_no)"
    except sqlite3.IntegrityError:
        rec.conn.rollback()
    print("✓ integrity: qty>0, market CHECK, unique(market,entry_order_no)")


def test_integration_record_from_log():
    """기존 trade_log 항목(dict) → record_from_log → 원장 이중기록 경로 검증."""
    from ledger.integration import record_from_log
    rec, _ = _fresh()
    # KR 매수 항목(_log_trade extra 포함 형태)
    buy_entry = {
        "timestamp": "2026-07-23T10:00:00", "action": "BUY", "session": "KR정규장",
        "code": "005930", "name": "삼성전자", "price": 10000, "qty": 100,
        "reason": "매수신호", "buy_score": 0.7, "ind_score": 5, "level": 1,
    }
    assert record_from_log(rec, buy_entry, "KR") is True
    r = _row(rec, "005930")
    assert r["status"] == "OPEN" and r["entry_qty_total"] == 100
    assert r["entry_indicators_json"] and "buy_score" in r["entry_indicators_json"]

    # KR 매도 항목
    sell_entry = {
        "timestamp": "2026-07-23T10:40:00", "action": "SELL", "session": "KR정규장",
        "code": "005930", "name": "삼성전자", "price": 11000, "qty": 100, "reason": "익절",
    }
    assert record_from_log(rec, sell_entry, "KR") is True
    r = _row(rec, "005930")
    exp = calc_trade_result(10000, 100, 11000)
    assert r["status"] == "CLOSED" and abs(r["net_pnl"] - exp.net_profit) < 1e-6

    # 대상 아님(HOLD) → False
    assert record_from_log(rec, {"action": "HOLD", "code": "X", "price": 1, "qty": 1}, "KR") is False
    # order_no 폴백(항목에 order_no 없음)에도 정상 기록됨
    assert r["entry_order_no"].startswith("005930-")
    print("✓ integration: record_from_log dual-record path OK (KR)")


def test_sell_raw_preserved_and_idempotent():
    """매도 주문이 raw_orders_json 에 보존되고, 중복 매도가 멱등 처리되는지."""
    import json as _j
    rec, _ = _fresh()
    rec.on_buy_fill("KR", "005930", "삼성전자", "B1", 10000, 100)
    rec.on_sell_fill("KR", "005930", "S1", 11000, 50, exit_reason="분할")
    rec.on_sell_fill("KR", "005930", "S1", 11000, 50, exit_reason="분할")  # 중복(재시도)
    r = _row(rec, "005930")
    assert r["status"] == "PARTIAL" and r["exit_qty_total"] == 50   # 중복 미반영
    raw = _j.loads(r["raw_orders_json"])
    assert any(o["side"] == "SELL" and o["order_no"] == "S1" for o in raw)  # 매도 원본 보존
    rec.on_sell_fill("KR", "005930", "S2", 12000, 50, exit_reason="전량")
    r = _row(rec, "005930")
    assert r["status"] == "CLOSED"
    raw = _j.loads(r["raw_orders_json"])
    assert sum(1 for o in raw if o["side"] == "SELL") == 2
    print("✓ sell raw preserved + duplicate sell idempotent")


def test_integration_us_dict_shapes():
    """US 관리자가 만드는 dict(symbol 키, timestamp 없음)로도 이중기록 동작."""
    from ledger.integration import record_from_log
    rec, _ = _fresh()
    buy = {"action": "BUY", "symbol": "TQQQ", "name": "ProShares", "price": 50.0,
           "qty": 10, "buy_score": 6, "rsi": 55, "reason": "돌파 진입"}
    assert record_from_log(rec, buy, "US") is True
    # 부분매도 → PARTIAL
    assert record_from_log(rec, {"action": "SELL_PARTIAL", "symbol": "TQQQ",
                                 "price": 52.0, "qty": 4, "reason": "분할익절"}, "US") is True
    r = _row(rec, "TQQQ", market="US")
    assert r["status"] == "PARTIAL" and r["exit_qty_total"] == 4
    # 전량매도 → CLOSED
    assert record_from_log(rec, {"action": "SELL", "symbol": "TQQQ",
                                 "price": 53.0, "qty": 6, "reason": "익절"}, "US") is True
    r = _row(rec, "TQQQ", market="US")
    assert r["status"] == "CLOSED" and r["exit_fill_count"] == 2
    print("✓ integration(US): symbol-key + partial→closed OK")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
    print(f"\n=== {passed}/{len(fns)} passed ===")


if __name__ == "__main__":
    _run_all()
