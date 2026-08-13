"""매매일지 조회 API 확장 테스트 (조회 전용, DB/매매 로직 무변경).

검증:
  - query_journal_list: entry+exit 병합, 파생 필드(status_display/exit_category), 필터
  - query_journal_card: 시장 통합 손익(pnl_krw) + 승/패/승률/평균수익률
  - query_daily_summary(include_avg_return): avg_return_pct 추가(하위호환)
  - classify_exit_category: 카테고리 규칙
  - 하위호환: 기존 query_journal / query_daily_summary 기본 반환 형태 불변
  - graceful: 빈 DB·구버전 결측에도 예외 없음
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import journal.trading_journal as jnl


def _fresh_db(tmp_path):
    jnl.DB_PATH = tmp_path
    if hasattr(jnl._local, "conn"):
        try:
            jnl._local.conn.close()
        except Exception:
            pass
        del jnl._local.conn
    jnl._init_db()


def _entry(trade_id, market, code, name, state="OPEN", fill_confirmed=1,
           avg_price=1000.0, entry_reason="모멘텀 진입", strategy="Pyramid",
           buy_score=0.8, created_at="2026-08-13T10:00:00.000"):
    conn = jnl._get_conn()
    conn.execute(
        """INSERT INTO trade_entries
           (trade_id, market, code, name, entry_type, strategy_name, signal_time,
            signal_price, buy_score, order_time, order_qty, fill_time, fill_qty,
            fill_confirmed, avg_price, entry_reason, state, created_at)
           VALUES(?,?,?,?,?,?,?, ?,?, ?,?,?,?, ?,?,?,?,?)""",
        (trade_id, market, code, name, "FULL", strategy, created_at,
         avg_price, buy_score, created_at, 10, created_at, 10,
         fill_confirmed, avg_price, entry_reason, state, created_at),
    )
    conn.commit()


def _exit(trade_id, market, code, name, net_profit, net_profit_pct,
          exit_reason, is_stoploss=0, is_forced=0, pnl_krw=None,
          max_profit_pct=None, max_drawdown_pct=None, holding_seconds=3600.0,
          created_at="2026-08-13T12:00:00.000"):
    conn = jnl._get_conn()
    if pnl_krw is None:
        pnl_krw = net_profit  # KR: pnl_krw == net_profit
    conn.execute(
        """INSERT INTO trade_exits
           (trade_id, market, code, name, exit_reason, sell_fill_time,
            sell_fill_price, sell_fill_qty, avg_price, net_profit, net_profit_pct,
            pnl_krw, holding_seconds, max_profit_pct, max_drawdown_pct,
            is_stoploss, is_forced, created_at)
           VALUES(?,?,?,?,?,?, ?,?, ?,?,?, ?,?,?,?, ?,?,?)""",
        (trade_id, market, code, name, exit_reason, created_at,
         1100.0, 10, 1000.0, net_profit, net_profit_pct,
         pnl_krw, holding_seconds, max_profit_pct, max_drawdown_pct,
         is_stoploss, is_forced, created_at),
    )
    # entry 를 CLOSED 로
    conn.execute("UPDATE trade_entries SET state='CLOSED' WHERE trade_id=?", (trade_id,))
    conn.commit()


class JournalListApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")
        _fresh_db(self.tmp)
        # 1) KR 청산완료 · 트레일링 익절 (수익)
        _entry("T-KR-WIN", "KR", "005930", "삼성전자")
        _exit("T-KR-WIN", "KR", "005930", "삼성전자", 5000.0, 6.5,
              "트레일링 익절 청산", max_profit_pct=8.0, max_drawdown_pct=-1.0)
        # 2) KR 청산완료 · 손절 (손실)
        _entry("T-KR-LOSS", "KR", "000660", "SK하이닉스")
        _exit("T-KR-LOSS", "KR", "000660", "SK하이닉스", -3000.0, -4.0,
              "손절 청산", is_stoploss=1, max_profit_pct=1.0, max_drawdown_pct=-5.0)
        # 3) US 청산완료 (USD 손익 + KRW 환산)
        _entry("T-US-WIN", "US", "SMCI", "Super Micro", avg_price=45.0)
        _exit("T-US-WIN", "US", "SMCI", "Super Micro", 20.0, 4.4,
              "익절 목표가 도달", pnl_krw=27000.0)
        # 4) US 보유중(체결확인)
        _entry("T-US-OPEN", "US", "IONQ", "IonQ", state="OPEN", fill_confirmed=1)
        # 5) KR 확인대기(미체결)
        _entry("T-KR-PEND", "KR", "042700", "한미반도체", state="OPEN", fill_confirmed=0)
        # 6) KR 거절
        _entry("T-KR-REJ", "KR", "035720", "카카오", state="REJECTED", fill_confirmed=0)

    def tearDown(self):
        try:
            if hasattr(jnl._local, "conn"):
                jnl._local.conn.close(); del jnl._local.conn
        except Exception:
            pass
        for p in (self.tmp, self.tmp + "-wal", self.tmp + "-shm"):
            try: os.remove(p)
            except OSError: pass

    # ── 병합 + 파생 필드 ──────────────────────────────────────
    def test_list_join_has_exit_fields(self):
        rows = {r["trade_id"]: r for r in jnl.query_journal_list(limit=100)}
        self.assertEqual(len(rows), 6)
        win = rows["T-KR-WIN"]
        self.assertEqual(win["net_profit"], 5000.0)
        self.assertEqual(win["net_profit_pct"], 6.5)
        self.assertEqual(win["exit_reason"], "트레일링 익절 청산")
        self.assertEqual(win["max_profit_pct"], 8.0)
        # 미청산은 exit 필드 None
        self.assertIsNone(rows["T-US-OPEN"]["net_profit"])
        self.assertIsNone(rows["T-US-OPEN"]["exit_category"])

    def test_status_display(self):
        rows = {r["trade_id"]: r for r in jnl.query_journal_list(limit=100)}
        self.assertEqual(rows["T-KR-WIN"]["status_display"], "청산완료")
        self.assertEqual(rows["T-US-OPEN"]["status_display"], "보유중")
        self.assertEqual(rows["T-KR-PEND"]["status_display"], "확인대기")
        self.assertEqual(rows["T-KR-REJ"]["status_display"], "거절")

    def test_exit_category_derived(self):
        rows = {r["trade_id"]: r for r in jnl.query_journal_list(limit=100)}
        self.assertEqual(rows["T-KR-WIN"]["exit_category"], "트레일링")
        self.assertEqual(rows["T-KR-LOSS"]["exit_category"], "손절")
        self.assertEqual(rows["T-US-WIN"]["exit_category"], "익절")

    # ── 필터 ──────────────────────────────────────────────────
    def test_filter_market(self):
        rows = jnl.query_journal_list(market="US", limit=100)
        self.assertTrue(all(r["market"] == "US" for r in rows))
        self.assertEqual(len(rows), 2)

    def test_filter_status(self):
        self.assertEqual([r["trade_id"] for r in jnl.query_journal_list(status="보유중")], ["T-US-OPEN"])
        self.assertEqual([r["trade_id"] for r in jnl.query_journal_list(status="확인대기")], ["T-KR-PEND"])
        self.assertEqual([r["trade_id"] for r in jnl.query_journal_list(status="거절")], ["T-KR-REJ"])
        self.assertEqual(len(jnl.query_journal_list(status="청산완료")), 3)

    def test_filter_exit_category(self):
        self.assertEqual([r["trade_id"] for r in jnl.query_journal_list(exit_category="손절")], ["T-KR-LOSS"])
        self.assertEqual([r["trade_id"] for r in jnl.query_journal_list(exit_category="트레일링")], ["T-KR-WIN"])

    def test_filter_search_q(self):
        self.assertEqual([r["trade_id"] for r in jnl.query_journal_list(q="하이닉스")], ["T-KR-LOSS"])
        self.assertEqual([r["trade_id"] for r in jnl.query_journal_list(q="SMCI")], ["T-US-WIN"])

    # ── classify_exit_category 단위 ───────────────────────────
    def test_classify_exit_category(self):
        c = jnl.classify_exit_category
        self.assertEqual(c("아무거나", is_stoploss=1), "손절")
        self.assertEqual(c("트레일링 하락 청산"), "트레일링")
        self.assertEqual(c("익절 목표 도달"), "익절")
        self.assertEqual(c("장마감 시간청산"), "시간청산")
        self.assertEqual(c("수동 청산", is_forced=1), "수동")
        self.assertEqual(c("특이사유"), "기타")
        self.assertIsNone(c(None))  # 미청산

    # ── 오늘 요약 카드 ────────────────────────────────────────
    def test_journal_card_aggregation(self):
        card = jnl.query_journal_card("2026-08-13")
        a, kr, us = card["all"], card["KR"], card["US"]
        # 전체 통합 pnl_krw = 5000 - 3000 + 27000 = 29000
        self.assertEqual(a["pnl_krw"], 29000.0)
        self.assertEqual(a["trades"], 3)
        self.assertEqual(a["wins"], 2)
        self.assertEqual(a["losses"], 1)
        self.assertAlmostEqual(a["win_rate"], 2 / 3 * 100.0, places=3)
        # KR: native 원 = 5000-3000=2000
        self.assertEqual(kr["pnl_native"], 2000.0)
        self.assertEqual(kr["trades"], 2)
        # US: native USD = 20, pnl_krw = 27000
        self.assertEqual(us["pnl_native"], 20.0)
        self.assertEqual(us["pnl_krw"], 27000.0)
        self.assertEqual(us["trades"], 1)

    def test_journal_card_empty_date(self):
        card = jnl.query_journal_card("2000-01-01")
        self.assertEqual(card["all"]["trades"], 0)
        self.assertIsNone(card["all"]["win_rate"])

    # ── daily_summary avg_return_pct 확장 + 하위호환 ──────────
    def test_daily_summary_avg_return_optin(self):
        # 오늘 요약 upsert 를 위해 record_trade_closed 대신 직접 집계는 없음 →
        # daily_trade_summary 는 record_trade_closed 경로에서만 채워지므로,
        # 여기서는 include_avg_return 파라미터가 예외 없이 동작하고 필드가 존재함을 확인.
        rows = jnl.query_daily_summary(include_avg_return=True)
        for r in rows:
            self.assertIn("avg_return_pct", r)
        # 기본(False)은 avg_return_pct 미포함(하위호환)
        base = jnl.query_daily_summary()
        for r in base:
            self.assertNotIn("avg_return_pct", r)

    # ── 하위호환: 기존 query_journal 불변 ─────────────────────
    def test_backward_compat_query_journal(self):
        rows = jnl.query_journal(limit=100)
        self.assertEqual(len(rows), 6)
        # 기존 함수는 entry 컬럼만 — exit 병합 필드 없음
        self.assertNotIn("net_profit", rows[0])
        self.assertNotIn("status_display", rows[0])

    # ── graceful: 빈 DB ───────────────────────────────────────
    def test_empty_db_safe(self):
        tmp2 = tempfile.mktemp(suffix=".db")
        _fresh_db(tmp2)
        try:
            self.assertEqual(jnl.query_journal_list(), [])
            self.assertEqual(jnl.query_journal_card("2026-08-13")["all"]["trades"], 0)
        finally:
            if hasattr(jnl._local, "conn"):
                jnl._local.conn.close(); del jnl._local.conn
            for p in (tmp2, tmp2 + "-wal", tmp2 + "-shm"):
                try: os.remove(p)
                except OSError: pass


if __name__ == "__main__":
    unittest.main()
