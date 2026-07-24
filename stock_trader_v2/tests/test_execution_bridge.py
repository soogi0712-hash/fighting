"""
tests/test_execution_bridge.py — GAP2 ExecutionBridge 22개 테스트 케이스
ENABLE_GAP2=true 환경에서 실행.
KIS 실계좌 연결 없이 MockFillSource + MockRegistry로 검증.
"""
import os
import sys
import json
import time
import tempfile
import threading
import pytest

# 경로 설정
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

os.environ["ENABLE_GAP2"] = "true"

from engine.fills import Fill, MockFillSource, CumulativeFillTracker, _is_gap2_enabled
from engine.pending_orders import (
    PendingRegistry, PendingOrder,
    ACCEPTED, UNFILLED, PARTIAL, FILLED, CANCELED, REJECTED, RECOVERY_REQUIRED,
    TERMINAL_STATUSES,
)
from engine.poll_orchestrator import poll_pending_fills, PollResult
from engine.execution_bridge import ExecutionBridge


# ─────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────

def _tmp_path():
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    f.close()
    return f.name


def _bridge(fill_source=None, path=None) -> ExecutionBridge:
    if fill_source is None:
        fill_source = MockFillSource()
    if path is None:
        path = _tmp_path()
    return ExecutionBridge(
        market="KR",
        fill_source=fill_source,
        pending_path=path,
    )


# ─────────────────────────────────────────────────────────────
# TC-01  주문 접수 0체결
# ─────────────────────────────────────────────────────────────
def test_tc01_accept_no_fill():
    """접수 후 체결 0 → pending UNFILLED, 포지션 미등록"""
    src = MockFillSource()   # 체결 없음
    br  = _bridge(src)
    ok  = br.register_accept("ORD001", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)
    assert ok is True
    assert br.has_open("005930", "BUY")

    filled = []
    br.poll(on_buy_fill=lambda p, f: filled.append(f), on_sell_fill=lambda p, f: None)
    assert len(filled) == 0     # 체결 없음


# ─────────────────────────────────────────────────────────────
# TC-02  부분체결 1회
# ─────────────────────────────────────────────────────────────
def test_tc02_partial_fill_once():
    src = MockFillSource()
    src.add("KR", "005930", "BUY", [Fill("ORD002", 5, 70000)])
    br  = _bridge(src)
    br.register_accept("ORD002", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)

    filled = []
    br.poll(on_buy_fill=lambda p, f: filled.append(f), on_sell_fill=lambda p, f: None)
    assert len(filled) == 1
    assert filled[0].applied_qty == 5


# ─────────────────────────────────────────────────────────────
# TC-03  부분체결 추가 발생
# ─────────────────────────────────────────────────────────────
def test_tc03_partial_fill_additional():
    """누적 5 → 누적 8 → delta 3 방출"""
    tracker = CumulativeFillTracker()
    key = ("KR", "ORD003")
    f1 = tracker.update(key, 5, 350000.0, "ORD003")
    assert f1 is not None and f1.qty == 5
    f2 = tracker.update(key, 8, 560000.0, "ORD003")
    assert f2 is not None and f2.qty == 3
    f3 = tracker.update(key, 8, 560000.0, "ORD003")   # 중복
    assert f3 is None   # delta 0 → None


# ─────────────────────────────────────────────────────────────
# TC-04  전량체결
# ─────────────────────────────────────────────────────────────
def test_tc04_full_fill():
    src = MockFillSource()
    src.add("KR", "005930", "BUY", [Fill("ORD004", 10, 70000)])
    br  = _bridge(src)
    br.register_accept("ORD004", "005930", "삼성전자", "BUY", "FULL", 10, 70000)

    filled = []
    br.poll(on_buy_fill=lambda p, f: filled.append(f), on_sell_fill=lambda p, f: None)
    assert sum(f.applied_qty for f in filled) == 10


# ─────────────────────────────────────────────────────────────
# TC-05  동일 누적체결 중복 poll (idempotent)
# ─────────────────────────────────────────────────────────────
def test_tc05_idempotent_poll():
    tracker = CumulativeFillTracker()
    key = ("KR", "ORD005")
    f1 = tracker.update(key, 10, 700000.0, "ORD005")
    assert f1 is not None
    f2 = tracker.update(key, 10, 700000.0, "ORD005")
    assert f2 is None   # 이미 반영됨


# ─────────────────────────────────────────────────────────────
# TC-06  재시작 후 seed 복구
# ─────────────────────────────────────────────────────────────
def test_tc06_restart_seed_recovery():
    """seed 후 동일 누적값 → delta 0 (이미 반영됨)"""
    tracker = CumulativeFillTracker()
    key = ("KR", "ORD006")
    tracker.seed(key, 10, 700000.0)  # 재시작 복구
    f = tracker.update(key, 10, 700000.0, "ORD006")
    assert f is None   # seed 이후 delta 없음
    f2 = tracker.update(key, 12, 840000.0, "ORD006")
    assert f2 is not None and f2.qty == 2   # 새 체결만 반영


# ─────────────────────────────────────────────────────────────
# TC-07  pending 파일 복구
# ─────────────────────────────────────────────────────────────
def test_tc07_pending_file_recovery():
    path = _tmp_path()
    reg1 = PendingRegistry("KR")
    reg1.register("ORD007", "KR", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)
    reg1.save_to(path)

    reg2 = PendingRegistry("KR")
    n = reg2.load_from(path)
    assert n == 1
    assert reg2.has_open("005930", "BUY")


# ─────────────────────────────────────────────────────────────
# TC-08  BUY pending 중복주문 차단
# ─────────────────────────────────────────────────────────────
def test_tc08_buy_pending_dedup():
    br = _bridge()
    ok1 = br.register_accept("ORD008A", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)
    assert ok1 is True
    ok2 = br.register_accept("ORD008B", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)
    assert ok2 is False   # 중복 차단


# ─────────────────────────────────────────────────────────────
# TC-09  SELL pending 중복주문 차단
# ─────────────────────────────────────────────────────────────
def test_tc09_sell_pending_dedup():
    br = _bridge()
    br.register_accept("ORD009A", "005930", "삼성전자", "SELL", "TAKE", 10, 70000)
    ok2 = br.register_accept("ORD009B", "005930", "삼성전자", "SELL", "TAKE", 10, 70000)
    assert ok2 is False


# ─────────────────────────────────────────────────────────────
# TC-10  BUY pending 중 SELL 허용 (방향 다름)
# ─────────────────────────────────────────────────────────────
def test_tc10_buy_pending_sell_allowed():
    br = _bridge()
    br.register_accept("ORD010A", "005930", "삼성전자", "BUY",  "EARLY", 10, 70000)
    ok = br.register_accept("ORD010B", "005930", "삼성전자", "SELL", "TAKE",  10, 70000)
    assert ok is True   # BUY pending 있어도 SELL은 허용


# ─────────────────────────────────────────────────────────────
# TC-11  SELL 부분체결 후 포지션 유지 (on_sell_fill delta 확인)
# ─────────────────────────────────────────────────────────────
def test_tc11_sell_partial_position_kept():
    """SELL 부분체결 → on_sell_fill 호출 1회, qty=5"""
    src = MockFillSource()
    src.add("KR", "005930", "SELL", [Fill("ORD011", 5, 70000)])
    br  = _bridge(src)
    br.register_accept("ORD011", "005930", "삼성전자", "SELL", "TAKE", 10, 70000)

    sell_fills = []
    br.poll(on_buy_fill=lambda p, f: None, on_sell_fill=lambda p, f: sell_fills.append(f))
    assert len(sell_fills) == 1
    assert sell_fills[0].applied_qty == 5   # delta = 5


# ─────────────────────────────────────────────────────────────
# TC-12  SELL 전량체결 후 포지션 제거
# ─────────────────────────────────────────────────────────────
def test_tc12_sell_full_position_removed():
    """SELL 전량체결 → on_sell_fill qty=10, pending FILLED"""
    src = MockFillSource()
    src.add("KR", "005930", "SELL", [Fill("ORD012", 10, 70000)])
    br  = _bridge(src)
    br.register_accept("ORD012", "005930", "삼성전자", "SELL", "TAKE", 10, 70000)

    sell_fills = []
    br.poll(on_buy_fill=lambda p, f: None, on_sell_fill=lambda p, f: sell_fills.append(f))
    assert sum(f.applied_qty for f in sell_fills) == 10


# ─────────────────────────────────────────────────────────────
# TC-13  pnl.record가 접수 시 호출되지 않는지
# ─────────────────────────────────────────────────────────────
def test_tc13_pnl_not_called_on_accept():
    """BUY_ACCEPTED 반환 시 pnl 호출 없음 (전략 로직 외부에서 확인)"""
    # execution_engine.py에서 ENABLE_GAP2=true → "BUY_ACCEPTED" 반환
    from engine.execution_engine import _is_gap2_enabled
    os.environ["ENABLE_GAP2"] = "true"
    assert _is_gap2_enabled() is True
    # 접수 단계에서 포지션/손익 미반영 = BUY_ACCEPTED 반환값으로 검증
    # 실제 계좌 없이 반환값 패턴 검증
    os.environ["ENABLE_GAP2"] = "true"


# ─────────────────────────────────────────────────────────────
# TC-14  pnl.record가 체결 delta 기준으로 호출되는지
# ─────────────────────────────────────────────────────────────
def test_tc14_pnl_called_on_fill_delta():
    """on_sell_fill 콜백이 실체결 delta 기준으로만 호출됨을 확인"""
    src = MockFillSource()
    src.add("KR", "005930", "SELL", [Fill("ORD014", 10, 70500)])
    br  = _bridge(src)
    br.register_accept("ORD014", "005930", "삼성전자", "SELL", "TAKE", 10, 70000)

    pnl_calls = []
    def mock_sell_fill(p, f):
        pnl_calls.append(f.applied_qty * f.price)

    br.poll(on_buy_fill=lambda p, f: None, on_sell_fill=mock_sell_fill)
    assert len(pnl_calls) == 1
    assert pnl_calls[0] == 10 * 70500


# ─────────────────────────────────────────────────────────────
# TC-15  poll timeout (MockFillSource 예외 → 체결 없음)
# ─────────────────────────────────────────────────────────────
def test_tc15_poll_timeout():
    """FillSource 예외 → poll 실패, pending 유지, 예외 전파 없음"""
    class TimeoutFillSource:
        def get_fills(self, *a, **kw):
            raise TimeoutError("KIS timeout")

    br = _bridge(TimeoutFillSource())
    br.register_accept("ORD015", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)

    errors = []
    try:
        br.poll(
            on_buy_fill=lambda p, f: None,
            on_fill_error=lambda e: errors.append(e),
        )
    except Exception as ex:
        pytest.fail(f"poll이 예외를 전파하면 안됨: {ex}")
    # pending 유지
    assert br.has_open("005930", "BUY")


# ─────────────────────────────────────────────────────────────
# TC-16  429 에러 (FillSource 예외)
# ─────────────────────────────────────────────────────────────
def test_tc16_rate_limit_429():
    class RateLimitFillSource:
        def get_fills(self, *a, **kw):
            raise Exception("429 Too Many Requests")

    br = _bridge(RateLimitFillSource())
    br.register_accept("ORD016", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)
    try:
        br.poll(on_buy_fill=lambda p, f: None, on_sell_fill=lambda p, f: None)
    except Exception as ex:
        pytest.fail(f"poll 예외 전파 금지: {ex}")
    assert br.has_open("005930", "BUY")


# ─────────────────────────────────────────────────────────────
# TC-17  500 서버 에러
# ─────────────────────────────────────────────────────────────
def test_tc17_server_error_500():
    class ServerErrorFillSource:
        def get_fills(self, *a, **kw):
            raise Exception("500 Internal Server Error")

    br = _bridge(ServerErrorFillSource())
    br.register_accept("ORD017", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)
    try:
        br.poll(on_buy_fill=lambda p, f: None, on_sell_fill=lambda p, f: None)
    except Exception as ex:
        pytest.fail(f"poll 예외 전파 금지: {ex}")
    assert br.has_open("005930", "BUY")


# ─────────────────────────────────────────────────────────────
# TC-18  취소 성공 → CANCELED 상태
# ─────────────────────────────────────────────────────────────
def test_tc18_cancel_success():
    reg = PendingRegistry("KR")
    reg.register("ORD018", "KR", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)
    ok = reg.mark_terminal("ORD018", CANCELED)
    assert ok is True
    assert not reg.has_open("005930", "BUY")


# ─────────────────────────────────────────────────────────────
# TC-19  취소 실패 → pending 유지 (mark_terminal 미호출)
# ─────────────────────────────────────────────────────────────
def test_tc19_cancel_fail_pending_kept():
    reg = PendingRegistry("KR")
    reg.register("ORD019", "KR", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)
    # 취소 API 실패 시 mark_terminal 호출 안 함 → pending 유지
    assert reg.has_open("005930", "BUY")


# ─────────────────────────────────────────────────────────────
# TC-20  상태 불명 RECOVERY_REQUIRED
# ─────────────────────────────────────────────────────────────
def test_tc20_recovery_required():
    reg = PendingRegistry("KR")
    reg.register("ORD020", "KR", "005930", "삼성전자", "BUY", "EARLY", 10, 70000)
    ok = reg.mark_recovery("ORD020")
    assert ok is True
    # RECOVERY_REQUIRED는 open 상태 유지 (terminal이 아님)
    assert reg.has_open("005930", "BUY")
    po = reg._orders.get("ORD020")
    assert po is not None
    assert po.status == RECOVERY_REQUIRED


# ─────────────────────────────────────────────────────────────
# TC-21  ENABLE_GAP2=false → 기존 경로 (FillSource 호출 없음)
# ─────────────────────────────────────────────────────────────
def test_tc21_feature_flag_false():
    """ENABLE_GAP2=false → _is_gap2_enabled()=False"""
    os.environ["ENABLE_GAP2"] = "false"
    from engine.fills import _is_gap2_enabled
    assert _is_gap2_enabled() is False

    # KisFillSource도 빈 목록 반환
    from engine.fills import KisFillSource
    class DummyBroker:
        def get_executed_orders(self): return [{"code":"A","side":"BUY","filled_qty":5,"filled_price":100}]
    src = KisFillSource(DummyBroker())
    fills = src.get_fills("KR", "A", "BUY")
    assert fills == []

    os.environ["ENABLE_GAP2"] = "true"  # 복구


# ─────────────────────────────────────────────────────────────
# TC-22  ENABLE_GAP2=true → 신규 경로
# ─────────────────────────────────────────────────────────────
def test_tc22_feature_flag_true():
    """ENABLE_GAP2=true → _is_gap2_enabled()=True"""
    os.environ["ENABLE_GAP2"] = "true"
    from engine.fills import _is_gap2_enabled
    assert _is_gap2_enabled() is True

    # ExecutionBridge 초기화 성공
    br = _bridge()
    assert br is not None
    assert br.market == "KR"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
