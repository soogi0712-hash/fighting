"""
피라미딩(Pyramiding) + 복리 재투자 + 복리형 초회전 단타 전략
=============================================================

★ 핵심 철학 ★
  1. 무차입(No Leverage) — 보유 현금 범위 내에서만 매수
  2. 복리형 초회전 단타  — 1~2% 수익을 빠르게 반복 실현
  3. Early Entry 분할진입 — BUY SCORE ≥ 0.60 → 30% 선진입,
                            BUY SCORE ≥ 0.75 → 나머지 70% 진입
  4. 수익 반납 방지      — SELL SCORE ≥ 6 + 수익 +1.0% 이상 → 즉시 청산

★ 진입 규칙 ★
  - Early Entry  : BUY SCORE ≥ 0.60 → 예정 투자금 30% 선진입
  - Full Entry   : BUY SCORE ≥ 0.75 → 나머지 70% 추가 진입
  - 쿨다운       : 매도 후 15분 이내 동일 종목 재진입 금지
  - 연속 손실    : 동일 종목 당일 2회 연속 손실 시 당일 재진입 금지

★ 청산 규칙 ★
  - 전량 익절    : 실질 수익률 ≥ +2.0% 즉시 전량 매도 (예외 없음)
  - SELL SCORE   : sell_score ≥ 6 + net_pct ≥ 1.0% → 수익 반납 방지 즉시 매도
  - 시간 청산    : 20분 내 +0.5% 미달 청산 / 40분 내 +1.0% 미달 청산
  - KRW 금액익절 : 미실현이익 ≥ 3만원 전량익절 / ≥ 1만원 부분익절
  - 트레일링     : +1.5% 달성 후 고점 대비 -1.0% 하락 시 청산
  - 최종 안전장치: 실질 수익률 ≤ -5.0% (고정 손절)

★ 비용 원칙 ★
  - 단일 비용 원천: screener/transaction_cost.py
  - avg_price = 수수료 포함 주당 취득원가 (total_cost / qty)
  - 손절/익절/추가매수 판단 → 모두 실질수익률 기준
  - profit 필드 → 실질 순손익 (수수료·세금 차감 후)
"""

import os
import json
import math
from datetime import datetime, timedelta
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

# ── 단계별 설정 ────────────────────────────────────────────────
# Early Entry  : 30% 선진입 (BUY SCORE ≥ 0.60)
# Full Entry   : +70% 추가 진입 (BUY SCORE ≥ 0.75)
# 2단계~4단계  : 피라미딩 추가 매수
PYRAMID_LEVELS = {
    1: {"add_pct": 0.00, "invest_ratio": 0.30, "indicator_min": 0,
        "label": "1단계 Early Entry(30%)"},
    2: {"add_pct": 1.00, "invest_ratio": 0.15, "indicator_min": 0,
        "label": "2단계 실질+1% 추가(15%)"},
    3: {"add_pct": 2.00, "invest_ratio": 0.10, "indicator_min": 0,
        "label": "3단계 실질+2% 추가(10%)"},
    4: {"add_pct": 3.00, "invest_ratio": 0.10, "indicator_min": 0,
        "label": "4단계 실질+3% 추가(10%)"},
}

# ★ 복리형 초회전 단타 파라미터
# ★ 모든 % 기준은 실질수익률 기준 (수수료·세금 차감 후)
STOP_LOSS_PCT            = -5.0   # ★ 최종 안전장치 -5% (손절은 청산 순서 마지막)
TRAILING_STOP_PCT        = -1.0   # 트레일링 스탑 (고점 대비 -1.0%)
TRAILING_ACTIVATE_PCT    =  1.5   # 트레일링 활성화 (+1.5% 달성 시)
PROFIT_SUPER_PCT         =  2.5   # ★ +2.5% 무조건 전량 익절 (최우선)
PROFIT_FULL_PCT          =  2.0   # ★ +2.0% 전량 익절 (SELL_SCORE 무관)
PROFIT_TRAIL_PCT         =  1.5   # ★ +1.5% + SELL_SCORE≥4 → 전량 익절
PARTIAL_PROFIT_PCT       =  2.0   # 단계별 부분 익절 기준 (% 폴백용)
PARTIAL_SELL_RATIO       =  0.5   # 부분 익절 시 해당 단계 보유 비율

# ── KRW 금액 기준 익절 ─────────────────────────────────────────
PROFIT_PARTIAL_KRW       = 10_000   # 미실현이익 1만원 → 50% 부분익절
PROFIT_FULL_KRW          = 30_000   # 미실현이익 3만원 → 전량 익절

# ── 시간 청산 파라미터 ─────────────────────────────────────────
TIME_EXIT_20_MIN         = 20       # 20분 후 미달 청산 체크
TIME_EXIT_20_PCT         = 0.5     # 20분 내 +0.5% 미달이면 청산
TIME_EXIT_40_MIN         = 40       # 40분 후 미달 청산 체크
TIME_EXIT_40_PCT         = 1.0     # 40분 내 +1.0% 미달이면 청산

# ── 쿨다운 / 연속 손실 ────────────────────────────────────────
COOLDOWN_MIN             = 15       # 매도 후 재진입 쿨다운 (분)
MAX_DAILY_LOSSES         = 2        # 당일 연속 손실 허용 횟수 (이 이상이면 당일 금지)

# ── SELL SCORE 수익 반납 방지 ─────────────────────────────────
SELL_SCORE_PROTECTION_PCT = 1.0    # 이 이상 수익 시 SELL SCORE 즉시 매도 적용

# ── Early / Full Entry BUY SCORE 임계 ─────────────────────────
BUY_SCORE_EARLY          = 0.40   # 0.60 → 0.40 (빠른 선점 전략)
BUY_SCORE_FULL           = 0.55   # 0.75 → 0.55 (빠른 선점 전략)

# ── 현금 안전 버퍼 ────────────────────────────────────────────
# 매수 예산 = 실제 주문가능현금 × CASH_SAFETY_BUFFER.
# 지정가↔체결가 차이·호가단위·시장가 슬리피지를 흡수해 총 주문금액이
# 실제 주문가능현금을 절대 초과하지 않도록 한다(신용·미수 미사용).
CASH_SAFETY_BUFFER       = 0.995   # 0.5% 여유


class PyramidStrategyManager:
    """
    피라미딩 포지션 완전 관리자 — 복리형 초회전 단타 모드
    - Early Entry(30%) / Full Entry(+70%) 분할 진입
    - +2% 전량 익절 / 시간 청산(20분/40분) / SELL SCORE 수익반납방지
    - 쿨다운 15분 / 연속 손실 2회 → 당일 재진입 금지
    - ★ 모든 수익률·손절·익절 판단은 실질수익률 기준
    """

    def __init__(self, kis_api, max_per_stock: float, max_total: float):
        self.api           = kis_api
        self.max_per_stock = max_per_stock   # 종목당 최대 투자금
        self.max_total     = max_total       # 전체 최대 투자금
        self.positions     = {}              # {code: PyramidPosition}
        self.compound_pool = 0.0             # 복리 적립금

        # ★ 쿨다운 / 연속 손실 추적
        self.cooldown      = {}              # {code: last_sell_datetime}
        self.daily_losses  = {}              # {code: {"date": str, "count": int}}

        os.makedirs(os.path.dirname(PYRAMID_FILE), exist_ok=True)
        self._load()

    # ── 저장/로드 ──────────────────────────────────────────────
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

    # ── 쿨다운 / 연속 손실 체크 ──────────────────────────────
    def _is_cooldown(self, code: str) -> bool:
        """15분 이내 매도한 종목이면 True"""
        last_sell = self.cooldown.get(code)
        if last_sell is None:
            return False
        elapsed = (datetime.now() - last_sell).total_seconds() / 60
        if elapsed < COOLDOWN_MIN:
            logger.info(f"⏳ {code} 쿨다운 중 (잔여 {COOLDOWN_MIN - elapsed:.1f}분)")
            return True
        return False

    def _is_daily_loss_banned(self, code: str) -> bool:
        """당일 연속 손실 2회 이상이면 True (당일 재진입 금지)"""
        rec = self.daily_losses.get(code)
        if rec is None:
            return False
        today_str = datetime.now().strftime("%Y-%m-%d")
        if rec["date"] != today_str:
            # 날짜가 바뀌었으면 리셋
            del self.daily_losses[code]
            return False
        if rec["count"] >= MAX_DAILY_LOSSES:
            logger.info(f"🚫 {code} 당일 연속 손실 {rec['count']}회 → 재진입 금지")
            return True
        return False

    def _record_sell(self, code: str, is_loss: bool):
        """매도 후 쿨다운 타임스탬프 + 연속 손실 카운터 갱신"""
        # 쿨다운 기록
        self.cooldown[code] = datetime.now()

        # 연속 손실 카운터
        today_str = datetime.now().strftime("%Y-%m-%d")
        if is_loss:
            rec = self.daily_losses.get(code)
            if rec and rec["date"] == today_str:
                self.daily_losses[code]["count"] += 1
            else:
                self.daily_losses[code] = {"date": today_str, "count": 1}
            logger.info(
                f"📉 {code} 당일 손실 누적 {self.daily_losses[code]['count']}회"
            )
        else:
            # 수익 실현 시 연속 손실 카운터 리셋
            if code in self.daily_losses:
                self.daily_losses[code]["count"] = 0

    # ── 핵심: 매매 결정 ──────────────────────────────────────
    def evaluate(self, code: str, name: str,
                 cur_price: float, indicator_score: int,
                 available_cash: float,
                 today_high: float = 0.0,
                 buy_score_norm: float = 0.0,
                 sell_score: int = 0) -> dict:
        """
        매매 판단 (복리형 초회전 단타 모드).

        Args:
            indicator_score:  현재 매수 신호 지표 수 (0~7, 하위 호환)
            buy_score_norm:   BUY SCORE 정규화값 (0.0~1.0)
            sell_score:       SELL SCORE 가중치 합산 (0~27)
            today_high:       당일 장중 고가

        Returns:
            {action, level, qty, price, reason, net_pct, profit, ...}
        ★ 모든 % 판단은 실질수익률 기준 ★
        """
        pos = self.positions.get(code)

        # ── 포지션 없음 → 진입 검토 ─────────────────────────
        if pos is None:
            return self._try_entry(code, name, cur_price,
                                   indicator_score, available_cash,
                                   buy_score_norm=buy_score_norm)

        # ── 포지션 있음 ──────────────────────────────────────
        # ★ 장중 고가까지 반영해 highest_price 정확히 갱신
        effective_high = max(cur_price, today_high) if today_high > 0 else cur_price
        pos.update_high(effective_high)
        # ★ lowest_price 갱신 (거래 복기·max_drawdown_pct 용, 매매 판단 미사용)
        pos.update_low(cur_price)

        # ★ 실질 수익률 계산 (수수료·세금 차감)
        net_pct = net_profit_pct_from_cost(pos.avg_price, cur_price)

        # ★ gross_pct / fee_pct 계산 (로그용)
        gross_pct = (cur_price - pos.avg_price) / pos.avg_price * 100 if pos.avg_price > 0 else 0.0
        fee_pct   = gross_pct - net_pct   # 수수료·세금 차감분 (양수 = 비용)

        # ─ 보유 시간 계산 ─
        now = datetime.now()
        try:
            created_at = datetime.fromisoformat(pos.created_at)
        except Exception:
            created_at = now
        elapsed_min = (now - created_at).total_seconds() / 60

        # ══════════════════════════════════════════════════════
        # [익절판정] 로그 — 포지션 보유 시 매 루프 항상 출력
        # ══════════════════════════════════════════════════════
        # 익절 기준 판별
        if net_pct >= PROFIT_SUPER_PCT:
            _profit_basis = f"+{PROFIT_SUPER_PCT}%무조건전량"
        elif net_pct >= PROFIT_FULL_PCT:
            _profit_basis = f"+{PROFIT_FULL_PCT}%전량"
        elif net_pct >= PROFIT_TRAIL_PCT:
            _profit_basis = f"+{PROFIT_TRAIL_PCT}%+SELL_SCORE≥4조건부전량(현재SELL_SCORE={sell_score})"
        else:
            _profit_basis = f"익절기준미달(최소+{PROFIT_TRAIL_PCT}%필요)"

        logger.info(
            f"[익절판정] 종목={name}({code}) | "
            f"매수가={pos.avg_price:,.0f} | 현재가={cur_price:,.0f} | "
            f"gross_pct={gross_pct:+.3f}% | fee_pct={fee_pct:.3f}% | "
            f"net_pct={net_pct:+.3f}% | 익절기준={_profit_basis} | "
            f"SELL_SCORE={sell_score} | 경과={elapsed_min:.0f}분"
        )

        # ══════════════════════════════════════════════════════
        # 청산 우선순위: ①+2.5% → ②+2.0% → ③+1.5%+SCORE≥4
        #               → ④SCORE≥6 → ⑤KRW → ⑥트레일링
        #               → ⑦시간청산 → ⑧손절(최후)
        # ══════════════════════════════════════════════════════

        # ── ① +2.5% 무조건 전량 익절 (최우선, 예외 없음) ─────
        if net_pct >= PROFIT_SUPER_PCT:
            sp = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            logger.info(
                f"[익절판정] 종목={name}({code}) | net_pct={net_pct:+.3f}% | "
                f"익절기준=+{PROFIT_SUPER_PCT}%무조건전량 | 결과=SELL_ALL"
            )
            return self._sell_result(
                "SELL_ALL", code, name, cur_price, pos, net_pct, sp, net_profit,
                reason=f"✅+2.5%무조건전량익절(실질{net_pct:.2f}% ≥ {PROFIT_SUPER_PCT}%)"
            )

        # ── ② +2.0% 전량 익절 (SELL_SCORE 무관, 예외 없음) ───
        if net_pct >= PROFIT_FULL_PCT:
            sp = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            logger.info(
                f"[익절판정] 종목={name}({code}) | net_pct={net_pct:+.3f}% | "
                f"익절기준=+{PROFIT_FULL_PCT}%전량 | 결과=SELL_ALL"
            )
            return self._sell_result(
                "SELL_ALL", code, name, cur_price, pos, net_pct, sp, net_profit,
                reason=f"✅+2.0%전량익절(실질{net_pct:.2f}% ≥ {PROFIT_FULL_PCT}%)"
            )

        # ── ③ +1.5% + SELL_SCORE≥4 → 전량 익절 ──────────────
        if net_pct >= PROFIT_TRAIL_PCT and sell_score >= 4:
            sp = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            logger.info(
                f"[익절판정] 종목={name}({code}) | net_pct={net_pct:+.3f}% | "
                f"익절기준=+{PROFIT_TRAIL_PCT}%+SELL_SCORE≥4 | 결과=SELL_ALL"
            )
            return self._sell_result(
                "SELL_ALL", code, name, cur_price, pos, net_pct, sp, net_profit,
                reason=(f"✅+1.5%익절+SELL_SCORE(실질{net_pct:.2f}%≥"
                        f"{PROFIT_TRAIL_PCT}%, score={sell_score}≥4)")
            )

        # HOLD 사유 기록 (익절 미발생 시)
        if net_pct >= PROFIT_TRAIL_PCT:
            _hold_reason = f"+1.5%이상이나SELL_SCORE={sell_score}<4(SELL_SCORE≥4필요)"
        elif net_pct > 0:
            _hold_reason = f"수익중이나익절기준미달(net={net_pct:+.2f}%, 최소+{PROFIT_TRAIL_PCT}%필요)"
        else:
            _hold_reason = f"손실중(net={net_pct:+.2f}%)"

        logger.info(
            f"[익절판정] 종목={name}({code}) | net_pct={net_pct:+.3f}% | "
            f"결과=HOLD | HOLD사유={_hold_reason}"
        )

        # ── ④ SELL SCORE 수익 반납 방지 (net_pct≥1.0% + score≥6) ─
        if sell_score >= 6 and net_pct >= SELL_SCORE_PROTECTION_PCT:
            sp = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            return self._sell_result(
                "SELL_ALL", code, name, cur_price, pos, net_pct, sp, net_profit,
                reason=(f"🚨SELL SCORE수익반납방지(score={sell_score}≥6, "
                        f"실질{net_pct:.2f}%≥{SELL_SCORE_PROTECTION_PCT}%)")
            )

        # ── ⑤ KRW 금액 기준 익절 ─────────────────────────────
        cur_profit_amt = (cur_price - pos.avg_price) * pos.total_qty  # 근사값

        if cur_profit_amt >= PROFIT_FULL_KRW:
            sp         = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            return self._sell_result(
                "SELL_ALL", code, name, cur_price, pos, net_pct, sp, net_profit,
                reason=(f"💰KRW전량익절 미실현이익"
                        f"{cur_profit_amt:,.0f}원 ≥ {PROFIT_FULL_KRW:,}원")
            )

        if cur_profit_amt >= PROFIT_PARTIAL_KRW:
            sell_qty = max(1, pos.total_qty // 2)
            sp       = calc_sell_proceeds(cur_price, sell_qty)
            cost_b   = pos.avg_price * sell_qty
            net_p    = sp.net_proceeds - cost_b
            return {
                "action":   "SELL_PARTIAL",
                "reason":   (f"💰KRW부분익절(50%) 미실현이익"
                             f"{cur_profit_amt:,.0f}원 ≥ {PROFIT_PARTIAL_KRW:,}원"),
                "qty":      sell_qty,
                "price":    cur_price,
                "code":     code,
                "name":     name,
                "level":    pos.current_level,
                "net_pct":  round(net_pct, 2),
                "profit":   round(net_p, 0),
                "elapsed_min": round(elapsed_min, 1),
                "sell_commission":  round(sp.commission, 0),
                "transaction_tax":  round(sp.transaction_tax, 0),
                "total_fee":        round(sp.commission + sp.transaction_tax, 0),
            }

        # ── ⑥ 트레일링 스탑 (+1.5% 활성화, -1.0% 하락 시 청산) ─
        activate_trigger = price_for_net_pct_from_cost(
            pos.avg_price, TRAILING_ACTIVATE_PCT
        )
        trail_pct = (cur_price - pos.highest_price) / pos.highest_price * 100
        if (pos.highest_price >= activate_trigger and
                trail_pct <= TRAILING_STOP_PCT):
            sp = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            return self._sell_result(
                "SELL_ALL", code, name, cur_price, pos, net_pct, sp, net_profit,
                reason=(f"🔔트레일링(고점대비{trail_pct:.2f}% ≤ "
                        f"{TRAILING_STOP_PCT}%, 실질{net_pct:.2f}%)")
            )

        # ── ⑦ 시간 청산 (20분 / 40분) ──────────────────────
        # 20분 경과 후 실질 수익률 +0.5% 미달이면 청산
        if elapsed_min >= TIME_EXIT_20_MIN and net_pct < TIME_EXIT_20_PCT:
            sp = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            return self._sell_result(
                "SELL_ALL", code, name, cur_price, pos, net_pct, sp, net_profit,
                reason=(f"⏱️20분 시간청산(경과{elapsed_min:.0f}분, "
                        f"실질{net_pct:.2f}% < +{TIME_EXIT_20_PCT}%)")
            )

        # 40분 경과 후 실질 수익률 +1.0% 미달이면 청산
        if elapsed_min >= TIME_EXIT_40_MIN and net_pct < TIME_EXIT_40_PCT:
            sp = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            return self._sell_result(
                "SELL_ALL", code, name, cur_price, pos, net_pct, sp, net_profit,
                reason=(f"⏱️40분 시간청산(경과{elapsed_min:.0f}분, "
                        f"실질{net_pct:.2f}% < +{TIME_EXIT_40_PCT}%)")
            )

        # ── ⑧ 최종 안전장치 손절: -5.0% (우선순위 마지막) ───
        if net_pct <= STOP_LOSS_PCT:
            sp = calc_sell_proceeds(cur_price, pos.total_qty)
            cost_basis = pos.avg_price * pos.total_qty
            net_profit = sp.net_proceeds - cost_basis
            return self._sell_result(
                "SELL_ALL", code, name, cur_price, pos, net_pct, sp, net_profit,
                reason=f"🛑최종손절(실질{net_pct:.2f}% ≤ {STOP_LOSS_PCT}%)"
            )

        # ── ⑨ % 기준 단계별 부분 익절 (폴백) ─────────────────
        for lvl, entry in pos.level_entries.items():
            entry_avg_price = entry.get("avg_price", entry["price"])
            remaining       = entry["remaining"]
            if remaining <= 0:
                continue
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
                    "profit":        round(net_p, 0),
                    "elapsed_min":   round(elapsed_min, 1),
                    "sell_commission":  round(sp.commission, 0),
                    "transaction_tax":  round(sp.transaction_tax, 0),
                    "total_fee":        round(sp.commission + sp.transaction_tax, 0),
                }

        # ── ⑩ Full Entry (나머지 70%) 진입 검토 ────────────
        # Early Entry(30%)만 체결된 포지션이 있고, BUY SCORE ≥ 0.75이면 추가 진입
        if (pos.current_level == 1 and
                "full_entry_done" not in pos.level_entries and
                buy_score_norm >= BUY_SCORE_FULL and
                net_pct > -1.0):  # 손실 중이면 추가 진입 금지
            return self._try_full_entry(code, name, cur_price,
                                        indicator_score, available_cash, pos)

        # ── ⑨ 다음 피라미딩 단계 진입 검토 ─────────────────
        next_level = pos.current_level + 1
        if next_level <= 4:
            lvl_cfg       = PYRAMID_LEVELS[next_level]
            required_gain = lvl_cfg["add_pct"]
            gain_from_entry = net_pct

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
            "net_pct":       round(net_pct, 2),
            "highest_price": pos.highest_price,
            "elapsed_min":   round(elapsed_min, 1),
            "reason": (f"피라미딩{pos.current_level}단계 유지 "
                       f"(실질{net_pct:+.2f}%, "
                       f"경과{elapsed_min:.0f}분, "
                       f"고점대비{trail_pct:+.1f}%)"),
        }

    # ── 헬퍼: SELL 결과 dict 생성 ───────────────────────────
    def _sell_result(self, action, code, name, cur_price, pos,
                     net_pct, sp, net_profit, reason):
        now = datetime.now()
        try:
            created_at = datetime.fromisoformat(pos.created_at)
        except Exception:
            created_at = now
        elapsed_min = (now - created_at).total_seconds() / 60
        return {
            "action":   action,
            "reason":   reason,
            "qty":      pos.total_qty,
            "price":    cur_price,
            "code":     code,
            "name":     name,
            "level":    pos.current_level,
            "net_pct":  round(net_pct, 2),
            "profit":   round(net_profit, 0),
            "elapsed_min": round(elapsed_min, 1),
            "sell_commission":  round(sp.commission, 0),
            "transaction_tax":  round(sp.transaction_tax, 0),
            "total_fee":        round(sp.commission + sp.transaction_tax, 0),
        }

    # ── Early Entry (30%) 첫 진입 ───────────────────────────
    def _try_entry(self, code, name, price, indicator_score, cash,
                   buy_score_norm: float = 0.0) -> dict:
        # ── 쿨다운 체크 ─
        if self._is_cooldown(code):
            last_sell = self.cooldown[code]
            elapsed   = (datetime.now() - last_sell).total_seconds() / 60
            remaining = COOLDOWN_MIN - elapsed
            return {"action": "SKIP",
                    "reason": f"쿨다운 중 (잔여 {remaining:.1f}분)",
                    "code": code, "name": name}

        # ── 연속 손실 체크 ─
        if self._is_daily_loss_banned(code):
            return {"action": "SKIP",
                    "reason": "당일 연속 손실 2회 → 재진입 금지",
                    "code": code, "name": name}

        # ── BUY SCORE 체크 ─
        # buy_score_norm이 전달되지 않았거나 Early Entry 임계 미달이면 SKIP
        if buy_score_norm > 0 and buy_score_norm < BUY_SCORE_EARLY:
            return {"action": "SKIP",
                    "reason": f"BUY SCORE 부족 ({buy_score_norm:.2f} < {BUY_SCORE_EARLY})",
                    "code": code, "name": name}

        cfg = PYRAMID_LEVELS[1]

        if cash <= 0:
            logger.warning(f"⚠️ {name}({code}) cash=0 감지 → 잔고 조회 실패 SKIP")
            return {"action": "SKIP", "reason": f"현금 0원 (잔고조회 실패 추정)",
                    "code": code, "name": name}

        # 종목당/전체 투자한도 제거 — 실제 주문가능현금 범위만 제한
        # (compound_pool 미가산, 신용·미수 금지)
        investable = max(0.0, cash) * CASH_SAFETY_BUFFER
        invest_amt = investable * cfg["invest_ratio"]   # 30%

        # ★ 최소 1주 보장
        min_for_one = price * 1.002
        if invest_amt < min_for_one and cash >= min_for_one:
            invest_amt = min_for_one
            logger.info(f"★ {name} 고가주 최소 1주 보장 ({price:,.0f}원)")

        qty = calc_buy_qty(invest_amt, price, invest_ratio=1.0)

        if qty < 1:
            return {"action": "SKIP",
                    "reason": f"투자가능금액 부족 (현금={cash:,.0f}원 × 30%={invest_amt:,.0f}원, 종목가={price:,.0f}원)",
                    "code": code, "name": name}

        bc = calc_buy_cost(price, qty)

        # ★ Early Entry vs Full Entry 구분
        action_label = "BUY_LEVEL1_FULL" if buy_score_norm >= BUY_SCORE_FULL else "BUY_LEVEL1_EARLY"
        entry_type   = "본진입(100%)" if buy_score_norm >= BUY_SCORE_FULL else "Early Entry(30%)"

        # BUY SCORE ≥ 0.75이면 전체 투자금(100%) 한번에 진입
        if buy_score_norm >= BUY_SCORE_FULL:
            # Full Entry: 100% 투자
            invest_full = investable
            qty_full = calc_buy_qty(invest_full, price, invest_ratio=1.0)
            if qty_full >= 1:
                bc_full = calc_buy_cost(price, qty_full)
                return {
                    "action":          "BUY_LEVEL1_FULL",
                    "level":           1,
                    "qty":             qty_full,
                    "price":           price,
                    "amount":          bc_full.buy_amount,
                    "total_cost":      bc_full.total_cost,
                    "buy_commission":  round(bc_full.commission, 0),
                    "code":            code,
                    "name":            name,
                    "buy_score_norm":  buy_score_norm,
                    "reason":          (f"피라미딩 1단계 {entry_type} "
                                        f"(BUY SCORE {buy_score_norm:.2f}≥{BUY_SCORE_FULL}, "
                                        f"투자금={bc_full.total_cost:,.0f}원)"),
                    "using_compound":  0,  # 복리풀 매수여력 미가산(원칙 6)
                }

        # Early Entry: 30% 진입
        return {
            "action":          "BUY_LEVEL1_EARLY",
            "level":           1,
            "qty":             qty,
            "price":           price,
            "amount":          bc.buy_amount,
            "total_cost":      bc.total_cost,
            "buy_commission":  round(bc.commission, 0),
            "code":            code,
            "name":            name,
            "buy_score_norm":  buy_score_norm,
            "reason":          (f"피라미딩 1단계 {entry_type} "
                                f"(BUY SCORE {buy_score_norm:.2f}≥{BUY_SCORE_EARLY}, "
                                f"투자금={bc.total_cost:,.0f}원, 현금기준)"),
            "using_compound":  0,  # 복리풀 매수여력 미가산(원칙 6)
        }

    # ── Full Entry (나머지 70%) 추가 진입 ────────────────────
    def _try_full_entry(self, code, name, price, indicator_score, cash, pos) -> dict:
        """Early Entry 후 BUY SCORE ≥ 0.75 달성 시 나머지 70% 추가 진입"""
        # 이미 full_entry_done 마크가 있으면 스킵
        if "full_entry_done" in pos.level_entries:
            return {"action": "HOLD", "code": code, "name": name,
                    "level": pos.current_level, "reason": "Full Entry 이미 완료"}

        # Full 조건 충족 → 종목당/전체 한도 없이 '남은 주문가능현금' 전액 범위에서 추가.
        # Early(30%) 후 남은 현금 전액까지 사용해 최종 현금 100%에 도달할 수 있게 한다.
        # (compound_pool 미가산 — 원칙 6 / 신용·미수 금지)
        full_invest_amt = max(0.0, cash) * CASH_SAFETY_BUFFER

        qty = calc_buy_qty(full_invest_amt, price, invest_ratio=1.0)

        if qty < 1:
            return {"action": "HOLD", "code": code, "name": name,
                    "level": pos.current_level,
                    "reason": f"Full Entry 투자금 부족 ({full_invest_amt:,.0f}원 < {price:,.0f}원)"}

        bc = calc_buy_cost(price, qty)
        net_pct = net_profit_pct_from_cost(pos.avg_price, price)

        return {
            "action":         "BUY_LEVEL1_FULL_ADD",
            "level":          1,
            "qty":            qty,
            "price":          price,
            "amount":         bc.buy_amount,
            "total_cost":     bc.total_cost,
            "buy_commission": round(bc.commission, 0),
            "code":           code,
            "name":           name,
            "net_pct":        round(net_pct, 2),
            "reason":         (f"Full Entry 남은현금 추가 진입 "
                               f"(BUY SCORE 달성, 투자금={bc.total_cost:,.0f}원)"),
            "using_compound": 0,  # 복리풀 매수여력 미가산(원칙 6)
        }

    # ── 추가 매수 ─────────────────────────────────────────────
    def _try_add(self, code, name, price, level, indicator_score, cash, pos) -> dict:
        cfg = PYRAMID_LEVELS[level]

        if level in pos.level_entries:
            return {"action": "HOLD", "code": code, "name": name,
                    "level": pos.current_level, "reason": f"{level}단계 이미 진입"}

        # 종목당/전체 한도 제거 — 남은 주문가능현금 × 단계비율만 사용
        # (compound_pool 미가산 — 원칙 6 / 신용·미수 금지)
        cash_budget   = max(0.0, cash) * CASH_SAFETY_BUFFER
        invest_amt    = cash_budget * cfg["invest_ratio"]

        qty = calc_buy_qty(invest_amt, price, invest_ratio=1.0)

        if qty < 1:
            return {"action": "HOLD", "code": code, "name": name,
                    "level": pos.current_level, "reason": "추가 투자금 부족"}

        bc      = calc_buy_cost(price, qty)
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
            "using_compound": 0,  # 복리풀 매수여력 미가산(원칙 6)
        }

    # ── 포지션 반영 ──────────────────────────────────────────
    def apply_buy(self, code: str, name: str,
                  level: int, qty: int, price: float,
                  using_compound: float = 0,
                  is_full_add: bool = False):
        """
        매수 체결 후 포지션 반영.
        avg_price = 수수료 포함 주당 취득원가 (total_cost / qty).
        is_full_add: Full Entry 나머지 70% 추가 진입 여부
        """
        bc = calc_buy_cost(price, qty)

        # 복리풀 차감
        used_compound      = min(self.compound_pool, using_compound)
        self.compound_pool = max(0, self.compound_pool - used_compound)

        if code not in self.positions:
            pos = PyramidPosition(code, name, price)
            pos.avg_price = bc.total_cost / qty
            self.positions[code] = pos
        self.positions[code].add_level(level, qty, price, bc.total_cost)

        # Full Entry 완료 마크
        if is_full_add:
            self.positions[code].level_entries["full_entry_done"] = {
                "price": price, "qty": qty, "added_at": datetime.now().isoformat()
            }

        self._save()
        logger.info(
            f"📥 피라미딩 {level}단계{'(Full+)' if is_full_add else ''} "
            f"{name} {qty}주 @{price:,}원 "
            f"(수수료 {bc.commission:.0f}원, 총원가 {bc.total_cost:,.0f}원, "
            f"복리풀사용={used_compound:,.0f}원)"
        )

    def apply_sell(self, code: str, qty: int, price: float,
                   level: int = None, is_full: bool = False) -> dict:
        """
        매도 체결 후 복리풀 적립 + 쿨다운·손실카운터 갱신.
        ★ 실질 순손익(수수료·세금 차감 후)을 복리풀에 적립.
        """
        pos = self.positions.get(code)
        if pos is None:
            return {"net_profit": 0.0, "net_profit_pct": 0.0}

        sp         = calc_sell_proceeds(price, qty)
        cost_basis = pos.avg_price * qty
        net_profit = sp.net_proceeds - cost_basis
        net_pct    = (net_profit / cost_basis * 100) if cost_basis > 0 else 0.0

        is_loss = net_profit < 0

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

        # ★ 쿨다운 기록 + 연속 손실 카운터 갱신
        self._record_sell(code, is_loss)

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


# ── 피라미딩 포지션 데이터 클래스 ────────────────────────────
class PyramidPosition:
    def __init__(self, code, name, entry_price):
        self.code          = code
        self.name          = name
        self.entry_price   = entry_price   # 최초 진입가 (순수 주가)
        self.highest_price = entry_price   # 고가 (트레일링 스탑용)
        # ★ lowest_price: 거래 복기 및 max_drawdown_pct 계산용 (매매 판단에 미사용)
        self.lowest_price  = entry_price   # 저가 (보유 중 최저가 추적)
        self.current_level = 0
        self.level_entries = {}            # {level: {price, avg_price, qty, remaining}}
        self.total_qty     = 0
        # ★ avg_price = 수수료 포함 주당 취득원가 (total_cost / total_qty)
        self.avg_price     = entry_price
        self.created_at    = datetime.now().isoformat()
        # ★ trade_id: 거래 저널 연결용 (재시작 후에도 매수·매도 연결 유지)
        self.trade_id: str = ""           # journal.make_trade_id()로 설정

    def add_level(self, level: int, qty: int, price: float,
                  total_cost: float = None):
        """
        레벨 추가. total_cost 가 제공되면 수수료 포함 취득원가로 avg_price 갱신.
        """
        if total_cost is None:
            bc         = calc_buy_cost(price, qty)
            total_cost = bc.total_cost

        avg_price_this_level = total_cost / qty

        self.level_entries[level] = {
            "price":     price,
            "avg_price": avg_price_this_level,
            "qty":       qty,
            "remaining": qty,
            "total_cost": round(total_cost, 2),
            "added_at":  datetime.now().isoformat(),
        }
        self.current_level = max(self.current_level, level)
        self.total_qty    += qty

        # ★ 전체 avg_price 갱신 (모든 레벨 총취득원가 / 총수량)
        # full_entry_done 마크는 수량 계산에서 제외
        numeric_levels = {k: v for k, v in self.level_entries.items()
                          if isinstance(k, int)}
        total_all_cost = sum(
            e.get("total_cost", e["price"] * e["qty"])
            for e in numeric_levels.values()
        )
        total_all_qty = sum(e["qty"] for e in numeric_levels.values())
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

    def update_low(self, price: float):
        """보유 중 최저가 갱신 (거래 복기·max_drawdown_pct 계산용, 매매 판단 미사용)."""
        if price < self.lowest_price:
            self.lowest_price = price

    def unrealized_pct(self, cur_price: float) -> float:
        """실질 미실현 수익률 (수수료·세금 차감)"""
        return net_profit_pct_from_cost(self.avg_price, cur_price)

    def summary(self) -> dict:
        return {
            "code":          self.code,
            "name":          self.name,
            "entry_price":   self.entry_price,
            "avg_price":     round(self.avg_price, 4),
            "highest_price": self.highest_price,
            "lowest_price":  self.lowest_price,
            "current_level": self.current_level,
            "total_qty":     self.total_qty,
            "levels":        self.level_entries,
            "created_at":    self.created_at,
            "trade_id":      self.trade_id,
        }

    def to_dict(self) -> dict:
        return {
            "code":          self.code,
            "name":          self.name,
            "entry_price":   self.entry_price,
            "highest_price": self.highest_price,
            "lowest_price":  self.lowest_price,
            "current_level": self.current_level,
            "avg_price":     self.avg_price,
            "total_qty":     self.total_qty,
            "level_entries": self.level_entries,
            "created_at":    self.created_at,
            "trade_id":      self.trade_id,
        }

    @classmethod
    def from_dict(cls, d: dict):
        obj = cls(d["code"], d["name"], d["entry_price"])
        obj.highest_price = d.get("highest_price", d["entry_price"])
        # ★ 하위 호환: 기존 저장 데이터에 lowest_price 없어도 정상 로드
        obj.lowest_price  = d.get("lowest_price", d["entry_price"])
        obj.current_level = d.get("current_level", 0)
        obj.avg_price     = d.get("avg_price", d["entry_price"])
        obj.total_qty     = d.get("total_qty", 0)
        obj.level_entries = d.get("level_entries", {})
        obj.created_at    = d.get("created_at", datetime.now().isoformat())
        # ★ 하위 호환: 기존 저장 데이터에 trade_id 없어도 정상 로드
        obj.trade_id      = d.get("trade_id", "")
        return obj
