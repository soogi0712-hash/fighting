"""
Recovery/복구 배선 검증: pending 영속·reconciliation 게이트·fx strict.
순수 로직/파일 — KIS·네트워크 무관.
"""
import os
import sys
import tempfile

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

import pytest
from strategies.pending_orders import PendingRegistry, CANCEL_REQUESTED, FILLED
from utils import order_gate as og


# ── E6/E7 기반: pending 영속 저장/복구 라운드트립 ─────────────
def test_pending_persist_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "pending.json")
        reg = PendingRegistry()
        reg.register("O1", "US", "AAPL", "Apple", "BUY", 1, 10, 190.0)
        reg.apply_delta("O1", 3)          # 부분체결 3주
        reg.register("O2", "US", "MSFT", "MS", "BUY", 1, 5, 400.0)
        reg.save_to(path)

        # 새 인스턴스로 복구
        reg2 = PendingRegistry()
        n = reg2.load_from(path)
        assert n == 2
        o1 = reg2.get("O1")
        assert o1.applied_qty == 3 and o1.remaining_qty() == 7
        assert reg2.get("O2").req_qty == 5
        # 복구 후 추가체결 delta 만 반영(E7)
        r = reg2.apply_delta("O1", 4)     # 누적 7
        assert r.applied_delta == 4 and reg2.get("O1").applied_qty == 7


def test_pending_load_missing_file_zero():
    reg = PendingRegistry()
    assert reg.load_from("/nonexistent/path/pending.json") == 0


# ── reconciliation 게이트 (E10 근거: 삭제 대신 격리→신규BUY 차단) ─
def test_reconciliation_gate_state():
    og.clear_reconciliation()
    assert og.reconciliation_status()["required"] is False
    og.set_reconciliation_required("포지션 불일치 테스트", ["005930", "AAPL"])
    st = og.reconciliation_status()
    assert st["required"] is True
    assert "005930" in st["codes"] and "AAPL" in st["codes"]
    assert "불일치" in st["reason"]
    og.clear_reconciliation()
    assert og.reconciliation_status()["required"] is False


# ── LIVE=false 주문 차단(E22 근거) — order_gate 게이트 ─────────
def test_orders_blocked_when_not_live():
    # 기본 프로필은 READ_ONLY 이므로 orders_allowed False 이어야 함
    ok, why = og.orders_allowed()
    assert ok is False   # LIVE 미충족/READ_ONLY → 신규주문 불가(네트워크 미호출)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
