"""이벤트 모델 및 팩토리.

Event 는 append-only 사실 기록이다. projection 은 이벤트에서 파생될 뿐,
이벤트 없이는 절대 바뀌지 않는다(Phase 1 설계 §2).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


# ── 열거형(문자열 상수) ────────────────────────────────────────────
class EventType:
    ORDER_INTENT_RECORDED = "OrderIntentRecorded"
    ORDER_SUBMIT_ATTEMPTED = "OrderSubmitAttempted"
    ORDER_ACK_RECEIVED = "OrderAckReceived"
    ORDER_SUBMIT_AMBIGUOUS = "OrderSubmitAmbiguous"
    EXECUTION_OBSERVED = "ExecutionObserved"
    ORDER_CANCELED = "OrderCanceled"
    ORDER_REJECTED = "OrderRejected"
    ORDER_CLOSED = "OrderClosed"
    POSITION_RECONCILED = "PositionReconciled"
    REALIZED_PNL_BOOKED = "RealizedPnlBooked"
    DAILY_SESSION_RESET = "DailySessionReset"
    RISK_STATE_CHANGED = "RiskStateChanged"
    RECOVERY_STARTED = "RecoveryStarted"
    RECOVERY_COMPLETED = "RecoveryCompleted"


class OrderSide:
    BUY = "BUY"
    SELL = "SELL"


class OrderState:
    INTENT = "INTENT"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    AMBIGUOUS = "AMBIGUOUS"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class IntentKind:
    """주문 의도 분류(Phase 1 설계 §8). 위험 증가/감소 게이트에 사용."""
    NEW_BUY = "NEW_BUY"        # 위험 증가
    ADD_BUY = "ADD_BUY"        # 위험 증가(피라미딩 추가매수)
    SELL = "SELL"             # 위험 감소(청산)
    LIQUIDATION = "LIQUIDATION"  # 위험 감소(손절/긴급청산)

    RISK_INCREASING = frozenset({NEW_BUY, ADD_BUY})
    RISK_REDUCING = frozenset({SELL, LIQUIDATION})


class ApplyStatus:
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"


@dataclass(frozen=True)
class ApplyResult:
    status: str
    seq: Optional[int] = None

    @property
    def applied(self) -> bool:
        return self.status == ApplyStatus.APPLIED


# ── Event ─────────────────────────────────────────────────────────
_EVENT_COLUMNS = (
    "seq", "event_uuid", "ts", "type", "aggregate_type", "aggregate_id",
    "client_order_id", "odno", "code", "side", "qty", "price",
    "cum_filled_qty", "realized_pnl", "idempotency_key", "payload", "schema_ver",
)


@dataclass
class Event:
    type: str
    aggregate_type: str
    aggregate_id: str
    event_uuid: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: str = field(default_factory=lambda: datetime.now().isoformat())
    client_order_id: Optional[str] = None
    odno: Optional[str] = None
    code: Optional[str] = None
    side: Optional[str] = None
    qty: Optional[int] = None
    price: Optional[float] = None
    cum_filled_qty: Optional[int] = None
    realized_pnl: Optional[float] = None
    idempotency_key: Optional[str] = None
    payload: Optional[str] = None
    schema_ver: int = 1
    seq: Optional[int] = None  # append 후 채워짐

    def insert_tuple(self) -> tuple:
        return (
            self.event_uuid, self.ts, self.type, self.aggregate_type,
            self.aggregate_id, self.client_order_id, self.odno, self.code,
            self.side, self.qty, self.price, self.cum_filled_qty,
            self.realized_pnl, self.idempotency_key, self.payload, self.schema_ver,
        )

    @classmethod
    def from_row(cls, row) -> "Event":
        d = {k: row[k] for k in _EVENT_COLUMNS}
        ev = cls(
            type=d["type"], aggregate_type=d["aggregate_type"],
            aggregate_id=d["aggregate_id"], event_uuid=d["event_uuid"], ts=d["ts"],
            client_order_id=d["client_order_id"], odno=d["odno"], code=d["code"],
            side=d["side"], qty=d["qty"], price=d["price"],
            cum_filled_qty=d["cum_filled_qty"], realized_pnl=d["realized_pnl"],
            idempotency_key=d["idempotency_key"], payload=d["payload"],
            schema_ver=d["schema_ver"],
        )
        ev.seq = d["seq"]
        return ev


# ── 팩토리 (idempotency_key 규칙을 한 곳에 집중) ─────────────────────
def order_key(odno: Optional[str], client_order_id: str) -> str:
    """반영/멱등 단위 키. odno 확보 시 브로커 durable 키를 우선 사용."""
    return odno or client_order_id


def intent_event(client_order_id: str, code: str, side: str, qty: int,
                 kind: str) -> Event:
    return Event(
        type=EventType.ORDER_INTENT_RECORDED, aggregate_type="order",
        aggregate_id=client_order_id, client_order_id=client_order_id,
        code=code, side=side, qty=qty,
        payload=f'{{"kind":"{kind}"}}',
        idempotency_key=f"intent:{client_order_id}",
    )


def ack_event(client_order_id: str, odno: str, code: str, side: str,
              qty: int) -> Event:
    return Event(
        type=EventType.ORDER_ACK_RECEIVED, aggregate_type="order",
        aggregate_id=client_order_id, client_order_id=client_order_id,
        odno=odno, code=code, side=side, qty=qty,
        idempotency_key=f"ack:{client_order_id}:{odno}",
    )


def ambiguous_event(client_order_id: str, code: str, side: str, qty: int) -> Event:
    return Event(
        type=EventType.ORDER_SUBMIT_AMBIGUOUS, aggregate_type="order",
        aggregate_id=client_order_id, client_order_id=client_order_id,
        code=code, side=side, qty=qty,
        idempotency_key=f"ambiguous:{client_order_id}",
    )


def execution_event(client_order_id: str, code: str, side: str,
                    cum_filled_qty: int, price: float,
                    odno: Optional[str] = None,
                    ord_qty: Optional[int] = None) -> Event:
    """브로커 관측: 누적 체결수량. idempotency_key = exec:{order_key}:{cum}."""
    ok = order_key(odno, client_order_id)
    return Event(
        type=EventType.EXECUTION_OBSERVED, aggregate_type="order",
        aggregate_id=client_order_id, client_order_id=client_order_id,
        odno=odno, code=code, side=side, qty=ord_qty,
        cum_filled_qty=cum_filled_qty, price=price,
        idempotency_key=f"exec:{ok}:{cum_filled_qty}",
    )


def canceled_event(client_order_id: str, code: str, odno: Optional[str] = None) -> Event:
    ok = order_key(odno, client_order_id)
    return Event(
        type=EventType.ORDER_CANCELED, aggregate_type="order",
        aggregate_id=client_order_id, client_order_id=client_order_id,
        odno=odno, code=code,
        idempotency_key=f"cancel:{ok}",
    )


def reconcile_event(code: str, broker_qty: int, broker_avg: float,
                    token: str) -> Event:
    """broker 잔고 기준 포지션 보정. token 으로 멱등 단위를 구분(예: 세션+seq)."""
    return Event(
        type=EventType.POSITION_RECONCILED, aggregate_type="position",
        aggregate_id=code, code=code, qty=broker_qty, price=broker_avg,
        idempotency_key=f"reconcile:{code}:{token}",
    )
