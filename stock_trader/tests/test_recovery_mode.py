"""
Recovery Mode 검증 (E13~E21 등). 순수 게이트 — 네트워크·KIS 무관.
"""
import os
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

import pytest
from strategies.recovery_mode import (
    RecoveryConfig, RecoveryState, RecoveryGate, BuyContext,
)


def _cfg(**kw):
    base = dict(enabled=True, market="US", max_positions=2, max_position_pct=10.0,
                max_consecutive_losses=2, disable_reentry_after_loss=True,
                disable_averaging_down=True, daily_loss_pct=1.0,
                daily_loss_krw=50_000.0, first_trade_only=False)
    base.update(kw)
    return RecoveryConfig(**base)


def _ctx(**kw):
    base = dict(market="US", code="AAPL", has_position=False, open_position_count=0,
                intended_cost=100.0, account_equity=10_000.0, is_averaging_down=False,
                daily_realized_loss=0.0, fx_ok=True, daily_loss_limit_ccy=40.0)
    base.update(kw)
    return BuyContext(**base)


def _gate(cfg=None, state=None):
    return RecoveryGate(config=cfg or _cfg(), state=state or RecoveryState())


# ── 비활성 ─────────────────────────────────────────────────────
def test_disabled_allows():
    g = RecoveryGate(config=_cfg(enabled=False), state=RecoveryState())
    ok, why = g.check_new_buy(_ctx())
    assert ok is True and "off" in why


def test_from_env_defaults_disabled():
    c = RecoveryConfig.from_env({})
    assert c.enabled is False and c.market == "US" and c.max_positions == 2


def test_from_env_parses():
    c = RecoveryConfig.from_env({
        "RECOVERY_MODE": "true", "RECOVERY_MARKET": "us",
        "RECOVERY_MAX_POSITIONS": "3", "RECOVERY_MAX_POSITION_PCT": "12.5",
        "RECOVERY_FIRST_TRADE_ONLY": "false",
    })
    assert c.enabled and c.market == "US" and c.max_positions == 3
    assert c.max_position_pct == 12.5 and c.first_trade_only is False


# ── E20: 미국장 외 신규 BUY 차단 ──────────────────────────────
def test_non_us_blocked():
    ok, why = _gate().check_new_buy(_ctx(market="KR"))
    assert ok is False and "신규매수 금지" in why


# ── E13: 최대 포지션 2개 ──────────────────────────────────────
def test_max_positions():
    g = _gate()
    assert g.check_new_buy(_ctx(open_position_count=1))[0] is True
    ok, why = g.check_new_buy(_ctx(open_position_count=2))
    assert ok is False and "동시보유 한도" in why


# ── E14: 포지션 비중 10% 초과 차단 ────────────────────────────
def test_position_pct_cap():
    g = _gate()
    # 10,000 계좌의 10% = 1,000
    assert g.check_new_buy(_ctx(intended_cost=1000.0))[0] is True
    ok, why = g.check_new_buy(_ctx(intended_cost=1200.0))
    assert ok is False and "비중" in why


# ── E15: 물타기 차단 ──────────────────────────────────────────
def test_averaging_down_blocked():
    g = _gate()
    assert g.check_new_buy(_ctx(has_position=True))[0] is False
    ok, why = g.check_new_buy(_ctx(is_averaging_down=True))
    assert ok is False and "물타기" in why


# ── E16: 손실매도 종목 당일 재진입 차단 ───────────────────────
def test_reentry_after_loss_blocked():
    st = RecoveryState()
    g = _gate(state=st)
    st.record_sell("AAPL", -50.0)   # 손실매도
    ok, why = g.check_new_buy(_ctx(code="AAPL"))
    assert ok is False and "재진입 금지" in why
    # 다른 종목은 허용
    assert g.check_new_buy(_ctx(code="MSFT"))[0] is True


# ── E17: 연속 2회 손실 후 신규 BUY 차단 ───────────────────────
def test_consecutive_losses_block():
    st = RecoveryState()
    g = _gate(state=st)
    st.record_sell("A", -10.0)
    assert g.check_new_buy(_ctx(code="B"))[0] is True   # 1회
    st.record_sell("B", -10.0)
    ok, why = g.check_new_buy(_ctx(code="C"))
    assert ok is False and "연속손실" in why             # 2회 → 차단
    # 이익 실현 시 리셋
    st.record_sell("D", +5.0)
    assert st.consecutive_losses == 0


# ── E18: 당일 손실한도 도달 후 차단 ───────────────────────────
def test_daily_loss_limit():
    # 계좌 10,000, pct 1% = 100 / krw환산한도 40 → min=40
    g = _gate()
    assert g.check_new_buy(_ctx(daily_realized_loss=-39.0))[0] is True
    ok, why = g.check_new_buy(_ctx(daily_realized_loss=-40.0))
    assert ok is False and "손실한도" in why


def test_daily_loss_uses_min_of_pct_and_krw():
    # pct 1% = 100, krw환산 = 40 → 더 작은 40 적용
    g = _gate()
    ok, _ = g.check_new_buy(_ctx(daily_realized_loss=-50.0, daily_loss_limit_ccy=40.0))
    assert ok is False   # 40 한도 초과
    # krw환산을 크게 두면 pct(100)가 바인딩
    g2 = _gate()
    assert g2.check_new_buy(_ctx(daily_realized_loss=-50.0, daily_loss_limit_ccy=200.0))[0] is True
    assert g2.check_new_buy(_ctx(daily_realized_loss=-100.0, daily_loss_limit_ccy=200.0))[0] is False


# ── 환율 미확보 → 차단 ────────────────────────────────────────
def test_fx_missing_blocks():
    ok, why = _gate().check_new_buy(_ctx(fx_ok=False))
    assert ok is False and "환율" in why


# ── E21: 최초 왕복거래 확인 전 두 번째 신규 BUY 차단 ──────────
def test_first_trade_only():
    st = RecoveryState()
    g = _gate(cfg=_cfg(first_trade_only=True), state=st)
    # 첫 매수 허용
    assert g.check_new_buy(_ctx(open_position_count=0))[0] is True
    st.record_buy("AAPL")
    # 왕복 확인 전 두 번째 매수 차단
    ok, why = g.check_new_buy(_ctx(code="MSFT", open_position_count=1))
    assert ok is False and "FIRST_TRADE_ONLY" in why
    # 왕복 완결(매도) 후 허용
    st.record_sell("AAPL", +10.0)
    assert g.check_new_buy(_ctx(code="MSFT", open_position_count=0))[0] is True


def test_account_equity_zero_blocks():
    ok, why = _gate().check_new_buy(_ctx(account_equity=0.0))
    assert ok is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
