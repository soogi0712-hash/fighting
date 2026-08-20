"""US 실보유 포지션 정합화 판정 (순수 로직·부수효과 없음).

KIS 해외 잔고를 '보유수량의 권위값'으로 삼아, 내부 원장을 복원/정리할지 판정한다.
★ 부수효과(포지션 생성·삭제·격리·저장·주문)는 호출부가 수행한다. 이 함수는 '무엇을 할지'만.

Fail-safe 원칙(§6/C):
  - 잔고 조회가 **명확히 성공(ok=True, source=='api', holdings 파싱 성공)** 인 경우에만
    복원·권위 갱신·격리 판정을 허용한다.
  - 다음이면 authoritative=False → **삭제·격리·복원 강제 안 함, 신규 BUY만 스킵**:
    API 오류/timeout/rate-limit/캐시 응답/파싱 실패/source!=api/불명확.
  - SELL·체결감시·기존 보유종목 보호는 이 판정과 무관하게 계속(호출부 책임).

완전성(complete)과 격리(broker-absent quarantine) — §1/§5:
  - complete=True(전 거래소·전 페이지 조회 완료) 인 **완전 스냅샷** 에서만
    'broker 부재(broker_absent)' 를 판정한다. broker_absent = 호출부가 **격리
    (BROKER_ABSENT_QUARANTINED)** 하거나(기본) 운영자 승인 시 최종 삭제할 후보.
  - complete=False(부분 거래소/부분 페이지/증거 없음) 이면 **이미 확인된 positive
    holding 만 복원** 가능하고, broker 부재 판정·격리는 **금지**(broker_absent=[]).
    일부 응답으로 실보유를 삭제/격리하는 사고를 막는다.
  - 이미 격리된 심볼(quarantined_symbols)은 broker_absent 재판정에서 제외한다.
    broker 잔고에 다시 나타나면 reappeared 로 보고(호출부가 즉시 정상 복구).

빈 잔고의 권위 판정(P0) — §7:
  - broker holdings 가 '빈 목록(양성 보유 0)'인데 내부 active 포지션이 존재하면,
    complete=True 라도 **권위 있는 0잔고로 확정하지 않는다**(authoritative=False,
    buy_allowed=False). 단일 빈응답으로 내부 포지션을 격리/삭제/제외하지 않는다.
    격리·삭제는 '양성 잔고 증거(broker 비어있지 않은 완전 스냅샷)' 또는 운영자 승인이
    있을 때만 허용한다. authoritative_empty=True 는 **내부 active 포지션이 없을 때만**
    성립한다(진짜 빈 계좌).
"""
from __future__ import annotations

from typing import Optional


class ReconcileResult:
    __slots__ = ("authoritative", "buy_allowed", "complete", "authoritative_empty",
                 "to_restore", "to_reconcile", "broker_absent", "reappeared",
                 "broker_count", "internal_count", "active_internal_count",
                 "quarantined_count", "mismatch_count", "reason")

    def __init__(self, authoritative, buy_allowed, complete, authoritative_empty,
                 to_restore, to_reconcile, broker_absent, reappeared,
                 broker_count, internal_count, active_internal_count,
                 quarantined_count, mismatch_count, reason):
        self.authoritative        = authoritative
        self.buy_allowed          = buy_allowed
        self.complete             = complete
        self.authoritative_empty  = authoritative_empty   # 완전·권위·보유0(정상 빈 잔고)
        self.to_restore           = to_restore        # broker 有·내부 active 無 → 복원/격리해제
        self.to_reconcile         = to_reconcile      # 양쪽 active 존재 → qty/avg 권위 갱신
        self.broker_absent        = broker_absent     # 내부 active 有·broker 無(완전스냅샷) → 격리후보
        self.reappeared           = reappeared        # 격리중이나 broker 재등장 → 정상 복구
        self.broker_count         = broker_count
        self.internal_count       = internal_count
        self.active_internal_count = active_internal_count
        self.quarantined_count    = quarantined_count
        self.mismatch_count       = mismatch_count
        self.reason               = reason

    def health(self) -> dict:
        """PII 없는 health 스냅샷(계좌·토큰·원문 미포함)."""
        return {
            "authoritative":       self.authoritative,
            "buy_allowed":         self.buy_allowed,
            "complete":            self.complete,
            "authoritative_empty": self.authoritative_empty,
            "restored_count":      len(self.to_restore),
            "reconciled_count":    len(self.to_reconcile),
            "broker_absent_count": len(self.broker_absent),
            "reappeared_count":    len(self.reappeared),
            "broker_count":        self.broker_count,
            "internal_count":      self.internal_count,
            "active_internal_count": self.active_internal_count,
            "quarantined_count":   self.quarantined_count,
            "mismatch_count":      self.mismatch_count,
            "reason":              self.reason,
        }


def is_authoritative(ok: bool, source: Optional[str], holdings) -> bool:
    """잔고 응답이 '명확한 성공'인지. 하나라도 불명확하면 False(fail-safe).

    ★ 빈 리스트([]) 는 '정상 성공한 빈 잔고'(authoritative empty)로 인정한다.
      오류로 인한 빈 응답은 호출부가 ok=False/source!=api 로 구분해 넘겨야 한다.
    """
    if not ok:
        return False
    if str(source or "").lower() != "api":   # 캐시/미지정/기타 → 비권위
        return False
    if not isinstance(holdings, list):        # 파싱 실패/None/dict → 비권위
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
    quarantined_symbols=None,
) -> ReconcileResult:
    """정합화 판정.

    Args:
      ok        : 잔고 조회 성공 여부(호출부가 raw 응답에서 판정; error 없음 등)
      source    : 데이터 출처('api' 만 권위; 'cache'/None → 비권위)
      holdings  : broker 보유 리스트 [{symbol, qty, avg_price, cur_price, name, excd}, ...]
      internal_symbols   : 내부 원장 전체 심볼(active + quarantined)
      complete  : 완전 스냅샷 증거(전 거래소·전 페이지). §5: 없으면 broker 부재
                  판정·격리 금지(복원만).
      quarantined_symbols: 이미 격리(BROKER_ABSENT_QUARANTINED)된 심볼 집합.

    Returns: ReconcileResult
    """
    internal    = set(internal_symbols or [])
    quarantined = set(quarantined_symbols or []) & internal
    active_internal = internal - quarantined

    if not is_authoritative(ok, source, holdings):
        # 비권위: 아무 것도 삭제/격리/복원하지 않는다. 신규 BUY만 스킵.
        return ReconcileResult(
            authoritative=False, buy_allowed=False, complete=False,
            authoritative_empty=False,
            to_restore=[], to_reconcile=[], broker_absent=[], reappeared=[],
            broker_count=0, internal_count=len(internal),
            active_internal_count=len(active_internal),
            quarantined_count=len(quarantined), mismatch_count=0,
            reason="broker_query_not_authoritative(fail-safe: no delete/quarantine/restore, skip new BUY)",
        )

    # 권위 성공: qty>0 만 유효 보유로 인정
    broker = {}
    for h in holdings:
        sym = str(h.get("symbol") or "").upper()
        q = _norm_qty(h)
        if sym and q > 0:
            broker[sym] = h

    broker_syms = set(broker.keys())

    # ★★ P0: broker 가 '빈 목록(양성 보유 0)'인데 내부 active 포지션이 존재하면 —
    #   휴장·조회지연·빈응답·불완전 응답에서 흔히 발생 — '권위 있는 0잔고'로 확정하지
    #   않는다(req1/req6). 단일 빈응답으로 격리·삭제·복원을 실행하지 않고,
    #   authoritative=False·buy_allowed=False 로 처리한다. 내부 포지션은 그대로 유지되어
    #   (호출부의 비권위 분기가 삭제/격리하지 않음) 시세감시·수익 트레일링·수동 SELL·
    #   체결조회가 계속되고, exposure 는 내부 qty×avg 를 포함한다. 신규 BUY·ADD 만 차단.
    #   → 명확한 '양성 잔고 증거(broker 비어있지 않음)' 또는 운영자 승인이 있을 때만
    #     격리/삭제가 가능하다(정상 broker_absent 경로).
    if len(broker_syms) == 0 and len(active_internal) > 0:
        return ReconcileResult(
            authoritative=False, buy_allowed=False, complete=complete,
            authoritative_empty=False,
            to_restore=[], to_reconcile=[], broker_absent=[], reappeared=[],
            broker_count=0, internal_count=len(internal),
            active_internal_count=len(active_internal),
            quarantined_count=len(quarantined), mismatch_count=0,
            reason=("empty_broker_with_internal_active(ambiguous empty balance: "
                    "no quarantine/delete/restore, block new BUY/ADD, keep positions active)"),
        )

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
        if sym in active_internal:
            to_reconcile.append(rec)     # 양쪽 active → 권위 갱신
        else:
            to_restore.append(rec)       # 신규 또는 격리중 재등장 → 복원/격리해제

    # 격리중이나 broker 잔고에 재등장한 심볼 → 즉시 정상 복구 대상(§ 재등장)
    reappeared = sorted(quarantined & broker_syms)

    # 내부 active 有·broker 無 → **완전 스냅샷일 때만** broker_absent(격리 후보). §5
    if complete:
        broker_absent = sorted(active_internal - broker_syms)
        reason = "authoritative_reconcile(complete: broker_absent → quarantine candidates)"
    else:
        broker_absent = []
        reason = ("authoritative_reconcile(incomplete_snapshot: restore positive holdings "
                  "only; broker-absence judgment & quarantine forbidden)")

    authoritative_empty = (len(broker_syms) == 0 and complete)
    mismatch = len(to_restore) + len(broker_absent)

    return ReconcileResult(
        authoritative=True, buy_allowed=True, complete=complete,
        authoritative_empty=authoritative_empty,
        to_restore=to_restore, to_reconcile=to_reconcile,
        broker_absent=broker_absent, reappeared=reappeared,
        broker_count=len(broker_syms), internal_count=len(internal),
        active_internal_count=len(active_internal),
        quarantined_count=len(quarantined), mismatch_count=mismatch,
        reason=reason,
    )
