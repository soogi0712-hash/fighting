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

CRITICAL-1 retry_queue 영속화:
  - 콜백 실패 delta는 retry_queue.json 에 영속화.
  - 프로세스 종료 후 재시작 시 restore()에서 자동 복구.
  - 이중반영 방지: apply_delta는 1회만 호출(orchestrator), 콜백만 재시도.

CRITICAL-2 RECOVERY_REQUIRED 거래 차단:
  - mark_recovery 호출 → bridge.is_blocked = True
  - register_accept() / poll() 모두 is_blocked == True면 즉시 거부.
  - clear_blocked() 명시적 호출로만 해제 가능.
"""
from __future__ import annotations

import json
import os
import time as _time

from engine.pending_orders import PendingRegistry
from engine.poll_orchestrator import (
    poll_pending_fills, NoOrderStateSource, AppliedFillEvent,
)

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


def _ev_to_dict(ev: AppliedFillEvent) -> dict:
    """AppliedFillEvent → JSON 직렬화 dict."""
    return {
        "order_no":      ev.order_no,
        "market":        ev.market,
        "code":          ev.code,
        "name":          ev.name,
        "side":          ev.side,
        "level":         ev.level,
        "applied_qty":   ev.applied_qty,
        "price":         ev.price,
        "remaining_qty": ev.remaining_qty,
        "using_compound": ev.using_compound,
        "is_full":       ev.is_full,
        "became_filled": ev.became_filled,
    }


def _ev_from_dict(d: dict) -> AppliedFillEvent:
    """dict → AppliedFillEvent 복원."""
    return AppliedFillEvent(
        order_no=d["order_no"],
        market=d["market"],
        code=d["code"],
        name=d["name"],
        side=d["side"],
        level=d.get("level", 0),
        applied_qty=int(d.get("applied_qty", 0)),
        price=float(d.get("price", 0.0)),
        remaining_qty=int(d.get("remaining_qty", 0)),
        using_compound=float(d.get("using_compound", 0.0)),
        is_full=bool(d.get("is_full", False)),
        became_filled=bool(d.get("became_filled", False)),
    )


class ExecutionBridge:
    """
    한 시장의 미확정 주문 상태 + 체결 반영 배선.

    콜백:
      on_buy_fill(ev)   : 실제 매수 체결 delta 반영(포지션 생성/증가)
      on_sell_fill(ev)  : 실제 매도 체결 delta 반영(포지션 차감/청산)
      on_terminal(st)   : (선택) 취소/거부 확정 통지
      on_cancel_request(cr): (선택) timeout → 취소 요청 통지
    ev 는 AppliedFillEvent(applied_qty=실제 delta, price=체결가, became_filled 등).

    CRITICAL-2: is_blocked 속성
      - RECOVERY_REQUIRED 발생 시 is_blocked = True
      - register_accept() / poll() 즉시 거부 (신규 BUY/SELL 차단)
      - clear_blocked() 명시적 호출로만 해제
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

        # CRITICAL-2: RECOVERY_REQUIRED 시 거래 차단 게이트
        self.is_blocked: bool = False      # True이면 신규 BUY/SELL 차단

        # CRITICAL-1: 콜백 실패 delta 영속화 retry_queue
        # list of (po_dict | None, ev_dict)
        # pending.json 경로와 동일 디렉토리에 {stem}.retry.json 으로 저장
        self._callback_retry_queue: list = []   # 메모리 : list of (po, ev)
        self._retry_path: str = self._make_retry_path(pending_path)

    # ── retry_queue 영속화 경로 ─────────────────────────────
    @staticmethod
    def _make_retry_path(pending_path: str) -> str:
        """pending_path 와 같은 디렉토리, stem.retry.json."""
        base = os.path.splitext(pending_path)[0]
        return f"{base}.retry.json"

    def _save_retry_queue(self):
        """retry_queue를 JSON으로 atomic 저장."""
        if not self._callback_retry_queue:
            # 빈 queue → 파일 삭제(정리)
            try:
                if os.path.exists(self._retry_path):
                    os.remove(self._retry_path)
            except OSError:
                pass
            return
        data = []
        for (po, ev) in self._callback_retry_queue:
            po_dict = None
            if po is not None:
                try:
                    import dataclasses
                    po_dict = dataclasses.asdict(po)
                except Exception:
                    try:
                        po_dict = vars(po).copy()
                    except Exception:
                        po_dict = None
            data.append({"po": po_dict, "ev": _ev_to_dict(ev)})
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._retry_path)),
                        exist_ok=True)
            tmp = f"{self._retry_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._retry_path)   # atomic
        except Exception as e:
            if self.log:
                self.log.error(
                    f"[ExecBridge:{self.market}] retry_queue 저장 실패: {e}"
                )

    def _load_retry_queue(self):
        """retry_queue.json → 메모리 복원."""
        if not os.path.exists(self._retry_path):
            return 0
        try:
            with open(self._retry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            if self.log:
                self.log.warning(
                    f"[ExecBridge:{self.market}] retry_queue 파일 손상(무시): {e}"
                )
            return 0
        if not isinstance(data, list):
            return 0
        n = 0
        from engine.pending_orders import PendingOrder
        for item in data:
            try:
                ev = _ev_from_dict(item["ev"])
                po_dict = item.get("po")
                if po_dict:
                    # extra 하위호환
                    if "extra" not in po_dict:
                        po_dict["extra"] = {}
                    po = PendingOrder(**po_dict)
                else:
                    po = None
                self._callback_retry_queue.append((po, ev))
                n += 1
            except Exception:
                continue
        if n and self.log:
            self.log.warning(
                f"[ExecBridge:{self.market}] retry_queue {n}건 복원 "
                f"(콜백 실패 delta — 다음 poll에서 재시도)"
            )
        return n

    # ── 재시작 복구 ─────────────────────────────────────────
    def restore(self) -> int:
        """
        재시작 시 pending 파일 + retry_queue 복구 + FillSource tracker seed.

        CRITICAL-1: retry_queue.json 복원
          - 프로세스 종료 전 저장된 콜백 실패 delta를 복원
          - 다음 poll() 시 자동 재시도
          - tracker seed 완료 후 같은 누적 재방출 안 됨 → 이중반영 없음

        CRITICAL-2: 손상 JSON → is_blocked = True (거래 차단)
        """
        # pending 복원
        try:
            n = self.registry.load_from(self.pending_path)
        except RuntimeError as e:
            # 손상 JSON → RECOVERY_REQUIRED → 거래 차단
            self.is_blocked = True
            if self.log:
                self.log.error(
                    f"[ExecBridge:{self.market}] RECOVERY_REQUIRED: "
                    f"pending 파일 손상 → 거래 차단(is_blocked=True) — {e}"
                )
            return 0
        except Exception as e:
            if self.log:
                self.log.error(f"[ExecBridge:{self.market}] pending 복구 실패: {e}")
            return 0

        # FillSource tracker seed (재시작 후 이중반영 방지)
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

        # CRITICAL-1: retry_queue 복원
        self._load_retry_queue()

        if n and self.log:
            self.log.info(f"[ExecBridge:{self.market}] pending {n}건 복구")
        return n

    def _save(self):
        """pending.json + retry_queue.json 동시 저장."""
        try:
            self.registry.save_to(self.pending_path)
        except Exception as e:
            if self.log:
                self.log.error(f"[ExecBridge:{self.market}] pending 저장 실패: {e}")
        # retry_queue도 항상 함께 저장
        self._save_retry_queue()

    # ── CRITICAL-2: 거래 차단 해제 ─────────────────────────
    def clear_blocked(self):
        """
        RECOVERY_REQUIRED 상태 해제.
        운영자가 수동 확인 후 명시적 호출로만 해제 가능.
        """
        self.is_blocked = False
        if self.log:
            self.log.warning(
                f"[ExecBridge:{self.market}] 거래 차단 해제 — clear_blocked() 호출됨"
            )

    def _check_blocked(self, operation: str) -> bool:
        """차단 상태이면 로그 후 True 반환 (→ 호출자가 즉시 반환)."""
        if self.is_blocked:
            if self.log:
                self.log.error(
                    f"[ExecBridge:{self.market}] 거래 차단(RECOVERY_REQUIRED) "
                    f"— {operation} 거부. clear_blocked() 필요."
                )
            return True
        return False

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

        CRITICAL-2: is_blocked == True이면 즉시 False 반환 (거래 차단).
        """
        # CRITICAL-2: 거래 차단 체크
        if self._check_blocked(f"register_accept({side} {code})"):
            return False

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
        """
        연속 실패가 임계값 초과 시 open pending → RECOVERY_REQUIRED.
        CRITICAL-2: is_blocked = True → 이후 신규 BUY/SELL 차단.
        """
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
        # CRITICAL-2: 거래 차단
        if not self.is_blocked:
            self.is_blocked = True
            if self.log:
                self.log.error(
                    f"[ExecBridge:{self.market}] 거래 차단(is_blocked=True) "
                    f"— RECOVERY_REQUIRED 발생. clear_blocked() 후 재개 가능."
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
          - _MAX_FAIL_BEFORE_RECOVERY 초과 → RECOVERY_REQUIRED + is_blocked=True
          - 실패만으로 pending 삭제/포지션 생성 금지

        CRITICAL-1 delta 유실 방지 (영속화):
          - apply_delta는 poll_orchestrator에서 확정됨 (registry 상태 변경)
          - 콜백 실패 시 (po, ev) → _callback_retry_queue + retry_queue.json 저장
          - 다음 poll 시 retry_queue 우선 처리 → 콜백 재시도
          - 재시작 시 restore()에서 retry_queue.json 복원 → 자동 재시도
          - 이중반영 방지: apply_delta는 1회만 호출

        CRITICAL-2:
          - is_blocked == True이면 신규 poll skip (retry_queue는 계속 처리)
        """
        # retry_queue는 blocked 상태에서도 처리 (이미 확정된 delta)
        # 단, 신규 poll은 blocked 시 skip
        has_retry = bool(self._callback_retry_queue)

        if not self.registry and not has_retry:
            return None

        # backoff 중이면 retry_queue만 처리
        skip_new_poll = self._is_backoff_active() or self.is_blocked

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
            # retry 결과 즉시 영속화
            self._save_retry_queue()

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
            self._apply_recovery_if_needed()   # fill_errors 연속 → RECOVERY_REQUIRED
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
        # 콜백 실패 시 retry_queue에 보관 + 영속화 → 재시작 후에도 복구.
        # 이중반영 없음: apply_delta는 1회만 호출됨.
        cb_failed_this_round = False
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
                        f"— retry_queue 영속화 (CRITICAL-1 delta 유실 방지)"
                    )
                # CRITICAL-1: 콜백 실패 → retry_queue 보관 + 영속화
                self._callback_retry_queue.append((po, ev))
                cb_failed_this_round = True

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

        # pending.json + retry_queue.json 동시 저장
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
            "is_blocked": self.is_blocked,
            "retry_queue_len": len(self._callback_retry_queue),
        }
