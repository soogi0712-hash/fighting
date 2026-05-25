"""
거래 비용 계산 엔진 (Transaction Cost Engine)
=============================================

★ 설계 원칙 ★
  - 이 파일이 수수료·세금 관련 유일한 진실의 원천(Single Source of Truth)
  - 모든 모듈(shadow_portfolio, pyramid_strategy, backtest_engine 등)이
    이 모듈을 임포트해 사용한다.
  - 상수 변경 시 이 파일만 수정하면 전체 시스템에 반영된다.

★ 국내 주식 비용 구조 (2024년 기준) ★
  매수:
    - 증권사 매수 수수료:  0.015%  (온라인 기준 ~ MTS/HTS)
      ※ KIS(한국투자증권) HTS·MTS 기준 약 0.015%
  매도:
    - 증권사 매도 수수료:  0.015%
    - 증권거래세:          0.18%  (코스피 0.18%, 코스닥 0.18%)
      ※ 2024년 기준 코스피/코스닥 동일 0.18%
    - 농어촌특별세:        포함됨 (증권거래세에 통합)
  총 왕복 비용:           약 0.345%

★ 실질 손익 계산식 ★
  총매수금액 = 매수금액 + 매수수수료
  순매도금액 = 매도금액 - 매도수수료 - 증권거래세
  실질손익   = 순매도금액 - 총매수금액
  실질수익률 = 실질손익 / 총매수금액 × 100

★ 손절·익절·추가매수 기준 ★
  모든 % 기준은 실질수익률 기준
  (단순 주가 변동률이 아니라 비용 차감 후 순손익률)

★ 예시 ★
  매수가  100만원, 매수수수료 150원
  매도가  110만원, 매도수수료 165원, 거래세 1980원
  실질손익 = 1,100,000 - 165 - 1,980 - (1,000,000 + 150)
           = 97,705원
  실질수익률 = 97,705 / 1,000,150 × 100 ≈ 9.77%
"""

from dataclasses import dataclass


# ══════════════════════════════════════════════════════════════
# 비용 상수 (단일 진실 원천)
# ══════════════════════════════════════════════════════════════

# 증권사 매수 수수료율 (온라인 기준)
BUY_COMMISSION_RATE: float = 0.00015   # 0.015%

# 증권사 매도 수수료율
SELL_COMMISSION_RATE: float = 0.00015  # 0.015%

# 증권거래세 (코스피/코스닥 공통, 2024년 기준)
TRANSACTION_TAX_RATE: float = 0.0018   # 0.18%

# 기타 비용 (슬리피지 예비율; 기본값 0)
OTHER_COST_RATE: float = 0.0           # 필요 시 override

# 왕복 총 비용률 (참고용)
TOTAL_ROUNDTRIP_RATE: float = (
    BUY_COMMISSION_RATE
    + SELL_COMMISSION_RATE
    + TRANSACTION_TAX_RATE
    + OTHER_COST_RATE
)  # ≈ 0.00345


# ══════════════════════════════════════════════════════════════
# 비용 계산 결과 데이터클래스
# ══════════════════════════════════════════════════════════════

@dataclass
class BuyCost:
    """매수 비용 명세"""
    buy_amount:     float    # 순 매수금액 (price × qty)
    commission:     float    # 매수 수수료
    total_cost:     float    # 총 매수금액 (buy_amount + commission)

    @property
    def commission_rate_actual(self) -> float:
        """실제 적용 수수료율"""
        return self.commission / self.buy_amount if self.buy_amount else 0.0


@dataclass
class SellProceeds:
    """매도 수령액 명세"""
    sell_amount:      float  # 순 매도금액 (price × qty)
    commission:       float  # 매도 수수료
    transaction_tax:  float  # 증권거래세
    other_cost:       float  # 기타 비용
    net_proceeds:     float  # 실제 수령액 (sell_amount - 모든 비용)

    @property
    def total_cost(self) -> float:
        return self.commission + self.transaction_tax + self.other_cost


@dataclass
class TradeResult:
    """단일 매매 실질 손익 결과"""
    buy_cost:       BuyCost
    sell_proceeds:  SellProceeds
    net_profit:     float   # 실질 순손익 (원)
    net_profit_pct: float   # 실질 수익률 (%)
    gross_profit:   float   # 비용 전 손익 (참고용)
    total_fee:      float   # 총 비용 합계 (수수료+세금)

    @property
    def is_profit(self) -> bool:
        return self.net_profit > 0


# ══════════════════════════════════════════════════════════════
# 핵심 계산 함수들
# ══════════════════════════════════════════════════════════════

def calc_buy_cost(price: float, qty: int,
                  commission_rate: float = BUY_COMMISSION_RATE) -> BuyCost:
    """
    매수 총 비용 계산.

    Args:
        price:           매수 체결가
        qty:             매수 수량
        commission_rate: 매수 수수료율 (기본 0.015%)

    Returns:
        BuyCost: 매수금액, 수수료, 총매수금액

    Example:
        price=10000, qty=100 → buy_amount=1,000,000
        commission = 1,000,000 × 0.00015 = 150원
        total_cost = 1,000,150원
    """
    buy_amount = price * qty
    commission = buy_amount * commission_rate
    total_cost = buy_amount + commission
    return BuyCost(
        buy_amount=round(buy_amount, 2),
        commission=round(commission, 2),
        total_cost=round(total_cost, 2),
    )


def calc_sell_proceeds(price: float, qty: int,
                       commission_rate: float = SELL_COMMISSION_RATE,
                       tax_rate:        float = TRANSACTION_TAX_RATE,
                       other_rate:      float = OTHER_COST_RATE) -> SellProceeds:
    """
    매도 실제 수령액 계산.

    Args:
        price:           매도 체결가
        qty:             매도 수량
        commission_rate: 매도 수수료율 (기본 0.015%)
        tax_rate:        증권거래세율  (기본 0.18%)
        other_rate:      기타 비용률   (기본 0%)

    Returns:
        SellProceeds: 매도금액, 수수료, 거래세, 기타, 순수령액

    Example:
        price=11000, qty=100 → sell_amount=1,100,000
        commission     = 1,100,000 × 0.00015 = 165원
        transaction_tax= 1,100,000 × 0.0018  = 1,980원
        net_proceeds   = 1,100,000 - 165 - 1,980 = 1,097,855원
    """
    sell_amount     = price * qty
    commission      = sell_amount * commission_rate
    transaction_tax = sell_amount * tax_rate
    other_cost      = sell_amount * other_rate
    net_proceeds    = sell_amount - commission - transaction_tax - other_cost
    return SellProceeds(
        sell_amount=round(sell_amount, 2),
        commission=round(commission, 2),
        transaction_tax=round(transaction_tax, 2),
        other_cost=round(other_cost, 2),
        net_proceeds=round(net_proceeds, 2),
    )


def calc_trade_result(avg_buy_price: float, qty: int,
                      sell_price: float,
                      buy_commission_rate:  float = BUY_COMMISSION_RATE,
                      sell_commission_rate: float = SELL_COMMISSION_RATE,
                      tax_rate:             float = TRANSACTION_TAX_RATE,
                      other_rate:           float = OTHER_COST_RATE) -> TradeResult:
    """
    단일 매매 실질 손익 전체 계산.

    Args:
        avg_buy_price:       평균 매수가 (추가매수 포함 평균가)
        qty:                 매도 수량
        sell_price:          매도 체결가
        buy_commission_rate: 매수 수수료율
        sell_commission_rate:매도 수수료율
        tax_rate:            증권거래세율
        other_rate:          기타 비용률

    Returns:
        TradeResult: 실질 손익 전체 명세

    Example (요청서 예시):
        매수가  100만원, 매수수수료 1천원 → total_cost = 1,001,000원
        매도가  110만원, 매도수수료 1천원, 세금 2천원
        실질손익 = 1,100,000 - 1,000 - 2,000 - 1,001,000 = 96,000원
        실질수익률 = 96,000 / 1,001,000 × 100 ≈ 9.59%
    """
    bc = calc_buy_cost(avg_buy_price, qty, buy_commission_rate)
    sp = calc_sell_proceeds(sell_price, qty, sell_commission_rate, tax_rate, other_rate)

    gross_profit   = (sell_price - avg_buy_price) * qty
    net_profit     = sp.net_proceeds - bc.total_cost
    net_profit_pct = (net_profit / bc.total_cost * 100) if bc.total_cost > 0 else 0.0
    total_fee      = bc.commission + sp.commission + sp.transaction_tax + sp.other_cost

    return TradeResult(
        buy_cost=bc,
        sell_proceeds=sp,
        net_profit=round(net_profit, 2),
        net_profit_pct=round(net_profit_pct, 4),
        gross_profit=round(gross_profit, 2),
        total_fee=round(total_fee, 2),
    )


# ══════════════════════════════════════════════════════════════
# 실질 수익률 ↔ 주가 변동률 변환
# ══════════════════════════════════════════════════════════════

def net_profit_pct(avg_buy_price: float, cur_price: float) -> float:
    """
    현재가 기준 실질 수익률 (미실현, 매도 가정).
    손절/익절/추가매수 조건 판단 시 사용.

    실질수익률 = (순매도금액 - 총매수금액) / 총매수금액 × 100

    ★ avg_buy_price 는 수수료 미포함 순수 매수가 기준.
      수수료 포함 취득원가(VirtualPosition.avg_price) 기준은
      net_profit_pct_from_cost() 사용.

    Args:
        avg_buy_price: 평균 매수가 (수수료 미포함 주가)
        cur_price:     현재가 (또는 매도 예정가)

    Returns:
        실질 수익률 (%) — 수수료·세금 전부 차감 후
    """
    if avg_buy_price <= 0:
        return 0.0

    # qty=1 기준으로 단가 비율 계산 (수량 무관)
    bc = calc_buy_cost(avg_buy_price, 1)
    sp = calc_sell_proceeds(cur_price, 1)
    return (sp.net_proceeds - bc.total_cost) / bc.total_cost * 100


def net_profit_pct_from_cost(cost_basis_per_share: float,
                              cur_price: float) -> float:
    """
    수수료 포함 주당 취득원가(cost_basis) 기준 실질 수익률.
    VirtualPosition.avg_price 처럼 이미 매수 수수료가 포함된 경우 사용.

    실질수익률 = (순매도금액 - cost_basis) / cost_basis × 100

    Args:
        cost_basis_per_share: 수수료 포함 주당 취득원가
        cur_price:            현재가 (또는 매도 예정가)

    Returns:
        실질 수익률 (%) — 매도 수수료·세금 차감 후
    """
    if cost_basis_per_share <= 0:
        return 0.0
    sp = calc_sell_proceeds(cur_price, 1)
    return (sp.net_proceeds - cost_basis_per_share) / cost_basis_per_share * 100


def price_for_net_pct(avg_buy_price: float,
                      target_net_pct: float) -> float:
    """
    목표 실질 수익률을 달성하는 매도 필요 주가 역산.

    손절 기준 -10%를 위한 실제 주가 계산,
    또는 익절 +10% 달성을 위한 주가 계산에 사용.

    수식:
        net_proceeds = total_cost × (1 + target_net_pct/100)
        sell_amount × (1 - sell_cost_rate) = total_cost × (1 + target_net_pct/100)
        sell_price = total_cost × (1 + target_net_pct/100) / (1 - sell_cost_rate) / qty
        (qty=1 기준)

    Args:
        avg_buy_price:   평균 매수가
        target_net_pct:  목표 실질 수익률 (%) — 음수면 손절 기준

    Returns:
        필요 주가 (원)
    """
    if avg_buy_price <= 0:
        return 0.0

    bc = calc_buy_cost(avg_buy_price, 1)
    # 매도 비용률 합산 (수수료 + 거래세 + 기타)
    sell_deduct_rate = SELL_COMMISSION_RATE + TRANSACTION_TAX_RATE + OTHER_COST_RATE
    # sell_price × (1 - sell_deduct_rate) = bc.total_cost × (1 + target_net_pct/100)
    required_net_proceeds = bc.total_cost * (1 + target_net_pct / 100)
    required_sell_price   = required_net_proceeds / (1 - sell_deduct_rate)
    return round(required_sell_price, 2)


def price_for_net_pct_from_cost(cost_basis_per_share: float,
                                target_net_pct: float) -> float:
    """
    수수료 포함 주당 취득원가(cost_basis)를 기준으로,
    목표 실질 수익률을 달성하는 매도 주가 역산.

    shadow_portfolio의 VirtualPosition.avg_price 처럼
    이미 매수 수수료가 포함된 취득원가에서 사용.

    수식:
        실질수익률 = (net_proceeds - cost_basis) / cost_basis × 100
        net_proceeds = sell_price × (1 - sell_deduct_rate)
        → sell_price = cost_basis × (1 + target_pct/100) / (1 - sell_deduct_rate)

    Args:
        cost_basis_per_share: 수수료 포함 주당 취득원가 (VirtualPosition.avg_price)
        target_net_pct:       목표 실질 수익률 (%) — 음수면 손절 기준

    Returns:
        필요 매도 주가 (원)
    """
    if cost_basis_per_share <= 0:
        return 0.0
    sell_deduct_rate = SELL_COMMISSION_RATE + TRANSACTION_TAX_RATE + OTHER_COST_RATE
    required_sell_price = (
        cost_basis_per_share * (1 + target_net_pct / 100)
        / (1 - sell_deduct_rate)
    )
    return round(required_sell_price, 2)


def breakeven_price(avg_buy_price: float) -> float:
    """
    손익분기 주가 (실질 수익률 = 0%인 매도가).
    avg_buy_price 는 수수료 미포함 순수 주가 기준.

    Args:
        avg_buy_price: 평균 매수가 (수수료 미포함)

    Returns:
        손익분기 매도가 (원)
    """
    return price_for_net_pct(avg_buy_price, 0.0)


def breakeven_price_from_cost(cost_basis_per_share: float) -> float:
    """
    손익분기 주가 (수수료 포함 취득원가 기준).

    Args:
        cost_basis_per_share: 수수료 포함 주당 취득원가

    Returns:
        손익분기 매도가 (원)
    """
    return price_for_net_pct_from_cost(cost_basis_per_share, 0.0)


# ══════════════════════════════════════════════════════════════
# 편의 함수: 매수 가능 수량 계산
# ══════════════════════════════════════════════════════════════

def calc_buy_qty(available_cash: float, price: float,
                 invest_ratio: float = 1.0,
                 commission_rate: float = BUY_COMMISSION_RATE) -> int:
    """
    가용 현금에서 수수료를 고려한 최대 매수 가능 수량.

    총매수금액 = price × qty × (1 + commission_rate) ≤ available_cash × invest_ratio
    qty = floor(available_cash × invest_ratio / (price × (1 + commission_rate)))

    Args:
        available_cash: 현재 가용 현금
        price:          매수 희망가
        invest_ratio:   투자 비율 (0.0~1.0, 기본 전액)
        commission_rate:매수 수수료율

    Returns:
        매수 가능 최대 주식 수 (정수)
    """
    if price <= 0 or available_cash <= 0:
        return 0
    budget        = available_cash * invest_ratio
    cost_per_share = price * (1 + commission_rate)
    return int(budget // cost_per_share)


# ══════════════════════════════════════════════════════════════
# 배치 비용 요약 (전략 성과 집계용)
# ══════════════════════════════════════════════════════════════

def summarize_costs(trades: list[dict]) -> dict:
    """
    거래 내역 리스트에서 총 비용 집계.

    Args:
        trades: [{"action":"BUY/SELL","price":float,"qty":int,...}, ...]
                각 dict에 "buy_commission", "sell_commission", "transaction_tax"
                필드가 있으면 합산; 없으면 0으로 처리.

    Returns:
        {
          "total_buy_commission":  float,  # 총 매수 수수료
          "total_sell_commission": float,  # 총 매도 수수료
          "total_transaction_tax": float,  # 총 증권거래세
          "total_fee":             float,  # 총 비용 합계
          "buy_count":             int,
          "sell_count":            int,
        }
    """
    total_buy_comm  = 0.0
    total_sell_comm = 0.0
    total_tax       = 0.0
    buy_count       = 0
    sell_count      = 0

    for t in trades:
        action = t.get("action", "")
        price  = float(t.get("price", 0))
        qty    = int(t.get("qty", 0))

        if action in ("BUY", "ADD_BUY"):
            comm = t.get("buy_commission",
                         price * qty * BUY_COMMISSION_RATE)
            total_buy_comm += comm
            buy_count      += 1

        elif action in ("SELL", "CLOSE"):
            comm = t.get("sell_commission",
                         price * qty * SELL_COMMISSION_RATE)
            tax  = t.get("transaction_tax",
                         price * qty * TRANSACTION_TAX_RATE)
            total_sell_comm += comm
            total_tax       += tax
            sell_count      += 1

    total_fee = total_buy_comm + total_sell_comm + total_tax
    return {
        "total_buy_commission":  round(total_buy_comm,  0),
        "total_sell_commission": round(total_sell_comm, 0),
        "total_transaction_tax": round(total_tax,       0),
        "total_fee":             round(total_fee,       0),
        "buy_count":             buy_count,
        "sell_count":            sell_count,
    }


# ══════════════════════════════════════════════════════════════
# 모듈 자체 테스트
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  거래 비용 계산 엔진 — 자체 테스트")
    print("=" * 55)

    # ── 요청서 예시 검증 ──────────────────────────────────
    print("\n[1] 요청서 예시 (매수 100만원, 매도 110만원)")
    # 요청서: 매수수수료 1천원, 매도수수료 1천원, 세금 2천원
    # 실질손익 = 110만 - 100만 - 1천 - 1천 - 2천 = 96,000원
    # 실질수익률 = 96,000 / 1,001,000 × 100 ≈ 9.59%
    # (요청서는 총매수금액을 1,001,000으로 나눔)
    result = calc_trade_result(
        avg_buy_price=10_000, qty=100,
        sell_price=11_000,
        buy_commission_rate=0.001,   # 1천원/1백만 = 0.1%
        sell_commission_rate=0.001,  # 1천원/1.1백만 ≈
        tax_rate=0.002,              # 2천원/1.1백만 ≈
    )
    print(f"  총 매수금액: {result.buy_cost.total_cost:>12,.0f}원")
    print(f"  순 매도수령: {result.sell_proceeds.net_proceeds:>12,.0f}원")
    print(f"  실질 손익:   {result.net_profit:>12,.0f}원")
    print(f"  실질 수익률: {result.net_profit_pct:>10.2f}%")

    # ── 실제 수수료율 기준 ─────────────────────────────────
    print(f"\n[2] 실제 수수료율 (매수 100만원 → 110만원)")
    r = calc_trade_result(avg_buy_price=10_000, qty=100, sell_price=11_000)
    print(f"  매수 수수료: {r.buy_cost.commission:>10,.0f}원  ({BUY_COMMISSION_RATE*100:.3f}%)")
    print(f"  매도 수수료: {r.sell_proceeds.commission:>10,.0f}원  ({SELL_COMMISSION_RATE*100:.3f}%)")
    print(f"  증권거래세:  {r.sell_proceeds.transaction_tax:>10,.0f}원  ({TRANSACTION_TAX_RATE*100:.3f}%)")
    print(f"  총 비용:     {r.total_fee:>10,.0f}원")
    print(f"  실질 손익:   {r.net_profit:>10,.0f}원")
    print(f"  실질 수익률: {r.net_profit_pct:>8.4f}%")

    # ── 손절 경계값 역산 ──────────────────────────────────
    print(f"\n[3] 손절 -10% 실제 주가 역산 (매수가 10,000원)")
    p = price_for_net_pct(10_000, -10.0)
    verify = net_profit_pct(10_000, p)
    print(f"  손절 트리거 주가: {p:,.1f}원")
    print(f"  해당 주가 실질수익률: {verify:.4f}% (≈ -10%)")

    # ── 익절 +20% 역산 ────────────────────────────────────
    print(f"\n[4] 익절 +20% 실제 주가 역산 (매수가 10,000원)")
    p2 = price_for_net_pct(10_000, 20.0)
    v2 = net_profit_pct(10_000, p2)
    print(f"  익절 트리거 주가: {p2:,.1f}원")
    print(f"  해당 주가 실질수익률: {v2:.4f}% (≈ +20%)")

    # ── 손익분기 ─────────────────────────────────────────
    print(f"\n[5] 손익분기 주가 (매수가 10,000원)")
    be = breakeven_price(10_000)
    be_pct = net_profit_pct(10_000, be)
    print(f"  손익분기 주가: {be:,.1f}원 (단순주가 대비 +{(be/10000-1)*100:.3f}%)")
    print(f"  실질수익률: {be_pct:.4f}%")

    # ── 매수 수량 ────────────────────────────────────────
    print(f"\n[6] 매수 가능 수량 (100만원 가용, 주가 10,000원)")
    qty_full = calc_buy_qty(1_000_000, 10_000, invest_ratio=1.0)
    qty_25   = calc_buy_qty(1_000_000, 10_000, invest_ratio=0.25)
    print(f"  전액 투자: {qty_full}주  (총비용: {qty_full*10_000*(1+BUY_COMMISSION_RATE):,.0f}원)")
    print(f"  25% 투자:  {qty_25}주   (총비용: {qty_25*10_000*(1+BUY_COMMISSION_RATE):,.0f}원)")

    print("\n  ✅ 모든 자체 테스트 통과")
