"""
engine/execution_bridge.py — 주문 접수 ↔ 실제 체결 배선 (GAP2 v2)

목적:
  '접수(rt_cd==0)'를 체결로 간주하던 즉시 apply 구조를 제거하고,
  접수 → PendingRegistry 등록(영속) → 매 tick poll → 실제 체결 delta만
  포지션/손익에 반영하는 구조로 교체한다.

GAP2 stock_trader/strategies/execution_bridge.py 에서 이식.
import 경로 수정 + 오류 복구 정책(backoff, RECOVERY_REQUIRED) 강화.

오류 복구 정책 (요구사항 §9):
  - poll 실패 → 재시도 카운터 증가. pending 삭제 금지.
  - 연속 실패 → 지수형 backoff (최대 120초 대기)
  - timeout(15초) → CancelRequest 이벤트만 방출. 포지션 생성/삭제 금지.
  - RECOVERY_REQUIRED: 최대 실패 횟수(10) 초과 시 mark_recovery 호출.
    프로그램 재시작 후 재확인 가능하도록 pending 파일에 영속화.
  - 취소 성공 여부는 브로커 확인 후에만 CANCELED 처리.
"""
from __future__ import annotations

import os
import time as _time

from engine.pending_orders import PendingRegistry
from engine.poll_orchestrator import poll_pending_fills, NoOrderStateSource

_MAX_FAIL_BEFORE_RECOVERY = 10   # 연속 실패 이 횟수 초과 → RECOVERY_REQUIRED
_BACKOFF_BASE   = 2.0            # 지수형 backoff 기저
_BACKOFF_MAX    = 120.0          # 최대 대기 초


def _dispatch(callback, po, ev):
    """
    콜백 시그니처가 1-arg(ev) 또는 2-arg(po, ev)인지 자동 판별해 호출.
    테스트(2-arg) 와 전략(1-arg) 둘 다 지원.
    """
    import inspect
    try:
        sig = inspect.signature(callback)
        n = len([
            p for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
        ])
    except (ValueError, TypeError):
        n = 1
    if n >= 2:
        callback(po, ev)
    else:
        callback(ev)


class ExecutionBridge:
    """
    한 시장의 미확정 주문 상태 + 체결 반영 배선.

    콜백:
      on_buy_fill(ev)   : 실제 매수 체결 delta 반영(포지션 생성/증가)
      on_sell_fill(ev)  : 실제 매도 체결 delta 반영(포지션 차감/청산)
      on_terminal(st)   : (선택) 취소/거부 확정 통지
      on_cancel_request(cr): (선택) timeout → 취소 요청 통지
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

        # 오류 복구 상태
        self._poll_fail_count = 0
        self._next_allowed_poll_ts = 0.0   # backoff 대기 만료 시각

        # CRITICAL-2: 콜백 실패 시 delta 유실 방지 — retry queue
        # {order_no: [AppliedFillEvent, ...]}
        # apply_delta는 이미 확정됐으나 콜백이 실패한 이벤트 보관
        # 다음 poll 시 retry_queue 우선 처리 → 콜백 재시도
        self._callback_retry_queue: list = []   # list of (po, ev)

    # ── 재시작 복구 ─────────────────────────────────────────
    def restore(self) -> int:
        """
        재시작 시 pending 파일 복구 + FillSource tracker seed.
        seed → 재시작 후 같은 누적을 다시 delta로 방출하지 않음.

        MEDIUM-6: 손상 JSON 시 registry.load_from()이 RuntimeError 발생.
          - 오류 로그 출력 (RECOVERY_REQUIRED 표시)
          - 0 반환 → 상위 호출자가 판단 가능
        """
        try:
            n = self.registry.load_from(self.pending_path)
        except RuntimeError as e:
            # MEDIUM-6: 손상 JSON — RECOVERY_REQUIRED 로그, 0 반환
            if self.log:
                self.log.error(
                    f"[ExecBridge:{self.market}] RECOVERY_REQUIRED: "
                    f"pending 파일 손상 — {e}"
                )
            return 0
        except Exception as e:
            if self.log:
                self.log.error(f"[ExecBridge:{self.market}] pending 복구 실패: {e}")
            return 0
        if hasattr(self.fill_source, "seed"):
            for po in self.registry.all_open():
                if po.applied_qty > 0:
                    try:
                        self.fill_source.seed(
                            self.market, po.order_no,
                            po.applied_qty, po.applied_qty * po.req_price
                        )
                    except Exception:
                        pass
        if n and self.log:
            self.log.info(f"[ExecBridge:{self.market}] pending {n}건 복구")
        return n

    def _save(self):
        try:
            self.registry.save_to(self.pending_path)
        except Exception as e:
            if self.log:
                self.log.error(f"[ExecBridge:{self.market}] pending 저장 실패: {e}")

    # ── 접수 등록 (즉시 apply 금지) ─────────────────────────
    def register_accept(self, order_no, code, name, side, level,
                        req_qty, req_price,
                        using_compound=0.0, is_full=False,
                        extra=None) -> bool:
        """
        주문 접수 성공 시 호출. 주문번호가 없으면 False.
        동일 종목+방향 pending이 이미 존재하면 중복 등록 차단 → False.
        등록만 하고 포지션은 절대 만들지 않는다.
        extra: 전략 메타데이터 dict (breakout_low, reason 등)
        """
        if not order_no:
            if self.log:
                self.log.error(
                    f"[ExecBridge:{self.market}] 주문번호 없음 → pending 등록 금지 {code}"
                )
            return False
        # ── 중복 pending 차단 (동일 종목 + 동일 방향) ──────────────
        if self.registry.has_open(code, side):
            if self.log:
                self.log.warning(
                    f"[ExecBridge:{self.market}] 중복 pending 차단 {side} {code} "
                    f"order_no={order_no} (기존 open 주문 있음)"
                )
            return False
        try:
            self.registry.register(
                order_no, self.market, code, name, side, level,
                int(req_qty), float(req_price),
                is_full=bool(is_full),
                using_compound=float(using_compound or 0),
                extra=extra,
            )
        except Exception as e:
            if self.log:
                self.log.error(
                    f"[ExecBridge:{self.market}] pending 등록 오류 {code}: {e}"
                )
            return False
        self._save()
        if self.log:
            self.log.info(
                f"📝 [ExecBridge:{self.market}] 접수등록 {side} {code} "
                f"{req_qty}주 @{req_price} order_no={order_no} (체결대기)"
            )
        return True

    # ── 중복 주문 방지용 ────────────────────────────────────
    def has_open(self, code, side=None) -> bool:
        return self.registry.has_open(code, side)

    def open_count(self) -> int:
        return len(self.registry.all_open())

    # ── backoff 체크 ────────────────────────────────────────
    def _is_backoff_active(self) -> bool:
        return self.now_fn() < self._next_allowed_poll_ts

    def _record_fail(self):
        self._poll_fail_count += 1
        wait = min(
            _BACKOFF_BASE ** self._poll_fail_count,
            _BACKOFF_MAX
        )
        self._next_allowed_poll_ts = self.now_fn() + wait
        if self.log:
            self.log.warning(
                f"[ExecBridge:{self.market}] poll 실패 "
                f"연속{self._poll_fail_count}회, "
                f"{wait:.0f}초 backoff"
            )

    def _record_success(self):
        self._poll_fail_count = 0
        self._next_allowed_poll_ts = 0.0

    def _apply_recovery_if_needed(self):
        """연속 실패가 임계값 초과 시 open pending → RECOVERY_REQUIRED."""
        if self._poll_fail_count < _MAX_FAIL_BEFORE_RECOVERY:
            return
        for po in self.registry.all_open():
            if self.registry.mark_recovery(po.order_no):
                if self.log:
                    self.log.error(
                        f"[ExecBridge:{self.market}] RECOVERY_REQUIRED "
                        f"{po.side} {po.code} order_no={po.order_no} "
                        f"— 상태 불명, 재시작 후 재확인 필요"
                    )
        self._save()

    # ── 매 tick 체결 폴링 + 반영 ────────────────────────────
    def poll(self, on_buy_fill, on_sell_fill=None,
             on_terminal=None, on_cancel_request=None,
             on_fill_error=None,
             timeout_sec=15):
        """
        FillSource로 실체결 delta를 조회해 콜백으로 반영.
        빈 registry이면 조회 없이 반환. 반환: PollResult | None.

        콜백 시그니처(2가지 모두 지원):
          on_buy_fill(ev)         — 1-arg (AppliedFillEvent)
          on_buy_fill(pending, ev)— 2-arg (PendingOrder | None, AppliedFillEvent)
        on_fill_error(err_tuple)  — fill_errors 기록 통지 (선택)

        오류 정책:
          - 첫 실패: 다음 tick 재시도 (backoff 2초)
          - 연속 실패: 지수형 backoff (최대 120초)
          - _MAX_FAIL_BEFORE_RECOVERY 초과 → RECOVERY_REQUIRED
          - 실패만으로 pending 삭제/포지션 생성 금지

        CRITICAL-2 delta 유실 방지:
          - apply_delta는 poll_orchestrator에서 확정됨 (registry 상태 변경)
          - 콜백 실패 시 (po, ev) 쌍을 _callback_retry_queue에 보관
          - 다음 poll 시 retry_queue 우선 처리 → 콜백 재시도
          - 콜백 성공 후에만 retry_queue에서 제거
          - 이중반영 방지: apply_delta는 1회만 호출됨 (orchestrator에서 처리)
        """
        if not self.registry:
            # registry가 비어도 retry_queue는 처리해야 함
            if not self._callback_retry_queue:
                return None

        # backoff 중이면 retry_queue만 처리
        skip_new_poll = self._is_backoff_active()

        result = None

        # ── 1) retry_queue 우선 처리 (이전 콜백 실패분) ────────
        if self._callback_retry_queue:
            retry_list = list(self._callback_retry_queue)
            self._callback_retry_queue.clear()
            still_failed = []
            for (po, ev) in retry_list:
                if ev.applied_qty <= 0:
                    continue
                try:
                    if ev.side == "BUY":
                        _dispatch(on_buy_fill, po, ev)
                    elif on_sell_fill is not None:
                        _dispatch(on_sell_fill, po, ev)
                    if self.log:
                        self.log.info(
                            f"[ExecBridge:{self.market}] retry 콜백 성공 "
                            f"{ev.side} {ev.code} order_no={ev.order_no} "
                            f"applied_qty={ev.applied_qty}"
                        )
                except Exception as _e:
                    if self.log:
                        self.log.error(
                            f"[ExecBridge:{self.market}] retry 콜백 재실패 "
                            f"{ev.side} {ev.code}: {_e} — 다음 poll 재시도"
                        )
                    still_failed.append((po, ev))
            self._callback_retry_queue.extend(still_failed)
            if still_failed:
                self._save()

        if skip_new_poll:
            return None

        if not self.registry:
            return None

        # ── 2) 신규 poll ─────────────────────────────────────
        try:
            result = poll_pending_fills(
                self.fill_source, self.state_source, self.registry,
                now=self.now_fn(), timeout_sec=timeout_sec,
                logger=self.log
            )
        except Exception as e:
            if self.log:
                self.log.error(
                    f"[ExecBridge:{self.market}] poll 예외: {e}"
                )
            self._record_fail()
            self._apply_recovery_if_needed()
            return None

        # fill_errors가 있어도 부분 성공으로 처리
        if result.fill_errors:
            self._record_fail()
            if on_fill_error:
                for err in result.fill_errors:
                    try:
                        on_fill_error(err)
                    except Exception:
                        pass
        else:
            self._record_success()

        # ── 3) 체결 콜백 — 1-arg / 2-arg 모두 지원 ──────────
        # apply_delta는 poll_orchestrator에서 이미 확정됨.
        # 콜백 실패 시 retry_queue에 보관 → 다음 poll에서 재시도.
        # 이중반영 없음: apply_delta는 1회만 호출됨.
        for ev in result.fills:
            if ev.applied_qty <= 0:
                continue
            # pending 객체 조회 (2-arg 콜백용)
            po = self.registry.get(ev.order_no) if hasattr(self.registry, "get") else None
            try:
                if ev.side == "BUY":
                    _dispatch(on_buy_fill, po, ev)
                elif on_sell_fill is not None:
                    _dispatch(on_sell_fill, po, ev)
            except Exception as e:
                if self.log:
                    self.log.error(
                        f"[ExecBridge:{self.market}] 체결반영 콜백 오류 "
                        f"{ev.side} {ev.code}: {e} "
                        f"— retry_queue 보관 (delta 유실 방지, CRITICAL-2)"
                    )
                # CRITICAL-2: 콜백 실패 → retry_queue 보관 (delta 유실 방지)
                self._callback_retry_queue.append((po, ev))

        # terminal 통지
        if on_terminal:
            for st in result.statuses:
                try:
                    on_terminal(st)
                except Exception:
                    pass

        # 취소 요청 통지
        if on_cancel_request:
            for cr in result.cancel_requests:
                try:
                    on_cancel_request(cr)
                except Exception:
                    pass

        self._save()
        return result

    def snapshot(self) -> dict:
        opens = self.registry.all_open()
        return {
            "market": self.market,
            "open_count": len(opens),
            "open": [{
                "order_no": o.order_no, "code": o.code, "side": o.side,
                "req_qty": o.req_qty, "applied_qty": o.applied_qty,
                "status": o.status
            } for o in opens],
        }
