"""Recovery — 부팅 시 projection 재구축 + 복구 게이트 상태 관리.

recovery_state 는 **부팅마다 재게이트**된다(이벤트 재생으로 유도하지 않음).
  begin()    : recovery_state=RECOVERING (신규 위험증가 주문 차단)
  rebuild()  : snapshot 무시 시 events 전량 재생으로 projection 재구축
  complete() : reconcile 완료 후 recovery_state=COMPLETED (게이트 해제)
"""
from __future__ import annotations

from .db import Database
from .projections import Projector


class Recovery:
    def __init__(self, db: Database, projector: Projector | None = None):
        self.db = db
        self.projector = projector or Projector()

    def state(self) -> str:
        r = self.db.conn.execute(
            "SELECT recovery_state FROM engine_state WHERE id=1").fetchone()
        return r["recovery_state"] if r else "RECOVERING"

    def is_completed(self) -> bool:
        return self.state() == "COMPLETED"

    def begin(self) -> None:
        """부팅 시작: 항상 RECOVERING 으로 되돌려 재게이트."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE engine_state SET recovery_state='RECOVERING' WHERE id=1")

    def rebuild_projection(self) -> int:
        """projection 을 events 재생으로 재구축. 마지막 seq 반환."""
        with self.db.transaction() as conn:
            return self.projector.rebuild(conn)

    def complete(self) -> None:
        """reconcile 완료 후 게이트 해제."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE engine_state SET recovery_state='COMPLETED' WHERE id=1")

    # ── snapshot (최적화용, 진실 아님) ─────────────────────────────
    def snapshot_last_seq(self) -> int:
        r = self.db.conn.execute(
            "SELECT last_seq FROM snapshot_meta WHERE id=1").fetchone()
        return int(r["last_seq"]) if r else 0
