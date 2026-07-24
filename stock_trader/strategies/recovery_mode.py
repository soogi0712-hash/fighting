"""
recovery_mode.py — 손실 반복 제한용 임시 안전모드 (오늘 미국장 제한 재가동)

기존 전략을 재작성하지 않고, 신규 '매수'만 강하게 제한하는 게이트를 추가한다.
매도(익절/손절/시간청산)와 기존 보유종목 관리는 절대 막지 않는다.

환경변수(모두 선택, 기본 보수적):
  RECOVERY_MODE=true|false
  RECOVERY_MARKET=US
  RECOVERY_MAX_POSITIONS=2
  RECOVERY_MAX_POSITION_PCT=10
  RECOVERY_MAX_CONSECUTIVE_LOSSES=2
  RECOVERY_DISABLE_REENTRY_AFTER_LOSS=true
  RECOVERY_DISABLE_AVERAGING_DOWN=true
  RECOVERY_DAILY_LOSS_PCT=1.0
  RECOVERY_DAILY_LOSS_KRW=50000
  RECOVERY_FIRST_TRADE_ONLY=true

원칙:
  - 게이트는 '신규 매수 허용 여부'만 판단(부작용 없는 순수 함수).
  - 환율 미확보 시 미국 신규매수 차단(임의 환산 금지).
  - 손실한도 도달 시 강제청산 하지 않음 — 신규매수만 중단.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass


def _get_bool(env, key, default):
    v = env.get(key)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _get_float(env, key, default):
    v = env.get(key)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _get_int(env, key, default):
    v = env.get(key)
    if v in (None, ""):
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RecoveryConfig:
    enabled:                    bool  = False
    market:                     str   = "US"
    max_positions:              int   = 2
    max_position_pct:           float = 10.0
    max_consecutive_losses:     int   = 2
    disable_reentry_after_loss: bool  = True
    disable_averaging_down:     bool  = True
    daily_loss_pct:             float = 1.0
    daily_loss_krw:             float = 50_000.0
    first_trade_only:           bool  = True

    @classmethod
    def from_env(cls, env=None) -> "RecoveryConfig":
        env = env if env is not None else os.environ
        return cls(
            enabled                    = _get_bool(env, "RECOVERY_MODE", False),
            market                     = (env.get("RECOVERY_MARKET", "US") or "US").upper(),
            max_positions              = _get_int(env, "RECOVERY_MAX_POSITIONS", 2),
            max_position_pct           = _get_float(env, "RECOVERY_MAX_POSITION_PCT", 10.0),
            max_consecutive_losses     = _get_int(env, "RECOVERY_MAX_CONSECUTIVE_LOSSES", 2),
            disable_reentry_after_loss = _get_bool(env, "RECOVERY_DISABLE_REENTRY_AFTER_LOSS", True),
            disable_averaging_down     = _get_bool(env, "RECOVERY_DISABLE_AVERAGING_DOWN", True),
            daily_loss_pct             = _get_float(env, "RECOVERY_DAILY_LOSS_PCT", 1.0),
            daily_loss_krw             = _get_float(env, "RECOVERY_DAILY_LOSS_KRW", 50_000.0),
            first_trade_only           = _get_bool(env, "RECOVERY_FIRST_TRADE_ONLY", True),
        )


@dataclass(frozen=True)
class BuyContext:
    market:               str       # "US" | "KR"
    code:                 str
    has_position:         bool      # 이미 이 종목 보유 중?
    open_position_count:  int       # 현재 보유 종목 수
    intended_cost:        float     # 이번 주문 예상 비용(시장 통화)
    account_equity:       float     # 계좌 평가액(시장 통화)
    is_averaging_down:    bool      # 평가손실 종목에 추가매수인가
    daily_realized_loss:  float     # 당일 실현손익(음수=손실, 시장 통화)
    fx_ok:                bool = True          # 환율 확보 여부(미국)
    daily_loss_limit_ccy: float | None = None  # 시장통화 환산 손실한도(미국은 fx 필요)


class RecoveryState:
    """세션 범위 가변 상태(연속손실·손절매도 재진입·왕복거래 확인)."""
    def __init__(self):
        self._lock = threading.Lock()
        self.consecutive_losses: int = 0
        self.loss_sold_codes: set = set()   # 당일 손실매도 종목(재진입 차단)
        self.round_trips_confirmed: int = 0  # 완결된 BUY→SELL 왕복 수
        self.buys_made: int = 0

    def record_buy(self, code):
        with self._lock:
            self.buys_made += 1

    def record_sell(self, code, net_pnl):
        """매도 실현손익 반영. 손실이면 연속손실+재진입차단, 이익이면 연속손실 리셋."""
        with self._lock:
            if net_pnl < 0:
                self.consecutive_losses += 1
                self.loss_sold_codes.add(code)
            else:
                self.consecutive_losses = 0
            # BUY→SELL 왕복 1회 완결
            self.round_trips_confirmed += 1

    def reset_session(self):
        with self._lock:
            self.consecutive_losses = 0
            self.loss_sold_codes = set()
            self.round_trips_confirmed = 0
            self.buys_made = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "consecutive_losses":  self.consecutive_losses,
                "loss_sold_codes":     sorted(self.loss_sold_codes),
                "round_trips_confirmed": self.round_trips_confirmed,
                "buys_made":           self.buys_made,
            }


class RecoveryGate:
    """신규 매수 허용 판단(순수). 상태는 RecoveryState 로 주입."""
    def __init__(self, config: RecoveryConfig = None, state: RecoveryState = None):
        self.config = config or RecoveryConfig.from_env()
        self.state = state or RecoveryState()

    def check_new_buy(self, ctx: BuyContext) -> tuple[bool, str]:
        """반환: (허용?, 사유). 비활성 시 (True, 'recovery off')."""
        c = self.config
        if not c.enabled:
            return True, "recovery off"

        # 1) 시장 제한 — 지정 시장만 신규매수
        if ctx.market.upper() != c.market:
            return False, f"recovery: {ctx.market} 신규매수 금지(허용시장={c.market})"

        # 2) 최초 왕복거래 전에는 동시 1종목만
        if c.first_trade_only and self.state.round_trips_confirmed < 1:
            if ctx.open_position_count >= 1 or self.state.buys_made >= 1:
                return False, "recovery: 최초 왕복거래 확인 전 추가 신규매수 금지(FIRST_TRADE_ONLY)"

        # 3) 동시 보유 최대 종목수
        if ctx.open_position_count >= c.max_positions:
            return False, f"recovery: 동시보유 한도({c.max_positions}) 도달"

        # 4) 물타기 금지(기존 보유/평가손실 추가매수 차단)
        if c.disable_averaging_down and (ctx.has_position or ctx.is_averaging_down):
            return False, "recovery: 물타기(추가매수) 금지"

        # 5) 손실매도 종목 당일 재진입 금지
        if c.disable_reentry_after_loss and ctx.code in self.state.loss_sold_codes:
            return False, "recovery: 당일 손실매도 종목 재진입 금지"

        # 6) 연속 실현손실 한도
        if self.state.consecutive_losses >= c.max_consecutive_losses:
            return False, f"recovery: 연속손실 {self.state.consecutive_losses}회 → 신규매수 중지"

        # 7) 당일 손실한도(더 작은 금액 적용). 미국은 환율 필요.
        if c.market == "US" and not ctx.fx_ok:
            return False, "recovery: 환율 미확보 → 신규매수 차단(임의 환산 금지)"
        pct_limit = abs(ctx.account_equity) * (c.daily_loss_pct / 100.0)
        krw_limit_ccy = ctx.daily_loss_limit_ccy
        if krw_limit_ccy is None:
            # 시장통화가 KRW 인 경우 daily_loss_krw 를 그대로 사용
            krw_limit_ccy = c.daily_loss_krw if c.market == "KR" else None
        candidates = [x for x in (pct_limit, krw_limit_ccy) if x is not None and x > 0]
        if not candidates:
            return False, "recovery: 손실한도 산출 불가 → 신규매수 차단"
        effective_limit = min(candidates)
        if ctx.daily_realized_loss <= -effective_limit:
            return False, (f"recovery: 당일 손실한도 도달"
                           f"(손실 {ctx.daily_realized_loss:,.0f} ≤ -{effective_limit:,.0f})")

        # 8) 포지션 비중 상한
        if ctx.account_equity > 0:
            pct = ctx.intended_cost / ctx.account_equity * 100.0
            if pct > c.max_position_pct + 1e-9:
                return False, (f"recovery: 포지션 비중 {pct:.1f}% > 한도 {c.max_position_pct:.0f}%")
        else:
            return False, "recovery: 계좌평가액 확인 불가 → 신규매수 차단"

        return True, "recovery: 신규매수 허용"

    def snapshot(self) -> dict:
        return {"config": self.config.__dict__, "state": self.state.snapshot()}


# ── 프로세스 공용 싱글턴 (KR/US 매니저가 상태 공유) ────────────
_GATE_LOCK = threading.Lock()
_GATE: "RecoveryGate | None" = None


def get_recovery_gate() -> RecoveryGate:
    """프로세스 1개 게이트(연속손실·재진입·왕복거래 상태 공유). env 로 1회 초기화."""
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            _GATE = RecoveryGate(config=RecoveryConfig.from_env(), state=RecoveryState())
        return _GATE


def reset_recovery_gate_for_test(config: RecoveryConfig = None) -> RecoveryGate:
    """테스트 전용: 싱글턴 재설정."""
    global _GATE
    with _GATE_LOCK:
        _GATE = RecoveryGate(config=config or RecoveryConfig.from_env(), state=RecoveryState())
        return _GATE
