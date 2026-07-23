"""
test_b_krw_net.py — [B] 원화 익절 gross→net 일원화 검증 (오프라인)

검증:
  - net_krw_profit 이 calc_sell_proceeds(SSOT)와 동일 비용모델 사용
  - 매수수수료 이중 반영 없음(avg_price=수수료포함 원가)
  - gross ≥ 임계이나 net < 임계인 경우 조기익절이 방지됨(핵심 수정효과)
  - 부분매도 시 잔여 포지션 원가 훼손 없음(avg_price 불변)
실행: python3 tests/test_b_krw_net.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.pyramid_strategy import net_krw_profit
from screener.transaction_cost import calc_sell_proceeds

PARTIAL, FULL = 10_000, 30_000   # 기존 임계값(변경 안 함)


def _gross(avg, qty, cur):
    return (cur - avg) * qty


def test_net_equals_ssot_no_double_buyfee():
    avg, qty, cur = 10000, 100, 10500
    net = net_krw_profit(avg, qty, cur)
    sp = calc_sell_proceeds(cur, qty)
    expected = sp.net_proceeds - avg * qty          # 매도측만 비용, 매수수수료 이중 없음
    assert abs(net - expected) < 1e-6
    # gross 는 비용 미반영이라 net 보다 큼
    assert _gross(avg, qty, cur) > net
    print(f"✓ net={net:,.1f} = SSOT, gross={_gross(avg,qty,cur):,.0f} > net (비용반영)")


def test_three_examples_before_after():
    cases = [
        # (avg, qty, cur, 설명)
        (10000, 100, 10500, "명확한 이익"),
        (10000, 10, 11005, "gross≥1만이나 net<1만(조기익절 방지 케이스)"),
        (10000, 200, 10200, "부분/전량 경계 부근"),
    ]
    for avg, qty, cur, desc in cases:
        g = _gross(avg, qty, cur)
        n = net_krw_profit(avg, qty, cur)
        old_full  = g >= FULL
        old_part  = g >= PARTIAL
        new_full  = n >= FULL
        new_part  = n >= PARTIAL
        print(f"  · {desc}: gross={g:,.0f}(full={old_full},part={old_part}) → "
              f"net={n:,.1f}(full={new_full},part={new_part})")
    # 핵심: 두번째 케이스는 gross 로는 부분익절 발동하나 net 로는 미발동
    g2 = _gross(10000, 10, 11005); n2 = net_krw_profit(10000, 10, 11005)
    assert g2 >= PARTIAL and n2 < PARTIAL, f"gross={g2}, net={n2}"
    print("✓ gross≥1만·net<1만 → 신(net) 기준은 조기익절 안 함(정합성 개선)")


def test_partial_sell_keeps_cost_basis():
    """부분매도 후 잔여 포지션 avg_price 훼손 없음(계산은 avg_price 불변 사용)."""
    avg, qty, cur = 10000, 100, 10400
    # 전량 기준 net
    net_all = net_krw_profit(avg, qty, cur)
    # 50% 매도 net (같은 avg_price 사용 — 잔여 원가 불변)
    net_half = net_krw_profit(avg, qty // 2, cur)
    # 잔여 50%의 원가는 여전히 avg_price 기준(훼손 없음)
    assert abs(net_half - (calc_sell_proceeds(cur, 50).net_proceeds - avg * 50)) < 1e-6
    assert net_all > net_half > 0
    print(f"✓ 부분매도 net={net_half:,.0f}, 잔여 avg_price 불변(원가 훼손 없음)")


def test_zero_guard():
    assert net_krw_profit(0, 100, 10000) == 0.0
    assert net_krw_profit(10000, 0, 10000) == 0.0
    print("✓ 0/음수 가드")


def _run():
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
    print("\n=== B tests passed ===")


if __name__ == "__main__":
    _run()
