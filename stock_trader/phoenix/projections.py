"""Projection 적용 로직 (positions / order_index / daily_pnl).

핵심: 인바운드 체결 멱등은 **누적 체결수량 watermark delta**(Phase 1 §5.2).
    delta = max(0, cum_filled_qty - applied_qty)
  → 중복/역순 관측은 delta=0 으로 무시, 부분체결 증가분만 반영.

Projector 는 오직 projection 테이블만 건드린다(events 테이블에 append 하지 않음).
따라서 rebuild() 시 저장된 events 를 순서대로 apply 하면 projection 이 그대로 재구축된다.
"""
from __future__ import annotations

import sqlite3

from .models import Event, EventType, OrderSide, OrderState


class Projector:
    # ── 진입점 ─────────────────────────────────────────────────────
    def apply(self, conn: sqlite3.Connection, ev: Event, seq: int | None = None) -> None:
        handler = self._DISPATCH.get(ev.type)
        if handler is None:
            return  # 순수 감사 이벤트(RiskStateChanged 등)는 projection 무영향
        handler(self, conn, ev, seq)

    # ── order 수명주기 ─────────────────────────────────────────────
    def _on_intent(self, conn, ev: Event, seq):
        conn.execute(
            "INSERT INTO order_index(client_order_id, code, side, ord_qty, "
            "applied_qty, state, updated_seq) VALUES (?,?,?,?,0,?,?) "
            "ON CONFLICT(client_order_id) DO NOTHING",
            (ev.client_order_id, ev.code, ev.side, ev.qty or 0,
             OrderState.INTENT, seq),
        )

    def _on_ack(self, conn, ev: Event, seq):
        self._ensure_order(conn, ev, seq)
        conn.execute(
            "UPDATE order_index SET odno=?, state=?, updated_seq=? "
            "WHERE client_order_id=?",
            (ev.odno, OrderState.SUBMITTED, seq, ev.client_order_id),
        )

    def _on_ambiguous(self, conn, ev: Event, seq):
        self._ensure_order(conn, ev, seq)
        conn.execute(
            "UPDATE order_index SET state=?, updated_seq=? WHERE client_order_id=?",
            (OrderState.AMBIGUOUS, seq, ev.client_order_id),
        )

    def _on_canceled(self, conn, ev: Event, seq):
        self._ensure_order(conn, ev, seq)
        conn.execute(
            "UPDATE order_index SET state=?, updated_seq=? WHERE client_order_id=?",
            (OrderState.CANCELED, seq, ev.client_order_id),
        )

    def _on_rejected(self, conn, ev: Event, seq):
        self._ensure_order(conn, ev, seq)
        conn.execute(
            "UPDATE order_index SET state=?, updated_seq=? WHERE client_order_id=?",
            (OrderState.REJECTED, seq, ev.client_order_id),
        )

    def _on_closed(self, conn, ev: Event, seq):
        conn.execute(
            "UPDATE order_index SET state=?, updated_seq=? WHERE client_order_id=?",
            (OrderState.CLOSED, seq, ev.client_order_id),
        )

    # ── 체결 관측: watermark delta ─────────────────────────────────
    def _on_execution(self, conn, ev: Event, seq):
        self._ensure_order(conn, ev, seq)
        row = conn.execute(
            "SELECT ord_qty, applied_qty FROM order_index WHERE client_order_id=?",
            (ev.client_order_id,),
        ).fetchone()
        applied = int(row["applied_qty"])
        ord_qty = int(row["ord_qty"] or (ev.qty or 0))
        cum = int(ev.cum_filled_qty or 0)
        delta = max(0, cum - applied)   # ★ 역순/중복 → 0
        if delta <= 0:
            return  # projection 무변경 (watermark 후퇴 없음)

        price = float(ev.price or 0.0)
        if ev.side == OrderSide.BUY:
            self._position_buy(conn, ev.code, delta, price, seq)
        else:
            realized = self._position_sell(conn, ev.code, delta, price, seq)
            self._book_realized(conn, realized, seq)

        new_state = OrderState.FILLED if (ord_qty > 0 and cum >= ord_qty) \
            else OrderState.PARTIALLY_FILLED
        conn.execute(
            "UPDATE order_index SET applied_qty=?, state=?, updated_seq=? "
            "WHERE client_order_id=?",
            (cum, new_state, seq, ev.client_order_id),
        )

    # ── 포지션 대사 ────────────────────────────────────────────────
    def _on_reconciled(self, conn, ev: Event, seq):
        """broker 잔고가 최종 권위(수량). 조용한 덮어쓰기가 아니라 이벤트로 반영."""
        qty = int(ev.qty or 0)
        avg = float(ev.price or 0.0)
        if qty <= 0:
            conn.execute(
                "INSERT INTO positions(code, qty, avg_price, cost_basis, "
                "balance_confirmed, updated_seq) VALUES (?,0,0,0,1,?) "
                "ON CONFLICT(code) DO UPDATE SET qty=0, cost_basis=0, "
                "balance_confirmed=1, updated_seq=excluded.updated_seq",
                (ev.code, seq),
            )
            return
        conn.execute(
            "INSERT INTO positions(code, qty, avg_price, cost_basis, "
            "balance_confirmed, updated_seq) VALUES (?,?,?,?,1,?) "
            "ON CONFLICT(code) DO UPDATE SET qty=excluded.qty, "
            "avg_price=excluded.avg_price, cost_basis=excluded.cost_basis, "
            "balance_confirmed=1, updated_seq=excluded.updated_seq",
            (ev.code, qty, avg, avg * qty, seq),
        )

    # ── 세션 ───────────────────────────────────────────────────────
    def _on_daily_reset(self, conn, ev: Event, seq):
        new_key = ev.aggregate_id
        conn.execute("UPDATE engine_state SET session_key=? WHERE id=1", (new_key,))
        conn.execute(
            "INSERT INTO daily_pnl(session_key, updated_seq) VALUES (?,?) "
            "ON CONFLICT(session_key) DO NOTHING",
            (new_key, seq),
        )

    # ── 포지션 원가/손익 계산 ──────────────────────────────────────
    def _position_buy(self, conn, code, delta, price, seq):
        row = conn.execute(
            "SELECT qty, cost_basis FROM positions WHERE code=?", (code,)
        ).fetchone()
        if row:
            qty = int(row["qty"]) + delta
            cost = float(row["cost_basis"]) + delta * price
            avg = cost / qty if qty else 0.0
            conn.execute(
                "UPDATE positions SET qty=?, avg_price=?, cost_basis=?, updated_seq=? "
                "WHERE code=?", (qty, avg, cost, seq, code),
            )
        else:
            cost = delta * price
            conn.execute(
                "INSERT INTO positions(code, qty, avg_price, cost_basis, "
                "balance_confirmed, updated_seq) VALUES (?,?,?,?,0,?)",
                (code, delta, price, cost, seq),
            )

    def _position_sell(self, conn, code, delta, price, seq) -> float:
        row = conn.execute(
            "SELECT qty, avg_price, cost_basis FROM positions WHERE code=?", (code,)
        ).fetchone()
        avg = float(row["avg_price"]) if row else 0.0
        realized = delta * (price - avg)
        if row:
            qty = int(row["qty"]) - delta
            cost = float(row["cost_basis"]) - delta * avg
            if qty <= 0:
                qty, cost = 0, 0.0
            conn.execute(
                "UPDATE positions SET qty=?, cost_basis=?, updated_seq=? WHERE code=?",
                (qty, cost, seq, code),
            )
        else:
            # 포지션 없이 매도 관측(대사 전 유령) — 수량 0으로 기록만
            conn.execute(
                "INSERT INTO positions(code, qty, avg_price, cost_basis, "
                "balance_confirmed, updated_seq) VALUES (?,0,0,0,0,?)",
                (code, seq),
            )
        return realized

    def _book_realized(self, conn, realized, seq):
        skey = self._session_key(conn)
        conn.execute(
            "INSERT INTO daily_pnl(session_key, realized_pnl, peak_pnl, trades, "
            "updated_seq) VALUES (?,?,?,1,?) "
            "ON CONFLICT(session_key) DO UPDATE SET "
            "realized_pnl = daily_pnl.realized_pnl + ?, "
            "trades = daily_pnl.trades + 1, updated_seq = ?",
            (skey, realized, max(0.0, realized), seq, realized, seq),
        )
        # peak 갱신
        conn.execute(
            "UPDATE daily_pnl SET peak_pnl = MAX(peak_pnl, realized_pnl) "
            "WHERE session_key=?", (skey,),
        )

    # ── 유틸 ───────────────────────────────────────────────────────
    def _ensure_order(self, conn, ev: Event, seq):
        conn.execute(
            "INSERT INTO order_index(client_order_id, code, side, ord_qty, "
            "applied_qty, state, updated_seq) VALUES (?,?,?,?,0,?,?) "
            "ON CONFLICT(client_order_id) DO NOTHING",
            (ev.client_order_id, ev.code, ev.side, ev.qty or 0,
             OrderState.INTENT, seq),
        )
        if ev.qty:  # ord_qty 가 나중 이벤트에서 확정되면 보강
            conn.execute(
                "UPDATE order_index SET ord_qty=? WHERE client_order_id=? AND ord_qty=0",
                (ev.qty, ev.client_order_id),
            )

    def _session_key(self, conn) -> str:
        r = conn.execute("SELECT session_key FROM engine_state WHERE id=1").fetchone()
        return r["session_key"] if r else "INIT"

    # ── 재구축(replay) ─────────────────────────────────────────────
    def rebuild(self, conn: sqlite3.Connection) -> int:
        """projection 을 폐기하고 events 전량 재생으로 재구축. 마지막 seq 반환."""
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM order_index")
        conn.execute("DELETE FROM daily_pnl")
        conn.execute(
            "UPDATE engine_state SET session_key='INIT' WHERE id=1")
        last_seq = 0
        for row in conn.execute("SELECT * FROM events ORDER BY seq"):
            ev = Event.from_row(row)
            self.apply(conn, ev, ev.seq)
            last_seq = ev.seq or last_seq
        conn.execute(
            "UPDATE processed_watermark SET last_seq=? WHERE id=1", (last_seq,))
        return last_seq

    _DISPATCH = {
        EventType.ORDER_INTENT_RECORDED: _on_intent,
        EventType.ORDER_ACK_RECEIVED: _on_ack,
        EventType.ORDER_SUBMIT_AMBIGUOUS: _on_ambiguous,
        EventType.ORDER_CANCELED: _on_canceled,
        EventType.ORDER_REJECTED: _on_rejected,
        EventType.ORDER_CLOSED: _on_closed,
        EventType.EXECUTION_OBSERVED: _on_execution,
        EventType.POSITION_RECONCILED: _on_reconciled,
        EventType.DAILY_SESSION_RESET: _on_daily_reset,
    }
