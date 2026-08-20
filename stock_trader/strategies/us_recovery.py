"""US 손실 회복 트레일링 + 수익 트레일링 관리 상태기계 (순수 로직·부수효과 없음).

이 모듈은 **판정만** 한다. 주문 제출·저장·시각/지표 획득은 호출부가 담당한다.
모든 시각/지표/봉 타임스탬프는 인자로 주입해 재시작/재현/테스트에서 결정론적으로
동작한다. 퍼센트는 호출부가 계산한 net_pct(수수료·세금 반영 수익률)를 받는다.

management_mode:
  NORMAL / RECOVERY_TRAILING / EXIT_PENDING / CLOSED

── 종가 확인(봉 중복 방지) ────────────────────────────────────────────
  트레일 이탈 '확정'은 매매루프 호출 횟수가 아니라 **서로 다른 마감 완료 봉**만
  센다. 동일한 봉 타임스탬프(bar_ts)가 다시 들어오면 카운터를 증가시키지 않는다.
  미완성 현재봉(bar_ts=None)은 확인봉으로 쓰지 않는다. 마지막 확인봉 시각과
  카운터는 상태에 영속되어 재시작 후에도 유지된다.

── 수익 트레일링(상방) 규칙 ──────────────────────────────────────────
  활성화 : 순수익률 최고점 >= +1.5% (sticky)
  트레일 : trail_pct = clamp(ATR% × 1.5, 1.0%, 2.5%)
  매도(아래 중 하나):
    · 서로 다른 확정 1분봉 2개에서 트레일 이탈 → EMA9 방향 무관 SELL_ALL
    · 완성된 5분봉 1개가 트레일 아래 마감 → SELL_ALL
    · EMA9 하락 또는 종가<EMA9 → 확정봉 1개만으로 빠른 SELL_ALL
    · 동적 트레일보다 추가 0.5%p 이상 급락 → 봉 확인 없이 즉시 안전 SELL_ALL
  ★ EMA9 상승은 확정매도를 '무기한 막는 veto'가 아니다(2봉 확정 시 매도).

── 손실 회복 트레일링 + 보호 연속성 ──────────────────────────────────
  진입 : net_pct <= -5.0 (즉시매도 금지)
  활성 : net_pct >= -3.5 회복해야 recovery trailing 활성(sticky)
  트레일: recovery high 대비 clamp(ATR% × 1.5, 1.2%, 2.5%) 이탈(확정봉/급락) → SELL_ALL
  하드 : net_pct <= -6.0 → SELL_ALL (반등 없이 최종 손절)
  보호 연속성:
    · net_pct >= -2.0 → '회복 성공' 표시만, recovery high 보호는 유지(계속 RECOVERY)
    · net_pct >= 0.0(손익분기) → NORMAL 전환(보호 해제)
    · 그 사이 다시 하락하면 recovery high 기준 동적 트레일로 계속 보호
    · +1.5% 도달은 0% 통과 후 NORMAL→수익 트레일링으로 자연 전환
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

# ── 관리 모드 ────────────────────────────────────────────────────
MODE_NORMAL   = "NORMAL"
MODE_RECOVERY = "RECOVERY_TRAILING"
MODE_EXIT     = "EXIT_PENDING"
MODE_CLOSED   = "CLOSED"

# ── 손실 회복 트레일링 임계 ──────────────────────────────────────
RECOVERY_ENTER_NET       = -5.0    # 진입: net_pct <= -5.0
RECOVERY_ARM_NET         = -3.5    # 활성: net_pct >= -3.5 까지 회복해야 트레일 활성
RECOVERY_SUCCESS_NET     = -2.0    # 회복 성공 표시(보호는 유지)
RECOVERY_NORMAL_NET      = 0.0     # 손익분기 회복 → NORMAL 전환(보호 해제)
RECOVERY_HARD_NET        = -6.0    # net_pct <= -6.0 → SELL_ALL (반등 없이 최종 손절)
RECOVERY_TRAIL_ATR_MULT  = 1.5
RECOVERY_TRAIL_MIN       = 1.2
RECOVERY_TRAIL_MAX       = 2.5
RECOVERY_TRAIL_PANIC_EXTRA = 0.5   # 동적 폭보다 +0.5%p 급락 → 즉시 안전매도
RECOVERY_TRAIL_CONFIRM_BARS = 1    # 회복 트레일 확정 완료봉 수

# ── 수익 트레일링(상방) 임계 ─────────────────────────────────────
PROFIT_TRAIL_ACTIVATE_NET = 1.5
PROFIT_TRAIL_ATR_MULT     = 1.5
PROFIT_TRAIL_MIN          = 1.0
PROFIT_TRAIL_MAX          = 2.5
PROFIT_TRAIL_PANIC_EXTRA  = 0.5    # 동적 폭보다 +0.5%p 급락 → 즉시 안전매도
PROFIT_TRAIL_CONFIRM_BARS = 2      # 서로 다른 확정 1분봉 2개

# ── 판정 액션 ────────────────────────────────────────────────────
ACT_HOLD     = "HOLD"
ACT_SELL_ALL = "SELL_ALL"
ACT_DEFER    = "DEFER"   # (미사용 — 단일 매도판정 권위. 호환용 상수만 유지)


class RecoveryDecision:
    """판정 결과. state 는 '갱신된 상태 dict'(호출부가 그대로 영속 저장)."""
    __slots__ = ("action", "mode", "reason", "state", "sell")

    def __init__(self, action, mode, reason, state):
        self.action = action
        self.mode   = mode
        self.reason = reason
        self.state  = state
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
        "recovery_trail_armed":  False,   # net>=-3.5 회복 시 True(sticky)
        "recovery_reached_exit": False,   # net>=-2.0 회복 성공 표시(보호 유지)
        # ── 회복 트레일 종가 확인(봉 중복 방지) ──
        "recovery_breach_closes":   0,
        "last_recovery_breach_bar_at": None,
        # ── 수익 트레일링 ──
        "profit_trail_active":   False,
        "profit_high_net_pct":   None,
        # ── 수익 트레일 종가 확인(봉 중복 방지) ──
        "profit_breach_closes":  0,
        "last_profit_breach_bar_at": None,
        "last_evaluated_at":     (now.isoformat() if now else None),
        "exit_pending_ref":      None,
        # ── 격리(broker-absent quarantine) ──
        "quarantined":           False,
        "quarantine":            None,
        # ── 명확 거절 후 SELL 재제출 쿨다운(ISO) ──
        "sell_cooldown_until":   None,
    }


def merge_state(raw: Optional[dict]) -> dict:
    """구버전 JSON·부분 dict 를 안전한 기본값으로 병합. 기존 값 보존, high 하락 금지."""
    base = default_state()
    if not isinstance(raw, dict):
        return base
    out = dict(base)
    for k in base:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    out["recovered"] = bool(out.get("recovered"))
    out["profit_trail_active"] = bool(out.get("profit_trail_active"))
    out["recovery_trail_armed"] = bool(out.get("recovery_trail_armed"))
    out["recovery_reached_exit"] = bool(out.get("recovery_reached_exit"))
    out["quarantined"] = bool(out.get("quarantined"))
    for ck in ("profit_breach_closes", "recovery_breach_closes"):
        try:
            out[ck] = int(out.get(ck) or 0)
        except (TypeError, ValueError):
            out[ck] = 0
    try:
        out["highest_price"] = float(out.get("highest_price") or 0.0)
    except (TypeError, ValueError):
        out["highest_price"] = 0.0
    if out.get("management_mode") not in (MODE_NORMAL, MODE_RECOVERY, MODE_EXIT, MODE_CLOSED):
        out["management_mode"] = MODE_NORMAL
    return out


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def bump_highest_price(state: dict, cur_price: float) -> dict:
    """수익 트레일링용 최고가 갱신 — 절대 낮아지지 않음(반복 복원·재시작 안전)."""
    try:
        cp = float(cur_price or 0.0)
    except (TypeError, ValueError):
        cp = 0.0
    if cp > float(state.get("highest_price") or 0.0):
        state["highest_price"] = cp
    return state


def _count_breach_bar(state: dict, closes_key: str, last_key: str,
                      bar_ts: Optional[str]) -> int:
    """트레일 이탈 확정 카운터. **서로 다른 마감 완료 봉(bar_ts)** 만 센다.
    - bar_ts is None(미완성/미상) → 증가하지 않음(현재값 반환).
    - bar_ts == 마지막 확인봉 → 증가하지 않음(동일 봉 중복 방지).
    - 새 완료봉 → +1, last 갱신.
    """
    cur = int(state.get(closes_key) or 0)
    if not bar_ts:
        return cur
    if state.get(last_key) == bar_ts:
        return cur
    state[last_key] = bar_ts
    state[closes_key] = cur + 1
    return state[closes_key]


def _reset_breach(state: dict, closes_key: str, last_key: str) -> None:
    state[closes_key] = 0
    state[last_key] = None


# ══════════════════════════════════════════════════════════════
# 손실 회복
# ══════════════════════════════════════════════════════════════
def evaluate(state: dict, net_pct: float, cur_price: float, now: datetime,
             atr_pct: float = 0.0, bar1_ts: Optional[str] = None) -> RecoveryDecision:
    """손실 회복 판정. EXIT/CLOSED 이면 HOLD. SELL_ALL 이면 호출부가 EXIT_PENDING 설정."""
    s = dict(state)
    s["last_evaluated_at"] = now.isoformat()
    mode = s.get("management_mode", MODE_NORMAL)

    if mode in (MODE_EXIT, MODE_CLOSED):
        return RecoveryDecision(ACT_HOLD, mode, "exit_pending_or_closed", s)

    bump_highest_price(s, cur_price)

    # ── NORMAL: 진입 감시 ──
    if mode == MODE_NORMAL:
        if net_pct is not None and net_pct <= RECOVERY_ENTER_NET:
            s["management_mode"]        = MODE_RECOVERY
            s["recovery_started_at"]    = now.isoformat()
            s["recovery_high_price"]    = float(cur_price or 0.0)
            s["recovery_high_net_pct"]  = float(net_pct)
            s["recovery_trail_armed"]   = False
            s["recovery_reached_exit"]  = False
            _reset_breach(s, "recovery_breach_closes", "last_recovery_breach_bar_at")
            return RecoveryDecision(ACT_HOLD, MODE_RECOVERY,
                                    f"recovery_enter(net={net_pct:.2f}%)", s)
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL, "normal_hold", s)

    # ── RECOVERY_TRAILING ──
    rhp = s.get("recovery_high_price")
    if rhp is None:
        rhp = float(cur_price or 0.0)
        s["recovery_high_price"] = rhp
    if cur_price is not None and float(cur_price) > float(rhp):
        s["recovery_high_price"] = float(cur_price)
        rhp = float(cur_price)
    rhn = s.get("recovery_high_net_pct")
    if net_pct is not None and (rhn is None or net_pct > rhn):
        s["recovery_high_net_pct"] = float(net_pct)

    # 하드손절(최우선): 반등 없이 -6.0
    if net_pct is not None and net_pct <= RECOVERY_HARD_NET:
        return RecoveryDecision(ACT_SELL_ALL, MODE_RECOVERY,
                                f"recovery_hard_stop(net={net_pct:.2f}%)", s)

    # 손익분기(0%) 회복 → NORMAL 전환(보호 해제)
    if net_pct is not None and net_pct >= RECOVERY_NORMAL_NET:
        s["management_mode"]        = MODE_NORMAL
        s["recovery_started_at"]    = None
        s["recovery_high_price"]    = None
        s["recovery_high_net_pct"]  = None
        s["recovery_trail_armed"]   = False
        s["recovery_reached_exit"]  = False
        _reset_breach(s, "recovery_breach_closes", "last_recovery_breach_bar_at")
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL,
                                f"recovery_exit_to_normal(net={net_pct:.2f}%>=0)", s)

    # -2.0 회복 성공 표시(보호는 유지, 계속 RECOVERY)
    if net_pct is not None and net_pct >= RECOVERY_SUCCESS_NET:
        s["recovery_reached_exit"] = True

    # -3.5 회복 → 트레일 활성(sticky)
    if not s.get("recovery_trail_armed") and net_pct is not None \
            and net_pct >= RECOVERY_ARM_NET:
        s["recovery_trail_armed"] = True

    # 활성 시: recovery high 대비 동적 트레일(확정봉/급락) → SELL_ALL
    if s.get("recovery_trail_armed") and rhp and rhp > 0 and cur_price is not None:
        trail_pct = _clamp(float(atr_pct or 0.0) * RECOVERY_TRAIL_ATR_MULT,
                           RECOVERY_TRAIL_MIN, RECOVERY_TRAIL_MAX)
        drop = (float(cur_price) - float(rhp)) / float(rhp) * 100.0
        if drop <= -trail_pct:
            # 급락(추가 0.5%p) → 봉 확인 없이 즉시 안전매도
            if drop <= -(trail_pct + RECOVERY_TRAIL_PANIC_EXTRA):
                return RecoveryDecision(
                    ACT_SELL_ALL, MODE_RECOVERY,
                    f"recovery_trail_panic(drop={drop:.2f}%<=-{trail_pct + RECOVERY_TRAIL_PANIC_EXTRA:.2f}%)", s)
            closes = _count_breach_bar(s, "recovery_breach_closes",
                                       "last_recovery_breach_bar_at", bar1_ts)
            if closes >= RECOVERY_TRAIL_CONFIRM_BARS:
                return RecoveryDecision(
                    ACT_SELL_ALL, MODE_RECOVERY,
                    f"recovery_trail_exit(drop={drop:.2f}%<=-{trail_pct:.2f}%,closes={closes})", s)
            return RecoveryDecision(ACT_HOLD, MODE_RECOVERY,
                                    f"recovery_trail_pending(closes={closes})", s)
        else:
            _reset_breach(s, "recovery_breach_closes", "last_recovery_breach_bar_at")

    return RecoveryDecision(ACT_HOLD, MODE_RECOVERY, "recovery_hold", s)


# ══════════════════════════════════════════════════════════════
# 수익 트레일링(상방)
# ══════════════════════════════════════════════════════════════
def evaluate_profit_trailing(state: dict, net_pct: float, cur_price: float,
                             atr_pct: float = 0.0, ema9: Optional[float] = None,
                             ema9_rising: bool = False,
                             bar1_ts: Optional[str] = None,
                             bar5_ts: Optional[str] = None) -> dict:
    """수익 트레일링 순수 판정. 반환: {"activate","sell","state","reason"}.

    bar1_ts : 현재 '완료된' 1분봉 타임스탬프(미완성/미상이면 None → 확인봉 아님).
    bar5_ts : 트레일 아래 마감한 '완료된' 5분봉 타임스탬프(있으면 즉시 확정).
    EMA9 상승은 veto 가 아니다(2봉 확정 시 매도). EMA9 하락/종가<EMA9 는 1봉 가속.
    """
    s = dict(state)
    hi = s.get("profit_high_net_pct")
    if net_pct is not None and (hi is None or net_pct > hi):
        s["profit_high_net_pct"] = float(net_pct)
        hi = float(net_pct)

    activate = False
    if not s.get("profit_trail_active") and hi is not None \
            and hi >= PROFIT_TRAIL_ACTIVATE_NET:
        s["profit_trail_active"] = True
        activate = True

    if not s.get("profit_trail_active"):
        _reset_breach(s, "profit_breach_closes", "last_profit_breach_bar_at")
        return {"activate": activate, "sell": False, "state": s,
                "reason": "profit_trail_inactive"}

    trail_pct = _clamp(float(atr_pct or 0.0) * PROFIT_TRAIL_ATR_MULT,
                       PROFIT_TRAIL_MIN, PROFIT_TRAIL_MAX)
    highest = float(s.get("highest_price") or 0.0)
    drop = (((float(cur_price) - highest) / highest * 100.0)
            if (highest > 0 and cur_price is not None) else 0.0)

    if drop > -trail_pct:
        # 이탈 아님 → 카운터 리셋(단일 순간 이탈 무시)
        _reset_breach(s, "profit_breach_closes", "last_profit_breach_bar_at")
        return {"activate": activate, "sell": False, "state": s,
                "reason": "profit_trail_hold"}

    # 급락(추가 0.5%p) → 봉 확인 없이 즉시 안전매도
    if drop <= -(trail_pct + PROFIT_TRAIL_PANIC_EXTRA):
        return {"activate": activate, "sell": True, "state": s,
                "reason": (f"profit_trail_panic(drop={drop:.2f}%<="
                           f"-{trail_pct + PROFIT_TRAIL_PANIC_EXTRA:.2f}%)")}

    # 완성된 5분봉이 트레일 아래 마감 → 즉시 확정
    if bar5_ts:
        return {"activate": activate, "sell": True, "state": s,
                "reason": f"profit_trail_bar5_close(drop={drop:.2f}%<=-{trail_pct:.2f}%)"}

    # 서로 다른 완료 1분봉 카운트(봉 중복 방지)
    closes = _count_breach_bar(s, "profit_breach_closes",
                               "last_profit_breach_bar_at", bar1_ts)
    ema9_down = (ema9 is not None and cur_price is not None
                 and float(cur_price) < float(ema9))
    fast = (not ema9_rising) or ema9_down   # EMA9 하락/종가<EMA9 → 1봉 가속

    if fast and closes >= 1:
        return {"activate": activate, "sell": True, "state": s,
                "reason": (f"profit_trail_fast(drop={drop:.2f}%,ema9_down={ema9_down},"
                           f"rising={ema9_rising},closes={closes})")}
    if closes >= PROFIT_TRAIL_CONFIRM_BARS:
        return {"activate": activate, "sell": True, "state": s,
                "reason": f"profit_trail_confirm(drop={drop:.2f}%,closes={closes}>=2)"}

    return {"activate": activate, "sell": False, "state": s,
            "reason": f"profit_trail_pending(drop={drop:.2f}%,closes={closes})"}


# ══════════════════════════════════════════════════════════════
# 통합 판정 — US 관리상태의 '단일 매도판정 권위' (DEFER 없음)
# ══════════════════════════════════════════════════════════════
def decide_management_action(state: dict, net_pct: float, cur_price: float,
                             now: datetime, ctx: Optional[dict] = None) -> RecoveryDecision:
    """NORMAL 최종 판정:
      EXIT_PENDING → HOLD / net<=-5 → RECOVERY / profit trail 활성 → 동적 트레일 /
      그 외 → HOLD. **DEFER(기존 ①~⑩ 위임) 없음** — 단일 매도판정 권위.

    ctx(선택): {atr_pct, ema9, ema9_rising, bar1_ts, bar5_ts}. 지표값은 동적 트레일의
    조기확정 보조정보로만 쓰인다(독립 매도 트리거 아님).
    """
    ctx = ctx or {}
    atr_pct     = float(ctx.get("atr_pct") or 0.0)
    ema9        = ctx.get("ema9")
    ema9_rising = bool(ctx.get("ema9_rising"))
    bar1_ts     = ctx.get("bar1_ts")
    bar5_ts     = ctx.get("bar5_ts")

    mode = state.get("management_mode", MODE_NORMAL)
    if mode in (MODE_EXIT, MODE_CLOSED):
        s = dict(state); s["last_evaluated_at"] = now.isoformat()
        return RecoveryDecision(ACT_HOLD, mode, "exit_pending_or_closed", s)

    # 1) 손실 회복(진입 포함) — 최우선
    if mode == MODE_RECOVERY or (net_pct is not None and net_pct <= RECOVERY_ENTER_NET):
        return evaluate(state, net_pct, cur_price, now, atr_pct=atr_pct, bar1_ts=bar1_ts)

    # 2) 수익 트레일링 — 활성 시 모든 NORMAL 포지션(복원/신규) 지배
    s = dict(state); s["last_evaluated_at"] = now.isoformat()
    bump_highest_price(s, cur_price)
    pt = evaluate_profit_trailing(s, net_pct, cur_price, atr_pct=atr_pct, ema9=ema9,
                                  ema9_rising=ema9_rising, bar1_ts=bar1_ts, bar5_ts=bar5_ts)
    s = pt["state"]
    if pt["sell"]:
        return RecoveryDecision(ACT_SELL_ALL, MODE_NORMAL,
                                f"profit_trailing_exit({pt['reason']})", s)
    if s.get("profit_trail_active"):
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL,
                                f"profit_trailing_hold({pt['reason']})", s)

    # 3) 복원 포지션(트레일 미활성) → 고정익절/시간/추세 매도 면제(HOLD)
    if state.get("recovered"):
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL,
                                "recovered_hold(고정익절·시간청산·MA매도 면제)", s)

    # 4) 신규·NORMAL·트레일 미활성 → HOLD (단일 권위, 기존 ①~⑩ 위임 금지)
    return RecoveryDecision(ACT_HOLD, MODE_NORMAL, "normal_hold(single_sell_authority)", s)
