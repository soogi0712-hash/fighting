"""주문수량 최종 확정 — 예수금이 아니라 KIS '현금 주문가능금액·수량'을 권위값으로.

국내·미국 매수 공통 규칙(단순 예수금 나눗셈 금지):

    최종수량 = min(
        전략 산출수량,
        KIS 현금 주문가능수량,                       (미수/신용 제외)
        floor(KIS 현금 주문가능금액 * CASH_BUFFER / 실제 주문가격)
    )

- CASH_BUFFER(0.98)= 수수료·환율·호가 슬리피지 여유. 가능금액을 100% 쓰지 않는다.
- 주문가격이 0 이하이거나 금액/수량이 0 이면 0 을 반환한다(호출측에서 BUY_BLOCKED).
- 순수 함수(부수효과·네트워크 없음) → 국내/미국 양쪽에서 그대로 재사용·단위테스트.
"""
from __future__ import annotations

import math

# 현금 주문가능금액을 주문에 쓸 때의 안전 버퍼(수수료/환율/슬리피지). 100% 사용 금지.
CASH_BUFFER: float = 0.98


def qty_from_cash(cash_amount, order_price, buffer: float = CASH_BUFFER) -> int:
    """floor(현금 주문가능금액 * buffer / 주문가격). 유효하지 않으면 0."""
    try:
        amt   = float(cash_amount)
        price = float(order_price)
    except (TypeError, ValueError):
        return 0
    if amt <= 0.0 or price <= 0.0:
        return 0
    return int(math.floor((amt * buffer) / price))


def finalize_order_qty(strategy_qty, kis_orderable_qty, kis_cash_amount,
                       order_price, buffer: float = CASH_BUFFER) -> int:
    """전략수량·KIS현금주문가능수량·현금가능금액환산수량의 최솟값(≥0).

    셋 중 하나라도 0 이면 0(주문 불가). 음수/파싱불가 입력도 0 으로 안전화한다.
    """
    try:
        s_qty = int(strategy_qty)
        k_qty = int(kis_orderable_qty)
    except (TypeError, ValueError):
        return 0
    if s_qty <= 0 or k_qty <= 0:
        return 0
    amt_qty = qty_from_cash(kis_cash_amount, order_price, buffer)
    if amt_qty <= 0:
        return 0
    return max(0, min(s_qty, k_qty, amt_qty))
