"""UNKNOWN 주문 정합화(reconciliation).

UNKNOWN(PENDING/UNKNOWN_NOT_FOUND) 을 KIS 당일 주문/체결 조회 후보와 대조한다.
단순 시간경과·조회횟수·장마감·날짜변경으로 자동해제하지 않는다. 안전 원칙:
  - 주문 발견(ODNO, 고유) → pending 승격 성공 시에만 RESOLVED_ACCEPTED(차단 해제)
  - 체결 발견 → 체결 부킹 성공 시에만 RESOLVED_FILLED(차단 해제)
  - 동일조건 후보 0건 → UNKNOWN_NOT_FOUND 로 '계속 차단'(자동 미접수 확정 금지).
      후보 0건 반복은 명확한 미접수 증거가 아니다(조회지연·조회범위·일자경계로
      실제 접수 주문이 늦게 나타날 수 있음). 이후 후보가 나타나면 승격·부킹으로 해소.
  - 동일조건 후보 다수 → 식별 불가 → AMBIGUOUS_MATCH(계속 차단)
  - 조회 실패/승격·부킹 실패 → 계속 차단(유지/MANUAL_REVIEW)
자동 미접수 해제 경로는 제공하지 않는다. 미접수 확정 해제는 운영자 수동
(ledger.release_unknown, 사유 필수) 또는 KIS 명시적 거절증거 경로에서만 허용한다.

candidate_provider(row) 반환:
  {"query_ok": bool,
   "candidates": [ {odno, qty, price, cum_filled_qty, unfilled_qty,
                    order_status, order_time}, ... ]}
on_promote(row, cand) / on_fill(row, cand): True 반환 시에만 해소(차단 해제).
없으면(None) 승격·부킹 미확인으로 보고 MANUAL_REVIEW 로 남겨 계속 차단한다.
"""
from __future__ import annotations

try:
    from journal.unknown_order_ledger import RECHECK_STATUSES
except Exception:   # pragma: no cover - 임포트 폴백
    RECHECK_STATUSES = ("PENDING", "UNKNOWN_NOT_FOUND", "PENDING_CONFIRM")


def _match(cand, row):
    try:
        return (int(cand.get("qty", 0)) == int(row["qty"])
                and int(cand.get("price", 0)) == int(row["price"]))
    except (TypeError, ValueError):
        return False


def reconcile_unknown_orders(ledger, candidate_provider,
                             on_promote=None, on_fill=None, now_iso=""):
    """UNKNOWN(재점검 상태) 을 정합화한다. 처리결과 [(id, outcome), ...] 반환.

    시간경과·조회횟수 기반 자동 미접수 해제는 하지 않는다. 후보 0건은
    UNKNOWN_NOT_FOUND 로 계속 차단하며, 후보가 나타날 때만 승격·부킹으로 해소한다.
    AMBIGUOUS_MATCH·MANUAL_REVIEW 는 수동확인 전용이라 자동 변경하지 않는다.
    """
    # 재점검 대상 = 차단(PENDING/UNKNOWN_NOT_FOUND) + 비차단 확인대기(PENDING_CONFIRM)
    _rows = (ledger.list_reconcilable()
             if hasattr(ledger, "list_reconcilable") else ledger.list_active())
    results = []
    for row in _rows:
        if row.get("status") not in RECHECK_STATUSES:
            continue    # AMBIGUOUS_MATCH·MANUAL_REVIEW·RESOLVED_* 는 자동 변경 안 함
        rid = row["id"]
        try:
            q = candidate_provider(row) or {}
        except Exception as e:
            ledger.mark_checked(rid, note=f"조회 예외: {e}", ts=now_iso)
            results.append((rid, "CHECK_ERROR"))
            continue
        if not q.get("query_ok", False):
            ledger.mark_checked(rid, note="조회 실패/불완전 → 유지", ts=now_iso)
            results.append((rid, "KEEP_PENDING_QUERY_FAIL"))
            continue

        cands = [c for c in (q.get("candidates") or []) if _match(c, row)]

        # ── 비차단 SELL 확인대기(PENDING_CONFIRM): ODNO·체결 '연결'만 수행 ──
        #   부킹은 하지 않는다(잔고 대사가 포지션 반영; 손익 임의 반영 금지).
        #   신규 주문을 차단하지도 않는다. 후보 0/다수는 유지(계속 확인대기).
        if row.get("status") == "PENDING_CONFIRM":
            if len(cands) == 1:
                c = cands[0]
                odno = str(c.get("odno", "") or "").strip()
                _filled = int(c.get("cum_filled_qty", 0) or 0) > 0
                ledger.resolve(
                    rid, "RESOLVED_FILLED" if _filled else "RESOLVED_ACCEPTED",
                    odno=odno,
                    note=("SELL 확인: 당일주문 발견 → ODNO 연결"
                          + ("·체결확인" if _filled else "·미체결")),
                    ts=now_iso)
                results.append((rid, "SELL_CONFIRMED"))
            else:
                # 0건 또는 다수 → 상태 유지(비차단), 다음 주기 재확인
                ledger.mark_checked(
                    rid, note=f"SELL 확인대기 유지(후보 {len(cands)}건)", ts=now_iso)
                results.append((rid, "SELL_CONFIRM_KEEP"))
            continue

        if len(cands) == 0:
            # 성공 조회지만 동일조건 주문 0건 → UNKNOWN_NOT_FOUND 로 '계속 차단'.
            # 자동 미접수 확정 금지. streak 는 진단·이력용으로만 증가시킨다.
            streak = int(row.get("not_found_streak", 0) or 0) + 1
            ledger.mark_not_found(
                rid, streak,
                note=f"동일조건 0건(누적 {streak}회) → UNKNOWN 유지(자동해제 금지)",
                ts=now_iso)
            results.append((rid, "UNKNOWN_NOT_FOUND"))
            continue

        if len(cands) > 1:
            # 동일조건 후보 다수 → 특정 불가 → 자동해제·자동재주문 금지, 계속 차단
            ledger.resolve(rid, "AMBIGUOUS_MATCH",
                           note=f"동일조건 후보 {len(cands)}건 → 특정 불가(수동확인)",
                           ts=now_iso)
            results.append((rid, "AMBIGUOUS_MATCH"))
            continue

        # 후보 1건 발견 → 0건 스트릭 리셋
        if int(row.get("not_found_streak", 0) or 0) > 0:
            ledger.reset_not_found(rid, ts=now_iso)
        c = cands[0]
        odno = str(c.get("odno", "") or "").strip()

        # 체결 발견 → 체결 부킹 성공 시에만 해소
        if int(c.get("cum_filled_qty", 0) or 0) > 0:
            ok = False
            if on_fill is not None:
                try:
                    ok = bool(on_fill(row, c))
                except Exception as e:
                    ledger.mark_checked(rid, note=f"체결부킹 예외: {e}", ts=now_iso)
                    results.append((rid, "FILL_HOOK_ERROR"))
                    continue
            if ok:
                ledger.resolve(rid, "RESOLVED_FILLED", odno=odno,
                               note="체결 발견 → 체결기반 부킹 완료", ts=now_iso)
                results.append((rid, "RESOLVED_FILLED"))
            else:
                ledger.resolve(rid, "MANUAL_REVIEW", odno=odno,
                               note="체결 발견했으나 부킹 미확인 → 수동확인", ts=now_iso)
                results.append((rid, "MANUAL_REVIEW"))
            continue

        # 접수(미체결) 발견 → pending 승격 성공 시에만 해소
        if odno:
            ok = False
            if on_promote is not None:
                try:
                    ok = bool(on_promote(row, c))
                except Exception as e:
                    ledger.mark_checked(rid, note=f"승격 예외: {e}", ts=now_iso)
                    results.append((rid, "PROMOTE_HOOK_ERROR"))
                    continue
            if ok:
                ledger.resolve(rid, "RESOLVED_ACCEPTED", odno=odno,
                               note="주문 발견 → pending 승격 완료", ts=now_iso)
                results.append((rid, "RESOLVED_ACCEPTED"))
            else:
                ledger.resolve(rid, "MANUAL_REVIEW", odno=odno,
                               note="주문 발견했으나 pending 승격 미확인 → 수동확인",
                               ts=now_iso)
                results.append((rid, "MANUAL_REVIEW"))
            continue

        # 후보는 있으나 odno·체결 모두 불명 → 유지
        ledger.mark_checked(rid, note="후보 있으나 odno/체결 불명 → 유지", ts=now_iso)
        results.append((rid, "KEEP_PENDING"))
    return results
