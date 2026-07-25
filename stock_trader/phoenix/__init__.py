"""
Project Phoenix — crash-safe 체결 엔진 (Phase 2-1 Event Store).

이 패키지는 기존 운영 코드(app.py / strategies / api)와 **분리**되어 있으며,
Shadow Mode(Phase 2-4) 이전에는 어떤 실계좌 주문 경로에도 연결되지 않는다.

구성:
  db          : SQLite 연결/PRAGMA(WAL, synchronous=FULL)/트랜잭션 헬퍼/스키마 마이그레이션
  models      : Event 및 이벤트 타입/열거형/팩토리
  projections : positions / order_index / daily_pnl projection (watermark delta)
  event_store : append-only events + UNIQUE idempotency + 단일 트랜잭션 apply
  recovery    : replay / snapshot / Safe-Halt / recovery 게이트 상태
  gate        : OrderGate (신규 위험증가 vs 위험감소 분류 + 복구 게이트)
  reconcile   : broker 잔고 ↔ event projection 대사
  backup      : 온라인 백업 + corruption 감지
"""
from .models import (
    Event, EventType, OrderSide, OrderState, IntentKind, ApplyStatus, ApplyResult,
)
from .db import Database, SimulatedCrash
from .event_store import EventStore
from .projections import Projector
from .recovery import Recovery
from .gate import OrderGate, OrderIntent, GateDecision
from .reconcile import Reconciler
from .backup import backup_online, check_integrity, IntegrityResult
from .errors import PhoenixError, SafeHaltError, CorruptionError, SchemaVersionError
from .lifecycle import (
    LifecycleState,
    LifecycleTransitionError,
    LifecycleNotFoundError,
    DuplicateClientOrderIdError,
    TransitionValidator,
    OrderLifecycle,
    OrderLifecycleManager,
    make_order_lifecycle_id,
)
from .execution_driven import ExecutionDrivenPositionUpdater

__all__ = [
    "Event", "EventType", "OrderSide", "OrderState", "IntentKind",
    "ApplyStatus", "ApplyResult", "Database", "SimulatedCrash",
    "EventStore", "Projector", "Recovery", "OrderGate", "OrderIntent",
    "GateDecision", "Reconciler", "backup_online", "check_integrity",
    "IntegrityResult", "PhoenixError", "SafeHaltError", "CorruptionError",
    "SchemaVersionError",
    # lifecycle
    "LifecycleState", "LifecycleTransitionError", "LifecycleNotFoundError",
    "DuplicateClientOrderIdError", "TransitionValidator",
    "OrderLifecycle", "OrderLifecycleManager", "make_order_lifecycle_id",
    # execution-driven position update
    "ExecutionDrivenPositionUpdater",
]

SCHEMA_VERSION = 1
