"""
서브스텝 S2 검증: CumulativeFillTracker.update() 원자성
=======================================================

목적:
  두 poll 스레드가 동일 order_no(key)에 대해 동시에 update()를 호출해도
  '동일 delta Fill' 이 두 번 생성되지 않아야 한다(정확히 1회).

불변(변경 금지) 확인:
  - _seen 은 (cum_qty, cum_amount) 만 저장
  - delta 계산 로직 불변 (dq = cum_qty-prev_q, avg = (cum_amount-prev_a)/dq)
  - Fill.price = '이번 delta 평균 체결가'
  - 공개 인터페이스 불변 (update(key, cum_qty, cum_amount, order_no, ts))
"""
import os
import sys
import threading

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

from ledger.fills import CumulativeFillTracker, Fill


# ── 델타/평균가 로직 불변 확인 (사용자 예제) ───────────────────
def test_delta_avg_price_example():
    """이전(60,100), 현재(100,106) → delta 40 @ 115. (100×106-60×100)/40=115."""
    tr = CumulativeFillTracker()
    key = ("KR", "O1")
    f1 = tr.update(key, 60, 60 * 100)              # 누적 60 @ 100
    assert f1.qty == 60 and abs(f1.price - 100.0) < 1e-9
    f2 = tr.update(key, 100, 100 * 106)            # 누적 100 @ 106
    assert f2.qty == 40
    assert abs(f2.price - 115.0) < 1e-9            # ★ (10600-6000)/40 = 115
    # 동일 누적 재조회 → no-op
    assert tr.update(key, 100, 100 * 106) is None


def test_seen_stores_only_cum_qty_amount():
    """_seen 은 (cum_qty, cum_amount) 튜플만 저장."""
    tr = CumulativeFillTracker()
    key = ("KR", "O1")
    tr.update(key, 40, 40 * 70000)
    assert tr._seen[key] == (40, 40 * 70000)
    tr.update(key, 100, 40 * 70000 + 60 * 71000)
    assert tr._seen[key] == (100, 40 * 70000 + 60 * 71000)


# ── 동시 update(): 동일 delta Fill 정확히 1회 ─────────────────
def test_concurrent_update_same_cumulative_emits_once():
    """
    N 스레드가 동일 누적데이터로 동시에 update() → Fill 은 정확히 1회,
    나머지는 None(no-op).
    """
    tr = CumulativeFillTracker()
    key = ("KR", "O1")
    cum_qty, cum_amt = 100, 100 * 70000

    results = []
    res_lock = threading.Lock()
    barrier = threading.Barrier(50)

    def worker():
        barrier.wait()                         # 최대한 동시에 진입
        r = tr.update(key, cum_qty, cum_amt, order_no="O1")
        with res_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    fills = [r for r in results if r is not None]
    nones = [r for r in results if r is None]
    assert len(fills) == 1, f"delta Fill 은 정확히 1회여야 함 (실제 {len(fills)})"
    assert len(nones) == 49
    assert fills[0].qty == 100
    assert abs(fills[0].price - 70000.0) < 1e-9
    assert tr._seen[key] == (100, 100 * 70000)


# ── 부분 진행 후 동시 update(): 델타 1회 ──────────────────────
def test_concurrent_update_after_partial_emits_delta_once():
    """
    이미 60 consume 된 상태에서 N 스레드가 누적 100 을 동시에 조회 →
    delta 40 @ 115 Fill 정확히 1회, 나머지 None.
    """
    tr = CumulativeFillTracker()
    key = ("KR", "O1")
    tr.update(key, 60, 60 * 100)               # 선행: 60 consume

    results = []
    res_lock = threading.Lock()
    barrier = threading.Barrier(40)

    def worker():
        barrier.wait()
        r = tr.update(key, 100, 100 * 106, order_no="O1")
        with res_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    fills = [r for r in results if r is not None]
    assert len(fills) == 1
    assert fills[0].qty == 40
    assert abs(fills[0].price - 115.0) < 1e-9
    assert sum(1 for r in results if r is None) == 39


# ── 서로 다른 key 는 독립적으로 각각 1회 ──────────────────────
def test_concurrent_distinct_keys_independent():
    tr = CumulativeFillTracker()
    N = 30
    results = {}
    res_lock = threading.Lock()
    barrier = threading.Barrier(N)

    def worker(i):
        key = ("KR", f"O{i}")
        barrier.wait()
        r = tr.update(key, 10, 10 * 1000, order_no=f"O{i}")
        with res_lock:
            results[i] = r

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 각 key 는 최초 1회이므로 전부 Fill(10)
    assert all(results[i] is not None and results[i].qty == 10 for i in range(N))
    assert len(tr._seen) == N


# ── 순서 통제(순차) — 산술 확정: 40 @106, 20 @115 ─────────────
def test_ordered_cumulatives_deterministic_40_20():
    """
    순서 통제(순차 호출). prev=(60,6000) 에서 100→120 순으로 처리하면
    delta 40 @106, delta 20 @115 로 확정된다. (동시성 테스트 아님)
      prev 60 @100 (누적금액 6000)
      snap 100:    누적금액 10240 = 6000 + 40×106  → delta 40 @106
      snap 120:    누적금액 12540 = 10240 + 20×115 → delta 20 @115
    """
    tr = CumulativeFillTracker()
    key = ("KR", "S")
    tr.update(key, 60, 6000)
    a = tr.update(key, 100, 10240)
    b = tr.update(key, 120, 12540)
    assert a.qty == 40 and abs(a.price - 106.0) < 1e-9
    assert b.qty == 20 and abs(b.price - 115.0) < 1e-9


# ── 비결정적 동시성 — 실행순서 무관 불변조건만 검증 ───────────
def test_concurrent_different_cumulatives_nondeterministic():
    """
    prev=(60,6000) 에서 update(100,10240) 와 update(120,12540) 를 동시 호출.
    실행순서에 따라 두 결과가 모두 정상:
      경우1(100 먼저): delta 40 @106, delta 20 @115   → qty [20,40]
      경우2(120 먼저): delta 60 @109, 100 스냅숏은 stale(-20≤0)→None → qty [60]
    무조건 [40,20] 을 요구하지 않는다. 불변조건만 검증한다.
    """
    for _ in range(300):
        tr = CumulativeFillTracker()
        key = ("KR", "O1")
        tr.update(key, 60, 6000)               # prev

        results = []
        res_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(cum, amt):
            barrier.wait()                     # 최대한 동시에 진입
            r = tr.update(key, cum, amt, order_no="O1")
            with res_lock:
                results.append(r)

        t1 = threading.Thread(target=worker, args=(100, 10240))
        t2 = threading.Thread(target=worker, args=(120, 12540))
        t1.start(); t2.start(); t1.join(); t2.join()

        fills = [r for r in results if r is not None]
        qtys  = sorted(f.qty for f in fills)

        # 1) qty 합 == 정확히 60
        assert sum(f.qty for f in fills) == 60, f"qty 합 오류: {qtys}"
        # 2) qty×price 합 == 누적금액 차 (12540 - 6000 = 6540)
        assert abs(sum(f.qty * f.price for f in fills) - (12540 - 6000)) < 1e-6
        # 3) 최종 _seen == 최신 누적수량·금액 (120, 12540)
        assert tr._seen[key] == (120, 12540)
        # 4) 음수 delta 없음
        assert all(f.qty > 0 for f in fills)
        # 5) 중복 체결 없음 (합 60 초과 불가 — 위 sum==60 이 보장)
        # 6) 정상 결과는 [60] 한 건 또는 [20,40] 두 건만 허용
        assert qtys in ([60], [20, 40]), f"비정상 조합: {qtys}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
