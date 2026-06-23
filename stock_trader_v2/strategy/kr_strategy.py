"""
strategy/kr_strategy.py — 국내장 V2 핵심 전략 엔진
====================================================
설계 원칙:
  - 패치 누적 없음: 진입/익절/손절 각 엔진 분리
  - 실계좌 기준 루프 (매 루프 AccountSync.sync())
  - 재진입 차단은 ReentryGuard에 위임
  - 주문 실행은 ExecutionEngine에 위임
  - 포지션 리스크는 PositionGuard에 위임

진입 전략:
  BUY_SCORE ≥ 0.40 → 30% 선진입 (Early Entry)
  BUY_SCORE ≥ 0.55 → +70% 추가 (Full Entry)
  필수: 거래량증가 + VWAP위 + SELL_SCORE < 5

손절 전략 (PositionGuard 위임):
  돌파봉 저가 이탈 → 즉시 청산
  돌파 실패 조건 2개 이상 → 조기 청산
  최종 에어백 -5%

익절 전략:
  +1.5% SELL_SCORE 연동
  +2.0% 전량 익절
  +2.5% 무조건 전량 익절

시간 관리:
  14:30 이후 신규매수 금지
  15:10 이후 수익 < +1% → 청산 검토
  15:20 전량 강제 청산 + 미체결 BUY 취소
"""

import time
import os
import json
from datetime import datetime, time as dtime
from typing import Optional

import numpy as np
import pandas as pd
import pytz

from broker.kr_broker    import KRBroker, ORD_MARKET, ORD_LIMIT
from risk.reentry_guard  import ReentryGuard
from risk.pnl_guard      import DailyPnLGuard
from risk.position_guard import PositionGuard, STOPLOSS_HARD_PCT
from engine.account_sync  import AccountSync
from engine.execution_engine import ExecutionEngine
from utils.v2_logger     import get_logger
from adaptive.trade_recorder import TradeRecorder, classify_signal
from adaptive.weight_adjuster import WeightAdjuster

logger = get_logger("KRStrategy")
KST    = pytz.timezone("Asia/Seoul")

# ── BUY SCORE 임계 ─────────────────────────────────────────────
BUY_SCORE_EARLY = 0.40
BUY_SCORE_FULL  = 0.55

# ── 추격매수 금지 기준 ─────────────────────────────────────────
CHASE_RISE_15M  = 4.0    # 최근 15분 상승률
CHASE_RISE_5M   = 2.0    # 최근 5분 상승률
CHASE_BULL_CNT  = 3      # 연속 양봉 수

# ── 신규매수 마감 ──────────────────────────────────────────────
BUY_STOP_TIME   = dtime(14, 30)
FORCE_CLOSE_TIME = dtime(15, 20)

# ════════════════════════════════════════════════════════════
# ■ 전략 B (BB하단 평균회귀) 상수
# ════════════════════════════════════════════════════════════
_STRAT_B_BB_PROX    = 1.02   # BB하단 근접: cur_price <= bb_lower * 1.02
_STRAT_B_MA20_FLOOR = 0.995  # MA20 하한선: cur_price >= ma20 * 0.995
_STRAT_B_RSI_MIN    = 40.0   # RSI 하한
_STRAT_B_RSI_MAX    = 60.0   # RSI 상한
_STRAT_B_VOL_MULT   = 1.1    # 거래량 배수: cur_vol >= avg20 * 1.1
_STRAT_B_SURGE_PCT  = 5.0    # 급등주 제외: 당일 상승률 >= 5%
_STRAT_B_VOL_SURGE  = 3.0    # 급등주 거래량 기준: cur_vol >= avg20 * 3.0 (급등 의심)


class KRStrategy:
    """
    국내장 단일 종목 전략 실행기.

    사용법:
        strat = KRStrategy(broker, account, reentry, pnl_guard)
        result = strat.run({"code": "035420", "name": "NAVER"})
    """

    def __init__(self,
                 broker:    KRBroker,
                 account:   AccountSync,
                 reentry:   ReentryGuard,
                 pnl:       DailyPnLGuard,
                 recorder:  Optional[TradeRecorder] = None,
                 adjuster:  Optional[WeightAdjuster] = None):
        self.broker    = broker
        self.account   = account
        self.reentry   = reentry
        self.pnl       = pnl
        self.recorder  = recorder   # Adaptive Engine 기록기
        self.adjuster  = adjuster   # Adaptive Engine 가중치 조정기
        self.executor  = ExecutionEngine(broker, account, reentry)

        # 포지션 상태 {code: PositionGuard}
        self._positions: dict[str, PositionGuard] = {}
        # 진입단계 추적 {code: "EARLY"|"FULL"}
        self._entry_stage: dict[str, str] = {}

        # 내부 포지션 영속화 파일
        _DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
        self._pos_file = os.path.join(_DATA_DIR, "v2_kr_positions.json")
        os.makedirs(_DATA_DIR, exist_ok=True)
        self._load_positions()

    # ════════════════════════════════════════════════════════════
    # 메인 루프 진입점
    # ════════════════════════════════════════════════════════════

    def run(self, stock: dict) -> dict:
        """
        단일 종목 매매 판단 + 실행.

        Args:
            stock: {"code": str, "name": str}
        Returns:
            {"action": str, "code": str, "name": str, ...}
        """
        code = stock["code"]
        name = stock.get("name", code)
        now  = datetime.now(KST)
        t    = now.time()

        # ── 세션 체크 ────────────────────────────────────────
        if now.weekday() >= 5:
            return self._skip(code, name, "주말 휴장")

        # ── 시세 조회 ────────────────────────────────────────
        price_data = self.broker.get_price(code)
        if not price_data or price_data.get("price", 0) <= 0:
            return self._skip(code, name, "시세 조회 실패")

        cur_price = price_data["price"]

        # ── 5분봉 조회 ────────────────────────────────────────
        candles_5m = self.broker.get_5min_candles(code, count=20)

        # ── 현재 포지션 확인 (실계좌 기준) ──────────────────
        holdings   = self.account.holdings
        held_item  = next((h for h in holdings if h["code"] == code), None)
        held_qty   = held_item["qty"]        if held_item else 0
        avg_price  = held_item["avg_price"]  if held_item else 0

        # ── 포지션 동기화 검증 ────────────────────────────────
        pg = self._positions.get(code)
        if pg and held_qty == 0:
            # 고스트 포지션 감지 → 내부 상태 제거
            logger.warning(
                f"[동기화] 고스트 포지션 감지: {name}({code}) "
                f"내부 보유 but 실계좌=0주 → 내부 제거"
            )
            self._positions.pop(code, None)
            self._entry_stage.pop(code, None)
            pg = None

        # ════════════════════════════════════════════════════
        # ■ 보유 중 — 익절/손절/시간청산 판단
        # ════════════════════════════════════════════════════
        if held_qty > 0:
            return self._eval_exit(
                code, name, cur_price, held_qty, avg_price,
                candles_5m, price_data, pg, now
            )

        # ════════════════════════════════════════════════════
        # ■ 미보유 — 신규진입 판단
        # ════════════════════════════════════════════════════
        # 14:30 이후 신규매수 금지
        if t >= BUY_STOP_TIME:
            return self._skip(code, name, f"14:30 이후 신규매수 금지 ({t.strftime('%H:%M')})")

        # PnL 한도 체크
        if not self.pnl.can_buy:
            return self._skip(code, name, f"일일수익잠금: {self.pnl.block_reason()}")

        # ★ [시간대별 신호 스캔 로그] — 전략 A/B 동시 평가
        logger.debug(
            f"[SIGNAL_SCAN] {name}({code}) | KST={t.strftime('%H:%M')} | "
            f"A/B 전략 평가 시작"
        )

        # ── 전략 A 평가 (기존 모멘텀 돌파) ──────────────────
        result_a = self._eval_entry(
            code, name, cur_price, candles_5m, price_data, now,
            strategy="A"
        )
        if result_a.get("action") == "BUY":
            return result_a

        # ── 전략 B 평가 (BB하단 평균회귀) ───────────────────
        result_b = self._eval_entry_b(
            code, name, cur_price, candles_5m, price_data, now
        )
        if result_b.get("action") == "BUY":
            return result_b

        # 둘 다 통과 못하면 A의 SKIP 반환 (사유 포함)
        return result_a

    # ════════════════════════════════════════════════════════════
    # ■ 진입 판단
    # ════════════════════════════════════════════════════════════

    def _eval_entry(self,
                    code: str, name: str, cur_price: int,
                    candles_5m: list, price_data: dict,
                    now: datetime, strategy: str = "A") -> dict:
        """전략 A (모멘텀 돌파) 진입 판단."""

        # ★ B6 수정: 재진입 차단 체크 (이전 누락)
        blocked, block_info = self.reentry.check("KR", code, name)
        if blocked:
            return self._skip(code, name,
                               f"⛔ 재진입 차단 — {block_info.get('block_reason', '')}")

        # ── ★ 수량0 사전 제외: BUY 판정 전에 확인 ──────────────
        entry_pre = self.account.calc_entry_amount(cur_price)
        if not entry_pre["can_enter"] or entry_pre.get("max_qty", 0) <= 0:
            pre_reason = entry_pre.get("block_reason", "수량0 예상")
            logger.info(
                f"[ENTRY_EXCLUDE] 종목={name}({code}) | 시장=KR | strategy={strategy} | "
                f"현재가={cur_price:,}원 | "
                f"배정금액={entry_pre.get('entry_amount_krw', 0):,.0f}원 | "
                f"사유={pre_reason}"
            )
            return self._skip(code, name, pre_reason)

        # ── 지표 계산 ────────────────────────────────────────
        iv  = self._calc_indicators(candles_5m, price_data)
        buy_score  = iv["buy_score"]
        sell_score = iv["sell_score"]

        # ── 항상 로그 ────────────────────────────────────────
        logger.info(
            f"[진입평가-A] {name}({code}) | strategy={strategy} | "
            f"현재가={cur_price:,} | "
            f"BUY={buy_score:.2f} | SELL={sell_score} | "
            f"RSI={iv.get('rsi', 0):.0f} | "
            f"거래량증가={iv['vol_increase']} | "
            f"VWAP위={iv['vwap_above']} | "
            f"추격={iv['chase_blocked']}"
        )

        # ── 필수 조건 체크 ───────────────────────────────────
        if iv["chase_blocked"]:
            return self._skip(code, name,
                               f"추격매수차단({iv['chase_reason']})")

        if not iv["vol_increase"]:
            return self._skip(code, name, "거래량 증가 없음")

        if not iv["vwap_above"]:
            return self._skip(code, name, "VWAP 아래")

        if sell_score >= 5:
            return self._skip(code, name,
                               f"SELL_SCORE={sell_score} ≥ 5 — 진입 금지")

        if buy_score < BUY_SCORE_EARLY:
            return self._skip(code, name,
                               f"BUY_SCORE={buy_score:.2f} < {BUY_SCORE_EARLY}")

        # ── ★ Adaptive Signal Filter + Weight 실전 반영 ──────
        # signal_type 분류 (진입 전 미리 판별)
        signal_type = classify_signal("KR", iv)
        adapted_buy_score = self._apply_adaptive_weight(
            code, name, signal_type, iv, buy_score
        )
        # DISABLED → 진입 금지
        if adapted_buy_score is None:
            return self._skip(code, name,
                               f"[ADAPTIVE] {signal_type} DISABLED — 진입 차단")
        buy_score = adapted_buy_score

        # ── [KR BUY 판정] 로그 — 매 스캔마다 판정 근거 출력 ──
        _vwap_val   = price_data.get("vwap", 0) if price_data else 0
        _vol_inc    = "✅" if iv.get("vol_increase") else "❌"
        _vwap_above = "✅" if iv.get("vwap_above")   else "❌"
        logger.info(
            f"[KR BUY 판정-A] {name}({code}) | strategy={strategy} | "
            f"현재가={cur_price:,.0f}원 | VWAP={_vwap_val:,.0f} | "
            f"RSI={iv.get('rsi', 0):.0f} | "
            f"거래량={_vol_inc} | VWAP위={_vwap_above} | "
            f"SELL={sell_score} | BUY={buy_score:.3f} | "
            f"단계={self._entry_stage.get(code, 'NONE')} | "
            f"진입기준(EARLY≥{BUY_SCORE_EARLY}/FULL≥{BUY_SCORE_FULL})"
        )

        # ── 자금 계산 (수량0 사전 제외 시 이미 통과 — entry_pre 재사용) ──
        entry_info = entry_pre  # 위에서 이미 검증 완료
        max_qty = entry_info["max_qty"]

        # ── 진입 비중 결정 (Early 30% / Full 100%) ──────────
        stage = self._entry_stage.get(code, "")
        if stage == "FULL":
            return self._skip(code, name, "이미 Full Entry 완료")

        if buy_score >= BUY_SCORE_FULL and stage != "EARLY":
            # Full Entry: 100%
            qty    = max_qty
            reason = f"[A]Full진입 BUY_SCORE={buy_score:.2f} signal={signal_type}"
            stage_next = "FULL"
        elif buy_score >= BUY_SCORE_EARLY and not stage:
            # Early Entry: 30%
            qty    = max(1, int(max_qty * 0.30))
            reason = f"[A]Early진입 BUY_SCORE={buy_score:.2f} signal={signal_type}"
            stage_next = "EARLY"
        elif buy_score >= BUY_SCORE_FULL and stage == "EARLY":
            # Early → 나머지 70% 추가
            total_qty   = max_qty
            current_qty = self._get_held_qty_internal(code)
            qty         = max(1, total_qty - current_qty)
            reason      = f"[A]Early→Full 추가진입 BUY_SCORE={buy_score:.2f} signal={signal_type}"
            stage_next  = "FULL"
        else:
            return self._skip(code, name,
                               f"BUY_SCORE={buy_score:.2f} 단계 미충족")

        # ── 지정가 / 시장가 결정 ─────────────────────────────
        ord_price, ord_dvsn = self._decide_order_price(
            cur_price, price_data
        )

        signal_time = now.isoformat()   # ★ 신호 발생 시각 (KST)
        order_time  = datetime.now(KST).isoformat()  # 주문 직전 시각

        result = self.executor.execute_buy(
            code     = code,
            name     = name,
            price    = ord_price,
            qty      = qty,
            reason   = reason,
            ord_dvsn = ord_dvsn,
        )

        if result.get("action") == "BUY":
            self._entry_stage[code] = stage_next
            # PositionGuard 등록 (돌파봉 저가 저장)
            breakout_low = self._get_breakout_low(candles_5m, cur_price)
            self._positions[code] = PositionGuard(
                code         = code,
                name         = name,
                avg_price    = ord_price if ord_price > 0 else float(cur_price),
                qty          = qty,
                entry_time   = now,
                breakout_low = breakout_low,
            )
            self._save_positions()
            # ★ execute_buy 반환값에서 order_no 추출
            order_no = result.get("order_no", "") or ""

            # ★ [BUY_OK] 표준 로그
            entry_price_log = ord_price if ord_price > 0 else cur_price
            logger.info(
                f"[BUY_OK] 종목={name}({code}) | "
                f"시장=KR | strategy={strategy} | "
                f"단계={stage_next} | "
                f"수량={qty}주 | "
                f"진입가={entry_price_log:,}원 | "
                f"돌파저가={breakout_low:,} | "
                f"order_no={order_no} | "
                f"사유={reason}"
            )
            # ★ [ENTRY_QUALITY] 진입 품질 상세 로그
            _pg_new = self._positions.get(code)
            _eff_stop = getattr(_pg_new, 'effective_stop', breakout_low)
            _stop_gap = (_eff_stop - entry_price_log) / entry_price_log * 100 if (entry_price_log > 0 and _eff_stop > 0) else 0
            logger.info(
                f"[ENTRY_QUALITY] 종목={name}({code}) | "
                f"BUY_SCORE={buy_score:.3f} | "
                f"진입단계={stage_next} | strategy={strategy} | "
                f"진입사유={reason} | "
                f"돌파저가={breakout_low:,.0f} | "
                f"유효손절선={_eff_stop:,.0f}(gap={_stop_gap:+.2f}%) | "
                f"VWAP위={iv.get('vwap_above', '?')} | "
                f"거래량증가={iv.get('vol_increase', '?')} | "
                f"RSI={iv.get('rsi', 0):.0f}"
            )
            # ★ [ORDER_NO_TRACE] 로그 — order_no 전달 추적
            logger.info(
                f"[ORDER_NO_TRACE] 종목={name}({code}) | "
                f"주문응답 order_no={order_no!r} | "
                f"signal_time={signal_time[11:19]} | "
                f"order_time={order_time[11:19]}"
            )
            # ★ Adaptive Engine: 진입 기록 (order_no + signal_time + order_time 포함)
            if self.recorder:
                try:
                    iv_with_strategy = dict(iv)
                    iv_with_strategy["strategy"] = strategy   # ★ strategy 태그 주입
                    self.recorder.record_entry(
                        market       = "KR",
                        code         = code,
                        name         = name,
                        price        = float(ord_price if ord_price > 0 else cur_price),
                        qty          = qty,
                        reason       = reason,
                        iv           = iv_with_strategy,
                        stage        = stage_next,
                        signal_time  = signal_time,
                        order_time   = order_time,
                        order_no     = order_no,
                        price_source = "KIS지정가" if ord_price > 0 else "KIS시장가",
                    )
                except Exception as _re:
                    logger.debug(f"[KRStrategy] 진입 기록 실패: {_re}")

        return result

    # ════════════════════════════════════════════════════════════
    # ■ Adaptive Weight 실전 반영 헬퍼
    # ════════════════════════════════════════════════════════════

    def _apply_adaptive_weight(self,
                                code: str, name: str,
                                signal_type: str, iv: dict,
                                base_buy_score: float) -> Optional[float]:
        """
        Adaptive Engine의 학습 가중치를 BUY_SCORE에 실전 반영.

        적용 대상 지표:
          - 거래량폭증 / 거래량증가     → vol_bonus (volume_bonus)
          - 초기돌파 / 강한돌파 / 폭발돌파 → breakout_bonus
          - VWAP / RSI / OBV (signal_bonus)

        안전장치 (절대 변경 금지):
          - 거래시간 / 장마감 / 재진입 / 손실한도 / 수익목표 → 변경 없음

        Returns:
          None  → DISABLED, 진입 차단
          float → 가중치 적용된 BUY_SCORE (LIVE_MIN~LIVE_MAX 범위)
        """
        if self.adjuster is None:
            return base_buy_score  # adjuster 없으면 원본 그대로

        # ① Signal 상태 확인
        allowed, status = self.adjuster.check_signal("KR", signal_type)

        # [ADAPTIVE SIGNAL FILTER] 로그
        filter_result = "ALLOW" if allowed else "BLOCK"
        if allowed and status == "WARNING":
            filter_result = "REDUCE"
        logger.info(
            f"[ADAPTIVE SIGNAL FILTER] "
            f"종목={name}({code}) | 시장=KR | 전략={signal_type} | "
            f"상태={status} | 결과={filter_result}"
        )

        if not allowed:
            return None  # DISABLED → 진입 차단

        # ② 유효 가중치 조회 (0.80~1.20, WARNING이면 ×0.5)
        live_w = self.adjuster.get_effective_weight("KR", signal_type)

        # ③ BUY_SCORE 구성요소별 가중치 적용
        #    base_score = 지표점수 합산 (score 변수 기준)
        #    각 구성요소 점수를 live_w로 보정
        bp = iv.get("breakout_bonus", 0.0)
        vol_surge    = iv.get("vol_surge",    False) if "vol_surge" in iv else (
            iv.get("vol_increase", False)  # fallback
        )
        vol_increase = iv.get("vol_increase", False)

        # 원본 score에서 적용 가능한 구성요소 추출 (정규화 전 기준)
        # 거래량 보너스: vol_surge→3점, vol_increase→2점
        if vol_surge and iv.get("vol_surge", False):
            vol_bonus_raw = 3.0
        elif vol_increase:
            vol_bonus_raw = 2.0
        else:
            vol_bonus_raw = 0.0

        # 돌파 보너스: breakout_bonus * 10
        breakout_bonus_raw = bp * 10.0

        # VWAP/RSI/OBV 시그널 보너스 (나머지 기반 지표 점수)
        # 전체 점수 중 거래량/돌파 제외한 기본 지표 점수
        total_raw = base_buy_score * 14.0  # 역정규화 (max 14점)
        signal_bonus_raw = max(0.0, total_raw - vol_bonus_raw - breakout_bonus_raw)

        # 가중치 적용: 각 보너스에 live_w 곱셈 후 재합산
        adjusted_raw = (
            signal_bonus_raw             # VWAP/RSI/OBV — 기본 지표 그대로
            + vol_bonus_raw      * live_w  # 거래량 보너스 × 학습가중치
            + breakout_bonus_raw * live_w  # 돌파 보너스   × 학습가중치
        )
        adjusted_score = round(min(adjusted_raw / 14.0, 1.0), 3)

        # [ADAPTIVE WEIGHT APPLIED] 로그
        logger.info(
            f"[ADAPTIVE WEIGHT APPLIED] "
            f"종목={name}({code}) | 시장=KR | 전략={signal_type} | "
            f"기본가중치=1.00 | 학습가중치={live_w:.3f} | "
            f"적용전BUY={base_buy_score:.3f} | 적용후BUY={adjusted_score:.3f}"
        )

        return adjusted_score

    # ════════════════════════════════════════════════════════════
    # ■ 보유 중 — 청산 판단
    # ════════════════════════════════════════════════════════════

    def _eval_exit(self,
                   code: str, name: str, cur_price: int,
                   held_qty: int, avg_price: float,
                   candles_5m: list, price_data: dict,
                   pg: Optional[PositionGuard],
                   now: datetime) -> dict:

        # PositionGuard 없으면 복원 시도
        if pg is None:
            # ★ B2/B3 수정: 영속화 파일에서 entry_time / breakout_low 복원 시도
            # 실패 시 fallback으로 now를 사용하되, 경고 로그 출력
            saved = self._load_single_position(code)
            if saved:
                entry_time   = saved.get("entry_time_dt", now)
                breakout_low = saved.get("breakout_low", 0.0)
                logger.info(
                    f"[eval_exit] {name}({code}) PositionGuard 영속화에서 복원 | "
                    f"entry_time={entry_time.strftime('%H:%M')} "
                    f"breakout_low={breakout_low:,.0f}"
                )
            else:
                # 파일에도 없음 — 최대한 보수적으로 처리
                # entry_time을 추정: 당일 09:00 기준 (elapsed 과소평가 방지)
                today_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
                entry_time   = today_open
                breakout_low = 0.0
                logger.warning(
                    f"[eval_exit] {name}({code}) PositionGuard 복원 불가 — "
                    f"entry_time=09:00(추정), breakout_low=0 (돌파저가 손절 비활성)"
                )
            pg = PositionGuard(
                code         = code,
                name         = name,
                avg_price    = avg_price,
                qty          = held_qty,
                entry_time   = entry_time,
                breakout_low = breakout_low,
            )
            self._positions[code] = pg

        iv = self._calc_indicators(candles_5m, price_data)
        iv["current_pct"] = pg.net_pct(cur_price)

        elapsed_min = (now - pg.entry_time).total_seconds() / 60
        iv["elapsed_min"] = elapsed_min

        # PositionGuard 평가
        dec = pg.evaluate(cur_price, iv, now)
        action = dec["action"]
        reason = dec["reason"]
        pct    = dec["pct"]

        logger.info(
            f"[보유평가] {name}({code}) | "
            f"현재가={cur_price:,} | net={pct:+.2f}% | "
            f"action={action} | {reason}"
        )

        if action in ("SELL_TAKE", "SELL_STOP", "SELL_FORCE"):
            is_sl = action == "SELL_STOP"
            is_pe = action == "SELL_TAKE"
            result = self.executor.execute_sell(
                code           = code,
                name           = name,
                qty            = held_qty,
                price          = 0,
                reason         = reason,
                ord_dvsn       = ORD_MARKET,
                is_stoploss    = is_sl,
                is_profit_exit = is_pe,
            )
            if result.get("action") == "SELL":
                # PnL 기록
                profit_amt = result.get("profit_amt", 0)
                self.pnl.record(float(profit_amt))

                # ★ [SELL_OK] 표준 로그
                logger.info(
                    f"[SELL_OK] 종목={name}({code}) | "
                    f"시장=KR | "
                    f"수량={held_qty}주 | "
                    f"평균단가={avg_price:,.0f}원 | "
                    f"매도가={cur_price:,}원 | "
                    f"실현손익={profit_amt:+,.0f}원 | "
                    f"수익률={pct:+.2f}% | "
                    f"사유={reason}"
                )

                # ★ [TRADE_REVIEW] 진입~청산 전체 요약 로그 (max_pct/min_pct 포함)
                entry_time_str = pg.entry_time.strftime("%H:%M:%S") if (pg and pg.entry_time) else "?"
                exit_time_str  = now.strftime("%H:%M:%S")
                hold_min       = (now - pg.entry_time).total_seconds() / 60 if (pg and pg.entry_time) else 0
                _hwm_val = getattr(pg, '_hwm_pct', None)
                _hwm_str = f"{_hwm_val:+.2f}%" if _hwm_val is not None else "N/A"
                logger.info(
                    f"[TRADE_REVIEW] 종목={name}({code}) | "
                    f"시장=KR | "
                    f"매수시각={entry_time_str} | "
                    f"매도시각={exit_time_str} | "
                    f"보유시간={hold_min:.0f}분 | "
                    f"매수가={avg_price:,.0f}원 | "
                    f"매도가={cur_price:,}원 | "
                    f"max_pct={_hwm_str} | "
                    f"min_pct={pct:+.2f}% | "
                    f"수익률={pct:+.2f}% | "
                    f"실현손익={profit_amt:+,.0f}원 | "
                    f"매도사유={reason}"
                )

                # ★ Adaptive Engine: 청산 기록
                if self.recorder:
                    try:
                        exit_pct  = pct  # PositionGuard.evaluate()의 net_pct
                        self.recorder.record_exit(
                            code        = code,
                            exit_price  = float(cur_price),
                            exit_qty    = held_qty,
                            exit_reason = reason,
                            exit_pct    = float(exit_pct),
                            pnl_krw     = float(profit_amt),
                        )
                    except Exception as _re:
                        logger.debug(f"[KRStrategy] 청산 기록 실패: {_re}")
                # 포지션 제거
                self._positions.pop(code, None)
                self._entry_stage.pop(code, None)
                self._save_positions()
            return result

        return {
            "action": "HOLD",
            "code":   code, "name": name,
            "reason": f"HOLD net={pct:+.2f}%",
            "pct":    pct,
        }

    # ════════════════════════════════════════════════════════════
    # ■ 지표 계산 (BUY_SCORE / SELL_SCORE / 거래량 / VWAP)
    # ════════════════════════════════════════════════════════════

    def _calc_indicators(self,
                         candles_5m: list,
                         price_data: dict) -> dict:
        """
        5분봉 + 현재가 데이터로 지표 계산.
        Returns: iv dict
        """
        iv = {
            "buy_score":    0.0,
            "sell_score":   0,
            "vol_increase": False,
            "vwap_above":   False,
            "vwap_5m_above": False,
            "rsi":          0.0,    # ★ RSI 버그수정: iv에 명시적 저장
            "rsi_falling":  False,
            "chase_blocked": False,
            "chase_reason": "",
            "breakout_bonus": 0.0,
            "bb_lower":     0.0,    # ★ 전략 B용
            "bb_upper":     0.0,    # ★ 전략 B용
            "ma20":         0.0,    # ★ 전략 B용
            "avg_vol20":    0.0,    # ★ 전략 B용
        }

        if len(candles_5m) < 4:
            return iv

        closes  = [c["close"]  for c in candles_5m]
        highs   = [c["high"]   for c in candles_5m]
        lows    = [c["low"]    for c in candles_5m]
        volumes = [c["volume"] for c in candles_5m]

        cur_vol  = volumes[-1] if volumes else 0
        prev_vol = volumes[-2] if len(volumes) >= 2 else 0
        avg_vol4 = sum(volumes[-5:-1]) / 4 if len(volumes) >= 5 else (sum(volumes[:-1]) / max(len(volumes)-1, 1))

        cur_close  = closes[-1]  if closes  else 0
        prev_close = closes[-2]  if len(closes) >= 2 else cur_close

        # ── 거래량 판단 ───────────────────────────────────────
        vol_increase = cur_vol > prev_vol * 1.2
        vol_surge    = cur_vol > avg_vol4 * 2.0
        iv["vol_increase"] = vol_increase or vol_surge

        # ── VWAP ─────────────────────────────────────────────
        vwap = price_data.get("vwap", 0)
        cur_price = price_data.get("price", cur_close)
        if vwap <= 0:
            # VWAP 없으면 MA20 대체
            vwap = np.mean(closes[-20:]) if len(closes) >= 20 else np.mean(closes)
        iv["vwap_above"]    = cur_price > vwap
        iv["vwap_5m_above"] = cur_close > vwap

        # ── RSI ───────────────────────────────────────────────
        if len(closes) >= 14:
            rsi = self._calc_rsi(closes, 14)
            iv["rsi"]         = rsi   # ★ RSI 버그수정: iv에 저장
            iv["rsi_falling"] = self._is_rsi_falling(closes)
        else:
            rsi = 50.0   # 봉 수 부족 시 중립값

        # ── 추격매수 차단 ─────────────────────────────────────
        if len(closes) >= 4:
            rise_5m  = (cur_close - closes[-2]) / max(closes[-2], 1) * 100 if closes[-2] else 0
            rise_15m = (cur_close - closes[-4]) / max(closes[-4], 1) * 100 if closes[-4] else 0
            bull_cnt = sum(
                1 for i in range(-3, 0)
                if i < 0 and -i <= len(closes)
                and closes[i] > closes[i-1]
            )
            if rise_15m >= CHASE_RISE_15M:
                iv["chase_blocked"] = True
                iv["chase_reason"]  = f"15분상승={rise_15m:.1f}%"
            elif rise_5m >= CHASE_RISE_5M:
                iv["chase_blocked"] = True
                iv["chase_reason"]  = f"5분상승={rise_5m:.1f}%"
            elif bull_cnt >= CHASE_BULL_CNT:
                iv["chase_blocked"] = True
                iv["chase_reason"]  = f"연속양봉={bull_cnt}개"

        # ── BUY SCORE 계산 ────────────────────────────────────
        score = 0.0

        # MA 배열 (MA5 > MA20)
        ma20 = 0.0
        bb_lower = 0.0
        bb_upper = 0.0
        if len(closes) >= 20:
            ma5  = np.mean(closes[-5:])
            ma20 = float(np.mean(closes[-20:]))
            bb_std = float(np.std(closes[-20:]))
            bb_lower = ma20 - 2.0 * bb_std
            bb_upper = ma20 + 2.0 * bb_std
            iv["ma20"]     = ma20       # ★ 전략 B용
            iv["bb_lower"] = bb_lower   # ★ 전략 B용
            iv["bb_upper"] = bb_upper   # ★ 전략 B용
            if ma5 > ma20:
                score += 1.0

        # 거래량 평균 (최근 20봉)
        avg_vol20 = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(avg_vol4)
        iv["avg_vol20"] = avg_vol20   # ★ 전략 B용

        # OBV 추세
        if len(closes) >= 5:
            obv_up = sum(
                1 for i in range(-5, 0)
                if -i <= len(closes) and closes[i] > closes[i-1]
                and volumes[i] > volumes[i-1]
            )
            if obv_up >= 3:
                score += 1.0

        # RSI 중립~상승 (40~70) — iv["rsi"]는 위에서 이미 설정됨
        rsi_val = iv.get("rsi", 0.0)
        if rsi_val == 0.0 and len(closes) >= 14:
            rsi_val = self._calc_rsi(closes, 14)
            iv["rsi"] = rsi_val
        if 40 <= rsi_val <= 70:
            score += 1.0

        # 볼린저밴드 중심선 위
        if ma20 > 0 and cur_close > ma20:
            score += 1.0

        # 거래량 보너스
        if vol_surge:
            score += 3.0
        elif vol_increase:
            score += 2.0

        # 돌파 가점
        breakout_bonus = self._calc_breakout_bonus(
            closes, highs, volumes, cur_price, avg_vol4, price_data
        )
        score += breakout_bonus * 10    # +0.10 → +1.0 점수
        iv["breakout_bonus"] = breakout_bonus

        # SELL SCORE 연동 (역방향)
        sell_score = self._calc_sell_score(closes, volumes, vwap, price_data)
        iv["sell_score"] = sell_score
        if sell_score >= 3:
            score -= float(sell_score - 2)

        # 정규화 (최대 14점 → 1.0)
        iv["buy_score"] = round(min(score / 14.0, 1.0), 3)

        return iv

    # ── 돌파 가점 ─────────────────────────────────────────────

    def _calc_breakout_bonus(self,
                              closes: list, highs: list, volumes: list,
                              cur_price: int, avg_vol4: float,
                              price_data: dict) -> float:
        """
        돌파 가점 (+0.10 / +0.20 / +0.30).
        """
        if len(closes) < 2 or len(highs) < 2 or len(volumes) < 2:
            return 0.0

        prev_high   = highs[-2]
        day_high    = price_data.get("high", cur_price)
        cur_vol     = volumes[-1]
        prev_vol    = volumes[-2]

        # 폭발 돌파: 현재가 > 당일고가+0.5% + 거래량 > avg*3 + 체결강도 > 150
        strength = price_data.get("strength", 0)
        if (cur_price > day_high * 1.005
                and cur_vol > avg_vol4 * 3
                and strength > 150):
            return 0.30

        # 강한 돌파: 현재가 > 당일고가 + 거래량 > avg*2
        if cur_price > day_high and cur_vol > avg_vol4 * 2:
            return 0.20

        # 초기 돌파: 현재가 > 직전 5분봉 고가 + 거래량 > 직전봉*1.5
        if cur_price > prev_high and cur_vol > prev_vol * 1.5:
            return 0.10

        return 0.0

    # ── SELL SCORE ────────────────────────────────────────────

    def _calc_sell_score(self,
                          closes: list, volumes: list,
                          vwap: float, price_data: dict) -> int:
        score = 0
        cur_price = price_data.get("price", closes[-1] if closes else 0)

        if cur_price < vwap and vwap > 0:          score += 4  # VWAP 이탈
        if len(closes) >= 2 and closes[-1] < closes[-2]: score += 2  # 하락봉
        if len(volumes) >= 2 and volumes[-1] < volumes[-2]: score += 2  # 거래량 감소
        if len(closes) >= 5:
            ma5 = np.mean(closes[-5:])
            if cur_price < ma5:                    score += 3  # MA5 이탈
        return score

    # ── RSI ───────────────────────────────────────────────────

    @staticmethod
    def _calc_rsi(closes: list, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        arr   = np.array(closes[-period-1:], dtype=float)
        diffs = np.diff(arr)
        gains = np.where(diffs > 0, diffs, 0.0)
        losses= np.where(diffs < 0, -diffs, 0.0)
        avg_g = gains.mean()
        avg_l = losses.mean()
        if avg_l == 0:
            return 100.0
        rs  = avg_g / avg_l
        return round(100 - 100 / (1 + rs), 2)

    @staticmethod
    def _is_rsi_falling(closes: list) -> bool:
        if len(closes) < 16:
            return False
        rsi_cur  = KRStrategy._calc_rsi(closes, 14)
        rsi_prev = KRStrategy._calc_rsi(closes[:-1], 14)
        return rsi_cur < rsi_prev

    # ── 돌파봉 저가 ───────────────────────────────────────────

    @staticmethod
    def _get_breakout_low(candles_5m: list, cur_price: int) -> float:
        """진입 시점 돌파봉 저가 (직전 5분봉 저가 사용)."""
        if len(candles_5m) >= 2:
            return float(candles_5m[-2]["low"])
        return 0.0

    # ── 주문 가격 결정 ─────────────────────────────────────────

    def _decide_order_price(self,
                             cur_price: int,
                             price_data: dict) -> tuple[int, str]:
        """
        현재가 기준 매수 주문가격 및 ORD_DVSN 결정.
        강도 높으면 시장가, 아니면 지정가(호가+1틱).
        """
        strength = price_data.get("strength", 100)
        if strength >= 120:
            # 체결강도 높음 → 시장가 (빠른 체결)
            return 0, ORD_MARKET

        # 지정가 = 현재가 + 1틱 (우선 체결)
        tick      = KRBroker.tick_size(cur_price)
        buy_price = KRBroker.round_to_tick(cur_price + tick, direction=1)
        return buy_price, ORD_LIMIT

    # ── 내부 보유수량 ─────────────────────────────────────────

    def _get_held_qty_internal(self, code: str) -> int:
        pg = self._positions.get(code)
        return pg.qty if pg else 0

    # ── 포지션 영속화 ─────────────────────────────────────────

    def _save_positions(self):
        try:
            data = {}
            for code, pg in self._positions.items():
                data[code] = {
                    "code":         pg.code,
                    "name":         pg.name,
                    "avg_price":    pg.avg_price,
                    "qty":          pg.qty,
                    "entry_time":   pg.entry_time.isoformat(),
                    "breakout_low": pg.breakout_low,
                    "stage":        self._entry_stage.get(code, ""),
                }
            with open(self._pos_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[KRStrategy] 포지션 저장 실패: {e}")

    def _load_positions(self):
        try:
            if not os.path.exists(self._pos_file):
                return
            with open(self._pos_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for code, d in data.items():
                try:
                    entry_time = datetime.fromisoformat(
                        d.get("entry_time", datetime.now(KST).isoformat())
                    )
                    if entry_time.tzinfo is None:
                        entry_time = KST.localize(entry_time)
                except Exception:
                    entry_time = datetime.now(KST)
                self._positions[code] = PositionGuard(
                    code         = d["code"],
                    name         = d.get("name", code),
                    avg_price    = float(d.get("avg_price", 0)),
                    qty          = int(d.get("qty", 0)),
                    entry_time   = entry_time,
                    breakout_low = float(d.get("breakout_low", 0)),
                )
                self._entry_stage[code] = d.get("stage", "")
            logger.info(f"[KRStrategy] 포지션 복원: {len(self._positions)}개")
        except Exception as e:
            logger.warning(f"[KRStrategy] 포지션 로드 실패: {e}")

    def _load_single_position(self, code: str) -> Optional[dict]:
        """
        영속화 파일에서 특정 종목의 포지션 데이터를 반환.
        entry_time_dt (datetime), breakout_low (float) 포함.
        없으면 None.
        """
        try:
            if not os.path.exists(self._pos_file):
                return None
            with open(self._pos_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            d = data.get(code)
            if not d:
                return None
            try:
                entry_time = datetime.fromisoformat(d.get("entry_time", ""))
                if entry_time.tzinfo is None:
                    entry_time = KST.localize(entry_time)
            except Exception:
                return None
            return {
                "entry_time_dt": entry_time,
                "breakout_low":  float(d.get("breakout_low", 0)),
                "avg_price":     float(d.get("avg_price", 0)),
                "stage":         d.get("stage", ""),
            }
        except Exception:
            return None

    # ════════════════════════════════════════════════════════════
    # ■ 전략 B — BB하단 평균회귀 진입 판단
    # ════════════════════════════════════════════════════════════

    def _eval_entry_b(self,
                      code: str, name: str, cur_price: int,
                      candles_5m: list, price_data: dict,
                      now: datetime) -> dict:
        """
        전략 B (BB하단 평균회귀) 진입 판단.
        조건:
          ① cur_price <= bb_lower * _STRAT_B_BB_PROX   (BB하단 근접)
          ② cur_price >= ma20 * _STRAT_B_MA20_FLOOR     (MA20 위)
          ③ _STRAT_B_RSI_MIN <= RSI <= _STRAT_B_RSI_MAX (RSI 40~60)
          ④ cur_vol >= avg_vol20 * _STRAT_B_VOL_MULT    (거래량 증가)
          ⑤ 급등주 제외: 당일 상승률 < _STRAT_B_SURGE_PCT (5%)
          ⑥ 급등량 제외: cur_vol < avg_vol20 * _STRAT_B_VOL_SURGE
        """
        # 재진입 차단
        blocked, block_info = self.reentry.check("KR", code, name)
        if blocked:
            return self._skip(code, name,
                               f"[B] 재진입 차단 — {block_info.get('block_reason', '')}")

        # 수량 사전 체크
        entry_pre = self.account.calc_entry_amount(cur_price)
        if not entry_pre["can_enter"] or entry_pre.get("max_qty", 0) <= 0:
            return self._skip(code, name,
                               f"[B] {entry_pre.get('block_reason', '수량0')}")

        # 지표 계산 (전략 A와 공유 — _calc_indicators 재활용)
        iv = self._calc_indicators(candles_5m, price_data)

        bb_lower  = iv.get("bb_lower", 0.0)
        ma20      = iv.get("ma20", 0.0)
        rsi       = iv.get("rsi", 0.0)
        avg_vol20 = iv.get("avg_vol20", 0.0)

        closes  = [c["close"]  for c in candles_5m] if candles_5m else []
        volumes = [c["volume"] for c in candles_5m] if candles_5m else []
        cur_vol = volumes[-1] if volumes else 0

        # ── 조건 평가 ─────────────────────────────────────────
        # 당일 상승률 (open 대비)
        open_price = price_data.get("open", cur_price)
        day_rise_pct = (cur_price - open_price) / open_price * 100 if open_price > 0 else 0.0

        cond_bb    = bb_lower > 0 and cur_price <= bb_lower * _STRAT_B_BB_PROX
        cond_ma20  = ma20 > 0 and cur_price >= ma20 * _STRAT_B_MA20_FLOOR
        cond_rsi   = _STRAT_B_RSI_MIN <= rsi <= _STRAT_B_RSI_MAX
        cond_vol   = avg_vol20 > 0 and cur_vol >= avg_vol20 * _STRAT_B_VOL_MULT
        cond_surge = day_rise_pct < _STRAT_B_SURGE_PCT
        cond_vol_surge = not (avg_vol20 > 0 and cur_vol >= avg_vol20 * _STRAT_B_VOL_SURGE)

        logger.info(
            f"[진입평가-B] {name}({code}) | strategy=B | "
            f"현재가={cur_price:,} | bb_lower={bb_lower:,.0f} | ma20={ma20:,.0f} | "
            f"RSI={rsi:.0f} | cur_vol={cur_vol:,} | avg_vol20={avg_vol20:,.0f} | "
            f"day_rise={day_rise_pct:+.1f}% | "
            f"BB근접={cond_bb} MA20위={cond_ma20} RSI범위={cond_rsi} "
            f"거래량={cond_vol} 급등제외={cond_surge} 급등량제외={cond_vol_surge}"
        )

        # 전체 조건 미충족 시 SKIP
        if not cond_bb:
            return self._skip(code, name,
                               f"[B] BB하단 미근접 cur={cur_price} bb_lower={bb_lower:.0f}×{_STRAT_B_BB_PROX}")
        if not cond_ma20:
            return self._skip(code, name,
                               f"[B] MA20 하회 cur={cur_price} ma20={ma20:.0f}×{_STRAT_B_MA20_FLOOR}")
        if not cond_rsi:
            return self._skip(code, name,
                               f"[B] RSI 범위이탈 rsi={rsi:.0f} 허용={_STRAT_B_RSI_MIN}~{_STRAT_B_RSI_MAX}")
        if not cond_vol:
            return self._skip(code, name,
                               f"[B] 거래량 미달 cur_vol={cur_vol} avg20={avg_vol20:.0f}×{_STRAT_B_VOL_MULT}")
        if not cond_surge:
            return self._skip(code, name,
                               f"[B] 급등주 제외 day_rise={day_rise_pct:+.1f}% >= {_STRAT_B_SURGE_PCT}%")
        if not cond_vol_surge:
            return self._skip(code, name,
                               f"[B] 급등거래량 제외 cur_vol={cur_vol} >= avg20×{_STRAT_B_VOL_SURGE}")

        # ── 진입 실행 ─────────────────────────────────────────
        max_qty = entry_pre["max_qty"]
        reason  = (f"[B]BB하단회귀 bb_lower={bb_lower:.0f} RSI={rsi:.0f} "
                   f"vol={cur_vol/avg_vol20:.1f}×avg")

        ord_price, ord_dvsn = self._decide_order_price(cur_price, price_data)
        signal_time = now.isoformat()
        order_time  = datetime.now(KST).isoformat()

        result = self.executor.execute_buy(
            code     = code,
            name     = name,
            price    = ord_price,
            qty      = max_qty,
            reason   = reason,
            ord_dvsn = ord_dvsn,
        )

        if result.get("action") == "BUY":
            self._entry_stage[code] = "FULL"
            breakout_low = self._get_breakout_low(candles_5m, cur_price)
            self._positions[code] = PositionGuard(
                code         = code,
                name         = name,
                avg_price    = ord_price if ord_price > 0 else float(cur_price),
                qty          = max_qty,
                entry_time   = now,
                breakout_low = breakout_low,
            )
            self._save_positions()
            order_no = result.get("order_no", "") or ""
            entry_price_log = ord_price if ord_price > 0 else cur_price
            logger.info(
                f"[BUY_OK] 종목={name}({code}) | 시장=KR | strategy=B | "
                f"수량={max_qty}주 | 진입가={entry_price_log:,}원 | "
                f"bb_lower={bb_lower:.0f} | RSI={rsi:.0f} | "
                f"order_no={order_no} | 사유={reason}"
            )
            if self.recorder:
                try:
                    iv_b = dict(iv)
                    iv_b["strategy"] = "B"
                    self.recorder.record_entry(
                        market       = "KR",
                        code         = code,
                        name         = name,
                        price        = float(ord_price if ord_price > 0 else cur_price),
                        qty          = max_qty,
                        reason       = reason,
                        iv           = iv_b,
                        stage        = "FULL",
                        signal_time  = signal_time,
                        order_time   = order_time,
                        order_no     = order_no,
                        price_source = "KIS지정가" if ord_price > 0 else "KIS시장가",
                    )
                except Exception as _re:
                    logger.debug(f"[KRStrategy-B] 진입 기록 실패: {_re}")

        return result

    # ── 유틸 ─────────────────────────────────────────────────

    @staticmethod
    def _skip(code: str, name: str, reason: str) -> dict:
        logger.debug(f"[SKIP] {name}({code}) → {reason}")
        return {"action": "SKIP", "code": code, "name": name, "reason": reason}
