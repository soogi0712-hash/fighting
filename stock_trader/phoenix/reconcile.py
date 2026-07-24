"""Reconciler — broker 잔고 ↔ event projection 대사 (Phase 1 §7).

권위: 현재 보유 수량은 **broker 가 최종 진실**. 단 조용한 덮어쓰기가 아니라
PositionReconciled 이벤트로 반영하고, balance_confirmed=1 로 표시(SELL 게이트 해제).
"""
from __future__ import annotations

from dataclasses import dataclass

from .db import Database
from .event_store import EventStore
from .models import reconcile_event


@dataclass
class Discrepancy:
    code: str
    projection_qty: int
    broker_qty: int
    action: str   # 'ADJUSTED' | 'CONFIRMED'


class Reconciler:
    def __init__(self, db: Database, store: EventStore):
        self.db = db
        self.store = store

    def reconcile(self, broker_holdings: list[dict], token: str | None = None
                  ) -> list[Discrepancy]:
        """broker_holdings: [{'code','qty','avg_price'}]. 대사 후 discrepancy 리포트."""
        if token is None:
            token = f"seq{self.store.last_seq()}"
        report: list[Discrepancy] = []
        broker_map = {h["code"]: h for h in broker_holdings}

        # ① broker 보유 종목 → projection 과 대조/확증
        for code, h in broker_map.items():
            bqty = int(h.get("qty", 0))
            bavg = float(h.get("avg_price", 0.0))
            pos = self.store.get_position(code)
            pqty = int(pos["qty"]) if pos else 0
            confirmed = bool(pos and pos["balance_confirmed"])
            if pqty != bqty or not confirmed:
                self.store.apply(reconcile_event(code, bqty, bavg, token))
                report.append(Discrepancy(code, pqty, bqty,
                                          "ADJUSTED" if pqty != bqty else "CONFIRMED"))

        # ② projection 엔 있으나 broker 엔 없는(qty>0) 종목 → 0 으로 보정
        rows = self.db.conn.execute(
            "SELECT code, qty FROM positions WHERE qty > 0").fetchall()
        for r in rows:
            code = r["code"]
            if code not in broker_map:
                self.store.apply(reconcile_event(code, 0, 0.0, token))
                report.append(Discrepancy(code, int(r["qty"]), 0, "ADJUSTED"))

        return report
