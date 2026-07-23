"""
order_gate.py — 신규 주문 게이트 + 런타임 킬스위치 + 긴급정지

원칙:
  - '신규 주문 차단'과 '기존 미체결 취소'를 분리한다.
    · 신규 주문(_order/buy_us/sell_us)은 orders_allowed() 로 게이트한다.
    · 취소(cancel_order)는 게이트하지 않는다(긴급정지 때 반드시 실행돼야 함).
  - LIVE_ORDER_ENABLED(.env)는 '정적 로딩' 마스터 스위치(재시작 필요).
    → 재시작 없는 즉시 차단은 '런타임 킬스위치'로 제공한다(주문 직전 확인).
  - 런타임 킬은 인메모리 플래그 + 파일(data/KILL_SWITCH) 이중.
    파일 존재만으로도 차단되며 재시작에도 유지된다.
"""
import os
import threading

try:
    from config import Config
except Exception:  # pragma: no cover
    from ..config import Config

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_PKG_DIR)
KILL_FILE = os.path.join(_PROJECT_DIR, "data", "KILL_SWITCH")

_lock = threading.Lock()
_state = {
    "kill": False,
    "kill_reason": None,
    "cancel_fail_count": 0,
    "last_pending_count": None,
}


def kill_active() -> bool:
    """런타임 킬 활성 여부 (인메모리 플래그 또는 킬 파일)."""
    if _state["kill"]:
        return True
    try:
        return os.path.exists(KILL_FILE)
    except Exception:
        return False


def activate_kill(reason: str = "manual"):
    """런타임 킬 활성화 — 재시작 없이 즉시 신규 주문 차단."""
    with _lock:
        _state["kill"] = True
        _state["kill_reason"] = reason
    try:
        os.makedirs(os.path.dirname(KILL_FILE), exist_ok=True)
        with open(KILL_FILE, "w") as f:
            f.write(str(reason))
    except Exception:
        pass


def clear_kill():
    """런타임 킬 해제."""
    with _lock:
        _state["kill"] = False
        _state["kill_reason"] = None
    try:
        if os.path.exists(KILL_FILE):
            os.remove(KILL_FILE)
    except Exception:
        pass


def orders_allowed():
    """
    신규 주문 허용 여부 판정. 반환 (allowed: bool, reason: str).
    = 활성 프로필 LIVE AND 마스터(LIVE_ORDER_ENABLED) AND (런타임 킬 비활성).
    주문 함수가 네트워크 호출 직전에 호출한다.
    ★ legacy/READ_ONLY 프로필은 구조적으로 항상 차단.
    """
    # ── 프로필 READ_ONLY 차단 (legacy·READ_ONLY 는 mode·env 무관 차단) ──
    try:
        from profile_config import resolve_profile
        prof = resolve_profile()
        if prof.is_read_only:
            return False, f"profile '{prof.profile_name}' READ_ONLY"
    except Exception as e:
        # 프로필 해석 실패 시 안전측(차단)
        return False, f"profile resolve failed: {e!r}"

    if not getattr(Config, "LIVE_ORDER_ENABLED", False):
        return False, "LIVE_ORDER_ENABLED=false"
    if kill_active():
        return False, f"runtime_kill_switch active({_state['kill_reason']})"
    return True, "ok"


def cancels_allowed():
    """
    취소/정정 허용 여부. 반환 (allowed, reason).
    - READ_ONLY/legacy 프로필: 취소·정정도 불가(과거자료 조회 전용).
    - LIVE 프로필: 허용(킬스위치 중 긴급취소가 실행돼야 하므로 kill/LIVE_ORDER_ENABLED 로 막지 않음).
    """
    try:
        from profile_config import resolve_profile
        prof = resolve_profile()
        if prof.is_read_only:
            return False, f"profile '{prof.profile_name}' READ_ONLY — 취소/정정 불가"
    except Exception as e:
        return False, f"profile resolve failed: {e!r}"
    return True, "ok"


def record_cancel_fail():
    with _lock:
        _state["cancel_fail_count"] += 1


def set_pending_count(n):
    with _lock:
        _state["last_pending_count"] = n


def snapshot() -> dict:
    """상태 API 노출용 (인증정보/계좌번호 미포함, 마스킹만)."""
    prof_summary = None
    allowed = False
    try:
        from profile_config import resolve_profile
        prof_summary = resolve_profile().safe_summary()   # 민감정보 없음
    except Exception as e:
        prof_summary = {"error": repr(e)}
    try:
        allowed = orders_allowed()[0]
    except Exception:
        allowed = False
    return {
        "active_profile": prof_summary,
        "orders_allowed": allowed,
        "live_order_enabled": bool(getattr(Config, "LIVE_ORDER_ENABLED", False)),
        "runtime_kill_switch": kill_active(),
        "kill_reason": _state["kill_reason"],
        "cancel_fail_count": _state["cancel_fail_count"],
        "pending_order_count": _state["last_pending_count"],
    }


def emergency_stop(api, stop_fn=None) -> dict:
    """
    긴급정지 절차:
      1) 신규 주문 즉시 차단(런타임 킬)
      2) 미체결 주문 조회
      3) 기존 미체결 주문 취소
      4) 취소 여부 확인
      5) 프로세스 중지(stop_fn 훅)
    실 API(get_open_orders/cancel_order)를 사용한다 → PHASE 5/실행 시에만.
    """
    report = {"steps": [], "cancelled": [], "cancel_failed": []}

    # 1) 신규 주문 즉시 차단
    activate_kill("emergency_stop")
    report["steps"].append(("1.block_new_orders", "done"))

    # 2) 미체결 조회
    try:
        pending = api.get_open_orders("ALL")
    except Exception as e:
        pending = []
        report["steps"].append(("2.query_pending", f"error:{e!r}"))
    else:
        report["steps"].append(("2.query_pending", f"{len(pending)} open"))
    set_pending_count(len(pending))

    # 3) 취소 (게이트 없음 — 취소는 항상 허용)
    for o in pending:
        try:
            r = api.cancel_order(o["order_no"], o["stock_code"],
                                 o.get("unexec_qty", 0), o.get("ord_unpr", 0),
                                 o.get("ord_dvsn", "00"))
            if isinstance(r, dict) and r.get("rt_cd") == "0":
                report["cancelled"].append(o["order_no"])
            else:
                report["cancel_failed"].append(o["order_no"])
                record_cancel_fail()
        except Exception:
            report["cancel_failed"].append(o["order_no"])
            record_cancel_fail()
    report["steps"].append(("3.cancel", f"{len(report['cancelled'])} ok / {len(report['cancel_failed'])} fail"))

    # 4) 취소 확인 (재조회)
    try:
        remain = api.get_open_orders("ALL")
        remain_n = len(remain)
    except Exception:
        remain_n = None
    set_pending_count(remain_n)
    report["remaining_after_cancel"] = remain_n
    report["steps"].append(("4.verify", f"remaining={remain_n}"))

    # 5) 프로세스 중지 훅
    if stop_fn:
        try:
            stop_fn()
            report["steps"].append(("5.stop_process", "called"))
        except Exception as e:
            report["steps"].append(("5.stop_process", f"error:{e!r}"))

    report["ok"] = (not report["cancel_failed"]) and (remain_n in (0, None))
    return report
