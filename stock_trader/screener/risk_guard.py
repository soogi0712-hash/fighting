"""
계좌 리스크 관리 엔진
======================
- 신용·미수·레버리지 금지 (현금 내 매수)
- 종목당 최대 10%
- 업종당 최대 25%
- 동일 업종 최대 3종목
- 하루 손실 -3% → 신규매수 중단
- 계좌 전체 손실 -15% → 자동매매 정지
"""

from utils.logger import get_logger

logger = get_logger("RiskGuard")

# ── 한도 설정 ────────────────────────────────────────────────
MAX_STOCK_WEIGHT  = 10.0    # 종목당 최대 비중(%)
MAX_SECTOR_WEIGHT = 25.0    # 업종당 최대 비중(%)
MAX_SECTOR_COUNT  =  3      # 동일 업종 최대 종목 수
DAILY_LOSS_LIMIT  = -3.0    # 하루 손실 한도(%)
TOTAL_LOSS_LIMIT  = -15.0   # 전체 계좌 손실 한도(%)


class RiskGuard:
    """
    매수 전 리스크 검사 게이트키퍼.
    모든 매수 결정 전에 반드시 check_buy() 를 호출해야 한다.
    """

    def check_buy(self, code: str, name: str, sector: str,
                  invest_amount: float, account: dict) -> tuple[bool, str]:
        """
        매수 리스크 종합 검사
        account: {
            cash, total_assets, total_invest,
            daily_pnl, daily_pnl_pct,
            total_pnl_pct,
            positions: [{code, sector, weight_pct, cur_value}],
        }
        returns (ok, reason)
        """
        cash          = account.get("cash", 0)
        total_assets  = account.get("total_assets", 1)
        daily_pnl_pct = account.get("daily_pnl_pct", 0)
        total_pnl_pct = account.get("total_pnl_pct", 0)
        positions     = account.get("positions", [])

        # 1. 현금 초과 금지 (무차입)
        if invest_amount > cash:
            return False, f"현금부족(필요:{invest_amount:,.0f}원/보유:{cash:,.0f}원)"

        # 2. 하루 손실 한도
        if daily_pnl_pct <= DAILY_LOSS_LIMIT:
            return False, f"일손실한도초과({daily_pnl_pct:.1f}%≤{DAILY_LOSS_LIMIT}%)"

        # 3. 전체 계좌 손실 한도
        if total_pnl_pct <= TOTAL_LOSS_LIMIT:
            return False, f"계좌손실한도초과({total_pnl_pct:.1f}%≤{TOTAL_LOSS_LIMIT}%)"

        # 4. 종목당 최대 비중 10%
        stock_weight = invest_amount / total_assets * 100
        if stock_weight > MAX_STOCK_WEIGHT:
            return False, f"종목비중초과({stock_weight:.1f}%>{MAX_STOCK_WEIGHT}%)"

        # 5. 업종당 최대 비중 25%
        sector_val = sum(
            p.get("cur_value", 0)
            for p in positions
            if p.get("sector") == sector
        )
        sector_weight = (sector_val + invest_amount) / total_assets * 100
        if sector_weight > MAX_SECTOR_WEIGHT:
            return False, f"업종비중초과({sector} {sector_weight:.1f}%>{MAX_SECTOR_WEIGHT}%)"

        # 6. 동일 업종 최대 3종목
        sector_count = sum(1 for p in positions if p.get("sector") == sector)
        if sector_count >= MAX_SECTOR_COUNT:
            return False, f"동일업종종목수초과({sector} {sector_count}종목)"

        logger.info(f"✅ 리스크 통과: {name}({code}) {invest_amount:,.0f}원 업종={sector}")
        return True, "통과"

    def get_account_status(self, account: dict) -> dict:
        """
        현재 계좌 리스크 상태 요약
        """
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

        # 투자 가능 여부
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
            "can_buy":       can_buy,
            "stop_reason":   stop_reason,
            "cash":          cash,
            "total_assets":  total_assets,
            "cash_ratio":    round(cash / total_assets * 100, 1),
            "daily_pnl_pct": round(daily_pct, 2),
            "total_pnl_pct": round(total_pct, 2),
            "sector_weights":sector_weights,
            "position_count":len(positions),
            "limits": {
                "max_stock_pct":  MAX_STOCK_WEIGHT,
                "max_sector_pct": MAX_SECTOR_WEIGHT,
                "max_sector_cnt": MAX_SECTOR_COUNT,
                "daily_limit":    DAILY_LOSS_LIMIT,
                "total_limit":    TOTAL_LOSS_LIMIT,
            }
        }

    def is_trading_halted(self, account: dict) -> tuple[bool, str]:
        """자동매매 완전 정지 여부"""
        total_pct = account.get("total_pnl_pct", 0)
        if total_pct <= TOTAL_LOSS_LIMIT:
            return True, f"계좌손실 {total_pct:.1f}% — 자동매매 정지"
        return False, ""

    def max_buy_amount(self, code: str, sector: str, account: dict) -> float:
        """
        해당 종목에 투자 가능한 최대 금액 계산
        (종목 비중 10% & 업종 비중 25% 중 작은 값)
        """
        total_assets = account.get("total_assets", 0)
        cash         = account.get("cash", 0)
        positions    = account.get("positions", [])

        # 종목 한도
        stock_max = total_assets * MAX_STOCK_WEIGHT / 100

        # 업종 잔여 한도
        sector_used = sum(
            p.get("cur_value", 0)
            for p in positions
            if p.get("sector") == sector
        )
        sector_max = total_assets * MAX_SECTOR_WEIGHT / 100 - sector_used

        # 현금 한도
        return min(stock_max, sector_max, cash)
