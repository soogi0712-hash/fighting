"""
계좌 리스크 관리 엔진
======================

★ 장기 복리수익률 극대화 설계 철학 ★

  1. 계좌 생존 (최우선)
     - 일일 손실 -3% → 신규매수 즉시 중단
     - 전체 계좌 손실 -15% → 자동매매 완전 정지
     - 신용·미수·레버리지 절대 금지 (현금 내 매수만)

  2. 강한 종목 집중 (동적 비중 상한)
     - AI 점수 < 80: 종목당 최대 10% (분산 기본)
     - AI 점수 ≥ 80: 종목당 최대 20% (고점수 집중)
     - AI 점수 ≥ 90 + RS ≥ +5%: 종목당 최대 25% (엘리트 집중)
     ※ 업종 한도·전체 손실 한도는 여전히 적용

  3. 업종 분산 유지 (꼬리 리스크 차단)
     - 업종당 최대 25% (집중 종목이어도 업종 한도 준수)
     - 동일 업종 최대 3종목

  ★ calc_dynamic_weight()는 trade_decision.py 와 동일 로직 사용.
     단일 진실 원천 원칙에 따라 trade_decision 에서 직접 import.
"""

from utils.logger import get_logger
from screener.transaction_cost import net_profit_pct_from_cost
from screener.trade_decision import calc_dynamic_weight

logger = get_logger("RiskGuard")

# ── 고정 한도 설정 ──────────────────────────────────────────
# 이 값들은 '계좌 생존' 규칙이므로 동적 조정 불가
MAX_SECTOR_WEIGHT = 25.0    # 업종당 최대 비중 (%)   — 꼬리 리스크 차단
MAX_SECTOR_COUNT  =  3      # 동일 업종 최대 종목 수
DAILY_LOSS_LIMIT  = -3.0    # 하루 손실 한도 (%)     — 계좌 생존 #1
TOTAL_LOSS_LIMIT  = -15.0   # 전체 계좌 손실 한도 (%) — 계좌 생존 #2

# 하위 호환용: 기본 종목 비중 상한 (동적 계산 미적용 시 폴백)
MAX_STOCK_WEIGHT  = 10.0    # 기본 종목당 최대 비중 (%)


class RiskGuard:
    """
    매수 전 리스크 검사 게이트키퍼.
    모든 매수 결정 전에 반드시 check_buy() 를 호출해야 한다.

    check_buy()는 AI 점수·RS에 따라 동적 종목 비중 상한을 계산한다.
    강한 종목에는 더 많은 자본을 허용하되, 업종·계좌 손실 한도는 유지.
    """

    def check_buy(self, code: str, name: str, sector: str,
                  invest_amount: float, account: dict,
                  total_score: float = 0.0,
                  rs_value: float = 0.0) -> tuple[bool, str]:
        """
        매수 리스크 종합 검사.

        ★ total_score, rs_value 를 받아 동적 종목 비중 상한 적용.
          - AI 점수 ≥ 90 + RS ≥ +5% → 최대 25%
          - AI 점수 ≥ 80            → 최대 20%
          - 그 외                   → 최대 10%

        Args:
            code:          종목 코드
            name:          종목명
            sector:        업종
            invest_amount: 투자 예정 금액 (원)
            account: {
                cash, total_assets, total_invest,
                daily_pnl, daily_pnl_pct,
                total_pnl_pct,
                positions: [{code, sector, weight_pct, cur_value}],
            }
            total_score:   AIScorer 총점 (0~100)
            rs_value:      상대강도 (%)

        Returns:
            (ok: bool, reason: str)
        """
        cash          = account.get("cash", 0)
        total_assets  = account.get("total_assets", 1)
        daily_pnl_pct = account.get("daily_pnl_pct", 0)
        total_pnl_pct = account.get("total_pnl_pct", 0)
        positions     = account.get("positions", [])

        # ── 계좌 생존 규칙 (절대 기준, 예외 없음) ────────────
        # 1. 현금 초과 금지 (무차입 원칙)
        if invest_amount > cash:
            return False, (f"현금부족(필요:{invest_amount:,.0f}원"
                           f"/보유:{cash:,.0f}원)")

        # 2. 하루 손실 한도
        if daily_pnl_pct <= DAILY_LOSS_LIMIT:
            return False, (f"일손실한도초과"
                           f"({daily_pnl_pct:.1f}%≤{DAILY_LOSS_LIMIT}%)")

        # 3. 전체 계좌 손실 한도
        if total_pnl_pct <= TOTAL_LOSS_LIMIT:
            return False, (f"계좌손실한도초과"
                           f"({total_pnl_pct:.1f}%≤{TOTAL_LOSS_LIMIT}%)")

        # ── 강한 종목 집중 허용 (동적 비중 상한) ─────────────
        # 4. 종목당 동적 비중 상한
        dynamic_max_weight = calc_dynamic_weight(total_score, rs_value)
        stock_weight = invest_amount / total_assets * 100
        if stock_weight > dynamic_max_weight:
            return False, (f"종목비중초과({stock_weight:.1f}%"
                           f">{dynamic_max_weight:.0f}%"
                           f" AI={total_score:.0f}pts RS={rs_value:+.1f}%)")

        # ── 꼬리 리스크 차단 (업종 분산) ─────────────────────
        # 5. 업종당 최대 비중 25% (집중 종목이어도 업종 한도 유지)
        sector_val = sum(
            p.get("cur_value", 0)
            for p in positions
            if p.get("sector") == sector
        )
        sector_weight = (sector_val + invest_amount) / total_assets * 100
        if sector_weight > MAX_SECTOR_WEIGHT:
            return False, (f"업종비중초과({sector} "
                           f"{sector_weight:.1f}%>{MAX_SECTOR_WEIGHT}%)")

        # 6. 동일 업종 최대 3종목
        sector_count = sum(
            1 for p in positions if p.get("sector") == sector
        )
        if sector_count >= MAX_SECTOR_COUNT:
            return False, (f"동일업종종목수초과"
                           f"({sector} {sector_count}종목)")

        logger.info(
            f"✅ 리스크 통과: {name}({code}) {invest_amount:,.0f}원 "
            f"업종={sector} 비중={stock_weight:.1f}%/{dynamic_max_weight:.0f}% "
            f"AI={total_score:.0f}pts RS={rs_value:+.1f}%"
        )
        return True, "통과"

    def get_account_status(self, account: dict) -> dict:
        """현재 계좌 리스크 상태 요약"""
        cash         = account.get("cash", 0)
        total_assets = account.get("total_assets", 1)
        daily_pct    = account.get("daily_pnl_pct", 0)
        total_pct    = account.get("total_pnl_pct", 0)
        positions    = account.get("positions", [])

        # 업종별 집중도
        sector_map = {}
        for p in positions:
            sec = p.get("sector", "기타")
            sector_map[sec] = sector_map.get(sec, 0) + p.get("cur_value", 0)
        sector_weights = {
            sec: round(val / total_assets * 100, 1)
            for sec, val in sector_map.items()
        }

        # 종목별 현재 비중 + 동적 한도
        position_weights = []
        for p in positions:
            cv    = p.get("cur_value", 0)
            ai    = p.get("total_score", 0.0)
            rs    = p.get("rs_value", 0.0)
            dyn   = calc_dynamic_weight(ai, rs)
            cur_w = round(cv / total_assets * 100, 1) if total_assets else 0
            position_weights.append({
                "code":        p.get("code"),
                "name":        p.get("name"),
                "cur_weight":  cur_w,
                "max_weight":  dyn,
                "headroom":    round(dyn - cur_w, 1),  # 추가 투자 여력 (%)
                "ai_score":    ai,
                "rs_value":    rs,
            })

        can_buy = (
            daily_pct > DAILY_LOSS_LIMIT and
            total_pct > TOTAL_LOSS_LIMIT
        )
        stop_reason = []
        if daily_pct <= DAILY_LOSS_LIMIT:
            stop_reason.append(f"일손실{daily_pct:.1f}%")
        if total_pct <= TOTAL_LOSS_LIMIT:
            stop_reason.append(f"계좌손실{total_pct:.1f}%")

        return {
            "can_buy":          can_buy,
            "stop_reason":      stop_reason,
            "cash":             cash,
            "total_assets":     total_assets,
            "cash_ratio":       round(cash / total_assets * 100, 1),
            "daily_pnl_pct":    round(daily_pct, 2),
            "total_pnl_pct":    round(total_pct, 2),
            "sector_weights":   sector_weights,
            "position_weights": position_weights,
            "position_count":   len(positions),
            "limits": {
                "max_stock_pct_base":   MAX_STOCK_WEIGHT,   # 기본 10%
                "max_stock_pct_high":   20.0,               # 고점수 20%
                "max_stock_pct_elite":  25.0,               # 엘리트 25%
                "max_sector_pct":       MAX_SECTOR_WEIGHT,
                "max_sector_cnt":       MAX_SECTOR_COUNT,
                "daily_limit":          DAILY_LOSS_LIMIT,
                "total_limit":          TOTAL_LOSS_LIMIT,
            }
        }

    def is_trading_halted(self, account: dict) -> tuple[bool, str]:
        """자동매매 완전 정지 여부"""
        total_pct = account.get("total_pnl_pct", 0)
        if total_pct <= TOTAL_LOSS_LIMIT:
            return True, f"계좌손실 {total_pct:.1f}% — 자동매매 정지"
        return False, ""

    def max_buy_amount(self, code: str, sector: str, account: dict,
                       total_score: float = 0.0,
                       rs_value: float = 0.0) -> float:
        """
        해당 종목에 투자 가능한 최대 금액 계산.
        종목 비중(동적) & 업종 비중 25% & 보유 현금 중 가장 작은 값.

        ★ total_score, rs_value 를 넘기면 동적 상한 적용.
        """
        total_assets = account.get("total_assets", 0)
        cash         = account.get("cash", 0)
        positions    = account.get("positions", [])

        # 동적 종목 한도
        dynamic_weight = calc_dynamic_weight(total_score, rs_value)
        stock_max = total_assets * dynamic_weight / 100

        # 업종 잔여 한도
        sector_used = sum(
            p.get("cur_value", 0)
            for p in positions
            if p.get("sector") == sector
        )
        sector_max = total_assets * MAX_SECTOR_WEIGHT / 100 - sector_used

        return min(stock_max, sector_max, cash)
