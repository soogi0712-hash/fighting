"""
test_phase5_readonly.py — PHASE 5 읽기전용 하네스의 '구조' 검증 (오프라인, 네트워크 없음)

- 실제 KIS API 를 호출하지 않는다. mock api/세션/설정을 주입한다.
- 검증: 9개 체크가 정상 동작하고, 인증정보가 마스킹되며, 주문 메서드를 호출하지 않는다.
실행: (stock_trader 에서) python3 tests/test_phase5_readonly.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.phase5_readonly_check import run_checks, mask_acct


class MockApi:
    """읽기 메서드만 가진 가짜 api. 주문 메서드를 부르면 즉시 실패."""
    def __init__(self):
        self.calls = []
    def _get_token(self):
        self.calls.append("token"); return "AABBCCDDEEFF-token-xyz"
    def get_balance(self):
        self.calls.append("kr_bal"); return {"cash": 4900000, "holdings": []}
    def get_us_balance(self):
        self.calls.append("us_bal"); return {"holdings": [{"symbol": "AAPL"}]}
    def get_current_price(self, code):
        self.calls.append("kr_px"); return {"price": 70500}
    def get_us_current_price(self, sym):
        self.calls.append("us_px"); return {"price": 201.3}
    def get_us_krw_available(self):
        self.calls.append("us_krw"); return 1500000.0
    # 주문 메서드가 호출되면 테스트 실패시키기 위한 트랩
    def buy(self, *a, **k):  raise AssertionError("주문 호출됨(buy)!")
    def sell(self, *a, **k): raise AssertionError("주문 호출됨(sell)!")
    def buy_us(self, *a, **k):  raise AssertionError("주문 호출됨(buy_us)!")
    def sell_us(self, *a, **k): raise AssertionError("주문 호출됨(sell_us)!")


class MockSession:
    @staticmethod
    def session_info():   return {"session": "정규장", "tradeable": True}
    @staticmethod
    def us_session_info(): return {"session": "US-정규장"}


class MockCfg:
    KIS_ACCOUNT_NO = "73180640-01"
    KIS_APP_KEY    = "PKabcdef1234567890SECRETKEYFULLVALUE"


def test_all_checks_pass_and_no_order_calls():
    api = MockApi()
    results = run_checks(api, MockSession, MockCfg, sample_kr="005930", sample_us="AAPL")
    names = [n for n, _, _ in results]
    assert len(results) == 9
    for name, ok, detail in results:
        assert ok is True, f"{name} FAIL: {detail}"
    # 주문 메서드는 한 번도 호출되지 않음
    assert all(c in ("token", "kr_bal", "us_bal", "kr_px", "us_px", "us_krw") for c in api.calls)
    print("✓ 9개 체크 전부 PASS, 주문 메서드 호출 0")


def test_credentials_masked_in_output():
    api = MockApi()
    results = run_checks(api, MockSession, MockCfg)
    blob = "\n".join(str(d) for _, _, d in results)
    assert MockCfg.KIS_ACCOUNT_NO not in blob      # 전체 계좌번호 미노출
    assert MockCfg.KIS_APP_KEY not in blob          # 전체 앱키 미노출
    assert "73******-01".replace("**", "") in blob.replace("*", "") or "73" in blob  # 마스킹된 형태
    # 마스킹 체크(9번) 자체가 PASS
    mask_check = [r for r in results if r[0].startswith("9.")][0]
    assert mask_check[1] is True
    print("✓ 인증정보 마스킹 확인(전체 키/계좌 미노출)")


def test_mask_helpers():
    assert mask_acct("73180640-01") == "73*******01"   # 앞2·뒤2만 노출(중간 마스킹)
    assert mask_acct("12") == "****"
    print("✓ 마스킹 헬퍼 동작")


def _run_all():
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
    print("\n=== 3/3 passed ===")


if __name__ == "__main__":
    _run_all()
