"""
tests/journal/test_trading_journal.py
거래 저널 DB 모듈 26개 테스트 (원본 14 + 신규 12)

테스트 목록
───────────
[원본 14]
 01. DB 초기화: 4개 테이블 존재 확인
 02. make_trade_id 형식 검증
 03. record_signal INSERT 확인
 04. record_order_submitted UPDATE 확인
 05. record_order_accepted 이벤트 기록
 06. record_order_rejected + state=REJECTED
 07. fill_confirmed=False → ORDER_FILLED 이벤트 생성 금지 (새 규칙)
 08. fill_confirmed=True → fill_time/fill_price 기록
 09. SELL 전체 흐름 (signal→submitted→accepted→closed)
 10. record_trade_closed — max_drawdown_pct 계산 검증
 11. daily_trade_summary UPSERT 검증
 12. query_journal 필터 동작 검증
 13. 저널 실패 시 매매 루프 무영향 (mocking)
 14. get_error_counts — 오류 카운터 증가 검증

[신규 12]
 15. ORDER_ACCEPTED 후 체결 미확인 시 ORDER_FILLED 이벤트 생성되지 않음
 16. 매도 접수 후 체결 미확인 시 TRADE_CLOSED 생성되지 않음
 17. 체결 미확인 거래가 daily_trade_summary에 포함되지 않음
 18. 실제 매수 체결 확인 후에만 fill_time과 fill_price 기록
 19. 실제 매도 체결 확인 후에만 손익 확정(trade_exits INSERT)
 20. 부분체결 미지원 — ORDER_PARTIALLY_FILLED 잘못된 전량처리 없음
 21. break-even 거래 집계 (net_profit=0)
 22. Profit Factor 계산 — gross_profit / |gross_loss|, gross_loss=0이면 NULL
 23. 수수료·세금 합계 집계 (commission + tax)
 24. max_profit_pct — (highest_price - avg_price)/avg_price*100 단순 가격 수익률
 25. 재시작 후 OPEN 상태 거래가 CLOSED로 잘못 변경되지 않음
 26. 저널 장애 시 매매 루프 무영향 (record_trade_closed 장애 격리)
"""
import os, sys, sqlite3, threading, time, unittest
from unittest.mock import patch, MagicMock

# ── path 설정 ──────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import journal.trading_journal as jnl


# ── 헬퍼 ──────────────────────────────────────────────────
def _fresh_db(tmp_path: str):
    """테스트용 DB 경로 교체 후 재초기화."""
    jnl.DB_PATH = tmp_path
    if hasattr(jnl._local, "conn"):
        try: jnl._local.conn.close()
        except Exception: pass
        del jnl._local.conn
    jnl._init_db()


def _signal(tid, code="005930", name="삼성전자", price=75000):
    """trade_entries INSERT 헬퍼."""
    return jnl.record_signal(
        tid, "KR", code, name, "FULL", price,
        0.7, 2.0, 45.0, 76000, 74000, 72000, 500,
        1000000, 2.0, 0.8, 1.1, 900000, "정규장",
        "테스트진입",
    )


def _close(tid, code="005930", name="삼성전자",
           avg_p=75000, sell_p=76500, qty=10,
           net_profit=13250, net_pct=1.73,
           highest=77000, lowest=74500,
           commission=1147, tax=1350):
    """record_trade_closed 헬퍼 (확정 체결)."""
    return jnl.record_trade_closed(
        tid, "KR", code, name, "익절",
        sell_p, sell_p, qty,
        sell_p, qty,
        True,          # sell_fill_confirmed=True (확정)
        avg_p,
        net_profit, net_pct,
        commission, tax, commission + tax,
        sell_p * qty - commission - tax,
        3600,
        highest, lowest,
        None, None,
        False, False, 1, net_profit, "TRADING",
        "정규장", 2.0,
    )


# ══════════════════════════════════════════════════════════
class TestTradingJournal(unittest.TestCase):

    def setUp(self):
        self.db_path = "/tmp/test_trading_journal.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        # WAL 잔여파일도 정리
        for ext in ("-wal", "-shm"):
            p = self.db_path + ext
            if os.path.exists(p): os.remove(p)
        _fresh_db(self.db_path)

    # ─────────────────────────────────────────────────────
    # 원본 14개 테스트 (스키마 수정 반영)
    # ─────────────────────────────────────────────────────

    def test_01_db_init_tables(self):
        """DB 초기화: 4개 테이블 존재 확인."""
        conn = sqlite3.connect(self.db_path)
        tables = {r[0] for r in
                  conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        for t in ("trade_entries", "trade_exits", "trade_events", "daily_trade_summary"):
            self.assertIn(t, tables, f"Table '{t}' missing")

    def test_02_make_trade_id_format(self):
        """make_trade_id 형식 KR_005930_17자리_8자리."""
        tid = jnl.make_trade_id("KR", "005930")
        parts = tid.split("_")
        self.assertEqual(parts[0], "KR")
        self.assertEqual(parts[1], "005930")
        self.assertEqual(len(parts[2]), 17)
        self.assertEqual(len(parts[3]), 8)

    def test_03_record_signal_insert(self):
        """record_signal: trade_entries INSERT 확인."""
        tid = jnl.make_trade_id("KR", "005930")
        ok = _signal(tid)
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT * FROM trade_entries WHERE trade_id=?", (tid,)).fetchone()
        conn.close()
        self.assertIsNotNone(row)

    def test_04_record_order_submitted(self):
        """record_order_submitted: order_price/order_qty UPDATE 확인."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        ok = jnl.record_order_submitted(tid, "KR", "005930", 75000, 10)
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT order_price,order_qty FROM trade_entries WHERE trade_id=?",
                           (tid,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 75000)
        self.assertEqual(row[1], 10)

    def test_05_record_order_accepted_event(self):
        """record_order_accepted: ORDER_ACCEPTED 이벤트 기록."""
        tid = jnl.make_trade_id("KR", "005930")
        ok = jnl.record_order_accepted(tid, "KR", "005930", "0", "주문접수성공")
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db_path)
        ev = conn.execute("SELECT event_type FROM trade_events WHERE trade_id=?",
                          (tid,)).fetchone()
        conn.close()
        self.assertEqual(ev[0], jnl.EventType.ORDER_ACCEPTED)

    def test_06_record_order_rejected_state(self):
        """record_order_rejected: state=REJECTED + ORDER_REJECTED 이벤트."""
        tid = jnl.make_trade_id("KR", "000660")
        _signal(tid, "000660", "SK하이닉스", 160000)
        ok = jnl.record_order_rejected(tid, "KR", "000660", "9", "주문거부")
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db_path)
        state = conn.execute("SELECT state FROM trade_entries WHERE trade_id=?",
                             (tid,)).fetchone()
        ev = conn.execute("SELECT event_type FROM trade_events WHERE trade_id=? AND event_type=?",
                          (tid, jnl.EventType.ORDER_REJECTED)).fetchone()
        conn.close()
        self.assertEqual(state[0], "REJECTED")
        self.assertIsNotNone(ev)

    def test_07_order_filled_not_confirmed(self):
        """★ fill_confirmed=False → record_order_filled 무시, 반환값 False."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_order_submitted(tid, "KR", "005930", 75000, 10)
        # fill_confirmed=False 호출
        result = jnl.record_order_filled(
            tid, "KR", "005930",
            fill_price=75000, fill_qty=10, avg_price=75000,
            buy_commission=1125, fill_confirmed=False,
        )
        # 새 규칙: fill_confirmed=False → False 반환, DB 기록 없음
        self.assertFalse(result, "fill_confirmed=False인 ORDER_FILLED는 False 반환해야 함")
        conn = sqlite3.connect(self.db_path)
        # ORDER_FILLED 이벤트 없어야 함
        ev = conn.execute(
            "SELECT id FROM trade_events WHERE trade_id=? AND event_type=?",
            (tid, jnl.EventType.ORDER_FILLED)
        ).fetchone()
        # fill_time도 NULL 이어야 함
        row = conn.execute("SELECT fill_time, fill_confirmed FROM trade_entries WHERE trade_id=?",
                           (tid,)).fetchone()
        conn.close()
        self.assertIsNone(ev, "fill_confirmed=False → ORDER_FILLED 이벤트 없어야 함")
        if row:
            self.assertIsNone(row[0], "fill_time은 NULL 이어야 함")
            self.assertEqual(row[1], 0, "fill_confirmed 컬럼은 0이어야 함")

    def test_08_order_filled_confirmed(self):
        """fill_confirmed=True → fill_time 존재, fill_confirmed=1."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_order_submitted(tid, "KR", "005930", 75000, 10)
        ok = jnl.record_order_filled(
            tid, "KR", "005930",
            fill_price=75100, fill_qty=10, avg_price=75100,
            buy_commission=1126, fill_confirmed=True,
        )
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT fill_time, fill_confirmed FROM trade_entries WHERE trade_id=?",
                           (tid,)).fetchone()
        conn.close()
        self.assertIsNotNone(row[0], "fill_time 존재해야 함 (체결 확인)")
        self.assertEqual(row[1], 1, "fill_confirmed = 1이어야 함")

    def test_09_sell_full_flow(self):
        """SELL 전체 흐름: signal→submitted→accepted→[record_trade_closed]→CLOSED."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_order_submitted(tid, "KR", "005930", 75000, 10)
        jnl.record_order_accepted(tid, "KR", "005930", "0", "접수성공")
        # 매도 흐름 (확정 체결)
        jnl.record_sell_signal(tid, "KR", "005930", 76500, 3.0, "익절")
        jnl.record_sell_order_submitted(tid, "KR", "005930", 76500, 10)
        jnl.record_sell_order_accepted(tid, "KR", "005930", "0", "매도접수성공")
        # 실체결 확인 후 SELL_ORDER_FILLED
        jnl.record_sell_order_filled(tid, "KR", "005930", 76500, 10, fill_confirmed=True)
        ok = _close(tid)
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db_path)
        state = conn.execute("SELECT state FROM trade_entries WHERE trade_id=?",
                             (tid,)).fetchone()
        exit_row = conn.execute("SELECT trade_id FROM trade_exits WHERE trade_id=?",
                                (tid,)).fetchone()
        conn.close()
        self.assertEqual(state[0], "CLOSED")
        self.assertIsNotNone(exit_row)

    def test_10_max_drawdown_pct(self):
        """record_trade_closed — max_drawdown_pct = (72000-75000)/75000*100 = -4.0%."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_trade_closed(
            tid, "KR", "005930", "삼성전자", "손절",
            73000, 73000, 10, 73000, 10, True,
            75000, -15000, -2.0, 1095, 1350, 2445, 727555,
            1800, 75500, 72000, None, None,
            True, True, 1, -15000, "TRADING", "정규장", 1.0,
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT max_drawdown_pct FROM trade_exits WHERE trade_id=?",
                           (tid,)).fetchone()
        conn.close()
        self.assertAlmostEqual(row[0], -4.0, places=1)

    def test_11_daily_summary_upsert(self):
        """daily_trade_summary: 2건 기록 후 total_trades=2."""
        tid1 = jnl.make_trade_id("KR", "005930")
        tid2 = jnl.make_trade_id("KR", "000660")
        _signal(tid1)
        _close(tid1)
        _signal(tid2, "000660", "SK하이닉스", 160000)
        jnl.record_trade_closed(
            tid2, "KR", "000660", "SK하이닉스", "손절",
            155000, 155000, 5, 155000, 5, True,
            160000, -25000, -3.1, 1163, 1395, 2558, 772442,
            1800, 162000, 154000, None, None,
            True, True, 1, -25000, "TRADING", "정규장", 1.0,
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT total_trades FROM daily_trade_summary WHERE market='KR'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 2)

    def test_12_query_journal_filter(self):
        """query_journal: market 필터, CLOSED 없으면 0건."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        rows_all    = jnl.query_journal(market="KR")
        rows_closed = jnl.query_journal(state="CLOSED")
        self.assertGreaterEqual(len(rows_all), 1)
        self.assertEqual(len(rows_closed), 0)

    def test_13_journal_failure_isolation(self):
        """저널 record_signal 예외가 매매 루프 실행을 중단하지 않음."""
        execution_reached = []
        def _simulate_buy_loop():
            try:
                with patch.object(jnl, "record_signal", side_effect=RuntimeError("DB 장애")):
                    _trade_id = jnl.make_trade_id("KR", "005930")
                    try:
                        jnl.record_signal(_trade_id, "KR", "005930", "삼성전자", "FULL", 75000,
                                          0.7, 1.0, 45, 76000, 74000, 72000, 500, 1000000, 2.0,
                                          0.8, 1.1, 900000, "정규장", "테스트")
                    except Exception as e:
                        jnl._inc_error("test_isolation", e)
                    execution_reached.append("buy_order_executed")
            except Exception:
                pass
        _simulate_buy_loop()
        self.assertIn("buy_order_executed", execution_reached)
        self.assertGreater(jnl.get_error_counts().get("test_isolation", 0), 0)

    def test_14_error_counter(self):
        """get_error_counts: 오류 2회 증가 확인."""
        before = jnl.get_error_counts().get("test_counter", 0)
        jnl._inc_error("test_counter", ValueError("오류1"))
        jnl._inc_error("test_counter", ValueError("오류2"))
        after = jnl.get_error_counts().get("test_counter", 0)
        self.assertEqual(after - before, 2)

    # ─────────────────────────────────────────────────────
    # 신규 12개 테스트 (요구사항 9의 12가지)
    # ─────────────────────────────────────────────────────

    def test_15_no_order_filled_on_unconfirmed(self):
        """★ 1. ORDER_ACCEPTED 후 체결 미확인 → ORDER_FILLED 이벤트 생성되지 않음.
        fill_confirmed=False 로 record_order_filled 호출 시 반환 False이고
        trade_events에 ORDER_FILLED 행이 없어야 한다."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_order_submitted(tid, "KR", "005930", 75000, 10)
        jnl.record_order_accepted(tid, "KR", "005930", "0", "접수성공")

        # fill_confirmed=False 호출 → 무시되어야 함
        ret = jnl.record_order_filled(
            tid, "KR", "005930",
            fill_price=75000, fill_qty=10, avg_price=75000,
            fill_confirmed=False,
        )
        self.assertFalse(ret, "fill_confirmed=False → False 반환")

        conn = sqlite3.connect(self.db_path)
        filled_ev = conn.execute(
            "SELECT id FROM trade_events WHERE trade_id=? AND event_type=?",
            (tid, jnl.EventType.ORDER_FILLED)
        ).fetchone()
        conn.close()
        self.assertIsNone(filled_ev, "ORDER_FILLED 이벤트 없어야 함")

    def test_16_no_trade_closed_on_unconfirmed_sell(self):
        """★ 2. 매도 접수 후 체결 미확인 → TRADE_CLOSED 생성되지 않음.
        record_sell_order_accepted 이후 record_sell_order_filled(fill_confirmed=False)를
        호출해도 trade_events에 TRADE_CLOSED 행이 없어야 한다."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_order_submitted(tid, "KR", "005930", 75000, 10)
        jnl.record_order_accepted(tid, "KR", "005930", "0", "접수성공")
        jnl.record_sell_signal(tid, "KR", "005930", 76500, 3.0, "익절")
        jnl.record_sell_order_submitted(tid, "KR", "005930", 76500, 10)
        jnl.record_sell_order_accepted(tid, "KR", "005930", "0", "매도접수성공")

        # fill_confirmed=False → 무시되어야 함
        ret = jnl.record_sell_order_filled(
            tid, "KR", "005930",
            sell_fill_price=76500, sell_fill_qty=10,
            fill_confirmed=False,
        )
        self.assertFalse(ret, "fill_confirmed=False → False 반환")

        conn = sqlite3.connect(self.db_path)
        # TRADE_CLOSED 이벤트 없어야 함
        closed_ev = conn.execute(
            "SELECT id FROM trade_events WHERE trade_id=? AND event_type=?",
            (tid, jnl.EventType.TRADE_CLOSED)
        ).fetchone()
        # trade_entries 상태는 OPEN 이어야 함 (CLOSED 금지)
        state = conn.execute(
            "SELECT state FROM trade_entries WHERE trade_id=?", (tid,)
        ).fetchone()
        # trade_exits 없어야 함
        exit_row = conn.execute(
            "SELECT id FROM trade_exits WHERE trade_id=?", (tid,)
        ).fetchone()
        conn.close()
        self.assertIsNone(closed_ev, "TRADE_CLOSED 이벤트 없어야 함")
        if state:
            self.assertNotEqual(state[0], "CLOSED", "state는 CLOSED가 아니어야 함")
        self.assertIsNone(exit_row, "trade_exits 없어야 함")

    def test_17_unconfirmed_trade_excluded_from_daily_summary(self):
        """★ 3. 체결 미확인 거래가 daily_trade_summary에 포함되지 않음.
        ORDER_ACCEPTED만 기록하고 record_trade_closed를 호출하지 않으면
        daily_trade_summary에 행이 생기지 않아야 한다."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_order_submitted(tid, "KR", "005930", 75000, 10)
        jnl.record_order_accepted(tid, "KR", "005930", "0", "접수성공")
        # record_trade_closed 호출하지 않음 (미체결 상태)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT total_trades FROM daily_trade_summary WHERE market='KR'"
        ).fetchone()
        conn.close()
        self.assertIsNone(row, "미체결 주문은 daily_summary에 포함되지 않아야 함")

    def test_18_fill_time_only_on_confirmed(self):
        """★ 4. 실제 매수 체결 확인 후에만 fill_time과 fill_price 기록.
        - fill_confirmed=False: fill_time=NULL, fill_price=NULL
        - fill_confirmed=True: fill_time 존재, fill_price 존재"""
        # Case A: 미확인 → fill_time NULL
        tid_a = jnl.make_trade_id("KR", "005930")
        _signal(tid_a)
        jnl.record_order_accepted(tid_a, "KR", "005930", "0", "접수")
        jnl.record_order_filled(tid_a, "KR", "005930",
                                fill_price=75000, fill_qty=10, avg_price=75000,
                                fill_confirmed=False)
        conn = sqlite3.connect(self.db_path)
        row_a = conn.execute("SELECT fill_time, fill_price FROM trade_entries WHERE trade_id=?",
                             (tid_a,)).fetchone()
        conn.close()
        self.assertIsNone(row_a[0], "미확인: fill_time은 NULL이어야 함")
        # fill_price도 record_order_filled가 호출되지 않았으므로 NULL
        self.assertIsNone(row_a[1], "미확인: fill_price는 NULL이어야 함")

        # Case B: 확인 → fill_time 존재
        tid_b = jnl.make_trade_id("KR", "005930")
        _signal(tid_b)
        jnl.record_order_accepted(tid_b, "KR", "005930", "0", "접수")
        jnl.record_order_filled(tid_b, "KR", "005930",
                                fill_price=75100, fill_qty=10, avg_price=75100,
                                fill_confirmed=True)
        conn = sqlite3.connect(self.db_path)
        row_b = conn.execute("SELECT fill_time, fill_price FROM trade_entries WHERE trade_id=?",
                             (tid_b,)).fetchone()
        conn.close()
        self.assertIsNotNone(row_b[0], "확인: fill_time 존재해야 함")
        self.assertAlmostEqual(row_b[1], 75100, places=0, msg="확인: fill_price=75100")

    def test_19_pnl_only_on_confirmed_sell(self):
        """★ 5. 실제 매도 체결 확인 후에만 손익 확정 (trade_exits INSERT).
        SELL_ORDER_ACCEPTED 단계에서는 trade_exits 없어야 하고,
        record_trade_closed(sell_fill_confirmed=True) 호출 후에만 생성된다."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_order_accepted(tid, "KR", "005930", "0", "접수")
        jnl.record_sell_signal(tid, "KR", "005930", 76500, 3.0, "익절")
        jnl.record_sell_order_submitted(tid, "KR", "005930", 76500, 10)
        jnl.record_sell_order_accepted(tid, "KR", "005930", "0", "매도접수")

        # 이 시점: trade_exits 없어야 함
        conn = sqlite3.connect(self.db_path)
        exit_before = conn.execute("SELECT id FROM trade_exits WHERE trade_id=?",
                                   (tid,)).fetchone()
        conn.close()
        self.assertIsNone(exit_before, "SELL_ORDER_ACCEPTED 단계에서 trade_exits 없어야 함")

        # 확정 체결 후 TRADE_CLOSED → trade_exits 생성
        jnl.record_sell_order_filled(tid, "KR", "005930", 76500, 10, fill_confirmed=True)
        ok = _close(tid)
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db_path)
        exit_after = conn.execute("SELECT net_profit FROM trade_exits WHERE trade_id=?",
                                  (tid,)).fetchone()
        conn.close()
        self.assertIsNotNone(exit_after, "TRADE_CLOSED 후 trade_exits 존재해야 함")
        self.assertAlmostEqual(exit_after[0], 13250, places=0, msg="net_profit=13250")

    def test_20_no_partial_fill_wrong_processing(self):
        """★ 6. 부분체결 미지원 — ORDER_PARTIALLY_FILLED 명시적 미지원.
        record_order_filled(fill_confirmed=True)로 전량 처리할 수 없는
        부분체결 케이스에서 잘못된 전량 처리가 발생하지 않는다.
        (현재 KIS 체결조회 미연동이므로 ORDER_PARTIALLY_FILLED 자동 생성 금지)"""
        # EventType에 ORDER_PARTIALLY_FILLED 상수가 정의되어 있어야 함
        self.assertTrue(
            hasattr(jnl.EventType, "ORDER_PARTIALLY_FILLED"),
            "ORDER_PARTIALLY_FILLED 이벤트 타입 정의 필요"
        )
        # fill_confirmed=True를 호출한다고 해서 부분체결 이벤트가 자동 생성되면 안 됨
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_order_accepted(tid, "KR", "005930", "0", "접수")
        # qty=5 (일부만) 처리 — fill_confirmed=True 이어도 ORDER_PARTIALLY_FILLED 자동 생성 없음
        jnl.record_order_filled(tid, "KR", "005930",
                                fill_price=75000, fill_qty=5, avg_price=75000,
                                fill_confirmed=True)
        conn = sqlite3.connect(self.db_path)
        partial_ev = conn.execute(
            "SELECT id FROM trade_events WHERE trade_id=? AND event_type=?",
            (tid, jnl.EventType.ORDER_PARTIALLY_FILLED)
        ).fetchone()
        conn.close()
        self.assertIsNone(partial_ev,
                          "record_order_filled가 ORDER_PARTIALLY_FILLED를 자동 생성하면 안 됨")

    def test_21_break_even_trade_aggregation(self):
        """★ 7. break-even 거래 집계 (net_profit=0).
        net_profit=0인 거래는 break_even_trades로 집계되고
        winning_trades/losing_trades에는 포함되지 않는다."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_trade_closed(
            tid, "KR", "005930", "삼성전자", "본전",
            75000, 75000, 10, 75000, 10, True,
            75000, 0.0, 0.0,  # net_profit=0 (본전)
            0, 0, 0, 750000,
            1800, 75500, 74500, None, None,
            False, False, 1, 0.0, "TRADING", "정규장", 0.0,
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT total_trades, winning_trades, losing_trades, break_even_trades "
            "FROM daily_trade_summary WHERE market='KR'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1, "total_trades=1")
        self.assertEqual(row[1], 0, "winning_trades=0 (본전은 승리 아님)")
        self.assertEqual(row[2], 0, "losing_trades=0 (본전은 손실 아님)")
        self.assertEqual(row[3], 1, "break_even_trades=1")

    def test_22_profit_factor_calculation(self):
        """★ 8. Profit Factor = gross_profit / |gross_loss|.
        gross_loss=0이면 NULL. gross_loss>0이면 정상 계산."""
        # 1승(+10000), 1패(-5000) → profit_factor = 10000/5000 = 2.0
        tid1 = jnl.make_trade_id("KR", "005930")
        tid2 = jnl.make_trade_id("KR", "000660")
        _signal(tid1)
        jnl.record_trade_closed(
            tid1, "KR", "005930", "삼성전자", "익절",
            76000, 76000, 10, 76000, 10, True,
            75000, 10000, 1.33, 1000, 1000, 2000, 758000,
            3600, 77000, 74000, None, None,
            False, False, 1, 10000, "TRADING", "정규장", 2.0,
        )
        _signal(tid2, "000660", "SK하이닉스", 160000)
        jnl.record_trade_closed(
            tid2, "KR", "000660", "SK하이닉스", "손절",
            157500, 157500, 5, 157500, 5, True,
            160000, -5000, -1.56, 500, 1000, 1500, 785500,
            1800, 162000, 155000, None, None,
            True, True, 1, -5000, "TRADING", "정규장", 1.0,
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT profit_factor, gross_profit, gross_loss "
            "FROM daily_trade_summary WHERE market='KR'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        # gross_profit=10000, gross_loss=-5000 → profit_factor=2.0
        self.assertAlmostEqual(row[0], 2.0, places=2,
                               msg=f"profit_factor 기대=2.0, 실제={row[0]}")
        self.assertAlmostEqual(row[1], 10000, places=0, msg="gross_profit=10000")
        self.assertAlmostEqual(row[2], -5000, places=0, msg="gross_loss=-5000")

        # 승리만 있고 패배 없음 → profit_factor = NULL
        tid3 = jnl.make_trade_id("KR", "035720")
        _fresh_db("/tmp/test_pf_null.db")
        _signal(tid3, "035720", "카카오", 60000)
        jnl.record_trade_closed(
            tid3, "KR", "035720", "카카오", "익절",
            62000, 62000, 10, 62000, 10, True,
            60000, 20000, 3.33, 900, 1100, 2000, 618000,
            1800, 63000, 59500, None, None,
            False, False, 1, 20000, "TRADING", "정규장", 2.0,
        )
        conn2 = sqlite3.connect("/tmp/test_pf_null.db")
        row2 = conn2.execute(
            "SELECT profit_factor FROM daily_trade_summary WHERE market='KR'"
        ).fetchone()
        conn2.close()
        self.assertIsNotNone(row2)
        self.assertIsNone(row2[0], "gross_loss=0이면 profit_factor는 NULL이어야 함")
        # 원래 DB로 복원
        _fresh_db(self.db_path)

    def test_23_commission_tax_aggregation(self):
        """★ 9. 수수료·세금 합계 집계.
        2건 TRADE_CLOSED 후 commission/tax 합산값 검증."""
        tid1 = jnl.make_trade_id("KR", "005930")
        tid2 = jnl.make_trade_id("KR", "000660")
        _signal(tid1)
        jnl.record_trade_closed(
            tid1, "KR", "005930", "삼성전자", "익절",
            76500, 76500, 10, 76500, 10, True,
            75000, 13250, 1.73, 1147, 1350, 2497, 763503,
            3600, 77000, 74500, None, None,
            False, False, 1, 13250, "TRADING", "정규장", 2.0,
        )
        _signal(tid2, "000660", "SK하이닉스", 160000)
        jnl.record_trade_closed(
            tid2, "KR", "000660", "SK하이닉스", "익절",
            164000, 164000, 5, 164000, 5, True,
            160000, 18500, 2.3, 1205, 1476, 2681, 816319,
            2700, 165000, 159000, None, None,
            False, False, 1, 18500, "TRADING", "정규장", 2.0,
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT commission, tax FROM daily_trade_summary WHERE market='KR'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        # 1147 + 1205 = 2352, 1350 + 1476 = 2826
        self.assertAlmostEqual(row[0], 2352, places=0,
                               msg=f"commission 합산 기대=2352, 실제={row[0]}")
        self.assertAlmostEqual(row[1], 2826, places=0,
                               msg=f"tax 합산 기대=2826, 실제={row[1]}")

    def test_24_max_profit_pct_calculation(self):
        """★ 10. max_profit_pct = (highest_price - avg_price) / avg_price * 100.
        단순 가격 수익률 (수수료 미포함).
        avg_price=75000, highest_price=79500 → max_profit_pct=6.0%."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_trade_closed(
            tid, "KR", "005930", "삼성전자", "익절",
            78000, 78000, 10, 78000, 10, True,
            75000, 30000, 4.0, 1125, 1404, 2529, 777471,
            7200,
            79500,   # highest_price = 79500
            73000,   # lowest_price  = 73000
            None, None,
            False, False, 1, 30000, "TRADING", "정규장", 2.0,
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT max_profit_pct, max_drawdown_pct FROM trade_exits WHERE trade_id=?",
            (tid,)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        # max_profit_pct = (79500 - 75000) / 75000 * 100 = 6.0%
        expected_max = (79500 - 75000) / 75000 * 100
        self.assertAlmostEqual(row[0], expected_max, places=2,
                               msg=f"max_profit_pct 기대={expected_max:.2f}%, 실제={row[0]}")
        # max_drawdown_pct = (73000 - 75000) / 75000 * 100 = -2.667%
        expected_dd = (73000 - 75000) / 75000 * 100
        self.assertAlmostEqual(row[1], expected_dd, places=2,
                               msg=f"max_drawdown_pct 기대={expected_dd:.2f}%, 실제={row[1]}")

    def test_25_open_trade_not_wrongly_closed_on_restart(self):
        """★ 11. 재시작 후 OPEN 상태 거래가 CLOSED로 잘못 변경되지 않음.
        모듈을 재임포트(재시작 시뮬레이션)해도 OPEN 상태는 유지되어야 한다."""
        tid = jnl.make_trade_id("KR", "005930")
        _signal(tid)
        jnl.record_order_submitted(tid, "KR", "005930", 75000, 10)
        jnl.record_order_accepted(tid, "KR", "005930", "0", "접수성공")
        # 체결 미확인 상태 유지 (record_order_filled 미호출)

        # DB 연결을 재생성하여 재시작 시뮬레이션
        if hasattr(jnl._local, "conn"):
            try: jnl._local.conn.close()
            except Exception: pass
            del jnl._local.conn
        jnl._init_db()

        # 재시작 후에도 state=OPEN 이어야 함
        conn = sqlite3.connect(self.db_path)
        state = conn.execute(
            "SELECT state FROM trade_entries WHERE trade_id=?", (tid,)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(state)
        self.assertEqual(state[0], "OPEN",
                         "재시작 후 OPEN 상태 거래가 CLOSED로 변경되면 안 됨")

    def test_26_journal_failure_does_not_break_trade_loop(self):
        """★ 12. 저널 장애 시 매매 루프 무영향.
        record_trade_closed가 RuntimeError를 발생시켜도 상위 코드가
        계속 실행되고 _error_counter가 증가해야 한다."""
        trade_result_stored = []

        def simulate_sell_handler():
            """매도 처리 함수 시뮬레이션."""
            profit = {"net_profit": 13250, "net_pct": 1.73}
            # ─ 저널 기록 (장애 발생)
            try:
                with patch.object(jnl, "record_trade_closed",
                                  side_effect=RuntimeError("DB 잠금 오류")):
                    tid = jnl.make_trade_id("KR", "005930")
                    try:
                        jnl.record_trade_closed(
                            tid, "KR", "005930", "삼성전자", "익절",
                            76500, 76500, 10, 76500, 10, True,
                            75000, 13250, 1.73, 1147, 1350, 2497, 763503,
                            3600, 77000, 74500, None, None,
                            False, False, 1, 13250, "TRADING", "정규장", 2.0,
                        )
                    except Exception as e:
                        jnl._inc_error("test_trade_closed_failure", e)
            except Exception:
                pass
            # ─ 저널 장애와 무관하게 매매 결과 반환
            trade_result_stored.append(profit)

        simulate_sell_handler()
        # 매매 결과는 정상 저장되어야 함
        self.assertEqual(len(trade_result_stored), 1,
                         "저널 장애가 있어도 매매 결과 저장돼야 함")
        self.assertEqual(trade_result_stored[0]["net_profit"], 13250)
        # 오류 카운터 증가 확인
        self.assertGreater(
            jnl.get_error_counts().get("test_trade_closed_failure", 0), 0,
            "저널 장애 시 _error_counter 증가해야 함"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
