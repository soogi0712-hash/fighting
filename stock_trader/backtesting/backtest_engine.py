"""
백테스팅 엔진
=============

★ 실질 수익률 원칙 ★
  - 모든 매수/매도에 수수료·세금 반영
  - 손절/익절 조건: 실질수익률 기준 (단순 주가 % 아님)
  - 성과 지표 전부 비용 차감 후 net profit 기준
  - 단일 비용 원천: screener/transaction_cost.py

비용 구조:
  매수: 수수료 0.015%
  매도: 수수료 0.015% + 증권거래세 0.18%
  왕복: ~0.345%
"""
import math
import pandas as pd
import numpy as np
from datetime import datetime
from utils.logger import get_logger
from strategies.ma_strategy       import MAStrategy
from strategies.rsi_macd_strategy import RSIMACDStrategy
from screener.transaction_cost import (
    calc_buy_cost,
    calc_sell_proceeds,
    calc_buy_qty,
    net_profit_pct_from_cost,
    price_for_net_pct_from_cost,
    summarize_costs,
    BUY_COMMISSION_RATE, SELL_COMMISSION_RATE, TRANSACTION_TAX_RATE,
)

logger = get_logger("Backtest")


class BacktestEngine:
    def __init__(self, initial_capital: float = 10_000_000):
        self.initial_capital = initial_capital
        self.ma_strat  = MAStrategy()
        self.rsi_strat = RSIMACDStrategy()

    def run(self, candles: list[dict], strategy: str = "combined",
            stop_loss: float = 3.0, take_profit: float = 5.0) -> dict:
        """
        과거 데이터로 전략 백테스트.

        Args:
            candles:     [{date, close, ...}, ...]  최소 70봉 이상
            strategy:    "ma" | "rsi_macd" | "combined"
            stop_loss:   손절 기준 실질수익률 % (양수 입력, 내부에서 음수 처리)
            take_profit: 익절 기준 실질수익률 % (양수 입력)

        Returns:
            성과 지표 dict (비용 차감 후)
        """
        if len(candles) < 70:
            return {"error": "데이터 부족"}

        df = pd.DataFrame(candles)
        df["close"] = df["close"].astype(float)

        capital      = self.initial_capital
        position     = 0        # 보유 수량
        # ★ avg_price = 수수료 포함 주당 취득원가 (total_cost / qty)
        avg_price    = 0.0
        trades: list[dict] = []
        equity_curve = [capital]

        for i in range(65, len(df)):
            window   = df.iloc[:i + 1].to_dict("records")
            price    = float(df.iloc[i]["close"])
            date_str = df.iloc[i]["date"]

            # ── 전략 신호 ────────────────────────────────
            if strategy == "ma":
                sig = self.ma_strat.analyze(window)["signal"]
            elif strategy == "rsi_macd":
                sig = self.rsi_strat.analyze(window)["signal"]
            else:   # combined
                ma_sig  = self.ma_strat.analyze(window)["signal"]
                rsi_sig = self.rsi_strat.analyze(window)["signal"]
                if ma_sig == rsi_sig:
                    sig = ma_sig
                elif ma_sig == "BUY" or rsi_sig == "BUY":
                    sig = "BUY"
                elif ma_sig == "SELL" or rsi_sig == "SELL":
                    sig = "SELL"
                else:
                    sig = "HOLD"

            # ── 손절/익절: 실질수익률 기준 ───────────────
            if position > 0 and avg_price > 0:
                # ★ 실질수익률 (수수료·세금 차감 후)
                net_pct = net_profit_pct_from_cost(avg_price, price)
                if net_pct <= -abs(stop_loss):
                    sig = "SELL"
                elif net_pct >= abs(take_profit):
                    sig = "SELL"

            # ── 매수: 수수료 포함 처리 ───────────────────
            if sig == "BUY" and position == 0:
                # ★ 수수료 포함 매수 가능 수량
                qty = calc_buy_qty(capital, price, invest_ratio=0.95)
                if qty < 1:
                    equity_curve.append(capital + position * price)
                    continue

                bc = calc_buy_cost(price, qty)
                if bc.total_cost > capital:
                    equity_curve.append(capital + position * price)
                    continue

                # ★ 총매수금액(수수료 포함) 차감
                capital  -= bc.total_cost
                position  = qty
                # ★ avg_price = 수수료 포함 주당 취득원가
                avg_price = bc.total_cost / qty

                trades.append({
                    "date":           date_str,
                    "action":         "BUY",
                    "price":          price,
                    "qty":            qty,
                    "avg_price":      round(avg_price, 4),  # 수수료 포함
                    "buy_commission": round(bc.commission, 0),
                    "total_cost":     round(bc.total_cost, 0),
                    "capital":        round(capital, 0),
                    "profit":         0.0,
                    "profit_pct":     0.0,
                })

            # ── 매도: 실질 순손익 계산 ───────────────────
            elif sig == "SELL" and position > 0:
                sp         = calc_sell_proceeds(price, position)
                cost_basis = avg_price * position      # 수수료 포함 총 취득원가
                net_profit = sp.net_proceeds - cost_basis
                net_pct    = (net_profit / cost_basis * 100) if cost_basis > 0 else 0.0

                # ★ 순매도수령액만 자본에 복원
                capital   += sp.net_proceeds

                trades.append({
                    "date":            date_str,
                    "action":          "SELL",
                    "price":           price,
                    "qty":             position,
                    "avg_price":       round(avg_price, 4),
                    "sell_commission": round(sp.commission, 0),
                    "transaction_tax": round(sp.transaction_tax, 0),
                    "total_fee":       round(sp.commission + sp.transaction_tax, 0),
                    "net_proceeds":    round(sp.net_proceeds, 0),
                    "profit":          round(net_profit, 0),     # ★ 실질 순손익
                    "profit_pct":      round(net_pct, 4),        # ★ 실질 수익률
                    "capital":         round(capital, 0),
                })
                position  = 0
                avg_price = 0.0

            total_equity = capital + position * price
            equity_curve.append(total_equity)

        # ── 잔여 포지션 청산 (수수료·세금 포함) ──────────
        if position > 0:
            final_price = float(df.iloc[-1]["close"])
            sp_final    = calc_sell_proceeds(final_price, position)
            cost_basis  = avg_price * position
            net_profit  = sp_final.net_proceeds - cost_basis
            net_pct     = (net_profit / cost_basis * 100) if cost_basis > 0 else 0.0

            capital += sp_final.net_proceeds

            trades.append({
                "date":            df.iloc[-1]["date"],
                "action":          "CLOSE",
                "price":           final_price,
                "qty":             position,
                "avg_price":       round(avg_price, 4),
                "sell_commission": round(sp_final.commission, 0),
                "transaction_tax": round(sp_final.transaction_tax, 0),
                "total_fee":       round(sp_final.commission + sp_final.transaction_tax, 0),
                "net_proceeds":    round(sp_final.net_proceeds, 0),
                "profit":          round(net_profit, 0),   # ★ 실질 순손익
                "profit_pct":      round(net_pct, 4),
                "capital":         round(capital, 0),
            })
            equity_curve[-1] = capital

        # ── 성과 지표 계산 (net profit 기반) ─────────────
        eq = np.array(equity_curve, dtype=float)

        # 총수익률 (비용 차감 후)
        total_return = (capital - self.initial_capital) / self.initial_capital * 100

        # MDD (equity curve 기반)
        peak  = np.maximum.accumulate(eq)
        dd    = (eq - peak) / peak * 100
        mdd   = float(dd.min())

        # 샤프비율 (net return 기반)
        daily_rets = np.diff(eq) / eq[:-1]
        sharpe = (
            float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(252))
            if np.std(daily_rets) > 0 else 0.0
        )

        # 승률·손익비 (실질 순손익 기준)
        sell_trades  = [t for t in trades if t["action"] in ("SELL", "CLOSE")]
        profits_list = [t.get("profit", 0) for t in sell_trades if t.get("profit", 0) > 0]
        losses_list  = [t.get("profit", 0) for t in sell_trades if t.get("profit", 0) < 0]

        win_rate = (
            len(profits_list) / len(sell_trades) * 100
            if sell_trades else 0.0
        )
        avg_win  = sum(profits_list) / len(profits_list) if profits_list else 0.0
        avg_loss = sum(losses_list)  / len(losses_list)  if losses_list  else 0.0
        profit_factor = (
            abs(sum(profits_list) / sum(losses_list))
            if losses_list and sum(losses_list) != 0 else
            (99.9 if profits_list else 0.0)
        )

        # 총 비용 집계 (summarize_costs 활용)
        cost_summary = summarize_costs(trades)

        # CAGR
        n_days = len(equity_curve)
        years  = max(n_days / 252, 1 / 252)
        cagr = ((capital / self.initial_capital) ** (1 / years) - 1) * 100 if self.initial_capital > 0 else 0.0

        return {
            "strategy":          strategy,
            "initial_capital":   self.initial_capital,
            "final_capital":     round(capital, 0),
            "total_return":      round(total_return, 2),    # ★ 비용 차감 후
            "cagr":              round(cagr, 2),
            "mdd":               round(mdd, 2),
            "sharpe":            round(sharpe, 2),
            "win_rate":          round(win_rate, 1),
            "avg_win":           round(avg_win, 0),
            "avg_loss":          round(avg_loss, 0),
            "profit_factor":     round(min(profit_factor, 99.9), 2),
            "total_trades":      len(trades),
            # ★ 비용 상세 (수수료·세금 분리)
            "total_fee":             cost_summary["total_fee"],
            "total_buy_commission":  cost_summary["total_buy_commission"],
            "total_sell_commission": cost_summary["total_sell_commission"],
            "total_transaction_tax": cost_summary["total_transaction_tax"],
            "buy_count":             cost_summary["buy_count"],
            "sell_count":            cost_summary["sell_count"],
            "trades":            trades[-30:],
            "equity_curve":      equity_curve[::5],
        }
