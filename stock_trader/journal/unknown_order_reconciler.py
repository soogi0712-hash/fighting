"""UNKNOWN 주문 정합화(reconciliation).

PENDING UNKNOWN 을 KIS 당일 주문/체결 조회 후보와 대조해 해소한다.
단순 시간경과로 자동해제하지 않는다. 안전 원칙:
  - 주문 발견(ODNO) → pending 승격 성공 시에만 RESOLVED_ACCEPTED(차단 해제)
  - 체결 발견 → 체결 부킹 성공 시에만 RESOLVED_FILLED(차단 해제)
  - 명확한 미접수(성공 조회 & 동일조건 주문 0건) → RESOLVED_NOT_ACCEPTED
  - 동일조건 후보 다수 → 식별 불가 → MANUAL_REVIEW(계속 차단)
  - 조회 실패/승격·부킹 실패 → 계속 차단(PENDING/MANUAL_REVIEW)

candidate_provider(row) 반환:
  {"query_ok": bool,
   "candidates": [ {odno, qty, price, cum_filled_qty, unfilled_qty,
                    order_status, order_time}, ... ]}
on_promote(row, cand) / on_fill(row, cand): True 반환 시에만 해소(차단 해제).
없으면(None) 승격·부킹 미확인으로 보고 MANUAL_REVIEW 로 남겨 계속 차단한다.
"""
from __future__ import annotations


def _match(cand, row):
    try:
        return (int(cand.get("qty", 0)) == int(row["qty"])
                and int(cand.get("price", 0)) == int(row["price"]))
    except (TypeError, ValueError):
        return False


def reconcile_unknown_orders(ledger, candidate_provider,
                             on_promote=None, on_fill=None, now_iso="",
                             release_after_zero_streak=3):
    """PENDING UNKNOWN 을 정합화한다. 처리결과 [(id, outcome), ...] 반환.

    release_after_zero_streak: 동일조건 주문 0건이 '연속 N회' 확인돼야 미접수로
      확정(RESOLVED_NOT_ACCEPTED)한다. 1회 0건은 조회지연일 수 있어 유지한다.
    """
    results = []
    for row in ledger.list_active():
        if row.get("status") != "PENDING":
            continue    # AMBIGUOUS_MATCH·MANUAL_REVIEW 는 자동 변경 안 함(수동확인)
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

        if len(cands) == 0:
            # 성공 조회지만 동일조건 주문 0건. 단발 0건은 조회지연일 수 있어
            # 즉시 해제하지 않고, 연속 N회 0건일 때만 미접수로 확정한다.
            streak = int(row.get("not_found_streak", 0) or 0) + 1
            if streak >= int(release_after_zero_streak):
                ledger.resolve(rid, "RESOLVED_NOT_ACCEPTED",
                               note=f"동일조건 주문 연속 {streak}회 0건 → 미접수 확정",
                               ts=now_iso)
                results.append((rid, "RESOLVED_NOT_ACCEPTED"))
            else:
                ledger.bump_not_found(
                    rid, streak,
                    note=f"동일조건 0건(연속 {streak}회, 조회지연 가능 → 유지)",
                    ts=now_iso)
                results.append((rid, "KEEP_PENDING_ZERO_STREAK"))
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
