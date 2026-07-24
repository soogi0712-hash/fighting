"""OrderGate — 복구/위험 게이트 (Phase 1 §8).

게이트는 브로커 제출 직전에 강제된다(부팅 1회가 아님). 분류:
  위험 증가: NEW_BUY, ADD_BUY  → 복구 완료 전 차단
  위험 감소: SELL, LIQUIDATION → 잔고 확증(balance_confirmed) 전 차단
Safe-Halt 시 전면 차단.

Phase 2-2 단계에서는 게이트 판정만 제공하며, 실제 주문 제출에는 연결되지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .db import Database
from .models import IntentKind


@dataclass
class OrderIntent:
    code: str
    side: str
    qty: int
    kind: str   # IntentKind.*


@dataclass
class GateDecision:
    allowed: bool
    reason: str


class OrderGate:
    def __init__(self, db: Database):
        self.db = db

    def check(self, intent: OrderIntent) -> GateDecision:
        st = self.db.conn.execute(
            "SELECT safe_halt, recovery_state FROM engine_state WHERE id=1"
        ).fetchone()
        safe_halt = bool(st and st["safe_halt"])
        recovery_state = st["recovery_state"] if st else "RECOVERING"

        if safe_halt:
            return GateDecision(False, "SAFE_HALT")

        if intent.kind in IntentKind.RISK_INCREASING:
            if recovery_state != "COMPLETED":
                return GateDecision(False, "RECOVERY_INCOMPLETE")
            if self._has_open_order(intent.code):
                return GateDecision(False, "ORDER_IN_FLIGHT")
            return GateDecision(True, "OK")

        if intent.kind in IntentKind.RISK_REDUCING:
            if not self._balance_confirmed(intent.code):
                return GateDecision(False, "BALANCE_UNCONFIRMED")
            return GateDecision(True, "OK")

        return GateDecision(False, "UNKNOWN_KIND")

    # ── 보조 ───────────────────────────────────────────────────────
    def _balance_confirmed(self, code: str) -> bool:
        r = self.db.conn.execute(
            "SELECT balance_confirmed, qty FROM positions WHERE code=?", (code,)
        ).fetchone()
        return bool(r and r["balance_confirmed"] and int(r["qty"]) > 0)

    def _has_open_order(self, code: str) -> bool:
        r = self.db.conn.execute(
            "SELECT COUNT(*) c FROM order_index WHERE code=? AND state IN "
            "('INTENT','SUBMITTING','SUBMITTED','AMBIGUOUS','PARTIALLY_FILLED')",
            (code,),
        ).fetchone()
        return bool(r and r["c"])
