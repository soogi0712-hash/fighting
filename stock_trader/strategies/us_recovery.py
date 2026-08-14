"""US 손실 회복 트레일링 + 복원 포지션 관리 상태기계 (순수 로직·부수효과 없음).

이 모듈은 **판정만** 한다. 주문 제출·저장·시각 획득 등 부수효과는 호출부가 담당한다.
모든 시각은 인자로 주입(now)해 재시작/재현/테스트에서 결정론적으로 동작한다.
퍼센트는 호출부가 계산한 net_pct(수수료·세금·환전 반영 수익률, 가능한 경우)를 받는다.

management_mode 상태:
  NORMAL            — 일반 관리(수익 트레일링/고정익절/하드손절 진입 감시)
  RECOVERY_TRAILING — net_pct<=-5% 진입 후 손실 회복 트레일링
  EXIT_PENDING      — SELL 제출 후 체결확인 대기(호출부가 설정, 중복제출 금지)
  CLOSED            — 청산 완료(호출부가 설정)

§4 손실 회복 트레일링 규칙(경계 포함):
  진입      : net_pct <= -5.0 최초 → RECOVERY_TRAILING (즉시매도 금지)
  회복고점  : 현재가가 recovery_high_price 상회 시 갱신(절대 하락 불가)
  고점이탈  : (cur-recovery_high)/recovery_high*100 <= -0.7 → SELL_ALL
  하드손절  : net_pct <= -6.0 → SELL_ALL (반등 무관)
  시간청산  : 경과 >= 15분 AND net_pct <= -4.0 → SELL_ALL
  회복종료  : net_pct >= -2.0 → NORMAL 복귀(HOLD)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

# ── 관리 모드 ────────────────────────────────────────────────────
MODE_NORMAL   = "NORMAL"
MODE_RECOVERY = "RECOVERY_TRAILING"
MODE_EXIT     = "EXIT_PENDING"
MODE_CLOSED   = "CLOSED"

# ── §4 손실 회복 트레일링 임계 (모두 net_pct·가격 기준) ──────────
RECOVERY_ENTER_NET      = -5.0    # 진입: net_pct <= -5.0
RECOVERY_HIGH_DROP_PCT  = -0.7    # 고점 대비 가격 하락률 <= -0.7 → SELL_ALL
RECOVERY_HARD_NET       = -6.0    # net_pct <= -6.0 → SELL_ALL
RECOVERY_TIME_SEC       = 15 * 60 # 15분
RECOVERY_TIME_NET       = -4.0    # 15분 경과 + net_pct <= -4.0 → SELL_ALL
RECOVERY_EXIT_NET       = -2.0    # net_pct >= -2.0 → NORMAL 복귀

# ── §3 복원 포지션 수익 트레일링 임계 ────────────────────────────
PROFIT_TRAIL_ACTIVATE_NET = 1.5   # 순수익률 최고점 >= +1.5% → 활성화
PROFIT_TRAIL_DROP_NET     = -1.0  # 활성화 후 최고 net 대비 -1.0%p 하락 → SELL_ALL

# ── 판정 액션 ────────────────────────────────────────────────────
ACT_HOLD     = "HOLD"
ACT_SELL_ALL = "SELL_ALL"


class RecoveryDecision:
    """판정 결과. state 는 '갱신된 상태 dict'(호출부가 그대로 영속 저장)."""
    __slots__ = ("action", "mode", "reason", "state", "sell")

    def __init__(self, action, mode, reason, state):
        self.action = action          # ACT_HOLD / ACT_SELL_ALL
        self.mode   = mode            # 갱신 후 management_mode
        self.reason = reason          # 사람이 읽는 사유(로그/저널)
        self.state  = state           # 갱신된 상태 dict
        self.sell   = (action == ACT_SELL_ALL)

    def __repr__(self):
        return f"<RecoveryDecision {self.action} mode={self.mode} reason={self.reason}>"


def default_state(recovered: bool = False, highest_price: float = 0.0,
                  now: Optional[datetime] = None) -> dict:
    """신규/복원 포지션의 기본 관리 상태. 구버전 JSON 병합용 기본값 제공."""
    return {
        "management_mode":       MODE_NORMAL,
        "recovered":             bool(recovered),
        "highest_price":         float(highest_price or 0.0),
        "recovery_started_at":   None,
        "recovery_high_price":   None,
        "recovery_high_net_pct": None,
        "profit_trail_active":   False,
        "profit_high_net_pct":   None,
        "last_evaluated_at":     (now.isoformat() if now else None),
        "exit_pending_ref":      None,
        # ── 격리(broker-absent quarantine): 완전 KIS 스냅샷에서 broker 부재 판정된
        #    내부 포지션. 삭제하지 않고 감사정보만 유지, 매도·판정·집계에서 제외.
        "quarantined":           False,
        "quarantine":            None,   # {symbol, quarantined_at, reason, snapshot_id}
        # ── 명확 거절(clear-reject) 후 SELL 재제출 쿨다운(ISO). 무한 재시도 방지.
        "sell_cooldown_until":   None,
    }


def merge_state(raw: Optional[dict]) -> dict:
    """구버전 JSON(신규 필드 없음)·부분 dict 를 안전한 기본값으로 병합.

    ★ 기존 값이 있으면 보존한다. highest_price/recovery_* 를 낮추지 않는다.
    """
    base = default_state()
    if not isinstance(raw, dict):
        return base
    out = dict(base)
    for k in base:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    # 타입 보정
    out["recovered"] = bool(out.get("recovered"))
    out["profit_trail_active"] = bool(out.get("profit_trail_active"))
    out["quarantined"] = bool(out.get("quarantined"))
    try:
        out["highest_price"] = float(out.get("highest_price") or 0.0)
    except (TypeError, ValueError):
        out["highest_price"] = 0.0
    if out.get("management_mode") not in (MODE_NORMAL, MODE_RECOVERY, MODE_EXIT, MODE_CLOSED):
        out["management_mode"] = MODE_NORMAL
    return out


def bump_highest_price(state: dict, cur_price: float) -> dict:
    """수익 트레일링용 최고가 갱신 — 절대 낮아지지 않음(반복 복원·재시작 안전)."""
    try:
        cp = float(cur_price or 0.0)
    except (TypeError, ValueError):
        cp = 0.0
    if cp > float(state.get("highest_price") or 0.0):
        state["highest_price"] = cp
    return state


def _elapsed_sec(started_at: Optional[str], now: datetime) -> Optional[float]:
    """recovery_started_at(ISO) → now 경과초. 파싱 실패 시 None(=시간조건 미충족)."""
    if not started_at:
        return None
    try:
        st = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return None
    try:
        return (now - st).total_seconds()
    except (TypeError, ValueError):
        return None


def evaluate(state: dict, net_pct: float, cur_price: float,
             now: datetime) -> RecoveryDecision:
    """관리 상태 + 현재 net_pct/현재가/시각 → 판정.

    호출부 계약:
      - EXIT_PENDING/CLOSED 이면 판정하지 않고 HOLD(중복 SELL 방지). 호출부가 체결
        확인 후 CLOSED/NORMAL 로 전이한다.
      - 반환 state 를 그대로 영속 저장한다(원자적).
      - action==SELL_ALL 이면 호출부가 SELL 제출 후 mode=EXIT_PENDING 로 바꾼다.
    """
    s = dict(state)  # 입력 불변 — 사본 갱신 후 반환
    s["last_evaluated_at"] = now.isoformat()
    mode = s.get("management_mode", MODE_NORMAL)

    # 체결확인 대기/청산완료 — 판정 보류(중복 SELL 금지)
    if mode in (MODE_EXIT, MODE_CLOSED):
        return RecoveryDecision(ACT_HOLD, mode, "exit_pending_or_closed", s)

    # 최고가 갱신(항상, 수익 트레일링용)
    bump_highest_price(s, cur_price)

    # ── NORMAL: 손실 회복 진입 감시 ─────────────────────────────
    if mode == MODE_NORMAL:
        if net_pct is not None and net_pct <= RECOVERY_ENTER_NET:
            # 진입 — 즉시매도 금지, recovery 기준 설정
            s["management_mode"]       = MODE_RECOVERY
            s["recovery_started_at"]   = now.isoformat()
            s["recovery_high_price"]   = float(cur_price or 0.0)
            s["recovery_high_net_pct"] = float(net_pct)
            return RecoveryDecision(ACT_HOLD, MODE_RECOVERY,
                                    f"recovery_enter(net={net_pct:.2f}%)", s)
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL, "normal_hold", s)

    # ── RECOVERY_TRAILING ───────────────────────────────────────
    # 1) 회복 고점 갱신(가격 상회 시). 절대 낮추지 않음. 진입 직후 동일가는 갱신 없음.
    rhp = s.get("recovery_high_price")
    if rhp is None:
        rhp = float(cur_price or 0.0)
        s["recovery_high_price"] = rhp
    if cur_price is not None and float(cur_price) > float(rhp):
        s["recovery_high_price"]   = float(cur_price)
        rhp = float(cur_price)
    # net 고점도 보존(가격 고점과 함께 갱신)
    rhn = s.get("recovery_high_net_pct")
    if net_pct is not None and (rhn is None or net_pct > rhn):
        s["recovery_high_net_pct"] = float(net_pct)

    # 2) 하드손절: net_pct <= -6.0 (반등 무관, 최우선)
    if net_pct is not None and net_pct <= RECOVERY_HARD_NET:
        return RecoveryDecision(ACT_SELL_ALL, MODE_RECOVERY,
                                f"recovery_hard_stop(net={net_pct:.2f}%)", s)

    # 3) 회복 고점 이탈: (cur - rhp)/rhp*100 <= -0.7
    if rhp and rhp > 0 and cur_price is not None:
        drop = (float(cur_price) - float(rhp)) / float(rhp) * 100.0
        if drop <= RECOVERY_HIGH_DROP_PCT:
            return RecoveryDecision(ACT_SELL_ALL, MODE_RECOVERY,
                                    f"recovery_high_drop({drop:.2f}% from high)", s)

    # 4) 시간청산: 경과 >= 15분 AND net_pct <= -4.0
    el = _elapsed_sec(s.get("recovery_started_at"), now)
    if el is not None and el >= RECOVERY_TIME_SEC and \
            net_pct is not None and net_pct <= RECOVERY_TIME_NET:
        return RecoveryDecision(ACT_SELL_ALL, MODE_RECOVERY,
                                f"recovery_time_stop({el/60:.1f}min,net={net_pct:.2f}%)", s)

    # 5) 회복 종료: net_pct >= -2.0 → NORMAL 복귀(HOLD)
    if net_pct is not None and net_pct >= RECOVERY_EXIT_NET:
        s["management_mode"]       = MODE_NORMAL
        s["recovery_started_at"]   = None
        s["recovery_high_price"]   = None
        s["recovery_high_net_pct"] = None
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL,
                                f"recovery_exit_to_normal(net={net_pct:.2f}%)", s)

    # 6) 유지
    return RecoveryDecision(ACT_HOLD, MODE_RECOVERY, "recovery_hold", s)


# ── 통합 판정 어댑터 (§8 우선순위) ──────────────────────────────
ACT_DEFER = "DEFER"   # 관리모드 개입 없음 → 기존 ①~⑩ 로직 사용(신규·NORMAL·net>-5)


def decide_management_action(state: dict, net_pct: float, cur_price: float,
                             now: datetime) -> RecoveryDecision:
    """복원/손실회복 관리가 기존 매도 판정보다 '먼저' 개입할지 결정한다(§8).

    반환 action:
      SELL_ALL — 손실회복/수익트레일링/하드손절 → 즉시 전량매도(호출부가 EXIT_PENDING 설정)
      HOLD     — 관리모드가 HOLD 를 강제(복원 면제/회복 유지) → 기존 ①~⑩ 실행 금지
      DEFER    — 관리 개입 없음 → 호출부가 기존 ①~⑩ 로직 수행

    우선순위:
      0) EXIT_PENDING/CLOSED → HOLD (중복 SELL 금지)
      1) RECOVERY_TRAILING 이거나 net_pct<=-5(진입) → 손실회복 판정이 최우선
         (기존 ⑧⑨ MA추세매도·⑩ -5%즉시손절보다 앞선다)
      2) recovered=True & NORMAL → 고정익절(①②③)·시간청산(⑥)·MA추세매도(⑧⑨) 면제.
         수익 트레일링만 적용(§3): 활성 후 고점 -1.0%p → SELL_ALL, 그 외 HOLD.
      3) 그 외(신규·NORMAL·net>-5) → DEFER(기존 로직).
    """
    mode = state.get("management_mode", MODE_NORMAL)
    if mode in (MODE_EXIT, MODE_CLOSED):
        s = dict(state); s["last_evaluated_at"] = now.isoformat()
        return RecoveryDecision(ACT_HOLD, mode, "exit_pending_or_closed", s)

    # 1) 손실 회복 트레일링(진입 포함) — 최우선
    if mode == MODE_RECOVERY or (net_pct is not None and net_pct <= RECOVERY_ENTER_NET):
        return evaluate(state, net_pct, cur_price, now)

    # 2) 복원 포지션(NORMAL) — 고정익절/시간청산/MA매도 면제, 수익 트레일링만
    if state.get("recovered"):
        s = dict(state)
        s["last_evaluated_at"] = now.isoformat()
        bump_highest_price(s, cur_price)
        pt = evaluate_profit_trailing(s, net_pct)
        s = pt["state"]
        if pt["sell"]:
            return RecoveryDecision(ACT_SELL_ALL, MODE_NORMAL,
                                    "recovered_profit_trailing_exit", s)
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL,
                                "recovered_hold(고정익절·시간청산·MA매도 면제)", s)

    # 3) 신규·NORMAL·net>-5 → 기존 로직에 위임
    s = dict(state); s["last_evaluated_at"] = now.isoformat()
    bump_highest_price(s, cur_price)
    return RecoveryDecision(ACT_DEFER, MODE_NORMAL, "defer_to_existing", s)


def evaluate_profit_trailing(state: dict, net_pct: float) -> dict:
    """§3 복원 포지션 수익 트레일링(상방). 순수 판정 — SELL 여부만 boolean.

    반환: {"activate": bool, "sell": bool, "state": 갱신상태}
      - 최고 net >= +1.5% → profit_trail_active=True
      - 활성화 후 최고 net 대비 -1.0%p 하락 → sell=True
    ※ 손실(하락) 측은 evaluate()의 RECOVERY_TRAILING 이 담당. 이 함수는 상방 전용.
    """
    s = dict(state)
    hi = s.get("profit_high_net_pct")
    if net_pct is not None and (hi is None or net_pct > hi):
        s["profit_high_net_pct"] = float(net_pct)
        hi = float(net_pct)
    activate = False
    if not s.get("profit_trail_active") and hi is not None and hi >= PROFIT_TRAIL_ACTIVATE_NET:
        s["profit_trail_active"] = True
        activate = True
    sell = False
    if s.get("profit_trail_active") and hi is not None and net_pct is not None:
        if (net_pct - hi) <= PROFIT_TRAIL_DROP_NET:
            sell = True
    return {"activate": activate, "sell": sell, "state": s}
