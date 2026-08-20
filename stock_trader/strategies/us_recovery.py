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

── 수익 전용 트레일링(손절 없음) 규칙 ────────────────────────────────
  ★ 미국 매도정책은 '손절 없는 수익 전용 트레일링'이다. 손실 구간에서는 어떤 자동
    SELL 도 실행하지 않는다(HOLD, 재상승 대기). 매수 체결 직후부터 highest·ATR
    트레일을 추적하되 매도는 수익이 확보된 경우에만 허용한다.
  트레일 : trail_pct = clamp(ATR% × 1.5, 1.0%, 2.5%)
  활성화 : 순수익률 최고점 >= trail_pct + MIN_NET_PROFIT_PCT (sticky)
  매도(다음 두 조건이 모두 충족될 때만):
    (1) 완성봉(5분 우선, 없으면 1분) 종가가 고점 대비 동적 트레일 아래로 이탈, AND
    (2) 수수료·환율 반영 현재 순손익률 >= MIN_NET_PROFIT_PCT
  ★ 예상 순손익이 기준 미만이면 매도하지 않고 HOLD 한다(재상승 대기).
  ★ 마지막 확인봉 시각·활성상태·highest 는 상태에 영속되어 재시작 후에도 유지된다.

── 손실 관리(구 RECOVERY) — 전면 비활성화 ────────────────────────────
  ★ 고정 -5%/-6% 손절, 구조하락+금액한도 손실매도, RECOVERY 손실청산 분기를
    **전부 비활성화**한다. 손실 구간은 어떤 경우에도 SELL 하지 않고 HOLD 한다.
  ★ EXIT_PENDING·중복매도 방지(SELL 제출 후)는 그대로 유지한다.
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

# ── 수익 전용 트레일링(손절 없음) 임계 ───────────────────────────
PROFIT_TRAIL_ACTIVATE_NET = 1.5    # (구 정책 상수 — 하위호환 유지, 현재 미사용)
PROFIT_TRAIL_ATR_MULT     = 1.5
PROFIT_TRAIL_MIN          = 1.0
PROFIT_TRAIL_MAX          = 2.5
PROFIT_TRAIL_PANIC_EXTRA  = 0.5    # (구 정책 상수 — 하위호환 유지, 현재 미사용)
PROFIT_TRAIL_CONFIRM_BARS = 2      # (구 정책 상수 — 하위호환 유지, 현재 미사용)
# ★ 순손익 최소 확보(%). 트레일 '활성화'와 '매도' 게이트 공통 기준(수수료·환율 반영).
MIN_NET_PROFIT_PCT        = 0.3
# ── 자동 SELL 최종 초크포인트: 순수익 절대금액 게이트 기본값(호출부가 override) ──
#   +0.3% 는 체결 미끄러짐으로 실제 순손실이 될 수 있으므로, 퍼센트뿐 아니라
#   '수수료 반영 예상 순손익($)'이 (왕복비용+안전버퍼)와 최소달러수익을 모두 넘을 때만 매도.
FEE_ROUND_TRIP_PCT        = 0.25   # 시스템 왕복 수수료 근사(USPosition.net_pct 의 -0.25 와 동일)
MIN_NET_PROFIT_USD        = 1.0    # 설정된 최소 달러 수익(기본값 — 호출부 override 가능)
SELL_SAFETY_BUFFER_USD    = 0.5    # 체결 미끄러짐 안전버퍼($, 기본값 — 호출부 override 가능)


def expected_net_profit(avg_price: float, cur_price: float, qty: int,
                        fee_pct: float = FEE_ROUND_TRIP_PCT) -> dict:
    """수수료 반영 예상 순손익(퍼센트/USD). **비용은 시스템 수수료 모델을 재사용**한다.

    · gross_pct = (cur-avg)/avg×100,  net_pct = gross_pct - fee_pct
      (fee_pct 는 USPosition.net_pct 가 쓰는 왕복 수수료 근사와 동일한 값).
    · round_trip_cost_usd = fee_pct% × (avg×qty)  ← 왕복 수수료($) 근사.
    · net_usd = (cur-avg)×qty - round_trip_cost_usd  ← 수수료 반영 예상 순손익($).
    ★ 미국 포지션은 매수·매도 모두 USD 라 '순손익률/USD'는 환율에 중립이다(환율은
      원화 환산 표기에만 영향). 따라서 게이트는 USD 기준으로 비교한다(환율 추정 없음).
    """
    try:
        a = float(avg_price); c = float(cur_price); q = int(qty)
    except (TypeError, ValueError):
        a = c = 0.0; q = 0
    gross_pct = ((c - a) / a * 100.0) if a > 0 else 0.0
    net_pct = gross_pct - float(fee_pct or 0.0)
    cost_basis = a * q
    round_trip_cost_usd = (float(fee_pct or 0.0) / 100.0) * cost_basis
    net_usd = (c - a) * q - round_trip_cost_usd
    return {
        "net_pct":              round(net_pct, 4),
        "net_usd":              round(net_usd, 4),
        "gross_pct":            round(gross_pct, 4),
        "round_trip_cost_usd":  round(round_trip_cost_usd, 4),
        "cost_basis_usd":       round(cost_basis, 4),
    }


def auto_sell_allowed(avg_price: float, cur_price: float, qty: int, *,
                      fee_pct: float = FEE_ROUND_TRIP_PCT,
                      min_net_pct: float = MIN_NET_PROFIT_PCT,
                      min_net_usd: float = MIN_NET_PROFIT_USD,
                      safety_buffer_usd: float = SELL_SAFETY_BUFFER_USD) -> tuple:
    """자동 SELL 최종 게이트(단일 초크포인트 로직). 반환: (allowed: bool, metrics: dict).

    allowed = (net_pct >= min_net_pct) AND
              (net_usd >= max(round_trip_cost_usd + safety_buffer_usd, min_net_usd)).
    ★ 상위 판정이 SELL 이어도 이 게이트 미달이면 주문하지 않는다(HOLD). 손실 구간은
      net_pct<0 이므로 항상 차단된다(자동 손절 없음).
    """
    m = expected_net_profit(avg_price, cur_price, qty, fee_pct=fee_pct)
    required_usd = max(m["round_trip_cost_usd"] + float(safety_buffer_usd or 0.0),
                       float(min_net_usd or 0.0))
    allowed = (m["net_pct"] >= float(min_net_pct)) and (m["net_usd"] >= required_usd)
    m = dict(m)
    m["required_usd"]  = round(required_usd, 4)
    m["min_net_pct"]   = float(min_net_pct)
    m["min_net_usd"]   = float(min_net_usd)
    m["safety_buffer_usd"] = float(safety_buffer_usd or 0.0)
    return bool(allowed), m

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
        # ── 종목별 최대허용 금액손실($, 진입 시 확정·영속. 매 루프 재계산/확대 금지) ──
        "max_loss_usd_at_entry": None,
        # ── /api/status·로그 노출용 관찰 스냅샷(휘발성; 매 루프 갱신) ──
        "last_net_pct":          None,
        "last_unrealized_usd":   None,
        "last_atr_pct":          None,
        "last_decision":         None,
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
    # max_loss_usd_at_entry: 숫자로 파싱 불가하거나 <=0 이면 None(손상 → 호출부가 fail-safe).
    _ml = out.get("max_loss_usd_at_entry")
    if _ml is not None:
        try:
            _mlf = float(_ml)
            out["max_loss_usd_at_entry"] = _mlf if _mlf > 0 else None
        except (TypeError, ValueError):
            out["max_loss_usd_at_entry"] = None
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
             symbol_risk_exceeded: bool = False,
             atr_valid: bool = True) -> RecoveryDecision:
    """손실 관리 — **전면 비활성화(손절 없음)**. 어떤 경우에도 SELL 하지 않는다.

    ★ 미국 매도정책은 '손절 없는 수익 전용 트레일링'으로 재정의되었다. 고정 -5%/-6%
      손절, 구조하락+금액한도 손실매도, RECOVERY 손실청산 분기를 전부 비활성화한다.
      이 함수는 하위호환을 위해 남겨두되 **항상 HOLD** 를 반환한다(손실 구간 매도 금지).
      매수 체결 직후부터 highest 는 계속 추적한다. 손실 매도 판정은 존재하지 않는다.
    """
    s = dict(state)
    s["last_evaluated_at"] = now.isoformat()
    mode = s.get("management_mode", MODE_NORMAL)
    if mode in (MODE_EXIT, MODE_CLOSED):
        return RecoveryDecision(ACT_HOLD, mode, "exit_pending_or_closed", s)
    # 손실 관리 상태(RECOVERY)는 더 이상 진입/청산하지 않는다 → NORMAL 로 정규화.
    s["management_mode"] = MODE_NORMAL
    bump_highest_price(s, cur_price)
    return RecoveryDecision(ACT_HOLD, MODE_NORMAL, "loss_management_disabled_hold", s)


# ══════════════════════════════════════════════════════════════
# 수익 트레일링(상방)
# ══════════════════════════════════════════════════════════════
def _fmt(v) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "NA"


def evaluate_profit_trailing(state: dict, net_pct: float, cur_price: float,
                             atr_pct: float = 0.0,
                             bar1_ts: Optional[str] = None,
                             bar1_close: Optional[float] = None,
                             bar5_ts: Optional[str] = None,
                             bar5_close: Optional[float] = None) -> dict:
    """수익 전용 트레일링 순수 판정(손절 없음). 반환: {"activate","sell","state","reason"}.

    ★ 활성화 : 순수익률 최고점 >= trail_pct + MIN_NET_PROFIT_PCT (sticky).
    ★ 매도(두 조건 모두 충족될 때만):
        (1) 완성봉(5분 우선, 없으면 1분) 종가가 고점 대비 동적 트레일 아래로 이탈, AND
        (2) 수수료·환율 반영 현재 순손익률(net_pct) >= MIN_NET_PROFIT_PCT.
      예상 순손익이 기준 미만이면 매도하지 않고 HOLD(재상승 대기). 손실 구간·미완성봉·
      데이터 없음이면 매도 없음. 마지막 확인봉 시각(last_profit_breach_bar_at)은
      상태에 영속되어 재시작 후에도 유지된다. cur_price 는 급락 즉시매도에 쓰지 않는다
      (매도는 오직 완성봉 이탈 기준).
    """
    s = dict(state)
    hi = s.get("profit_high_net_pct")
    if net_pct is not None and (hi is None or net_pct > hi):
        s["profit_high_net_pct"] = float(net_pct)
        hi = float(net_pct)

    trail_pct = _clamp(float(atr_pct or 0.0) * PROFIT_TRAIL_ATR_MULT,
                       PROFIT_TRAIL_MIN, PROFIT_TRAIL_MAX)
    activate_need = trail_pct + MIN_NET_PROFIT_PCT

    activate = False
    if not s.get("profit_trail_active") and hi is not None and hi >= activate_need:
        s["profit_trail_active"] = True
        activate = True

    if not s.get("profit_trail_active"):
        return {"activate": activate, "sell": False, "state": s,
                "reason": (f"profit_trail_inactive(hi={_fmt(hi)}%,"
                           f"need>={activate_need:.2f}%)")}

    highest = float(s.get("highest_price") or 0.0)

    # 완성봉만 사용(미완성/데이터 없음 → 매도 보류). 5분봉 우선, 없으면 1분봉.
    if bar5_ts and bar5_close is not None:
        bar_ts, bar_close, kind = bar5_ts, float(bar5_close), "5m"
    elif bar1_ts and bar1_close is not None:
        bar_ts, bar_close, kind = bar1_ts, float(bar1_close), "1m"
    else:
        bar_ts, bar_close, kind = None, None, None

    if bar_ts is None or highest <= 0:
        return {"activate": activate, "sell": False, "state": s,
                "reason": "profit_trail_hold(no_completed_bar)"}

    # 마지막 확인봉 영속(재시작 유지)
    s["last_profit_breach_bar_at"] = bar_ts
    drop = (bar_close - highest) / highest * 100.0
    if drop > -trail_pct:
        return {"activate": activate, "sell": False, "state": s,
                "reason": f"profit_trail_hold({kind}_drop={drop:.2f}%>-{trail_pct:.2f}%)"}

    # 트레일 이탈 확정 — 순손익 게이트(수수료·환율 반영 net_pct >= MIN_NET_PROFIT_PCT)
    if net_pct is None or float(net_pct) < MIN_NET_PROFIT_PCT:
        return {"activate": activate, "sell": False, "state": s,
                "reason": (f"profit_trail_breach_but_net_below_min("
                           f"net={_fmt(net_pct)}%<{MIN_NET_PROFIT_PCT}%)")}
    return {"activate": activate, "sell": True, "state": s,
            "reason": (f"profit_trail_exit({kind}_drop={drop:.2f}%,"
                       f"net={float(net_pct):.2f}%>={MIN_NET_PROFIT_PCT}%)")}


# ══════════════════════════════════════════════════════════════
# 통합 판정 — US 관리상태의 '단일 매도판정 권위' (DEFER 없음)
# ══════════════════════════════════════════════════════════════
def decide_management_action(state: dict, net_pct: float, cur_price: float,
                             now: datetime, ctx: Optional[dict] = None) -> RecoveryDecision:
    """단일 매도판정 권위 — **수익 전용 트레일링(손절 없음)**.

    · EXIT_PENDING/CLOSED → HOLD(재제출 금지, 중복매도 방지 유지).
    · 그 외 → NORMAL 로 정규화 후 수익 트레일링만 적용. 매수 체결 직후부터 highest 를
      계속 추적한다. 손실 구간에서는 어떤 SELL 도 실행하지 않는다(HOLD, 재상승 대기).
    · SELL 은 '완성봉 트레일 이탈 AND 순손익률 >= MIN_NET_PROFIT_PCT' 일 때만.

    ctx(선택): {atr_pct, bar1_ts, bar1_close, bar5_ts, bar5_close}. (구 정책의
    ema9/symbol_risk/account_risk 키는 더 이상 사용하지 않는다 — 하위호환으로 무시.)
    """
    ctx = ctx or {}
    atr_pct    = float(ctx.get("atr_pct") or 0.0)
    bar1_ts    = ctx.get("bar1_ts")
    bar1_close = ctx.get("bar1_close")
    bar5_ts    = ctx.get("bar5_ts")
    bar5_close = ctx.get("bar5_close")

    mode = state.get("management_mode", MODE_NORMAL)
    s = dict(state); s["last_evaluated_at"] = now.isoformat()
    if mode in (MODE_EXIT, MODE_CLOSED):
        return RecoveryDecision(ACT_HOLD, mode, "exit_pending_or_closed", s)

    # 손실 관리(RECOVERY) 분기 전면 비활성화 → 항상 NORMAL 로 정규화(손절 없음).
    s["management_mode"] = MODE_NORMAL
    bump_highest_price(s, cur_price)   # 매수 체결 직후부터 highest 추적(항상)

    pt = evaluate_profit_trailing(s, net_pct, cur_price, atr_pct=atr_pct,
                                  bar1_ts=bar1_ts, bar1_close=bar1_close,
                                  bar5_ts=bar5_ts, bar5_close=bar5_close)
    s = pt["state"]
    if pt["sell"]:
        return RecoveryDecision(ACT_SELL_ALL, MODE_NORMAL,
                                f"profit_trailing_exit({pt['reason']})", s)
    return RecoveryDecision(ACT_HOLD, MODE_NORMAL, f"hold({pt['reason']})", s)
