"""
strategy/us_strategy.py — 미국장 V2 핵심 전략 엔진
====================================================
설계 원칙:
  - KR 전략과 동일한 구조 (run → _eval_entry / _eval_exit)
  - 미국 정규장 시간 기준 진입/청산 (USBroker.is_buy_allowed())
  - USD 기준 주문금액 계산 (총자산 KRW → 환율 환산)
  - 재진입 차단 / 손익 관리 / 포지션 리스크는 공통 모듈 위임
  - 국내장과 완전 분리 — DailyPnLGuard(market="US") 별도 인스턴스

진입 전략:
  BUY_SCORE ≥ 0.40 → 30% 선진입 (Early Entry)
  BUY_SCORE ≥ 0.55 → +70% 추가 (Full Entry)
  필수: 거래량증가 + VWAP위 + SELL_SCORE < 5
  추격매수 금지: 15분+4%, 5분+2%, 연속3봉

손절 전략:
  돌파봉 저가 이탈 → 즉시 청산
  돌파 실패 조건 2개 이상 → 조기 청산
  에어백: -5%

익절 전략:
  +1.5% SELL_SCORE 연동 청산
  +2.0% 전량 익절
  +2.5% 무조건 전량 익절

시간 관리:
  정규장 마감 30분 전: 신규매수 금지
  정규장 마감 10분 전: 수익 < +1% → 청산 검토
  정규장 마감:         전량 강제 청산

주문 통화:
  - 주문단위: USD
  - 1회 진입금액: AccountSync.calc_entry_amount()로 KRW 계산 후 환율로 USD 변환
  - orderable_usd: USBroker.get_orderable_usd()로 실제 가능 금액 확인
"""

import os
import json
import time
from datetime import datetime, date, time as dtime, timedelta
from typing import Optional, List

import pytz

from broker.us_broker     import (
    USBroker, ORD_MARKET, ORD_LIMIT,
    EXCH_NASD, EXCH_NYSE, EXCH_AMEX,
)
from risk.reentry_guard   import ReentryGuard
from risk.pnl_guard       import DailyPnLGuard
from risk.position_guard  import PositionGuard, STOPLOSS_HARD_PCT
from engine.account_sync  import AccountSync
from engine.execution_engine import ExecutionEngine
from utils.v2_logger      import get_logger
from adaptive.trade_recorder import TradeRecorder
from adaptive.weight_adjuster import WeightAdjuster

logger = get_logger("USStrategy")
KST    = pytz.timezone("Asia/Seoul")

# ── BUY SCORE 임계 ─────────────────────────────────────────────
BUY_SCORE_EARLY = 0.40   # 30% 선진입
BUY_SCORE_FULL  = 0.55   # 100% 진입

# ── 추격매수 금지 기준 ─────────────────────────────────────────
CHASE_RISE_15M  = 4.0    # 최근 15분 상승률(%) 초과 시 금지
CHASE_RISE_5M   = 2.0    # 최근 5분 상승률(%) 초과 시 금지
CHASE_BULL_CNT  = 3      # 연속 양봉 N개 이상 시 금지

# ── 장중 신호 (Midday Signal) 상수 ────────────────────────────
# 미국장 개장(22:30 KST / EDT) 후 N분 이후를 "장중"으로 간주
MIDDAY_START_MIN    = 60         # 개장 후 60분 = 23:30 KST(EDT) 이후
MIDDAY_VOL_MULT     = 2.5        # 직전 4봉 평균 대비 2.5x 이상 거래량
MIDDAY_VWAP_CROSS_MARGIN = 0.001 # VWAP 크로스 허용 오차 0.1%
MIDDAY_SCORE_MIN    = 0.40       # 장중 면제 경로 최소 BUY_SCORE (EARLY와 동일)

# ── 오버나이트 관련 ───────────────────────────────────────────
OVERNIGHT_CHECK_MIN   = 10   # 마감 10분 전: 수익<+1% → 청산 검토
OVERNIGHT_PROFIT_PCT  = 1.0  # 오버나이트 허용 최소 수익률(%)

# ── 기본 환율 (API 없을 시 폴백) ─────────────────────────────
_DEFAULT_USD_KRW = 1_350.0   # USD/KRW 폴백 환율

# ── KIS 계좌에서 주문 불가한 종목 블랙리스트 ─────────────────
# APBK1672: 해외ETP 거래 미신청 계좌 (ETF/ETN 레버리지 상품)
# APBK0656: KIS에 종목정보 없음 (장외/비상장/거래정지 등)
_KIS_ORDER_BLACKLIST: dict[str, str] = {
    # ETP(해외 레버리지 ETF) — 계좌 별도 신청 필요 (APBK1672)
    "TQQQ": "해외ETP 미신청(APBK1672)",
    "SOXL": "해외ETP 미신청(APBK1672)",
    "TECL": "해외ETP 미신청(APBK1672)",
    "LABU": "해외ETP 미신청(APBK1672)",
    "FNGU": "해외ETP 미신청(APBK1672)",
    "UVXY": "해외ETP 미신청(APBK1672)",
    "SPXL": "해외ETP 미신청(APBK1672)",
    "UDOW": "해외ETP 미신청(APBK1672)",
    # KIS 종목정보 없음 — 상장 폐지 / 거래정지 / 미지원 (APBK0656)
    "BBAI": "KIS종목정보없음(APBK0656)",
    "QBTS": "KIS종목정보없음(APBK0656)",
    "CIFR": "KIS종목정보없음(APBK0656)",
    "ACHR": "KIS종목정보없음(APBK0656)",   # 2026-06-15 BUY_FAIL 반복 확인
}

# ══════════════════════════════════════════════════════════════
# 전략 B (BB하단 평균회귀) 파라미터
# ══════════════════════════════════════════════════════════════
# strategy=A : 기존 돌파/모멘텀 전략 (BUY_SCORE >= 0.40)
# strategy=B : 볼린저밴드 하단 평균회귀 전략 (눌림목 반등 포착)

_STRAT_B_BB_PROX    = 1.02   # BB 하단의 102% 이내 (하단 근접 기준)
_STRAT_B_MA20_FLOOR = 0.995  # MA20의 99.5% 이상 (하락추세 제외)
_STRAT_B_RSI_MIN    = 40.0   # RSI 하한 (극단 과매도 제외)
_STRAT_B_RSI_MAX    = 60.0   # RSI 상한 (과열 제외)
_STRAT_B_VOL_MULT   = 1.1    # 최근 20봉 평균 대비 거래량 증가 기준
_STRAT_B_SURGE_PCT  = 5.0    # 급등주 제외: 당일 변동률 > 5%
_STRAT_B_VOL_SURGE  = 3.0    # 급등주 제외: 거래량 > 20봉평균 × 3배


_CANDLE_MIN  = 5    # 최소 5분봉 수 (계산 가능 최소치)
_CANDLE_MAX  = 20   # 지표 계산에 사용하는 봉 수

# ══════════════════════════════════════════════════════════════
# 트레일링 익절 (Trailing Take-Profit) 파라미터
# ══════════════════════════════════════════════════════════════
# 트레일링 진입 기준: 현재수익률이 이 값 이상이면 트레일링 모드 시작
_TRAIL_ENTRY_PCT      = 2.0   # % — 최초 +2.0% 도달 시 트레일링 시작
# HWM 대비 반납 허용 한도: 이 이상 반납하면 즉시 매도
_TRAIL_PULLBACK_PCT   = 1.5   # % — 최고수익률 대비 1.5% 하락 시 청산
# 수익 보호 하한: +2% 이상 찍은 종목이 이 수준까지 내려오면 청산
_TRAIL_PROTECT_PCT    = 0.5   # % — +0.5% 이하로 내려오면 무조건 청산
# 강제 상한: 이 이상이면 트레일링 무관 즉시 익절
_TRAIL_HARD_CAP_PCT   = 10.0  # % — +10% 이상 즉시 전량 익절


class USStrategy:
    """
    미국장 단일 종목 전략 실행기.

    사용법:
        strat = USStrategy(broker_us, account, reentry, pnl_us, usd_krw_rate)
        result = strat.run({"code": "AAPL", "name": "Apple", "exch_cd": "NASD"})
    """

    def __init__(self,
                 broker:    USBroker,
                 account:   AccountSync,
                 reentry:   ReentryGuard,
                 pnl:       DailyPnLGuard,
                 usd_krw:   float = _DEFAULT_USD_KRW,
                 recorder:  Optional[TradeRecorder] = None,
                 adjuster:  Optional[WeightAdjuster] = None):
        self.broker    = broker
        self.account   = account
        self.reentry   = reentry
        self.pnl       = pnl
        self.usd_krw   = usd_krw
        self.recorder  = recorder   # Adaptive Engine 기록기
        self.adjuster  = adjuster   # Adaptive Engine 가중치 조정기
        self.executor  = ExecutionEngine(broker, account, reentry)

        # 포지션 상태 {code: PositionGuard}
        self._positions:    dict = {}
        # 진입 단계 {code: "EARLY"|"FULL"}
        self._entry_stage:  dict = {}
        # Adaptive 진입 iv 캐시 {code: iv_dict}  ← record_exit 시 활용
        self._entry_iv:     dict = {}

        # ★ US 실잔고 캐시 (broker_us.get_balance() 결과, 30초 TTL)
        # AccountSync는 KRBroker 기반이므로 US 잔고 별도 관리
        self._us_holdings_cache: dict  = {}   # {code: holding_dict}
        self._us_holdings_ts:    float = 0.0
        _US_HOLDINGS_TTL = 25.0   # 25초 (루프 30초보다 짧게)

        # ★ 트레일링 익절 상태
        # {code: {"hwm_pct": float, "active": bool}}
        #   hwm_pct  : 최고수익률(High-Water Mark), %
        #   active   : True = 트레일링 모드 진행 중
        self._trail_state: dict = {}

        # ★ [BUG2 FIX] SELL_OK 직후 KIS 캐시 지연 재등록 방지
        # SELL_OK 성공 시점으로부터 60초간 해당 코드를 KIS 기준 재등록 차단
        # {code: timestamp_float}
        self._sold_grace: dict = {}          # SELL_OK 후 60초 재등록 차단
        _SOLD_GRACE_SEC = 60.0               # 60초 (KIS 체결 반영 대기)

        _DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
        self._pos_file = os.path.join(_DATA_DIR, "v2_us_positions.json")
        os.makedirs(_DATA_DIR, exist_ok=True)
        self._load_positions()

    # ════════════════════════════════════════════════════════════
    # 메인 루프 진입점
    # ════════════════════════════════════════════════════════════

    def run(self, stock: dict) -> dict:
        """
        단일 종목 전략 실행.

        Args:
            stock: {"code": "AAPL", "name": "Apple", "exch_cd": "NASD"}

        Returns:
            {"action": str, "code": str, "name": str, "reason": str, ...}
        """
        code    = stock.get("code", "").upper()
        name    = stock.get("name", code)
        exch_cd = stock.get("exch_cd", EXCH_NASD).upper()
        now_kst = datetime.now(KST)

        if not code:
            return self._skip(code, name, "종목코드 없음")

        # ★ US 실잔고 조회 (broker_us 직접 호출, 캐시 25초)
        # ★ 주말/장외/블랙리스트 체크보다 먼저 수행 → 어떤 상황에서도 잔고 인식 가능
        # AccountSync.holdings는 KRBroker 기반 → 미국 종목 인식 불가
        # 반드시 USBroker.get_balance()를 사용해야 함
        now_ts = time.time()
        if now_ts - self._us_holdings_ts > 55.0:   # 55초 TTL: 루프 1바퀴 내 1회만 호출
            try:
                bal_us = self.broker.get_balance(force=True)
                self._us_holdings_cache = {
                    h["code"].upper(): h
                    for h in bal_us.get("holdings", [])
                    if h.get("code")
                }
                self._us_holdings_ts = now_ts
            except Exception as _e:
                logger.warning(f"[USStrategy] US 잔고 조회 실패: {_e}")
                # 캐시 만료 시에도 기존 캐시 유지 (주문 중단 방지)

        real_holdings = self._us_holdings_cache
        _kis_cache_valid_early = self._us_holdings_ts > 0

        # ★ 블랙리스트 종목이지만 KIS 실잔고에 존재 → 강제청산
        if code in _KIS_ORDER_BLACKLIST:
            bl_reason = _KIS_ORDER_BLACKLIST[code]
            if _kis_cache_valid_early and code in real_holdings:
                h_bl      = real_holdings[code]
                bl_qty    = int(h_bl.get("qty", 0))
                bl_avg    = float(h_bl.get("avg_price", 0))
                # 현재가 조회 시도
                pd_bl = self.broker.get_price(code, exch_cd)
                bl_price  = pd_bl.get("cur_price", 0) if pd_bl.get("ok") else 0
                if bl_qty > 0 and bl_price > 0:
                    bl_pnl = (bl_price - bl_avg) / bl_avg * 100 if bl_avg > 0 else 0
                    logger.warning(
                        f"[BL 강제청산] {name}({code}) → 블랙리스트+KIS보유 | "
                        f"qty={bl_qty} avg=${bl_avg:.4f} 현재가=${bl_price:.2f} "
                        f"손익={bl_pnl:+.2f}% | 사유={bl_reason}"
                    )
                    return self._do_sell(
                        code=code, name=name, exch_cd=exch_cd,
                        qty=bl_qty, cur_price=bl_price,
                        reason=f"블랙리스트강제청산({bl_reason},pnl={bl_pnl:+.2f}%)",
                        is_stoploss=(bl_pnl < 0),
                        now_kst=now_kst,
                    )
            logger.debug(f"[BL SKIP] {name}({code}) → {bl_reason}")
            return self._skip(code, name, f"KIS주문불가:{bl_reason}")

        # ★ 주말/장외 체크 — 단, KIS 실잔고에 포지션 있으면 익절/손절은 무조건 진행
        _is_weekend   = now_kst.weekday() >= 5
        _is_mkt_open  = self.broker.is_market_open(now_kst)
        _has_position = _kis_cache_valid_early and (
            code in real_holdings or code in self._positions
        )

        if _is_weekend or not _is_mkt_open:
            if not _has_position:
                # 포지션 없음 → 진입 불필요, 그냥 SKIP
                reason_skip = "주말 — 미국장 휴장" if _is_weekend else "미국 정규장 외 시간"
                return self._skip(code, name, reason_skip)
            # ★ 포지션 보유 중 → 주말/장외여도 익절/손절 판단 진행
            logger.debug(
                f"[US 주말/장외 포지션 유지판단] {name}({code}) | "
                f"주말={_is_weekend} 장외={not _is_mkt_open} → 익절/손절 판단 계속"
            )

        # 현재가 조회
        price_data = self.broker.get_price(code, exch_cd)
        if not price_data.get("ok") or price_data.get("cur_price", 0) <= 0:
            return self._skip(code, name, "시세 조회 실패")

        cur_price = price_data["cur_price"]

        # 5분봉 조회
        candles_5m = self.broker.get_5min_candles(code, exch_cd)
        if len(candles_5m) < _CANDLE_MIN:
            return self._skip(code, name, f"5분봉 부족 ({len(candles_5m)}개)")

        # ── [KIS-POS 동기화] KIS 실잔고 → 내부 포지션 자동 보정 ──
        h_data = real_holdings.get(code)
        if h_data:
            kis_qty = int(h_data.get("qty", 0))
            kis_avg = float(h_data.get("avg_price", 0))
            if code in self._positions:
                pg = self._positions[code]
                int_qty = pg.qty
                int_avg = pg.avg_price
                if int_qty != kis_qty or abs(int_avg - kis_avg) > 0.01:
                    logger.warning(
                        f"[KIS-POS 동기화] 종목={name}({code}) | "
                        f"KIS수량={kis_qty} 내부수량={int_qty} | "
                        f"KIS평균단가=${kis_avg:.4f} 내부평균단가=${int_avg:.4f} | "
                        f"처리결과=KIS기준보정"
                    )
                    pg.qty       = kis_qty
                    pg.avg_price = kis_avg
                else:
                    logger.debug(
                        f"[KIS-POS 동기화] 종목={name}({code}) | "
                        f"KIS수량={kis_qty} 내부수량={int_qty} | "
                        f"KIS평균단가=${kis_avg:.4f} 내부평균단가=${int_avg:.4f} | "
                        f"처리결과=일치"
                    )
            else:
                # 내부 포지션 없음 → KIS 기준으로 즉시 등록
                # ★ [BUG2 FIX] SELL_OK 직후 grace period 내이면 재등록 차단
                _grace_ts = self._sold_grace.get(code, 0.0)
                _now_ts   = time.time()
                _SOLD_GRACE_SEC = 60.0
                if _grace_ts > 0 and (_now_ts - _grace_ts) < _SOLD_GRACE_SEC:
                    logger.info(
                        f"[KIS-POS 동기화] 종목={name}({code}) | "
                        f"KIS수량={kis_qty} 내부수량=0 | "
                        f"처리결과=SELL_OK grace period 내 재등록 차단 "
                        f"(경과={_now_ts - _grace_ts:.0f}초 < {_SOLD_GRACE_SEC:.0f}초)"
                    )
                else:
                    breakout_low = round(kis_avg * 0.98, 4)
                    self._positions[code] = PositionGuard(
                        code         = code,
                        name         = name,
                        avg_price    = kis_avg,
                        qty          = kis_qty,
                        entry_time   = datetime.now(KST),
                        breakout_low = breakout_low,
                        market       = "US",
                    )
                    self._save_positions()
                    logger.warning(
                        f"[KIS-POS 동기화] 종목={name}({code}) | "
                        f"KIS수량={kis_qty} 내부수량=0 | "
                        f"KIS평균단가=${kis_avg:.4f} 내부평균단가=없음 | "
                        f"처리결과=KIS기준신규등록(breakout=${breakout_low:.4f})"
                    )

        # ★ KIS API 캐시 유효 여부 플래그 (_kis_cache_valid_early와 동일 값, 가독성용 별칭)
        _kis_cache_valid = _kis_cache_valid_early

        # 고스트 포지션 감지: 내부에는 있는데 KIS 실잔고에 없음 → 청산 완료로 간주
        # ★ 반드시 캐시가 유효(1회 이상 KIS 조회 성공)할 때만 실행
        #   KIS 타임아웃으로 real_holdings={} 일 때 실행하면 정상 포지션이 삭제됨
        if _kis_cache_valid and code in self._positions and code not in real_holdings:
            logger.warning(
                f"[USStrategy] 고스트 포지션 감지 {name}({code}) "
                f"— 내부 기록 제거"
            )
            del self._positions[code]
            self._entry_stage.pop(code, None)
            self._save_positions()

        # ── 보유 중 → 청산 평가 ──────────────────────────────────
        # 우선순위: KIS 실잔고 캐시 → 내부 _positions 순서로 판단
        # KIS API 타임아웃으로 real_holdings가 비어도 내부 포지션 기준으로 즉각 판단
        _in_kis  = code in real_holdings          # KIS 캐시에 보유 확인
        _in_pos  = code in self._positions        # 내부 포지션에 보유 확인
        _is_held = _in_kis or _in_pos             # 어느 쪽이든 보유 중

        # ★ [KIS 실잔고 HARD_STOP 에어백] ──────────────────────────
        # 내부 포지션 꼬임과 무관하게 KIS 실잔고 기준으로 독립 발동
        # 조건: KIS 실보유>0 AND 손익률<=-5.0%
        # _unified_exit() 보다 먼저 체크 → SELL_FAIL 루프 탈출 보장
        if _kis_cache_valid and _in_kis:
            _h_airbag      = real_holdings[code]
            _kis_qty_ab    = int(_h_airbag.get("qty", 0))
            _ord_psbl_ab   = int(_h_airbag.get("ord_psbl_qty", _kis_qty_ab))
            _avg_ab        = float(_h_airbag.get("avg_price", 0))
            _pnl_ab        = (cur_price - _avg_ab) / _avg_ab * 100 if _avg_ab > 0 else 0.0
            if _kis_qty_ab > 0 and _pnl_ab <= STOPLOSS_HARD_PCT:
                logger.warning(
                    f"[US_HARD_STOP_AIRBAG] 에어백 발동 조건 충족 "
                    f"종목={name}({code}) "
                    f"KIS보유={_kis_qty_ab} ord_psbl={_ord_psbl_ab} "
                    f"평균단가=${_avg_ab:.4f} 현재가=${cur_price:.2f} "
                    f"손익률={_pnl_ab:+.2f}% → _kis_hard_stop_airbag() 호출"
                )
                return self._kis_hard_stop_airbag(
                    code=code, name=name, exch_cd=exch_cd,
                    kis_qty=_kis_qty_ab, ord_psbl_qty=_ord_psbl_ab,
                    avg_price=_avg_ab, cur_price=cur_price,
                    now_kst=now_kst,
                )

        if _is_held:
            # ─ 평균단가·수량: KIS 캐시 > 내부 포지션 순서로 채움 ─
            if _in_kis:
                h        = real_holdings[code]
                held_qty = int(h.get("qty", 0))
                kis_avg  = float(h.get("avg_price", cur_price))
                _src     = "KIS실잔고"
            else:
                # KIS API 타임아웃 — 내부 포지션 사용 (fallback)
                pg_fb    = self._positions[code]
                held_qty = pg_fb.qty
                kis_avg  = pg_fb.avg_price if pg_fb.avg_price > 0 else cur_price
                _src     = "내부포지션(KIS타임아웃)"

            if held_qty > 0:
                # ★ 통합 청산 판단 — 한 루프에서 단 한 번만 판정
                return self._unified_exit(
                    code=code, name=name, exch_cd=exch_cd,
                    cur_price=cur_price, held_qty=held_qty,
                    avg_price=kis_avg, avg_price_src=_src,
                    candles_5m=candles_5m,
                    price_data=price_data,
                    now_kst=now_kst,
                )

        # ── 미보유 → 진입 평가 ───────────────────────────────────
        # ★ 주말/장외 시간에는 신규 진입 불가 (보유 청산 로직은 이미 위에서 처리됨)
        if _is_weekend or not _is_mkt_open:
            return self._skip(code, name, "주말/장외 — 신규진입 불가(미보유)")

        # 신규 매수 시간 체크
        if not self.broker.is_buy_allowed(now_kst):
            return self._skip(code, name, "미국장 신규매수 금지 시간")

        # 일일 손익 체크
        if not self.pnl.can_buy:
            return self._skip(code, name, f"[US PnL] {self.pnl.block_reason()}")

        # ── 전략 A + B 동시 평가 ────────────────────────────────
        # 전략 A (모멘텀 돌파): BUY_SCORE >= 0.40
        result_a = self._eval_entry(
            code=code, name=name, exch_cd=exch_cd,
            cur_price=cur_price,
            candles_5m=candles_5m,
            price_data=price_data,
            now_kst=now_kst,
        )
        # 전략 A가 진입 성공하면 즉시 반환 (우선순위)
        if result_a.get("ok"):
            result_a["strategy"] = "A"
            return result_a

        # 전략 A 진입 불가 → 전략 B 시도 (BB하단 평균회귀)
        result_b = self._eval_entry_b(
            code=code, name=name, exch_cd=exch_cd,
            cur_price=cur_price,
            candles_5m=candles_5m,
            price_data=price_data,
            now_kst=now_kst,
        )
        if result_b.get("ok"):
            result_b["strategy"] = "B"
            logger.info(
                f"[전략B 진입] {name}({code}) | 전략A 미충족 후 B 진입 | "
                f"현재가=${cur_price:.2f} | 사유={result_b.get('reason','')}"
            )
            return result_b

        # 둘 다 실패 → 전략 A 결과(SKIP) 반환
        return result_a

    # ════════════════════════════════════════════════════════════
    # 진입 평가
    # ════════════════════════════════════════════════════════════

    def _eval_entry(self,
                    code: str, name: str, exch_cd: str,
                    cur_price: float,
                    candles_5m: list,
                    price_data: dict,
                    now_kst: datetime) -> dict:
        """미국 주식 진입 조건 평가."""

        # 재진입 차단 체크
        blocked, block_info = self.reentry.check("US", code, name)
        if blocked:
            return self._skip(
                code, name,
                f"⛔ 재진입 차단 — {block_info.get('block_reason', '')}"
            )

        # ── ★ 수량0 사전 제외: BUY 판정 전에 자금 확인 ─────────────
        _price_krw  = int(cur_price * self.usd_krw)
        _entry_pre  = self.account.calc_entry_amount(
            _price_krw, market="US", usd_krw=self.usd_krw
        )
        _budget_krw    = _entry_pre.get("entry_amount_krw", 0)
        _budget_usd    = _budget_krw / self.usd_krw if self.usd_krw > 0 else 0
        _total_asset   = _entry_pre.get("total_asset", self.account.total_asset)
        _orderable_usd = (_entry_pre.get("us_orderable_usd", 0.0)
                         or getattr(self.account, "us_orderable_usd", 0.0))

        # ① 기본 자금차단: 예수금/주문가능금액 아예 없음
        if not _entry_pre.get("can_enter"):
            _pre_reason = _entry_pre.get("block_reason", "자금부족")
            logger.info(
                f"[ENTRY_EXCLUDE] 종목={name}({code}) | 시장=US | "
                f"현재가=${cur_price:.2f} | "
                f"기본배정KRW={_budget_krw:,.0f}원(${_budget_usd:.0f}) | "
                f"US주문가능=${_orderable_usd:.2f} | "
                f"사유={_pre_reason}"
            )
            return self._skip(code, name, _pre_reason)

        # ② 총자산 40% 초과 고가주 차단: 1주 가격이 총자산의 40% 초과
        _max_single_krw = _total_asset * 0.40
        if _total_asset > 0 and _price_krw > _max_single_krw:
            _ex_reason = (
                f"1주가격({_price_krw:,.0f}원=${cur_price:.2f}) > "
                f"총자산40%({_max_single_krw:,.0f}원) → 초고가주 차단"
            )
            logger.info(
                f"[ENTRY_EXCLUDE] 종목={name}({code}) | 시장=US | "
                f"현재가=${cur_price:.2f} | 1주KRW={_price_krw:,.0f}원 | "
                f"총자산40%={_max_single_krw:,.0f}원 | 사유={_ex_reason}"
            )
            return self._skip(code, name, _ex_reason)

        # ③ 1주 매수 가능 여부: orderable_usd 기준 (기본배정과 OR 조건)
        #    기본배정으로 1주 이상 가능 → 통과
        #    기본배정 부족해도 orderable_usd >= cur_price → 1주 허용으로 통과
        #    orderable_usd=0(장외) → 폴백 우선순위:
        #   ★ 신규진입 사전체크는 정확한 값만 사용 (추정값 사용 금지)
        #      1) broker._orderable_last_ok (장중 마지막 성공값) — 유일하게 신뢰 가능
        #      2) 위도 없으면 → 기본배정 USD로만 판단 (1주 가능 여부)
        _broker_last_ok = getattr(self.broker, "_orderable_last_ok", 0.0)
        _orderable_src  = "없음"
        if _orderable_usd > 0:
            _orderable_src = "TTTS3011R"
        elif _broker_last_ok > 0:
            _orderable_usd = _broker_last_ok
            _orderable_src = f"last_ok(${_broker_last_ok:.0f})"
        # _orderable_usd가 여전히 0이면 → 기본배정(budget_usd)으로만 진입 판단
        logger.info(
            f"[US_CASH_DEBUG] 종목={name}({code}) | "
            f"현재가=${cur_price:.2f} | "
            f"orderable_usd=${_orderable_usd:.2f}(source={_orderable_src}) | "
            f"기본배정${_budget_usd:.0f} | "
            f"last_ok=${_broker_last_ok:.0f}"
        )
        _can_afford_basic     = (_budget_usd >= cur_price)
        _can_afford_orderable = (_orderable_usd >= cur_price) if _orderable_usd > 0 else False
        if not _can_afford_basic and not _can_afford_orderable:
            if _orderable_usd <= 0:
                _ex_reason = (
                    f"US주문가능 미확인(last_ok=0, TTTS3011R=0) | "
                    f"기본배정${_budget_usd:.0f} < 1주${cur_price:.2f} | "
                    f"장중 첫 주문가능 조회 성공 후 진입 가능"
                )
            else:
                _ex_reason = (
                    f"1주 매수 불가 | 기본배정${_budget_usd:.0f} / "
                    f"주문가능${_orderable_usd:.2f}({_orderable_src}) < 1주${cur_price:.2f}"
                )
            logger.info(
                f"[ENTRY_EXCLUDE] 종목={name}({code}) | 시장=US | "
                f"현재가=${cur_price:.2f} | "
                f"기본배정KRW={_budget_krw:,.0f}원(${_budget_usd:.0f}) | "
                f"US주문가능=${_orderable_usd:.2f}({_orderable_src}) | 사유={_ex_reason}"
            )
            return self._skip(code, name, _ex_reason)

        # 지표 계산
        iv = self._calc_indicators(candles_5m, price_data)

        # ── 장중 신호 판단 (개장 후 MIDDAY_START_MIN 이상 경과 시 chase_block 면제) ──
        _midday_sig = iv.get("midday_signal", False)
        _midday_tags = []
        if iv.get("vwap_cross_up"):    _midday_tags.append("VWAP크로스")
        if iv.get("bb_upper_cross"):   _midday_tags.append("BB상단돌파")
        if iv.get("midday_vol_surge"): _midday_tags.append("장중거래량급증")
        _midday_str = "+".join(_midday_tags) if _midday_tags else "없음"

        _midday_chase_exempt = False
        if iv.get("chase_blocked") and _midday_sig:
            # 미국장 개장 시각 계산 (EDT=22:30 KST, EST=23:30 KST)
            from broker.us_broker import _is_dst_kst as _dst_fn3
            _is_dst = _dst_fn3(now_kst)
            _open_h, _open_m = (22, 30) if _is_dst else (23, 30)
            _market_open_kst = now_kst.replace(
                hour=_open_h, minute=_open_m, second=0, microsecond=0
            )
            # 자정을 넘긴 경우 (EST 23:30 이후 KST 다음날) 조정 없이 직접 계산
            if now_kst < _market_open_kst:
                # 아직 개장 전 (이론상 이 경로는 is_buy_allowed()에서 걸림)
                _elapsed_open = 0.0
            else:
                _elapsed_open = (now_kst - _market_open_kst).total_seconds() / 60
            if _elapsed_open >= MIDDAY_START_MIN:
                _midday_chase_exempt = True
                logger.info(
                    f"[MIDDAY_EXEMPT] {name}({code}) | 시장=US | "
                    f"추격차단 면제 — 장중신호({_midday_str}) | "
                    f"개장후={_elapsed_open:.0f}분 | "
                    f"chase_reason={iv.get('chase_reason', '')}"
                )

        # 추격매수 금지 (장중 신호 면제 경로 제외)
        if iv.get("chase_blocked") and not _midday_chase_exempt:
            return self._skip(code, name,
                f"추격매수 금지: {iv.get('chase_reason', '')}")

        # 거래량 필터 (장중 거래량급증 신호면 면제)
        if not iv.get("vol_ok") and not iv.get("midday_vol_surge"):
            return self._skip(code, name, "거래량 부족")

        # VWAP 위 필터 (VWAP 크로스 업이면 현재봉은 VWAP 위이므로 자연히 통과)
        if not iv.get("above_vwap"):
            return self._skip(code, name, "VWAP 하방")

        # SELL_SCORE 필터
        if iv.get("sell_score", 0) >= 5:
            return self._skip(code, name,
                f"SELL_SCORE={iv['sell_score']} ≥ 5 → 매도 신호 우세")

        buy_score = iv.get("buy_score", 0.0)
        stage = self._entry_stage.get(code, "NONE")

        # ── ★ Adaptive Signal Filter + Weight 실전 반영 ──
        # signal_type 분류 (진입 전 미리 판별) — 장중 면제 경로는 US_MIDDAY
        from adaptive.trade_recorder import classify_signal
        stage_for_classify = "FULL" if buy_score >= BUY_SCORE_FULL else "EARLY"
        signal_type = classify_signal(
            "US", iv, stage=stage_for_classify,
            midday_exempt=_midday_chase_exempt
        )
        adapted_buy_score = self._apply_adaptive_weight(
            code, name, signal_type, iv, buy_score
        )
        # DISABLED → 진입 금지
        if adapted_buy_score is None:
            return self._skip(code, name,
                               f"[ADAPTIVE] {signal_type} DISABLED — 진입 차단")
        buy_score = adapted_buy_score

        # 장중 면제 경로는 MIDDAY_SCORE_MIN 기준 적용
        _score_min = MIDDAY_SCORE_MIN if _midday_chase_exempt else BUY_SCORE_EARLY
        if buy_score < _score_min and stage == "NONE":
            return self._skip(code, name,
                f"BUY_SCORE={buy_score:.3f} < {_score_min:.2f} (장중면제경로)" if _midday_chase_exempt
                else f"BUY_SCORE={buy_score:.3f} < {_score_min:.2f}")

        # ── [US BUY 판정] 로그 — 매 스캔마다 판정 근거 출력 ──
        vwap     = iv.get("vwap", 0)
        rsi      = iv.get("rsi", 0)
        vol_ratio = iv.get("vol_ratio", 0)
        sell_score = iv.get("sell_score", 0)
        _midday_ok = "✅" if _midday_sig else "❌"
        logger.info(
            f"[US BUY 판정] {name}({code}) | "
            f"현재가=${cur_price:.2f} | VWAP=${vwap:.2f} | "
            f"RSI={rsi:.1f} | 거래량비={vol_ratio:.1f}x | "
            f"장중신호={_midday_ok}({_midday_str}) | "
            f"SELL={sell_score} | BUY={buy_score:.3f} | "
            f"단계={stage} | 진입기준(EARLY≥{BUY_SCORE_EARLY}/FULL≥{BUY_SCORE_FULL})"
        )

        # Early Entry (30%)
        if buy_score >= BUY_SCORE_EARLY and stage == "NONE":
            entry_result = self._place_buy(
                code=code, name=name, exch_cd=exch_cd,
                cur_price=cur_price, ratio=0.30,
                reason=f"US_EARLY(score={buy_score:.2f},signal={signal_type})",
                candles_5m=candles_5m,
                iv=iv,
                now_kst=now_kst,
            )
            if entry_result.get("ok"):
                self._entry_stage[code] = "EARLY"
            return entry_result

        # Full Entry (나머지 70%)
        if buy_score >= BUY_SCORE_FULL and stage == "EARLY":
            entry_result = self._place_buy(
                code=code, name=name, exch_cd=exch_cd,
                cur_price=cur_price, ratio=0.70,
                reason=f"US_FULL(score={buy_score:.2f},signal={signal_type})",
                candles_5m=candles_5m,
                iv=iv,
                now_kst=now_kst,
            )
            if entry_result.get("ok"):
                self._entry_stage[code] = "FULL"
            return entry_result

        return self._skip(
            code, name,
            f"BUY_SCORE={buy_score:.3f} < 임계({BUY_SCORE_EARLY:.2f})"
        )

    # ════════════════════════════════════════════════════════════
    # 전략 B: 볼린저밴드 하단 평균회귀 진입 평가
    # ════════════════════════════════════════════════════════════

    def _eval_entry_b(self,
                      code: str, name: str, exch_cd: str,
                      cur_price: float,
                      candles_5m: list,
                      price_data: dict,
                      now_kst: datetime) -> dict:
        """
        전략 B 진입 조건 평가 — 볼린저밴드 하단 평균회귀.

        전략 A와 독립적으로 실행. 동시 보유 불가 (이미 포지션 있으면 SKIP).
        진입 성공 시 signal_type="B_BB_REVERT", strategy="B" 태깅.
        """
        # 재진입 차단
        blocked, block_info = self.reentry.check("US", code, name)
        if blocked:
            return self._skip(code, name, f"⛔ [B] 재진입 차단 — {block_info.get('block_reason','')}")

        # 자금 사전 확인
        _price_krw = int(cur_price * self.usd_krw)
        _entry_pre = self.account.calc_entry_amount(_price_krw, market="US", usd_krw=self.usd_krw)
        if not _entry_pre.get("can_enter"):
            return self._skip(code, name, f"[B] 자금부족: {_entry_pre.get('block_reason','')}")

        # 전략 B 지표 계산
        iv_b = self._calc_indicators_b(candles_5m, price_data)

        # 조건 미충족 → SKIP (상세 로그)
        if not iv_b["strat_b_ok"]:
            failed = []
            if not iv_b["cond_bb"]:      failed.append(f"BB위치={iv_b['bb_pos']:.2f}(>{_STRAT_B_BB_PROX})")
            if not iv_b["cond_ma20"]:    failed.append(f"MA20이탈(cur={cur_price:.2f}<MA20×{_STRAT_B_MA20_FLOOR})")
            if not iv_b["cond_rsi"]:     failed.append(f"RSI={iv_b['rsi']:.1f}(범위외 {_STRAT_B_RSI_MIN}~{_STRAT_B_RSI_MAX})")
            if not iv_b["cond_vol"]:     failed.append(f"거래량부족({iv_b['cur_vol']:.0f}<avg×{_STRAT_B_VOL_MULT})")
            if not iv_b["cond_nosurge"]: failed.append("급등주감지")
            logger.debug(
                f"[전략B SKIP] {name}({code}) | 미충족={', '.join(failed)}"
            )
            return self._skip(code, name, f"[B] 조건미충족: {', '.join(failed)}")

        # ── 진입 실행 ────────────────────────────────────────────
        bb_l   = iv_b["bb_lower"]
        bb_m   = iv_b["bb_mean"]
        bb_pos = iv_b["bb_pos"]

        logger.info(
            f"[전략B 진입신호] {name}({code}) | "
            f"현재가=${cur_price:.2f} | BB하단=${bb_l:.2f}(위치={bb_pos:.2f}) | "
            f"MA20=${iv_b['ma20']:.2f} | RSI={iv_b['rsi']:.1f} | "
            f"거래량={iv_b['cur_vol']:.0f}/avg={iv_b['avg_vol20']:.0f}"
        )

        # 목표가: BB 중심선 (평균회귀 목표)
        target_pct = (bb_m - cur_price) / cur_price * 100 if cur_price > 0 else 0

        reason = (
            f"B_BB_REVERT(bb_pos={bb_pos:.2f},rsi={iv_b['rsi']:.1f},"
            f"target+{target_pct:.1f}%)"
        )

        return self._place_buy(
            code=code, name=name, exch_cd=exch_cd,
            cur_price=cur_price,
            ratio=1.0,             # 전략 B는 단일 진입 (분할 없음)
            reason=reason,
            candles_5m=candles_5m,
            iv={
                "buy_score": 0.55,   # B전략 고정값 (tracker용)
                "rsi":       iv_b["rsi"],
                "vwap":      bb_m,
                "vol_ok":    iv_b["cond_vol"],
                "vol_surge": False,
                "above_vwap": False,
                "bb_lower":  bb_l,
                "bb_mean":   bb_m,
                "strategy":  "B",    # ★ 전략 구분 태그
            },
            now_kst=now_kst,
        )

    def _place_buy(
                   self,
                   code: str, name: str, exch_cd: str,
                   cur_price: float, ratio: float,
                   reason: str, candles_5m: list,
                   iv: Optional[dict] = None,
                   now_kst: Optional[datetime] = None) -> dict:
        """매수 주문 실행 헬퍼 (USD 환산 → 수량 계산 → ExecutionEngine).

        자금배분 설계 (동적 배정):
          ① 기본배정 = 총자산 × 비중(20~30%) × ratio(Early30%/Full70%)
          ② orderable_usd 확정: TTTS3011R → us_orderable_cache → us_cash_krw → KR예수금 순 폴백
          ③ 배정 동적 결정:
             - 기본배정으로 1주 이상 → 기본배정 사용 (저가주 기존 방식)
             - 기본배정 부족 + orderable_usd >= 1주가격 → 1주 허용 (고가주 진입)
               target = min(orderable_usd, max(비중상한, 1주가격))
             - 모두 부족 → SKIP
          ④ 총자산 40% 초과 초고가주는 _eval_entry에서 이미 차단
          ⑤ 로그: [US_POSITION_SIZE] 포맷으로 통일
        """
        if iv is None:
            iv = {}
        # now_kst 미전달 시 현재 시각으로 폴백 (NameError 방지)
        if now_kst is None:
            now_kst = datetime.now(KST)

        # ── ① 1주 가격(KRW) 계산 ──────────────────────────────────
        price_krw = int(cur_price * self.usd_krw)

        # ── ② AccountSync.calc_entry_amount 호출 (market="US") ──
        entry_info = self.account.calc_entry_amount(
            price_krw, market="US", usd_krw=self.usd_krw
        )
        if not entry_info.get("can_enter"):
            block = entry_info.get("block_reason", "")
            logger.warning(
                f"[US_POSITION_SIZE] 종목={name}({code}) | "
                f"현재가USD=${cur_price:.2f} | 현재가KRW={price_krw:,}원 | "
                f"주문가능USD=- | 기본배정KRW=- | 1주필요KRW={price_krw:,}원 | "
                f"계산수량=0 | 차단여부=Y | 차단사유={block}"
            )
            return self._skip(code, name, f"[자금관리] {block}")

        base_krw = entry_info["entry_amount_krw"]   # 총자산×비중 기준 배정금액(원)
        base_usd = base_krw / self.usd_krw          # KRW → USD

        # ── ③ USD 주문가능금액 확정 (폴백 우선순위 적용) ─────────
        orderable_usd = self.broker.get_orderable_usd(code, exch_cd, cur_price)
        _src = "TTTS3011R"
        if orderable_usd <= 0:
            # ★ 폴백 1순위: broker._orderable_last_ok (장중 성공값 영속 캐시)
            #   us_broker.py의 get_orderable_usd()에서 성공 시 저장한 값
            # ★ 폴백 1순위: broker._orderable_last_ok (장중 성공값 영속 캐시)
            _broker_last_ok = getattr(self.broker, "_orderable_last_ok", 0.0)
            if _broker_last_ok > 0:
                orderable_usd = _broker_last_ok
                _src = f"last_ok(${_broker_last_ok:.0f})"
            else:
                # ★ last_ok도 없으면 → 주문가능금액 불명확 → 기본배정으로만 판단
                #   추정값(KR예수금, 총자산비중 등) 사용 금지
                #   → base_usd < cur_price이면 이후 ④에서 차단됨
                _src = "없음(장외_미확인)"
                orderable_usd = 0.0
                logger.warning(
                    f"[US_CASH_DEBUG] 종목={name}({code}) | "
                    f"TTTS3011R=0 & last_ok=0 → orderable_usd 미확인 | "
                    f"기본배정${base_usd:.0f}으로만 진입 판단 | "
                    f"장중 첫 주문가능 조회 성공 후 정상 진입 가능"
                )

        # ── ④ 배정금액 동적 결정 ─────────────────────────────────
        # 기본배정(총자산×비중×ratio)으로 1주 이상 살 수 있으면 기본배정 사용
        # 기본배정 부족하지만 orderable_usd >= cur_price 이면 → 1주 허용
        # 단, 1주 가격이 총자산 40% 초과 시 차단 (_eval_entry에서 이미 차단되나 방어)
        _total_asset_krw = entry_info.get("total_asset", self.account.total_asset)
        target_usd = base_usd * ratio               # Early=30%, Full=70% 적용

        if target_usd < cur_price:
            # 기본배정으로는 1주 미만 — orderable_usd 기준으로 재계산
            if orderable_usd >= cur_price:
                # orderable_usd 기준으로 target 재산정
                ratio_cap_usd = (_total_asset_krw * entry_info.get("ratio_used", 0.25) * ratio) / self.usd_krw
                target_usd = min(orderable_usd, max(ratio_cap_usd, cur_price))
            else:
                # orderable_usd=0(미확인)이면 명확한 사유로 차단 (추정값 사용 금지)
                if orderable_usd <= 0:
                    _block_reason = (
                        f"US주문가능 미확인(last_ok=0,TTTS3011R=0) | "
                        f"기본배정${base_usd:.0f} < 1주${cur_price:.2f} | "
                        f"장중 첫 TTTS3011R 성공 후 진입 가능"
                    )
                else:
                    _block_reason = f"주문가능USD${orderable_usd:.0f}({_src}) < 1주${cur_price:.2f}"
                logger.warning(
                    f"[US_POSITION_SIZE] 종목={name}({code}) | "
                    f"현재가USD=${cur_price:.2f} | 현재가KRW={price_krw:,}원 | "
                    f"주문가능USD=${orderable_usd:.2f}({_src}) | "
                    f"기본배정KRW={base_krw:,.0f}원(${base_usd:.0f}) | "
                    f"1주필요KRW={price_krw:,}원 | "
                    f"계산수량=0 | 차단여부=Y | 차단사유={_block_reason}"
                )
                return self._skip(code, name, _block_reason)

        # ── ⑤ 최종 수량 계산 ──────────────────────────────────────
        # ★ orderable_usd=0(미확인)이면 target_usd 그대로 사용 (기본배정 기준)
        #   orderable_usd > 0이면 min() 적용 (초과 주문 방지)
        #   기준: _eval_entry에서 이미 _can_afford_basic=True 확인 → 기본배정으로 1주 가능
        if orderable_usd > 0:
            buy_usd = min(target_usd, orderable_usd)
        else:
            # orderable_usd 미확인(장외/TTTS3011R 실패) → 기본배정 사용
            buy_usd = target_usd
            logger.warning(
                f"[US_CASH_WARN] 종목={name}({code}) | "
                f"orderable_usd 미확인({_src}) → 기본배정${target_usd:.0f}으로 주문 진행 | "
                f"장중 TTTS3011R 성공 후에는 실제 주문가능금액 기준으로 자동 전환됨"
            )
        qty = int(buy_usd / cur_price)

        logger.info(
            f"[US_POSITION_SIZE] 종목={name}({code}) | "
            f"현재가USD=${cur_price:.2f} | 현재가KRW={price_krw:,}원 | "
            f"주문가능USD=${orderable_usd:.2f}({_src}) | "
            f"기본배정KRW={base_krw:,.0f}원(${base_usd:.0f},ratio={ratio:.0%}) | "
            f"1주필요KRW={price_krw:,}원 | "
            f"계산수량={qty}주 | "
            f"차단여부={'N' if qty >= 1 else 'Y'} | "
            f"차단사유={'없음' if qty >= 1 else f'buy_usd${buy_usd:.0f}<1주${cur_price:.2f}'}"
        )

        if qty < 1:
            return self._skip(code, name,
                f"수량0차단 | buy_usd=${buy_usd:.0f} < 1주${cur_price:.2f}")

        # 지정가: 호가 단위 보정 (매수는 tick down)
        order_price = USBroker.round_to_tick(cur_price, direction="up")
        ord_dvsn    = ORD_LIMIT

        # 돌파봉 저가 저장
        breakout_low = USBroker.round_to_tick(
            self._get_breakout_low(candles_5m, cur_price), "down"
        )
        signal_time_iso = now_kst.isoformat()   # ★ 신호 발생 시각 (KST)
        order_time_iso  = datetime.now(KST).isoformat()  # 주문 직전 시각

        result = self.executor.execute_buy(
            code=code, name=name,
            price=order_price, qty=qty,
            reason=reason,
            ord_dvsn=ord_dvsn,
            session="US",
            exch_cd=exch_cd,          # ★ US: exch_cd 전달 (NASD/NYSE)
        )

        # ★ BUG FIX: execute_buy() 성공 시 action="BUY" 반환 ("ok" 키 없음)
        #   이전: result.get("ok") → 항상 None → 항상 BUY_FAIL 오판정
        #   수정: action == "BUY" 로 성공 판별
        if result.get("action") == "BUY":
            # 포지션 등록
            self._positions[code] = PositionGuard(
                code=code, name=name,
                avg_price=order_price, qty=qty,
                entry_time=datetime.now(KST),
                breakout_low=breakout_low,
                market="US",
            )
            self._save_positions()
            # ★ execute_buy 반환값에서 order_no 추출
            order_no = result.get("order_no", "") or ""
            # ★ [ORDER_NO_TRACE] 로그
            logger.info(
                f"[ORDER_NO_TRACE] 종목={name}({code}) | "
                f"주문응답 order_no={order_no!r} | "
                f"signal_time={signal_time_iso[11:19]} | "
                f"order_time={order_time_iso[11:19]}"
            )
            # ★ Adaptive Engine: 진입 기록 + iv 캐시 (order_no 포함)
            if self.recorder:
                try:
                    self._entry_iv[code] = iv  # exit 시 재사용
                    # stage 추출: reason에서 FULL/EARLY 판별
                    stage_val = "FULL" if "US_FULL" in reason else "EARLY"
                    self.recorder.record_entry(
                        market       = "US",
                        code         = code,
                        name         = name,
                        price        = float(order_price),
                        qty          = qty,
                        reason       = reason,
                        iv           = iv,
                        stage        = stage_val,
                        signal_time  = signal_time_iso,
                        order_time   = order_time_iso,
                        order_no     = order_no,
                        price_source = "KIS지정가",
                    )
                except Exception as _re:
                    logger.debug(f"[USStrategy] 진입 기록 실패: {_re}")
            # ★ [BUY_OK] 표준 로그 — 실제 주문 성공(포지션 등록) 직후 출력
            stage_label = "US_FULL" if "US_FULL" in reason else "US_EARLY"
            logger.info(
                f"[BUY_OK] 종목={name}({code}) | "
                f"시장=US | "
                f"단계={stage_label} | "
                f"수량={qty}주 | "
                f"진입가=${order_price:.2f} | "
                f"order_no={order_no} | "
                f"사유={reason}"
            )
            return {
                "action":  "BUY_US",
                "code":    code,
                "name":    name,
                "reason":  reason,
                "price":   order_price,
                "qty":     qty,
                "ok":      True,
            }
        return {
            "action": "BUY_FAIL",
            "code":   code, "name": name,
            "reason": result.get("reason", result.get("msg", "BUY 실패")),
            "ok":     False,
        }

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
          - 거래량폭증 (vol_surge)    → vol_bonus
          - 거래량증가 (vol_ok)       → vol_bonus
          - 돌파 가점 (breakout)      → breakout_bonus
          - VWAP / RSI / 기타 지표   → signal_bonus

        안전장치 (절대 변경 금지):
          - 거래시간 / 장마감 / 재진입 / 손실한도 / 수익목표 → 변경 없음

        Returns:
          None  → DISABLED, 진입 차단
          float → 가중치 적용된 BUY_SCORE (LIVE_MIN~LIVE_MAX 범위)
        """
        if self.adjuster is None:
            return base_buy_score  # adjuster 없으면 원본 그대로

        # ① Signal 상태 확인
        allowed, status = self.adjuster.check_signal("US", signal_type)

        # [ADAPTIVE SIGNAL FILTER] 로그
        filter_result = "ALLOW" if allowed else "BLOCK"
        if allowed and status == "WARNING":
            filter_result = "REDUCE"
        logger.info(
            f"[ADAPTIVE SIGNAL FILTER] "
            f"종목={name}({code}) | 시장=US | 전략={signal_type} | "
            f"상태={status} | 결과={filter_result}"
        )

        if not allowed:
            return None  # DISABLED → 진입 차단

        # ② 유효 가중치 조회 (0.80~1.20, WARNING이면 ×0.5)
        live_w = self.adjuster.get_effective_weight("US", signal_type)

        # ③ BUY_SCORE 구성요소별 가중치 적용
        vol_surge    = iv.get("vol_surge", False)
        vol_ok       = iv.get("vol_ok",    False)
        bp           = iv.get("breakout_bonus", 0.0)

        # 거래량 보너스 (score_raw 기준: vol_ok=1, vol_surge=+1 → 합산)
        if vol_surge:
            vol_bonus_raw = 2.0   # vol_ok(1) + vol_surge(1)
        elif vol_ok:
            vol_bonus_raw = 1.0
        else:
            vol_bonus_raw = 0.0

        # 돌파 보너스
        breakout_bonus_raw = bp * 10.0   # 0.0~0.30 → 0~3점

        # VWAP/RSI/MA/BB 등 나머지 기본 지표 점수
        total_raw      = base_buy_score * 10.0  # 역정규화 (max 10점)
        signal_bonus_raw = max(0.0, total_raw - vol_bonus_raw - breakout_bonus_raw)

        # 가중치 적용: 각 보너스 × live_w 후 재합산
        adjusted_raw = (
            signal_bonus_raw             # 기본 지표 그대로
            + vol_bonus_raw      * live_w  # 거래량 보너스 × 학습가중치
            + breakout_bonus_raw * live_w  # 돌파 보너스   × 학습가중치
        )
        adjusted_score = round(max(0.0, min(adjusted_raw / 10.0, 1.0)), 3)

        # [ADAPTIVE WEIGHT APPLIED] 로그
        logger.info(
            f"[ADAPTIVE WEIGHT APPLIED] "
            f"종목={name}({code}) | 시장=US | 전략={signal_type} | "
            f"기본가중치=1.00 | 학습가중치={live_w:.3f} | "
            f"적용전BUY={base_buy_score:.3f} | 적용후BUY={adjusted_score:.3f}"
        )

        return adjusted_score

    # ════════════════════════════════════════════════════════════
    # 청산 평가
    # ════════════════════════════════════════════════════════════

    def _unified_exit(self,
                      code: str, name: str, exch_cd: str,
                      cur_price: float, held_qty: int,
                      avg_price: float, avg_price_src: str,
                      candles_5m: list,
                      price_data: dict,
                      now_kst: datetime) -> dict:
        """
        통합 청산 판단 — 한 루프에서 종목당 단 한 번만 실행.

        판정 우선순위 (순서대로 체크, 최초 매도 신호 발생 시 즉시 실행):
          ① 마감 강제청산 (오버나이트 방지)
          ② 강제 손절 에어백 (-5.0%)
          ③ 트레일링 강제상한 익절 (+10%)
          ④ PositionGuard 손절 (SELL_STOP/SELL_FORCE — 트레일링 무관)
          ⑤ 트레일링 반납 익절 (HWM 대비 -1.5%)
          ⑥ 수익 보호 하한 청산 (트레일링 후 +0.5% 이하)
          ⑦ 오버나이트 예방 (마감 10분 전, net < +1%)
          ⑧ PositionGuard 일반 익절 (SELL_TAKE — 트레일링 미활성 시)
          ⑨ HOLD (매도 사유 없음)
        """
        pnl_pct = (cur_price - avg_price) / avg_price * 100 if avg_price > 0 else 0.0

        # ── PositionGuard 인스턴스 보장 ───────────────────────
        if code not in self._positions:
            saved = self._load_single_position(code)
            if saved:
                entry_time   = saved.get("entry_time_dt", now_kst)
                breakout_low = saved.get("breakout_low", 0.0)
                logger.info(
                    f"[USStrategy] 포지션 영속화 복원 {name}({code}) "
                    f"entry={entry_time.strftime('%H:%M')} "
                    f"breakout_low=${breakout_low:.2f}"
                )
            else:
                from broker.us_broker import _is_dst_kst as _dst_fn2
                open_h = 22 if _dst_fn2(now_kst) else 23
                entry_time   = now_kst.replace(hour=open_h, minute=30, second=0, microsecond=0)
                breakout_low = 0.0
                logger.warning(
                    f"[USStrategy] 포지션 복원 불가 {name}({code}) "
                    f"— entry_time={open_h}:30(추정), breakout_low=0"
                )
            self._positions[code] = PositionGuard(
                code=code, name=name,
                avg_price=avg_price, qty=held_qty,
                entry_time=entry_time,
                breakout_low=breakout_low,
                market="US",
            )
            self._save_positions()
            logger.info(
                f"[USStrategy] 포지션 재구성 {name}({code}) "
                f"avg=${avg_price:.2f} qty={held_qty}"
            )

        pg = self._positions[code]

        # ── ★ [POSITION_MONITOR] 매 루프 보유 상태 로그 ──────────
        _ts_state  = self._trail_state.get(code, {})
        _hwm_log   = _ts_state.get("hwm_pct", 0.0)
        _lwm_log   = _ts_state.get("lwm_pct", 0.0)
        _trail_log = _ts_state.get("active", False)
        logger.info(
            f"[POSITION_MONITOR] "
            f"종목={name}({code}) | "
            f"현재가=${cur_price:.2f} | "
            f"현재수익률={pnl_pct:+.2f}% | "
            f"max_pct={_hwm_log:+.2f}% | "
            f"min_pct={_lwm_log:+.2f}% | "
            f"익절조건=+{_TRAIL_ENTRY_PCT:.1f}%→트레일 | "
            f"손절조건={STOPLOSS_HARD_PCT:.1f}% | "
            f"트레일링상태={'활성(HWM=' + f'{_hwm_log:+.2f}%)' if _trail_log else '비활성'}"
        )

        # ── 시간 기준 계산 ────────────────────────────────────
        from broker.us_broker import _is_dst_kst as _dst_fn
        is_dst      = _dst_fn(now_kst)
        close_t     = dtime(4, 50) if is_dst else dtime(5, 50)
        hard_t      = dtime(5,  0) if is_dst else dtime(6,  0)
        t           = now_kst.time()
        in_overnight_check = t >= close_t and t < hard_t
        in_force_close     = (t >= hard_t and t < dtime(6, 30)) or t < dtime(0, 30)

        # ── 트레일링 상태 ─────────────────────────────────────
        # hwm_pct: 최고수익률(High-Water Mark)  lwm_pct: 최저수익률(Low-Water Mark)
        ts = self._trail_state.setdefault(code, {"hwm_pct": 0.0, "lwm_pct": 0.0, "active": False})
        # 최저수익률 갱신 (매 루프마다)
        if pnl_pct < ts.get("lwm_pct", 0.0):
            ts["lwm_pct"] = pnl_pct
        _trail_active = ts.get("active", False)

        sell_reason   = ""
        force_action  = None   # 최종 매도 트리거

        # ═══════════════════════════════════════════════════════
        # ① 마감 강제청산
        # ═══════════════════════════════════════════════════════
        if in_force_close:
            force_action = "FORCE_CLOSE"
            sell_reason  = "US 마감 강제청산 — 오버나이트 방지"

        # ═══════════════════════════════════════════════════════
        # ② 강제 손절 에어백 (-5.0%)
        # ═══════════════════════════════════════════════════════
        if force_action is None and pnl_pct <= STOPLOSS_HARD_PCT:
            force_action = "STOPLOSS_HARD"
            sell_reason  = (
                f"강제손절에어백(pnl={pnl_pct:+.2f}%"
                f"≤{STOPLOSS_HARD_PCT:.1f}%,src={avg_price_src})"
            )

        # ═══════════════════════════════════════════════════════
        # PositionGuard 평가 (③④⑧에서 공통 사용)
        # ═══════════════════════════════════════════════════════
        if force_action is None:
            iv = self._calc_indicators(candles_5m, price_data)
            elapsed_min = (now_kst - pg.entry_time).total_seconds() / 60
            iv["elapsed_min"] = elapsed_min
            iv["current_pct"] = pnl_pct
            pg_result  = pg.evaluate(cur_price, iv, now_kst)
            pg_action  = pg_result.get("action", "HOLD")
            pg_reason  = pg_result.get("reason", "")
        else:
            iv = {}
            pg_action  = "HOLD"
            pg_reason  = ""

        # ═══════════════════════════════════════════════════════
        # ③ 트레일링 강제상한 (+10%)
        # ═══════════════════════════════════════════════════════
        if force_action is None and pnl_pct >= _TRAIL_HARD_CAP_PCT:
            force_action = "TAKE_PROFIT_HARD_CAP"
            sell_reason  = (
                f"트레일링강제상한익절(pnl={pnl_pct:+.2f}%"
                f"≥+{_TRAIL_HARD_CAP_PCT:.0f}%)"
            )
            logger.info(
                f"[TRAIL EXIT] 종목={name}({code}) | "
                f"현재수익률={pnl_pct:+.2f}% | 최고수익률={ts['hwm_pct']:+.2f}% | "
                f"매도사유=강제상한(+{_TRAIL_HARD_CAP_PCT:.0f}%) 도달"
            )

        # ═══════════════════════════════════════════════════════
        # ④ PositionGuard 손절 (SELL_STOP/SELL_FORCE) — 트레일링 무관
        # ═══════════════════════════════════════════════════════
        if force_action is None and pg_action in ("SELL_STOP", "SELL_FORCE"):
            force_action = pg_action
            sell_reason  = pg_reason

        # ═══════════════════════════════════════════════════════
        # ⑤ 트레일링 익절 로직 (pnl >= _TRAIL_ENTRY_PCT)
        # ═══════════════════════════════════════════════════════
        if force_action is None and pnl_pct >= _TRAIL_ENTRY_PCT:
            if not _trail_active:
                ts["active"]  = True
                ts["hwm_pct"] = pnl_pct
                _trail_active = True
                self._save_positions()
                logger.info(
                    f"[TRAIL START] 종목={name}({code}) | "
                    f"최고수익률={pnl_pct:+.2f}% | "
                    f"트레일링폭={_TRAIL_PULLBACK_PCT:.1f}% | "
                    f"수익보호하한=+{_TRAIL_PROTECT_PCT:.1f}%"
                )
            else:
                if pnl_pct > ts["hwm_pct"]:
                    old_hwm = ts["hwm_pct"]
                    ts["hwm_pct"] = pnl_pct
                    logger.info(
                        f"[TRAIL UPDATE] 종목={name}({code}) | "
                        f"현재수익률={pnl_pct:+.2f}% | "
                        f"최고수익률={old_hwm:+.2f}%→{pnl_pct:+.2f}% (갱신)"
                    )
                hwm      = ts["hwm_pct"]
                pullback = round(hwm - pnl_pct, 4)
                if pullback >= _TRAIL_PULLBACK_PCT and pnl_pct > 0:  # ★ 수익 구간에서만 트레일링 청산
                    force_action = "TAKE_PROFIT_TRAIL"
                    sell_reason  = (
                        f"트레일링반납익절(hwm={hwm:+.2f}%"
                        f"→현재={pnl_pct:+.2f}%,반납={pullback:.2f}%)"
                    )
                    logger.info(
                        f"[TRAIL EXIT] 종목={name}({code}) | "
                        f"현재수익률={pnl_pct:+.2f}% | 최고수익률={hwm:+.2f}% | "
                        f"반납률={pullback:.2f}% | "
                        f"매도사유=HWM대비{_TRAIL_PULLBACK_PCT:.1f}% 반납"
                    )
                else:
                    logger.info(
                        f"[TRAIL UPDATE] 종목={name}({code}) | "
                        f"현재수익률={pnl_pct:+.2f}% | 최고수익률={hwm:+.2f}% | "
                        f"반납률={pullback:.2f}% | HOLD"
                    )

        # ═══════════════════════════════════════════════════════
        # ⑥ 수익 보호 하한 (트레일링 활성 후 +0.5% 이하)
        # ═══════════════════════════════════════════════════════
        if (force_action is None
                and _trail_active
                and 0 < pnl_pct <= _TRAIL_PROTECT_PCT):  # ★ 수익 구간(0%초과)에서만 수익보호하한 발동
            hwm = ts["hwm_pct"]
            force_action = "TAKE_PROFIT_PROTECT"
            sell_reason  = (
                f"수익보호하한청산(hwm={hwm:+.2f}%"
                f"→현재={pnl_pct:+.2f}%≤+{_TRAIL_PROTECT_PCT:.1f}%)"
            )
            logger.info(
                f"[TRAIL EXIT] 종목={name}({code}) | "
                f"현재수익률={pnl_pct:+.2f}% | 최고수익률={hwm:+.2f}% | "
                f"반납률={(hwm - pnl_pct):.2f}% | "
                f"매도사유=수익보호하한(현재≤+{_TRAIL_PROTECT_PCT:.1f}%)"
            )

        # ═══════════════════════════════════════════════════════
        # ⑦ 오버나이트 예방 (마감 10분 전, net < +1%)
        # ═══════════════════════════════════════════════════════
        if force_action is None and in_overnight_check:
            net_p = pg.net_pct(cur_price)
            if net_p < OVERNIGHT_PROFIT_PCT:
                force_action = "OVERNIGHT_PREVENT"
                sell_reason  = f"US 오버나이트 예방 청산 (net={net_p:+.2f}%)"
                logger.info(
                    f"[US 오버나이트] {name}({code}) "
                    f"마감 10분 전 수익 {net_p:+.2f}% < +{OVERNIGHT_PROFIT_PCT}% → 청산"
                )

        # ═══════════════════════════════════════════════════════
        # ⑧ PositionGuard 일반 익절 (트레일링 미활성 시만)
        # ═══════════════════════════════════════════════════════
        if force_action is None and pg_action == "SELL_TAKE" and not _trail_active:
            force_action = "SELL_TAKE_PG"
            sell_reason  = pg_reason
            logger.info(
                f"[TRAIL GUARD 통과] {name}({code}) "
                f"PositionGuard 익절 — 트레일링 미활성 → 즉시 청산 | {pg_reason}"
            )

        # ═══════════════════════════════════════════════════════
        # ★ [EXIT_DECISION] 통합 판정 로그
        # ═══════════════════════════════════════════════════════
        logger.info(
            f"[EXIT_DECISION] "
            f"종목={name}({code}) | "
            f"수익률={pnl_pct:+.2f}% | "
            f"판정경로=UNIFIED_EXIT | "
            f"매도사유={sell_reason or '없음(HOLD)'} | "
            f"결과={force_action or 'HOLD'}"
        )

        # ═══════════════════════════════════════════════════════
        # 매도 실행 또는 HOLD 반환
        # ═══════════════════════════════════════════════════════
        if force_action:
            is_sl = "STOP" in force_action or "STOPLOSS" in force_action
            is_pe = "TAKE" in force_action or "익절" in (sell_reason or "") or "트레일" in (sell_reason or "")
            # ★ [필수 로그 태그] 대시보드 식별용 표준 태그 출력
            if force_action in ("TAKE_PROFIT_HARD_CAP", "TAKE_PROFIT_TRAIL", "SELL_TAKE_PG"):
                logger.info(
                    f"[TAKE_PROFIT] 종목={name}({code}) | "
                    f"수익률={pnl_pct:+.2f}% | 트리거={force_action} | 사유={sell_reason}"
                )
            elif force_action in ("STOPLOSS_HARD", "SELL_STOP", "SELL_FORCE"):
                logger.warning(
                    f"[STOP_LOSS] 종목={name}({code}) | "
                    f"수익률={pnl_pct:+.2f}% | 트리거={force_action} | 사유={sell_reason}"
                )
            elif force_action == "TAKE_PROFIT_PROTECT":
                logger.info(
                    f"[PROFIT_PROTECT] 종목={name}({code}) | "
                    f"수익률={pnl_pct:+.2f}% | 트리거={force_action} | 사유={sell_reason}"
                )
            elif force_action in ("FORCE_CLOSE", "OVERNIGHT_PREVENT"):
                logger.info(
                    f"[TIME_EXIT] 종목={name}({code}) | "
                    f"수익률={pnl_pct:+.2f}% | 트리거={force_action} | 사유={sell_reason}"
                )
            else:
                logger.info(
                    f"[SELL_TRIGGER] 종목={name}({code}) | "
                    f"수익률={pnl_pct:+.2f}% | 트리거={force_action} | 사유={sell_reason}"
                )
            return self._do_sell(
                code=code, name=name, exch_cd=exch_cd,
                qty=held_qty, cur_price=cur_price,
                reason=sell_reason,
                is_stoploss=is_sl,
                is_profit_exit=is_pe,
                now_kst=now_kst,
            )

        # HOLD
        net_p = pg.net_pct(cur_price)
        ts_info = self._trail_state.get(code, {})
        _hwm_info = f" | 최고수익률={ts_info['hwm_pct']:+.2f}%" if ts_info.get("active") else ""
        logger.info(
            f"[US 보유판단] 종목={name}({code}) | "
            f"현재가=${cur_price:.2f} | 평균단가=${avg_price:.4f}({avg_price_src}) | "
            f"현재손익률={net_p:+.2f}%{_hwm_info} | "
            f"트레일링={'ON' if _trail_active else 'OFF'}"
        )
        return {
            "action": "HOLD",
            "code":   code, "name": name,
            "reason": f"HOLD net={net_p:+.2f}% sell_score={iv.get('sell_score', 0)}",
            "ok":     True,
        }

    # ════════════════════════════════════════════════════════════
    # KIS 실잔고 기반 HARD_STOP 에어백
    # ════════════════════════════════════════════════════════════

    def _kis_hard_stop_airbag(self,
                               code: str, name: str, exch_cd: str,
                               kis_qty: int, ord_psbl_qty: int,
                               avg_price: float, cur_price: float,
                               now_kst: datetime) -> dict:
        """
        KIS 실잔고 기반 강제 손절 에어백.

        조건: KIS 실보유수량 > 0 AND 현재손익률 <= STOPLOSS_HARD_PCT(-5.0%)
        → 내부 포지션 여부 무관하게 즉시 SELL_FORCE 실행

        매도 실패 시 사유별 분기 처리:
          - ord_psbl_qty=0: 미체결 SELL 주문 조회 → 취소 후 재시도
          - rt_cd=7: KIS 잔고 강제 재조회 후 재시도
          - API 오류: 30초 후 1회 재시도
          - 계속 실패: [US_HARD_STOP_FAIL] 긴급 로그

        로그 포맷:
          [US_HARD_STOP_AIRBAG] 종목= KIS보유수량= 주문가능수량= 평균단가= 현재가= 손익률= 매도수량= 결과=
          [US_HARD_STOP_FAIL]   종목= 사유= rt_cd= msg= 대응=
        """
        if avg_price <= 0:
            return {"action": "SKIP", "code": code, "reason": "avg_price=0 에어백 스킵"}

        pnl_pct = (cur_price - avg_price) / avg_price * 100

        # ── 에어백 발동 조건: KIS 실보유>0 AND 손익률<=-5% ──────
        if not (kis_qty > 0 and pnl_pct <= STOPLOSS_HARD_PCT):
            return {"action": "SKIP", "code": code,
                    "reason": f"에어백 미발동 kis_qty={kis_qty} pnl={pnl_pct:+.2f}%"}

        sell_qty    = kis_qty
        sell_reason = (f"KIS실잔고강제손절에어백(pnl={pnl_pct:+.2f}%"
                       f"≤{STOPLOSS_HARD_PCT:.1f}%,kis={kis_qty}주)")

        logger.warning(
            f"[US_HARD_STOP_AIRBAG] "
            f"종목={name}({code}) | "
            f"KIS보유수량={kis_qty} | "
            f"주문가능수량={ord_psbl_qty} | "
            f"평균단가=${avg_price:.4f} | "
            f"현재가=${cur_price:.2f} | "
            f"손익률={pnl_pct:+.2f}% | "
            f"매도수량={sell_qty} | "
            f"결과=SELL_FORCE_시도"
        )

        # ── 1차 시도: ord_psbl_qty=0이면 미체결 주문 취소 선행 ─────
        if ord_psbl_qty <= 0:
            logger.warning(
                f"[US_HARD_STOP_AIRBAG] {name}({code}) 주문가능수량=0 "
                f"→ 미체결 SELL 주문 조회·취소 시도"
            )
            try:
                pending = self.broker.get_pending_orders()
                sell_pending = [
                    o for o in pending
                    if o["code"].upper() == code.upper() and o["side"] == "02"
                ]
                if sell_pending:
                    for po in sell_pending:
                        cancelled = self.broker.cancel_order(
                            ord_no=po["ord_no"],
                            code=code,
                            exch_cd=po.get("exch_cd", exch_cd),
                            qty=po["remaining"],
                        )
                        logger.warning(
                            f"[US_HARD_STOP_AIRBAG] {name}({code}) "
                            f"미체결SELL취소 ord_no={po['ord_no']} "
                            f"remaining={po['remaining']} → {'성공' if cancelled else '실패'}"
                        )
                    time.sleep(1.0)   # KIS 처리 대기
                    # 취소 후 잔고 강제 재조회
                    bal_r = self.broker.get_balance(force=True)
                    for h in bal_r.get("holdings", []):
                        if h["code"].upper() == code.upper():
                            ord_psbl_qty = h.get("ord_psbl_qty", h["qty"])
                            sell_qty     = h["qty"]
                            break
                    logger.warning(
                        f"[US_HARD_STOP_AIRBAG] {name}({code}) 취소 후 재조회 "
                        f"→ ord_psbl_qty={ord_psbl_qty}"
                    )
                else:
                    # 미체결 SELL 없는데 ord_psbl_qty=0 → KIS 잔고 강제 재조회
                    logger.warning(
                        f"[US_HARD_STOP_AIRBAG] {name}({code}) 미체결SELL없음 "
                        f"(ord_psbl_qty=0) → 잔고 강제 재조회"
                    )
                    bal_r = self.broker.get_balance(force=True)
                    for h in bal_r.get("holdings", []):
                        if h["code"].upper() == code.upper():
                            ord_psbl_qty = h.get("ord_psbl_qty", h["qty"])
                            sell_qty     = h["qty"]
                            break
            except Exception as _e:
                logger.error(
                    f"[US_HARD_STOP_AIRBAG] {name}({code}) 미체결주문 조회 오류: {_e}"
                )

        # ── 실제 매도 주문 (최대 2회 시도) ────────────────────────
        sell_price = round(cur_price, 2) if cur_price > 0 else 0.0
        result     = None

        for attempt in range(1, 3):   # 1차 → 실패 시 30초 후 2차
            if attempt == 2:
                logger.warning(
                    f"[US_HARD_STOP_AIRBAG] {name}({code}) 1차 실패 "
                    f"→ 30초 대기 후 잔고 재조회 및 2차 시도"
                )
                time.sleep(30)
                # 2차: 잔고 강제 재조회
                bal_r2 = self.broker.get_balance(force=True)
                for h in bal_r2.get("holdings", []):
                    if h["code"].upper() == code.upper():
                        sell_qty     = h["qty"]
                        ord_psbl_qty = h.get("ord_psbl_qty", sell_qty)
                        cur_price_r  = h.get("cur_price", cur_price)
                        if cur_price_r > 0:
                            cur_price  = cur_price_r
                            sell_price = round(cur_price, 2)
                        break
                if sell_qty <= 0:
                    logger.error(
                        f"[US_HARD_STOP_FAIL] "
                        f"종목={name}({code}) | "
                        f"사유=2차재조회후보유수량0 | "
                        f"rt_cd=N/A | "
                        f"msg=KIS잔고에서종목사라짐 | "
                        f"대응=포지션정리후종료"
                    )
                    # 내부 포지션 잔재 제거
                    self._positions.pop(code, None)
                    self._entry_stage.pop(code, None)
                    self._trail_state.pop(code, None)
                    self._save_positions()
                    return {"action": "SELL_FAIL", "code": code,
                            "reason": "hard_stop_airbag_qty_zero_after_retry"}

            try:
                result = self.executor.execute_sell(
                    code=code, name=name,
                    qty=sell_qty, price=sell_price,
                    reason=sell_reason,
                    ord_dvsn=ORD_LIMIT,
                    is_stoploss=True,
                    is_profit_exit=False,
                    session="US",
                    exch_cd=exch_cd,
                )
            except Exception as _e:
                rt_cd = "EXC"
                msg   = str(_e)
                logger.error(
                    f"[US_HARD_STOP_FAIL] "
                    f"종목={name}({code}) | "
                    f"사유=execute_sell_예외 | "
                    f"rt_cd={rt_cd} | "
                    f"msg={msg} | "
                    f"대응={'30초후2차시도' if attempt == 1 else '포기'}"
                )
                continue  # 2차 시도

            rt_cd = result.get("rt_cd", "9")
            msg   = result.get("msg", "")

            if result.get("action") == "SELL":
                # ── 매도 성공 ───────────────────────────────────────
                # 내부 포지션 정리 (이미 _do_sell에서 처리됐을 수도 있지만 방어적으로)
                pg = self._positions.pop(code, None)
                self._entry_stage.pop(code, None)
                self._trail_state.pop(code, None)
                self._save_positions()

                pnl_usd = (cur_price - avg_price) * sell_qty
                pnl_krw = pnl_usd * self.usd_krw
                self.pnl.record(pnl_krw)

                logger.warning(
                    f"[US_HARD_STOP_AIRBAG] "
                    f"종목={name}({code}) | "
                    f"KIS보유수량={kis_qty} | "
                    f"주문가능수량={ord_psbl_qty} | "
                    f"평균단가=${avg_price:.4f} | "
                    f"현재가=${cur_price:.2f} | "
                    f"손익률={pnl_pct:+.2f}% | "
                    f"매도수량={sell_qty} | "
                    f"결과=SELL_OK(시도{attempt}차) "
                    f"pnl={pnl_krw:+,.0f}원(${pnl_usd:+.2f})"
                )
                return {
                    "action":  "SELL_STOP",
                    "code":    code, "name": name,
                    "reason":  sell_reason,
                    "price":   cur_price,
                    "qty":     sell_qty,
                    "ok":      True,
                }

            # ── 매도 실패 — 사유별 분기 ────────────────────────────
            if rt_cd == "7":
                # rt_cd=7: 주문수량 > 주문가능수량
                # 미체결 주문이 남아있거나 수량 계산 오류
                logger.error(
                    f"[US_HARD_STOP_FAIL] "
                    f"종목={name}({code}) | "
                    f"사유=rt_cd=7(주문가능수량초과) | "
                    f"rt_cd=7 | "
                    f"msg={msg!r} | "
                    f"대응={'잔고재조회+2차시도' if attempt == 1 else '긴급포기'}"
                )
                if attempt == 1:
                    # 미체결 SELL 추가 확인 및 취소
                    try:
                        pend2 = self.broker.get_pending_orders()
                        sell_pend2 = [
                            o for o in pend2
                            if o["code"].upper() == code.upper() and o["side"] == "02"
                        ]
                        for po in sell_pend2:
                            self.broker.cancel_order(
                                ord_no=po["ord_no"], code=code,
                                exch_cd=po.get("exch_cd", exch_cd),
                                qty=po["remaining"],
                            )
                        if sell_pend2:
                            time.sleep(1.0)
                    except Exception as _pe:
                        logger.error(f"[US_HARD_STOP_AIRBAG] 미체결취소 오류: {_pe}")
                    continue   # 2차 시도로 이동
                else:
                    break  # 2차도 실패 → 긴급 로그

            elif rt_cd == "0":
                # ok가 False인데 rt_cd=0은 비정상 케이스 — 1차 실패로 처리
                if attempt == 1:
                    continue
                break

            else:
                # 기타 API 오류
                logger.error(
                    f"[US_HARD_STOP_FAIL] "
                    f"종목={name}({code}) | "
                    f"사유=API오류 | "
                    f"rt_cd={rt_cd} | "
                    f"msg={msg!r} | "
                    f"대응={'30초후2차시도' if attempt == 1 else '포기'}"
                )
                if attempt == 1:
                    continue
                break

        # ── 2차까지 실패 → 긴급 로그 ─────────────────────────────
        final_rt  = result.get("rt_cd", "N/A") if result else "N/A"
        final_msg = result.get("msg", "") if result else "execute_sell_exception"
        logger.critical(
            f"[US_HARD_STOP_FAIL] "
            f"종목={name}({code}) | "
            f"사유=2차시도까지SELL실패 | "
            f"rt_cd={final_rt} | "
            f"msg={final_msg!r} | "
            f"대응=수동청산필요 KIS보유수량={kis_qty}주 손익률={pnl_pct:+.2f}%"
        )
        return {
            "action": "SELL_FAIL",
            "code":   code, "name": name,
            "reason": f"hard_stop_airbag_fail rt_cd={final_rt}",
            "ok":     False,
        }


    def _do_sell(self,
                 code: str, name: str, exch_cd: str,
                 qty: int, cur_price: float,
                 reason: str, is_stoploss: bool,
                 is_profit_exit: bool = False,
                 now_kst: Optional[datetime] = None) -> dict:
        """매도 주문 실행 → 포지션 정리 → PnL 기록.

        ★ KIS 해외주식 매도 주문 구분:
          - ORD_MARKET("01") + price=0 → KIS API rt_cd=7 '주문구분 입력오류'
          - KIS 미국 매도는 반드시 ORD_LIMIT("00") + 현재가(USD) 지정가 주문
          - 현재가로 지정가 주문 → 즉시 체결 (슬리피지 최소화)
        """
        # ★ now_kst NameError 방지 — TRADE_REVIEW 로그에서 참조
        if now_kst is None:
            now_kst = datetime.now(KST)
        # ★ KIS 미국 매도: 시장가(01) 불가 → 지정가(00) + 현재가로 즉시 체결
        # cur_price는 USD float ($93.05 등) → round 2자리 유지
        sell_price_usd = round(cur_price, 2) if cur_price > 0 else 0.0

        # ★ [BUG1 FIX] 미체결 SELL 주문 사전 확인 및 취소
        # rt_cd=7 원인: 이전 루프의 SELL 주문이 미체결 상태로 남아 주문가능수량=0
        # → 매도 전 미체결 SELL 주문 자동 취소로 rt_cd=7 무한반복 방지
        try:
            pending = self.broker.get_pending_orders()
            sell_pending = [
                o for o in pending
                if o.get("code", "").upper() == code.upper()
                and o.get("side") == "02"  # 02=매도
            ]
            if sell_pending:
                logger.warning(
                    f"[DO_SELL] {name}({code}) 미체결 SELL 주문 {len(sell_pending)}건 발견 "
                    f"→ 취소 후 재주문 (rt_cd=7 방지)"
                )
                for po in sell_pending:
                    cancelled = self.broker.cancel_order(
                        ord_no=po["ord_no"],
                        code=code,
                        exch_cd=po.get("exch_cd", exch_cd),
                        qty=po["remaining"],
                    )
                    logger.warning(
                        f"[DO_SELL] {name}({code}) 미체결SELL취소 "
                        f"ord_no={po['ord_no']} remaining={po['remaining']} "
                        f"→ {'성공' if cancelled else '실패'}"
                    )
                time.sleep(1.0)  # KIS 처리 대기
        except Exception as _pe:
            logger.debug(f"[DO_SELL] {name}({code}) 미체결주문 조회 실패(무시): {_pe}")

        result = self.executor.execute_sell(
            code=code, name=name,
            qty=qty, price=sell_price_usd,   # USD 지정가 (소수점 2자리)
            reason=reason,
            ord_dvsn=ORD_LIMIT,              # ★ "00" 지정가 (시장가 불가)
            is_stoploss=is_stoploss,
            is_profit_exit=is_profit_exit,
            session="US",
            exch_cd=exch_cd,                 # ★ US: exch_cd 전달 (NASD/NYSE)
        )

        # ★ BUG FIX: execute_sell() 성공 시 action="SELL" 반환 ("ok" 키 없음)
        if result.get("action") == "SELL":
            # 손익 계산 (USD → KRW)
            pg = self._positions.pop(code, None)
            self._entry_stage.pop(code, None)
            cached_iv = self._entry_iv.pop(code, {})
            # ★ trail_state는 TRADE_REVIEW 로그 출력 후 pop (최고/최저수익률 보존)
            _trail_snap = self._trail_state.get(code, {}).copy()
            self._trail_state.pop(code, None)   # ★ 트레일링 상태 초기화
            self._save_positions()
            # ★ [BUG2 FIX] SELL_OK 시점 기록 → KIS 캐시 재등록 grace period 시작
            self._sold_grace[code] = time.time()

            if pg:
                pnl_usd  = (cur_price - pg.avg_price) * qty
                pnl_krw  = pnl_usd * self.usd_krw
                exit_pct = (cur_price - pg.avg_price) / pg.avg_price * 100 if pg.avg_price else 0.0
                self.pnl.record(pnl_krw)

                # ★ [SELL_OK] 표준 로그
                logger.info(
                    f"[SELL_OK] 종목={name}({code}) | "
                    f"시장=US | "
                    f"수량={qty}주 | "
                    f"평균단가=${pg.avg_price:.2f} | "
                    f"매도가=${cur_price:.2f} | "
                    f"실현손익={pnl_krw:+,.0f}원(${pnl_usd:+.2f}) | "
                    f"수익률={exit_pct:+.2f}% | "
                    f"사유={reason}"
                )

                # ★ [TRADE_REVIEW] 진입~청산 전체 요약 로그
                entry_time_str = pg.entry_time.strftime("%H:%M:%S") if pg.entry_time else "?"
                exit_time_str  = now_kst.strftime("%H:%M:%S")
                hold_min       = (now_kst - pg.entry_time).total_seconds() / 60 if pg.entry_time else 0
                # 최고/최저수익률: pop 전에 저장한 trail_snap 사용
                _hwm_pct = _trail_snap.get("hwm_pct", exit_pct)
                _lwm_pct = _trail_snap.get("lwm_pct", exit_pct)
                logger.info(
                    f"[TRADE_REVIEW] 종목={name}({code}) | "
                    f"시장=US | "
                    f"매수시각={entry_time_str} | "
                    f"매도시각={exit_time_str} | "
                    f"보유시간={hold_min:.0f}분 | "
                    f"매수가=${pg.avg_price:.2f} | "
                    f"매도가=${cur_price:.2f} | "
                    f"최고수익률={_hwm_pct:+.2f}% | "
                    f"최저수익률={_lwm_pct:+.2f}% | "
                    f"수익률={exit_pct:+.2f}% | "
                    f"실현손익={pnl_krw:+,.0f}원 | "
                    f"매도사유={reason}"
                )

                # ★ Adaptive Engine: 청산 기록
                if self.recorder:
                    try:
                        self.recorder.record_exit(
                            code        = code,
                            exit_price  = float(cur_price),
                            exit_qty    = qty,
                            exit_reason = reason,
                            exit_pct    = float(exit_pct),
                            pnl_krw     = float(pnl_krw),
                        )
                    except Exception as _re:
                        logger.debug(f"[USStrategy] 청산 기록 실패: {_re}")

            action = "SELL_STOP" if is_stoploss else "SELL_TAKE"
        else:
            action = "SELL_FAIL"
            logger.warning(
                f"❌ [US SELL FAIL] {name}({code}) {qty}주 "
                f"msg={result.get('msg')} | {reason}"
            )
            # ★ [BUG3 FIX] SELL_FAIL도 학습 기록 — 실패한 매도 시도를 EV에 반영
            # KIS 주문 오류로 청산 안됨 → 실제 손실은 포지션에 누적됨
            # recorder에 '미청산' 사유로 기록하여 전략 학습 데이터 유지
            _pg_fail = self._positions.get(code)
            if _pg_fail and self.recorder:
                _fail_exit_pct = (cur_price - _pg_fail.avg_price) / _pg_fail.avg_price * 100 \
                                 if _pg_fail.avg_price > 0 else 0.0
                _fail_pnl_krw  = (_fail_exit_pct / 100) * _pg_fail.avg_price * qty * self.usd_krw
                try:
                    self.recorder.record_exit(
                        code        = code,
                        exit_price  = float(cur_price),
                        exit_qty    = qty,
                        exit_reason = f"SELL_FAIL(미청산)|{reason}",
                        exit_pct    = float(_fail_exit_pct),
                        pnl_krw     = float(_fail_pnl_krw),
                    )
                    logger.info(
                        f"[SELL_FAIL_RECORD] {name}({code}) 학습 기록 "
                        f"pct={_fail_exit_pct:+.2f}% pnl={_fail_pnl_krw:+,.0f}원"
                    )
                except Exception as _rfe:
                    logger.debug(f"[USStrategy] SELL_FAIL 학습 기록 실패: {_rfe}")

        return {
            "action":  action,
            "code":    code, "name": name,
            "reason":  reason,
            "price":   cur_price,
            "qty":     qty,
            "ok":      result.get("ok", False),
        }

    # ════════════════════════════════════════════════════════════
    # 지표 계산
    # ════════════════════════════════════════════════════════════

    def _calc_indicators(self,
                         candles_5m: list,
                         price_data: dict) -> dict:
        """
        BUY_SCORE / SELL_SCORE / 지표 종합 계산.
        미국 주식 특성 반영:
          - USD 단위 (호가단위 $0.01)
          - 거래량 기준 미국장 유동성 (일반적으로 더 큼)
          - 체결강도 대신 price_change_rate 사용

        Returns: {
          "buy_score":    float,   # 0.0 ~ 1.0
          "sell_score":   int,     # 0~7
          "above_vwap":   bool,
          "vol_ok":       bool,
          "vol_surge":    bool,
          "chase_blocked": bool,
          "chase_reason": str,
          "rsi":          float,
          "vwap":         float,
          ...
        }
        """
        if len(candles_5m) < _CANDLE_MIN:
            return {"buy_score": 0.0, "sell_score": 0, "above_vwap": False,
                    "vol_ok": False, "vol_surge": False, "chase_blocked": False}

        candles = candles_5m[:_CANDLE_MAX]
        closes  = [c["close"] for c in candles]
        highs   = [c["high"]  for c in candles]
        lows    = [c["low"]   for c in candles]
        volumes = [c["volume"] for c in candles]
        opens   = [c["open"]  for c in candles]

        cur_price  = price_data.get("cur_price", closes[0])
        cur_vol    = volumes[0]
        prev_vol   = volumes[1] if len(volumes) > 1 else 1
        avg_vol4   = sum(volumes[1:5]) / max(len(volumes[1:5]), 1)

        # VWAP (간이: 종가 × 거래량 가중평균)
        vwap = self._calc_vwap(closes, volumes)

        # RSI
        rsi = self._calc_rsi(closes)

        # 볼린저밴드 (20기간 → 봉 수 부족 시 전체)
        bb_period = min(20, len(closes))
        bb_mean   = sum(closes[:bb_period]) / bb_period
        bb_std    = (sum((c - bb_mean) ** 2 for c in closes[:bb_period])
                     / bb_period) ** 0.5
        bb_upper  = bb_mean + 2 * bb_std
        bb_lower  = bb_mean - 2 * bb_std

        # 거래량 증가 / 폭증
        # [개선 2026-06-28] vol_ok: 직전봉 비교 OR avg_vol4 대비 0.8x 이상
        # yfinance/KIS 5분봉에서 cur_vol=0 리턴 시 false negative 방지
        # 거래량비=0.0x 표시는 cur_vol/avg_vol4 계산 로그용 (별도)
        if cur_vol > 0 and prev_vol > 0:
            vol_ok = cur_vol > prev_vol or cur_vol > avg_vol4 * 0.8
        elif cur_vol > 0:
            vol_ok = cur_vol > avg_vol4 * 0.8
        else:
            vol_ok = False  # 거래량 데이터 자체가 없음
        vol_surge = cur_vol > avg_vol4 * 2.0

        # VWAP 위 여부
        above_vwap = cur_price > vwap if vwap > 0 else False

        # 추격매수 금지 판정
        chase_blocked, chase_reason = self._check_chase(
            closes, opens, candles_5m
        )

        # BUY_SCORE 계산 (6개 기준, 만점 10점 → /10.0)
        score_raw = 0.0

        # 1. MA 추세 (5봉 > 10봉)
        ma5  = sum(closes[:5])  / 5  if len(closes) >= 5  else cur_price
        ma10 = sum(closes[:10]) / 10 if len(closes) >= 10 else cur_price
        if ma5 > ma10:
            score_raw += 1.5

        # 2. RSI (45~65 매수 구간)
        if 45 <= rsi <= 65:
            score_raw += 1.5
        elif 65 < rsi <= 75:
            score_raw += 0.5    # 과열 주의

        # 3. VWAP 위
        if above_vwap:
            score_raw += 2.0

        # 4. 볼린저밴드 (가격 > 중심선)
        if cur_price > bb_mean:
            score_raw += 1.0
        if cur_price > bb_upper * 0.99:   # 상단 돌파 시도
            score_raw += 0.5

        # 5. 거래량
        if vol_ok:
            score_raw += 1.0
        if vol_surge:
            score_raw += 1.0

        # 6. 돌파 가점
        breakout_bonus = self._calc_breakout_bonus(
            closes, highs, volumes, cur_price, avg_vol4, price_data
        )
        score_raw += breakout_bonus * 10.0  # bonus는 0.0~0.30 → 0~3점

        # 7. SELL_SCORE 역방향 감점
        sell_score = self._calc_sell_score(
            closes, volumes, vwap, cur_price, rsi, candles_5m
        )
        if sell_score >= 3:
            score_raw -= 1.0
        if sell_score >= 5:
            score_raw -= 2.0

        # 정규화
        buy_score = max(0.0, min(score_raw / 10.0, 1.0))

        # 거래량비 (로그용)
        vol_ratio = (cur_vol / avg_vol4) if avg_vol4 > 0 else 0.0

        # ── 장중 신호 계산 (Midday Signals) ──────────────────────
        # US 5분봉은 최신봉이 인덱스 0 (내림차순)
        # VWAP 크로스 업: 직전봉은 VWAP 아래, 현재봉은 VWAP 위
        vwap_cross_up = False
        if vwap > 0 and len(closes) >= 2:
            prev_close_for_cross = closes[1]  # US: 인덱스 0=현재, 1=직전
            vwap_cross_up = (
                prev_close_for_cross < vwap * (1 + MIDDAY_VWAP_CROSS_MARGIN)
                and cur_price > vwap
            )

        # 볼린저 상단 재돌파: 현재가 > BB 상단
        bb_upper_cross = (cur_price > bb_upper) if bb_upper > 0 else False

        # 장중 거래량 급증: 직전 4봉 평균 대비 2.5x 이상
        midday_vol_surge = (cur_vol > avg_vol4 * MIDDAY_VOL_MULT) if avg_vol4 > 0 else False

        # 장중 신호 종합 플래그
        midday_signal = vwap_cross_up or bb_upper_cross or midday_vol_surge

        return {
            "buy_score":     buy_score,
            "sell_score":    sell_score,
            "above_vwap":    above_vwap,
            "vol_ok":        vol_ok,
            "vol_surge":     vol_surge,
            "vwap":          vwap,
            "rsi":           rsi,
            "bb_mean":       bb_mean,
            "bb_upper":      bb_upper,
            "bb_lower":      bb_lower,
            "ma5":           ma5,
            "ma10":          ma10,
            "chase_blocked": chase_blocked,
            "chase_reason":  chase_reason,
            "cur_vol":       cur_vol,
            "avg_vol4":      avg_vol4,
            "vol_ratio":     vol_ratio,
            # 장중 신호
            "vwap_cross_up":    vwap_cross_up,
            "bb_upper_cross":   bb_upper_cross,
            "midday_vol_surge": midday_vol_surge,
            "midday_signal":    midday_signal,
        }

    def _calc_breakout_bonus(self,
                             closes: list, highs: list,
                             volumes: list, cur_price: float,
                             avg_vol4: float,
                             price_data: dict) -> float:
        """
        돌파 가점 계산.
          폭발 돌파: +0.30 (당일 고점+0.5%, 거래량×3, 등락률>2%)
          강한 돌파: +0.20 (당일 고점 돌파, 거래량×2)
          초기 돌파: +0.10 (직전봉 고가 돌파, 거래량×1.5)
        """
        if len(closes) < 3:
            return 0.0

        day_high    = price_data.get("high",  closes[0])
        change_pct  = price_data.get("change_pct", 0.0)
        prev_high   = highs[1] if len(highs) > 1 else highs[0]
        cur_vol     = volumes[0]
        prev_vol    = volumes[1] if len(volumes) > 1 else 1

        # 폭발 돌파
        if (cur_price > day_high * 1.005
                and cur_vol > avg_vol4 * 3
                and change_pct > 2.0):
            return 0.30

        # 강한 돌파
        if cur_price > day_high and cur_vol > avg_vol4 * 2:
            return 0.20

        # 초기 돌파
        if cur_price > prev_high and prev_vol > 0 and cur_vol > prev_vol * 1.5:
            return 0.10

        return 0.0

    def _calc_sell_score(self,
                         closes: list, volumes: list,
                         vwap: float, cur_price: float,
                         rsi: float, candles_5m: list) -> int:
        """
        SELL_SCORE 계산 (7개 체크, ≥5면 매도 신호 우세).
        미국 주식 기준으로 RSI/거래량/모멘텀 위주.
        """
        score = 0

        # 1. VWAP 이탈
        if vwap > 0 and cur_price < vwap:
            score += 1

        # 2. RSI 하락 (최근 봉 기준)
        if self._is_rsi_falling(closes):
            score += 1

        # 3. 거래량 감소 (현재 < 직전)
        if len(volumes) >= 2 and volumes[0] < volumes[1]:
            score += 1

        # 4. 최근 5봉 MA 이탈
        if len(closes) >= 5:
            ma5 = sum(closes[:5]) / 5
            if cur_price < ma5 * 0.998:
                score += 1

        # 5. 연속 음봉 (2봉)
        if len(candles_5m) >= 2:
            bearish = sum(
                1 for c in candles_5m[:2]
                if c.get("close", 0) < c.get("open", 0)
            )
            if bearish >= 2:
                score += 1

        # 6. RSI 과매수 (≥75)
        if rsi >= 75:
            score += 1

        # 7. 최근 20분 상승률 미달 (20분=4봉 기준, +0.5% 미달)
        if len(closes) >= 4:
            past_close = closes[3]
            if past_close > 0 and (cur_price - past_close) / past_close * 100 < 0.5:
                score += 1

        return score

    # ════════════════════════════════════════════════════════════
    # 보조 계산 함수
    # ════════════════════════════════════════════════════════════

    def _check_chase(self,
                     closes: list, opens: list,
                     candles_5m: list) -> tuple:
        """추격매수 금지 판정."""
        if len(closes) < 3:
            return False, ""

        cur_price = closes[0]

        # 최근 15분 상승률 (3봉)
        past_15m = closes[2] if len(closes) >= 3 else closes[-1]
        if past_15m > 0:
            rise_15m = (cur_price - past_15m) / past_15m * 100
            if rise_15m > CHASE_RISE_15M:
                return True, f"15분 +{rise_15m:.1f}% > {CHASE_RISE_15M}%"

        # 최근 5분 상승률 (1봉)
        past_5m = closes[1] if len(closes) >= 2 else closes[0]
        if past_5m > 0:
            rise_5m = (cur_price - past_5m) / past_5m * 100
            if rise_5m > CHASE_RISE_5M:
                return True, f"5분 +{rise_5m:.1f}% > {CHASE_RISE_5M}%"

        # 연속 양봉 체크
        if len(candles_5m) >= CHASE_BULL_CNT:
            bull_cnt = sum(
                1 for c in candles_5m[:CHASE_BULL_CNT]
                if c.get("close", 0) >= c.get("open", 0)
            )
            if bull_cnt >= CHASE_BULL_CNT:
                return True, f"연속양봉 {CHASE_BULL_CNT}개"

        return False, ""

    @staticmethod
    def _calc_vwap(closes: list, volumes: list) -> float:
        """간이 VWAP (close × volume 가중평균)."""
        total_vol = sum(volumes)
        if total_vol == 0:
            return 0.0
        return sum(c * v for c, v in zip(closes, volumes)) / total_vol

    @staticmethod
    def _calc_rsi(closes: list, period: int = 14) -> float:
        """RSI 계산."""
        if len(closes) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(period):
            diff = closes[i] - closes[i + 1]
            if diff > 0:
                gains.append(diff)
            else:
                losses.append(-diff)
        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 0.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _is_rsi_falling(closes: list, period: int = 14) -> bool:
        """RSI 하락 추세 여부 (최근 RSI > 이전 RSI)."""
        if len(closes) < period + 3:
            return False
        rsi_now  = USStrategy._calc_rsi(closes[:period + 1])
        rsi_prev = USStrategy._calc_rsi(closes[1:period + 2])
        return rsi_now < rsi_prev

    @staticmethod
    def _get_breakout_low(candles_5m: list, cur_price: float) -> float:
        """돌파봉 저가: 현재가가 고가를 돌파한 첫 봉의 저가."""
        if len(candles_5m) < 2:
            return cur_price * 0.98
        # 첫 번째 봉(최신)의 저가 반환
        return candles_5m[0].get("low", cur_price * 0.98)

    # ════════════════════════════════════════════════════════════
    # 전략 B: 볼린저밴드 하단 평균회귀 지표 계산
    # ════════════════════════════════════════════════════════════

    def _calc_indicators_b(self,
                           candles_5m: list,
                           price_data: dict) -> dict:
        """
        전략 B 전용 지표 계산.
        볼린저밴드 하단 평균회귀 전략 (눌림목 반등 포착).

        진입 조건 (5개 ALL 충족):
          1. BB 하단 근접: cur_price <= BB_lower × 1.02
          2. MA20 유지:   cur_price >= MA20 × 0.995
          3. RSI 40~60:   중립 구간 (극단 회피)
          4. 거래량 증가:  cur_vol >= avg_vol20 × 1.1
          5. 급등주 제외:  change_pct <= 5% AND vol < avg_vol20 × 3

        Returns: {
          "strat_b_ok":    bool,    # 5개 조건 전부 충족 여부
          "rsi":           float,
          "ma20":          float,
          "bb_upper":      float,
          "bb_mean":       float,
          "bb_lower":      float,
          "bb_pos":        float,   # 0=BB하단 1=중심선
          "avg_vol20":     float,
          "cond_bb":       bool,
          "cond_ma20":     bool,
          "cond_rsi":      bool,
          "cond_vol":      bool,
          "cond_nosurge":  bool,
        }
        """
        if len(candles_5m) < _CANDLE_MIN:
            return {"strat_b_ok": False, "cond_bb": False, "cond_ma20": False,
                    "cond_rsi": False, "cond_vol": False, "cond_nosurge": True}

        candles = candles_5m[:max(20, len(candles_5m))]
        closes  = [c["close"]  for c in candles]
        volumes = [c["volume"] for c in candles]

        cur_price  = price_data.get("cur_price", closes[0])
        change_pct = abs(price_data.get("change_pct", 0.0))

        # RSI (14기간)
        rsi = self._calc_rsi(closes)

        # 볼린저밴드 20기간
        bb_period = min(20, len(closes))
        bb_mean   = sum(closes[:bb_period]) / bb_period
        bb_std    = (sum((c - bb_mean)**2 for c in closes[:bb_period]) / bb_period) ** 0.5
        bb_upper  = bb_mean + 2 * bb_std
        bb_lower  = bb_mean - 2 * bb_std

        # MA20
        ma20 = bb_mean  # 동일 계산

        # 거래량 (최근 20봉 평균)
        avg_vol20 = sum(volumes[:bb_period]) / bb_period if bb_period > 0 else 1.0
        cur_vol   = volumes[0] if volumes else 0

        # BB 하단 대비 위치 (0=정확히 하단, 1=중심선, >1=중심선 위)
        bb_pos = (cur_price - bb_lower) / (bb_mean - bb_lower) if (bb_mean - bb_lower) > 0 else 1.0

        # ── 개별 조건 평가 ───────────────────────────────────────
        cond_bb      = cur_price <= bb_lower * _STRAT_B_BB_PROX     # BB 하단 근접
        cond_ma20    = cur_price >= ma20 * _STRAT_B_MA20_FLOOR       # MA20 이상
        cond_rsi     = _STRAT_B_RSI_MIN <= rsi <= _STRAT_B_RSI_MAX  # RSI 40~60
        cond_vol     = cur_vol >= avg_vol20 * _STRAT_B_VOL_MULT      # 거래량 증가
        cond_nosurge = not (
            change_pct > _STRAT_B_SURGE_PCT or                       # 급등주 제외
            (avg_vol20 > 0 and cur_vol > avg_vol20 * _STRAT_B_VOL_SURGE)  # 거래량 폭증 제외
        )

        strat_b_ok = cond_bb and cond_ma20 and cond_rsi and cond_vol and cond_nosurge

        return {
            "strat_b_ok":   strat_b_ok,
            "rsi":          rsi,
            "ma20":         ma20,
            "bb_upper":     bb_upper,
            "bb_mean":      bb_mean,
            "bb_lower":     bb_lower,
            "bb_pos":       round(bb_pos, 3),
            "avg_vol20":    avg_vol20,
            "cur_vol":      cur_vol,
            "cond_bb":      cond_bb,
            "cond_ma20":    cond_ma20,
            "cond_rsi":     cond_rsi,
            "cond_vol":     cond_vol,
            "cond_nosurge": cond_nosurge,
        }



    # ════════════════════════════════════════════════════════════
    # 포지션 영속화
    # ════════════════════════════════════════════════════════════

    def _save_positions(self):
        """포지션 상태를 파일에 저장."""
        try:
            data = {
                "updated": datetime.now(KST).isoformat(),
                "positions": {},
                "entry_stages": self._entry_stage,
                "trail_state":  self._trail_state,   # ★ 트레일링 상태 영속화
            }
            for code, pg in self._positions.items():
                data["positions"][code] = {
                    "code":         pg.code,
                    "name":         pg.name,
                    "avg_price":    pg.avg_price,
                    "qty":          pg.qty,
                    "entry_time":   pg.entry_time.isoformat() if pg.entry_time else None,
                    "breakout_low": pg.breakout_low,
                }
            with open(self._pos_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[USStrategy] 포지션 저장 실패: {e}")

    def _load_positions(self):
        """파일에서 포지션 상태 복원.

        ★ 수정: '오늘 진입만 복원' 제한 제거 — 재시작 후에도 모든 포지션 복원
          KIS 잔고와의 최종 정합성은 sync_positions_from_kis()에서 보장.
        """
        if not os.path.exists(self._pos_file):
            return
        try:
            with open(self._pos_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for code, p in data.get("positions", {}).items():
                qty = int(p.get("qty", 0))
                if qty <= 0:
                    continue
                entry_t = p.get("entry_time", "")
                try:
                    entry_dt = datetime.fromisoformat(entry_t) if entry_t else datetime.now(KST)
                    if entry_dt.tzinfo is None:
                        entry_dt = KST.localize(entry_dt)
                except Exception:
                    entry_dt = datetime.now(KST)
                self._positions[code] = PositionGuard(
                    code          = p["code"],
                    name          = p["name"],
                    avg_price     = float(p.get("avg_price", 0)),
                    qty           = qty,
                    entry_time    = entry_dt,
                    breakout_low  = float(p.get("breakout_low", 0)),
                    market        = "US",
                )
            self._entry_stage  = data.get("entry_stages", {})
            # ★ 트레일링 상태 복원 — V2 재시작 후에도 HWM 유지
            self._trail_state  = data.get("trail_state", {})
            if self._positions:
                trail_on = [c for c, v in self._trail_state.items() if v.get("active")]
                logger.info(
                    f"[USStrategy] 포지션 파일 복원: "
                    f"{list(self._positions.keys())}"
                    + (f" | 트레일링중: {trail_on}" if trail_on else "")
                )
        except Exception as e:
            logger.warning(f"[USStrategy] 포지션 로드 실패: {e}")

    def _load_single_position(self, code: str) -> Optional[dict]:
        """
        ★ B7 추가: 영속화 파일에서 특정 종목 포지션 복원.
        entry_time_dt (datetime, KST), breakout_low (float) 포함.
        없으면 None.
        """
        try:
            if not os.path.exists(self._pos_file):
                return None
            with open(self._pos_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            p = data.get("positions", {}).get(code)
            if not p:
                return None
            entry_t = p.get("entry_time", "")
            if not entry_t:
                return None
            # 오늘 진입분만 복원
            if not entry_t.startswith(date.today().isoformat()):
                return None
            try:
                entry_dt = datetime.fromisoformat(entry_t)
                if entry_dt.tzinfo is None:
                    entry_dt = KST.localize(entry_dt)
            except Exception:
                return None
            return {
                "entry_time_dt": entry_dt,
                "breakout_low":  float(p.get("breakout_low", 0)),
                "avg_price":     float(p.get("avg_price", 0)),
            }
        except Exception:
            return None

    # ════════════════════════════════════════════════════════════
    # 보조 메서드
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _skip(code: str, name: str, reason: str) -> dict:
        return {
            "action": "SKIP",
            "code":   code,
            "name":   name,
            "reason": reason,
            "ok":     True,
        }

    # update_usd_krw: 제거됨 — AccountSync.update_usd_krw() 사용
    # get_positions:  제거됨 — self._positions 직접 접근

    def sync_positions_from_kis(self) -> int:
        """
        KIS 실잔고 기반 포지션 자동 복구.

        V2 재시작 / BUY_FAIL 오판정 등으로 내부 _positions가 비어 있어도
        KIS 실잔고에 있는 종목을 PositionGuard로 복원한다.

        Returns:
            복구된 종목 수 (int)
        """
        try:
            bal = self.broker.get_balance(force=True)
        except Exception as e:
            logger.warning(f"[USStrategy] KIS 잔고 조회 실패 (포지션 복구 불가): {e}")
            return 0

        holdings = bal.get("holdings", [])
        if not holdings:
            return 0

        recovered = 0
        now_kst = datetime.now(KST)

        for h in holdings:
            code      = h.get("code", "")
            name      = h.get("name", code)
            qty       = int(h.get("qty", 0))
            avg_price = float(h.get("avg_price", 0))
            exch_cd   = h.get("exch_cd", EXCH_NASD)

            if not code or qty <= 0:
                continue

            if code in self._positions:
                # 이미 내부 포지션 존재 — 수량만 KIS 기준으로 보정
                pg = self._positions[code]
                if pg.qty != qty:
                    logger.warning(
                        f"[POS SYNC] {name}({code}) 수량 불일치 보정: "
                        f"내부={pg.qty}주 → KIS={qty}주"
                    )
                    pg.qty = qty
                continue

            # 내부 포지션 없음 → KIS 실잔고로 복구
            breakout_low = round(avg_price * 0.98, 4)   # 매수평균 -2% 안전 손절선
            self._positions[code] = PositionGuard(
                code         = code,
                name         = name,
                avg_price    = avg_price,
                qty          = qty,
                entry_time   = now_kst,
                breakout_low = breakout_low,
                market       = "US",
            )
            # 블랙리스트 종목이면 강제청산 필요 명시
            if code in _KIS_ORDER_BLACKLIST:
                logger.warning(
                    f"[POS SYNC] ⚠️ KIS 잔고 기반 포지션 복구 (블랙리스트): "
                    f"{name}({code}) qty={qty} avg=${avg_price:.4f} "
                    f"exch={exch_cd} breakout_low=${breakout_low:.4f} "
                    f"★ 다음 루프에서 강제청산 예정"
                )
            else:
                logger.warning(
                    f"[POS SYNC] ⚠️ KIS 잔고 기반 포지션 복구: "
                    f"{name}({code}) qty={qty} avg=${avg_price:.4f} "
                    f"exch={exch_cd} breakout_low=${breakout_low:.4f}"
                )
            recovered += 1

        if recovered > 0:
            self._save_positions()
            logger.info(
                f"[POS SYNC] ✅ {recovered}개 종목 포지션 복구 완료: "
                f"{[c for c in self._positions]}"
            )
        return recovered
