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

── 손실 관리(RECOVERY_WAIT) — 고정 손절 제거, 회복 기회 부여 ──────────
  ★ 고정 -6% 전량손절 제거. -5%/-6% 는 매도 조건이 아니라 상태 진입·경고 기준.
  진입 : net_pct <= -5.0 → RECOVERY_WAIT (즉시매도 금지, 회복 대기)
  경고 : net_pct <= -6.0 → 경고 표시만(recovery_warn), 매도 없음
  회복 : recovery high 대비 '단순 하락'만으로 매도하지 않는다. 손익분기 0% 까지
         회복할 기회를 준다. net_pct >= 0.0 → NORMAL 복귀, 이후 +1.5% 부터 ATR 수익 트레일.
  손실 매도 허용(둘 중 하나뿐):
    (1) 완성된 5분봉 기준 ATR '구조적 추세 붕괴'가 **연속 확인**(STRUCT_CONFIRM_BARS)
    (2) 계좌 위험한도 초과(account_risk_exceeded, 호출부가 판정) — 손실 포지션 한정
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

# ── 관리 모드 ────────────────────────────────────────────────────
MODE_NORMAL        = "NORMAL"
MODE_RECOVERY_WAIT = "RECOVERY_WAIT"
MODE_RECOVERY      = MODE_RECOVERY_WAIT   # 하위호환 별칭(코드 참조용)
MODE_EXIT          = "EXIT_PENDING"
MODE_CLOSED        = "CLOSED"
_LEGACY_RECOVERY   = "RECOVERY_TRAILING"  # 구버전 저장값 → RECOVERY_WAIT 로 병합

# ── 손실 관리(RECOVERY_WAIT) 임계 ─────────────────────────────────
RECOVERY_ENTER_NET   = -5.0    # 진입: net_pct <= -5.0 (상태 진입 기준, 매도 아님)
RECOVERY_WARN_NET    = -6.0    # 경고 기준(매도 아님)
RECOVERY_SUCCESS_NET = -2.0    # 회복 성공 표시(정보용)
RECOVERY_NORMAL_NET  =  0.0    # 손익분기 회복 → NORMAL 전환

# ── 구조적 추세 붕괴(완성 5분봉·ATR·연속) — 유일한 '추세' 손실 매도 ──
STRUCT_ATR_MULT      = 3.0     # 구조적 손실거리 = ATR% × 3.0 (넓은 거리)
STRUCT_MIN_PCT       = 3.0     # 구조적 손실거리 하한(%)
STRUCT_CONFIRM_BARS  = 2       # 연속 확인 완성 5분봉 수

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
        "recovery_reached_exit": False,   # net>=-2.0 회복 성공 표시(정보)
        "recovery_warn":         False,   # net<=-6.0 경고 표시(매도 아님)
        # ── 매매일지 분석용(향후 실제 데이터로 정책 조정) ──
        "recovery_entry_price":     None,   # RECOVERY_WAIT 진입 시각 가격
        "recovery_entry_net":       None,   # 진입 시 net_pct(≈-5, 조기손절 가상손익 기준)
        "recovery_max_drawdown_net": None,  # 진입 후 최저 net_pct(최대하락률)
        "recovery_warn_at":         None,   # -6% 최초 도달 시각(ISO)
        "recovery_last_outcome":    None,   # 회복/청산 시 1회 기록용(호출부가 소비 후 제거)
        # ── 구조적 추세 붕괴 '완성 5분봉' 연속 확인(봉 중복/역행/늦은정정 처리) ──
        "struct_breach_closes":      0,
        "struct_prev_breach_closes": 0,
        "last_struct_breach_bar_at": None,
        # ── 수익 트레일링 ──
        "profit_trail_active":   False,
        "profit_high_net_pct":   None,
        # ── 수익 트레일 '완료봉' 연속 확인(봉 중복/역행/늦은정정 처리) ──
        "profit_breach_closes":  0,
        "profit_prev_breach_closes": 0,
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
    out["recovery_reached_exit"] = bool(out.get("recovery_reached_exit"))
    out["recovery_warn"] = bool(out.get("recovery_warn"))
    out["quarantined"] = bool(out.get("quarantined"))
    for ck in ("profit_breach_closes", "profit_prev_breach_closes",
               "struct_breach_closes", "struct_prev_breach_closes"):
        try:
            out[ck] = int(out.get(ck) or 0)
        except (TypeError, ValueError):
            out[ck] = 0
    try:
        out["highest_price"] = float(out.get("highest_price") or 0.0)
    except (TypeError, ValueError):
        out["highest_price"] = 0.0
    # 구버전 RECOVERY_TRAILING → RECOVERY_WAIT 로 병합
    if out.get("management_mode") == _LEGACY_RECOVERY:
        out["management_mode"] = MODE_RECOVERY_WAIT
    if out.get("management_mode") not in (MODE_NORMAL, MODE_RECOVERY_WAIT,
                                          MODE_EXIT, MODE_CLOSED):
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


def _update_breach_count(state: dict, closes_key: str, prev_key: str,
                         last_key: str, bar_ts: Optional[str], is_breach: bool) -> int:
    """'완료봉' 기반 **연속(consecutive)** 이탈 카운터 갱신. 반환: 갱신된 count.

    규칙(§3/§4):
      - bar_ts None(데이터 없음/미완성/미상) → 변경 없음(현재값 반환, 판정 보류).
      - bar_ts < last(역행) → 무시(변경 없음).
      - bar_ts == last(동일 봉 중복/늦은 정정) → **직전 카운트(prev)로부터 재계산**:
          이탈이면 prev+1, 정상이면 0. (늦게 정상 종가로 바뀌면 안전하게 교정)
      - 새 완료봉 → prev 저장 후, 이탈이면 prev+1, 정상봉이면 0(연속 초기화).
    """
    if not bar_ts:
        return int(state.get(closes_key) or 0)
    last = state.get(last_key)
    if last is not None and str(bar_ts) < str(last):
        return int(state.get(closes_key) or 0)   # 역행 무시
    if last == bar_ts:
        prev = int(state.get(prev_key) or 0)
        cnt = (prev + 1) if is_breach else 0
        state[closes_key] = cnt
        return cnt
    # 새 완료봉
    prev = int(state.get(closes_key) or 0)
    state[prev_key] = prev
    cnt = (prev + 1) if is_breach else 0
    state[closes_key] = cnt
    state[last_key] = bar_ts
    return cnt


def _reset_breach(state: dict, closes_key: str, prev_key: str, last_key: str) -> None:
    state[closes_key] = 0
    state[prev_key] = 0
    state[last_key] = None


# ══════════════════════════════════════════════════════════════
# 손실 회복
# ══════════════════════════════════════════════════════════════
def struct_stop_pct(atr_pct: float) -> float:
    """구조적 손실거리(%) = max(하한, ATR% × 배수). '넓은' 거리."""
    return max(STRUCT_MIN_PCT, float(atr_pct or 0.0) * STRUCT_ATR_MULT)


def risk_capped_qty(cur_price: float, atr_pct: float, budget_qty: int,
                    max_loss_usd: float) -> int:
    """신규 미국 매수 수량을 '종목별 최대 허용손실 + 넓어진 위험거리'로 **축소**.

    고정 -5%/-6% 손절을 제거했으므로, 손실 위험거리는 구조적 손실거리(넓음)로 본다.
      per_share_risk = cur_price × struct_stop_pct(atr)%/100
      qty_cap        = floor(max_loss_usd / per_share_risk)
      반환           = max(0, min(budget_qty, qty_cap))   ← 예산 수량을 넘겨 늘리지 않음
    """
    try:
        cp = float(cur_price); ml = float(max_loss_usd); bq = int(budget_qty)
    except (TypeError, ValueError):
        try:
            return max(0, int(budget_qty or 0))
        except (TypeError, ValueError):
            return 0
    if bq <= 0:
        return 0
    if cp <= 0 or ml <= 0:
        return bq
    risk_frac = struct_stop_pct(atr_pct) / 100.0
    per_share = cp * risk_frac
    if per_share <= 0:
        return bq
    cap = int(ml / per_share)
    return max(0, min(bq, cap))


def _elapsed_seconds(started_iso: Optional[str], now: datetime) -> Optional[float]:
    if not started_iso:
        return None
    try:
        return (now - datetime.fromisoformat(started_iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def _recovery_outcome(s: dict, outcome: str, net_pct, cur_price, now) -> dict:
    """매매일지 분석 레코드(향후 실제 데이터로 정책 조정). 순수 dict."""
    return {
        "outcome":                outcome,          # recovered / structural_and_risk_sell
        "entry_price":            s.get("recovery_entry_price"),
        "entry_net_pct":          s.get("recovery_entry_net"),
        "max_drawdown_net_pct":   s.get("recovery_max_drawdown_net"),
        "warn6_at":               s.get("recovery_warn_at"),
        "started_at":             s.get("recovery_started_at"),
        "ended_at":               now.isoformat(),
        "recovery_seconds":       _elapsed_seconds(s.get("recovery_started_at"), now),
        "final_net_pct":          (float(net_pct) if net_pct is not None else None),
        "final_price":            (float(cur_price) if cur_price is not None else None),
        # 조기손절 가상손익 기준(진입 net≈-5%). 호출부가 수량으로 $환산.
        "hypothetical_early_stop_net_pct": s.get("recovery_entry_net"),
    }


def evaluate(state: dict, net_pct: float, cur_price: float, now: datetime,
             atr_pct: float = 0.0, bar5_ts: Optional[str] = None,
             bar5_close: Optional[float] = None,
             account_risk_exceeded: bool = False,
             symbol_risk_exceeded: bool = False) -> RecoveryDecision:
    """손실 관리(RECOVERY_WAIT) 판정. EXIT/CLOSED → HOLD.

    목적: '정해진 손절률 준수'가 아니라 **누적 실현수익 극대화·불필요한 손실확정 최소화**.
    ★ 고정 -6% 전량손절 없음. -5%/-6% 는 상태 진입·관찰(경고) 기준. recovery high 대비
      '단순 하락'이나 '짧은 반등 실패'만으로 매도하지 않는다(0% 회복 기회 부여).
    ★ 손실 매도는 **다음이 모두 동시 충족**될 때만 허용:
        (1) 완성 5분봉 ATR 구조적 하락이 **연속 확인**(STRUCT_CONFIRM_BARS)  그리고
        (2) account_risk_exceeded(계좌 위험한도 초과)                        그리고
        (3) symbol_risk_exceeded(종목 위험한도 초과 — 호출부가 금액기준 판정)
      net_pct >= 0.0 → NORMAL 복귀. 회복/청산 시 분석 레코드를 recovery_last_outcome 에 남긴다.
    """
    s = dict(state)
    s["last_evaluated_at"] = now.isoformat()
    mode = s.get("management_mode", MODE_NORMAL)

    if mode in (MODE_EXIT, MODE_CLOSED):
        return RecoveryDecision(ACT_HOLD, mode, "exit_pending_or_closed", s)

    bump_highest_price(s, cur_price)

    # ── NORMAL: 진입 감시(매도 아님) ──
    if mode == MODE_NORMAL:
        if net_pct is not None and net_pct <= RECOVERY_ENTER_NET:
            s["management_mode"]        = MODE_RECOVERY_WAIT
            s["recovery_started_at"]    = now.isoformat()
            s["recovery_high_price"]    = float(cur_price or 0.0)
            s["recovery_high_net_pct"]  = float(net_pct)
            s["recovery_reached_exit"]  = False
            s["recovery_warn"]          = bool(net_pct <= RECOVERY_WARN_NET)
            # 분석용 진입 스냅샷
            s["recovery_entry_price"]     = float(cur_price or 0.0)
            s["recovery_entry_net"]       = float(net_pct)
            s["recovery_max_drawdown_net"] = float(net_pct)
            s["recovery_warn_at"]         = (now.isoformat()
                                             if net_pct <= RECOVERY_WARN_NET else None)
            _reset_breach(s, "struct_breach_closes",
                          "struct_prev_breach_closes", "last_struct_breach_bar_at")
            return RecoveryDecision(ACT_HOLD, MODE_RECOVERY_WAIT,
                                    f"recovery_wait_enter(net={net_pct:.2f}%)", s)
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL, "normal_hold", s)

    # ── RECOVERY_WAIT ──
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

    # 분석용: 최대하락률 갱신, -6% 최초도달 시각
    if net_pct is not None:
        md = s.get("recovery_max_drawdown_net")
        if md is None or net_pct < md:
            s["recovery_max_drawdown_net"] = float(net_pct)
        if net_pct <= RECOVERY_WARN_NET:
            s["recovery_warn"] = True
            if not s.get("recovery_warn_at"):
                s["recovery_warn_at"] = now.isoformat()

    # 손익분기(0%) 회복 → NORMAL 전환 (매매일지 분석 레코드 기록)
    if net_pct is not None and net_pct >= RECOVERY_NORMAL_NET:
        s["recovery_last_outcome"] = _recovery_outcome(
            s, "recovered", net_pct, cur_price, now)
        s["management_mode"]        = MODE_NORMAL
        s["recovery_started_at"]    = None
        s["recovery_high_price"]    = None
        s["recovery_high_net_pct"]  = None
        s["recovery_reached_exit"]  = False
        s["recovery_warn"]          = False
        s["recovery_entry_price"]   = None
        s["recovery_entry_net"]     = None
        s["recovery_max_drawdown_net"] = None
        s["recovery_warn_at"]       = None
        _reset_breach(s, "struct_breach_closes",
                      "struct_prev_breach_closes", "last_struct_breach_bar_at")
        return RecoveryDecision(ACT_HOLD, MODE_NORMAL,
                                f"recovery_exit_to_normal(net={net_pct:.2f}%>=0)", s)

    # -2.0 회복 성공 표시(정보)
    if net_pct is not None and net_pct >= RECOVERY_SUCCESS_NET:
        s["recovery_reached_exit"] = True

    # 손실 매도 — (1)구조적 붕괴 연속 확인 AND (2)계좌위험 AND (3)종목위험 **동시** 충족.
    #   구조 카운트는 항상 갱신(정상 5분봉 끼면 초기화). 위험한도 미충족이면 HOLD(관찰).
    struct_confirmed = False
    if bar5_ts and bar5_close is not None and rhp and rhp > 0:
        sstop = struct_stop_pct(atr_pct)
        b5drop = (float(bar5_close) - float(rhp)) / float(rhp) * 100.0
        breakdown = b5drop <= -sstop
        closes = _update_breach_count(
            s, "struct_breach_closes", "struct_prev_breach_closes",
            "last_struct_breach_bar_at", bar5_ts, breakdown)
        struct_confirmed = breakdown and closes >= STRUCT_CONFIRM_BARS

    if struct_confirmed and account_risk_exceeded and symbol_risk_exceeded:
        s["recovery_last_outcome"] = _recovery_outcome(
            s, "structural_and_risk_sell", net_pct, cur_price, now)
        return RecoveryDecision(
            ACT_SELL_ALL, MODE_RECOVERY_WAIT,
            "structural_breakdown+account_risk+symbol_risk("
            f"net={net_pct:.2f}%,struct_closes={int(s.get('struct_breach_closes') or 0)})", s)

    return RecoveryDecision(ACT_HOLD, MODE_RECOVERY_WAIT, "recovery_wait_hold", s)


# ══════════════════════════════════════════════════════════════
# 수익 트레일링(상방)
# ══════════════════════════════════════════════════════════════
def evaluate_profit_trailing(state: dict, net_pct: float, cur_price: float,
                             atr_pct: float = 0.0, ema9: Optional[float] = None,
                             ema9_rising: bool = False,
                             bar1_ts: Optional[str] = None,
                             bar1_close: Optional[float] = None,
                             bar5_ts: Optional[str] = None,
                             bar5_close: Optional[float] = None) -> dict:
    """수익 트레일링 순수 판정. 반환: {"activate","sell","state","reason"}.

    cur_price(실시간)는 급락 안전매도에만, **bar1_close/bar5_close(완료봉 종가)** 는
    트레일 확정에 쓴다(§2/§4). bar_ts None(데이터 없음/미완성) → 확정봉 매도 보류.
      · 서로 다른 '연속' 완료 1분봉 2개가 트레일 아래 마감 → SELL(EMA9 무관).
      · 완료 5분봉 1개가 트레일 아래 마감 → SELL.
      · 트레일 이탈(완료봉) + (EMA9 하락 or 종가<EMA9) → 완료봉 1개로 빠른 SELL.
      · 실시간가 급락(동적폭 +0.5%p) → 봉 없이 즉시 안전매도.
    정상봉이 끼면 연속 카운트 초기화. EMA9 상승은 veto 아님.
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
        _reset_breach(s, "profit_breach_closes",
                      "profit_prev_breach_closes", "last_profit_breach_bar_at")
        return {"activate": activate, "sell": False, "state": s,
                "reason": "profit_trail_inactive"}

    trail_pct = _clamp(float(atr_pct or 0.0) * PROFIT_TRAIL_ATR_MULT,
                       PROFIT_TRAIL_MIN, PROFIT_TRAIL_MAX)
    highest = float(s.get("highest_price") or 0.0)

    # (a) 급락 안전매도 — 실시간 가격 기준(데이터 없어도 동작)
    if highest > 0 and cur_price is not None:
        live_drop = (float(cur_price) - highest) / highest * 100.0
        if live_drop <= -(trail_pct + PROFIT_TRAIL_PANIC_EXTRA):
            return {"activate": activate, "sell": True, "state": s,
                    "reason": f"profit_trail_panic(live_drop={live_drop:.2f}%)"}

    # (b) 완료 5분봉 종가가 트레일 아래 → 즉시 확정
    if bar5_ts and bar5_close is not None and highest > 0:
        d5 = (float(bar5_close) - highest) / highest * 100.0
        if d5 <= -trail_pct:
            return {"activate": activate, "sell": True, "state": s,
                    "reason": f"profit_trail_bar5_close(bar_drop={d5:.2f}%)"}

    # (c) 완료 1분봉 종가 기준 '연속' 확정 — 정상봉 끼면 초기화(§3)
    if bar1_ts and bar1_close is not None and highest > 0:
        d1 = (float(bar1_close) - highest) / highest * 100.0
        breach = d1 <= -trail_pct
        closes = _update_breach_count(
            s, "profit_breach_closes", "profit_prev_breach_closes",
            "last_profit_breach_bar_at", bar1_ts, breach)
        if breach:
            ema9_down = ((ema9 is not None and float(bar1_close) < float(ema9))
                         or (not ema9_rising))
            if closes >= 1 and ema9_down:
                return {"activate": activate, "sell": True, "state": s,
                        "reason": (f"profit_trail_fast(bar_drop={d1:.2f}%,"
                                   f"ema9_down={ema9_down},closes={closes})")}
            if closes >= PROFIT_TRAIL_CONFIRM_BARS:
                return {"activate": activate, "sell": True, "state": s,
                        "reason": f"profit_trail_confirm(bar_drop={d1:.2f}%,closes={closes})"}

    return {"activate": activate, "sell": False, "state": s,
            "reason": ("profit_trail_pending("
                       f"closes={int(s.get('profit_breach_closes') or 0)})")}


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
    bar1_close  = ctx.get("bar1_close")
    bar5_ts     = ctx.get("bar5_ts")
    bar5_close  = ctx.get("bar5_close")
    account_risk_exceeded = bool(ctx.get("account_risk_exceeded"))
    symbol_risk_exceeded  = bool(ctx.get("symbol_risk_exceeded"))

    mode = state.get("management_mode", MODE_NORMAL)
    if mode in (MODE_EXIT, MODE_CLOSED):
        s = dict(state); s["last_evaluated_at"] = now.isoformat()
        return RecoveryDecision(ACT_HOLD, mode, "exit_pending_or_closed", s)

    # 1) 손실 관리(RECOVERY_WAIT 진입 포함) — 최우선.
    #    손실 매도는 '구조적 5분봉 붕괴 연속 + 계좌위험 + 종목위험'이 **모두** 충족될 때만.
    if mode == MODE_RECOVERY_WAIT or (net_pct is not None and net_pct <= RECOVERY_ENTER_NET):
        return evaluate(state, net_pct, cur_price, now, atr_pct=atr_pct,
                        bar5_ts=bar5_ts, bar5_close=bar5_close,
                        account_risk_exceeded=account_risk_exceeded,
                        symbol_risk_exceeded=symbol_risk_exceeded)

    # 2) 수익 트레일링 — 활성 시 모든 NORMAL 포지션(복원/신규) 지배
    s = dict(state); s["last_evaluated_at"] = now.isoformat()
    bump_highest_price(s, cur_price)
    pt = evaluate_profit_trailing(s, net_pct, cur_price, atr_pct=atr_pct, ema9=ema9,
                                  ema9_rising=ema9_rising, bar1_ts=bar1_ts,
                                  bar1_close=bar1_close, bar5_ts=bar5_ts,
                                  bar5_close=bar5_close)
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
