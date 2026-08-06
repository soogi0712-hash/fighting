from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from screener.transaction_cost import net_profit_pct_from_cost, price_for_net_pct_from_cost


SELL_SCORE_THRESHOLD = 7


def normalize_sell_score(raw_score: object) -> int:
    """Normalize dedicated SELL_SCORE values into a consistent integer."""
    if raw_score is None:
        return 0

    if isinstance(raw_score, (int, float)):
        value = float(raw_score)
        if value <= 1.0:
            return 0
        return int(round(value))

    if isinstance(raw_score, str):
        try:
            value = float(raw_score)
        except ValueError:
            return 0
        if value <= 1.0:
            return 0
        return int(round(value))

    return 0


class SellReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    SIGNAL_EXIT = "SIGNAL_EXIT"
    TIME_EXIT = "TIME_EXIT"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    EMERGENCY_STOP = "EMERGENCY_STOP"


@dataclass
class SellDecision:
    action: str
    reason: str
    sell_type: Optional[str] = None
    sell_reason: Optional[SellReason] = None
    is_forced: bool = False
    net_pct: Optional[float] = None
    elapsed_min: Optional[float] = None
    sell_score: Optional[int] = None
    payload: Optional[dict] = None

    def to_dict(self) -> dict:
        data = {
            "action": self.action,
            "reason": self.reason,
            "sell_type": self.sell_type,
            "sell_reason": self.sell_reason.value if self.sell_reason else None,
            "is_forced": self.is_forced,
            "net_pct": self.net_pct,
            "elapsed_min": self.elapsed_min,
            "sell_score": self.sell_score,
        }
        if self.payload:
            data.update(self.payload)
        return data


class SellDecisionEngine:
    """Sell decision logic split from TradeDecisionEngine."""

    STOP_LOSS_PCT = -10.0
    GENERAL_STOP_LOSS_PCT = -1.2
    EMERGENCY_STOP_PCT = -3.0
    MIN_HOLD_MINUTES = 5
    GENERAL_STOP_LOSS_SCORE_THRESHOLD = SELL_SCORE_THRESHOLD
    TRAILING_STOP_PCT = -12.0
    TRAILING_ACTIVATE_NET_PCT = 1.5
    MA20_EXIT_BUFFER = -1.0

    def evaluate(self, position: dict, score_result: dict, *, sell_score: int = 0) -> SellDecision:
        code = position.get("code", "")
        name = position.get("name", "")
        avg_price = position.get("avg_price")
        highest_price = position.get("highest_price", avg_price)
        qty = position.get("qty", 0)
        cur_price = score_result.get("cur_price", position.get("cur_price", avg_price))

        if not cur_price:
            return SellDecision("HOLD", f"{name}({code}) 현재가없음")

        net_pct = net_profit_pct_from_cost(avg_price, cur_price)
        trail_pct = ((cur_price - highest_price) / highest_price * 100) if highest_price else 0.0
        ma20 = score_result.get("price_ma20", 0)
        ma20_pct = (cur_price - ma20) / ma20 * 100 if ma20 else 0.0

        now = datetime.now()
        created_at = position.get("created_at")
        elapsed_min = 0.0
        if created_at:
            try:
                elapsed_min = (now - datetime.fromisoformat(created_at)).total_seconds() / 60
            except Exception:
                elapsed_min = 0.0

        if net_pct <= self.EMERGENCY_STOP_PCT:
            return SellDecision(
                "SELL",
                f"긴급손절(실질{net_pct:.2f}% ≤ {self.EMERGENCY_STOP_PCT}%)",
                sell_type="STOP_LOSS",
                sell_reason=SellReason.EMERGENCY_STOP,
                is_forced=True,
                net_pct=round(net_pct, 2),
                elapsed_min=round(elapsed_min, 1),
                sell_score=sell_score,
            )

        if net_pct <= self.GENERAL_STOP_LOSS_PCT:
            if elapsed_min < self.MIN_HOLD_MINUTES:
                return SellDecision(
                    "HOLD",
                    f"일반손절보류(실질{net_pct:.2f}% ≤ {self.GENERAL_STOP_LOSS_PCT}%, 보유{elapsed_min:.1f}분)",
                    net_pct=round(net_pct, 2),
                    elapsed_min=round(elapsed_min, 1),
                    sell_score=sell_score,
                )
            if sell_score >= self.GENERAL_STOP_LOSS_SCORE_THRESHOLD:
                return SellDecision(
                    "SELL",
                    f"일반손절(실질{net_pct:.2f}% ≤ {self.GENERAL_STOP_LOSS_PCT}%, SELL_SCORE={sell_score})",
                    sell_type="STOP_LOSS",
                    sell_reason=SellReason.STOP_LOSS,
                    is_forced=False,
                    net_pct=round(net_pct, 2),
                    elapsed_min=round(elapsed_min, 1),
                    sell_score=sell_score,
                )
            return SellDecision(
                "HOLD",
                f"일반손절보류(실질{net_pct:.2f}% ≤ {self.GENERAL_STOP_LOSS_PCT}%, SELL_SCORE={sell_score})",
                net_pct=round(net_pct, 2),
                elapsed_min=round(elapsed_min, 1),
                sell_score=sell_score,
            )

        # 기존 트레일링 / MA20 / score-drop 경로는 보존
        activate_price = price_for_net_pct_from_cost(avg_price, self.TRAILING_ACTIVATE_NET_PCT)
        trailing_active = highest_price >= activate_price if highest_price else False
        if trailing_active and trail_pct <= self.TRAILING_STOP_PCT:
            return SellDecision(
                "SELL",
                f"트레일링스탑(고점대비{trail_pct:.2f}% ≤ {self.TRAILING_STOP_PCT}%, 실질{net_pct:.2f}%)",
                sell_type="TRAILING_STOP",
                sell_reason=SellReason.TRAILING_STOP,
                is_forced=True,
                net_pct=round(net_pct, 2),
                elapsed_min=round(elapsed_min, 1),
                sell_score=sell_score,
            )

        if ma20 and net_pct > 0 and ma20_pct <= self.MA20_EXIT_BUFFER:
            return SellDecision(
                "SELL",
                f"MA20추세이탈(실질{net_pct:.2f}%, MA20괴리{ma20_pct:.2f}%)",
                sell_type="MA20_EXIT",
                sell_reason=SellReason.SIGNAL_EXIT,
                is_forced=False,
                net_pct=round(net_pct, 2),
                elapsed_min=round(elapsed_min, 1),
                sell_score=sell_score,
            )

        grade = score_result.get("grade", "")
        score = score_result.get("total_score", 100)
        if grade == "EXCLUDE" and score < 40:
            return SellDecision(
                "SELL",
                f"AI점수급락({score:.0f}pts/{grade})",
                sell_type="SCORE_DROP",
                sell_reason=SellReason.SIGNAL_EXIT,
                is_forced=False,
                net_pct=round(net_pct, 2),
                elapsed_min=round(elapsed_min, 1),
                sell_score=sell_score,
            )

        return SellDecision(
            "HOLD",
            f"추세유지(실질{net_pct:+.2f}% 고점대비{trail_pct:+.2f}% MA20={ma20_pct:+.2f}% 트레일활성={'ON' if trailing_active else 'OFF'})",
            net_pct=round(net_pct, 2),
            elapsed_min=round(elapsed_min, 1),
            sell_score=sell_score,
        )
