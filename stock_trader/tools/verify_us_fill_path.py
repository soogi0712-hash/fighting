#!/usr/bin/env python3
"""
verify_us_fill_path.py — 미국 체결경로 실KIS 검증 (로컬, LIVE=false 유지).

주문을 제출하지 않는다. 실제 KIS 응답 필드 매핑을 확인하고 pending↔KIS 대조,
체결 delta 계산 가능 여부까지 점검한 뒤 READY_FOR_MINIMUM_LIVE_TRADE / NOT_READY.

사용:  python tools/verify_us_fill_path.py
"""
import os
import sys
import json
import traceback

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)

OK, FAIL, WARN, INFO = "✅ OK", "❌ FAIL", "⚠️ WARN", "ℹ️"
_fails, _fixes = [], []


def line(label, status, detail=""):
    print(f"  {status:8} | {label}" + (f" — {detail}" if detail else ""))


def fail(label, detail, fix):
    line(label, FAIL, detail)
    _fails.append(f"{label}: {detail}")
    if fix:
        _fixes.append(fix)


def _sanitize(d: dict) -> dict:
    """민감정보(계좌·잔고금액) 제거하고 필드 키/샘플만 남김."""
    drop = {"cano", "acnt_prdt_cd", "prdt_name"}
    return {k: v for k, v in d.items() if k.lower() not in drop}


def main():
    print("=" * 66)
    print("  verify_us_fill_path — 미국 체결경로 검증 (주문 미제출, LIVE=false)")
    print("=" * 66)

    from config import Config
    live = getattr(Config, "LIVE_ORDER_ENABLED", False)
    line("LIVE_ORDER_ENABLED", OK if live is False else WARN, str(live))
    if live:
        line("주의", WARN, "이 도구는 LIVE=false 에서만 실행하십시오(주문 미제출 보장)")

    from strategies.recovery_mode import RecoveryConfig
    rc = RecoveryConfig.from_env()
    line("RECOVERY_MODE", OK if rc.enabled else WARN,
         f"enabled={rc.enabled} market={rc.market} max_pos={rc.max_positions} first_only={rc.first_trade_only}")

    # 인증 + API
    api = None
    try:
        from profile_config import resolve_profile
        resolve_profile()
        from api.kis_api import KISApi
        api = KISApi()
        tok = api._get_token()
        line("KIS 인증/토큰", OK if tok else FAIL, "발급 성공" if tok else "토큰 None")
        if not tok:
            fail("KIS 인증", "토큰 None", "KIS_ACTIVE_PROFILE/키/계좌 확인")
    except Exception as e:
        fail("KIS 인증", repr(e), "네트워크/자격 확인")

    if api and not _fails:
        # 미국 잔고
        try:
            bal = api.get_us_balance() or {}
            line("미국 계좌잔고", OK, f"holdings={len(bal.get('holdings', []))}종목")
        except Exception as e:
            fail("미국 계좌잔고", repr(e), "overseas 잔고 TR 권한 확인")

        # 미국 미체결
        try:
            oo = api.get_open_orders("ALL") if hasattr(api, "get_open_orders") else []
            line("미체결 주문", OK, f"count={len(oo)}")
        except Exception as e:
            line("미체결 주문", WARN, repr(e))

        # ★ 미국 당일 주문·체결 원본 덤프(필드 매핑 확인)
        try:
            rows = api.get_us_order_history_raw(days=1)
            line("미국 체결내역(TTTS3035R)", OK, f"rows={len(rows)}")
            if rows:
                print("    [원본 필드 샘플 — 민감정보 제거] (US 필드 매핑 확인용):")
                print("    " + json.dumps(_sanitize(rows[0]), ensure_ascii=False)[:600])
                # UsKisFillSource 후보키 매핑 점검
                from ledger.fills import UsKisFillSource, _first_num
                src = UsKisFillSource(api)
                sample = rows[0]
                qty = _first_num(sample, src.QTY_KEYS, -1)
                odno = next((sample.get(k) for k in src.ODNO_KEYS if sample.get(k)), None)
                mapped_ok = (qty >= 0 and odno)
                line("US 필드 매핑", OK if mapped_ok else WARN,
                     f"체결수량키매핑={'성공' if qty>=0 else '실패'} odno={'있음' if odno else '없음'}")
                if not mapped_ok:
                    line("→ 매핑 조치", INFO,
                         "위 원본 필드를 확인해 UsKisFillSource.QTY_KEYS/ODNO_KEYS 에 실제 키 추가")
            else:
                line("US 필드 매핑", INFO, "당일 체결내역 0건 — 실주문 1회 후 재확인 필요")
        except Exception as e:
            line("미국 체결내역", WARN, repr(e))

    # 로컬 pending 로드 + KIS 대조
    try:
        from strategies.pending_orders import PendingRegistry
        pend_path = os.path.join(BASE, "data", "pending_us.json")
        reg = PendingRegistry()
        n = reg.load_from(pend_path)
        line("로컬 pending 로드", OK, f"{n}건 ({pend_path})")
        if n and api and not _fails:
            try:
                rows = api.get_us_order_history_raw(days=1)
                from ledger.fills import UsKisFillSource
                kis_odnos = {str(r.get(k)) for r in rows for k in UsKisFillSource.ODNO_KEYS if r.get(k)}
                local_odnos = {o.order_no for o in reg.all_open()}
                matched = local_odnos & kis_odnos
                line("pending↔KIS 대조", OK, f"로컬 {len(local_odnos)} / KIS 매칭 {len(matched)}")
            except Exception as e:
                line("pending↔KIS 대조", WARN, repr(e))
    except Exception as e:
        line("로컬 pending", WARN, repr(e))

    # 체결 delta 계산 가능(트래커 자체점검)
    try:
        from ledger.fills import CumulativeFillTracker
        t = CumulativeFillTracker(); k = ("US", "T")
        f1 = t.update(k, 3, 300); f2 = t.update(k, 7, 700)
        ok = (f1 and f1.qty == 3 and f2 and f2.qty == 4)
        line("체결 delta 계산", OK if ok else FAIL, "누적→delta 정상")
    except Exception as e:
        fail("체결 delta 계산", repr(e), "ledger.fills 확인")

    # ledger 쓰기 가능
    try:
        from ledger.recorder import LedgerRecorder
        import tempfile
        LedgerRecorder(db_path=os.path.join(tempfile.mkdtemp(), "t.db"))
        line("ledger 쓰기", OK, "LedgerRecorder 생성 가능")
    except Exception as e:
        line("ledger 쓰기", WARN, repr(e))

    # 안전 게이트
    try:
        from utils import order_gate as og
        rec = og.reconciliation_status()
        line("RECONCILIATION hold", OK if not rec["required"] else FAIL,
             "없음" if not rec["required"] else rec["reason"])
        if rec["required"]:
            fail("RECONCILIATION", rec["reason"], "근거 확인 후 order_gate.clear_reconciliation()")
        line("KILL_SWITCH", OK if not og.kill_active() else FAIL,
             "비활성" if not og.kill_active() else "활성")
        if og.kill_active():
            fail("KILL_SWITCH", "활성", "정상화 후 order_gate.clear_kill() 또는 data/KILL_SWITCH 삭제")
    except Exception as e:
        line("order_gate", WARN, repr(e))

    # 판정
    print("-" * 66)
    if _fails:
        print("실패 항목:")
        for f in _fails:
            print(f"   - {f}")
        print("해결 방법:")
        for fx in (_fixes or ["원인 해결 후 재실행"]):
            print(f"   - {fx}")
        print("\nNOT_READY")
        return 2
    if not (rc.enabled and rc.market == "US"):
        print("\nNOT_READY")
        print("해결: .env 에 RECOVERY_MODE=true, RECOVERY_MARKET=US 설정 후 재실행")
        return 2
    print("\nREADY_FOR_MINIMUM_LIVE_TRADE")
    print("(실제 주문은 LIVE_ORDER_ENABLED=true 로 사용자가 명시적으로 켠 뒤에만 발생)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        print("\nNOT_READY")
        sys.exit(2)
