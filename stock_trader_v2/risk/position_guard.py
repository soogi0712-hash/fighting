"""
risk/position_guard.py — 포지션 리스크 관리 (V2)
==================================================
역할:
  1. 오버나이트 금지 — 15:10 기준 수익률 < +1.0% → 청산 검토
                     — 15:20 기준 무조건 전량 청산
  2. 돌파봉 저가 이탈 손절 (최소 손절폭 0.8% 플로어 적용)
  3. WEAK_ENTRY_EXIT — 5분 이내 max_pct<+0.3% AND 현재손익<=-0.7% → 즉시 청산
  4. TIME_EXIT — 횡보 종목 조기 청산
       20분+현재수익률<+0.5% → 청산
       40분+현재수익률<+1.0% → 청산
       ※ max_pct>=+1.0% 종목은 PROFIT_PROTECT 우선
  5. PROFIT_PROTECT — 수익 보호 트레일링
       활성: (max_pct>=+0.45% OR 평가수익>=1,000원) AND 현재평가손익>0
       발동: HWM 대비 40% 이상 반납 시 즉시 청산
  6. 돌파 실패 조기 청산 (조건 2개 이상)
  7. 최종 손절선 -5% (에어백)
  8. 15:20 이후 미체결 BUY 전량 취소

변경 이력:
  - 2026-06-18: 손절 플로어 0.8% / WEAK_ENTRY_EXIT / TIME_EXIT /
                PROFIT_PROTECT / TRADE_REVIEW max_pct 기록 추가
  - 2026-06-19: PROFIT_PROTECT 활성 조건 완화
                (HWM>=1.0% AND 2000원) → (HWM>=0.45% OR 1000원) AND cur_pnl>0
"""

from datetime import time as dtime, datetime
import pytz
from utils.v2_logger import get_logger

logger = get_logger("PositionGuard")
KST    = pytz.timezone("Asia/Seoul")

# ── 시간 기준 ────────────────────────────────────────────────────
T_OVERNIGHT_CHECK  = dtime(15, 10)   # 오버나이트 검토 시작
T_FORCE_CLOSE      = dtime(15, 20)   # 전량 강제 청산
T_CANCEL_BUY       = dtime(15, 20)   # 미체결 매수 취소

# ── 손절 파라미터 ────────────────────────────────────────────────
STOPLOSS_HARD_PCT    = -5.0    # 최종 에어백 -5%
STOPLOSS_SOFT_PCT    = -1.5    # 돌파 실패 청산 -1.5%
OVERNIGHT_PROFIT_MIN = 1.0     # 오버나이트 검토 기준 수익률 +1.0%

# ── 손절 플로어 ──────────────────────────────────────────────────
STOP_FLOOR_PCT       = 0.8     # 돌파봉저가 손절선 최소 폭 (진입가 대비 -0.8%)

# ── WEAK_ENTRY_EXIT 파라미터 ─────────────────────────────────────
WEAK_ENTRY_MAX_MIN   = 5.0     # 진입 후 경과 시간 상한 (분)
WEAK_ENTRY_MAX_PCT   = 0.3     # HWM 상한 (% 미만)
WEAK_ENTRY_CUT_PCT   = -0.7    # 현재손익 기준 (% 이하)

# ── TIME_EXIT 파라미터 ───────────────────────────────────────────
TIME_EXIT_20MIN_PCT  = 0.5     # 20분 경과 후 수익률 기준
TIME_EXIT_40MIN_PCT  = 1.0     # 40분 경과 후 수익률 기준
TIME_EXIT_SKIP_HWM   = 0.45    # 이 HWM 이상이면 TIME_EXIT 건너뜀 (PROFIT_PROTECT 우선) ← 2026-06-19 (1.0→0.45)

# ── PROFIT_PROTECT 파라미터 ──────────────────────────────────────
PROFIT_PROTECT_HWM_PCT   = 0.45    # 활성 HWM 기준 (%) ← 2026-06-19 완화 (1.0→0.45)
PROFIT_PROTECT_MIN_KRW   = 1000    # 활성 평가수익 기준 (원) ← 2026-06-19 완화 (2000→1000)
PROFIT_PROTECT_PULLBACK  = 0.40    # HWM 대비 40% 반납 시 청산

# ── 익절 파라미터 ────────────────────────────────────────────────
TAKE_PROFIT_1   = 1.5    # +1.5% 익절 검토
TAKE_PROFIT_2   = 2.0    # +2.0% 전량 익절
TAKE_PROFIT_MAX = 2.5    # +2.5% 무조건 전량 익절


class PositionGuard:
    """단일 포지션에 대한 리스크 판단 엔진."""

    def __init__(self,
                 code:       str,
                 name:       str,
                 avg_price:  float,
                 qty:        int,
                 entry_time: datetime,
                 breakout_low: float = 0.0):
        """
        breakout_low: 진입 시점 돌파봉 저가 (0이면 사용 안 함).
        손절 플로어: max(breakout_low, avg_price * (1 - STOP_FLOOR_PCT/100))
        """
        self.code         = code
        self.name         = name
        self.avg_price    = avg_price
        self.qty          = qty
        self.entry_time   = entry_time
        self.breakout_low = breakout_low

        # ── 손절선 결정 (플로어 적용) ──────────────────────────
        # breakout_low가 있으면 max(breakout_low, floor_price) 로 실제 손절선 결정
        # → 손절선이 진입가 대비 0.8%보다 좁으면 0.8% 플로어로 상향
        if avg_price > 0 and breakout_low > 0:
            floor_price = avg_price * (1.0 - STOP_FLOOR_PCT / 100.0)
            self.effective_stop = max(breakout_low, floor_price)
        elif breakout_low > 0:
            self.effective_stop = breakout_low
        else:
            self.effective_stop = 0.0

        # 플로어 발동 여부 로그
        if breakout_low > 0 and avg_price > 0:
            floor_price = avg_price * (1.0 - STOP_FLOOR_PCT / 100.0)
            if breakout_low < floor_price:
                logger.info(
                    f"[STOP_FLOOR] {name}({code}) "
                    f"돌파저가={breakout_low:,.0f}원 < 플로어={floor_price:,.0f}원 "
                    f"(진입가 -{STOP_FLOOR_PCT}%) → 손절선={self.effective_stop:,.0f}원으로 상향"
                )

        # ── HWM (최고수익률) 내부 추적 ─────────────────────────
        # PROFIT_PROTECT / WEAK_ENTRY_EXIT / TIME_EXIT 에서 사용
        self._hwm_pct: float = 0.0          # 지금까지 최고 net_pct
        self._profit_protect_active: bool = False  # PROFIT_PROTECT 활성 여부

    # ── 수익률 계산 ──────────────────────────────────────────────

    def net_pct(self, cur_price: float) -> float:
        """수수료 포함 실질 수익률 (0.05% 거래세 + 0.015% 수수료 각방향)."""
        if self.avg_price <= 0:
            return 0.0
        gross = (cur_price - self.avg_price) / self.avg_price * 100
        fee   = 0.015 * 2 + 0.20   # 수수료 양방향 + 거래세
        return gross - fee

    def unrealized_krw(self, cur_price: float) -> float:
        """평가수익금 (원, 수수료 제외 gross 기준으로 단순 계산)."""
        if self.avg_price <= 0 or self.qty <= 0:
            return 0.0
        return (cur_price - self.avg_price) * self.qty

    # ── HWM 업데이트 ─────────────────────────────────────────────

    def update_hwm(self, pct: float) -> None:
        """매 루프마다 호출해 최고수익률을 갱신."""
        if pct > self._hwm_pct:
            self._hwm_pct = pct

    # ── 메인 판단 로직 ───────────────────────────────────────────

    def evaluate(self,
                 cur_price: float,
                 indicators: dict,
                 now_kst: datetime = None) -> dict:
        """
        현재가 + 지표를 받아 액션을 결정.

        indicators 키:
          vwap_above:   bool — 현재가 VWAP 위
          vwap_5m_above: bool — 5분봉 종가 VWAP 위
          sell_score:   int  — SELL 점수
          vol_increase: bool — 거래량 증가
          rsi_falling:  bool — RSI 하락
          elapsed_min:  float — 보유 경과 시간(분)

        Returns:
            {"action": "HOLD"|"SELL_TAKE"|"SELL_STOP"|"SELL_FORCE",
             "reason": str, "pct": float}
        """
        if now_kst is None:
            now_kst = datetime.now(KST)

        pct  = self.net_pct(cur_price)
        t    = now_kst.time()
        code = self.code
        name = self.name

        # HWM 실시간 갱신
        self.update_hwm(pct)
        hwm = self._hwm_pct

        elapsed_min = indicators.get("elapsed_min", 0.0)
        result = {"action": "HOLD", "reason": "", "pct": pct}

        # ── 1. 최종 에어백 손절 (-5%) ──────────────────────────
        if pct <= STOPLOSS_HARD_PCT:
            reason = f"에어백손절 net={pct:.2f}% ≤ {STOPLOSS_HARD_PCT}%"
            logger.warning(f"[PositionGuard] {name}({code}) {reason}")
            return {"action": "SELL_STOP", "reason": reason, "pct": pct}

        # ── 2. 돌파봉 저가 이탈 (플로어 적용) ──────────────────
        if self.effective_stop > 0 and cur_price < self.effective_stop:
            # 플로어 발동 여부 표시
            floor_price = self.avg_price * (1.0 - STOP_FLOOR_PCT / 100.0) if self.avg_price > 0 else 0
            if self.breakout_low > 0 and self.effective_stop > self.breakout_low:
                # 플로어가 원래 돌파저가보다 높아서 발동됨
                reason = (
                    f"돌파봉저가이탈[플로어] {cur_price:,}원 < 손절선={self.effective_stop:,.0f}원 "
                    f"(원돌파저가={self.breakout_low:,.0f}→플로어{STOP_FLOOR_PCT}% 적용) | "
                    f"net={pct:.2f}%"
                )
            else:
                reason = (
                    f"돌파봉저가이탈 {cur_price:,}원 < {self.effective_stop:,.0f}원 | "
                    f"net={pct:.2f}%"
                )
            logger.warning(f"[PositionGuard] {name}({code}) {reason}")
            return {"action": "SELL_STOP", "reason": reason, "pct": pct}

        # ── 3. 15:20 강제 청산 ──────────────────────────────────
        if t >= T_FORCE_CLOSE:
            reason = f"15:20 강제청산 | net={pct:.2f}%"
            logger.info(f"[PositionGuard] {name}({code}) {reason}")
            return {"action": "SELL_FORCE", "reason": reason, "pct": pct}

        # ── 4. 오버나이트 검토 (15:10 이후) ────────────────────
        if t >= T_OVERNIGHT_CHECK and pct < OVERNIGHT_PROFIT_MIN:
            reason = (
                f"오버나이트방지 15:10후 "
                f"net={pct:.2f}% < +{OVERNIGHT_PROFIT_MIN}% | 청산"
            )
            logger.info(f"[PositionGuard] {name}({code}) {reason}")
            return {"action": "SELL_FORCE", "reason": reason, "pct": pct}

        # ── 5. WEAK_ENTRY_EXIT ───────────────────────────────────
        # 진입 5분 이내 + HWM < +0.3% + 현재손익 <= -0.7% → 즉시 청산
        if (elapsed_min <= WEAK_ENTRY_MAX_MIN
                and hwm < WEAK_ENTRY_MAX_PCT
                and pct <= WEAK_ENTRY_CUT_PCT):
            reason = (
                f"[WEAK_ENTRY_EXIT] {elapsed_min:.1f}분경과 | "
                f"HWM={hwm:+.2f}%(<{WEAK_ENTRY_MAX_PCT}%) | "
                f"net={pct:+.2f}%(≤{WEAK_ENTRY_CUT_PCT}%)"
            )
            logger.warning(f"[PositionGuard] {name}({code}) {reason}")
            return {"action": "SELL_STOP", "reason": reason, "pct": pct}

        # ── 6. +2.5% 무조건 전량 익절 ──────────────────────────
        if pct >= TAKE_PROFIT_MAX:
            reason = f"전량익절 net={pct:.2f}% ≥ +{TAKE_PROFIT_MAX}%"
            logger.info(f"[PositionGuard] {name}({code}) {reason}")
            return {"action": "SELL_TAKE", "reason": reason, "pct": pct}

        # ── 7. +2.0% 전량 익절 ────────────────────────────────
        if pct >= TAKE_PROFIT_2:
            reason = f"전량익절 net={pct:.2f}% ≥ +{TAKE_PROFIT_2}%"
            logger.info(f"[PositionGuard] {name}({code}) {reason}")
            return {"action": "SELL_TAKE", "reason": reason, "pct": pct}

        # ── 8. +1.5% SELL_SCORE 연동 익절 ─────────────────────
        sell_score = indicators.get("sell_score", 0)
        if pct >= TAKE_PROFIT_1 and sell_score >= 5:
            reason = (
                f"익절+SELL신호 net={pct:.2f}% ≥ +{TAKE_PROFIT_1}% "
                f"SELL_SCORE={sell_score}"
            )
            logger.info(f"[PositionGuard] {name}({code}) {reason}")
            return {"action": "SELL_TAKE", "reason": reason, "pct": pct}

        # ── 9. PROFIT_PROTECT ────────────────────────────────────
        # 활성 조건: (HWM >= +0.45% OR 평가수익 >= 1,000원) AND 현재평가손익 > 0
        # 발동 조건: 현재가가 HWM 대비 40% 이상 반납
        unrealized = self.unrealized_krw(cur_price)
        if (hwm >= PROFIT_PROTECT_HWM_PCT or unrealized >= PROFIT_PROTECT_MIN_KRW) \
                and unrealized > 0:
            if not self._profit_protect_active:
                self._profit_protect_active = True
                logger.info(
                    f"[PROFIT_PROTECT_ACTIVE] {name}({code}) "
                    f"HWM={hwm:+.2f}% | 평가수익={unrealized:+,.0f}원 → 수익보호 활성"
                )
            # HWM 대비 반납률 계산: (hwm - pct) / hwm
            # hwm > 0 보장됨 (활성 조건에서 이미 체크)
            pullback_ratio = (hwm - pct) / hwm if hwm > 0 else 0
            if pullback_ratio >= PROFIT_PROTECT_PULLBACK:
                reason = (
                    f"[PROFIT_PROTECT] HWM={hwm:+.2f}% | "
                    f"현재={pct:+.2f}% | "
                    f"반납={pullback_ratio*100:.0f}%(≥{PROFIT_PROTECT_PULLBACK*100:.0f}%) | "
                    f"평가수익={unrealized:+,.0f}원"
                )
                logger.info(f"[PositionGuard] {name}({code}) {reason}")
                return {"action": "SELL_TAKE", "reason": reason, "pct": pct}

        # ── 10. TIME_EXIT ─────────────────────────────────────────
        # max_pct >= +1.0% 종목은 PROFIT_PROTECT가 담당 → 건너뜀
        if hwm < TIME_EXIT_SKIP_HWM:
            if elapsed_min >= 40 and pct < TIME_EXIT_40MIN_PCT:
                reason = (
                    f"[TIME_EXIT] {elapsed_min:.0f}분경과 | "
                    f"net={pct:+.2f}%(<+{TIME_EXIT_40MIN_PCT}%) | "
                    f"HWM={hwm:+.2f}% | 횡보청산"
                )
                logger.info(f"[PositionGuard] {name}({code}) {reason}")
                return {"action": "SELL_FORCE", "reason": reason, "pct": pct}
            elif elapsed_min >= 20 and pct < TIME_EXIT_20MIN_PCT:
                reason = (
                    f"[TIME_EXIT] {elapsed_min:.0f}분경과 | "
                    f"net={pct:+.2f}%(<+{TIME_EXIT_20MIN_PCT}%) | "
                    f"HWM={hwm:+.2f}% | 횡보청산"
                )
                logger.info(f"[PositionGuard] {name}({code}) {reason}")
                return {"action": "SELL_FORCE", "reason": reason, "pct": pct}

        # ── 11. 돌파 실패 조기 청산 ───────────────────────────
        #   조건 2개 이상 → SELL_STOP (단, -1.5% 이하일 때만)
        if pct <= STOPLOSS_SOFT_PCT:
            fail_signals = self._count_fail_signals(indicators)
            if fail_signals >= 2:
                reason = (
                    f"돌파실패({fail_signals}개조건) net={pct:.2f}% "
                    f"≤ {STOPLOSS_SOFT_PCT}%"
                )
                logger.warning(f"[PositionGuard] {name}({code}) {reason}")
                return {"action": "SELL_STOP", "reason": reason, "pct": pct}

        return result

    def _count_fail_signals(self, iv: dict) -> int:
        """
        돌파 실패 신호 카운트 (7가지 체크 → 2개 이상이면 청산).
        ★ B8 수정: KR(vwap_above/vol_increase) + US(above_vwap/vol_ok) 키 모두 수용
        """
        count = 0
        # VWAP 이탈: KR=vwap_above, US=above_vwap
        vwap_ok = iv.get("vwap_above", iv.get("above_vwap", True))
        if not vwap_ok:                           count += 1   # VWAP 이탈
        # 5분봉 VWAP: KR만 존재, US는 없으면 True(패스)
        if not iv.get("vwap_5m_above", True):     count += 1   # 5분봉 VWAP 아래
        if iv.get("sell_score", 0) >= 5:          count += 1   # SELL_SCORE ≥ 5
        # 거래량: KR=vol_increase, US=vol_ok
        vol_ok = iv.get("vol_increase", iv.get("vol_ok", True))
        if not vol_ok:                            count += 1   # 거래량 감소
        if iv.get("rsi_falling",  False):         count += 1   # RSI 하락
        elapsed = iv.get("elapsed_min", 0)
        pct     = iv.get("current_pct", 0)
        if elapsed >= 20 and pct < 0.5:           count += 1   # 20분 +0.5% 미달
        if elapsed >= 40 and pct < 1.0:           count += 1   # 40분 +1.0% 미달
        return count
