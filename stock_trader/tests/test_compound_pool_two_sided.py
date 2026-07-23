"""
보완 #1 검증: compound_pool 양방향화 (역마틴게일 제거)
====================================================

목적:
  apply_sell() 이 실질 순손익을 복리풀에 '양방향'으로 반영하는지 검증한다.
    - 이익 실현: 풀 증가 (기존 동작 유지)
    - 손실 실현: 풀에서 차감 (신규 동작) — 0 하한
    - 손실이 풀보다 크면 풀은 0에서 멈춘다 (음수 금지)

배경(수정 전 버그):
  손실은 로그만 남기고 풀에서 차감하지 않아, 드로다운 중에도
  compound_pool 이 유지되어 매수 여력이 부풀려지는 역마틴게일 위험이 있었다.
"""

import os
import sys
import tempfile
import importlib

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

import strategies.pyramid_strategy as ps
from screener.transaction_cost import calc_sell_proceeds, calc_buy_cost


class _DummyApi:
    """apply_sell 은 api 를 사용하지 않으므로 최소 스텁."""
    pass


def _make_mgr(tmpdir):
    """파일 경로를 임시 디렉터리로 우회한 매니저 생성."""
    ps.PYRAMID_FILE  = os.path.join(tmpdir, "pyramid_positions.json")
    ps.COMPOUND_FILE = os.path.join(tmpdir, "compound_pool.json")
    return ps.PyramidStrategyManager(_DummyApi(), max_per_stock=1_000_000,
                                     max_total=5_000_000)


def _open_position(mgr, code="005930", name="삼성전자", price=70_000, qty=10):
    """레벨1 진입: avg_price 는 수수료 포함 취득원가로 설정된다."""
    bc = calc_buy_cost(price, qty)
    pos = ps.PyramidPosition(code, name, price)
    pos.add_level(1, qty, price, bc.total_cost)
    mgr.positions[code] = pos
    return pos, bc


def test_profit_adds_to_pool():
    """이익 실현 → 풀 증가 (기존 동작 유지)."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_mgr(d)
        pos, bc = _open_position(mgr, price=70_000, qty=10)
        mgr.compound_pool = 0.0
        # +10% 상승 매도
        sell_price = 77_000
        res = mgr.apply_sell(pos.code, qty=10, price=sell_price, is_full=True)
        assert res["net_profit"] > 0, "상승 매도는 순이익이어야 함"
        assert abs(mgr.compound_pool - res["net_profit"]) < 1.0, \
            "풀 증가분 = 실질 순이익"


def test_loss_subtracts_from_pool():
    """손실 실현 → 풀에서 차감 (신규 동작). 핵심 회귀 검증."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_mgr(d)
        pos, bc = _open_position(mgr, price=70_000, qty=10)
        seed = 500_000.0
        mgr.compound_pool = seed
        # -5% 하락 매도
        sell_price = 66_500
        res = mgr.apply_sell(pos.code, qty=10, price=sell_price, is_full=True)
        assert res["net_profit"] < 0, "하락 매도는 순손실이어야 함"
        expected = seed + res["net_profit"]  # net_profit 이 음수
        assert abs(mgr.compound_pool - expected) < 1.0, \
            f"풀은 손실만큼 차감돼야 함: {mgr.compound_pool} != {expected}"
        assert mgr.compound_pool < seed, "손실 후 풀은 감소해야 함(역마틴게일 제거)"


def test_loss_floored_at_zero():
    """손실 > 풀 잔액 → 풀은 0 하한, 절대 음수 금지."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_mgr(d)
        pos, bc = _open_position(mgr, price=70_000, qty=10)
        mgr.compound_pool = 1_000.0   # 손실보다 작은 풀
        sell_price = 63_000           # -10% 큰 손실
        res = mgr.apply_sell(pos.code, qty=10, price=sell_price, is_full=True)
        assert res["net_profit"] < -1_000, "손실이 풀 잔액보다 커야 하는 시나리오"
        assert mgr.compound_pool == 0.0, "풀은 0에서 멈춰야 함(음수 금지)"


def test_breakeven_leaves_pool_unchanged():
    """손익분기(순손익 0 근처) → 풀 변화 최소."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_mgr(d)
        pos, bc = _open_position(mgr, price=70_000, qty=10)
        seed = 200_000.0
        mgr.compound_pool = seed
        # avg_price(수수료 포함) 그대로 매도 → 매도수수료·세금만큼 소폭 손실
        res = mgr.apply_sell(pos.code, qty=10, price=pos.avg_price, is_full=True)
        # 매도 비용 때문에 아주 작은 손실 → 풀도 그만큼만 감소
        assert mgr.compound_pool <= seed
        assert seed - mgr.compound_pool < 5_000, "손익분기 부근 변화는 소액"


def test_pool_never_negative_across_sequence():
    """이익→손실 연속 매매에서 풀이 절대 음수로 가지 않음."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_mgr(d)
        mgr.compound_pool = 0.0
        # 1) 이익 매도로 풀 적립
        p1, _ = _open_position(mgr, code="AAA", price=70_000, qty=10)
        mgr.apply_sell("AAA", qty=10, price=77_000, is_full=True)
        assert mgr.compound_pool > 0
        # 2) 큰 손실 매도 여러 번 → 풀은 0에서 멈춤
        for i in range(3):
            code = f"B{i}"
            _open_position(mgr, code=code, price=70_000, qty=10)
            mgr.apply_sell(code, qty=10, price=60_000, is_full=True)
            assert mgr.compound_pool >= 0.0, "풀은 항상 0 이상"
        assert mgr.compound_pool == 0.0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
