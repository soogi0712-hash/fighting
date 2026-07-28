"""
KIS 체결 관측·기록 모듈 (단계 1: 관측 전용)
=============================================

목적:
  KIS 국내·미국 주문의 실제 체결 사실을 조회하여
  Phoenix EventStore와 trading_journal.db에 "관측 기록"만 남긴다.

절대 금지 사항 (이번 단계):
  - apply_buy()  호출 금지
  - apply_sell() 호출 금지
  - 포지션 수량 변경 금지
  - DailyPnLGuard 변경 금지
  - 쿨다운 / 복리풀 / 매수·매도 신호 변경 금지
  - TRADE_CLOSED 기록 금지
  - daily_trade_summary 갱신 금지
  - trade_entries.state = CLOSED 변경 금지

핵심 원칙:
  1. fill_delta_qty <= 0 → 새 이벤트 생성 안 함 (중복 방지)
  2. Phoenix에는 EXECUTION_OBSERVED_ONLY 타입으로 기록
     → Projector _DISPATCH에 없으므로 포지션에 절대 영향 없음
  3. Journal에는 BUY_ORDER_PARTIALLY_FILLED / BUY_ORDER_FILLED /
     SELL_ORDER_PARTIALLY_FILLED / SELL_ORDER_FILLED 만 기록
  4. 모든 기록 실패는 예외를 상위로 전파하지 않고 오류 카운터 증가
  5. API 호출 실패 시 pending order 상태 유지, retry_count만 증가

모듈 구성:
  - ExecutionObservation : 공통 정규화 모델 (dataclass)
  - ExecutionNormalizer  : KR/US 응답 → ExecutionObservation 변환
  - PendingOrderRegistry : pending_orders 테이블 CRUD (trading_journal.db)
  - FillObserver         : poll_pending_orders_once() 메인 로직
  - poll_pending_orders_once() : 모듈 레벨 편의 함수
"""

from __future__ import annotations

import json
import sqlite3
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Any

from utils.logger import get_logger

logger = get_logger("FillObserver")

# ── DB 경로 (trading_journal.db 공유) ──────────────────────────
_JOURNAL_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "trading_journal.db"
)

# ── Thread-local 연결 (journal과 독립) ─────────────────────────
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """thread-local SQLite 연결 반환."""
    if not hasattr(_local, "fo_conn") or _local.fo_conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(_JOURNAL_DB_PATH)), exist_ok=True)
        conn = sqlite3.connect(_JOURNAL_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.fo_conn = conn
    return _local.fo_conn


# ──────────────────────────────────────────────────────────────
# pending_orders 테이블 DDL
# ──────────────────────────────────────────────────────────────
_PENDING_DDL = """
CREATE TABLE IF NOT EXISTS pending_orders (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    market               TEXT    NOT NULL,          -- KR / US
    trade_id             TEXT    NOT NULL,           -- journal trade_id
    code                 TEXT    NOT NULL,           -- 종목코드 / ticker
    side                 TEXT    NOT NULL,           -- BUY / SELL
    odno                 TEXT    NOT NULL DEFAULT '', -- KIS 주문번호 (접수 직후 채워짐)
    client_order_id      TEXT    NOT NULL DEFAULT '', -- Phoenix client_order_id
    order_qty            INTEGER NOT NULL DEFAULT 0,
    cumulative_filled_qty INTEGER NOT NULL DEFAULT 0, -- 마지막으로 확인된 누적 체결 수량
    status               TEXT    NOT NULL DEFAULT 'ACCEPTED',
    submitted_at         TEXT    NOT NULL,           -- 접수 시각 (ISO8601)
    last_checked_at      TEXT,                       -- 마지막 체결조회 시각
    retry_count          INTEGER NOT NULL DEFAULT 0,
    raw_order_response   TEXT,                       -- KIS 주문 응답 JSON
    exchange             TEXT,                       -- US만: NASD / NYSE / AMEX
    currency             TEXT    DEFAULT 'KRW',
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime')),
    updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_po_status   ON pending_orders(status);
CREATE INDEX IF NOT EXISTS idx_po_market   ON pending_orders(market);
CREATE INDEX IF NOT EXISTS idx_po_odno     ON pending_orders(odno);
CREATE INDEX IF NOT EXISTS idx_po_trade_id ON pending_orders(trade_id);
"""

# ★ P0-5: trade_id 는 주문 1건을 가리키는 식별자인데 UNIQUE 제약이 없어서
#   update_fill()/increment_retry() 의 WHERE trade_id=? 가 여러 행을 동시에
#   갱신하고 get_by_trade_id() 가 비결정적으로 동작했다.
#   기존 중복 행을 정리한 뒤 UNIQUE 인덱스를 만든다.
_PENDING_DEDUPE_SQL = """
DELETE FROM pending_orders
 WHERE id NOT IN (SELECT MAX(id) FROM pending_orders GROUP BY trade_id);
"""
_PENDING_UNIQUE_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_po_trade_id ON pending_orders(trade_id);
"""

# pending_orders.status 상수
class PendingStatus:
    ACCEPTED         = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED           = "FILLED"
    REJECTED         = "REJECTED"
    CANCELLED        = "CANCELLED"
    EXPIRED          = "EXPIRED"
    UNKNOWN          = "UNKNOWN"

    # 추적 대상 상태 (조회 대상)
    TRACKABLE = frozenset({ACCEPTED, PARTIALLY_FILLED})


# ── 체결조회 재시도 상한 (P0-5) ────────────────────────────────
# 이 횟수를 넘도록 체결조회가 실패/무응답이면 EXPIRED 로 종결해 폴링에서 제외한다.
# 상한이 없으면 취소·거부된 주문이 영구히 KIS 체결조회 API 를 소모한다.
MAX_POLL_RETRY = 40


# ──────────────────────────────────────────────────────────────
# 주문 응답 파싱 헬퍼 (P0-5)
# ──────────────────────────────────────────────────────────────
# KIS 주문 접수 응답의 주문번호 키. 국내/해외 모두 output.ODNO 가 정식이며,
# 소문자 odno 는 조회계 API 가 쓰는 표기다. KNO_ORD_NO 는 이 저장소에만
# 존재하던 잘못된 키로, 하위호환을 위해 마지막 후보로만 남긴다.
_ODNO_KEYS = ("ODNO", "odno", "KRX_FWDG_ORD_ODNO", "ODER_NO", "KNO_ORD_NO")


def _as_price(val) -> Optional[float]:
    """체결가 정규화. 값이 없거나 숫자가 아니면 None, 그 외에는 float 그대로.

    0.0 을 None 으로 바꾸지 않는 것이 핵심이다 — 기존 `float(x) or None` 은
    체결가 0 을 '값 없음' 으로 뭉개 하위 로직을 스킵시켰다.
    """
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def is_dry_run_response(order_response: dict) -> bool:
    """LIVE_ORDER_ENABLED=false 로 인해 실제 제출되지 않은 응답인지 판정."""
    if not isinstance(order_response, dict):
        return False
    return bool(order_response.get("_dry_run") or order_response.get("_live_disabled"))


def extract_odno(order_response: dict) -> str:
    """KIS 주문 접수 응답에서 주문번호(odno)를 추출한다.

    국내·해외 공통. output 하위에서 _ODNO_KEYS 순서로 탐색하고,
    없으면 최상위에서도 한 번 더 찾는다(일부 폴백 응답 대비).
    추출 실패 시 빈 문자열을 반환한다 — 호출자는 반드시 이를 확인해
    pending 등록을 건너뛰어야 한다(빈 odno 는 남의 체결을 자기 것으로
    오인하는 원인이 된다).
    """
    if not isinstance(order_response, dict):
        return ""
    for container in (order_response.get("output") or {}, order_response):
        if not isinstance(container, dict):
            continue
        for key in _ODNO_KEYS:
            val = container.get(key)
            if val is None:
                continue
            s = str(val).strip()
            if s:
                return s
    return ""


# ──────────────────────────────────────────────────────────────
# ExecutionObservation — 공통 정규화 모델
# ──────────────────────────────────────────────────────────────
@dataclass
class ExecutionObservation:
    """KR / US KIS 체결 응답을 공통 형태로 정규화한 관측 결과.

    fill_delta_qty = cumulative_filled_qty - previous_cumulative_filled_qty
    fill_delta_qty <= 0 이면 신규 이벤트를 생성하지 않는다.
    """
    market:                       str
    code:                         str
    side:                         Optional[str]    # "BUY" | "SELL" | None
    odno:                         Optional[str]
    client_order_id:              Optional[str]
    order_qty:                    Optional[int]
    cumulative_filled_qty:        int
    previous_cumulative_filled_qty: int
    fill_delta_qty:               int              # = cum - prev (≤0 → skip)
    unfilled_qty:                 Optional[int]
    average_fill_price:           Optional[float]
    order_status:                 Optional[str]
    observed_at:                  str              # ISO8601
    order_time:                   Optional[str]    # HHMMSS
    exchange:                     Optional[str]    # US only
    currency:                     str              # "KRW" | "USD"
    raw_payload:                  dict = field(default_factory=dict)

    # ── 파생 상태 ──────────────────────────────────────────────
    @property
    def is_new_fill(self) -> bool:
        """새로운 체결이 있으면 True."""
        return self.fill_delta_qty > 0

    @property
    def is_fully_filled(self) -> bool:
        """전량 체결 여부."""
        if self.order_qty and self.order_qty > 0:
            return self.cumulative_filled_qty >= self.order_qty
        return False

    @property
    def is_partially_filled(self) -> bool:
        """부분 체결 여부 (체결 있고 전량 미완성)."""
        if not self.is_new_fill:
            return False
        return not self.is_fully_filled


# ──────────────────────────────────────────────────────────────
# ExecutionNormalizer — KR/US 응답 → ExecutionObservation
# ──────────────────────────────────────────────────────────────
class ExecutionNormalizer:
    """KIS get_kr_ccld_by_odno / get_us_ccld 응답을 ExecutionObservation으로 변환."""

    @staticmethod
    def from_kr(
        raw: dict,
        prev_cum: int = 0,
        client_order_id: Optional[str] = None,
    ) -> Optional[ExecutionObservation]:
        """국내주식 체결조회 응답(get_kr_ccld_by_odno 반환값)을 정규화.

        raw: get_kr_ccld_by_odno() 반환 dict
             필수: "code", "side", "odno", "order_qty",
                   "cum_filled_qty", "unfilled_qty", "avg_fill_price"
        prev_cum: pending_orders.cumulative_filled_qty (DB에 저장된 이전 누적)
        """
        if not raw:
            return None
        cum = int(raw.get("cum_filled_qty", 0) or 0)
        delta = cum - prev_cum
        return ExecutionObservation(
            market=                       "KR",
            code=                         raw.get("code", ""),
            side=                         raw.get("side", None),
            odno=                         raw.get("odno", None),
            client_order_id=              client_order_id,
            order_qty=                    int(raw.get("order_qty", 0) or 0) or None,
            cumulative_filled_qty=        cum,
            previous_cumulative_filled_qty= prev_cum,
            fill_delta_qty=               delta,
            unfilled_qty=                 int(raw.get("unfilled_qty", 0) or 0),
            # ★ P0-5: 기존 `float(...) or None` 은 체결가 0.0 을 None 으로 뭉개
            #   trade_entries.fill_confirmed 갱신과 apply_buy 가격 전달을 막았다.
            #   값이 없을 때만 None 이 되도록 명시적으로 판정한다.
            average_fill_price=           _as_price(raw.get("avg_fill_price")),
            order_status=                 raw.get("order_status", None),
            observed_at=                  datetime.now().isoformat(),
            order_time=                   raw.get("order_time", None),
            exchange=                     None,
            currency=                     "KRW",
            raw_payload=                  raw,
        )

    @staticmethod
    def from_us(
        raw: dict,
        prev_cum: int = 0,
        client_order_id: Optional[str] = None,
    ) -> Optional[ExecutionObservation]:
        """미국주식 체결조회 응답(get_us_ccld 반환값)을 정규화.

        raw: get_us_ccld() 반환 dict
        prev_cum: pending_orders.cumulative_filled_qty
        """
        if not raw:
            return None
        cum = int(raw.get("cum_filled_qty", 0) or 0) if raw.get("cum_filled_qty") is not None else 0
        delta = cum - prev_cum
        order_qty_raw = raw.get("order_qty", None)
        return ExecutionObservation(
            market=                       "US",
            code=                         raw.get("code", "") or "",
            side=                         raw.get("side", None),
            odno=                         raw.get("odno", None),
            client_order_id=              client_order_id,
            order_qty=                    int(order_qty_raw) if order_qty_raw is not None else None,
            cumulative_filled_qty=        cum,
            previous_cumulative_filled_qty= prev_cum,
            fill_delta_qty=               delta,
            unfilled_qty=                 raw.get("unfilled_qty", None),
            average_fill_price=           _as_price(raw.get("avg_fill_price")),
            order_status=                 raw.get("order_status", None),
            observed_at=                  datetime.now().isoformat(),
            order_time=                   raw.get("order_time", None),
            exchange=                     raw.get("exchange", None),
            currency=                     "USD",
            raw_payload=                  raw,
        )


# ──────────────────────────────────────────────────────────────
# PendingOrderRegistry — pending_orders CRUD
# ──────────────────────────────────────────────────────────────
class PendingOrderRegistry:
    """trading_journal.db 내 pending_orders 테이블 CRUD.

    재시작 후에도 미체결 주문을 추적할 수 있도록
    ACCEPTED / PARTIALLY_FILLED 상태를 영속적으로 저장한다.
    """

    def __init__(self):
        self._init_table()

    def _init_table(self):
        conn = _get_conn()
        conn.executescript(_PENDING_DDL)
        # 기존 trading_journal.db에 테이블이 없으면 DDL로 생성
        # (trading_journal._init_db()와 독립적으로 관리)
        conn.commit()
        # ★ P0-5: trade_id UNIQUE 마이그레이션 (중복 정리 후 인덱스 생성)
        try:
            removed = conn.execute(_PENDING_DEDUPE_SQL).rowcount
            conn.executescript(_PENDING_UNIQUE_SQL)
            conn.commit()
            if removed and removed > 0:
                logger.warning(
                    f"[PendingRegistry] trade_id 중복 행 {removed}건 정리 후 "
                    f"UNIQUE 인덱스 생성"
                )
        except Exception as e:
            # 인덱스 생성 실패해도 기존 동작은 유지 (경고만)
            logger.warning(f"[PendingRegistry] trade_id UNIQUE 마이그레이션 실패: {e}")

    # ── 등록 ──────────────────────────────────────────────────
    def register(
        self,
        market: str,
        trade_id: str,
        code: str,
        side: str,
        order_qty: int,
        submitted_at: str,
        odno: str = "",
        client_order_id: str = "",
        raw_order_response: Optional[dict] = None,
        exchange: Optional[str] = None,
        currency: str = "KRW",
    ) -> int:
        """신규 pending order 등록. 반환: 삽입된 행 id (거부 시 0).

        ★ P0-5: odno 가 비어 있으면 등록하지 않는다.
          odno 없는 행은 get_kr_ccld_by_odno(odno="") 로 조회되어
          당일 첫 체결 레코드를 자기 것으로 오인한다.
        """
        odno = (odno or "").strip()
        if not odno:
            logger.error(
                f"[PendingRegistry] odno 없음 — 등록 거부 (오인 매칭 방지): "
                f"market={market} code={code} side={side} trade_id={trade_id}"
            )
            return 0

        conn = _get_conn()
        raw_json = json.dumps(raw_order_response or {}, ensure_ascii=False)
        now = datetime.now().isoformat()
        # trade_id 는 UNIQUE — 재시작 후 동일 주문 재등록 시 갱신으로 흡수한다.
        cur = conn.execute(
            """
            INSERT INTO pending_orders
              (market, trade_id, code, side, odno, client_order_id,
               order_qty, cumulative_filled_qty, status,
               submitted_at, last_checked_at, retry_count,
               raw_order_response, exchange, currency,
               created_at, updated_at)
            VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?)
            ON CONFLICT(trade_id) DO UPDATE SET
               odno            = excluded.odno,
               client_order_id = excluded.client_order_id,
               order_qty       = excluded.order_qty,
               exchange        = excluded.exchange,
               currency        = excluded.currency,
               updated_at      = excluded.updated_at
            """,
            (
                market, trade_id, code, side, odno, client_order_id,
                order_qty, 0, PendingStatus.ACCEPTED,
                submitted_at, None, 0,
                raw_json, exchange, currency,
                now, now,
            ),
        )
        conn.commit()
        logger.info(
            f"[PendingRegistry] 등록: market={market} code={code} side={side} "
            f"odno={odno!r} trade_id={trade_id}"
        )
        return cur.lastrowid

    # ── ODNO 업데이트 (접수 후 ondo 수신 시) ──────────────────
    def update_odno(self, trade_id: str, odno: str) -> bool:
        """pending order의 odno를 업데이트한다 (접수 응답 후 odno 수신 시)."""
        conn = _get_conn()
        conn.execute(
            "UPDATE pending_orders SET odno=?, updated_at=? WHERE trade_id=?",
            (odno, datetime.now().isoformat(), trade_id),
        )
        conn.commit()
        return True

    # ── 체결 수량 + 상태 업데이트 ─────────────────────────────
    def update_fill(
        self,
        trade_id: str,
        new_cum: int,
        new_status: str,
    ) -> bool:
        """체결 관측 후 cumulative_filled_qty 및 status 갱신."""
        now = datetime.now().isoformat()
        conn = _get_conn()
        conn.execute(
            """UPDATE pending_orders
               SET cumulative_filled_qty=?, status=?,
                   last_checked_at=?, updated_at=?
               WHERE trade_id=?""",
            (new_cum, new_status, now, now, trade_id),
        )
        conn.commit()
        return True

    # ── 조회 실패 시 retry_count 증가 ─────────────────────────
    def increment_retry(self, trade_id: str) -> bool:
        """API 호출 실패 시 retry_count 증가. 상태는 변경하지 않는다."""
        now = datetime.now().isoformat()
        conn = _get_conn()
        conn.execute(
            """UPDATE pending_orders
               SET retry_count = retry_count + 1,
                   last_checked_at=?, updated_at=?
               WHERE trade_id=?""",
            (now, now, trade_id),
        )
        conn.commit()
        return True

    # ── 상태 강제 전이 (취소/만료 반영용) ──────────────────────
    def set_status(self, trade_id: str, new_status: str) -> bool:
        """pending order 의 status 를 직접 전이한다 (P0-5).

        용도: 재시도 상한 초과 → EXPIRED, 외부 취소 감지 → CANCELLED.
        폴링 대상에서 빠지게 하는 것이 목적이다.
        """
        now = datetime.now().isoformat()
        conn = _get_conn()
        conn.execute(
            "UPDATE pending_orders SET status=?, last_checked_at=?, updated_at=? "
            "WHERE trade_id=?",
            (new_status, now, now, trade_id),
        )
        conn.commit()
        return True

    # ── 추적 대상 조회 ─────────────────────────────────────────
    def get_trackable(self, market: Optional[str] = None,
                      max_retry: int = MAX_POLL_RETRY) -> list[dict]:
        """추적 대상(ACCEPTED / PARTIALLY_FILLED) 주문 목록 반환.

        ★ P0-5 필터 3가지:
          1. market — KR/US 레지스트리가 같은 테이블을 공유하므로 지정하지 않으면
             KR 폴러가 US 주문을 조회하고 그 반대도 발생해 중복 dispatch 가 된다.
          2. odno != '' — 빈 odno 는 남의 체결에 오인 매칭된다.
          3. retry_count < max_retry — 상한 없는 재시도는 KIS API 를 영구 소모한다.
        """
        status_list = sorted(PendingStatus.TRACKABLE)
        sql = (
            "SELECT * FROM pending_orders "
            f"WHERE status IN ({','.join('?' * len(status_list))}) "
            "  AND TRIM(COALESCE(odno,'')) != '' "
            "  AND retry_count < ? "
        )
        params: list = [*status_list, int(max_retry)]
        if market:
            sql += "  AND market = ? "
            params.append(market)
        sql += "ORDER BY submitted_at"

        conn = _get_conn()
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── 상태별 조회 (P0-4a: EXPIRED 복구 감사용) ───────────────
    def get_by_status(self, status: str,
                      since_iso: Optional[str] = None) -> list[dict]:
        """특정 status 의 주문 목록. since_iso 지정 시 updated_at >= since_iso 만."""
        conn = _get_conn()
        if since_iso:
            rows = conn.execute(
                "SELECT * FROM pending_orders WHERE status=? AND updated_at>=? "
                "ORDER BY updated_at",
                (status, since_iso),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pending_orders WHERE status=? ORDER BY updated_at",
                (status,),
            ).fetchall()
        return [dict(r) for r in rows]

    def restore_from_terminal(self, trade_id: str, new_cum: int,
                              new_status: str = None) -> bool:
        """[정정] EXPIRED/CANCELLED 로 마킹됐던 pending 을 실제 체결 증거에 근거해
        FILLED(또는 PARTIALLY_FILLED)로 되돌린다. P0-4a 복구 전용.

        ★ 현재 status 가 EXPIRED/CANCELLED 인 경우에만 동작(멱등: 이미 FILLED 면 no-op).
        """
        new_status = new_status or PendingStatus.FILLED
        now = datetime.now().isoformat()
        conn = _get_conn()
        cur = conn.execute(
            """UPDATE pending_orders
               SET cumulative_filled_qty=?, status=?, last_checked_at=?, updated_at=?
               WHERE trade_id=? AND status IN (?,?)""",
            (new_cum, new_status, now, now, trade_id,
             PendingStatus.EXPIRED, PendingStatus.CANCELLED),
        )
        conn.commit()
        changed = cur.rowcount > 0
        if changed:
            logger.warning(
                "[PendingRegistry] terminal→%s 정정(체결 증거): trade_id=%s cum=%s",
                new_status, trade_id, new_cum,
            )
        return changed

    # ── stale 후보 조회 (P0-4) ─────────────────────────────────
    def get_stale_trackable(self, max_age_sec: int,
                            now: Optional[datetime] = None) -> list[dict]:
        """TRACKABLE(ACCEPTED/PARTIALLY_FILLED) 중 submitted_at 이 max_age_sec 이상
        경과한 주문 목록. (KIS 검증 대상 = stale 후보)

        now 미지정 시 datetime.now() 사용(테스트에서 주입 가능).
        """
        ref = now or datetime.now()
        rows = self.get_trackable()
        stale = []
        for r in rows:
            sub = r.get("submitted_at") or ""
            try:
                ts = datetime.fromisoformat(sub)
            except (ValueError, TypeError):
                # 파싱 불가한 submitted_at 은 안전하게 stale 후보에서 제외
                continue
            if (ref - ts).total_seconds() >= max_age_sec:
                stale.append(r)
        return stale

    # ── 재시도 상한 초과 주문 조회 ─────────────────────────────
    def get_retry_exhausted(self, market: Optional[str] = None,
                            max_retry: int = MAX_POLL_RETRY) -> list[dict]:
        """추적 상태이지만 재시도 상한을 넘겨 만료 처리해야 할 주문 목록."""
        status_list = sorted(PendingStatus.TRACKABLE)
        sql = (
            "SELECT * FROM pending_orders "
            f"WHERE status IN ({','.join('?' * len(status_list))}) "
            "  AND retry_count >= ? "
        )
        params: list = [*status_list, int(max_retry)]
        if market:
            sql += "  AND market = ? "
            params.append(market)
        sql += "ORDER BY submitted_at"

        conn = _get_conn()
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ── 단말 상태 마킹 (P0-4) ──────────────────────────────────
    def mark_terminal(self, trade_id: str, status: str,
                      reason: Optional[str] = None) -> bool:
        """pending order 를 단말 상태(CANCELLED/REJECTED/EXPIRED)로 마킹한다.

        ★ idempotent + 안전: 현재 TRACKABLE(ACCEPTED/PARTIALLY_FILLED) 인 경우에만
          전이한다. 이미 FILLED/terminal 이면 아무 것도 하지 않는다(반환 False).
          체결(FILLED)을 단말로 덮어써 회계를 잃는 사고를 방지한다.
        ★ 회계 무개입: cumulative_filled_qty 는 건드리지 않는다(관측값 보존).

        Returns:
            True  — 실제로 TRACKABLE → 단말 로 전이함
            False — 대상 없음 또는 이미 terminal(멱등 no-op)
        """
        if status not in (PendingStatus.CANCELLED, PendingStatus.REJECTED,
                          PendingStatus.EXPIRED):
            raise ValueError(f"mark_terminal 은 단말 상태만 허용: {status!r}")
        now = datetime.now().isoformat()
        conn = _get_conn()
        cur = conn.execute(
            """UPDATE pending_orders
               SET status=?, last_checked_at=?, updated_at=?
               WHERE trade_id=? AND status IN (?,?)""",
            (status, now, now, trade_id,
             PendingStatus.ACCEPTED, PendingStatus.PARTIALLY_FILLED),
        )
        conn.commit()
        changed = cur.rowcount > 0
        if changed:
            logger.info(
                "[PendingRegistry] 단말 마킹: trade_id=%s → %s (reason=%s)",
                trade_id, status, reason,
            )
        return changed

    # ── in-flight 매도 가드 (P0-0) ─────────────────────────────
    def has_active_order(self, market: str, code: str, side: str) -> bool:
        """해당 (market, code, side)에 ACTIVE(ACCEPTED/PARTIALLY_FILLED) 주문이 있는가.

        accept↔fill 창에서 apply_sell/apply_buy 가 아직 실행되지 않아 포지션이
        full-qty 로 남아 있을 때, 동일 종목에 대한 중복 주문(이중매도 등)을
        막기 위한 durable 조회.
        """
        conn = _get_conn()
        row = conn.execute(
            "SELECT 1 FROM pending_orders "
            "WHERE market=? AND code=? AND side=? AND status IN (?,?) LIMIT 1",
            (market, code, side,
             PendingStatus.ACCEPTED, PendingStatus.PARTIALLY_FILLED),
        ).fetchone()
        return row is not None

    def has_active_sell(self, market: str, code: str) -> bool:
        """해당 종목에 미체결(ACTIVE) 매도 주문이 있으면 True."""
        return self.has_active_order(market, code, "SELL")

    # ── trade_id로 단건 조회 ───────────────────────────────────
    def get_by_trade_id(self, trade_id: str) -> Optional[dict]:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM pending_orders WHERE trade_id=? ORDER BY id DESC",
            (trade_id,),
        ).fetchone()
        return dict(row) if row else None

    # ── 전체 목록 조회 ─────────────────────────────────────────
    def get_all(
        self,
        status: Optional[str] = None,
        market: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        conn = _get_conn()
        wheres, params = [], []
        if status:
            wheres.append("status=?"); params.append(status)
        if market:
            wheres.append("market=?"); params.append(market)
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM pending_orders {where_sql} ORDER BY submitted_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────
# Phoenix EXECUTION_OBSERVED_ONLY 이벤트 타입 + 격리 기록
# ──────────────────────────────────────────────────────────────

# 이 이벤트 타입은 Phoenix Projector._DISPATCH 에 등록되지 않으므로
# 포지션(positions) / order_index / daily_pnl 에 절대 영향을 주지 않는다.
EXECUTION_OBSERVED_ONLY = "ExecutionObservedOnly"  # noqa: N816


def _record_phoenix_observation(
    obs: ExecutionObservation,
    phoenix_db_path: Optional[str] = None,
) -> bool:
    """Phoenix EventStore에 EXECUTION_OBSERVED_ONLY 이벤트를 append.

    ★ 포지션 투영 안전 격리:
      - 이벤트 타입 = EXECUTION_OBSERVED_ONLY
      - Projector._DISPATCH 에 없음 → apply() 시 projection 무변경
      - 향후 실제 포지션 반영이 승인되면 EventType을 EXECUTION_OBSERVED로
        교체하는 것만으로 투영 활성화 가능.

    idempotency_key = "obs_only:{odno_or_coid}:{cum_filled_qty}"
    → 동일 주문·동일 누적 수량 중복 기록 방지.

    phoenix_db_path: None 이면 기본 data/phoenix.db 사용.
    """
    if not obs.is_new_fill:
        return False  # delta <= 0 → 기록 불필요

    try:
        if phoenix_db_path is None:
            phoenix_db_path = os.path.join(
                os.path.dirname(__file__), "..", "data", "phoenix.db"
            )

        # phoenix.db 직접 연결 (EventStore 없이 단순 INSERT)
        # Projector 미호출 → 포지션 영향 없음
        conn = sqlite3.connect(phoenix_db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        ok = _odno_or_coid = obs.odno or obs.client_order_id or "UNKNOWN"
        idem_key = f"obs_only:{ok}:{obs.cumulative_filled_qty}"

        payload = json.dumps({
            "market":          obs.market,
            "code":            obs.code,
            "side":            obs.side,
            "delta":           obs.fill_delta_qty,
            "avg_price":       obs.average_fill_price,
            "order_qty":       obs.order_qty,
            "unfilled_qty":    obs.unfilled_qty,
            "order_status":    obs.order_status,
            "exchange":        obs.exchange,
            "currency":        obs.currency,
        }, ensure_ascii=False)

        import uuid as _uuid
        cur = conn.execute(
            """INSERT OR IGNORE INTO events
               (event_uuid, ts, type, aggregate_type, aggregate_id,
                client_order_id, odno, code, side, qty, price,
                cum_filled_qty, realized_pnl,
                idempotency_key, payload, schema_ver)
               VALUES (?,?,?,?,?, ?,?,?,?,?,?, ?,?, ?,?,?)""",
            (
                _uuid.uuid4().hex,
                obs.observed_at,
                EXECUTION_OBSERVED_ONLY,   # ← 투영 비활성 타입
                "order",
                obs.client_order_id or obs.odno or "UNKNOWN",
                obs.client_order_id,
                obs.odno,
                obs.code,
                obs.side,
                obs.fill_delta_qty,        # qty = delta (이번 관측 증분)
                obs.average_fill_price,
                obs.cumulative_filled_qty, # cum_filled_qty = 누적
                None,                      # realized_pnl = None (포지션 미확정)
                idem_key,
                payload,
                1,
            ),
        )
        inserted = cur.rowcount  # INSERT OR IGNORE: rowcount=0 이면 이미 존재
        conn.commit()
        conn.close()
        if inserted:
            logger.info(
                f"[Phoenix] EXECUTION_OBSERVED_ONLY 기록: "
                f"{obs.market} {obs.code} {obs.side} "
                f"odno={obs.odno} cum={obs.cumulative_filled_qty} "
                f"delta={obs.fill_delta_qty} idem={idem_key!r}"
            )
            return True
        else:
            # idempotency_key UNIQUE 충돌 → 이미 기록됨 (멱등)
            logger.debug(
                f"[Phoenix] 중복 기록 무시 (idem={idem_key!r})"
            )
            return False
    except Exception as e:
        logger.error(f"[Phoenix] EXECUTION_OBSERVED_ONLY 기록 실패: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# Journal 부분/전량 체결 이벤트 기록
# ──────────────────────────────────────────────────────────────

# journal 이벤트 타입 상수 (trading_journal.EventType 확장)
class FillEventType:
    BUY_ORDER_PARTIALLY_FILLED  = "BUY_ORDER_PARTIALLY_FILLED"
    BUY_ORDER_FILLED            = "BUY_ORDER_FILLED"
    SELL_ORDER_PARTIALLY_FILLED = "SELL_ORDER_PARTIALLY_FILLED"
    SELL_ORDER_FILLED           = "SELL_ORDER_FILLED"
    # TRADE_CLOSED — 이번 단계에서 기록 금지


def _record_journal_fill(obs: ExecutionObservation) -> bool:
    """Journal trade_events 에 체결 이벤트 기록.

    - 부분체결: BUY_ORDER_PARTIALLY_FILLED / SELL_ORDER_PARTIALLY_FILLED
    - 전량체결: BUY_ORDER_FILLED / SELL_ORDER_FILLED
    - TRADE_CLOSED / daily_summary / state=CLOSED → 이번 단계 금지

    idempotency:
      trade_events에 동일 (trade_id, event_type, cum_filled_qty) 조합이
      이미 존재하면 기록하지 않는다.
    """
    if not obs.is_new_fill:
        return False

    trade_id = obs.client_order_id  # journal trade_id = client_order_id
    if not trade_id:
        logger.warning(
            f"[Journal] FillEvent 기록 스킵: client_order_id 없음 "
            f"odno={obs.odno} code={obs.code}"
        )
        return False

    side = obs.side or "BUY"
    if side == "BUY":
        ev_type = (FillEventType.BUY_ORDER_FILLED
                   if obs.is_fully_filled
                   else FillEventType.BUY_ORDER_PARTIALLY_FILLED)
    else:
        ev_type = (FillEventType.SELL_ORDER_FILLED
                   if obs.is_fully_filled
                   else FillEventType.SELL_ORDER_PARTIALLY_FILLED)

    try:
        conn = _get_conn()

        # ── 멱등: 동일 (trade_id, event_type, cum) 이미 존재하면 스킵 ──
        existing = conn.execute(
            """SELECT id FROM trade_events
               WHERE trade_id=? AND event_type=?
               AND JSON_EXTRACT(payload_json, '$.cum_filled_qty')=?""",
            (trade_id, ev_type, obs.cumulative_filled_qty),
        ).fetchone()
        if existing:
            logger.debug(
                f"[Journal] 중복 FillEvent 스킵: "
                f"trade_id={trade_id} ev={ev_type} cum={obs.cumulative_filled_qty}"
            )
            return False

        payload = json.dumps({
            "odno":              obs.odno,
            "cum_filled_qty":    obs.cumulative_filled_qty,
            "fill_delta_qty":    obs.fill_delta_qty,
            "avg_fill_price":    obs.average_fill_price,
            "order_qty":         obs.order_qty,
            "unfilled_qty":      obs.unfilled_qty,
            "order_status":      obs.order_status,
            "exchange":          obs.exchange,
            "currency":          obs.currency,
            "observed_at":       obs.observed_at,
        }, ensure_ascii=False)

        note = (
            f"cum={obs.cumulative_filled_qty}/"
            f"{'?' if obs.order_qty is None else obs.order_qty}"
            f"|delta={obs.fill_delta_qty}"
            f"|price={obs.average_fill_price}"
        )
        conn.execute(
            """INSERT INTO trade_events
               (trade_id, event_type, side, ts, market, code,
                price, qty, note, payload_json)
               VALUES (?,?,?,?,?,?, ?,?,?,?)""",
            (
                trade_id, ev_type, side,
                obs.observed_at,
                obs.market, obs.code,
                obs.average_fill_price,
                obs.fill_delta_qty,
                note,
                payload,
            ),
        )

        # trade_entries: fill_price / fill_qty / fill_time / fill_confirmed 갱신
        # (전량 체결 시만. 부분체결은 미반영 — 추가 체결 가능성)
        if obs.is_fully_filled and obs.average_fill_price:
            now = datetime.now().isoformat()
            conn.execute(
                """UPDATE trade_entries
                   SET fill_price=COALESCE(fill_price, ?),
                       fill_qty=COALESCE(fill_qty, ?),
                       fill_time=COALESCE(fill_time, ?),
                       fill_confirmed=1,
                       updated_at=?
                   WHERE trade_id=? AND fill_confirmed=0""",
                (
                    obs.average_fill_price,
                    obs.cumulative_filled_qty,
                    now,
                    now,
                    trade_id,
                ),
            )

        conn.commit()
        logger.info(
            f"[Journal] FillEvent 기록: {ev_type} "
            f"trade_id={trade_id} code={obs.code} "
            f"cum={obs.cumulative_filled_qty} delta={obs.fill_delta_qty} "
            f"price={obs.average_fill_price}"
        )
        return True
    except Exception as e:
        logger.error(f"[Journal] FillEvent 기록 실패 trade_id={trade_id}: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# FillObserver — 메인 관측 엔진
# ──────────────────────────────────────────────────────────────

class FillObserver:
    """체결 관측 엔진.

    poll_once() 를 외부에서 호출하여 1회 폴링을 수행한다.
    백그라운드 루프는 이번 단계에서 추가하지 않는다.
    """

    def __init__(
        self,
        kis_api=None,
        phoenix_db_path: Optional[str] = None,
    ):
        """
        kis_api: KISApi 인스턴스 (None이면 실제 API 호출 생략 — 테스트용)
        phoenix_db_path: None이면 기본 data/phoenix.db
        """
        self._kis = kis_api
        self._phoenix_db = phoenix_db_path
        self.registry = PendingOrderRegistry()

    # ── 주문 등록 (strategy에서 접수 직후 호출) ────────────────
    def register_order(
        self,
        market: str,
        trade_id: str,
        code: str,
        side: str,
        order_qty: int,
        submitted_at: Optional[str] = None,
        odno: str = "",
        client_order_id: str = "",
        raw_order_response: Optional[dict] = None,
        exchange: Optional[str] = None,
        currency: str = "KRW",
    ) -> int:
        """pending_orders에 주문 등록. 반환: 삽입된 행 id."""
        ts = submitted_at or datetime.now().isoformat()
        return self.registry.register(
            market=market,
            trade_id=trade_id,
            code=code,
            side=side,
            order_qty=order_qty,
            submitted_at=ts,
            odno=odno,
            client_order_id=client_order_id,
            raw_order_response=raw_order_response,
            exchange=exchange,
            currency=currency,
        )

    # ── 단일 주문 체결 조회 + 기록 ────────────────────────────
    def _poll_one(self, order: dict) -> dict:
        """단일 pending order 1회 체결 조회 + 이벤트 기록.

        반환: {
          "trade_id": str,
          "market": str,
          "code": str,
          "status_before": str,
          "status_after": str,
          "fill_delta": int,       # 이번 관측의 증분
          "cum_filled": int,
          "error": str | None,
        }
        """
        trade_id   = order["trade_id"]
        market     = order["market"]
        code       = order["code"]
        side       = order["side"]
        odno       = order.get("odno", "")
        coid       = order.get("client_order_id", "")
        order_qty  = int(order.get("order_qty", 0) or 0)
        prev_cum   = int(order.get("cumulative_filled_qty", 0) or 0)
        exchange   = order.get("exchange", "NASD")
        status_before = order["status"]
        result = {
            "trade_id":      trade_id,
            "market":        market,
            "code":          code,
            "side":          side,
            "order_qty":     order_qty,
            "status_before": status_before,
            "status_after":  status_before,   # 기본: 변경 없음
            "fill_delta":    0,
            "cum_filled":    prev_cum,
            # ★ P0-5: 체결가를 결과에 포함한다.
            #   이전에는 details 에 없어서 호출자가 0.0 폴백을 썼고,
            #   그 결과 _handle_buy_filled 의 price<=0 가드에 걸려
            #   apply_buy 가 통째로 스킵됐다.
            "avg_fill_price": None,
            "error":         None,
        }

        # ── KIS 체결조회 호출 ──────────────────────────────────
        raw = {}
        try:
            if self._kis is None:
                # 테스트용: API 없이 등록만
                return result
            if market == "KR":
                raw = self._kis.get_kr_ccld_by_odno(odno=odno, code=code)
            else:
                raw = self._kis.get_us_ccld(
                    odno=odno, symbol=code, excd=exchange or "NASD"
                )
        except Exception as e:
            err_msg = f"KIS 체결조회 오류: {e}"
            logger.error(f"[FillObserver] {err_msg} trade_id={trade_id}")
            # ★ API 실패 시 상태 변경 금지, retry_count만 증가
            self.registry.increment_retry(trade_id)
            result["error"] = err_msg
            return result

        if not raw:
            # 조회 결과 없음 → 상태 유지
            self.registry.increment_retry(trade_id)
            result["error"] = "체결조회 응답 없음 (미체결 또는 API 오류)"
            return result

        # ── ExecutionObservation 정규화 ────────────────────────
        if market == "KR":
            obs = ExecutionNormalizer.from_kr(
                raw, prev_cum=prev_cum, client_order_id=coid or trade_id
            )
        else:
            obs = ExecutionNormalizer.from_us(
                raw, prev_cum=prev_cum, client_order_id=coid or trade_id
            )

        if obs is None:
            self.registry.increment_retry(trade_id)
            result["error"] = "Observation 정규화 실패"
            return result

        result["fill_delta"]     = obs.fill_delta_qty
        result["cum_filled"]     = obs.cumulative_filled_qty
        result["avg_fill_price"] = obs.average_fill_price
        if obs.order_qty:
            result["order_qty"] = obs.order_qty
        if obs.side:
            result["side"] = obs.side

        if not obs.is_new_fill:
            # 새 체결 없음 → 상태 유지
            logger.debug(
                f"[FillObserver] 신규 체결 없음: trade_id={trade_id} "
                f"cum={obs.cumulative_filled_qty} (prev={prev_cum})"
            )
            return result

        # ── Phoenix 관측 이벤트 기록 (포지션 비투영) ─────────────
        _record_phoenix_observation(obs, self._phoenix_db)

        # ── Journal 체결 이벤트 기록 ──────────────────────────
        _record_journal_fill(obs)

        # ── pending_orders 상태 업데이트 ──────────────────────
        if obs.is_fully_filled:
            new_status = PendingStatus.FILLED
        else:
            new_status = PendingStatus.PARTIALLY_FILLED

        self.registry.update_fill(trade_id, obs.cumulative_filled_qty, new_status)
        result["status_after"] = new_status

        logger.info(
            f"[FillObserver] 체결 관측: {market} {code} {side} "
            f"odno={odno} delta={obs.fill_delta_qty} "
            f"cum={obs.cumulative_filled_qty}/{order_qty} "
            f"→ {new_status}"
        )
        return result

    # ── 재시도 상한 초과 주문 만료 처리 ───────────────────────
    def _expire_retry_exhausted(self, market: Optional[str] = None) -> int:
        """재시도 상한을 넘긴 추적 주문을 EXPIRED 로 종결한다 (P0-5).

        상한이 없으면 취소·거부된 주문이 영구히 체결조회 API 를 소모하고
        로그를 오염시킨다. 여기서 종결시켜 폴링 대상에서 제외한다.
        """
        try:
            stale = self.registry.get_retry_exhausted(market=market)
        except Exception as e:
            logger.warning(f"[FillObserver] 만료 대상 조회 실패: {e}")
            return 0
        for row in stale:
            self.registry.set_status(row["trade_id"], PendingStatus.EXPIRED)
            logger.warning(
                f"[FillObserver] 체결조회 재시도 {row.get('retry_count')}회 초과 "
                f"→ EXPIRED 종결: market={row.get('market')} "
                f"code={row.get('code')} odno={row.get('odno')!r} "
                f"trade_id={row.get('trade_id')}"
            )
        return len(stale)

    # ── 1회 폴링 (전체 추적 대상) ─────────────────────────────
    def poll_once(self, market: Optional[str] = None) -> dict:
        """ACCEPTED / PARTIALLY_FILLED 주문 전체를 1회 체결조회.

        동작:
          0. 재시도 상한 초과 주문 → EXPIRED 로 종결 (폴링 대상에서 제외)
          1. ACCEPTED / PARTIALLY_FILLED 주문 목록 조회
          2. 각 주문 KIS 체결조회
          3. fill_delta 계산
          4. Phoenix EXECUTION_OBSERVED_ONLY 이벤트 기록
          5. Journal BUY/SELL_ORDER_PARTIALLY/FILLED 이벤트 기록
          6. pending_orders 상태 업데이트
          7. 결과 요약 반환

        market: "KR" | "US" | None(전체). ★ 지정하지 않으면 KR 폴러가 US 주문을
                조회하는 교차 오염이 발생하므로 운영 경로는 반드시 지정한다.

        반환: {
          "total":    int,   # 조회한 주문 수
          "filled":   int,   # 전량 체결 확인 수
          "partial":  int,   # 부분 체결 확인 수
          "no_change":int,   # 변화 없음
          "errors":   int,   # 오류 발생 수
          "expired":  int,   # 재시도 상한 초과로 종결한 수
          "details":  list,  # 각 주문 결과 dict 목록
        }
        """
        expired = self._expire_retry_exhausted(market)
        orders = self.registry.get_trackable(market=market)
        summary = {
            "total":     len(orders),
            "filled":    0,
            "partial":   0,
            "no_change": 0,
            "errors":    0,
            "expired":   expired,
            "details":   [],
        }
        for order in orders:
            r = self._poll_one(order)
            summary["details"].append(r)
            if r["error"]:
                summary["errors"] += 1
            elif r["fill_delta"] > 0 and r["status_after"] == PendingStatus.FILLED:
                # 이번 폴링에서 전량 체결 확인
                summary["filled"] += 1
            elif r["fill_delta"] > 0:
                # 이번 폴링에서 신규 부분체결 확인
                summary["partial"] += 1
            else:
                # 신규 체결 없음 (delta=0 또는 API 오류 외)
                summary["no_change"] += 1

        logger.info(
            f"[FillObserver] poll_once 완료: "
            f"total={summary['total']} filled={summary['filled']} "
            f"partial={summary['partial']} no_change={summary['no_change']} "
            f"errors={summary['errors']}"
        )
        return summary


# ──────────────────────────────────────────────────────────────
# 모듈 레벨 싱글턴 + 편의 함수
# ──────────────────────────────────────────────────────────────

_default_observer: Optional[FillObserver] = None


def get_observer(kis_api=None, phoenix_db_path: Optional[str] = None) -> FillObserver:
    """기본 FillObserver 싱글턴 반환.

    kis_api와 phoenix_db_path가 지정되면 재생성한다.
    테스트 코드는 FillObserver(kis_api=mock) 로 직접 생성 권장.
    """
    global _default_observer
    if _default_observer is None or kis_api is not None:
        _default_observer = FillObserver(
            kis_api=kis_api,
            phoenix_db_path=phoenix_db_path,
        )
    return _default_observer


def poll_pending_orders_once(
    kis_api=None,
    phoenix_db_path: Optional[str] = None,
) -> dict:
    """ACCEPTED/PARTIALLY_FILLED 주문을 1회 체결조회하여 관측 이벤트 기록.

    이 함수 자체는 apply_buy/apply_sell을 절대 호출하지 않는다.
    포지션, DailyPnLGuard, 쿨다운, 복리풀 등에 영향을 주지 않는다.

    백그라운드 루프는 이번 단계에서 추가하지 않음.
    정기적 폴링이 필요하면 호출부(strategy_manager 루프 등)에서
    주기적으로 이 함수를 호출하면 된다.

    반환: poll_once() 요약 dict
    """
    obs = get_observer(kis_api=kis_api, phoenix_db_path=phoenix_db_path)
    return obs.poll_once()


# ── 모듈 import 시 pending_orders 테이블 초기화 ──────────────
PendingOrderRegistry()  # 테이블 없으면 생성
