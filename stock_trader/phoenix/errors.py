"""Phoenix 예외 계층."""


class PhoenixError(Exception):
    """Phoenix 기반 예외."""


class SafeHaltError(PhoenixError):
    """엔진이 Safe-Halt 상태라 주문/처리를 거부할 때."""


class CorruptionError(PhoenixError):
    """event store 무결성 검사 실패."""


class SchemaVersionError(PhoenixError):
    """DB 스키마 버전이 코드보다 높아 안전하게 열 수 없을 때(다운그레이드 금지)."""


class AlreadyApplied(PhoenixError):
    """idempotency_key 충돌 — 이미 반영된 이벤트(내부 신호용)."""
