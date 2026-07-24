#!/usr/bin/env python3
"""
preflight_us.py — 로컬(자격 설정) PC 에서 미국장 재가동 전 통합 점검.

LIVE_ORDER_ENABLED=false 를 유지한 채 실데이터 읽기만 수행한다(실주문 없음).
마지막 줄에 다음 중 하나만 출력:
    READY_FOR_DRY_RUN
    READY_FOR_MINIMUM_LIVE_TRADE
    NOT_READY
NOT_READY 이면 원인과 수정 명령을 함께 출력한다.

사용:  python tools/preflight_us.py
"""
import os
import sys
import traceback

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

OK, FAIL, WARN = "✅ OK", "❌ FAIL", "⚠️ WARN"
_fails: list[str] = []
_fixes: list[str] = []


def line(label, status, detail=""):
    print(f"  {status:8} | {label}" + (f" — {detail}" if detail else ""))


def fail(label, detail, fix):
    line(label, FAIL, detail)
    _fails.append(f"{label}: {detail}")
    if fix:
        _fixes.append(fix)


def main():
    print("=" * 64)
    print("  preflight_us — 미국장 재가동 전 통합 점검 (LIVE=false 유지)")
    print("=" * 64)

    # 0) LIVE / RECOVERY 환경 상태
    try:
        from config import Config
        live = getattr(Config, "LIVE_ORDER_ENABLED", False)
        line("LIVE_ORDER_ENABLED", OK if live is False else WARN, str(live))
    except Exception as e:
        fail("config 로드", repr(e), "config.py / .env 확인")
        live = None

    try:
        from strategies.recovery_mode import RecoveryConfig
        rc = RecoveryConfig.from_env()
        line("RECOVERY_MODE", OK if rc.enabled else WARN,
             f"enabled={rc.enabled} market={rc.market} max_pos={rc.max_positions} "
             f"pct={rc.max_position_pct} first_only={rc.first_trade_only}")
    except Exception as e:
        rc = None
        line("RECOVERY_MODE", WARN, repr(e))

    # 1) 활성 KIS 프로필
    api = None
    try:
        from profile_config import resolve_profile
        prof = resolve_profile()
        line("활성 KIS 프로필", OK, f"profile={getattr(prof,'name','?')}")
    except Exception as e:
        fail("활성 KIS 프로필", repr(e),
             "환경변수 KIS_ACTIVE_PROFILE=kakao(또는 legacy) 및 해당 키/계좌를 .env 에 설정")

    # 2) API 생성 + 토큰 발급
    if not _fails:
        try:
            from api.kis_api import KISApi
            api = KISApi()
            tok = api._get_token()
            line("토큰 발급", OK if tok else FAIL, "발급 성공" if tok else "토큰 None")
            if not tok:
                fail("토큰 발급", "토큰 None", "앱키/시크릿/계좌·모의구분 확인")
        except Exception as e:
            fail("토큰 발급", repr(e), "네트워크/자격 확인 후 재시도")

    # 3~5) 미국 계좌잔고 / 미체결 / 현재가
    if api and not _fails:
        try:
            bal = api.get_us_balance() or {}
            line("미국 계좌잔고", OK, f"keys={list(bal)[:6]}")
        except Exception as e:
            fail("미국 계좌잔고", repr(e), "overseas 계좌 권한/잔고조회 TR 확인")
        try:
            # 미체결(국내 TR 재사용 지점) — 없으면 WARN
            oo = api.get_open_orders("ALL") if hasattr(api, "get_open_orders") else []
            line("미체결 주문 조회", OK, f"count={len(oo)}")
        except Exception as e:
            line("미체결 주문 조회", WARN, repr(e))
        try:
            px = api.get_us_current_price("AAPL")
            line("미국 현재가(AAPL)", OK if px else WARN, f"{px}")
        except Exception as e:
            line("미국 현재가", WARN, repr(e))
        try:
            fx = api.get_usd_exchange_rate(strict=True)
            line("환율(strict)", OK if fx else FAIL, f"USD/KRW={fx}")
            if fx is None and rc and rc.enabled:
                fail("환율", "strict 조회 실패", "환율 소스 확인 — 미확보 시 Recovery 가 신규주문 차단")
        except Exception as e:
            line("환율", WARN, repr(e))

    # 6) 미국장 세션
    try:
        from utils.market_session import us_session_info
        us = us_session_info()
        line("미국장 세션", OK, f"{us.get('session')} tradeable={us.get('tradeable')}")
    except Exception as e:
        line("미국장 세션", WARN, repr(e))

    # 7) 스크리너 후보(watchlist)
    try:
        from strategies.us_strategy_manager import DEFAULT_US_WATCHLIST
        line("US 워치리스트", OK, f"기본 {len(DEFAULT_US_WATCHLIST)}종목")
    except Exception as e:
        line("US 워치리스트", WARN, repr(e))

    # 8) pending 복구 상태
    try:
        from strategies.pending_orders import PendingRegistry
        from profile_config import resolve_profile
        pend_path = os.path.join(BASE, "data", "pending_us.json")
        reg = PendingRegistry()
        n = reg.load_from(pend_path)
        line("pending 복구", OK, f"{n}건 로드 ({pend_path})")
    except Exception as e:
        line("pending 복구", WARN, repr(e))

    # 9) ledger 상태
    try:
        from ledger.health import LEDGER_HEALTH
        line("ledger health", OK, str(LEDGER_HEALTH.snapshot() if hasattr(LEDGER_HEALTH,'snapshot') else 'present'))
    except Exception as e:
        line("ledger health", WARN, repr(e))

    # 10) reconciliation / kill 상태
    try:
        from utils import order_gate as og
        rec = og.reconciliation_status()
        line("RECONCILIATION", OK if not rec["required"] else FAIL,
             "정상" if not rec["required"] else rec["reason"])
        if rec["required"]:
            fail("RECONCILIATION", rec["reason"], "근거 확인 후 order_gate.clear_reconciliation()")
        line("KILL_SWITCH", OK if not og.kill_active() else WARN,
             "비활성" if not og.kill_active() else "활성")
    except Exception as e:
        line("order_gate", WARN, repr(e))

    # ── 최종 판정 ────────────────────────────────────────────
    print("-" * 64)
    if _fails:
        print("원인:")
        for f in _fails:
            print(f"   - {f}")
        print("수정 명령/조치:")
        for fx in (_fixes or ["(위 원인 해결 후 재실행)"]):
            print(f"   $ {fx}" if fx and not fx.startswith("환경") else f"   - {fx}")
        print("\nNOT_READY")
        return 2

    # reads 성공. LIVE=false → dry-run 가능. Recovery+first_trade_only → 최소 실거래 준비.
    if rc and rc.enabled and rc.market == "US":
        print("\nREADY_FOR_MINIMUM_LIVE_TRADE")
        print("(주의: 실제 주문은 LIVE_ORDER_ENABLED=true 로 사용자가 명시적으로 켠 뒤에만 발생)")
        return 0
    print("\nREADY_FOR_DRY_RUN")
    print("(RECOVERY_MODE=true, RECOVERY_MARKET=US 설정 시 최소 실거래 준비 상태로 승격)")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        print("\nNOT_READY")
        sys.exit(2)
