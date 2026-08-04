"""신규 매수 차단 가드 (ETF/개별주 공용).

동일 종목 반복 신규매수를 막는다. 다음 중 하나라도 해당하면 신규 매수를 스킵한다:
  1) 이미 보유 중(is_held)
  2) 미체결(ACCEPTED/PARTIALLY_FILLED) 신규 매수 주문 존재(has_active_buy)
  3) 최근 매수 후 쿨다운(cooldown_sec) 이내 — 접수↔체결/잔고반영 지연 창에서
     동일 종목을 반복 매수하는 사고를 방지

순수 함수로 구현해 어떤 매수 경로(app.py ETF, StrategyManager 개별주)에서도
재사용·단위테스트가 가능하게 한다.
"""
from __future__ import annotations

DEFAULT_BUY_COOLDOWN_SEC = 180.0


def should_skip_new_buy(
    code: str,
    is_held: bool,
    has_active_buy: bool,
    recent_buy_ts: float | None = None,
    now_ts: float = 0.0,
    cooldown_sec: float = DEFAULT_BUY_COOLDOWN_SEC,
) -> tuple[bool, str]:
    """신규 매수를 건너뛰어야 하면 (True, 사유)를, 아니면 (False, "")를 반환.

    Args:
        code:           종목코드 (로그용)
        is_held:        현재 보유 중 여부
        has_active_buy: 미체결 신규 매수 주문 존재 여부
        recent_buy_ts:  직전 매수 시각(epoch sec). None 이면 쿨다운 미적용
        now_ts:         현재 시각(epoch sec)
        cooldown_sec:   최근 매수 후 신규 매수 금지 시간(초)
    """
    if is_held:
        return True, f"{code} 이미 보유 중 — 신규 매수 스킵"
    if has_active_buy:
        return True, f"{code} 미체결 매수 주문 존재 — 신규 매수 스킵"
    if recent_buy_ts is not None and cooldown_sec > 0:
        elapsed = now_ts - recent_buy_ts
        if 0 <= elapsed < cooldown_sec:
            return True, (
                f"{code} 최근 매수 {elapsed:.0f}s 경과 "
                f"< 쿨다운 {cooldown_sec:.0f}s — 신규 매수 스킵"
            )
    return False, ""
