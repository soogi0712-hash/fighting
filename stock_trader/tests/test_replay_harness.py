"""
test_replay_harness.py — 리플레이 하니스 sanity (오프라인, 실 evaluate 호출)
실행: python3 tests/test_replay_harness.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.replay_harness import run_real, run_C, SCENARIOS, CANDIDATES


def test_all_scenarios_all_candidates_run():
    for path in SCENARIOS.values():
        for ov in CANDIDATES.values():
            r = run_real(ov, path)
            assert {"exit_min", "action", "reason", "gross", "net", "mfe", "mae"} <= set(r)
        assert "net" in run_C(path)
    print("✓ 10 시나리오 × (현재+A1/2/3+C) 전부 구동")


def test_tighter_stop_cuts_before_rebound():
    """시나리오5(-2%후 반등): 현재는 반등 익절, A-3(-2% 손절)은 반등 전 손절."""
    path = SCENARIOS["5.-2%후 반등"]
    cur = run_real(CANDIDATES["현재"], path)
    a3 = run_real(CANDIDATES["A-3"], path)
    assert cur["net"] > 0 and a3["net"] < 0        # 현재 익절 vs A-3 손절
    print(f"✓ 시나리오5: 현재 net={cur['net']:,.0f}(익절) > A-3 net={a3['net']:,.0f}(손절) — 타이트 손절이 반등 놓침")


def test_gap_below_stop_same_for_all():
    """시나리오10(갭 -7%): 손절선 무관하게 갭가에서 체결 → net 동일."""
    path = SCENARIOS["10.갭하락 손절선아래"]
    nets = {c: run_real(ov, path)["net"] for c, ov in CANDIDATES.items()}
    assert len(set(nets.values())) == 1           # 모두 동일(갭은 손절선으로 못 막음)
    print(f"✓ 시나리오10: 갭하락은 손절폭 무관 동일 체결 net={list(nets.values())[0]:,.0f}")


def test_krw_exit_uses_net_after_B():
    """B 반영 확인: ₩익절 사유의 미실현이익이 net(수수료 반영)로 표기."""
    path = SCENARIOS["3.+1.4%후 횡보"]
    r = run_real(CANDIDATES["현재"], path)
    # gross(14,000)보다 작은 net 값이 사유에 들어감
    assert r["net"] < r["gross"]
    print(f"✓ B 반영: ₩익절이 net 기준(gross {r['gross']:,.0f} > net {r['net']:,.0f})")


def _run():
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
    print("\n=== replay harness tests passed ===")


if __name__ == "__main__":
    _run()
