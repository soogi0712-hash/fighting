"""US 실보유 포지션 정합화 판정 (순수 로직·부수효과 없음).

KIS 해외 잔고를 '보유수량의 권위값'으로 삼아, 내부 원장을 복원/정리할지 판정한다.
★ 부수효과(포지션 생성·삭제·저장·주문)는 호출부가 수행한다. 이 함수는 '무엇을 할지'만.

Fail-safe 원칙(§6/C):
  - 잔고 조회가 **명확히 성공(ok=True, source=='api', holdings 파싱 성공)** 인 경우에만
    stale 정리·권위 갱신을 허용한다.
  - 다음이면 authoritative=False → **내부 포지션 삭제 금지, 복원 강제 안 함, 신규 BUY만
    스킵**: API 오류/timeout/rate-limit/빈 응답/캐시 응답/파싱 실패/source!=api/불명확.
  - SELL·체결감시·기존 보유종목 보호는 이 판정과 무관하게 계속(호출부 책임).
"""
from __future__ import annotations

from typing import Optional


class ReconcileResult:
    __slots__ = ("authoritative", "buy_allowed", "to_restore", "to_reconcile",
                 "to_stale_remove", "broker_count", "internal_count",
                 "mismatch_count", "reason")

    def __init__(self, authoritative, buy_allowed, to_restore, to_reconcile,
                 to_stale_remove, broker_count, internal_count, mismatch_count, reason):
        self.authoritative   = authoritative
        self.buy_allowed     = buy_allowed
        self.to_restore      = to_restore        # 내부에 없는 broker 보유 → 복원
        self.to_reconcile    = to_reconcile      # 양쪽 존재 → qty/avg 권위 갱신
        self.to_stale_remove = to_stale_remove   # 내부에만 존재 → stale 정리(권위 성공 시만)
        self.broker_count    = broker_count
        self.internal_count  = internal_count
        self.mismatch_count  = mismatch_count
        self.reason          = reason

    def health(self) -> dict:
        """PII 없는 health 스냅샷(계좌·토큰·원문 미포함)."""
        return {
            "authoritative":     self.authoritative,
            "buy_allowed":       self.buy_allowed,
            "restored_count":    len(self.to_restore),
            "stale_removed_count": len(self.to_stale_remove),
            "reconciled_count":  len(self.to_reconcile),
            "broker_count":      self.broker_count,
            "internal_count":    self.internal_count,
            "mismatch_count":    self.mismatch_count,
            "reason":            self.reason,
        }


def is_authoritative(ok: bool, source: Optional[str], holdings) -> bool:
    """잔고 응답이 '명확한 성공'인지. 하나라도 불명확하면 False(fail-safe)."""
    if not ok:
        return False
    if str(source or "").lower() != "api":   # 캐시/미지정/기타 → 비권위
        return False
    if not isinstance(holdings, list):        # 파싱 실패/빈 응답 형태 → 비권위
        return False
    return True


def _norm_qty(h) -> int:
    try:
        return int(float(h.get("qty", 0) or 0))
    except (TypeError, ValueError):
        return 0


def reconcile_decision(
    ok: bool,
    source: Optional[str],
    holdings,
    internal_symbols,
    complete: bool = False,
) -> ReconcileResult:
    """정합화 판정.

    Args:
      ok        : 잔고 조회 성공 여부(호출부가 raw 응답에서 판정; error 없음 등)
      source    : 데이터 출처('api' 만 권위; 'cache'/None → 비권위)
      holdings  : broker 보유 리스트 [{symbol, qty, avg_price, cur_price, name, excd}, ...]
      internal_symbols : 내부 원장 보유 심볼 집합/리스트
      complete  : **완전한 스냅샷 증거**(전 거래소·전 페이지 조회 완료). §5:
                  완전성 증거가 없으면(complete=False) **복원만 허용, stale 삭제 금지**.
                  일부 거래소/일부 페이지 응답으로 실보유를 삭제하는 사고를 막는다.

    Returns: ReconcileResult
    """
    internal = set(internal_symbols or [])

    if not is_authoritative(ok, source, holdings):
        # 비권위: 아무 것도 삭제/복원하지 않는다. 신규 BUY만 스킵.
        return ReconcileResult(
            authoritative=False, buy_allowed=False,
            to_restore=[], to_reconcile=[], to_stale_remove=[],
            broker_count=0, internal_count=len(internal), mismatch_count=0,
            reason="broker_query_not_authoritative(fail-safe: no delete/restore, skip new BUY)",
        )

    # 권위 성공: qty>0 만 유효 보유로 인정
    broker = {}
    for h in holdings:
        sym = str(h.get("symbol") or "").upper()
        q = _norm_qty(h)
        if sym and q > 0:
            broker[sym] = h

    broker_syms = set(broker.keys())
    to_restore, to_reconcile = [], []
    for sym, h in broker.items():
        try:
            avg = float(h.get("avg_price", 0) or 0)
        except (TypeError, ValueError):
            avg = 0.0
        try:
            cur = float(h.get("cur_price", 0) or 0)
        except (TypeError, ValueError):
            cur = 0.0
        rec = {
            "symbol": sym, "qty": _norm_qty(h), "avg_price": avg,
            "cur_price": cur, "name": h.get("name") or sym,
            "excd": h.get("excd") or "NASD",
            # §B: highest_price = max(avg, current) — 낮게 잡히지 않도록
            "highest_price": max(avg, cur) if (avg or cur) else 0.0,
        }
        if sym in internal:
            to_reconcile.append(rec)
        else:
            to_restore.append(rec)

    # 내부에만 존재 → stale 후보. 단, §5: **완전성 증거(complete)가 있을 때만 삭제 허용**.
    #   complete=False(부분 거래소/부분 페이지/증거없음) → stale 삭제 금지, 복원만 수행.
    stale_candidates = sorted(internal - broker_syms)
    if complete:
        to_stale_remove = stale_candidates
        reason = "authoritative_reconcile(complete: stale cleanup allowed)"
    else:
        to_stale_remove = []
        reason = ("authoritative_reconcile(incomplete_snapshot: restore-only, "
                  "stale delete forbidden; %d stale candidate(s) preserved)"
                  % len(stale_candidates))
    mismatch = len(to_restore) + len(stale_candidates)

    return ReconcileResult(
        authoritative=True, buy_allowed=True,
        to_restore=to_restore, to_reconcile=to_reconcile,
        to_stale_remove=to_stale_remove,
        broker_count=len(broker_syms), internal_count=len(internal),
        mismatch_count=mismatch,
        reason=reason,
    )
