"""
phase5_readonly_check.py — PHASE 5 실계좌 '읽기 전용' 검증 하네스

목적:
  신규 카카오뱅크 연계 계좌 + 신규 API 발급 후, 실주문 없이 아래만 검증한다.
    1) 신규 API 인증          2) 신규 계좌번호 인식
    3) 국내 잔고 조회          4) 해외 잔고 조회
    5) 국내 시세 조회          6) 해외 시세 조회
    7) 주문 가능금액 조회      8) 장 운영시간 판정
    9) 로그 내 인증정보 마스킹 확인

안전장치:
  - 주문/취소 API 를 절대 호출하지 않는다(읽기 메서드만).
  - LIVE_ORDER_ENABLED 와 무관하게 동작하지만, 만약 true 여도 이 스크립트는 주문하지 않는다.
  - 실 API 호출은 환경변수 PHASE5_CONFIRM=1 이 있을 때만 수행한다(오발사 방지).
    (없으면 '실행 보류'만 출력하고 네트워크를 호출하지 않는다.)
  - 인증정보(app_key/secret/account)는 마스킹해서만 출력한다.

실행(신규 키를 .env 에 넣은 뒤):
    PHASE5_CONFIRM=1 python3 tools/phase5_readonly_check.py
오프라인 구조 테스트:
    tests/test_phase5_readonly.py (mock api, 네트워크 없음)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def mask_secret(s, keep=4) -> str:
    s = str(s or "")
    return (s[:keep] + "…(masked)") if s else "(empty)"


def mask_acct(s) -> str:
    s = str(s or "")
    if len(s) <= 4:
        return "****"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def run_checks(api, session_mod, cfg, sample_kr="005930", sample_us="AAPL"):
    """읽기 전용 검증 실행. (name, ok, detail) 목록 반환. 주문 호출 없음."""
    results = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"예외: {e!r}"
        results.append((name, bool(ok), detail))

    # 1) 인증 (토큰 발급)
    def _auth():
        tok = api._get_token()
        return (bool(tok), f"token={mask_secret(tok, 6)}")
    check("1.신규 API 인증", _auth)

    # 2) 계좌번호 인식 (마스킹 출력)
    def _acct():
        acc = getattr(cfg, "KIS_ACCOUNT_NO", "")
        parts = acc.split("-")
        return (bool(acc) and len(parts) == 2, f"account={mask_acct(acc)} (형식 {'OK' if len(parts)==2 else '오류'})")
    check("2.신규 계좌번호 인식", _acct)

    # 3) 국내 잔고
    def _kr_bal():
        b = api.get_balance()
        return (isinstance(b, dict), f"cash={b.get('cash') if isinstance(b,dict) else '?'} holdings={len(b.get('holdings',[])) if isinstance(b,dict) else '?'}")
    check("3.국내 잔고 조회", _kr_bal)

    # 4) 해외 잔고
    def _us_bal():
        b = api.get_us_balance()
        return (isinstance(b, dict), f"holdings={len(b.get('holdings',[])) if isinstance(b,dict) else '?'}")
    check("4.해외 잔고 조회", _us_bal)

    # 5) 국내 시세
    def _kr_px():
        p = api.get_current_price(sample_kr)
        px = p.get("price") if isinstance(p, dict) else None
        return (bool(px and px > 0), f"{sample_kr} price={px}")
    check("5.국내 시세 조회", _kr_px)

    # 6) 해외 시세
    def _us_px():
        p = api.get_us_current_price(sample_us)
        px = p.get("price") if isinstance(p, dict) else None
        return (bool(px and px > 0), f"{sample_us} price={px}")
    check("6.해외 시세 조회", _us_px)

    # 7) 주문 가능금액 (KR 예수금 + US 원화가능)
    def _ord_cash():
        b = api.get_balance()
        kr_cash = b.get("cash") if isinstance(b, dict) else None
        us_krw = api.get_us_krw_available()
        return (kr_cash is not None and us_krw is not None, f"KR예수금={kr_cash} / US원화가능={us_krw}")
    check("7.주문 가능금액 조회", _ord_cash)

    # 8) 장 운영시간 판정 (로컬 판정 — API 무관)
    def _hours():
        kr = session_mod.session_info()
        us = session_mod.us_session_info()
        return (isinstance(kr, dict) and isinstance(us, dict),
                f"KR={kr.get('session')} tradeable={kr.get('tradeable')} / US={us.get('session')}")
    check("8.장 운영시간 판정", _hours)

    # 9) 마스킹 확인 (전체 키가 출력에 노출되지 않는지)
    def _mask():
        full_key = getattr(cfg, "KIS_APP_KEY", "")
        blob = "\n".join(str(d) for _, _, d in results)   # 지금까지 출력된 detail 전체
        leaked = bool(full_key) and full_key in blob
        return (not leaked, "노출 없음" if not leaked else "★ 전체 키 노출 감지!")
    check("9.로그 인증정보 마스킹", _mask)

    return results


def main():
    from config import Config
    from utils import market_session
    from api.kis_api import KISApi

    if os.getenv("PHASE5_CONFIRM") != "1":
        print("─" * 60)
        print("PHASE 5 읽기 전용 검증 — 실행 보류(네트워크 미호출).")
        print("신규 .env 키 입력 후, 실행하려면:")
        print("    PHASE5_CONFIRM=1 python3 tools/phase5_readonly_check.py")
        print("※ 이 스크립트는 주문 API 를 절대 호출하지 않습니다(읽기 전용).")
        print("─" * 60)
        return 0

    print(f"LIVE_ORDER_ENABLED={Config.LIVE_ORDER_ENABLED} (읽기 검증엔 무관, 주문 호출 없음)")
    api = KISApi()
    results = run_checks(api, market_session, Config)
    print("─" * 60)
    allok = True
    for name, ok, detail in results:
        allok = allok and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print("─" * 60)
    print("결과:", "✅ 전체 통과 → 실주문 직전 보고 단계로" if allok else "❌ 실패 항목 있음 → 원인 수정 후 재검증")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
