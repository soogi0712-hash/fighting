"""Phoenix OrderLifecycleManager — 개별 주문 상태 전이 전용 컴포넌트.

역할:
  Strategy / Journal / Phoenix / PendingOrder 사이에서
  "개별 주문의 현재 상태"만 관리한다.

설계 원칙:
  - 1 OrderLifecycle = 1 개별 주문 (BUY 1건 or SELL 1건)
  - 동일 trade_id에 복수 OrderLifecycle 존재 가능
    (최초매수 + 추가매수 + 부분매도 + 최종매도 등)
  - PRIMARY KEY = order_lifecycle_id (주문 단위 식별자)
  - trade_id = 동일 포지션/거래 묶음 그룹키 (FOREIGN KEY 아님)
  - TRADE_CLOSED 는 OrderLifecycle 책임 범위 밖
    (향후 TradeLifecycle / PositionLifecycle 에서 관리)

절대 금지 사항 (이번 단계):
  - apply_buy() 호출 금지
  - apply_sell() 호출 금지
  - 포지션 수량 변경 금지
  - DailyPnLGuard 변경 금지
  - Cooldown 변경 금지
  - Phoenix Projector 변경 금지
  - AI 기능 추가 금지

기록 원칙:
  - EventStore에 lifecycle 전이 사실만 기록 (EXECUTION_OBSERVED_ONLY 사용)
    → Projector._DISPATCH 미등록 → positions 테이블 무영향
  - lifecycle_orders 테이블을 통해 상태를 영속화한다
  - 재시작 후 lifecycle_orders에서 상태를 복원할 수 있다

부분체결 후 취소/만료:
  - filled_qty > 0 이더라도 CANCELLED / EXPIRED 도달 가능
  - had_partial_fill, terminal_reason 필드로 이력 보존
  - 예: 100주 주문 → 30주 체결 → 70주 취소
       had_partial_fill=True, terminal_reason="PARTIAL_FILL_REMAINDER_CANCELLED"

UPSERT 방식:
  - INSERT ... ON CONFLICT(order_lifecycle_id) DO UPDATE SET ...
  - created_at은 최초 삽입값 유지, updated_at만 변경
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional

from utils.logger import get_logger

logger = get_logger("OrderLifecycleManager")

# Projector._DISPATCH 에 등록되지 않은 관측 전용 이벤트 타입
EXECUTION_OBSERVED_ONLY = "ExecutionObservedOnly"


# ══════════════════════════════════════════════════════════════════════
# LifecycleState — 개별 주문 생명주기 상태 (9개, TRADE_CLOSED 없음)
# ══════════════════════════════════════════════════════════════════════
class LifecycleState(Enum):
    """개별 주문 생명주기 9단계 상태.

    문자열 비교 금지 — 반드시 Enum 값으로 비교한다.

    단말 상태 (4개): FILLED / CANCELLED / REJECTED / EXPIRED
    TRADE_CLOSED 는 OrderLifecycle 책임 범위 밖이므로 제외.
    """
    UNKNOWN           = auto()   # 초기 상태 (생성 직후)
    SIGNAL_CONFIRMED  = auto()   # 전략이 신호를 확정
    ORDER_SUBMITTED   = auto()   # 주문을 KIS에 전송
    ORDER_ACCEPTED    = auto()   # KIS 접수 확인 (odno 확보)
    PARTIALLY_FILLED  = auto()   # 부분 체결
    FILLED            = auto()   # 전량 체결 (주문 단말)
    CANCELLED         = auto()   # 취소 완료 (주문 단말)
    REJECTED          = auto()   # 거부 (주문 단말)
    EXPIRED           = auto()   # 만료 — 당일 미체결 소멸 (주문 단말)


# ══════════════════════════════════════════════════════════════════════
# 허용 전이 테이블
# ══════════════════════════════════════════════════════════════════════
_ALLOWED_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.UNKNOWN: frozenset({
        LifecycleState.SIGNAL_CONFIRMED,
    }),
    LifecycleState.SIGNAL_CONFIRMED: frozenset({
        LifecycleState.ORDER_SUBMITTED,
        LifecycleState.CANCELLED,
    }),
    LifecycleState.ORDER_SUBMITTED: frozenset({
        LifecycleState.ORDER_ACCEPTED,
        LifecycleState.REJECTED,
        LifecycleState.CANCELLED,
    }),
    LifecycleState.ORDER_ACCEPTED: frozenset({
        LifecycleState.PARTIALLY_FILLED,
        LifecycleState.FILLED,
        LifecycleState.CANCELLED,
        LifecycleState.REJECTED,
        LifecycleState.EXPIRED,
    }),
    LifecycleState.PARTIALLY_FILLED: frozenset({
        LifecycleState.PARTIALLY_FILLED,   # 추가 부분체결
        LifecycleState.FILLED,
        LifecycleState.CANCELLED,
        LifecycleState.EXPIRED,
    }),
    LifecycleState.FILLED:    frozenset(),   # 주문 단말
    LifecycleState.CANCELLED: frozenset(),   # 주문 단말
    LifecycleState.REJECTED:  frozenset(),   # 주문 단말
    LifecycleState.EXPIRED:   frozenset(),   # 주문 단말
}

# 단말 상태 집합 (빠른 판별용)
_TERMINAL_STATES: frozenset[LifecycleState] = frozenset(
    s for s, targets in _ALLOWED_TRANSITIONS.items() if not targets
)

# order_index projection 을 갱신할 도메인 이벤트를 발행하는 상태 (P0-5).
# PARTIALLY_FILLED 는 제외 — 주문이 여전히 in-flight 이므로 order_index 를
# SUBMITTED 로 유지해야 OrderGate 의 ORDER_IN_FLIGHT 판정이 맞다.
_DOMAIN_EVENT_STATES: frozenset[LifecycleState] = frozenset({
    LifecycleState.ORDER_SUBMITTED,
    LifecycleState.ORDER_ACCEPTED,
    LifecycleState.FILLED,
    LifecycleState.CANCELLED,
    LifecycleState.REJECTED,
    LifecycleState.EXPIRED,
})


# ══════════════════════════════════════════════════════════════════════
# 예외
# ══════════════════════════════════════════════════════════════════════
class LifecycleTransitionError(Exception):
    """허용되지 않는 상태 전이 시도."""


class LifecycleNotFoundError(Exception):
    """lifecycle_orders에서 order_lifecycle_id를 찾을 수 없을 때."""


class DuplicateClientOrderIdError(Exception):
    """동일 client_order_id로 두 번 생성하려 할 때."""


# ══════════════════════════════════════════════════════════════════════
# TransitionValidator — 전이 유효성 검사
# ══════════════════════════════════════════════════════════════════════
class TransitionValidator:
    """상태 전이 유효성 검사기 (stateless — 공유 인스턴스 사용 가능)."""

    def is_valid_transition(
        self,
        from_state: LifecycleState,
        to_state: LifecycleState,
    ) -> bool:
        """전이가 허용되면 True, 아니면 False."""
        return to_state in _ALLOWED_TRANSITIONS.get(from_state, frozenset())

    def validate(
        self,
        from_state: LifecycleState,
        to_state: LifecycleState,
        order_lifecycle_id: Optional[str] = None,
    ) -> None:
        """전이가 허용되지 않으면 LifecycleTransitionError 발생.

        Args:
            from_state:          현재 상태
            to_state:            목표 상태
            order_lifecycle_id:  오류 메시지에 포함할 식별자 (선택)

        Raises:
            LifecycleTransitionError
        """
        if not self.is_valid_transition(from_state, to_state):
            ctx = f" (order_lifecycle_id={order_lifecycle_id})" if order_lifecycle_id else ""
            allowed = _ALLOWED_TRANSITIONS.get(from_state, frozenset())
            allowed_names = (
                ", ".join(s.name for s in sorted(allowed, key=lambda s: s.value))
                if allowed
                else "없음(단말 상태)"
            )
            raise LifecycleTransitionError(
                f"허용되지 않는 상태 전이{ctx}: "
                f"{from_state.name} → {to_state.name}. "
                f"현재 상태에서 허용된 전이: [{allowed_names}]"
            )


# 모듈 수준 공유 인스턴스 (stateless)
_validator = TransitionValidator()


# ══════════════════════════════════════════════════════════════════════
# order_lifecycle_id 생성 헬퍼
# ══════════════════════════════════════════════════════════════════════
def make_order_lifecycle_id(
    market: str,
    side: str,
    code: str,
) -> str:
    """order_lifecycle_id 생성.

    형식: {MARKET}_{SIDE}_{CODE}_{YYYYMMDDHHMMSSMMM}_{uuid8}
    예:   KR_BUY_005930_20260725143022123_a1b2c3d4
          US_SELL_AAPL_20260725150000001_e5f6g7h8
    """
    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]  # YYYYMMDDHHMMSSMMM
    uid = uuid.uuid4().hex[:8]
    mkt = (market or "XX").upper()
    sd  = (side or "XX").upper()
    cd  = (code or "UNKNOWN").upper()
    return f"{mkt}_{sd}_{cd}_{ts}_{uid}"


# ══════════════════════════════════════════════════════════════════════
# OrderLifecycle — 단일 주문 생명주기 데이터 클래스
# ══════════════════════════════════════════════════════════════════════
@dataclass
class OrderLifecycle:
    """단일 개별 주문(BUY 1건 or SELL 1건)의 생명주기 상태.

    1 trade_id → N OrderLifecycle 허용.
    TRADE_CLOSED 전이는 이 클래스의 책임 범위 밖.
    """

    # ── 주문 식별자 (PRIMARY KEY) ────────────────────────────────
    order_lifecycle_id: str              # 주문 단위 고유 ID

    # ── 거래 그룹 키 ─────────────────────────────────────────────
    trade_id:           str              # 동일 포지션/거래 묶음 (N:1)

    # ── 기본 정보 ────────────────────────────────────────────────
    market:             str              # "KR" | "US"
    code:               str              # 종목코드 / ticker
    side:               str              # "BUY" | "SELL" (필수)
    strategy_name:      Optional[str]    # 전략명

    # ── 브로커 주문 ID ───────────────────────────────────────────
    client_order_id:    Optional[str] = None   # Phoenix 클라이언트 주문ID (UNIQUE)
    odno:               Optional[str] = None   # KIS 주문번호

    # ── 상태 ────────────────────────────────────────────────────
    current_state:      LifecycleState = field(default=LifecycleState.UNKNOWN)

    # ── 수량 ────────────────────────────────────────────────────
    order_qty:          Optional[int]  = None
    filled_qty:         int            = 0
    remaining_qty:      Optional[int]  = None

    # ── 가격 ────────────────────────────────────────────────────
    avg_fill_price:     Optional[float] = None

    # ── 타임스탬프 ───────────────────────────────────────────────
    submitted_at:       Optional[str]  = None
    accepted_at:        Optional[str]  = None
    first_fill_at:      Optional[str]  = None
    last_fill_at:       Optional[str]  = None
    closed_at:          Optional[str]  = None   # 단말 상태 도달 시각

    # ── 부분체결 후 단말 이력 보존 ──────────────────────────────
    had_partial_fill:   bool           = False
    terminal_reason:    Optional[str]  = None   # 단말 상태 도달 사유

    # ── 오류 추적 ───────────────────────────────────────────────
    retry_count:        int            = 0
    last_error:         Optional[str]  = None

    # ── 내부: 검증기 ────────────────────────────────────────────
    _validator: TransitionValidator = field(
        default_factory=TransitionValidator, repr=False, compare=False
    )

    # ── 전이 헬퍼 ────────────────────────────────────────────────
    def _transition(self, to_state: LifecycleState) -> None:
        self._validator.validate(
            self.current_state, to_state, self.order_lifecycle_id
        )
        self.current_state = to_state

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    # ── 공개 전이 메서드 ─────────────────────────────────────────
    def confirm_signal(self) -> None:
        """UNKNOWN → SIGNAL_CONFIRMED."""
        self._transition(LifecycleState.SIGNAL_CONFIRMED)

    def submit(self, client_order_id: Optional[str] = None) -> None:
        """SIGNAL_CONFIRMED → ORDER_SUBMITTED."""
        self._transition(LifecycleState.ORDER_SUBMITTED)
        if client_order_id:
            self.client_order_id = client_order_id
        self.submitted_at = self._now()

    def accept(self, odno: Optional[str] = None) -> None:
        """ORDER_SUBMITTED → ORDER_ACCEPTED.

        이미 ORDER_ACCEPTED이면 멱등 처리 (예외 없음, odno 변경 안 함).
        """
        if self.current_state == LifecycleState.ORDER_ACCEPTED:
            return   # 멱등
        self._transition(LifecycleState.ORDER_ACCEPTED)
        if odno:
            self.odno = odno
        self.accepted_at = self._now()

    def partial_fill(
        self,
        delta: int,
        avg_price: Optional[float] = None,
    ) -> None:
        """→ PARTIALLY_FILLED: 부분 체결.

        Args:
            delta:     이번에 새로 체결된 수량 (양수여야 함)
            avg_price: 갱신된 평균 체결가 (선택)

        Raises:
            ValueError:              delta ≤ 0
            LifecycleTransitionError: 허용되지 않는 전이
        """
        if delta <= 0:
            raise ValueError(
                f"partial_fill delta는 양수여야 합니다: delta={delta}"
            )
        self._transition(LifecycleState.PARTIALLY_FILLED)
        self._apply_fill(delta, avg_price)
        self.had_partial_fill = True

    def full_fill(
        self,
        delta: int = 0,
        avg_price: Optional[float] = None,
    ) -> None:
        """→ FILLED: 전량 체결.

        이미 FILLED이면 멱등 처리 (예외 없음, filled_qty 추가 없음).

        Args:
            delta:     마지막으로 체결된 추가 수량 (0 허용)
            avg_price: 최종 평균 체결가 (선택)
        """
        if self.current_state == LifecycleState.FILLED:
            return   # 멱등
        self._transition(LifecycleState.FILLED)
        if delta > 0:
            self._apply_fill(delta, avg_price)
        elif avg_price is not None:
            self.avg_fill_price = avg_price
        self.closed_at = self._now()

    def cancel(self, reason: Optional[str] = None) -> None:
        """→ CANCELLED: 주문 취소.

        부분체결 상태에서 취소 시 had_partial_fill=True, terminal_reason 설정.
        """
        was_partial = (
            self.current_state == LifecycleState.PARTIALLY_FILLED
            or self.had_partial_fill
        )
        self._transition(LifecycleState.CANCELLED)
        if was_partial:
            self.had_partial_fill = True
            self.terminal_reason = reason or "PARTIAL_FILL_REMAINDER_CANCELLED"
        else:
            if reason:
                self.terminal_reason = reason
        self.closed_at = self._now()

    def reject(self, reason: Optional[str] = None) -> None:
        """→ REJECTED: 주문 거부."""
        self._transition(LifecycleState.REJECTED)
        if reason:
            self.last_error = reason
            self.terminal_reason = reason
        self.closed_at = self._now()

    def expire(self, reason: Optional[str] = None) -> None:
        """→ EXPIRED: 당일 미체결 소멸.

        부분체결 상태에서 만료 시 had_partial_fill=True, terminal_reason 설정.
        """
        was_partial = (
            self.current_state == LifecycleState.PARTIALLY_FILLED
            or self.had_partial_fill
        )
        self._transition(LifecycleState.EXPIRED)
        if was_partial:
            self.had_partial_fill = True
            self.terminal_reason = reason or "PARTIAL_FILL_REMAINDER_EXPIRED"
        else:
            if reason:
                self.terminal_reason = reason
        self.closed_at = self._now()

    # ── 내부 체결 처리 ───────────────────────────────────────────
    def _apply_fill(self, delta: int, avg_price: Optional[float]) -> None:
        self.filled_qty += delta
        if self.order_qty is not None:
            self.remaining_qty = max(0, self.order_qty - self.filled_qty)
        if avg_price is not None:
            self.avg_fill_price = avg_price
        now = self._now()
        if self.first_fill_at is None:
            self.first_fill_at = now
        self.last_fill_at = now

    # ── 상태 판별 ────────────────────────────────────────────────
    @property
    def is_terminal(self) -> bool:
        """더 이상 전이가 불가능한 단말 상태이면 True."""
        return self.current_state in _TERMINAL_STATES

    # ── 직렬화 / 역직렬화 ────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "order_lifecycle_id": self.order_lifecycle_id,
            "trade_id":           self.trade_id,
            "market":             self.market,
            "code":               self.code,
            "side":               self.side,
            "strategy_name":      self.strategy_name,
            "client_order_id":    self.client_order_id,
            "odno":               self.odno,
            "current_state":      self.current_state.name,
            "order_qty":          self.order_qty,
            "filled_qty":         self.filled_qty,
            "remaining_qty":      self.remaining_qty,
            "avg_fill_price":     self.avg_fill_price,
            "submitted_at":       self.submitted_at,
            "accepted_at":        self.accepted_at,
            "first_fill_at":      self.first_fill_at,
            "last_fill_at":       self.last_fill_at,
            "closed_at":          self.closed_at,
            "had_partial_fill":   int(self.had_partial_fill),
            "terminal_reason":    self.terminal_reason,
            "retry_count":        self.retry_count,
            "last_error":         self.last_error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OrderLifecycle":
        state_name = d.get("current_state", "UNKNOWN")
        try:
            state = LifecycleState[state_name]
        except KeyError:
            logger.warning(
                "알 수 없는 lifecycle state '%s', UNKNOWN으로 복원", state_name
            )
            state = LifecycleState.UNKNOWN

        return cls(
            order_lifecycle_id= d["order_lifecycle_id"],
            trade_id=           d["trade_id"],
            market=             d["market"],
            code=               d["code"],
            side=               d.get("side", ""),
            strategy_name=      d.get("strategy_name"),
            client_order_id=    d.get("client_order_id"),
            odno=               d.get("odno"),
            current_state=      state,
            order_qty=          d.get("order_qty"),
            filled_qty=         int(d.get("filled_qty") or 0),
            remaining_qty=      d.get("remaining_qty"),
            avg_fill_price=     d.get("avg_fill_price"),
            submitted_at=       d.get("submitted_at"),
            accepted_at=        d.get("accepted_at"),
            first_fill_at=      d.get("first_fill_at"),
            last_fill_at=       d.get("last_fill_at"),
            closed_at=          d.get("closed_at"),
            had_partial_fill=   bool(d.get("had_partial_fill", 0)),
            terminal_reason=    d.get("terminal_reason"),
            retry_count=        int(d.get("retry_count") or 0),
            last_error=         d.get("last_error"),
        )


# ══════════════════════════════════════════════════════════════════════
# lifecycle_orders 테이블 DDL
# ══════════════════════════════════════════════════════════════════════
_LIFECYCLE_DDL = """
CREATE TABLE IF NOT EXISTS lifecycle_orders (
    order_lifecycle_id  TEXT    NOT NULL PRIMARY KEY,
    trade_id            TEXT    NOT NULL,
    market              TEXT    NOT NULL,
    code                TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    strategy_name       TEXT,
    client_order_id     TEXT    UNIQUE,
    odno                TEXT,
    current_state       TEXT    NOT NULL DEFAULT 'UNKNOWN',
    order_qty           INTEGER,
    filled_qty          INTEGER NOT NULL DEFAULT 0,
    remaining_qty       INTEGER,
    avg_fill_price      REAL,
    submitted_at        TEXT,
    accepted_at         TEXT,
    first_fill_at       TEXT,
    last_fill_at        TEXT,
    closed_at           TEXT,
    had_partial_fill    INTEGER NOT NULL DEFAULT 0,
    terminal_reason     TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_lco_trade_id  ON lifecycle_orders(trade_id);
CREATE INDEX IF NOT EXISTS idx_lco_odno      ON lifecycle_orders(odno);
CREATE INDEX IF NOT EXISTS idx_lco_market_odno
    ON lifecycle_orders(market, odno)
    WHERE odno IS NOT NULL AND odno != '';
CREATE INDEX IF NOT EXISTS idx_lco_state     ON lifecycle_orders(current_state);
CREATE INDEX IF NOT EXISTS idx_lco_code      ON lifecycle_orders(code);
"""

# 마이그레이션: 기존 trade_id PK 테이블에서 신규 스키마로 이행
# 실행 조건: lifecycle_orders 가 이미 존재하고 order_lifecycle_id 열이 없을 때
_MIGRATION_SQL = """
-- 기존 테이블이 trade_id PK 방식일 때만 마이그레이션 수행
-- (order_lifecycle_id 열 부재 여부를 pragma로 확인 후 호출)
ALTER TABLE lifecycle_orders RENAME TO lifecycle_orders_v1_backup;

CREATE TABLE lifecycle_orders (
    order_lifecycle_id  TEXT    NOT NULL PRIMARY KEY,
    trade_id            TEXT    NOT NULL,
    market              TEXT    NOT NULL,
    code                TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    strategy_name       TEXT,
    client_order_id     TEXT    UNIQUE,
    odno                TEXT,
    current_state       TEXT    NOT NULL DEFAULT 'UNKNOWN',
    order_qty           INTEGER,
    filled_qty          INTEGER NOT NULL DEFAULT 0,
    remaining_qty       INTEGER,
    avg_fill_price      REAL,
    submitted_at        TEXT,
    accepted_at         TEXT,
    first_fill_at       TEXT,
    last_fill_at        TEXT,
    closed_at           TEXT,
    had_partial_fill    INTEGER NOT NULL DEFAULT 0,
    terminal_reason     TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_lco_trade_id   ON lifecycle_orders(trade_id);
CREATE INDEX IF NOT EXISTS idx_lco_odno       ON lifecycle_orders(odno);
CREATE INDEX IF NOT EXISTS idx_lco_market_odno
    ON lifecycle_orders(market, odno)
    WHERE odno IS NOT NULL AND odno != '';
CREATE INDEX IF NOT EXISTS idx_lco_state      ON lifecycle_orders(current_state);
CREATE INDEX IF NOT EXISTS idx_lco_code       ON lifecycle_orders(code);

-- 기존 데이터 이행 (trade_id를 order_lifecycle_id로 재활용)
INSERT INTO lifecycle_orders (
    order_lifecycle_id, trade_id, market, code, side,
    strategy_name, client_order_id, odno, current_state,
    order_qty, filled_qty, remaining_qty, avg_fill_price,
    submitted_at, accepted_at, first_fill_at, last_fill_at, closed_at,
    had_partial_fill, terminal_reason, retry_count, last_error,
    created_at, updated_at
)
SELECT
    trade_id AS order_lifecycle_id,
    trade_id,
    market, code,
    COALESCE(side, 'UNKNOWN') AS side,
    strategy_name, client_order_id, odno, current_state,
    order_qty, filled_qty, remaining_qty, avg_fill_price,
    submitted_at, accepted_at, first_fill_at, last_fill_at, closed_at,
    COALESCE(had_partial_fill, 0),
    terminal_reason, retry_count, last_error,
    created_at, updated_at
FROM lifecycle_orders_v1_backup;
"""


# ══════════════════════════════════════════════════════════════════════
# OrderLifecycleManager — 생명주기 총괄 관리자
# ══════════════════════════════════════════════════════════════════════
class OrderLifecycleManager:
    """개별 주문 생명주기 총괄 관리자.

    1 trade_id → N OrderLifecycle (복수 주문 지원).

    역할:
      1. OrderLifecycle 객체 생성 / 조회 / 저장
      2. 상태 전이를 EventStore에 기록 (EXECUTION_OBSERVED_ONLY)
      3. lifecycle_orders 테이블을 통한 영속화 및 재시작 복원

    ExecutionObservation 연결 순서:
      1. market + odno → lifecycle_orders 조회
      2. client_order_id → lifecycle_orders 조회
      3. 찾지 못하면 오류 로그 (임의 생성 금지)

    절대 금지:
      apply_buy() / apply_sell() / positions 테이블 수정 / DailyPnLGuard
    """

    def __init__(
        self,
        journal_db_path: str,
        event_store=None,
        phoenix_db_path: Optional[str] = None,
    ):
        self._journal_db_path = journal_db_path
        self._event_store = event_store
        self._phoenix_db_path = phoenix_db_path
        self._local = threading.local()
        self._validator = TransitionValidator()
        self._ensure_schema()

    # ── DB 연결 ──────────────────────────────────────────────────
    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "lc_conn") or self._local.lc_conn is None:
            import os
            os.makedirs(
                os.path.dirname(os.path.abspath(self._journal_db_path)),
                exist_ok=True,
            )
            conn = sqlite3.connect(
                self._journal_db_path, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.lc_conn = conn
        return self._local.lc_conn

    def _ensure_schema(self) -> None:
        """lifecycle_orders 테이블 생성 및 스키마 마이그레이션."""
        conn = self._get_conn()

        # 테이블 존재 여부 확인
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='lifecycle_orders'"
        ).fetchone()

        if row is not None:
            # 테이블이 있으면 order_lifecycle_id 열 존재 여부 확인
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(lifecycle_orders)"
                ).fetchall()
            }
            if "order_lifecycle_id" not in cols:
                # 구 스키마(trade_id PK) → 마이그레이션 수행
                logger.info(
                    "lifecycle_orders 구 스키마 감지 → 마이그레이션 실행"
                )
                for stmt in _MIGRATION_SQL.strip().split(";"):
                    stmt = stmt.strip()
                    if stmt and not stmt.startswith("--"):
                        conn.execute(stmt)
                conn.commit()
                logger.info("lifecycle_orders 마이그레이션 완료")
            # 이미 신 스키마 → 아무것도 하지 않음
        else:
            # 테이블 없음 → 신규 생성
            for stmt in _LIFECYCLE_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
            conn.commit()

    # ── CRUD ─────────────────────────────────────────────────────
    def create(
        self,
        trade_id: str,
        market: str,
        code: str,
        side: str,                          # "BUY" | "SELL" 필수
        strategy_name: Optional[str] = None,
        client_order_id: Optional[str] = None,
        order_qty: Optional[int] = None,
        order_lifecycle_id: Optional[str] = None,
    ) -> OrderLifecycle:
        """새로운 OrderLifecycle을 생성하고 DB에 저장한다.

        Args:
            trade_id:           포지션/거래 묶음 그룹 키
            market:             "KR" | "US"
            code:               종목코드 / ticker
            side:               "BUY" | "SELL"
            strategy_name:      전략명 (선택)
            client_order_id:    Phoenix 클라이언트 주문 ID (선택, UNIQUE)
            order_qty:          주문 수량 (선택)
            order_lifecycle_id: 명시 지정 (미지정 시 auto-generate)

        Raises:
            DuplicateClientOrderIdError: client_order_id가 이미 존재하는 경우
        """
        if not side or side.upper() not in ("BUY", "SELL"):
            raise ValueError(
                f"side는 'BUY' 또는 'SELL' 이어야 합니다: side={side!r}"
            )

        # client_order_id 중복 검사
        if client_order_id:
            existing = self.find_by_client_order_id(client_order_id)
            if existing is not None:
                raise DuplicateClientOrderIdError(
                    f"client_order_id 중복: {client_order_id} "
                    f"(기존 order_lifecycle_id={existing.order_lifecycle_id})"
                )

        oid = order_lifecycle_id or make_order_lifecycle_id(market, side, code)
        lc = OrderLifecycle(
            order_lifecycle_id= oid,
            trade_id=           trade_id,
            market=             market,
            code=               code,
            side=               side.upper(),
            strategy_name=      strategy_name,
            client_order_id=    client_order_id,
            order_qty=          order_qty,
        )
        self._insert(lc)
        return lc

    def open_and_submit(
        self,
        *,
        trade_id: str,
        market: str,
        code: str,
        side: str,
        order_qty: Optional[int] = None,
        strategy_name: Optional[str] = None,
        client_order_id: Optional[str] = None,
        order_lifecycle_id: Optional[str] = None,
    ) -> OrderLifecycle:
        """create → confirm_signal → submit 을 한 번에 수행한다.

        ★ 반드시 브로커 주문 전송(api.buy/sell) **직전**에 호출한다.

        이유(P0-5): 주문 전송 직후 프로세스가 죽으면 브로커에는 주문이 접수되어
        있는데 우리 쪽에는 아무 기록이 없는 상태가 된다. 전송 전에
        ORDER_SUBMITTED 까지 영속화해 두면, 재시작 시 load_all_active() 가
        해당 주문을 되살려 체결 대사를 이어갈 수 있다.

        반환된 lc 는 ORDER_SUBMITTED 상태이며, 브로커 응답을 받은 뒤
        accept(lc, odno=...) 또는 reject(lc, reason=...) 로 이어가야 한다.
        """
        lc = self.create(
            trade_id           = trade_id,
            market             = market,
            code               = code,
            side               = side,
            strategy_name      = strategy_name,
            client_order_id    = client_order_id,
            order_qty          = order_qty,
            order_lifecycle_id = order_lifecycle_id,
        )
        self.confirm_signal(lc)
        self.submit(lc, client_order_id=client_order_id)
        return lc

    def load(self, order_lifecycle_id: str) -> Optional[OrderLifecycle]:
        """order_lifecycle_id로 OrderLifecycle을 로드한다."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM lifecycle_orders WHERE order_lifecycle_id=?",
            (order_lifecycle_id,),
        ).fetchone()
        return OrderLifecycle.from_dict(dict(row)) if row else None

    def load_by_trade_id(self, trade_id: str) -> list[OrderLifecycle]:
        """동일 trade_id에 속한 모든 OrderLifecycle을 로드한다."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM lifecycle_orders WHERE trade_id=? "
            "ORDER BY created_at ASC",
            (trade_id,),
        ).fetchall()
        return [OrderLifecycle.from_dict(dict(r)) for r in rows]

    def find_by_odno(
        self, market: str, odno: str
    ) -> Optional[OrderLifecycle]:
        """market + odno 로 OrderLifecycle을 조회한다.

        ExecutionObservation 연결 1순위.
        """
        if not odno:
            return None
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM lifecycle_orders WHERE market=? AND odno=?",
            (market, odno),
        ).fetchone()
        return OrderLifecycle.from_dict(dict(row)) if row else None

    def find_by_client_order_id(
        self, client_order_id: str
    ) -> Optional[OrderLifecycle]:
        """client_order_id 로 OrderLifecycle을 조회한다.

        ExecutionObservation 연결 2순위.
        """
        if not client_order_id:
            return None
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM lifecycle_orders WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        return OrderLifecycle.from_dict(dict(row)) if row else None

    def find_for_observation(
        self,
        market: str,
        odno: Optional[str],
        client_order_id: Optional[str],
    ) -> Optional[OrderLifecycle]:
        """ExecutionObservation 연결용 조회.

        순서: (1) market+odno → (2) client_order_id → None
        찾지 못하면 임의 생성하지 않고 None 반환.
        """
        if odno:
            lc = self.find_by_odno(market, odno)
            if lc:
                return lc
        if client_order_id:
            lc = self.find_by_client_order_id(client_order_id)
            if lc:
                return lc
        logger.warning(
            "ExecutionObservation 연결 실패: market=%s odno=%s coid=%s",
            market, odno, client_order_id,
        )
        return None

    def load_all_active(self) -> list[OrderLifecycle]:
        """단말 상태가 아닌 모든 OrderLifecycle을 로드한다 (재시작 복원용)."""
        terminal_names = [s.name for s in _TERMINAL_STATES]
        placeholders = ",".join("?" * len(terminal_names))
        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT * FROM lifecycle_orders "
            f"WHERE current_state NOT IN ({placeholders}) "
            f"ORDER BY created_at ASC",
            terminal_names,
        ).fetchall()
        return [OrderLifecycle.from_dict(dict(r)) for r in rows]

    def save(self, lc: OrderLifecycle) -> None:
        """OrderLifecycle 변경 사항을 DB에 저장한다."""
        self._upsert(lc)

    # ── 전이 래퍼 (DB 저장 + EventStore 기록) ────────────────────
    def confirm_signal(self, lc: OrderLifecycle) -> None:
        lc.confirm_signal()
        self._persist_and_record(lc, f"lifecycle:signal:{lc.order_lifecycle_id}")

    def submit(
        self, lc: OrderLifecycle, client_order_id: Optional[str] = None
    ) -> None:
        lc.submit(client_order_id)
        self._persist_and_record(lc, f"lifecycle:submit:{lc.order_lifecycle_id}")

    def accept(
        self, lc: OrderLifecycle, odno: Optional[str] = None
    ) -> None:
        lc.accept(odno)
        self._persist_and_record(lc, f"lifecycle:accept:{lc.order_lifecycle_id}")

    def partial_fill(
        self,
        lc: OrderLifecycle,
        delta: int,
        avg_price: Optional[float] = None,
    ) -> None:
        lc.partial_fill(delta, avg_price)
        self._persist_and_record(
            lc,
            f"lifecycle:partial:{lc.order_lifecycle_id}:{lc.filled_qty}",
        )

    def full_fill(
        self,
        lc: OrderLifecycle,
        delta: int = 0,
        avg_price: Optional[float] = None,
        on_filled=None,
    ) -> None:
        """→ FILLED 전이 후 on_filled 콜백을 정확히 1회 실행.

        Idempotency:
          - lc.full_fill() 내부에서 이미 FILLED 이면 상태 전이 없이 조기 반환.
          - 따라서 on_filled 콜백도 실행되지 않는다.
          - 동일 order_lifecycle_id 로 두 번 호출해도 apply_buy/apply_sell 1회만.

        Args:
            lc:        OrderLifecycle 인스턴스 (전이 대상)
            delta:     이번에 체결된 추가 수량 (0 허용)
            avg_price: 최종 평균 체결가 (선택)
            on_filled: Callable(lc) — FILLED 전이 성공 시에만 호출.
                       ExecutionDrivenPositionUpdater 인스턴스를 전달하면
                       apply_buy / apply_sell 이 정확히 1회 실행된다.
        """
        already_filled = lc.current_state == LifecycleState.FILLED
        lc.full_fill(delta, avg_price)
        self._persist_and_record(
            lc, f"lifecycle:filled:{lc.order_lifecycle_id}"
        )
        # 이미 FILLED 였으면 콜백 실행 안 함 (멱등 보장)
        if already_filled:
            return
        if on_filled is not None:
            try:
                on_filled(lc)
            except Exception as exc:
                logger.error(
                    "full_fill on_filled 콜백 오류: "
                    "order_lifecycle_id=%s error=%s",
                    lc.order_lifecycle_id, exc,
                )
                raise

    def cancel(
        self, lc: OrderLifecycle, reason: Optional[str] = None
    ) -> None:
        lc.cancel(reason)
        self._persist_and_record(
            lc, f"lifecycle:cancel:{lc.order_lifecycle_id}", reason=reason
        )

    def reject(
        self, lc: OrderLifecycle, reason: Optional[str] = None
    ) -> None:
        lc.reject(reason)
        self._persist_and_record(
            lc, f"lifecycle:reject:{lc.order_lifecycle_id}", reason=reason
        )

    def expire(
        self, lc: OrderLifecycle, reason: Optional[str] = None
    ) -> None:
        lc.expire(reason)
        self._persist_and_record(
            lc, f"lifecycle:expire:{lc.order_lifecycle_id}", reason=reason
        )

    # ── 내부: 저장 + 기록 ─────────────────────────────────────────
    def _persist_and_record(
        self, lc: OrderLifecycle, idem_key: str, *, reason: Optional[str] = None
    ) -> None:
        self._upsert(lc)
        self._record_event(lc, idem_key)
        self._record_domain_event(lc, reason)

    def _record_domain_event(
        self, lc: OrderLifecycle, reason: Optional[str] = None
    ) -> None:
        """order_index projection 을 갱신하는 도메인 이벤트를 append 한다 (P0-5).

        이것이 있어야 OrderGate 의 ORDER_IN_FLIGHT 판정이 실제로 동작한다.
        (이전에는 order_index 가 비어 있어 해당 검사가 항상 통과했다.)

        ★ EXECUTION_OBSERVED 는 절대 append 하지 않는다.
          positions projection 은 PositionReconciled 로만 갱신되는 브로커 미러이며,
          실제 포지션의 진실은 pyramid_positions.json / us_positions.json 이다.
          여기서 체결을 투영하면 포지션 소스가 이중화되어 충돌한다.
        """
        if self._event_store is None:
            return
        state = lc.current_state
        if state not in _DOMAIN_EVENT_STATES:
            return
        try:
            from phoenix import models as _m   # 순환 import 방지
            coid = lc.client_order_id or lc.order_lifecycle_id

            if state is LifecycleState.ORDER_SUBMITTED:
                ev = _m.intent_event(coid, lc.code, lc.side,
                                     lc.order_qty or 0,
                                     kind=(lc.side or "UNKNOWN"))
            elif state is LifecycleState.ORDER_ACCEPTED:
                if not lc.odno:
                    # odno 없는 접수는 order_index 에 기록할 durable 키가 없다.
                    # INTENT 상태로 남겨 두면 in-flight 로 계속 잡히므로 그대로 둔다.
                    return
                ev = _m.ack_event(coid, lc.odno, lc.code, lc.side,
                                  lc.order_qty or 0)
            elif state is LifecycleState.FILLED:
                ev = _m.closed_event(coid, lc.code, odno=lc.odno,
                                     reason="FILLED")
            elif state is LifecycleState.EXPIRED:
                ev = _m.closed_event(coid, lc.code, odno=lc.odno,
                                     reason=reason or "EXPIRED")
            elif state is LifecycleState.CANCELLED:
                ev = _m.canceled_event(coid, lc.code, odno=lc.odno,
                                       reason=reason)
            elif state is LifecycleState.REJECTED:
                ev = _m.rejected_event(coid, lc.code, odno=lc.odno,
                                       reason=reason)
            else:
                return

            self._event_store.apply(ev, allow_during_halt=True)
        except Exception as exc:
            # 도메인 이벤트 기록 실패가 상태 전이를 롤백하지는 않는다.
            logger.warning("lifecycle domain event 기록 실패 (무시): %s", exc)

    def _record_event(self, lc: OrderLifecycle, idem_key: str) -> None:
        """Phoenix EventStore에 EXECUTION_OBSERVED_ONLY 이벤트를 기록한다.

        EXECUTION_OBSERVED_ONLY 는 Projector._DISPATCH 에 미등록 →
        positions 테이블에 절대 영향 없음.
        """
        if self._event_store is None:
            return
        try:
            from phoenix.models import Event  # 순환 import 방지
            ev = Event(
                type=EXECUTION_OBSERVED_ONLY,
                aggregate_type="lifecycle",
                aggregate_id=lc.order_lifecycle_id,
                client_order_id=lc.client_order_id,
                odno=lc.odno,
                code=lc.code,
                side=lc.side,
                qty=lc.order_qty,
                cum_filled_qty=lc.filled_qty,
                idempotency_key=idem_key,
                payload=json.dumps({
                    "state":              lc.current_state.name,
                    "order_lifecycle_id": lc.order_lifecycle_id,
                    "trade_id":           lc.trade_id,
                    "filled_qty":         lc.filled_qty,
                    "avg_fill_price":     lc.avg_fill_price,
                    "had_partial_fill":   lc.had_partial_fill,
                    "terminal_reason":    lc.terminal_reason,
                }),
            )
            result = self._event_store.apply(ev, allow_during_halt=True)
            logger.debug(
                "lifecycle event 기록: %s order_lifecycle_id=%s status=%s",
                lc.current_state.name, lc.order_lifecycle_id, result.status,
            )
        except Exception as exc:
            # EventStore 기록 실패는 상태 전이를 롤백하지 않음
            logger.warning("lifecycle event 기록 실패 (무시): %s", exc)

    # ── 내부: DB CRUD ─────────────────────────────────────────────
    def _insert(self, lc: OrderLifecycle) -> None:
        conn = self._get_conn()
        d = lc.to_dict()
        conn.execute(
            """
            INSERT INTO lifecycle_orders (
                order_lifecycle_id, trade_id, market, code, side,
                strategy_name, client_order_id, odno, current_state,
                order_qty, filled_qty, remaining_qty, avg_fill_price,
                submitted_at, accepted_at, first_fill_at, last_fill_at,
                closed_at, had_partial_fill, terminal_reason,
                retry_count, last_error
            ) VALUES (
                :order_lifecycle_id, :trade_id, :market, :code, :side,
                :strategy_name, :client_order_id, :odno, :current_state,
                :order_qty, :filled_qty, :remaining_qty, :avg_fill_price,
                :submitted_at, :accepted_at, :first_fill_at, :last_fill_at,
                :closed_at, :had_partial_fill, :terminal_reason,
                :retry_count, :last_error
            )
            """,
            d,
        )
        conn.commit()

    def _upsert(self, lc: OrderLifecycle) -> None:
        """INSERT ... ON CONFLICT DO UPDATE SET ...

        - created_at 은 최초 삽입값 유지
        - updated_at 만 현재 시각으로 갱신
        """
        conn = self._get_conn()
        d = lc.to_dict()
        conn.execute(
            """
            INSERT INTO lifecycle_orders (
                order_lifecycle_id, trade_id, market, code, side,
                strategy_name, client_order_id, odno, current_state,
                order_qty, filled_qty, remaining_qty, avg_fill_price,
                submitted_at, accepted_at, first_fill_at, last_fill_at,
                closed_at, had_partial_fill, terminal_reason,
                retry_count, last_error
            ) VALUES (
                :order_lifecycle_id, :trade_id, :market, :code, :side,
                :strategy_name, :client_order_id, :odno, :current_state,
                :order_qty, :filled_qty, :remaining_qty, :avg_fill_price,
                :submitted_at, :accepted_at, :first_fill_at, :last_fill_at,
                :closed_at, :had_partial_fill, :terminal_reason,
                :retry_count, :last_error
            )
            ON CONFLICT(order_lifecycle_id) DO UPDATE SET
                trade_id         = excluded.trade_id,
                market           = excluded.market,
                code             = excluded.code,
                side             = excluded.side,
                strategy_name    = excluded.strategy_name,
                client_order_id  = excluded.client_order_id,
                odno             = excluded.odno,
                current_state    = excluded.current_state,
                order_qty        = excluded.order_qty,
                filled_qty       = excluded.filled_qty,
                remaining_qty    = excluded.remaining_qty,
                avg_fill_price   = excluded.avg_fill_price,
                submitted_at     = excluded.submitted_at,
                accepted_at      = excluded.accepted_at,
                first_fill_at    = excluded.first_fill_at,
                last_fill_at     = excluded.last_fill_at,
                closed_at        = excluded.closed_at,
                had_partial_fill = excluded.had_partial_fill,
                terminal_reason  = excluded.terminal_reason,
                retry_count      = excluded.retry_count,
                last_error       = excluded.last_error,
                updated_at       = strftime('%Y-%m-%dT%H:%M:%f','now','localtime')
            -- created_at 은 갱신하지 않음 (최초 삽입값 유지)
            """,
            d,
        )
        conn.commit()
