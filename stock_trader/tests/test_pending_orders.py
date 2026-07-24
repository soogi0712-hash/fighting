"""
갭2 단계 1 검증: PendingOrder / PendingRegistry (메모리 전용)
=============================================================

범위: 순수 자료구조/상태머신만. 디스크·KIS·StrategyManager·app.py 무관.

필수 시나리오(지시):
  1) 정상 등록
  2) 같은 order_no 중복 등록 멱등
  3) BUY/SELL별 has_open
  4) 부분체결 applied_qty 증가
  5) applied_qty 초과 방지
  6) 전량 반영 후 FILLED
  7) terminal 주문은 has_open에서 제외
  8) remaining_qty 계산
  9) purge_terminal
 10) clock 주입
 11) 잘못된 side/status/수량 입력 방어
 12) 기본 동시성(스레드) — 최종 구조상 스레드풀 동시 접근 가능
"""
import os
import sys
import threading

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

import pytest
from strategies.pending_orders import (
    PendingOrder, PendingRegistry,
    ACCEPTED, UNFILLED, PARTIAL, FILLED, REJECTED, CANCELED, TIMEOUT,
    TERMINAL_STATUSES,
)


def _reg(now_fn=None):
    return PendingRegistry(now_fn=now_fn)


def _register_buy(reg, order_no="O1", code="005930", qty=100, price=70_000,
                  side="BUY", level=1, name="삼성전자"):
    return reg.register(order_no, "KR", code, name, side, level, qty, price)


# ── 1. 정상 등록 ────────────────────────────────────────────────
def test_register_basic():
    reg = _reg()
    po = _register_buy(reg)
    assert isinstance(po, PendingOrder)
    assert po.order_no == "O1"
    assert po.code == "005930"
    assert po.side == "BUY"
    assert po.req_qty == 100
    assert po.req_price == 70_000
    assert po.applied_qty == 0
    assert po.status == ACCEPTED
    assert po.accepted_ts == po.last_check_ts
    assert reg.get("O1") is po
    assert len(reg) == 1


def test_register_normalizes_side_lowercase():
    reg = _reg()
    po = reg.register("O", "KR", "X", "n", "buy", 1, 10, 100)
    assert po.side == "BUY"


# ── 2. 같은 order_no 중복 등록 멱등 ─────────────────────────────
def test_register_idempotent_same_order_no():
    reg = _reg()
    po1 = _register_buy(reg, order_no="DUP", qty=100)
    # 일부 체결 반영 후
    reg.apply_delta("DUP", 60)
    assert reg.get("DUP").applied_qty == 60
    assert reg.get("DUP").status == PARTIAL
    # 같은 order_no 재등록 → 기존 유지(덮어쓰기/초기화 금지)
    po2 = _register_buy(reg, order_no="DUP", qty=999)  # 다른 값으로 재시도
    assert po2 is po1
    assert reg.get("DUP").applied_qty == 60      # 초기화 안 됨
    assert reg.get("DUP").req_qty == 100         # 덮어쓰기 안 됨
    assert reg.get("DUP").status == PARTIAL
    assert len(reg) == 1


# ── 3. BUY/SELL별 has_open ──────────────────────────────────────
def test_has_open_by_side():
    reg = _reg()
    _register_buy(reg, order_no="B1", code="005930", side="BUY")
    reg.register("S1", "KR", "000660", "하이닉스", "SELL", 1, 50, 120_000)
    assert reg.has_open("005930", "BUY") is True
    assert reg.has_open("005930", "SELL") is False
    assert reg.has_open("005930") is True          # 양방향
    assert reg.has_open("000660", "SELL") is True
    assert reg.has_open("000660", "BUY") is False
    assert reg.has_open("999999") is False         # 없는 종목


# ── 4. 부분체결 applied_qty 증가 ────────────────────────────────
def test_partial_fill_accumulates():
    reg = _reg()
    _register_buy(reg, order_no="P", qty=100)
    reg.apply_delta("P", 30)
    assert reg.get("P").applied_qty == 30
    assert reg.get("P").status == PARTIAL
    reg.apply_delta("P", 30)
    assert reg.get("P").applied_qty == 60
    assert reg.get("P").status == PARTIAL
    assert reg.remaining_qty("P") == 40


# ── 5. applied_qty 초과 방지 ────────────────────────────────────
def test_applied_qty_cannot_exceed_req():
    reg = _reg()
    _register_buy(reg, order_no="X", qty=100)
    reg.apply_delta("X", 80)
    reg.apply_delta("X", 50)   # 80+50=130 → req 100 로 clamp
    assert reg.get("X").applied_qty == 100
    assert reg.get("X").remaining_qty() == 0
    assert reg.get("X").status == FILLED


# ── 6. 전량 반영 후 FILLED ──────────────────────────────────────
def test_full_fill_transitions_to_filled():
    reg = _reg()
    _register_buy(reg, order_no="F", qty=100)
    reg.apply_delta("F", 100)
    assert reg.get("F").status == FILLED
    assert reg.get("F").is_terminal() is True
    assert reg.remaining_qty("F") == 0


# ── 7. terminal 주문은 has_open에서 제외 ───────────────────────
def test_terminal_excluded_from_has_open():
    reg = _reg()
    _register_buy(reg, order_no="T", code="005930", qty=100)
    assert reg.has_open("005930", "BUY") is True
    reg.apply_delta("T", 100)          # → FILLED (terminal)
    assert reg.has_open("005930", "BUY") is False
    assert reg.all_open() == []

    # REJECTED/CANCELED/TIMEOUT 도 제외
    for st, on in ((REJECTED, "R"), (CANCELED, "C"), (TIMEOUT, "TO")):
        reg.register(on, "KR", "111111", "n", "BUY", 1, 10, 100)
        assert reg.has_open("111111", "BUY") is True
        reg.mark_terminal(on, st)
        assert reg.has_open("111111", "BUY") is False


# ── 8. remaining_qty 계산 ───────────────────────────────────────
def test_remaining_qty():
    reg = _reg()
    po = _register_buy(reg, order_no="RQ", qty=100)
    assert po.remaining_qty() == 100
    reg.apply_delta("RQ", 25)
    assert reg.remaining_qty("RQ") == 75
    with pytest.raises(KeyError):
        reg.remaining_qty("NOPE")


# ── 9. purge_terminal ───────────────────────────────────────────
def test_purge_terminal():
    reg = _reg()
    _register_buy(reg, order_no="A", code="A", qty=10)
    _register_buy(reg, order_no="B", code="B", qty=10)
    _register_buy(reg, order_no="C", code="C", qty=10)
    reg.apply_delta("A", 10)            # FILLED
    reg.mark_terminal("B", CANCELED)    # CANCELED
    # C 는 열린 상태
    n = reg.purge_terminal()
    assert n == 2
    assert reg.get("A") is None
    assert reg.get("B") is None
    assert reg.get("C") is not None
    assert len(reg) == 1


# ── 10. clock 주입 ──────────────────────────────────────────────
def test_clock_injection():
    fake = {"t": 1000.0}
    reg = _reg(now_fn=lambda: fake["t"])
    po = _register_buy(reg, order_no="CK", qty=100)
    assert po.accepted_ts == 1000.0
    assert po.last_check_ts == 1000.0
    fake["t"] = 1015.0
    reg.apply_delta("CK", 40)
    assert reg.get("CK").last_check_ts == 1015.0
    assert reg.get("CK").accepted_ts == 1000.0     # 접수시각 불변
    fake["t"] = 1030.0
    reg.update_status("CK", UNFILLED)
    assert reg.get("CK").last_check_ts == 1030.0


# ── 11. 잘못된 입력 방어 ────────────────────────────────────────
def test_invalid_inputs():
    reg = _reg()
    # side
    with pytest.raises(ValueError):
        reg.register("o", "KR", "c", "n", "HOLD", 1, 10, 100)
    # req_qty <= 0
    with pytest.raises(ValueError):
        reg.register("o", "KR", "c", "n", "BUY", 1, 0, 100)
    with pytest.raises(ValueError):
        reg.register("o", "KR", "c", "n", "BUY", 1, -5, 100)
    # req_price < 0
    with pytest.raises(ValueError):
        reg.register("o", "KR", "c", "n", "BUY", 1, 10, -1)
    # order_no 빈값
    with pytest.raises(ValueError):
        reg.register("", "KR", "c", "n", "BUY", 1, 10, 100)
    # 잘못된 status
    _register_buy(reg, order_no="S", qty=10)
    with pytest.raises(ValueError):
        reg.update_status("S", "WEIRD")
    # 없는 주문 갱신
    with pytest.raises(KeyError):
        reg.update_status("NONE", UNFILLED)
    with pytest.raises(KeyError):
        reg.apply_delta("NONE", 1)
    # 음수 델타
    with pytest.raises(ValueError):
        reg.apply_delta("S", -1)
    # terminal 에 체결 적용 금지
    reg.apply_delta("S", 10)           # FILLED
    with pytest.raises(ValueError):
        reg.apply_delta("S", 1)
    # mark_terminal 에 비-terminal status
    _register_buy(reg, order_no="S2", qty=10)
    with pytest.raises(ValueError):
        reg.mark_terminal("S2", PARTIAL)
    # has_open 잘못된 side
    with pytest.raises(ValueError):
        reg.has_open("c", "SIDEWAYS")


# ── 12. 기본 동시성 ─────────────────────────────────────────────
def test_basic_concurrency():
    """
    최종 구조상 루프 선두 poll + 전용 poll 잡이 스레드풀에서 동시에
    레지스트리를 변이할 수 있으므로, 동시 등록/체결이 유실·손상 없이
    처리되는지 확인(RLock).
    """
    reg = _reg()
    N = 50

    def worker(i):
        on = f"ORD-{i}"
        reg.register(on, "KR", f"C{i}", "n", "BUY", 1, 100, 10_000)
        # 같은 주문에 대해 여러 스레드가 부분체결을 밀어넣어도 초과 없이 누적
        for _ in range(10):
            reg.apply_delta(on, 10)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(reg) == N
    for i in range(N):
        po = reg.get(f"ORD-{i}")
        assert po is not None
        assert po.applied_qty == 100        # 10*10, req 100 로 정확히 채워짐
        assert po.status == FILLED
    # 동일 주문 동시 register 멱등 — 하나만 존재
    errs = []
    def dup():
        try:
            reg.register("SAME", "KR", "Z", "n", "BUY", 1, 100, 10_000)
        except Exception as e:  # noqa
            errs.append(e)
    ts = [threading.Thread(target=dup) for _ in range(20)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert errs == []
    assert reg.get("SAME").req_qty == 100
    assert sum(1 for k in [f"ORD-{i}" for i in range(N)] + ["SAME"]
               if reg.get(k)) == N + 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
