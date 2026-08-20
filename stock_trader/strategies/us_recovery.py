"""US 손실 회복 트레일링 + 수익 트레일링 관리 상태기계 (순수 로직·부수효과 없음).

이 모듈은 **판정만** 한다. 주문 제출·저장·시각 획득·지표 계산 등 부수효과·입력은
호출부가 담당한다. 모든 시각/지표는 인자로 주입해 재시작/재현/테스트에서 결정론적으로
동작한다. 퍼센트는 호출부가 계산한 net_pct(수수료·세금 반영 수익률)를 받는다.

management_mode 상태:
  NORMAL            — 일반 관리(수익 트레일링/하드손절 진입 감시)
  RECOVERY_TRAILING — net_pct<=-5% 진입 후 손실 회복 트레일링
  EXIT_PENDING      — SELL 제출 후 체결확인 대기(호출부가 설정, 중복제출 금지)
  CLOSED            — 청산 완료(호출부가 설정)

── 수익 트레일링(상방) 규칙 ──────────────────────────────────────────
  활성화     : 순수익률 최고점(profit_high_net_pct) >= +1.5%
  트레일 폭  : trail_pct = clamp(ATR% × 1.5, 1.0%, 2.5%)  (변동성 동적)
  종가 확정  : 단일 틱 이탈로 매도하지 않는다. 트레일 이탈이 1분봉 종가 2회
               연속(또는 5분봉 종가) 확인될 때만 SELL.
  강한 상승  : EMA9 상승 AND 현재가 > EMA9 → 항상 HOLD(트레일 무시).
  매도 조건  : 트레일 이탈 확정 AND EMA9 하향 이탈(현재가 < EMA9) 동시 충족 → SELL_ALL.
  최고가     : 단조 증가(하락 금지), 재시작 후에도 유지.
  ※ +2.0/+2.5 고정익절은 이 트레일보다 먼저 실행되지 않도록 호출부에서 비활성화한다.

── 손실 회복 트레일링 규칙 ────────────────────────────────────────────
  진입       : net_pct <= -5.0 최초 → RECOVERY_TRAILING (즉시매도 금지)
  ★ 진입 직후 '고점 대비 -0.7% 즉시 매도' 규칙은 삭제되었다.
  활성 조건  : 최소 net_pct >= -3.5% 까지 회복해야 recovery trailing 활성(작은 반등
               만으로는 활성화되지 않는다). 활성 상태는 sticky(한번 활성이면 유지).
  트레일 폭  : trail_pct = clamp(ATR% × 1.5, 1.2%, 2.5%)  (최소 1.2%)
  매도 조건  : 활성 후 recovery high 대비 하락률 <= -trail_pct → SELL_ALL.
  하드손절   : net_pct <= -6.0 → SELL_ALL (반등 없이 최종 손절, 최우선).
  회복종료   : net_pct >= -2.0 → NORMAL 복귀(HOLD).
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
RECOVERY_HARD_NET        = -6.0    # net_pct <= -6.0 → SELL_ALL (반등 없이 최종 손절)
RECOVERY_EXIT_NET        = -2.0    # net_pct >= -2.0 → NORMAL 복귀
RECOVERY_TRAIL_ATR_MULT  = 1.5     # 회복 트레일 폭 = ATR% × 1.5
RECOVERY_TRAIL_MIN       = 1.2     # 회복 트레일 최소 폭(%)
RECOVERY_TRAIL_MAX       = 2.5     # 회복 트레일 최대 폭(%)

# ── 수익 트레일링(상방) 임계 ─────────────────────────────────────
PROFIT_TRAIL_ACTIVATE_NET = 1.5    # 순수익률 최고점 >= +1.5% → 활성화
PROFIT_TRAIL_ATR_MULT     = 1.5    # 수익 트레일 폭 = ATR% × 1.5
PROFIT_TRAIL_MIN          = 1.0    # 수익 트레일 최소 폭(%)
PROFIT_TRAIL_MAX          = 2.5    # 수익 트레일 최대 폭(%)
PROFIT_TRAIL_CONFIRM_CLOSES = 2    # 트레일 이탈 확정에 필요한 연속 종가 수(1분봉 2회)

# ── 판정 액션 ────────────────────────────────────────────────────
ACT_HOLD     = "HOLD"
ACT_SELL_ALL = "SELL_ALL"
ACT_DEFER    = "DEFER"   # 관리 개입 없음 → 호출부 기존 ①~⑩ 로직 사용


class RecoveryDecision:
    """판정 결과. state 는 '갱신된 상태 dict'(호출부가 그대로 영속 저장)."""
    __slots__ = ("action", "mode", "reason", "state", "sell")

    def __init__(self, action, mode, reason, state):
        self.action = action          # ACT_HOLD / ACT_SELL_ALL / ACT_DEFER
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
        "recovery_trail_armed":  False,   # net>=-3.5 회복 시 True(sticky)
        "profit_trail_active":   False,
        "profit_high_net_pct":   None,
        "profit_breach_closes":  0,       # 트레일 이탈 연속 종가 카운터(종가 확정용)
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
    out["recovery_trail_armed"] = bool(out.get("recovery_trail_armed"))
    out["quarantined"] = bool(out.get("quarantined"))
    try:
        out["profit_breach_closes"] = int(out.get("profit_breach_closes") or 0)
    except (TypeError, ValueError):
        out["profit_breach_closes"] = 0
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


def evaluate(state: dict, net_pct: float, cur_price: float,
             now: datetime, atr_pct: float = 0.0) -> RecoveryDecision:
    """손실 회복 판정. 호출부 계약:
      - EXIT_PENDING/CLOSED 이면 HOLD(중복 SELL 방지).
      - 반환 state 를 그대로 영속 저장한다(원자적).
      - action==SELL_ALL 이면 호출부가 SELL 제출 후 mode=EXIT_PENDING 로 바꾼다.
    """
    s = dict(state)
    s["last_evaluated_at"] = now.isoformat()
    mode = s.get("management_mode", MODE_NORMAL)

    if mode in (MODE_EXIT, MODE_CLOSED):
        return RecoveryDecision(ACT_HOLD, mode, "exit_pending_or_closed", s)

    bump_highest_price(s, cur_price)

    # ── NORMAL: 손실 회복 진입 감시 ─────────────────────────────
    if mode == MODE_NORMAL:
        if net_pct is not None and net_pct <= RECOVERY_ENTER_NET:
            s["management_mode"]       = MODE_RECOVERY
            s["recovery_started_at"]   = now.isoformat()
            s["recovery_high_price"]   = float(cur_price or 0.0)
            s["recovery_high_net_pct"] = float(net_pct)
            s["recovery_trail_armed"]  = False   # 아직 -3.5% 회복 전 → 비활성
            return RecoveryDecision(ACT_HOLD, MODE_RECOVERY,
                                    f"recovery_enter(net={net_pct:.2f}%)", s)
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL, "normal_hold", s)

    # ── RECOVERY_TRAILING ───────────────────────────────────────
    # 1) 회복 고점(가격/net) 갱신 — 절대 하락 금지
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

    # 2) 하드손절: net_pct <= -6.0 (반등 없이 최종 손절, 최우선)
    if net_pct is not None and net_pct <= RECOVERY_HARD_NET:
        return RecoveryDecision(ACT_SELL_ALL, MODE_RECOVERY,
                                f"recovery_hard_stop(net={net_pct:.2f}%)", s)

    # 3) 회복 종료: net_pct >= -2.0 → NORMAL 복귀(HOLD)
    if net_pct is not None and net_pct >= RECOVERY_EXIT_NET:
        s["management_mode"]       = MODE_NORMAL
        s["recovery_started_at"]   = None
        s["recovery_high_price"]   = None
        s["recovery_high_net_pct"] = None
        s["recovery_trail_armed"]  = False
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL,
                                f"recovery_exit_to_normal(net={net_pct:.2f}%)", s)

    # 4) 트레일 활성화: net_pct >= -3.5 까지 회복해야 활성(sticky)
    if not s.get("recovery_trail_armed") and net_pct is not None \
            and net_pct >= RECOVERY_ARM_NET:
        s["recovery_trail_armed"] = True

    # 5) 활성 시: recovery high 대비 동적 트레일 폭 이탈 → SELL_ALL
    if s.get("recovery_trail_armed") and rhp and rhp > 0 and cur_price is not None:
        trail_pct = _clamp(float(atr_pct or 0.0) * RECOVERY_TRAIL_ATR_MULT,
                           RECOVERY_TRAIL_MIN, RECOVERY_TRAIL_MAX)
        drop = (float(cur_price) - float(rhp)) / float(rhp) * 100.0
        if drop <= -trail_pct:
            return RecoveryDecision(
                ACT_SELL_ALL, MODE_RECOVERY,
                f"recovery_trail_exit(drop={drop:.2f}%<=-{trail_pct:.2f}%, armed)", s)

    # 6) 유지
    return RecoveryDecision(ACT_HOLD, MODE_RECOVERY, "recovery_hold", s)


def evaluate_profit_trailing(state: dict, net_pct: float, cur_price: float,
                             atr_pct: float = 0.0, ema9: Optional[float] = None,
                             ema9_rising: bool = False,
                             bar5_close: bool = False) -> dict:
    """수익 트레일링(상방) 순수 판정. 반환: {"activate","sell","state","reason"}.

    · 최고 net >= +1.5% → profit_trail_active=True (활성화, sticky)
    · trail_pct = clamp(ATR% × 1.5, 1.0%, 2.5%)
    · EMA9 상승 AND 현재가 > EMA9 → 무조건 HOLD(강한 상승, 카운터 리셋)
    · 트레일 이탈(고점 대비 <= -trail_pct) AND EMA9 하향 이탈(현재가 < EMA9) 동시:
        연속 종가 카운터 증가 → 2회(또는 bar5_close) 확정 시 sell=True.
      그 외에는 카운터 리셋(단일 순간 이탈로 매도하지 않음).
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
        s["profit_breach_closes"] = 0
        return {"activate": activate, "sell": False, "state": s,
                "reason": "profit_trail_inactive"}

    # 강한 상승(EMA9 상승 + 현재가 EMA9 위) → 항상 HOLD
    if ema9 is not None and ema9_rising and cur_price is not None \
            and float(cur_price) > float(ema9):
        s["profit_breach_closes"] = 0
        return {"activate": activate, "sell": False, "state": s,
                "reason": "ema9_uptrend_hold"}

    trail_pct = _clamp(float(atr_pct or 0.0) * PROFIT_TRAIL_ATR_MULT,
                       PROFIT_TRAIL_MIN, PROFIT_TRAIL_MAX)
    highest = float(s.get("highest_price") or 0.0)
    drop = (((float(cur_price) - highest) / highest * 100.0)
            if (highest > 0 and cur_price is not None) else 0.0)
    trail_breached = drop <= -trail_pct
    ema9_down = (ema9 is not None and cur_price is not None
                 and float(cur_price) < float(ema9))

    if trail_breached and ema9_down:
        s["profit_breach_closes"] = int(s.get("profit_breach_closes") or 0) + 1
        confirmed = (s["profit_breach_closes"] >= PROFIT_TRAIL_CONFIRM_CLOSES
                     or bool(bar5_close))
        return {"activate": activate, "sell": confirmed, "state": s,
                "reason": (f"profit_trail_breach(drop={drop:.2f}%<=-{trail_pct:.2f}%,"
                           f"closes={s['profit_breach_closes']},ema9_down,"
                           f"confirmed={confirmed})")}

    # 이탈 미충족(또는 EMA9 지지) → 카운터 리셋(단일 순간 이탈 무시)
    s["profit_breach_closes"] = 0
    return {"activate": activate, "sell": False, "state": s,
            "reason": "profit_trail_hold"}


def decide_management_action(state: dict, net_pct: float, cur_price: float,
                             now: datetime, ctx: Optional[dict] = None) -> RecoveryDecision:
    """관리(손실회복/수익트레일링)가 기존 매도 판정보다 '먼저' 개입할지 결정한다.

    ctx(지표 주입, 선택): {atr_pct, ema9, ema9_rising, bar5_close}.

    우선순위:
      0) EXIT_PENDING/CLOSED → HOLD (중복 SELL 금지)
      1) RECOVERY 이거나 net_pct<=-5(진입) → 손실회복 판정 최우선
      2) 수익 트레일링 활성(최고 net>=+1.5) → 모든 NORMAL 포지션(복원/신규) 지배.
         SELL 또는 HOLD 로 기존 ①~⑩(특히 +2.0/+2.5 고정익절)을 대체한다.
      3) recovered=True & NORMAL & 트레일 미활성 → 고정익절/시간/추세 매도 면제(HOLD).
      4) 그 외(신규·NORMAL·트레일 미활성) → DEFER(기존 로직: 단, +2.0/+2.5 비활성).
    """
    ctx = ctx or {}
    atr_pct     = float(ctx.get("atr_pct") or 0.0)
    ema9        = ctx.get("ema9")
    ema9_rising = bool(ctx.get("ema9_rising"))
    bar5_close  = bool(ctx.get("bar5_close"))

    mode = state.get("management_mode", MODE_NORMAL)
    if mode in (MODE_EXIT, MODE_CLOSED):
        s = dict(state); s["last_evaluated_at"] = now.isoformat()
        return RecoveryDecision(ACT_HOLD, mode, "exit_pending_or_closed", s)

    # 1) 손실 회복 트레일링(진입 포함) — 최우선
    if mode == MODE_RECOVERY or (net_pct is not None and net_pct <= RECOVERY_ENTER_NET):
        return evaluate(state, net_pct, cur_price, now, atr_pct=atr_pct)

    # 2) 수익 트레일링 — 활성 시 모든 NORMAL 포지션 지배(고정익절 대체)
    s = dict(state); s["last_evaluated_at"] = now.isoformat()
    bump_highest_price(s, cur_price)
    pt = evaluate_profit_trailing(s, net_pct, cur_price, atr_pct=atr_pct,
                                  ema9=ema9, ema9_rising=ema9_rising,
                                  bar5_close=bar5_close)
    s = pt["state"]
    if pt["sell"]:
        return RecoveryDecision(ACT_SELL_ALL, MODE_NORMAL,
                                f"profit_trailing_exit({pt['reason']})", s)
    if s.get("profit_trail_active"):
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL,
                                f"profit_trailing_hold({pt['reason']})", s)

    # 3) 복원 포지션(트레일 미활성) → 고정익절/시간/추세 매도 면제
    if state.get("recovered"):
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL,
                                "recovered_hold(고정익절·시간청산·MA매도 면제)", s)

    # 4) 신규·NORMAL·트레일 미활성 → 기존 로직 위임
    return RecoveryDecision(ACT_DEFER, MODE_NORMAL, "defer_to_existing", s)
