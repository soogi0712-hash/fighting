"""
integration.py — 기존 trade_log 항목(dict) → LedgerRecorder 매핑

목적:
  - KR/US 관리자가 이미 만드는 '거래 로그 항목(dict)'을 그대로 받아 원장에 이중기록.
  - 매핑 로직을 독립 함수로 분리해 오프라인 단위테스트가 가능하게 한다.
  - 라이브 코드(strategy_manager/us_strategy_manager) 수정은 '항목 dict + 시장'을 넘기는
    한 줄로 최소화한다. 실패해도 매매를 막지 않도록 호출측에서 try/except 로 감싼다.

주의:
  - 이 함수는 기록만 한다. 주문/조회 API 를 호출하지 않는다.
  - 주문번호(ODNO)가 항목에 없으면 합성 키(code-timestamp)로 폴백한다.
    (증권사 체결 검증 정확도를 위해, 추후 호출측에서 result 의 ODNO 를
     entry["order_no"] 로 넘겨주면 자동으로 사용된다.)
"""

# 진입/청산 지표로 함께 남길 점수 키들 (기존 extra 에 이미 존재)
_INDICATOR_KEYS = (
    "buy_score", "sell_score", "ind_score", "trend_score",
    "level", "strong_trend", "rsi", "vol_ratio", "vwap",
)


def _indicators(entry: dict) -> dict:
    return {k: entry[k] for k in _INDICATOR_KEYS if k in entry and entry[k] is not None}


def _code(entry: dict):
    return entry.get("code") or entry.get("symbol")


def _order_no(entry: dict) -> str:
    ono = entry.get("order_no") or entry.get("odno") or entry.get("ODNO")
    if ono:
        return str(ono)
    # 폴백: 체결시각 기반 합성 키 (멱등성은 (side, order_no) 조합으로 보장)
    from datetime import datetime
    ts = entry.get("timestamp") or datetime.now().isoformat()
    return f"{_code(entry) or '?'}-{ts}"


def record_from_log(recorder, entry: dict, market: str):
    """
    trade_log 항목(dict)을 원장에 반영.
    entry 필수: action('BUY'/'SELL' 접두), code, price, qty
    entry 선택: name, reason, timestamp, decision_price, strategy_version,
                param_snapshot, order_no, 그리고 점수 지표들(extra 병합분)
    반환: True(기록 시도됨) / False(대상 아님)
    """
    if recorder is None or not entry:
        return False
    action = str(entry.get("action", ""))
    code   = _code(entry)
    price  = entry.get("price")
    qty    = entry.get("qty")
    if not code or not price or not qty:
        return False

    ono   = _order_no(entry)
    ts    = entry.get("timestamp")
    inds  = _indicators(entry)
    reason = entry.get("reason")
    dprice = entry.get("decision_price")

    if action.startswith("BUY") or action.startswith("ADD"):
        recorder.on_buy_fill(
            market, code, entry.get("name"), ono, price, qty,
            decision_price=dprice,
            strategy_version=entry.get("strategy_version"),
            param_snapshot=entry.get("param_snapshot"),
            entry_reason=reason, entry_indicators=inds, ts=ts,
        )
        return True

    if action.startswith("SELL"):
        recorder.on_sell_fill(
            market, code, ono, price, qty,
            decision_price=dprice,
            exit_reason=reason, exit_indicators=inds, ts=ts,
            rates=entry.get("rates"),
        )
        return True

    return False
