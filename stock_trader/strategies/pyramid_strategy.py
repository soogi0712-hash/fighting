"""
피라미딩(Pyramiding) + 복리 재투자 + 무차입 투자 전략
========================================================

★ 핵심 철학 ★
  1. 무차입(No Leverage) — 보유 현금 범위 내에서만 매수
  2. 피라미딩             — 실질 수익이 날 때마다 단계적으로 추가 매수
  3. 복리 재투자          — 실질 실현 수익을 원금에 합산해 다음 투자에 재사용

★ 피라미딩 규칙 ★
  - 최대 4단계 (1~4레벨)
  - 1단계: 첫 진입           → 가용자금의 25% 투입
  - 2단계: 실질 +2% 달성  → 가용자금의 20% 추가 (보조지표 1개 이상)
  - 3단계: 실질 +4% 달성  → 가용자금의 15% 추가 (보조지표 2개 이상)
  - 4단계: 실질 +6% 달성  → 가용자금의 10% 추가 (보조지표 3개 이상)
  ※ 단계 기준은 실질 수익률(수수료·세금 차감 후) 기준

★ 청산 규칙 ★
  - 전량 손절: 실질수익률 ≤ -3%  (단순 주가 -3% 아님)
  - 단계별 익절: 해당 단계 실질수익률 ≥ +5% 도달 시 보유 50% 매도
  - 전량 익절: 실질 +5% 이상 달성 후 고점 대비 -2% 트레일링 스탑
    (트레일링 활성화도 실질수익률 기준 역산)

★ 복리 재투자 ★
  - 매도 실질 순손익을 '복리풀(compounding_pool)'에 적립
  - 다음 피라미딩 1단계 진입 시 원금 + 복리풀 합산
  - 복리풀은 별도 파일로 영속 보관

★ 비용 원칙 ★
  - 단일 비용 원천: screener/transaction_cost.py
  - avg_price = 수수료 포함 주당 취득원가 (total_cost / qty)
  - 손절/익절/추가매수 판단 → 모두 실질수익률 기준
  - profit 필드 → 실질 순손익 (수수료·세금 차감 후)
"""

import os
import json
import math
from datetime import datetime
from utils.logger import get_logger
from screener.transaction_cost import (
    calc_buy_cost,
    calc_sell_proceeds,
    calc_buy_qty,
    net_profit_pct_from_cost,
    price_for_net_pct_from_cost,
    summarize_costs,
    BUY_COMMISSION_RATE, SELL_COMMISSION_RATE, TRANSACTION_TAX_RATE,
)

logger = get_logger("PyramidStrategy")

PYRAMID_FILE  = os.path.join(os.path.dirname(__file__), "..", "data", "pyramid_positions.json")
COMPOUND_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "compound_pool.json")

# ── 단계별 설정 ────────────────────────────────────────────
# add_pct: 실질수익률 기준 추가매수 트리거 (수수료·세금 차감 후 %)
PYRAMID_LEVELS = {
    1: {"add_pct": 0.00, "invest_ratio": 0.25, "indicator_min": 0,
        "label": "1단계 첫진입"},
    2: {"add_pct": 2.00, "invest_ratio": 0.20, "indicator_min": 1,
        "label": "2단계 실질+2% 추가"},
    3: {"add_pct": 4.00, "invest_ratio": 0.15, "indicator_min": 2,
        "label": "3단계 실질+4% 추가"},
    4: {"add_pct": 6.00, "invest_ratio": 0.10, "indicator_min": 3,
        "label": "4단계 실질+6% 추가"},
}

# ★ 모든 % 기준은 실질수익률 기준 (수수료·세금 차감 후)
STOP_LOSS_PCT            = -3.0   # 전량 손절 기준 (실질수익률 %)
TRAILING_STOP_PCT        = -2.0   # 트레일링 스탑 (고점 대비 주가 %)
TRAILING_ACTIVATE_PCT    =  5.0   # 트레일링 활성화 기준 (실질수익률 %)
PARTIAL_PROFIT_PCT       =  5.0   # 단계별 부분 익절 기준 (실질수익률 %)
PARTIAL_SELL_RATIO       =  0.5   # 부분 익절 시 해당 단계 보유 비율


class PyramidStrategyManager:
    """
    피라미딩 포지션 완전 관리자
    - 종목별로 피라미딩 레벨을 추적
    - 무차입 원칙 준수 (현금 초과 매수 금지)
    - 복리풀 관리
    - ★ 모든 수익률·손절·익절 판단은 실질수익률 기준
    """

    def __init__(self, kis_api, max_per_stock: float, max_total: float):
        self.api           = kis_api
        self.max_per_stock = max_per_stock   # 종목당 최대 투자금
        self.max_total     = max_total       # 전체 최대 투자금
        self.positions     = {}              # {code: PyramidPosition}
        self.compound_pool = 0.0             # 복리 적립금

        os.makedirs(os.path.dirname(PYRAMID_FILE), exist_ok=True)
        self._load()

    # ── 저장/로드 ──────────────────────────────────────────
    def _load(self):
        if os.path.exists(PYRAMID_FILE):
            try:
                raw = json.load(open(PYRAMID_FILE))
                self.positions = {k: PyramidPosition.from_dict(v)
                                  for k, v in raw.items()}
            except Exception:
                self.positions = {}
        if os.path.exists(COMPOUND_FILE):
            try:
                self.compound_pool = json.load(open(COMPOUND_FILE)).get("pool", 0.0)
            except Exception:
                self.compound_pool = 0.0
        logger.info(f"💰 복리풀 잔액: {self.compound_pool:,.0f}원")

    def _save(self):
        json.dump({k: v.to_dict() for k, v in self.positions.items()},
                  open(PYRAMID_FILE, "w"), ensure_ascii=False, indent=2)
        json.dump({"pool": self.compound_pool,
                   "updated": datetime.now().isoformat()},
                  open(COMPOUND_FILE, "w"), ensure_ascii=False, indent=2)

    # ── 핵심: 매매 결정 ───────────────────────────────────
    def evaluate(self, code: str, name: str,
                 cur_price: float, indicator_score: int,
                 available_cash: float) -> dict:
        """
        indicator_score: 현재 매수 신호를 보내는 보조지표 수 (0~6)
        returns: {action, level, qty, price, reason, net_pct, profit, ...}

        ★ 모든 % 판단은 실질수익률 기준 ★
        """
        pos = self.positions.get(code)

        # ── 포지션 없음 → 1단계 진입 검토 ─────────────────
        if pos is None:
            return self._try_entry(code, name, cur_price,
                                   indicator_score, available_cash)

        # ── 포지션 있음 ─────────────────────────────────────
        pos.update_high(cur_price)

        # ★ 실질 수익률 계산 (수수료·세금 차감)
        # pos.avg_price = 수수료 포함 주당 취득원가
        net_pct = net_profit_pct_from_cost(pos.avg_price, cur_price)

        # ─ 1) 전량 손절: 실질수익률 ≤ STOP_LOSS_PCT ────────
        if net_pct <= STOP_LOSS_PCT:
            sp = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            return {
                "action":   "SELL_ALL",
                "reason":   f"손절(실질{net_pct:.2f}% ≤ {STOP_LOSS_PCT}%)",
                "qty":      pos.total_qty,
                "price":    cur_price,
                "code":     code,
                "name":     name,
                "level":    pos.current_level,
                "net_pct":  round(net_pct, 2),
                # ★ 실질 순손익 (수수료·세금 차감)
                "profit":   round(net_profit, 0),
                "sell_commission":  round(sp.commission, 0),
                "transaction_tax":  round(sp.transaction_tax, 0),
                "total_fee":        round(sp.commission + sp.transaction_tax, 0),
            }

        # ─ 2) 트레일링 스탑 ──────────────────────────────────
        # 활성화 조건: 실질수익률 ≥ TRAILING_ACTIVATE_PCT
        activate_trigger = price_for_net_pct_from_cost(
            pos.avg_price, TRAILING_ACTIVATE_PCT
        )
        trail_pct = (cur_price - pos.highest_price) / pos.highest_price * 100
        if (pos.highest_price >= activate_trigger and
                trail_pct <= TRAILING_STOP_PCT):
            sp = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            return {
                "action":  "SELL_ALL",
                "reason":  (f"트레일링스탑(고점대비{trail_pct:.2f}% ≤ "
                            f"{TRAILING_STOP_PCT}%, 실질{net_pct:.2f}%)"),
                "qty":     pos.total_qty,
                "price":   cur_price,
                "code":    code,
                "name":    name,
                "level":   pos.current_level,
                "net_pct": round(net_pct, 2),
                # ★ 실질 순손익
                "profit":  round(net_profit, 0),
                "sell_commission":  round(sp.commission, 0),
                "transaction_tax":  round(sp.transaction_tax, 0),
                "total_fee":        round(sp.commission + sp.transaction_tax, 0),
            }

        # ─ 3) 단계별 부분 익절: 실질수익률 기준 ─────────────
        for lvl, entry in pos.level_entries.items():
            entry_avg_price = entry.get("avg_price", entry["price"])  # 수수료 포함
            remaining       = entry["remaining"]
            if remaining <= 0:
                continue
            # 해당 레벨의 실질 수익률 (레벨 avg_price 기준)
            level_net_pct = net_profit_pct_from_cost(entry_avg_price, cur_price)
            if level_net_pct >= PARTIAL_PROFIT_PCT:
                sell_qty = max(1, int(remaining * PARTIAL_SELL_RATIO))
                sp       = calc_sell_proceeds(cur_price, sell_qty)
                cost_b   = entry_avg_price * sell_qty
                net_p    = sp.net_proceeds - cost_b
                return {
                    "action":        "SELL_PARTIAL",
                    "reason":        (f"{lvl}단계 부분익절"
                                      f"(실질{level_net_pct:.2f}% ≥ {PARTIAL_PROFIT_PCT}%)"),
                    "qty":           sell_qty,
                    "price":         cur_price,
                    "code":          code,
                    "name":          name,
                    "level":         lvl,
                    "net_pct":       round(level_net_pct, 2),
                    # ★ 실질 순손익
                    "profit":        round(net_p, 0),
                    "sell_commission":  round(sp.commission, 0),
                    "transaction_tax":  round(sp.transaction_tax, 0),
                    "total_fee":        round(sp.commission + sp.transaction_tax, 0),
                }

        # ─ 4) 다음 피라미딩 단계 진입 검토: 실질수익률 기준 ─
        next_level = pos.current_level + 1
        if next_level <= 4:
            lvl_cfg       = PYRAMID_LEVELS[next_level]
            required_gain = lvl_cfg["add_pct"]   # 실질수익률 기준
            # pos.avg_price(수수료포함) 기준 실질수익률
            gain_from_entry = net_pct  # 이미 계산된 전체 포지션 실질수익률

            if (gain_from_entry >= required_gain and
                    indicator_score >= lvl_cfg["indicator_min"]):
                return self._try_add(code, name, cur_price, next_level,
                                     indicator_score, available_cash, pos)

        return {
            "action":        "HOLD",
            "code":          code,
            "name":          name,
            "level":         pos.current_level,
            "price":         cur_price,
            "avg_price":     round(pos.avg_price, 4),
            "total_qty":     pos.total_qty,
            "net_pct":       round(net_pct, 2),           # ★ 실질수익률
            "highest_price": pos.highest_price,
            "reason": (f"피라미딩{pos.current_level}단계 유지 "
                       f"(실질{net_pct:+.2f}%, "
                       f"고점대비{trail_pct:+.1f}%)"),
        }

    # ── 1단계 첫 진입 ────────────────────────────────────
    def _try_entry(self, code, name, price, indicator_score, cash) -> dict:
        cfg = PYRAMID_LEVELS[1]

        # 무차입: 현금 이내 + 복리풀 포함
        investable = min(
            self.max_per_stock,
            self.max_total,
            cash + self.compound_pool,
        )
        invest_amt = investable * cfg["invest_ratio"]   # 25%

        # ★ 수수료 포함 매수 가능 수량
        qty = calc_buy_qty(invest_amt, price, invest_ratio=1.0)

        if qty < 1:
            return {"action": "SKIP", "reason": "투자가능금액 부족",
                    "code": code, "name": name}

        bc = calc_buy_cost(price, qty)

        # 총 투자금 한도 체크 (취득원가 기준)
        total_invested = sum(
            p.avg_price * p.total_qty for p in self.positions.values()
        )
        if total_invested + bc.total_cost > self.max_total:
            return {"action": "SKIP", "reason": "전체 투자 한도 초과",
                    "code": code, "name": name}

        return {
            "action":          "BUY_LEVEL1",
            "level":           1,
            "qty":             qty,
            "price":           price,
            "amount":          bc.buy_amount,       # 순 매수금액
            "total_cost":      bc.total_cost,        # 수수료 포함 총매수금액
            "buy_commission":  round(bc.commission, 0),
            "code":            code,
            "name":            name,
            "reason":          (f"피라미딩 1단계 진입 "
                                f"(투자금={bc.total_cost:,.0f}원, 복리풀 포함)"),
            "using_compound":  min(self.compound_pool, bc.total_cost),
        }

    # ── 추가 매수 ────────────────────────────────────────
    def _try_add(self, code, name, price, level, indicator_score, cash, pos) -> dict:
        cfg = PYRAMID_LEVELS[level]

        # 이미 해당 레벨 진입했으면 스킵
        if level in pos.level_entries:
            return {"action": "HOLD", "code": code, "name": name,
                    "level": pos.current_level, "reason": f"{level}단계 이미 진입"}

        # 남은 투자 여력 (취득원가 기준)
        investable = min(
            self.max_per_stock - pos.avg_price * pos.total_qty,
            cash + self.compound_pool
        )
        invest_limit = min(self.max_per_stock, self.max_total) * cfg["invest_ratio"]
        invest_amt   = min(invest_limit, investable)

        # ★ 수수료 포함 매수 가능 수량
        qty = calc_buy_qty(invest_amt, price, invest_ratio=1.0)

        if qty < 1:
            return {"action": "HOLD", "code": code, "name": name,
                    "level": pos.current_level, "reason": "추가 투자금 부족"}

        bc = calc_buy_cost(price, qty)

        # 실질 수익률 (현재 avg_price 기준)
        net_pct = net_profit_pct_from_cost(pos.avg_price, price)

        return {
            "action":         f"BUY_LEVEL{level}",
            "level":          level,
            "qty":            qty,
            "price":          price,
            "amount":         bc.buy_amount,
            "total_cost":     bc.total_cost,
            "buy_commission": round(bc.commission, 0),
            "code":           code,
            "name":           name,
            "net_pct":        round(net_pct, 2),
            "reason":         (f"피라미딩 {level}단계 추가 "
                               f"(실질{net_pct:+.2f}%, 지표{indicator_score}개)"),
            "using_compound": min(self.compound_pool, bc.total_cost),
        }

    # ── 포지션 반영 ───────────────────────────────────────
    def apply_buy(self, code: str, name: str,
                  level: int, qty: int, price: float,
                  using_compound: float = 0):
        """
        매수 체결 후 포지션 반영.
        avg_price = 수수료 포함 주당 취득원가 (total_cost / qty).
        """
        bc = calc_buy_cost(price, qty)

        # 복리풀 차감
        used_compound = min(self.compound_pool, using_compound)
        self.compound_pool = max(0, self.compound_pool - used_compound)

        if code not in self.positions:
            pos = PyramidPosition(code, name, price)
            # 취득원가 = 수수료 포함
            pos.avg_price = bc.total_cost / qty
            self.positions[code] = pos
        self.positions[code].add_level(level, qty, price, bc.total_cost)
        self._save()
        logger.info(
            f"📥 피라미딩 {level}단계 {name} {qty}주 @{price:,}원 "
            f"(수수료 {bc.commission:.0f}원, 총원가 {bc.total_cost:,.0f}원, "
            f"복리풀사용={used_compound:,.0f}원)"
        )

    def apply_sell(self, code: str, qty: int, price: float,
                   level: int = None, is_full: bool = False) -> dict:
        """
        매도 체결 후 복리풀 적립.
        ★ 실질 순손익(수수료·세금 차감 후)을 복리풀에 적립.
        """
        pos = self.positions.get(code)
        if pos is None:
            return {"net_profit": 0.0, "net_profit_pct": 0.0}

        sp         = calc_sell_proceeds(price, qty)
        cost_basis = pos.avg_price * qty    # 수수료 포함 취득원가
        net_profit = sp.net_proceeds - cost_basis
        net_pct    = (net_profit / cost_basis * 100) if cost_basis > 0 else 0.0

        if is_full or pos.total_qty <= qty:
            del self.positions[code]
        else:
            pos.remove_qty(qty, level)

        # ★ 실질 순손익(양수만) 복리 적립
        if net_profit > 0:
            self.compound_pool += net_profit
            logger.info(
                f"💰 복리풀 적립 +{net_profit:,.0f}원 → 누적={self.compound_pool:,.0f}원"
            )
        elif net_profit < 0:
            logger.info(
                f"📉 실질손실 {net_profit:,.0f}원 (수수료·세금 {sp.total_cost:.0f}원 포함)"
            )

        self._save()
        return {
            "net_profit":      round(net_profit, 0),
            "net_profit_pct":  round(net_pct, 2),
            "sell_commission": round(sp.commission, 0),
            "transaction_tax": round(sp.transaction_tax, 0),
            "total_fee":       round(sp.commission + sp.transaction_tax, 0),
            "net_proceeds":    round(sp.net_proceeds, 0),
        }

    def get_position(self, code: str):
        return self.positions.get(code)

    def get_compound_pool(self) -> float:
        return self.compound_pool

    def get_all_status(self) -> list:
        return [p.summary() for p in self.positions.values()]


# ── 피라미딩 포지션 데이터 클래스 ─────────────────────────
class PyramidPosition:
    def __init__(self, code, name, entry_price):
        self.code          = code
        self.name          = name
        self.entry_price   = entry_price   # 최초 진입가 (순수 주가)
        self.highest_price = entry_price   # 고가 (트레일링 스탑용, 주가 기준)
        self.current_level = 0
        self.level_entries = {}            # {level: {price, avg_price, qty, remaining}}
        self.total_qty     = 0
        # ★ avg_price = 수수료 포함 주당 취득원가 (total_cost / total_qty)
        self.avg_price     = entry_price
        self.created_at    = datetime.now().isoformat()

    def add_level(self, level: int, qty: int, price: float,
                  total_cost: float = None):
        """
        레벨 추가. total_cost 가 제공되면 수수료 포함 취득원가로 avg_price 갱신.
        """
        if total_cost is None:
            bc         = calc_buy_cost(price, qty)
            total_cost = bc.total_cost

        avg_price_this_level = total_cost / qty  # 수수료 포함 주당 원가

        self.level_entries[level] = {
            "price":     price,               # 체결 주가
            "avg_price": avg_price_this_level,  # ★ 수수료 포함 주당 원가
            "qty":       qty,
            "remaining": qty,
            "total_cost": round(total_cost, 2),
            "added_at":  datetime.now().isoformat(),
        }
        self.current_level = max(self.current_level, level)
        self.total_qty    += qty

        # ★ 전체 avg_price 갱신 (모든 레벨 총취득원가 / 총수량)
        total_all_cost = sum(
            e.get("total_cost", e["price"] * e["qty"])
            for e in self.level_entries.values()
        )
        total_all_qty = sum(e["qty"] for e in self.level_entries.values())
        self.avg_price = total_all_cost / total_all_qty if total_all_qty else price

    def remove_qty(self, qty: int, level: int = None):
        if level and level in self.level_entries:
            self.level_entries[level]["remaining"] = max(
                0, self.level_entries[level]["remaining"] - qty
            )
        self.total_qty = max(0, self.total_qty - qty)

    def update_high(self, price: float):
        if price > self.highest_price:
            self.highest_price = price

    def unrealized_pct(self, cur_price: float) -> float:
        """실질 미실현 수익률 (수수료·세금 차감)"""
        return net_profit_pct_from_cost(self.avg_price, cur_price)

    def summary(self) -> dict:
        return {
            "code":          self.code,
            "name":          self.name,
            "entry_price":   self.entry_price,
            "avg_price":     round(self.avg_price, 4),   # 수수료 포함 주당 취득원가
            "highest_price": self.highest_price,
            "current_level": self.current_level,
            "total_qty":     self.total_qty,
            "levels":        self.level_entries,
        }

    def to_dict(self) -> dict:
        return {
            "code":          self.code,
            "name":          self.name,
            "entry_price":   self.entry_price,
            "highest_price": self.highest_price,
            "current_level": self.current_level,
            "avg_price":     self.avg_price,
            "total_qty":     self.total_qty,
            "level_entries": self.level_entries,
            "created_at":    self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict):
        obj = cls(d["code"], d["name"], d["entry_price"])
        obj.highest_price = d.get("highest_price", d["entry_price"])
        obj.current_level = d.get("current_level", 0)
        obj.avg_price     = d.get("avg_price", d["entry_price"])
        obj.total_qty     = d.get("total_qty", 0)
        obj.level_entries = d.get("level_entries", {})
        obj.created_at    = d.get("created_at", "")
        return obj
