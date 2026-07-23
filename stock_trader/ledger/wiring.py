"""
wiring.py — 체결 기반 기록 라우터 + 재시작 정합성

record_trade_event():
  - 매매 이벤트(trade_log dict)를 받아 '실제 체결'만 원장에 기록한다.
  - fill_source 가 없으면(=체결 확인 불가) 접수값을 체결로 기록하지 않는다(안전).
  - fill_source 가 실제 체결(수량/체결가/주문번호)을 주면 그 값으로 기록한다.
    → 부분체결은 실제 체결수량으로, 중복체결은 (side, order_no) 멱등으로 처리.
  - 실패해도 예외를 밖으로 던지지 않는다(매매 흐름 보호). 실패는 health 에 집계.

reconcile():
  - 재시작 후 ledger 의 OPEN/PARTIAL 포지션과 브로커 실보유를 대조한다.
  - 실 API 호출은 하지 않는다(브로커 보유 dict 를 인자로 받음 — PHASE 5에서 주입).
"""
from .integration import record_from_log, _code


def _side_of(action: str):
    a = str(action)
    if a.startswith("SELL"):
        return "SELL"
    if a.startswith("BUY") or a.startswith("ADD"):
        return "BUY"
    return None


def record_trade_event(recorder, health, entry, market, fill_source):
    """반환: 상태 문자열(recorded/no_fill_source/no_fill/not_trade/empty/error)."""
    if not entry:
        return "empty"
    side = _side_of(entry.get("action", ""))
    if side is None:
        return "not_trade"
    code = _code(entry)
    order_no = entry.get("order_no")

    # 체결 확인 소스가 없으면 '접수'를 '체결'로 기록하지 않는다.
    if fill_source is None:
        return "no_fill_source"

    try:
        fills = fill_source.get_fills(
            market, code, side,
            requested_qty=entry.get("qty"),
            requested_price=entry.get("price"),
            order_hint=order_no,
            ts=entry.get("timestamp"),
        )
        if not fills:
            if health:
                health.record_fail(market, code, entry.get("action"), order_no, "no_fill_confirmed")
            return "no_fill"
        for f in fills:
            e = dict(entry)
            e["price"]    = f.price          # 실제 체결가
            e["qty"]      = f.qty            # 실제 체결수량(부분체결 반영)
            e["order_no"] = f.order_no       # 실제 주문번호
            if f.ts:
                e.setdefault("timestamp", f.ts)
            record_from_log(recorder, e, market)
        if health:
            health.record_ok()
        return "recorded"
    except Exception as ex:
        if health:
            health.record_fail(market, code, entry.get("action"), order_no, repr(ex))
        return "error"


def reconcile(recorder, broker_positions: dict, market: str) -> dict:
    """
    ledger OPEN/PARTIAL vs 브로커 실보유(dict: {code: qty}) 대조.
    실 API 호출 없음. 반환: 불일치 리포트.
      - ledger_only:   ledger엔 열려있으나 브로커엔 없음(외부청산/유령)
      - broker_only:   브로커엔 있으나 ledger엔 없음(재시작 복원 등 미추적)
      - qty_mismatch:  수량 불일치
    """
    rows = recorder.conn.execute(
        "SELECT code, entry_qty_total, exit_qty_total FROM trades "
        "WHERE market=? AND status IN ('OPEN','PARTIAL')", (market,)
    ).fetchall()
    ledger_open = {r["code"]: (r["entry_qty_total"] or 0) - (r["exit_qty_total"] or 0)
                   for r in rows}
    broker = {k: int(v) for k, v in (broker_positions or {}).items()}

    ledger_only  = sorted(set(ledger_open) - set(broker))
    broker_only  = sorted(set(broker) - set(ledger_open))
    qty_mismatch = {c: {"ledger": ledger_open[c], "broker": broker[c]}
                    for c in set(ledger_open) & set(broker)
                    if ledger_open[c] != broker[c]}
    return {
        "market": market,
        "ledger_only": ledger_only,
        "broker_only": broker_only,
        "qty_mismatch": qty_mismatch,
        "consistent": not ledger_only and not broker_only and not qty_mismatch,
    }
