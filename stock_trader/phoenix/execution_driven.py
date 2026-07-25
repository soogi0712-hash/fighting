"""phoenix/execution_driven.py — Execution-driven Position Update Bridge.

역할:
  OrderLifecycleManager.full_fill() 이 FILLED 단말에 도달했을 때
  정확히 1회 apply_buy() / apply_sell() 을 호출하는 콜백 게이트.

설계 원칙:
  1. 이 모듈 자체는 상태를 보유하지 않는다 — 멱등성은 lifecycle DB로 보장.
  2. on_buy_filled / on_sell_filled 콜백을 StrategyManager 가 주입한다.
  3. OrderLifecycleManager.full_fill() 의 on_filled 파라미터로 전달된다.
  4. PARTIALLY_FILLED → full_fill() 경로에서는 호출되지 않는다.
     (PARTIAL_FILL 중간 이벤트, FULL_FILL 단말 이벤트 구분)
  5. BUY 와 SELL 경로 모두 이 게이트를 통해서만 포지션을 변경한다.

Idempotency 보장 방법:
  - OrderLifecycle.full_fill() 내부에서 이미 FILLED 인 경우 조기 반환 (멱등).
  - 따라서 동일 order_lifecycle_id 로 full_fill() 이 두 번 호출되어도
    on_filled 콜백은 1회만 실행된다.

절대 금지:
  - apply_buy() / apply_sell() 를 이 모듈 밖에서 호출하지 않는다.
    (StrategyManager 의 rt_cd==0 경로에서 제거되어야 한다)
  - positions 테이블 직접 수정 금지.
  - DailyPnLGuard / Cooldown 을 이 모듈 안에서 조작하지 않는다.
    (on_sell_filled 콜백 내부에서 StrategyManager 가 처리한다)
"""
from __future__ import annotations

from typing import Callable, Optional
from utils.logger import get_logger

logger = get_logger("ExecutionDrivenPositionUpdater")


class ExecutionDrivenPositionUpdater:
    """FILLED 이벤트를 받아 apply_buy() / apply_sell() 을 정확히 1회 실행.

    Args:
        on_buy_filled:  BUY FILLED 시 호출. (order_lifecycle_id, lc) → None
        on_sell_filled: SELL FILLED 시 호출. (order_lifecycle_id, lc) → None

    사용법 (StrategyManager 초기화 시):
        updater = ExecutionDrivenPositionUpdater(
            on_buy_filled  = self._handle_buy_filled,
            on_sell_filled = self._handle_sell_filled,
        )
        # OrderLifecycleManager.full_fill() 에 on_filled=updater 로 전달
        self._lifecycle_mgr.full_fill(lc, on_filled=updater)
    """

    def __init__(
        self,
        on_buy_filled:  Optional[Callable] = None,
        on_sell_filled: Optional[Callable] = None,
    ):
        self._on_buy_filled  = on_buy_filled
        self._on_sell_filled = on_sell_filled

    def __call__(self, lc) -> None:
        """OrderLifecycleManager.full_fill() 이 FILLED 전이 후 호출하는 진입점.

        Args:
            lc: OrderLifecycle (FILLED 상태로 전이 완료된 인스턴스)
        """
        from phoenix.lifecycle import LifecycleState  # 순환 import 방지

        if lc.current_state != LifecycleState.FILLED:
            # 안전 가드 — 이미 FILLED 가 아니면 무시
            logger.warning(
                "on_filled 콜백이 FILLED 아닌 상태로 호출됨 — 무시: "
                "order_lifecycle_id=%s state=%s",
                lc.order_lifecycle_id, lc.current_state.name,
            )
            return

        side = (lc.side or "").upper()
        if side == "BUY":
            if self._on_buy_filled:
                logger.info(
                    "BUY FILLED → apply_buy 콜백 실행: "
                    "order_lifecycle_id=%s code=%s filled_qty=%s avg_fill_price=%s",
                    lc.order_lifecycle_id, lc.code,
                    lc.filled_qty, lc.avg_fill_price,
                )
                try:
                    self._on_buy_filled(lc)
                except Exception as exc:
                    logger.error(
                        "on_buy_filled 콜백 오류 (포지션 미반영): "
                        "order_lifecycle_id=%s error=%s",
                        lc.order_lifecycle_id, exc,
                    )
                    raise
            else:
                logger.warning(
                    "BUY FILLED 도달했으나 on_buy_filled 콜백 없음 — 포지션 미반영: "
                    "order_lifecycle_id=%s", lc.order_lifecycle_id,
                )
        elif side == "SELL":
            if self._on_sell_filled:
                logger.info(
                    "SELL FILLED → apply_sell 콜백 실행: "
                    "order_lifecycle_id=%s code=%s filled_qty=%s avg_fill_price=%s",
                    lc.order_lifecycle_id, lc.code,
                    lc.filled_qty, lc.avg_fill_price,
                )
                try:
                    self._on_sell_filled(lc)
                except Exception as exc:
                    logger.error(
                        "on_sell_filled 콜백 오류 (포지션 미반영): "
                        "order_lifecycle_id=%s error=%s",
                        lc.order_lifecycle_id, exc,
                    )
                    raise
            else:
                logger.warning(
                    "SELL FILLED 도달했으나 on_sell_filled 콜백 없음 — 포지션 미반영: "
                    "order_lifecycle_id=%s", lc.order_lifecycle_id,
                )
        else:
            logger.error(
                "알 수 없는 side — on_filled 콜백 스킵: "
                "order_lifecycle_id=%s side=%r",
                lc.order_lifecycle_id, lc.side,
            )
