"""
test_safety_phase5.py — PHASE 5 안전 결함 수정 검증 (오프라인, 실 API 미호출)

검증:
  - 신규주문 차단 vs 취소 허용 분리
  - 재시작 없는 런타임 킬스위치(정적 LIVE_ORDER_ENABLED 와 구분)
  - 긴급정지 절차(차단→조회→취소→확인→중지)
  - 누적 부분체결 차분 처리 / 누적금액 기반 평균가
  - get_order_history 파싱(fixture) + odno
  - 정합성 거짓 mismatch 제거
  - 게이트 스냅샷에 인증정보/계좌 미포함
실행: (stock_trader 에서) python3 tests/test_safety_phase5.py
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from utils import order_gate
from ledger.recorder import LedgerRecorder
from ledger.fills import Fill, MockFillSource, CumulativeFillTracker
from ledger.wiring import record_trade_event
from ledger.health import LedgerHealth, check_consistency
from api.kis_api import _parse_order_history
import profile_config as _pc

# ── 이 파일의 게이트 테스트는 'LIVE 프로필' 전제(신규주문 차단은 LIVE_ORDER_ENABLED/kill 로만) ──
for _k in [x for x in os.environ if x.startswith("KIS_")]:
    del os.environ[_k]
os.environ.update({"KIS_ACTIVE_PROFILE": "kakao", "KIS_KAKAO_APP_KEY": "D",
                   "KIS_KAKAO_APP_SECRET": "D", "KIS_KAKAO_ACCOUNT": "22222222",
                   "KIS_KAKAO_MODE": "LIVE"})
_pc.reset_for_test(); _pc.resolve_profile(force_reload=True)

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "order_history_sample.json")


def _rec():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    return LedgerRecorder(db_path=tmp.name)


# ── FakeResp/Requests (네트워크 대체) ─────────────────────────
class _FakeResp:
    def __init__(self, payload): self._p = payload; self.status_code = 200
    def raise_for_status(self): pass
    def json(self): return self._p

class _FakeRequests:
    def __init__(self, payload): self._p = payload; self.posted = 0
    def post(self, *a, **k): self.posted += 1; return _FakeResp(self._p)
    def get(self, *a, **k):  return _FakeResp(self._p)


def test_config_static_env_change_has_no_effect():
    """실행 중 os.environ 변경은 Config.LIVE_ORDER_ENABLED(정적 로딩)에 반영되지 않음."""
    before = Config.LIVE_ORDER_ENABLED
    os.environ["LIVE_ORDER_ENABLED"] = "true"
    assert Config.LIVE_ORDER_ENABLED == before   # 재시작 없이는 안 바뀜(정적)
    print("✓ .env LIVE_ORDER_ENABLED 는 정적 — 실행 중 변경 무효(긴급정지 수단 아님)")


def test_runtime_kill_switch_dynamic_without_reimport():
    """런타임 킬스위치는 재시작/재import 없이 즉시 신규주문을 차단한다."""
    saved = Config.LIVE_ORDER_ENABLED
    Config.LIVE_ORDER_ENABLED = True   # PHASE5 활성 상황 가정
    try:
        order_gate.clear_kill()
        assert order_gate.orders_allowed()[0] is True
        order_gate.activate_kill("test")
        assert order_gate.orders_allowed()[0] is False   # 즉시 차단(동일 프로세스)
    finally:
        order_gate.clear_kill()
        Config.LIVE_ORDER_ENABLED = saved
    print("✓ 런타임 킬스위치: 재시작 없이 즉시 차단/해제")


def test_new_order_blocked_by_kill_no_network():
    """live=True 라도 런타임 킬이면 신규주문이 네트워크 없이 차단."""
    import api.kis_api as kmod
    saved = Config.LIVE_ORDER_ENABLED
    Config.LIVE_ORDER_ENABLED = True
    boom = _FakeRequests({}); boom.post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("네트워크 호출!"))
    orig = kmod.requests; kmod.requests = boom
    try:
        order_gate.activate_kill("test")
        api = kmod.KISApi(); api.account_no = "00000000-01"; api.app_key = "DUMMY"
        for r in (api.buy("005930", 1, 70000), api.sell("005930", 1, 70000),
                  api.buy_us("AAPL", 1, 200.0), api.sell_us("AAPL", 1, 200.0)):
            assert r.get("_blocked") is True
    finally:
        kmod.requests = orig; order_gate.clear_kill(); Config.LIVE_ORDER_ENABLED = saved
    print("✓ 런타임 킬 활성 시 신규주문 차단(네트워크 미호출)")


def test_cancel_not_gated():
    """취소는 신규주문 게이트(LIVE_ORDER_ENABLED/kill)로 막지 않음 —
    LIVE 프로필(모듈 상단 설정)이면 live=False + kill 이어도 긴급정지 취소가 실행된다.
    (READ_ONLY/legacy 프로필은 취소도 차단 — test_profile_separation 에서 검증)"""
    import api.kis_api as kmod
    saved = Config.LIVE_ORDER_ENABLED
    Config.LIVE_ORDER_ENABLED = False
    fake = _FakeRequests({"rt_cd": "0", "msg_cd": "OK", "msg1": "취소완료"})
    orig = kmod.requests; kmod.requests = fake
    try:
        order_gate.activate_kill("test")
        api = kmod.KISApi(); api.account_no = "00000000-01"
        api._headers = lambda *a, **k: {}; api._rate_limit = lambda: None
        r = api.cancel_order("0000123456", "005930", 1, 70000)
        assert r.get("rt_cd") == "0" and r.get("_blocked") is None   # LIVE 프로필 → 차단 아님
        assert fake.posted == 1   # 실제 취소 요청 수행
    finally:
        kmod.requests = orig; order_gate.clear_kill(); Config.LIVE_ORDER_ENABLED = saved
    print("✓ 취소: LIVE 프로필은 kill 중에도 실행(신규주문 차단과 분리), READ_ONLY는 차단")


class _MockApi:
    def __init__(self, pending): self._pending = pending; self.cancelled = []
    def get_open_orders(self, order_type="BUY"):
        return list(self._pending)
    def cancel_order(self, order_no, code, qty, unpr, dvsn="00"):
        self.cancelled.append(order_no)
        self._pending = [o for o in self._pending if o["order_no"] != order_no]
        return {"rt_cd": "0"}


def test_emergency_stop_sequence():
    """긴급정지: 차단→조회→취소→확인→중지."""
    order_gate.clear_kill()
    pend = [{"order_no": "A", "stock_code": "005930", "unexec_qty": 1, "ord_unpr": 70000, "ord_dvsn": "00"},
            {"order_no": "B", "stock_code": "000660", "unexec_qty": 2, "ord_unpr": 200000, "ord_dvsn": "00"}]
    api = _MockApi(pend)
    stopped = {"v": False}
    rep = order_gate.emergency_stop(api, stop_fn=lambda: stopped.__setitem__("v", True))
    try:
        assert order_gate.kill_active() is True                 # 1) 신규주문 차단
        assert set(rep["cancelled"]) == {"A", "B"}              # 3) 취소
        assert rep["remaining_after_cancel"] == 0               # 4) 확인
        assert stopped["v"] is True                             # 5) 중지
        assert rep["ok"] is True
    finally:
        order_gate.clear_kill()
    print("✓ 긴급정지 절차: 차단→조회→취소2→확인0→중지, ok")


def test_emergency_stop_cancel_failure_counts():
    """취소 실패 시 cancel_fail_count 증가·ok=False."""
    class _FailApi(_MockApi):
        def cancel_order(self, *a, **k): return {"rt_cd": "9", "msg1": "실패"}
    order_gate.clear_kill()
    before = order_gate.snapshot()["cancel_fail_count"]
    api = _FailApi([{"order_no": "X", "stock_code": "005930", "unexec_qty": 1, "ord_unpr": 100, "ord_dvsn": "00"}])
    rep = order_gate.emergency_stop(api)
    try:
        assert rep["cancel_failed"] == ["X"]
        assert order_gate.snapshot()["cancel_fail_count"] == before + 1
        assert rep["ok"] is False
    finally:
        order_gate.clear_kill()
    print("✓ 취소 실패 집계 + ok=False")


def test_cumulative_partial_fill_diff_totals_100():
    """37주 부분체결 후 100주 완전체결 조회 → 총 100주만 기록(누적 차분)."""
    rec = _rec(); h = LedgerHealth(); tr = CumulativeFillTracker()
    key = ("KR", "005930")
    # 1차 조회: 누적 37 @ 70000 → 델타 37
    f1 = tr.update(key, 37, 37 * 70000, order_no="O1")
    record_trade_event(rec, h, {"action": "BUY", "code": "005930", "price": 70000, "qty": 37,
                                "order_no": "O1"}, "KR",
                       MockFillSource().add("KR", "005930", "BUY", [f1]))
    # 2차 조회: 누적 100 @ 70000 → 델타 63
    f2 = tr.update(key, 100, 100 * 70000, order_no="O1b")
    record_trade_event(rec, h, {"action": "BUY", "code": "005930", "price": 70000, "qty": 63,
                                "order_no": "O1b"}, "KR",
                       MockFillSource().add("KR", "005930", "BUY", [f2]))
    r = rec.conn.execute("SELECT entry_qty_total FROM trades WHERE code='005930'").fetchone()
    assert r["entry_qty_total"] == 100    # 137 아님
    print("✓ 누적 부분체결 차분: 37→100 조회 시 총 100주만 기록")


def test_avg_price_from_cumulative_amounts():
    """동일 주문 여러 체결가 → 누적금액/누적수량으로 평균가."""
    tr = CumulativeFillTracker(); key = ("KR", "X")
    d1 = tr.update(key, 40, 40 * 70000)                 # 40@70000
    d2 = tr.update(key, 100, 40 * 70000 + 60 * 71000)   # +60@71000 (누적금액 기반)
    assert d1.qty == 40 and abs(d1.price - 70000) < 1e-6
    assert d2.qty == 60 and abs(d2.price - 71000) < 1e-6  # 델타평균 = 4,260,000/60
    print("✓ 누적금액 기반 델타 평균가: 70000 / 71000")


def test_order_history_parse_fixture():
    """get_order_history 파싱(fixture): odno/누적수량/평균가/누적금액 매핑."""
    data = json.load(open(FIX))
    out = _parse_order_history(data)
    assert len(out) == 1
    o = out[0]
    assert o["order_no"] == "0000123456"   # odno
    assert o["code"] == "005930" and o["type"] == "매수"
    assert o["qty"] == 100 and o["price"] == 70600 and o["amount"] == 7060000
    print("✓ get_order_history 파싱: odno·tot_ccld_qty·avg_prvs·tot_ccld_amt")


def test_consistency_no_false_mismatch():
    """접수(trade_log) vs 체결(ledger) 기준 차이로 거짓 mismatch 안 냄."""
    rec = _rec()
    tmplog = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False)
    json.dump([{"action": "BUY"}, {"action": "SELL"}, {"action": "SELL"}], tmplog); tmplog.close()
    res = check_consistency(rec, tmplog.name)
    os.unlink(tmplog.name)
    assert res["match"] is None            # 건수 불일치여도 mismatch 아님
    assert res["log_sell_accepted"] == 2 and res["ledger_closed"] == 0
    print("✓ 정합성: 접수/체결 기준 차이 → 거짓 mismatch 없음(match=None)")


def test_gate_snapshot_no_credentials():
    """게이트 스냅샷: 필수필드 포함 + 원문 시크릿/전체 계좌번호 미노출(마스킹 account 필드는 허용)."""
    snap = order_gate.snapshot()
    for k in ("live_order_enabled", "runtime_kill_switch", "cancel_fail_count", "pending_order_count"):
        assert k in snap
    blob = json.dumps(snap, ensure_ascii=False)
    # 원문 값 미노출: 전체 계좌번호(모듈 kakao 프로필=22222222)·app_key/secret 원문
    assert "22222222" not in blob                 # 전체 계좌번호 원문 미노출
    assert snap["active_profile"]["account"] == "22****22"   # 마스킹 형태만
    for secret in ("app_secret", "appsecret", "KIS_KAKAO_APP_SECRET"):
        assert secret not in blob                 # 시크릿 필드/원문 미노출
    print("✓ 게이트 스냅샷: 필수필드 포함·원문 시크릿/전체계좌 미노출(account 마스킹만)")


def _run_all():
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
    order_gate.clear_kill()
    print("\n=== all passed ===")


if __name__ == "__main__":
    _run_all()
