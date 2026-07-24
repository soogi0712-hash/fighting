"""
보완 #2(구조 수정): 종목당 최대 투자비중 20% 하드 캡 검증
========================================================

목적:
  기존 `max_per_stock == max_total` 구조로 단일 종목에 사실상 전체 자본
  (≈100%)까지 노출되던 집중 위험을, 계좌자본의 20%로 제한한다.

검증 원칙(지시사항):
  - 진입 조건·BUY_SCORE·손절/익절 변경 없음 (본 파일은 사이징만 확인)
  - 30/70/100 진입구조 유지: Early 30% / Full 100% 는 '캡 대비' 비율로 동작
  - 20% 초과 주문만 제한 (그 이하 주문은 기존과 동일)
  - 현금 이내(무차입) 유지
"""

import os
import sys
import tempfile

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

import strategies.pyramid_strategy as ps
from strategies.pyramid_strategy import MAX_STOCK_WEIGHT_PCT
from screener.transaction_cost import calc_buy_cost


class _DummyApi:
    pass


def _make_mgr(tmpdir, max_per_stock, max_total):
    ps.PYRAMID_FILE  = os.path.join(tmpdir, "pyramid_positions.json")
    ps.COMPOUND_FILE = os.path.join(tmpdir, "compound_pool.json")
    return ps.PyramidStrategyManager(_DummyApi(),
                                     max_per_stock=max_per_stock,
                                     max_total=max_total)


# ── 1. 생성자 클램프 ────────────────────────────────────────────
def test_constructor_clamps_to_20pct():
    """max_per_stock == max_total 로 넘겨도 20%로 클램프된다."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_mgr(d, max_per_stock=5_000_000, max_total=5_000_000)
        assert mgr.max_per_stock == 5_000_000 * 0.20 == 1_000_000
        assert mgr.max_total == 5_000_000  # 전체 한도는 불변


def test_constructor_respects_smaller_limit():
    """호출측이 더 낮은 종목한도를 주면 그 값을 존중(min)."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _make_mgr(d, max_per_stock=300_000, max_total=5_000_000)
        assert mgr.max_per_stock == 300_000  # 20%(=1M)보다 작으므로 유지


def test_weight_pct_constant_is_20():
    assert MAX_STOCK_WEIGHT_PCT == 20.0


# ── 2. Full 진입: 단일 종목 총투자 ≤ 20% ────────────────────────
def test_full_entry_capped_at_20pct():
    """BUY SCORE≥FULL 단일 진입도 계좌의 20%를 넘지 않는다."""
    with tempfile.TemporaryDirectory() as d:
        max_total = 5_000_000
        mgr = _make_mgr(d, max_per_stock=max_total, max_total=max_total)
        # 충분한 현금 + 높은 점수 → Full 진입
        dec = mgr.evaluate("005930", "삼성전자", cur_price=70_000,
                           indicator_score=7, available_cash=max_total,
                           buy_score_norm=0.90)
        assert dec["action"].startswith("BUY_LEVEL1"), dec
        cap = max_total * 0.20
        assert dec["total_cost"] <= cap + 1, \
            f"Full 진입 총원가 {dec['total_cost']} 가 20% 캡 {cap} 초과"
        # 20%에 근접(직전 캡 대비 95% 이상 채움 — 캡이 실제로 바인딩)
        assert dec["total_cost"] >= cap * 0.90


def test_early_entry_is_30pct_of_cap():
    """Early(30%) 진입은 '캡의 30%' = 계좌의 6% 수준 (구조 유지)."""
    with tempfile.TemporaryDirectory() as d:
        max_total = 5_000_000
        mgr = _make_mgr(d, max_per_stock=max_total, max_total=max_total)
        dec = mgr.evaluate("000660", "SK하이닉스", cur_price=70_000,
                           indicator_score=3, available_cash=max_total,
                           buy_score_norm=0.55)  # EARLY≤score<FULL
        assert dec["action"] == "BUY_LEVEL1_EARLY", dec
        cap = max_total * 0.20
        # Early = 캡의 30% (수수료 포함, floor 오차 허용)
        assert dec["total_cost"] <= cap * 0.30 + 70_000
        assert dec["total_cost"] >= cap * 0.30 - 70_000


# ── 3. 피라미딩 누적도 캡 이내 ──────────────────────────────────
def test_pyramid_total_never_exceeds_cap():
    """Early→Full→추가매수 누적 총투자금이 20% 캡을 넘지 않는다."""
    with tempfile.TemporaryDirectory() as d:
        max_total = 5_000_000
        cap = max_total * 0.20
        mgr = _make_mgr(d, max_per_stock=max_total, max_total=max_total)
        code, name, price = "005930", "삼성전자", 70_000

        # 1) Early 진입 반영
        dec = mgr.evaluate(code, name, price, 3, max_total, buy_score_norm=0.55)
        assert dec["action"] == "BUY_LEVEL1_EARLY"
        mgr.apply_buy(code, name, level=1, qty=dec["qty"], price=price,
                      using_compound=dec.get("using_compound", 0))

        # 2) 여러 번 추가매수 시도 → 누적이 캡을 넘지 않아야 함
        for _ in range(6):
            pos = mgr.positions.get(code)
            if pos is None:
                break
            invested = pos.avg_price * pos.total_qty
            assert invested <= cap + 1, f"누적 투자 {invested} 가 캡 {cap} 초과"
            # 다음 단계 강제 추가매수 시뮬레이션
            nxt = pos.current_level + 1
            if nxt > 4:
                break
            add = mgr._try_add(code, name, price + 1_000 * nxt, nxt, 5, max_total, pos)
            if add.get("action", "").startswith("BUY_LEVEL"):
                mgr.apply_buy(code, name, level=nxt, qty=add["qty"],
                              price=price + 1_000 * nxt,
                              using_compound=add.get("using_compound", 0))
            else:
                break

        pos = mgr.positions.get(code)
        if pos is not None:
            invested = pos.avg_price * pos.total_qty
            assert invested <= cap + 1, f"최종 누적 {invested} 가 캡 {cap} 초과"


# ── 4. 무차입(현금 이내) 유지 ───────────────────────────────────
def test_never_orders_more_than_cash():
    """현금이 캡보다 작으면 주문금액은 현금 이내로 제한된다."""
    with tempfile.TemporaryDirectory() as d:
        max_total = 5_000_000
        mgr = _make_mgr(d, max_per_stock=max_total, max_total=max_total)
        small_cash = 200_000  # 캡(1M)보다 훨씬 작음
        dec = mgr.evaluate("035720", "카카오", cur_price=50_000,
                           indicator_score=7, available_cash=small_cash,
                           buy_score_norm=0.90)
        if dec["action"].startswith("BUY_LEVEL"):
            assert dec["total_cost"] <= small_cash + 1, \
                "현금 초과 주문 발생(무차입 위반)"


def test_below_cap_order_unchanged():
    """캡 이하 소액 주문은 기존과 동일하게 산출(20% 초과분만 제한)."""
    with tempfile.TemporaryDirectory() as d:
        max_total = 5_000_000
        cap = max_total * 0.20
        # 종목한도를 아주 크게(현금이 실질 상한) 두되 max_total=5M → 캡=1M
        mgr = _make_mgr(d, max_per_stock=max_total, max_total=max_total)
        # 현금 = 캡의 30% 미만 → Early 30%가 현금에 걸림(캡 아님)
        cash = int(cap * 0.30 * 0.5)  # 150,000
        dec = mgr.evaluate("068270", "셀트리온", cur_price=10_000,
                           indicator_score=3, available_cash=cash,
                           buy_score_norm=0.55)
        if dec["action"].startswith("BUY_LEVEL"):
            # 캡(1M)이 아니라 현금 제약이 바인딩 → total_cost ≤ cash
            assert dec["total_cost"] <= cash + 1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
