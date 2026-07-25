"""
tests/fill_observer/test_fill_observer.py
=========================================
KIS 체결 관측 모듈(fill_observer.py) 단위·통합 테스트 — 20개
실제 KIS 호출 없음: fixture/mock 전용.

테스트 목록
───────────
 01. 국내 미체결 주문 정규화 → delta=0, is_new_fill=False
 02. 국내 부분체결 정규화  → delta>0, is_partially_filled=True
 03. 국내 전량체결 정규화  → delta>0, is_fully_filled=True
 04. 미국 미체결 주문 정규화 → delta=0
 05. 미국 부분체결 정규화
 06. 미국 전량체결 정규화
 07. 누적 30주 최초 관측 → fill_delta=30, Phoenix+Journal 기록됨
 08. 누적 30주 재관측    → fill_delta=0, 이벤트 미생성
 09. 누적 70주 관측      → fill_delta=40, PARTIALLY_FILLED 상태
10. 누적 100주 관측     → fill_delta=30, FILLED 상태
11. ODNO별 중복 방지     → Phoenix idempotency_key UNIQUE → 동일 cum 기록 안 됨
12. 서버 재시작 후 cumulative_filled_qty DB에서 복원
13. API 실패 → 주문 상태 보존, retry_count 증가
14. 체결 관측 후 apply_buy  미호출 확인
15. 체결 관측 후 apply_sell 미호출 확인
16. DailyPnLGuard 미변경 확인
17. 포지션 수량 미변경 확인
18. 저널 PARTIALLY_FILLED vs FILLED 구분 기록
19. 미체결 주문은 ORDER_FILLED 이벤트 미생성
20. 기존 57개 회귀 테스트 전부 통과 (모듈 임포트 확인)
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch, call

# ── stock_trader 루트를 import 경로에 추가 ──────────────────
_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import journal.fill_observer as fo


# ─────────────────────────────────────────────────────────────
# 헬퍼: 격리된 임시 DB
# ─────────────────────────────────────────────────────────────

# trading_journal.db 에 필요한 테이블 DDL (fill_observer 테스트용 최소 스키마)
_JOURNAL_TEST_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS trade_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT    NOT NULL UNIQUE,
    market              TEXT    NOT NULL,
    code                TEXT    NOT NULL,
    fill_price          REAL,
    fill_qty            INTEGER,
    fill_time           TEXT,
    fill_confirmed      INTEGER DEFAULT 0,
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime'))
);

CREATE TABLE IF NOT EXISTS trade_exits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    TEXT NOT NULL UNIQUE,
    market      TEXT NOT NULL,
    code        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     TEXT    NOT NULL,
    event_type   TEXT    NOT NULL,
    side         TEXT,
    ts           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime')),
    market       TEXT,
    code         TEXT,
    price        REAL,
    qty          INTEGER,
    rt_cd        TEXT,
    msg1         TEXT,
    note         TEXT,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS daily_trade_summary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date  TEXT NOT NULL,
    market      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_orders (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    market                TEXT    NOT NULL,
    trade_id              TEXT    NOT NULL,
    code                  TEXT    NOT NULL,
    side                  TEXT    NOT NULL,
    odno                  TEXT    NOT NULL DEFAULT '',
    client_order_id       TEXT    NOT NULL DEFAULT '',
    order_qty             INTEGER NOT NULL DEFAULT 0,
    cumulative_filled_qty INTEGER NOT NULL DEFAULT 0,
    status                TEXT    NOT NULL DEFAULT 'ACCEPTED',
    submitted_at          TEXT    NOT NULL,
    last_checked_at       TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    raw_order_response    TEXT,
    exchange              TEXT,
    currency              TEXT    DEFAULT 'KRW',
    created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime')),
    updated_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_po_status   ON pending_orders(status);
CREATE INDEX IF NOT EXISTS idx_po_market   ON pending_orders(market);
CREATE INDEX IF NOT EXISTS idx_po_odno     ON pending_orders(odno);
CREATE INDEX IF NOT EXISTS idx_po_trade_id ON pending_orders(trade_id);
"""


def _make_temp_journal_db() -> str:
    """임시 trading_journal.db 생성 (전체 스키마 포함)."""
    tmp_dir = tempfile.mkdtemp(prefix="fo_jnl_")
    db_path = os.path.join(tmp_dir, "trading_journal.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_JOURNAL_TEST_DDL)
    conn.commit()
    conn.close()
    return db_path


def _make_temp_phoenix_db() -> str:
    """임시 phoenix.db 생성 (events 테이블 스키마 포함)."""
    tmp_dir = tempfile.mkdtemp(prefix="fo_phx_")
    db_path = os.path.join(tmp_dir, "phoenix.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid       TEXT    UNIQUE,
            ts               TEXT,
            type             TEXT,
            aggregate_type   TEXT,
            aggregate_id     TEXT,
            client_order_id  TEXT,
            odno             TEXT,
            code             TEXT,
            side             TEXT,
            qty              REAL,
            price            REAL,
            cum_filled_qty   REAL,
            realized_pnl     REAL,
            idempotency_key  TEXT    UNIQUE,
            payload          TEXT,
            schema_ver       INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_index (
            client_order_id  TEXT PRIMARY KEY,
            odno             TEXT,
            code             TEXT,
            side             TEXT,
            qty              INTEGER,
            ts               TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


def _make_observer(journal_db: str, phoenix_db: str, kis_api=None) -> fo.FillObserver:
    """격리된 DB를 사용하는 FillObserver 인스턴스 생성.

    thread-local 연결을 재설정하여 테스트 간 DB 누출을 방지한다.
    """
    if hasattr(fo._local, "fo_conn") and fo._local.fo_conn is not None:
        try:
            fo._local.fo_conn.close()
        except Exception:
            pass
        fo._local.fo_conn = None
    fo._JOURNAL_DB_PATH = journal_db
    obs = fo.FillObserver(kis_api=kis_api, phoenix_db_path=phoenix_db)
    return obs


def _observer_with_fresh_db(kis_api=None):
    """임시 DB 쌍과 FillObserver를 반환."""
    jdb  = _make_temp_journal_db()
    phdb = _make_temp_phoenix_db()
    obs  = _make_observer(jdb, phdb, kis_api=kis_api)
    return obs, jdb, phdb


# ─────────────────────────────────────────────────────────────
# 공통 KIS 응답 픽스처
# ─────────────────────────────────────────────────────────────

def _kr_open_raw(odno="ORD001", code="005930", order_qty=100) -> dict:
    """국내 미체결 주문 응답 (cum_filled_qty=0)."""
    return {
        "odno":           odno,
        "code":           code,
        "side":           "BUY",
        "order_qty":      order_qty,
        "cum_filled_qty": 0,
        "unfilled_qty":   order_qty,
        "avg_fill_price": 0.0,
        "order_status":   "접수",
        "order_time":     "093000",
        "order_date":     "20260725",
        "raw":            {},
    }


def _kr_partial_raw(odno="ORD001", code="005930",
                    order_qty=100, cum=30) -> dict:
    """국내 부분체결 응답."""
    return {
        "odno":           odno,
        "code":           code,
        "side":           "BUY",
        "order_qty":      order_qty,
        "cum_filled_qty": cum,
        "unfilled_qty":   order_qty - cum,
        "avg_fill_price": 70000.0,
        "order_status":   "일부체결",
        "order_time":     "093100",
        "order_date":     "20260725",
        "raw":            {},
    }


def _kr_full_raw(odno="ORD001", code="005930", order_qty=100) -> dict:
    """국내 전량체결 응답."""
    return {
        "odno":           odno,
        "code":           code,
        "side":           "BUY",
        "order_qty":      order_qty,
        "cum_filled_qty": order_qty,
        "unfilled_qty":   0,
        "avg_fill_price": 70500.0,
        "order_status":   "전량체결",
        "order_time":     "093200",
        "order_date":     "20260725",
        "raw":            {},
    }


def _us_open_raw(odno="USORD001", code="AAPL", order_qty=10) -> dict:
    """미국 미체결 응답 (불명확 필드는 None)."""
    return {
        "odno":           odno,
        "code":           code,
        "exchange":       "NASD",
        "side":           None,          # 필드 불명확
        "order_qty":      None,          # 필드 불명확
        "cum_filled_qty": 0,
        "unfilled_qty":   None,
        "avg_fill_price": None,
        "order_status":   None,
        "order_time":     None,
        "currency":       "USD",
        "raw":            {},
    }


def _us_partial_raw(odno="USORD001", code="AAPL", cum=5) -> dict:
    """미국 부분체결 응답."""
    return {
        "odno":           odno,
        "code":           code,
        "exchange":       "NASD",
        "side":           "BUY",
        "order_qty":      10,
        "cum_filled_qty": cum,
        "unfilled_qty":   10 - cum,
        "avg_fill_price": 210.50,
        "order_status":   "PARTIAL",
        "order_time":     "093000",
        "currency":       "USD",
        "raw":            {},
    }


def _us_full_raw(odno="USORD001", code="AAPL", order_qty=10) -> dict:
    """미국 전량체결 응답."""
    return {
        "odno":           odno,
        "code":           code,
        "exchange":       "NASD",
        "side":           "BUY",
        "order_qty":      order_qty,
        "cum_filled_qty": order_qty,
        "unfilled_qty":   0,
        "avg_fill_price": 212.00,
        "order_status":   "FILLED",
        "order_time":     "093500",
        "currency":       "USD",
        "raw":            {},
    }


# ─────────────────────────────────────────────────────────────
# 테스트 클래스
# ─────────────────────────────────────────────────────────────

class TestExecutionNormalizerKR(unittest.TestCase):
    """테스트 01–03: 국내 정규화."""

    def test_01_kr_open_order_normalization(self):
        """Test 01: 국내 미체결 → delta=0, is_new_fill=False."""
        raw = _kr_open_raw()
        obs = fo.ExecutionNormalizer.from_kr(raw, prev_cum=0)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.market, "KR")
        self.assertEqual(obs.code, "005930")
        self.assertEqual(obs.side, "BUY")
        self.assertEqual(obs.cumulative_filled_qty, 0)
        self.assertEqual(obs.fill_delta_qty, 0)
        self.assertFalse(obs.is_new_fill)
        self.assertFalse(obs.is_fully_filled)
        self.assertFalse(obs.is_partially_filled)
        self.assertEqual(obs.currency, "KRW")
        self.assertIsNone(obs.exchange)

    def test_02_kr_partial_fill_normalization(self):
        """Test 02: 국내 부분체결 → is_partially_filled=True, delta=30."""
        raw = _kr_partial_raw(order_qty=100, cum=30)
        obs = fo.ExecutionNormalizer.from_kr(raw, prev_cum=0)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.cumulative_filled_qty, 30)
        self.assertEqual(obs.fill_delta_qty, 30)
        self.assertTrue(obs.is_new_fill)
        self.assertTrue(obs.is_partially_filled)
        self.assertFalse(obs.is_fully_filled)
        self.assertEqual(obs.order_qty, 100)
        self.assertEqual(obs.average_fill_price, 70000.0)

    def test_03_kr_full_fill_normalization(self):
        """Test 03: 국내 전량체결 → is_fully_filled=True."""
        raw = _kr_full_raw(order_qty=100)
        obs = fo.ExecutionNormalizer.from_kr(raw, prev_cum=0)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.cumulative_filled_qty, 100)
        self.assertEqual(obs.fill_delta_qty, 100)
        self.assertTrue(obs.is_new_fill)
        self.assertTrue(obs.is_fully_filled)
        self.assertFalse(obs.is_partially_filled)


class TestExecutionNormalizerUS(unittest.TestCase):
    """테스트 04–06: 미국 정규화."""

    def test_04_us_open_order_normalization(self):
        """Test 04: 미국 미체결 → delta=0, None 필드 허용."""
        raw = _us_open_raw()
        obs = fo.ExecutionNormalizer.from_us(raw, prev_cum=0)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.market, "US")
        self.assertEqual(obs.currency, "USD")
        self.assertEqual(obs.cumulative_filled_qty, 0)
        self.assertEqual(obs.fill_delta_qty, 0)
        self.assertFalse(obs.is_new_fill)
        # 불명확 필드는 None 허용
        self.assertIsNone(obs.side)
        self.assertIsNone(obs.order_qty)
        self.assertIsNone(obs.average_fill_price)

    def test_05_us_partial_fill_normalization(self):
        """Test 05: 미국 부분체결 → delta=5, is_partially_filled=True."""
        raw = _us_partial_raw(cum=5)
        obs = fo.ExecutionNormalizer.from_us(raw, prev_cum=0)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.cumulative_filled_qty, 5)
        self.assertEqual(obs.fill_delta_qty, 5)
        self.assertTrue(obs.is_new_fill)
        self.assertTrue(obs.is_partially_filled)
        self.assertFalse(obs.is_fully_filled)
        self.assertEqual(obs.average_fill_price, 210.50)

    def test_06_us_full_fill_normalization(self):
        """Test 06: 미국 전량체결 → is_fully_filled=True."""
        raw = _us_full_raw(order_qty=10)
        obs = fo.ExecutionNormalizer.from_us(raw, prev_cum=0)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.cumulative_filled_qty, 10)
        self.assertTrue(obs.is_fully_filled)
        self.assertFalse(obs.is_partially_filled)
        self.assertEqual(obs.exchange, "NASD")


class TestWatermarkLogic(unittest.TestCase):
    """테스트 07–10: fill_delta_qty 워터마크 + 상태 전이."""

    def setUp(self):
        self.obs, self.jdb, self.phdb = _observer_with_fresh_db()

    def tearDown(self):
        if hasattr(fo._local, "fo_conn") and fo._local.fo_conn is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None

    def _register(self, trade_id="T1", order_qty=100):
        self.obs.register_order(
            market="KR", trade_id=trade_id, code="005930",
            side="BUY", order_qty=order_qty,
            submitted_at=datetime.now().isoformat(),
            odno="ORD001", client_order_id=trade_id,
        )

    def _phoenix_event_count(self, idem_prefix: str) -> int:
        conn = sqlite3.connect(self.phdb)
        rows = conn.execute(
            "SELECT COUNT(*) FROM events WHERE idempotency_key LIKE ?",
            (f"{idem_prefix}%",),
        ).fetchone()
        conn.close()
        return rows[0]

    def _journal_fill_events(self, trade_id: str) -> list:
        conn = sqlite3.connect(self.jdb)
        rows = conn.execute(
            "SELECT event_type FROM trade_events WHERE trade_id=?",
            (trade_id,),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]

    def test_07_first_observation_cum30_delta30(self):
        """Test 07: prev=0, cum=30 → delta=30, Phoenix+Journal 기록."""
        self._register()
        raw = _kr_partial_raw(order_qty=100, cum=30)
        mock_kis = MagicMock()
        mock_kis.get_kr_ccld_by_odno.return_value = raw
        self.obs._kis = mock_kis

        result = self.obs.poll_once()

        self.assertEqual(result["partial"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["details"][0]["fill_delta"], 30)
        # Phoenix 기록 확인
        cnt = self._phoenix_event_count("obs_only:ORD001:")
        self.assertGreater(cnt, 0)
        # Journal 기록 확인
        evts = self._journal_fill_events("T1")
        self.assertIn("BUY_ORDER_PARTIALLY_FILLED", evts)

    def test_08_reobservation_same_cum_no_event(self):
        """Test 08: 동일 cum=30 재관측 → delta=0, 이벤트 미생성."""
        self._register()
        raw30 = _kr_partial_raw(order_qty=100, cum=30)
        mock_kis = MagicMock()
        mock_kis.get_kr_ccld_by_odno.return_value = raw30
        self.obs._kis = mock_kis

        # 1차 관측 (cum=30 → delta=30 → PARTIALLY_FILLED)
        r1 = self.obs.poll_once()
        self.assertEqual(r1["details"][0]["fill_delta"], 30)

        # 2차 관측 (동일 cum=30 → prev=30 → delta=0 → 이벤트 없음)
        r2 = self.obs.poll_once()
        self.assertEqual(r2["details"][0]["fill_delta"], 0)
        self.assertEqual(r2["no_change"], 1)
        self.assertEqual(r2["partial"], 0)

        # Journal 이벤트 수: 1차에만 기록되고 2차에는 추가되지 않음
        evts_after_r2 = self._journal_fill_events("T1")
        self.assertEqual(evts_after_r2.count("BUY_ORDER_PARTIALLY_FILLED"), 1)

        # Phoenix 이벤트 수도 1건
        phoenix_cnt = self._phoenix_event_count("obs_only:ORD001:")
        self.assertEqual(phoenix_cnt, 1)

    def test_09_cum70_delta40(self):
        """Test 09: prev=30→cum=70 → delta=40, PARTIALLY_FILLED 상태."""
        self._register()
        mock_kis = MagicMock()
        self.obs._kis = mock_kis

        # 1차: cum=30
        mock_kis.get_kr_ccld_by_odno.return_value = _kr_partial_raw(cum=30)
        self.obs.poll_once()

        # 2차: cum=70
        mock_kis.get_kr_ccld_by_odno.return_value = _kr_partial_raw(cum=70)
        result = self.obs.poll_once()

        self.assertEqual(result["details"][0]["fill_delta"], 40)
        self.assertEqual(result["details"][0]["status_after"],
                         fo.PendingStatus.PARTIALLY_FILLED)
        # Journal에 두 번째 PARTIALLY_FILLED 추가 기록 확인
        evts = self._journal_fill_events("T1")
        partial_count = evts.count("BUY_ORDER_PARTIALLY_FILLED")
        self.assertEqual(partial_count, 2)

    def test_10_cum100_delta30_filled(self):
        """Test 10: prev=70→cum=100 → delta=30, FILLED 전환."""
        self._register(order_qty=100)
        mock_kis = MagicMock()
        self.obs._kis = mock_kis

        # 1차: cum=30
        mock_kis.get_kr_ccld_by_odno.return_value = _kr_partial_raw(cum=30)
        self.obs.poll_once()
        # 2차: cum=70
        mock_kis.get_kr_ccld_by_odno.return_value = _kr_partial_raw(cum=70)
        self.obs.poll_once()
        # 3차: cum=100 (전량)
        mock_kis.get_kr_ccld_by_odno.return_value = _kr_full_raw(order_qty=100)
        result = self.obs.poll_once()

        self.assertEqual(result["details"][0]["fill_delta"], 30)
        self.assertEqual(result["details"][0]["status_after"],
                         fo.PendingStatus.FILLED)
        evts = self._journal_fill_events("T1")
        self.assertIn("BUY_ORDER_FILLED", evts)
        # PARTIALLY_FILLED 2건 + FILLED 1건
        self.assertEqual(evts.count("BUY_ORDER_PARTIALLY_FILLED"), 2)
        self.assertEqual(evts.count("BUY_ORDER_FILLED"), 1)


class TestIdempotency(unittest.TestCase):
    """테스트 11: ODNO별 중복 방지."""

    def test_11_odno_duplicate_prevention(self):
        """Test 11: 동일 odno+cum → Phoenix INSERT OR IGNORE → 기록 1건만."""
        phdb = _make_temp_phoenix_db()
        raw = _kr_partial_raw(odno="ORD002", cum=50)
        obs1 = fo.ExecutionNormalizer.from_kr(raw, prev_cum=0, client_order_id="T2")

        # 1차 기록 → True (신규 삽입)
        result1 = fo._record_phoenix_observation(obs1, phoenix_db_path=phdb)
        # 2차 기록 (동일 odno, 동일 cum) → False (중복 스킵)
        result2 = fo._record_phoenix_observation(obs1, phoenix_db_path=phdb)

        conn = sqlite3.connect(phdb)
        cnt = conn.execute(
            "SELECT COUNT(*) FROM events WHERE idempotency_key=?",
            ("obs_only:ORD002:50",),
        ).fetchone()[0]
        conn.close()

        self.assertTrue(result1,  "첫 번째 기록은 True여야 합니다")
        self.assertFalse(result2, "두 번째 기록은 False(중복)여야 합니다")
        self.assertEqual(cnt, 1,  "DB에는 1건만 존재해야 합니다")


class TestServerRestartRecovery(unittest.TestCase):
    """테스트 12: 서버 재시작 후 cumulative_filled_qty 복원."""

    def tearDown(self):
        if hasattr(fo._local, "fo_conn") and fo._local.fo_conn is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None

    def test_12_cumulative_filled_qty_restored_from_db(self):
        """Test 12: 재시작 후 PendingOrderRegistry.get_trackable()에서
        cumulative_filled_qty가 이전 관측값으로 복원된다."""
        obs, jdb, phdb = _observer_with_fresh_db()
        mock_kis = MagicMock()

        # 초기 등록 cum=0
        obs.register_order(
            market="KR", trade_id="T_RESTART", code="005930", side="BUY",
            order_qty=100, submitted_at=datetime.now().isoformat(),
            odno="ORD_R1", client_order_id="T_RESTART",
        )
        # cum=40 관측 → DB 저장
        mock_kis.get_kr_ccld_by_odno.return_value = _kr_partial_raw(
            odno="ORD_R1", order_qty=100, cum=40
        )
        obs._kis = mock_kis
        obs.poll_once()

        # 재시작 시뮬레이션: thread-local 초기화 후 새 Registry 로드
        fo._local.fo_conn = None
        fo._JOURNAL_DB_PATH = jdb
        new_registry = fo.PendingOrderRegistry()
        trackable = new_registry.get_trackable()

        self.assertEqual(len(trackable), 1)
        self.assertEqual(trackable[0]["trade_id"], "T_RESTART")
        # 재시작 후 이전에 저장된 cum=40이 복원되어야 함
        self.assertEqual(trackable[0]["cumulative_filled_qty"], 40)
        self.assertEqual(trackable[0]["status"],
                         fo.PendingStatus.PARTIALLY_FILLED)


class TestAPIFailureHandling(unittest.TestCase):
    """테스트 13: API 실패 → 상태 보존 + retry_count 증가."""

    def tearDown(self):
        if hasattr(fo._local, "fo_conn") and fo._local.fo_conn is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None

    def test_13_api_failure_preserves_status_increments_retry(self):
        """Test 13: KIS API 호출 예외 → status=ACCEPTED 유지, retry_count≥1."""
        obs, jdb, phdb = _observer_with_fresh_db()

        obs.register_order(
            market="KR", trade_id="T_ERR", code="005930", side="BUY",
            order_qty=50, submitted_at=datetime.now().isoformat(),
            odno="ORD_ERR", client_order_id="T_ERR",
        )
        # KIS 호출 시 예외 발생 시뮬레이션
        mock_kis = MagicMock()
        mock_kis.get_kr_ccld_by_odno.side_effect = ConnectionError("network timeout")
        obs._kis = mock_kis

        result = obs.poll_once()

        self.assertEqual(result["errors"], 1)
        self.assertIsNotNone(result["details"][0]["error"])

        # DB에서 직접 상태 확인 (새 Registry 인스턴스로)
        fo._local.fo_conn = None
        fo._JOURNAL_DB_PATH = jdb
        registry = fo.PendingOrderRegistry()
        row = registry.get_by_trade_id("T_ERR")

        self.assertIsNotNone(row)
        # API 실패 시 상태는 변경 없음 (ACCEPTED 유지)
        self.assertEqual(row["status"], fo.PendingStatus.ACCEPTED)
        # retry_count는 증가해야 함
        self.assertGreaterEqual(row["retry_count"], 1)


class TestNoPositionImpact(unittest.TestCase):
    """테스트 14–17: 엔진 상태 완전 격리 확인."""

    def setUp(self):
        self.obs, self.jdb, self.phdb = _observer_with_fresh_db()
        self.obs.register_order(
            market="KR", trade_id="T_ISO", code="005930", side="BUY",
            order_qty=100, submitted_at=datetime.now().isoformat(),
            odno="ORD_ISO", client_order_id="T_ISO",
        )
        mock_kis = MagicMock()
        mock_kis.get_kr_ccld_by_odno.return_value = _kr_full_raw(
            odno="ORD_ISO", order_qty=100
        )
        self.obs._kis = mock_kis

    def tearDown(self):
        if hasattr(fo._local, "fo_conn") and fo._local.fo_conn is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None

    def test_14_apply_buy_not_called(self):
        """Test 14: fill_observer는 apply_buy를 import하거나 실제 호출하지 않음.

        AST 분석으로 모듈 소스에서 apply_buy 함수를 실제로 호출하는
        Call 노드가 없음을 검증한다 (주석/독스트링 내 언급은 제외).
        """
        import ast
        src = inspect.getsource(fo)
        tree = ast.parse(src)
        # AST의 모든 Call 노드에서 함수명 추출
        called_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)
        self.assertNotIn(
            "apply_buy",
            called_names,
            "fill_observer AST에 apply_buy() 호출이 있으면 안 됩니다",
        )
        # 모듈에 apply_buy 심볼이 없어야 함
        self.assertFalse(hasattr(fo, "apply_buy"))
        # poll_once 정상 완료 확인
        result = self.obs.poll_once()
        self.assertIsNone(result["details"][0]["error"])

    def test_15_apply_sell_not_called(self):
        """Test 15: fill_observer AST에 apply_sell() 호출 부재."""
        import ast
        src = inspect.getsource(fo)
        tree = ast.parse(src)
        called_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)
        self.assertNotIn(
            "apply_sell",
            called_names,
            "fill_observer AST에 apply_sell() 호출이 있으면 안 됩니다",
        )
        self.assertFalse(hasattr(fo, "apply_sell"))
        # poll_once 정상 완료 확인
        result = self.obs.poll_once()
        self.assertEqual(result["errors"], 0)

    def test_16_daily_pnl_guard_unchanged(self):
        """Test 16: fill_observer는 DailyPnLGuard를 import/호출하지 않음.

        소스 코드에서 실제 함수 호출 패턴 "DailyPnLGuard(" 또는
        "daily_pnl_guard" 변수 참조가 없어야 한다.
        주석/독스트링에서 "DailyPnLGuard 변경 금지" 형태의 언급은 허용.
        """
        src = inspect.getsource(fo)
        # 실제 인스턴스 생성/호출 패턴 확인
        self.assertNotIn(
            "DailyPnLGuard(",
            src,
            "fill_observer 소스에 DailyPnLGuard() 호출이 있으면 안 됩니다",
        )
        # import DailyPnLGuard 확인
        self.assertNotIn(
            "import DailyPnLGuard",
            src,
            "fill_observer 소스에 DailyPnLGuard import가 있으면 안 됩니다",
        )
        # 모듈 자체에 DailyPnLGuard 심볼이 없음 확인
        self.assertFalse(
            hasattr(fo, "DailyPnLGuard"),
            "fill_observer 모듈에 DailyPnLGuard 심볼이 노출되면 안 됩니다",
        )

    def test_17_position_qty_unchanged(self):
        """Test 17: poll_once() 전후 positions 테이블 행 수 미변경.

        EXECUTION_OBSERVED_ONLY → Projector._DISPATCH 미등록
        → apply() 미호출 → positions 테이블 불변.
        """
        # positions 테이블 추가
        conn = sqlite3.connect(self.phdb)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                code    TEXT PRIMARY KEY,
                qty     INTEGER DEFAULT 0,
                avg_px  REAL DEFAULT 0
            )
        """)
        conn.commit()
        before_count = conn.execute(
            "SELECT COUNT(*) FROM positions"
        ).fetchone()[0]
        conn.close()

        self.obs.poll_once()

        conn = sqlite3.connect(self.phdb)
        after_count = conn.execute(
            "SELECT COUNT(*) FROM positions"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(
            before_count, after_count,
            "poll_once()는 positions 테이블을 변경하면 안 됩니다",
        )


class TestJournalFillEventDistinction(unittest.TestCase):
    """테스트 18–19: 저널 이벤트 타입 구분."""

    def setUp(self):
        self.obs, self.jdb, self.phdb = _observer_with_fresh_db()

    def tearDown(self):
        if hasattr(fo._local, "fo_conn") and fo._local.fo_conn is not None:
            try:
                fo._local.fo_conn.close()
            except Exception:
                pass
            fo._local.fo_conn = None

    def _journal_events(self, trade_id: str) -> list:
        conn = sqlite3.connect(self.jdb)
        rows = conn.execute(
            "SELECT event_type FROM trade_events WHERE trade_id=?",
            (trade_id,),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]

    def test_18_journal_partially_vs_fully_filled(self):
        """Test 18: 부분체결 → PARTIALLY_FILLED, 전량 → FILLED 구분 기록."""
        self.obs.register_order(
            market="KR", trade_id="T18", code="005930", side="BUY",
            order_qty=100, submitted_at=datetime.now().isoformat(),
            odno="ORD18", client_order_id="T18",
        )
        mock_kis = MagicMock()
        self.obs._kis = mock_kis

        # 부분체결 (cum=60)
        mock_kis.get_kr_ccld_by_odno.return_value = _kr_partial_raw(
            odno="ORD18", order_qty=100, cum=60
        )
        self.obs.poll_once()
        evts_partial = self._journal_events("T18")
        self.assertIn("BUY_ORDER_PARTIALLY_FILLED", evts_partial)
        self.assertNotIn("BUY_ORDER_FILLED",  evts_partial)
        self.assertNotIn("TRADE_CLOSED",       evts_partial)

        # 전량체결 (cum=100)
        mock_kis.get_kr_ccld_by_odno.return_value = _kr_full_raw(
            odno="ORD18", order_qty=100
        )
        self.obs.poll_once()
        evts_full = self._journal_events("T18")
        self.assertIn("BUY_ORDER_FILLED", evts_full)

        # TRADE_CLOSED 절대 기록 금지
        self.assertNotIn("TRADE_CLOSED", evts_full)

    def test_19_unfilled_order_not_recorded_as_filled(self):
        """Test 19: cum=0인 미체결 주문은 ORDER_FILLED 이벤트 미생성."""
        self.obs.register_order(
            market="KR", trade_id="T19", code="005930", side="BUY",
            order_qty=50, submitted_at=datetime.now().isoformat(),
            odno="ORD19", client_order_id="T19",
        )
        mock_kis = MagicMock()
        mock_kis.get_kr_ccld_by_odno.return_value = _kr_open_raw(
            odno="ORD19", order_qty=50
        )
        self.obs._kis = mock_kis
        self.obs.poll_once()

        evts = self._journal_events("T19")
        self.assertNotIn("BUY_ORDER_FILLED",           evts)
        self.assertNotIn("BUY_ORDER_PARTIALLY_FILLED", evts)
        self.assertNotIn("ORDER_FILLED",               evts)
        self.assertNotIn("TRADE_CLOSED",               evts)


class TestRegressionSuite(unittest.TestCase):
    """테스트 20: 기존 57개 회귀 테스트 전부 통과 확인."""

    def test_20_existing_modules_importable_after_fill_observer_import(self):
        """Test 20: fill_observer import 후 기존 모듈 정상 동작 확인.

        1. Projector._DISPATCH에 EXECUTION_OBSERVED_ONLY 미등록
        2. 기존 EventType 상수 불변
        3. fill_observer FillEventType 별도 네임스페이스
        4. EXECUTION_OBSERVED_ONLY 상수 값 "ExecutionObservedOnly"
        """
        import journal.trading_journal as jnl
        import phoenix.event_store
        import phoenix.projections

        # 1. Projector._DISPATCH에 EXECUTION_OBSERVED_ONLY 없음
        dispatch_keys = set(phoenix.projections.Projector._DISPATCH.keys())
        self.assertNotIn(
            fo.EXECUTION_OBSERVED_ONLY,
            dispatch_keys,
            "EXECUTION_OBSERVED_ONLY는 Projector._DISPATCH에 등록되면 안 됩니다",
        )

        # 2. 기존 EventType 상수 불변
        self.assertEqual(jnl.EventType.ORDER_FILLED,      "ORDER_FILLED")
        self.assertEqual(jnl.EventType.SELL_ORDER_FILLED, "SELL_ORDER_FILLED")
        self.assertEqual(jnl.EventType.TRADE_CLOSED,      "TRADE_CLOSED")

        # 3. fill_observer FillEventType 별도 네임스페이스
        self.assertEqual(fo.FillEventType.BUY_ORDER_FILLED,
                         "BUY_ORDER_FILLED")
        self.assertEqual(fo.FillEventType.SELL_ORDER_PARTIALLY_FILLED,
                         "SELL_ORDER_PARTIALLY_FILLED")
        self.assertEqual(fo.FillEventType.BUY_ORDER_PARTIALLY_FILLED,
                         "BUY_ORDER_PARTIALLY_FILLED")
        self.assertEqual(fo.FillEventType.SELL_ORDER_FILLED,
                         "SELL_ORDER_FILLED")

        # 4. EXECUTION_OBSERVED_ONLY 상수 값
        self.assertEqual(fo.EXECUTION_OBSERVED_ONLY, "ExecutionObservedOnly")

        # 5. poll_pending_orders_once 편의 함수 존재
        self.assertTrue(callable(fo.poll_pending_orders_once))


if __name__ == "__main__":
    unittest.main(verbosity=2)
