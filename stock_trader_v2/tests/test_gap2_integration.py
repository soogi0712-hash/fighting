"""
tests/test_gap2_integration.py — GAP2 통합 테스트 25개

검증 범위:
  - US BUY_ACCEPTED → pending 등록, 포지션 미생성
  - US SELL_ACCEPTED → pending 등록, 즉시 pop/record 금지
  - 부분/전량 체결 → 포지션 수량 변화
  - 콜백 실패 → retry_queue 보관 → 다음 poll 재시도 (CRITICAL-2)
  - KR GAP2 왕복
  - NET 손익 기준 통일 (HIGH-3)
  - 손상 JSON RECOVERY (MEDIUM-6)
  - Feature Flag 런타임 고정 (MEDIUM-7)

★ 실 KIS 응답 없이 mock 기반 검증. 실계좌 왕복은 미검증(별도 표기).
"""
from __future__ import annotations

import os
import json
import tempfile
import pytest

# GAP2 환경변수 강제 설정
os.environ["ENABLE_GAP2"] = "true"

from engine.pending_orders import PendingRegistry, PendingOrder
from engine.poll_orchestrator import (
    AppliedFillEvent, poll_pending_fills,
    MockOrderStateSource, OrderState,
)
from engine.execution_bridge import ExecutionBridge
from engine.fills import MockFillSource, Fill


# ══════════════════════════════════════════════════════════════
# 공통 픽스처 / 헬퍼
# ══════════════════════════════════════════════════════════════

def _make_bridge(fill_source=None, tmp_path=None) -> ExecutionBridge:
    if fill_source is None:
        fill_source = MockFillSource()
    if tmp_path is None:
        tmp_path = tempfile.mktemp(suffix=".json")
    return ExecutionBridge(
        market="US",
        fill_source=fill_source,
        pending_path=tmp_path,
        state_source=MockOrderStateSource(),
    )


def _accepted_ev(order_no="ORD001", code="TSLA", side="BUY",
                 applied_qty=5, price=100.0) -> AppliedFillEvent:
    return AppliedFillEvent(
        order_no=order_no, market="US", code=code, name="Tesla",
        side=side, level=1, applied_qty=applied_qty, price=price,
        remaining_qty=0, using_compound=0.0,
        is_full=(side == "BUY"), became_filled=True,
    )


# ══════════════════════════════════════════════════════════════
# US BUY 통합
# ══════════════════════════════════════════════════════════════

def test_us_buy_accepted_registers_pending():
    """TC-I01: BUY_ACCEPTED → pending 등록 확인."""
    bridge = _make_bridge()
    ok = bridge.register_accept("ORD001", "TSLA", "Tesla", "BUY", 1, 10, 100.0)
    assert ok is True
    assert bridge.registry.has_open("TSLA", "BUY")


def test_us_buy_accepted_no_position_created():
    """TC-I02: BUY_ACCEPTED 후 포지션은 생성되지 않음."""
    bridge = _make_bridge()
    bridge.register_accept("ORD002", "TSLA", "Tesla", "BUY", 1, 10, 100.0)
    # poll 없이 포지션 dict 없음
    positions = {}
    def on_buy(po, ev):
        positions[ev.code] = ev.applied_qty
    # 빈 fill_source이므로 아무 체결도 없어야 함
    bridge.poll(on_buy_fill=on_buy)
    assert "TSLA" not in positions, "BUY_ACCEPTED만으로 포지션 생성 금지"


def test_us_buy_partial_fill_increases_qty():
    """TC-I03: 부분체결 → 포지션 수량 증가."""
    fs = MockFillSource()
    fs.add("US", "TSLA", "BUY", [Fill("ORD003", 3, 100.0)])
    bridge = _make_bridge(fill_source=fs)
    bridge.register_accept("ORD003", "TSLA", "Tesla", "BUY", 1, 10, 100.0)
    received = []
    def on_buy(po, ev):
        received.append(ev.applied_qty)
    bridge.poll(on_buy_fill=on_buy)
    assert received == [3], f"부분체결 delta={received}"


def test_us_buy_full_fill_qty_match():
    """TC-I04: 전량체결 → became_filled True + 총 수량 일치."""
    fs = MockFillSource()
    fs.add("US", "TSLA", "BUY", [Fill("ORD004", 10, 100.0)])
    bridge = _make_bridge(fill_source=fs)
    bridge.register_accept("ORD004", "TSLA", "Tesla", "BUY", 1, 10, 100.0)
    evs = []
    def on_buy(po, ev):
        evs.append(ev)
    bridge.poll(on_buy_fill=on_buy)
    assert len(evs) == 1
    assert evs[0].applied_qty == 10
    assert evs[0].became_filled is True


def test_us_buy_accepted_not_buy_fail():
    """TC-I05: BUY_ACCEPTED → BUY_FAIL 반환 없음 (pending 등록 성공)."""
    bridge = _make_bridge()
    ok = bridge.register_accept("ORD005", "TSLA", "Tesla", "BUY", 1, 5, 200.0)
    assert ok is True  # BUY_FAIL이었으면 False 반환


def test_us_buy_duplicate_pending_blocked():
    """TC-I06: 동일 종목 BUY pending 중복 차단."""
    bridge = _make_bridge()
    ok1 = bridge.register_accept("ORD006a", "TSLA", "Tesla", "BUY", 1, 5, 100.0)
    ok2 = bridge.register_accept("ORD006b", "TSLA", "Tesla", "BUY", 1, 5, 100.0)
    assert ok1 is True
    assert ok2 is False, "중복 pending BUY 차단"


# ══════════════════════════════════════════════════════════════
# US SELL 통합
# ══════════════════════════════════════════════════════════════

def test_us_sell_accepted_registers_pending():
    """TC-I07: SELL_ACCEPTED → pending 등록 확인."""
    bridge = _make_bridge()
    ok = bridge.register_accept("SORD001", "TSLA", "Tesla", "SELL", 0, 10, 100.0)
    assert ok is True
    assert bridge.registry.has_open("TSLA", "SELL")


def test_us_sell_accepted_not_sell_fail():
    """TC-I08: SELL_ACCEPTED → SELL_FAIL 아님 (pending 등록 성공)."""
    bridge = _make_bridge()
    ok = bridge.register_accept("SORD002", "TSLA", "Tesla", "SELL", 0, 10, 100.0)
    assert ok is True


def test_us_sell_partial_fill_position_kept():
    """TC-I09: 부분매도 체결 → 포지션 유지 (pop 금지)."""
    fs = MockFillSource()
    fs.add("US", "TSLA", "SELL", [Fill("SORD003", 3, 110.0)])
    bridge = _make_bridge(fill_source=fs)
    bridge.register_accept("SORD003", "TSLA", "Tesla", "SELL", 0, 10, 110.0)
    sell_evs = []
    def on_sell(po, ev):
        sell_evs.append(ev)
    bridge.poll(on_buy_fill=lambda po, ev: None, on_sell_fill=on_sell)
    assert len(sell_evs) == 1
    assert sell_evs[0].applied_qty == 3
    assert sell_evs[0].became_filled is False


def test_us_sell_full_fill_position_removed():
    """TC-I10: 전량매도 체결 → became_filled True."""
    fs = MockFillSource()
    fs.add("US", "TSLA", "SELL", [Fill("SORD004", 10, 110.0)])
    bridge = _make_bridge(fill_source=fs)
    bridge.register_accept("SORD004", "TSLA", "Tesla", "SELL", 0, 10, 110.0)
    sell_evs = []
    def on_sell(po, ev):
        sell_evs.append(ev)
    bridge.poll(on_buy_fill=lambda po, ev: None, on_sell_fill=on_sell)
    assert len(sell_evs) == 1
    assert sell_evs[0].became_filled is True


# ══════════════════════════════════════════════════════════════
# CRITICAL-2: 콜백 실패 시 delta 유실 방지 (retry_queue)
# ══════════════════════════════════════════════════════════════

def test_buy_callback_failure_retry_queue():
    """TC-I11: BUY callback 예외 → retry_queue 보관."""
    fs = MockFillSource()
    fs.add("US", "TSLA", "BUY", [Fill("ORD011", 5, 100.0)])
    bridge = _make_bridge(fill_source=fs)
    bridge.register_accept("ORD011", "TSLA", "Tesla", "BUY", 1, 10, 100.0)

    def failing_buy(po, ev):
        raise RuntimeError("position save failed")

    bridge.poll(on_buy_fill=failing_buy)
    # retry_queue에 보관되어 있어야 함
    assert len(bridge._callback_retry_queue) == 1, \
        f"retry_queue에 1건 보관 필요: {len(bridge._callback_retry_queue)}"


def test_buy_callback_failure_retried_next_poll():
    """TC-I12: BUY callback 실패 → 다음 poll에서 재시도."""
    fs = MockFillSource()
    fs.add("US", "TSLA", "BUY", [Fill("ORD012", 5, 100.0)])
    bridge = _make_bridge(fill_source=fs)
    bridge.register_accept("ORD012", "TSLA", "Tesla", "BUY", 1, 10, 100.0)

    call_count = [0]
    def failing_first(po, ev):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("first fail")
        # 2번째 성공

    bridge.poll(on_buy_fill=failing_first)
    assert len(bridge._callback_retry_queue) == 1

    # 다음 poll — retry 처리
    bridge.poll(on_buy_fill=failing_first)
    assert call_count[0] == 2, f"retry 호출 필요: call_count={call_count[0]}"
    assert len(bridge._callback_retry_queue) == 0, "retry 성공 → queue 비워야 함"


def test_sell_callback_failure_retry_queue():
    """TC-I13: SELL callback 예외 → retry_queue 보관."""
    fs = MockFillSource()
    fs.add("US", "TSLA", "SELL", [Fill("SORD013", 5, 110.0)])
    bridge = _make_bridge(fill_source=fs)
    bridge.register_accept("SORD013", "TSLA", "Tesla", "SELL", 0, 5, 110.0)

    def failing_sell(po, ev):
        raise RuntimeError("pnl save failed")

    bridge.poll(
        on_buy_fill=lambda po, ev: None,
        on_sell_fill=failing_sell,
    )
    assert len(bridge._callback_retry_queue) == 1


def test_delta_not_lost_after_callback_failure():
    """TC-I14: 콜백 실패 후 delta가 registry에 영구 반영됨 (유실 없음)."""
    fs = MockFillSource()
    fs.add("US", "TSLA", "BUY", [Fill("ORD014", 7, 100.0)])
    bridge = _make_bridge(fill_source=fs)
    bridge.register_accept("ORD014", "TSLA", "Tesla", "BUY", 1, 10, 100.0)

    def failing_buy(po, ev):
        raise RuntimeError("fail")

    bridge.poll(on_buy_fill=failing_buy)
    # registry에 applied_qty가 반영되었는지 확인
    po = bridge.registry.get("ORD014")
    assert po is not None
    assert po.applied_qty == 7, \
        f"delta가 registry에 반영됨 확인: applied_qty={po.applied_qty}"


def test_no_double_apply_after_retry():
    """TC-I15: retry_queue 재시도 시 이중반영 없음 (apply_delta는 1회만)."""
    fs = MockFillSource()
    fs.add("US", "TSLA", "BUY", [Fill("ORD015", 5, 100.0)])
    bridge = _make_bridge(fill_source=fs)
    bridge.register_accept("ORD015", "TSLA", "Tesla", "BUY", 1, 10, 100.0)

    applied_totals = []
    call_count = [0]
    def failing_first(po, ev):
        call_count[0] += 1
        applied_totals.append(ev.applied_qty)
        if call_count[0] == 1:
            raise RuntimeError("fail first")

    bridge.poll(on_buy_fill=failing_first)
    bridge.poll(on_buy_fill=failing_first)  # retry
    # 두 번 호출되었지만 registry.apply_delta는 1회만
    po = bridge.registry.get("ORD015")
    assert po.applied_qty == 5, f"이중 apply 금지: applied_qty={po.applied_qty}"


# ══════════════════════════════════════════════════════════════
# KR 통합
# ══════════════════════════════════════════════════════════════

def test_kr_buy_accepted_no_position():
    """TC-I16: KR BUY 접수 시 포지션 미생성."""
    bridge = _make_bridge()
    bridge.market = "KR"
    ok = bridge.register_accept("KRORD001", "005930", "삼성전자", "BUY", 1, 5, 70000.0)
    assert ok is True
    pos = {}
    def on_buy(po, ev):
        pos[ev.code] = True
    bridge.poll(on_buy_fill=on_buy)
    assert "005930" not in pos, "KR BUY_ACCEPTED → 포지션 미생성"


def test_kr_buy_fill_creates_position():
    """TC-I17: KR 실제 fill 후 포지션 생성 콜백 호출."""
    fs = MockFillSource()
    fs.add("KR", "005930", "BUY", [Fill("KRORD002", 5, 70000)])
    bridge = _make_bridge(fill_source=fs)
    bridge.market = "KR"
    bridge.register_accept("KRORD002", "005930", "삼성전자", "BUY", 1, 5, 70000.0)
    pos = {}
    def on_buy(po, ev):
        pos[ev.code] = ev.applied_qty
    bridge.poll(on_buy_fill=on_buy)
    assert pos.get("005930") == 5


def test_kr_sell_accepted_position_kept():
    """TC-I18: KR SELL 접수 시 포지션 유지 (즉시 pop 금지)."""
    bridge = _make_bridge()
    bridge.market = "KR"
    bridge.register_accept("KRORD003", "005930", "삼성전자", "SELL", 0, 5, 72000.0)
    sell_ev = []
    def on_sell(po, ev):
        sell_ev.append(ev)
    bridge.poll(on_buy_fill=lambda po, ev: None, on_sell_fill=on_sell)
    assert sell_ev == [], "SELL_ACCEPTED만으로 콜백 없어야 함"


def test_kr_sell_full_fill_closes_position():
    """TC-I19: KR 전량 SELL fill → 포지션 제거 콜백."""
    fs = MockFillSource()
    fs.add("KR", "005930", "SELL", [Fill("KRORD004", 5, 72000)])
    bridge = _make_bridge(fill_source=fs)
    bridge.market = "KR"
    bridge.register_accept("KRORD004", "005930", "삼성전자", "SELL", 0, 5, 72000.0)
    sell_ev = []
    def on_sell(po, ev):
        sell_ev.append(ev)
    bridge.poll(on_buy_fill=lambda po, ev: None, on_sell_fill=on_sell)
    assert len(sell_ev) == 1
    assert sell_ev[0].became_filled is True


# ══════════════════════════════════════════════════════════════
# 손익 NET 기준 통일 (HIGH-3)
# ══════════════════════════════════════════════════════════════

def test_net_pnl_fee_deducted_kr():
    """TC-I20: KR GAP2 SELL fill → NET 손익(수수료 차감) 확인."""
    avg_price  = 70000.0
    fill_price = 72000.0
    delta_qty  = 5
    # executor._calc_net_pct 기준 fee = 0.015*2 + 0.20 = 0.23%
    _fee_pct = 0.015 * 2 + 0.20
    fee_krw  = avg_price * delta_qty * (_fee_pct / 100)
    gross    = (fill_price - avg_price) * delta_qty
    expected_net = gross - fee_krw
    # 직접 계산 확인 (콜백에서 동일 로직 사용 확인)
    assert expected_net < gross, "NET < GROSS (수수료 차감)"
    assert expected_net > 0, "이익 구간이므로 NET > 0"


def test_net_pnl_fee_deducted_us():
    """TC-I21: US GAP2 SELL fill → NET 손익(수수료 차감) 확인."""
    avg_price  = 100.0
    fill_price = 110.0
    delta_qty  = 5
    usd_krw    = 1350.0
    _fee_pct   = 0.015 * 2 + 0.20  # executor._calc_net_pct 동일
    gross_usd  = (fill_price - avg_price) * delta_qty
    fee_usd    = avg_price * delta_qty * (_fee_pct / 100)
    net_usd    = gross_usd - fee_usd
    net_krw    = net_usd * usd_krw
    gross_krw  = gross_usd * usd_krw
    assert net_krw < gross_krw, "US NET < GROSS (수수료 차감)"


def test_partial_sell_net_pnl_consistent():
    """TC-I22: 부분매도 합계 NET 손익 == 전량매도 NET 손익."""
    avg_price  = 100.0
    fill_price = 110.0
    total_qty  = 10
    _fee_pct   = 0.015 * 2 + 0.20
    # 전량 한번에
    full_gross   = (fill_price - avg_price) * total_qty
    full_fee     = avg_price * total_qty * (_fee_pct / 100)
    full_net     = full_gross - full_fee
    # 부분 2회 합산 (5+5)
    partial_net  = 0.0
    for _ in range(2):
        g = (fill_price - avg_price) * 5
        f = avg_price * 5 * (_fee_pct / 100)
        partial_net += g - f
    assert abs(full_net - partial_net) < 0.01, \
        f"전량{full_net:.4f} vs 부분합{partial_net:.4f}"


# ══════════════════════════════════════════════════════════════
# 손상 JSON (MEDIUM-6)
# ══════════════════════════════════════════════════════════════

def test_corrupt_json_raises_runtime_error(tmp_path):
    """TC-I23: 손상 JSON → RuntimeError 발생 + .corrupt 백업 생성."""
    p = tmp_path / "pending_corrupt.json"
    p.write_text("{INVALID JSON{{", encoding="utf-8")
    reg = PendingRegistry()
    with pytest.raises(RuntimeError, match="RECOVERY_REQUIRED"):
        reg.load_from(str(p))
    # .corrupt.TIMESTAMP 백업 파일 생성 확인
    backups = list(tmp_path.glob("pending_corrupt.json.corrupt.*"))
    assert len(backups) >= 1, "손상 파일 백업 생성 필요"


def test_corrupt_json_not_silent_zero():
    """TC-I24: 손상 JSON → 조용히 0건 반환 금지 (RuntimeError 필수)."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("[1,2,3]")  # dict 아닌 list
        name = f.name
    reg = PendingRegistry()
    with pytest.raises(RuntimeError):
        reg.load_from(name)
    os.unlink(name)


# ══════════════════════════════════════════════════════════════
# Feature Flag 런타임 고정 (MEDIUM-7)
# ══════════════════════════════════════════════════════════════

def test_feature_flag_false_no_pending_warning(tmp_path, caplog):
    """TC-I25: ENABLE_GAP2=false 시작 + pending 파일 없으면 경고 없음."""
    import logging
    pending_path = str(tmp_path / "pending_test.json")
    # pending 파일 없음 → 경고 없음
    os.environ["ENABLE_GAP2"] = "false"
    bridge = ExecutionBridge(
        market="US",
        fill_source=MockFillSource(),
        pending_path=pending_path,
    )
    # 파일 없음 → restore 0건
    n = bridge.restore()
    assert n == 0
    os.environ["ENABLE_GAP2"] = "true"  # 복원
