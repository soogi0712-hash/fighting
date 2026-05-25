"""
Shadow Portfolio — 가상 포트폴리오 & 성과 지표 엔진
======================================================

★ 핵심 원칙 ★
  - 실계좌 주문 절대 금지
  - 모든 매수·매도는 가상(virtual)
  - 실제 시장 가격을 그대로 반영하여 현실적 성과 측정

★ 실질 손익 원칙 ★
  - 모든 수익률·손절·익절·추가매수 판단은
    수수료·세금을 차감한 실질 순손익 기준
  - 단순 주가 변동률 사용 금지
  - transaction_cost.py 가 단일 비용 원천

  총매수금액 = 매수금액 + 매수수수료
  순매도금액 = 매도금액 - 매도수수료 - 증권거래세
  실질수익률 = (순매도금액 - 총매수금액) / 총매수금액 × 100

성과 지표:
  - 총수익률 / 연환산수익률(CAGR)  ← 실질 손익 기준
  - 최대낙폭(MDD)                   ← 실질 equity curve
  - 샤프비율
  - 승률 / 평균수익 / 평균손실 / 손익비(Profit Factor)
  - 거래횟수 / 평균보유기간
  - 월별 수익률
  - 최근 3개월·6개월 성과
  - 총 비용(수수료+세금) 집계
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import math
from datetime import datetime, date, timedelta
from collections import defaultdict
from typing import Optional

from utils.logger import get_logger
from strategy_lab.lab_config import StrategyConfig, TIER_LIVE
from screener.transaction_cost import (
    calc_buy_cost, calc_sell_proceeds, calc_trade_result,
    net_profit_pct as tc_net_pct,
    price_for_net_pct,
    price_for_net_pct_from_cost,   # ★ avg_price(수수료포함) 기준 역산
    calc_buy_qty,
    summarize_costs,
    BUY_COMMISSION_RATE, SELL_COMMISSION_RATE,
    TRANSACTION_TAX_RATE,
)

logger = get_logger("ShadowPortfolio")

# ── 가상 포지션 ─────────────────────────────────────────────
class VirtualPosition:
    """단일 종목 가상 포지션"""

    def __init__(self, code: str, name: str,
                 entry_price: float, qty: int,
                 entry_date: str, strategy_id: str):
        self.code           = code
        self.name           = name
        self.entry_price    = entry_price
        self.qty            = qty
        self.entry_date     = entry_date
        self.strategy_id    = strategy_id
        self.highest_price  = entry_price   # 트레일링스탑 추적
        self.avg_price      = entry_price
        self.add_buy_done   = []            # 완료된 추가매수 레벨 (%)
        self.created_ts     = datetime.now().isoformat()

        # 추가매수로 수량·평균가 변경 시 내역
        self.add_history: list[dict] = []

    def update_high(self, price: float):
        if price > self.highest_price:
            self.highest_price = price

    def add_buy(self, price: float, qty: int, level_pct: float):
        """
        추가매수 반영.
        avg_price 는 수수료 포함 실제 취득 원가(주당) 기준.
        """
        # 기존 총 취득 원가 = avg_price(비용 포함 주당가) × 기존 수량
        old_total_cost = self.avg_price * self.qty
        # 추가매수 총 취득 원가 = (price + 수수료/qty)
        add_bc         = calc_buy_cost(price, qty)
        add_total_cost = add_bc.total_cost

        new_qty        = self.qty + qty
        # 새 평균 취득 원가 (주당, 수수료 포함)
        self.avg_price = (old_total_cost + add_total_cost) / new_qty
        self.qty       = new_qty
        self.add_buy_done.append(level_pct)
        self.add_history.append({
            "price":        price,
            "qty":          qty,
            "level_pct":    level_pct,
            "buy_cost":     round(add_bc.total_cost, 0),
            "date":         date.today().isoformat(),
        })

    def unrealized_pct(self, cur_price: float) -> float:
        """
        현재가 기준 실질 미실현 수익률 (매도 수수료·세금 가정).
        avg_price 는 수수료 포함 주당 취득 원가.
        """
        if self.avg_price <= 0:
            return 0.0
        # 수수료 포함 평균 취득 원가 → qty=1 기준 net_pct 계산
        # avg_price 가 이미 "수수료 포함 주당 원가"이므로
        # 매도 비용(수수료+세금)만 차감하면 됨
        sp = calc_sell_proceeds(cur_price, 1)
        net_proceeds_per_share = sp.net_proceeds
        return (net_proceeds_per_share - self.avg_price) / self.avg_price * 100

    def to_dict(self) -> dict:
        return {
            "code":          self.code,
            "name":          self.name,
            "entry_price":   self.entry_price,
            "qty":           self.qty,
            "avg_price":     round(self.avg_price, 4),  # 수수료 포함 주당 취득원가
            "entry_date":    self.entry_date,
            "strategy_id":   self.strategy_id,
            "highest_price": self.highest_price,
            "add_buy_done":  self.add_buy_done,
            "add_history":   self.add_history,
            "created_ts":    self.created_ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VirtualPosition":
        obj = cls(
            d["code"], d["name"],
            d["entry_price"], d["qty"],
            d["entry_date"], d["strategy_id"],
        )
        obj.avg_price      = d.get("avg_price", d["entry_price"])
        obj.highest_price  = d.get("highest_price", d["entry_price"])
        obj.add_buy_done   = d.get("add_buy_done", [])
        obj.add_history    = d.get("add_history", [])
        obj.created_ts     = d.get("created_ts", "")
        return obj


# ── 가상 거래 기록 ───────────────────────────────────────────
class VirtualTrade:
    """매수·매도 거래 1건"""

    def __init__(self, strategy_id: str, code: str, name: str,
                 action: str,          # "BUY" / "SELL" / "ADD_BUY"
                 price: float, qty: int,
                 date_str: str,
                 profit: float = 0.0,      # 실질 순손익 (수수료·세금 차감)
                 profit_pct: float = 0.0,  # 실질 수익률 (%)
                 hold_days: int = 0,
                 reason: str = "",
                 buy_commission:   float = 0.0,
                 sell_commission:  float = 0.0,
                 transaction_tax:  float = 0.0,
                 total_fee:        float = 0.0):
        self.strategy_id     = strategy_id
        self.code            = code
        self.name            = name
        self.action          = action
        self.price           = price
        self.qty             = qty
        self.date            = date_str
        self.profit          = profit        # 실질 순손익
        self.profit_pct      = profit_pct    # 실질 수익률
        self.hold_days       = hold_days
        self.reason          = reason
        self.buy_commission  = buy_commission
        self.sell_commission = sell_commission
        self.transaction_tax = transaction_tax
        self.total_fee       = total_fee
        self.ts              = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "strategy_id":    self.strategy_id,
            "code":           self.code,
            "name":           self.name,
            "action":         self.action,
            "price":          self.price,
            "qty":            self.qty,
            "date":           self.date,
            "profit":         round(self.profit, 0),       # 실질 순손익
            "profit_pct":     round(self.profit_pct, 4),   # 실질 수익률
            "hold_days":      self.hold_days,
            "reason":         self.reason,
            "buy_commission":  round(self.buy_commission,  0),
            "sell_commission": round(self.sell_commission, 0),
            "transaction_tax": round(self.transaction_tax, 0),
            "total_fee":       round(self.total_fee,       0),
            "ts":             self.ts,
        }


# ── 성과 지표 계산기 ─────────────────────────────────────────
class MetricsCalculator:
    """
    거래 내역 + 자산 곡선으로 전략 성과 지표 계산
    """

    @staticmethod
    def calc(trades: list[dict],
             equity_curve: list[float],
             initial_capital: float,
             start_date: str = "",
             end_date:   str = "") -> dict:
        """
        trades       : [{action, profit, hold_days, ...}]
        equity_curve : [daily_equity, ...]
        """
        # SELL 거래만 성과 집계 (ADD_BUY·BUY 제외)
        sell_trades = [t for t in trades if t["action"] in ("SELL",)]

        if not equity_curve or len(equity_curve) < 2:
            return MetricsCalculator._empty()

        final_cap = equity_curve[-1]

        # ── 기간 ──────────────────────────────────────────
        days = len(equity_curve)
        years = max(days / 252, 1 / 252)

        # ── 총수익률 ──────────────────────────────────────
        total_return = (final_cap - initial_capital) / initial_capital * 100

        # ── CAGR ─────────────────────────────────────────
        cagr = ((final_cap / initial_capital) ** (1 / years) - 1) * 100

        # ── MDD ───────────────────────────────────────────
        eq     = equity_curve
        mdd    = 0.0
        peak   = eq[0]
        for v in eq:
            if v > peak:
                peak = v
            dd = (v - peak) / peak * 100
            if dd < mdd:
                mdd = dd

        # ── 샤프비율 ──────────────────────────────────────
        if len(equity_curve) >= 2:
            rets = [(equity_curve[i] - equity_curve[i-1]) / equity_curve[i-1]
                    for i in range(1, len(equity_curve))]
            n = len(rets)
            if n >= 2:
                mean_r = sum(rets) / n
                var_r  = sum((r - mean_r) ** 2 for r in rets) / n
                std_r  = math.sqrt(var_r) if var_r > 0 else 0.0
                sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0.0
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0

        # ── 승률·손익비 (실질 순손익 기준) ──────────────
        profits = [t["profit"] for t in sell_trades if t.get("profit", 0) > 0]
        losses  = [t["profit"] for t in sell_trades if t.get("profit", 0) < 0]
        n_sell  = len(sell_trades)

        win_rate = (len(profits) / n_sell * 100) if n_sell > 0 else 0.0
        avg_win  = (sum(profits) / len(profits)) if profits else 0.0
        avg_loss = (sum(losses)  / len(losses))  if losses  else 0.0
        profit_factor = (
            abs(sum(profits) / sum(losses))
            if losses and sum(losses) != 0 else
            float("inf") if profits else 0.0
        )
        if profit_factor == float("inf"):
            profit_factor = 99.9

        # ── 평균 보유기간 ─────────────────────────────────
        hold_days_list = [t.get("hold_days", 0) for t in sell_trades]
        avg_hold = (sum(hold_days_list) / len(hold_days_list)) if hold_days_list else 0

        # ── 월별 수익률 (실질 손익 합계) ─────────────────
        monthly = MetricsCalculator._monthly_returns(sell_trades)

        # ── 최근 3개월·6개월 수익률 ──────────────────────
        now    = date.today()
        d3m    = (now - timedelta(days=90)).isoformat()
        d6m    = (now - timedelta(days=180)).isoformat()
        t3m    = [t for t in sell_trades if t.get("date","") >= d3m]
        t6m    = [t for t in sell_trades if t.get("date","") >= d6m]
        ret_3m = sum(t["profit"] for t in t3m) / initial_capital * 100
        ret_6m = sum(t["profit"] for t in t6m) / initial_capital * 100

        # ── 총 비용 집계 ──────────────────────────────────
        cost_summary = summarize_costs(trades)

        return {
            "trade_count":    n_sell,
            "total_return":   round(total_return, 2),
            "cagr":           round(cagr, 2),
            "mdd":            round(mdd, 2),
            "sharpe":         round(sharpe, 2),
            "win_rate":       round(win_rate, 1),
            "avg_win":        round(avg_win, 0),
            "avg_loss":       round(avg_loss, 0),
            "profit_factor":  round(min(profit_factor, 99.9), 2),
            "avg_hold_days":  round(avg_hold, 1),
            "monthly":        monthly,
            "return_3m":      round(ret_3m, 2),
            "return_6m":      round(ret_6m, 2),
            "final_capital":  round(final_cap, 0),
            "initial_capital":round(initial_capital, 0),
            # 비용 집계
            "total_fee":             cost_summary["total_fee"],
            "total_buy_commission":  cost_summary["total_buy_commission"],
            "total_sell_commission": cost_summary["total_sell_commission"],
            "total_transaction_tax": cost_summary["total_transaction_tax"],
        }

    @staticmethod
    def _monthly_returns(sell_trades: list[dict]) -> dict:
        """월별 수익 합계 → {'2026-01': 1200000, ...}"""
        monthly = defaultdict(float)
        for t in sell_trades:
            d = t.get("date", "")[:7]   # "YYYY-MM"
            if d:
                monthly[d] += t.get("profit", 0)
        return dict(sorted(monthly.items()))

    @staticmethod
    def _empty() -> dict:
        return {
            "trade_count": 0, "total_return": 0.0, "cagr": 0.0,
            "mdd": 0.0, "sharpe": 0.0, "win_rate": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 0.0,
            "avg_hold_days": 0.0, "monthly": {},
            "return_3m": 0.0, "return_6m": 0.0,
            "final_capital": 0.0, "initial_capital": 0.0,
            "total_fee": 0.0, "total_buy_commission": 0.0,
            "total_sell_commission": 0.0, "total_transaction_tax": 0.0,
        }


# ── Shadow Portfolio ─────────────────────────────────────────
class ShadowPortfolio:
    """
    단일 전략의 가상 포트폴리오.
    실계좌와 완전 분리된 가상 매매만 수행.
    """

    INITIAL_CAPITAL = 10_000_000   # 가상 초기 자금 1천만원

    def __init__(self, strategy: StrategyConfig):
        self.strategy        = strategy
        self.capital         = self.INITIAL_CAPITAL   # 가상 현금
        self.positions: dict[str, VirtualPosition] = {}
        self.trades:    list[VirtualTrade]          = []
        self.equity_curve:  list[float]             = [self.INITIAL_CAPITAL]
        self._safety_check()

    def _safety_check(self):
        """실전 전략이 실수로 가상 포트폴리오로 처리되는 것 방지"""
        if self.strategy.is_real_order:
            logger.warning(
                f"[안전장치] {self.strategy.id}: LIVE 전략은 ShadowPortfolio 대신 "
                f"실제 주문 엔진을 사용해야 합니다."
            )

    # ── 가상 매수 ─────────────────────────────────────────
    def virtual_buy(self, code: str, name: str,
                    price: float, date_str: str,
                    reason: str = "") -> dict:
        """
        가상 1단계 진입 (첫 매수). 실계좌 주문 절대 없음.

        수수료 처리:
          - calc_buy_qty()로 수수료 포함 매수 가능 수량 산출
          - 차감 금액 = 총매수금액 (매수금액 + 수수료)
          - VirtualPosition.avg_price = 수수료 포함 주당 취득원가
        """
        if self.strategy.is_real_order:
            return {"ok": False, "reason": "실전전략은 ShadowPortfolio 사용 불가"}

        if code in self.positions:
            return {"ok": False, "reason": "이미 포지션 보유 중"}

        # 수수료 포함 매수 가능 수량
        qty = calc_buy_qty(
            self.capital, price,
            invest_ratio=self.strategy.entry_ratio
        )
        if qty < 1:
            return {"ok": False, "reason": "가상 자금 부족"}

        bc = calc_buy_cost(price, qty)
        if bc.total_cost > self.capital:
            return {"ok": False, "reason": "가상 자금 부족(수수료 포함)"}

        # 총매수금액(수수료 포함) 차감
        self.capital -= bc.total_cost

        # avg_price = 수수료 포함 주당 취득원가
        avg_price_with_fee = bc.total_cost / qty

        pos = VirtualPosition(
            code=code, name=name,
            entry_price=price, qty=qty,
            entry_date=date_str,
            strategy_id=self.strategy.id,
        )
        pos.avg_price = avg_price_with_fee  # 수수료 포함 원가로 덮어쓰기
        self.positions[code] = pos

        trade = VirtualTrade(
            strategy_id=self.strategy.id,
            code=code, name=name,
            action="BUY",
            price=price, qty=qty,
            date_str=date_str,
            reason=reason or f"가상진입 {self.strategy.name}",
            buy_commission=bc.commission,
            total_fee=bc.commission,
        )
        self.trades.append(trade)
        logger.debug(
            f"[{self.strategy.id}] 가상매수 {name} {qty}주 @{price:,} "
            f"(수수료 {bc.commission:.0f}원, 총원가 {bc.total_cost:,.0f}원)"
        )
        return {"ok": True, "qty": qty, "total_cost": bc.total_cost,
                "commission": bc.commission}

    # ── 가상 추가매수 ────────────────────────────────────
    def virtual_add_buy(self, code: str, cur_price: float,
                        date_str: str) -> dict:
        """
        추가매수 조건 확인 후 가상 추가매수.

        ★ 추가매수 조건은 실질수익률 기준 ★
          - pos.unrealized_pct(cur_price)가 level_pct 이상일 때
          - 단순 주가 상승률이 아닌 수수료·세금 차감 후 실질 수익률
        """
        if self.strategy.is_real_order:
            return {"ok": False, "reason": "실전전략"}

        pos = self.positions.get(code)
        if pos is None:
            return {"ok": False, "reason": "포지션 없음"}
        if not self.strategy.add_buy_levels:
            return {"ok": False, "reason": "추가매수 없는 전략"}

        # ★ 실질 수익률로 판단 (수수료·세금 차감)
        net_gain_pct = pos.unrealized_pct(cur_price)

        for level_pct in self.strategy.add_buy_levels:
            if level_pct in pos.add_buy_done:
                continue
            if net_gain_pct >= level_pct:
                # 수수료 포함 매수 가능 수량
                qty = calc_buy_qty(
                    self.capital, cur_price,
                    invest_ratio=self.strategy.add_buy_ratio
                )
                if qty < 1:
                    return {"ok": False, "reason": "자금 부족"}

                bc = calc_buy_cost(cur_price, qty)
                if bc.total_cost > self.capital:
                    return {"ok": False, "reason": "자금 부족(수수료 포함)"}

                self.capital -= bc.total_cost
                pos.add_buy(cur_price, qty, level_pct)  # avg_price 자동 갱신

                trade = VirtualTrade(
                    strategy_id=self.strategy.id,
                    code=code, name=pos.name,
                    action="ADD_BUY",
                    price=cur_price, qty=qty,
                    date_str=date_str,
                    reason=f"가상추가매수 실질+{net_gain_pct:.1f}%(기준+{level_pct}%)",
                    buy_commission=bc.commission,
                    total_fee=bc.commission,
                )
                self.trades.append(trade)
                logger.debug(
                    f"[{self.strategy.id}] 가상추가매수 {pos.name} "
                    f"실질{net_gain_pct:+.1f}%(기준+{level_pct}%) "
                    f"→ {qty}주 @{cur_price:,} (수수료 {bc.commission:.0f}원)"
                )
                return {"ok": True, "level_pct": level_pct, "qty": qty,
                        "net_gain_pct": round(net_gain_pct, 2)}

        return {"ok": False, "reason": "추가매수 조건 미충족"}

    # ── 가상 매도 ─────────────────────────────────────────
    def virtual_sell(self, code: str, cur_price: float,
                     date_str: str, reason: str = "") -> dict:
        """
        가상 전량 매도.

        실질 손익 계산:
          cost_basis   = pos.avg_price × pos.qty  (수수료 포함 총 취득원가)
          net_proceeds = 매도금액 - 매도수수료 - 증권거래세
          net_profit   = net_proceeds - cost_basis
          net_profit_pct = net_profit / cost_basis × 100
        """
        if self.strategy.is_real_order:
            return {"ok": False, "reason": "실전전략"}

        pos = self.positions.pop(code, None)
        if pos is None:
            return {"ok": False, "reason": "포지션 없음"}

        sp         = calc_sell_proceeds(cur_price, pos.qty)
        cost_basis = pos.avg_price * pos.qty   # 수수료 포함 총 취득원가
        net_profit = sp.net_proceeds - cost_basis
        net_profit_pct = (net_profit / cost_basis * 100) if cost_basis > 0 else 0.0

        # 현금 복원: 순 매도 수령액만 (세금·수수료 차감 후)
        self.capital += sp.net_proceeds

        # 보유 기간
        try:
            d0 = date.fromisoformat(pos.entry_date)
            d1 = date.fromisoformat(date_str)
            hold_days = (d1 - d0).days
        except Exception:
            hold_days = 0

        trade = VirtualTrade(
            strategy_id=self.strategy.id,
            code=code, name=pos.name,
            action="SELL",
            price=cur_price, qty=pos.qty,
            date_str=date_str,
            profit=net_profit,             # 실질 순손익
            profit_pct=net_profit_pct,     # 실질 수익률
            hold_days=hold_days,
            reason=reason,
            sell_commission=sp.commission,
            transaction_tax=sp.transaction_tax,
            total_fee=sp.commission + sp.transaction_tax,
        )
        self.trades.append(trade)

        emoji = "💰" if net_profit >= 0 else "📉"
        logger.debug(
            f"[{self.strategy.id}] {emoji} 가상매도 {pos.name} "
            f"실질 {net_profit:+,.0f}원({net_profit_pct:+.2f}%) "
            f"(매도수수료 {sp.commission:.0f}원, 거래세 {sp.transaction_tax:.0f}원, "
            f"보유 {hold_days}일) → {reason}"
        )
        return {
            "ok":            True,
            "profit":        round(net_profit, 0),
            "profit_pct":    round(net_profit_pct, 2),
            "sell_commission": round(sp.commission, 0),
            "transaction_tax": round(sp.transaction_tax, 0),
            "total_fee":     round(sp.commission + sp.transaction_tax, 0),
            "hold_days":     hold_days,
        }

    # ── 청산 조건 확인 ───────────────────────────────────
    def check_exit(self, code: str, cur_price: float,
                   date_str: str) -> Optional[dict]:
        """
        손절·트레일링스탑 조건 확인.

        ★ 모든 % 판단은 실질수익률 기준 ★
          - 손절:          실질수익률 ≤ stop_loss_pct
          - 트레일링스탑:  실질수익률 기준 고점 대비 하락

        청산 필요 시 {reason, action, net_pct} 반환, 아니면 None.
        """
        pos = self.positions.get(code)
        if pos is None:
            return None

        pos.update_high(cur_price)

        # ★ 실질 수익률로 손절 판단 (수수료·세금 차감)
        net_pct = pos.unrealized_pct(cur_price)
        if net_pct <= self.strategy.stop_loss_pct:
            return {
                "action":  "STOP_LOSS",
                "reason":  f"손절(실질{net_pct:.2f}% ≤ {self.strategy.stop_loss_pct}%)",
                "net_pct": round(net_pct, 2),
            }

        # 트레일링스탑 활성화 조건: 고점의 실질 수익률이 activate_pct 이상
        # pos.avg_price 는 수수료 포함 취득원가 → price_for_net_pct_from_cost 사용
        activate_trigger_price = price_for_net_pct_from_cost(
            pos.avg_price, self.strategy.trailing_activate
        )
        if pos.highest_price >= activate_trigger_price:
            # 고점 대비 현재가 하락률 (주가 기준, 트레일링은 주가% 사용)
            trail_pct = (cur_price - pos.highest_price) / pos.highest_price * 100
            if trail_pct <= self.strategy.trailing_pct:
                return {
                    "action":  "TRAILING_STOP",
                    "reason":  (f"트레일링스탑(고점대비{trail_pct:.2f}% ≤ "
                                f"{self.strategy.trailing_pct}%, "
                                f"실질{net_pct:.2f}%)"),
                    "net_pct": round(net_pct, 2),
                }
        return None

    # ── 자산 스냅샷 ─────────────────────────────────────
    def snapshot_equity(self, price_map: dict[str, float]):
        """
        현재 가격 기준 총 자산 계산 후 equity_curve 에 추가.
        price_map: {code: cur_price}
        """
        total = self.capital
        for code, pos in self.positions.items():
            p = price_map.get(code, pos.avg_price)
            total += p * pos.qty
        self.equity_curve.append(total)
        return total

    # ── 성과 지표 ────────────────────────────────────────
    def get_metrics(self) -> dict:
        return MetricsCalculator.calc(
            trades        = [t.to_dict() for t in self.trades],
            equity_curve  = self.equity_curve,
            initial_capital = self.INITIAL_CAPITAL,
        )

    # ── 직렬화 ───────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "strategy_id":  self.strategy.id,
            "capital":      self.capital,
            "positions":    {k: v.to_dict() for k, v in self.positions.items()},
            "trades":       [t.to_dict() for t in self.trades[-200:]],  # 최근 200건
            "equity_curve": self.equity_curve[-500:],                   # 최근 500일
        }

    @classmethod
    def from_dict(cls, d: dict, strategy: StrategyConfig) -> "ShadowPortfolio":
        obj = cls(strategy)
        obj.capital       = d.get("capital", cls.INITIAL_CAPITAL)
        obj.equity_curve  = d.get("equity_curve", [cls.INITIAL_CAPITAL])
        obj.positions     = {
            k: VirtualPosition.from_dict(v)
            for k, v in d.get("positions", {}).items()
        }
        # trades 복원 (간략)
        obj.trades = []
        for td in d.get("trades", []):
            t = VirtualTrade(
                strategy_id = td["strategy_id"],
                code=td["code"], name=td["name"],
                action=td["action"],
                price=td["price"], qty=td["qty"],
                date_str=td["date"],
                profit=td.get("profit", 0),
                hold_days=td.get("hold_days", 0),
                reason=td.get("reason", ""),
            )
            t.ts = td.get("ts", "")
            obj.trades.append(t)
        return obj
