"""
execution_bridge.py — 주문 접수 ↔ 실제 체결 배선 (GAP2 라이브)

목적:
  '접수(rt_cd==0)'를 체결로 간주하던 즉시 apply 구조를 제거하고,
  접수 → PendingRegistry 등록(영속) → 매 tick poll → 실제 체결 delta 만
  포지션/손익에 반영하는 구조로 교체한다.

기존 gap2 구성요소만 재사용(새 체결엔진 작성 금지):
  - PendingRegistry / PendingOrder (영속 save_to/load_from)
  - FillSource(+CumulativeFillTracker) : 누적→delta, idempotent
  - poll_pending_fills / PollResult    : 조인·상태전이
  - OrderStateSource                    : (선택) 취소/거부 확정

시장(KR/US)별로 1개 브릿지. apply 는 콜백으로 주입(포지션 매니저 분리).
"""
from __future__ import annotations

import time as _time

from strategies.pending_orders import PendingRegistry
from strategies.poll_orchestrator import poll_pending_fills, OrderStateSource


class NoOrderStateSource(OrderStateSource):
    """취소/거부 확정 소스 미확보 시 사용(빈 상태 → 추론 금지). 체결은 FillSource 로만."""
    def get_states(self, market, order_nos):
        return {}


class ExecutionBridge:
    """
    한 시장의 미확정 주문 상태 + 체결 반영 배선.

    콜백:
      on_buy_fill(ev)   : 실제 매수 체결 delta 반영(포지션 생성/증가)
      on_sell_fill(ev)  : 실제 매도 체결 delta 반영(포지션 차감/청산)
      on_terminal(st)   : (선택) 취소/거부 확정 통지
    ev 는 AppliedFillEvent(applied_qty=실제 delta, price=체결가, became_filled 등).
    """
    def __init__(self, market: str, fill_source, pending_path: str,
                 state_source=None, now_fn=None, logger=None):
        self.market = market
        self.fill_source = fill_source
        self.state_source = state_source or NoOrderStateSource()
        self.pending_path = pending_path
        self.now_fn = now_fn or _time.time
        self.registry = PendingRegistry(now_fn=self.now_fn)
        self.log = logger

    # ── 재시작 복구 ─────────────────────────────────────────
    def restore(self) -> int:
        try:
            n = self.registry.load_from(self.pending_path)
        except Exception as e:
            if self.log:
                self.log.error(f"[ExecBridge:{self.market}] pending 복구 실패: {e}")
            return 0
        # ★ 이미 반영된 누적수량으로 FillSource tracker 를 seed → 재시작 후
        #   같은 누적을 다시 delta 로 방출(=이중 반영)하지 않도록 한다.
        if hasattr(self.fill_source, "seed"):
            for po in self.registry.all_open():
                if po.applied_qty > 0:
                    try:
                        self.fill_source.seed(self.market, po.order_no,
                                              po.applied_qty, po.applied_qty * po.req_price)
                    except Exception:
                        pass
        return n

    def _save(self):
        try:
            self.registry.save_to(self.pending_path)
        except Exception as e:
            if self.log:
                self.log.error(f"[ExecBridge:{self.market}] pending 저장 실패: {e}")

    # ── 접수 등록 (즉시 apply 금지) ─────────────────────────
    def register_accept(self, order_no, code, name, side, level,
                        req_qty, req_price, using_compound=0.0, is_full=False) -> bool:
        """
        주문 접수 성공 시 호출. 주문번호가 없으면 등록하지 않고 False(오류).
        등록만 하고 포지션은 절대 만들지 않는다(체결은 poll 이 반영).
        """
        if not order_no:
            if self.log:
                self.log.error(f"[ExecBridge:{self.market}] 주문번호 없음 → pending 등록 금지 {code}")
            return False
        try:
            self.registry.register(order_no, self.market, code, name, side, level,
                                   int(req_qty), float(req_price),
                                   is_full=bool(is_full), using_compound=float(using_compound or 0))
        except Exception as e:
            if self.log:
                self.log.error(f"[ExecBridge:{self.market}] pending 등록 오류 {code}: {e}")
            return False
        self._save()
        if self.log:
            self.log.info(f"📝 [ExecBridge:{self.market}] 접수등록 {side} {code} {req_qty}주 "
                          f"@{req_price} order_no={order_no} (체결대기)")
        return True

    # ── 중복 주문 방지용 ────────────────────────────────────
    def has_open(self, code, side=None) -> bool:
        return self.registry.has_open(code, side)

    def open_count(self) -> int:
        return len(self.registry.all_open())

    # ── 매 tick 체결 폴링 + 반영 ────────────────────────────
    def poll(self, on_buy_fill, on_sell_fill, on_terminal=None, timeout_sec=15):
        """
        FillSource 로 실제 체결 delta 를 조회해 콜백으로 반영. 처리 후 pending 저장.
        빈 상태면 조회 없이 즉시 반환. 반환: PollResult | None.
        """
        if not self.registry:            # 비어있으면 API 조회 안 함
            return None
        result = poll_pending_fills(
            self.fill_source, self.state_source, self.registry,
            now=self.now_fn(), timeout_sec=timeout_sec, logger=self.log)
        for ev in result.fills:
            if ev.applied_qty <= 0:
                continue
            try:
                if ev.side == "BUY":
                    on_buy_fill(ev)
                else:
                    on_sell_fill(ev)
            except Exception as e:
                if self.log:
                    self.log.error(f"[ExecBridge:{self.market}] 체결반영 콜백 오류 "
                                   f"{ev.side} {ev.code}: {e}")
        if on_terminal:
            for st in result.statuses:
                try:
                    on_terminal(st)
                except Exception:
                    pass
        self._save()
        return result

    def snapshot(self) -> dict:
        opens = self.registry.all_open()
        return {
            "market": self.market,
            "open_count": len(opens),
            "open": [{"order_no": o.order_no, "code": o.code, "side": o.side,
                      "req_qty": o.req_qty, "applied_qty": o.applied_qty,
                      "status": o.status} for o in opens],
        }
