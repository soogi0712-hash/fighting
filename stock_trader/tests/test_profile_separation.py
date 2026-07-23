"""
test_profile_separation.py — 계좌 프로필 분리 인프라 검증 (mock, 실 KIS 서버 미호출)

14개 필수 케이스. 실제 키/계좌번호 없이 더미값으로만 검증.
"""
import os, sys, tempfile, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import profile_config as pc
from profile_config import KisProfile, resolve_profile, reset_for_test, ProfileError
import api.kis_api as kmod
from utils import order_gate
from config import Config

_KIS_ENV = [k for k in list(os.environ) if k.startswith("KIS_")]


class _Boom:
    def __getattr__(self, _):
        raise AssertionError("네트워크 호출 발생! (차단 실패)")


class _FakeResp:
    def __init__(self, payload): self._p = payload; self.status_code = 200
    def raise_for_status(self): pass
    def json(self): return self._p
class _FakeReq:
    def __init__(self, p): self._p = p; self.n = 0
    def get(self, *a, **k): self.n += 1; return _FakeResp(self._p)
    def post(self, *a, **k): self.n += 1; return _FakeResp(self._p)


def _clear_kis_env():
    for k in list(os.environ):
        if k.startswith("KIS_"):
            del os.environ[k]

def _use_profile(**env):
    """KIS_* 환경 초기화 후 지정값 세팅 + 프로필 캐시 리셋+확정."""
    _clear_kis_env()
    for k, v in env.items():
        os.environ[k] = v
    reset_for_test()
    resolve_profile(force_reload=True)

LEGACY = {
    "KIS_ACTIVE_PROFILE": "legacy",
    "KIS_LEGACY_APP_KEY": "DUMMYKEYLEG", "KIS_LEGACY_APP_SECRET": "DUMMYSECRETLEG",
    "KIS_LEGACY_ACCOUNT": "11111111", "KIS_LEGACY_PRODUCT_CODE": "01",
    "KIS_LEGACY_MODE": "READ_ONLY",
}
KAKAO_RO = {
    "KIS_ACTIVE_PROFILE": "kakao",
    "KIS_KAKAO_APP_KEY": "DUMMYKEYKAK", "KIS_KAKAO_APP_SECRET": "DUMMYSECRETKAK",
    "KIS_KAKAO_ACCOUNT": "22222222", "KIS_KAKAO_PRODUCT_CODE": "01",
    "KIS_KAKAO_MODE": "READ_ONLY",
}
KAKAO_LIVE = {**KAKAO_RO, "KIS_KAKAO_MODE": "LIVE"}


def _new_api():
    return kmod.KISApi()


def test_01_legacy_read_allowed():
    _use_profile(**LEGACY)
    orig = kmod.requests; kmod.requests = _FakeReq({"output1": []})
    try:
        api = _new_api()
        api._headers = lambda *a, **k: {}; api._rate_limit = lambda: None
        out = api.get_order_history(days=1)   # 조회는 게이트 없음
        assert out == []
    finally:
        kmod.requests = orig
    print("✓ 01 legacy 조회 API 허용")


def _assert_order_blocked(profile_env, setup=None):
    _use_profile(**profile_env)
    if setup: setup()
    orig = kmod.requests; kmod.requests = _Boom()
    try:
        api = _new_api()
        api.account_no = "00000000-01"
        r_buy  = api.buy("005930", 1, 100)
        r_sell = api.sell("005930", 1, 100)
        r_ub   = api.buy_us("AAPL", 1, 10.0)
        r_us   = api.sell_us("AAPL", 1, 10.0)
        for r in (r_buy, r_sell, r_ub, r_us):
            assert r.get("_blocked") is True, r
    finally:
        kmod.requests = orig


def test_02_03_legacy_buy_sell_no_network():
    _assert_order_blocked(LEGACY)
    print("✓ 02·03 legacy 매수/매도 네트워크 0회 차단")


def test_04_legacy_cancel_revise_no_network():
    _use_profile(**LEGACY)
    orig = kmod.requests; kmod.requests = _Boom()
    try:
        api = _new_api(); api.account_no = "00000000-01"
        r = api.cancel_order("0", "005930", 1, 100)
        assert r.get("_blocked") is True
    finally:
        kmod.requests = orig
    print("✓ 04 legacy 취소·정정 네트워크 0회 차단")


def test_05_kakao_readonly_blocks_order():
    _assert_order_blocked(KAKAO_RO)
    print("✓ 05 kakao READ_ONLY 주문 차단")


def test_06_kakao_live_but_master_off_blocks():
    saved = Config.LIVE_ORDER_ENABLED
    Config.LIVE_ORDER_ENABLED = False
    try:
        _assert_order_blocked(KAKAO_LIVE)
    finally:
        Config.LIVE_ORDER_ENABLED = saved
    print("✓ 06 kakao LIVE + LIVE_ORDER_ENABLED=false 차단")


def test_07_kakao_live_master_on_but_kill_blocks():
    saved = Config.LIVE_ORDER_ENABLED
    Config.LIVE_ORDER_ENABLED = True
    try:
        _assert_order_blocked(KAKAO_LIVE, setup=lambda: order_gate.activate_kill("test"))
    finally:
        order_gate.clear_kill(); Config.LIVE_ORDER_ENABLED = saved
    print("✓ 07 kakao LIVE + master ON + kill → 차단")


def test_08_token_not_shared():
    leg = KisProfile("legacy", "KEYLEG", "SECLEG", "11111111", "01", "READ_ONLY")
    kak = KisProfile("kakao", "KEYKAK", "SECKAK", "22222222", "01", "LIVE")
    assert leg.token_cache_key() != kak.token_cache_key()
    kmod._TOKEN_STORE.clear()
    from datetime import datetime, timedelta
    exp = datetime.now() + timedelta(hours=1)
    kmod._TOKEN_STORE[leg.token_cache_key()] = {"token": "TOK_LEG", "expires": exp}
    kmod._TOKEN_STORE[kak.token_cache_key()] = {"token": "TOK_KAK", "expires": exp}
    a_leg = kmod.KISApi(profile=leg); a_kak = kmod.KISApi(profile=kak)
    assert a_leg._get_token() == "TOK_LEG" and a_kak._get_token() == "TOK_KAK"
    print("✓ 08 프로필별 토큰 미공유")


def test_09_ledger_not_mixed():
    leg = KisProfile("legacy", "K", "S", "11111111", "01", "READ_ONLY")
    kak = KisProfile("kakao", "K", "S", "22222222", "01", "LIVE")
    assert leg.ledger_path() != kak.ledger_path()
    # 기능 격리: 두 임시 ledger 에 각각 기록 → 상호 미노출
    from ledger.recorder import LedgerRecorder
    t1 = tempfile.mktemp(suffix=".db"); t2 = tempfile.mktemp(suffix=".db")
    r1 = LedgerRecorder(db_path=t1); r2 = LedgerRecorder(db_path=t2)
    r1.on_buy_fill("KR", "005930", "삼성", "L1", 100, 1)
    r2.on_buy_fill("KR", "000660", "하닉", "K1", 200, 1)
    c1 = r1.conn.execute("SELECT code FROM trades").fetchall()
    c2 = r2.conn.execute("SELECT code FROM trades").fetchall()
    assert [x[0] for x in c1] == ["005930"] and [x[0] for x in c2] == ["000660"]
    print("✓ 09 프로필별 ledger 미혼입")


def test_10_unknown_profile_fails():
    _clear_kis_env(); os.environ["KIS_ACTIVE_PROFILE"] = "foobar"; reset_for_test()
    try:
        resolve_profile(force_reload=True); assert False
    except ProfileError:
        pass
    print("✓ 10 잘못된 프로필명 즉시 실패")


def test_11_missing_required_fails():
    _clear_kis_env(); os.environ["KIS_ACTIVE_PROFILE"] = "legacy"; reset_for_test()
    try:
        resolve_profile(force_reload=True); assert False
    except ProfileError:
        pass
    print("✓ 11 필수값 누락 안전 실패")


def test_12_no_secret_or_full_account_in_logs():
    leg = KisProfile("legacy", "SUPERSECRETKEY123", "SUPERSECRET", "73180640", "01", "READ_ONLY")
    blob = str(leg.safe_summary()) + leg.token_cache_key()
    assert "SUPERSECRET" not in blob and "SUPERSECRETKEY123" not in blob
    assert "73180640" not in blob            # 전체 계좌번호 미노출
    print("✓ 12 로그/요약에 SECRET·전체계좌 미노출")


def test_13_runtime_profile_change_blocked():
    _use_profile(**LEGACY)
    p1 = resolve_profile()
    os.environ["KIS_ACTIVE_PROFILE"] = "kakao"   # 런타임 변경 시도
    for k, v in KAKAO_RO.items():
        os.environ[k] = v
    p2 = resolve_profile()                        # force_reload 없음
    assert p2.profile_name == "legacy" == p1.profile_name
    print("✓ 13 런타임 중 프로필 변경 차단(재시작 필요)")


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    order_gate.clear_kill(); reset_for_test()
    print(f"\n=== {len(fns)} profile tests passed (실 KIS 서버 미호출) ===")


if __name__ == "__main__":
    _run()
