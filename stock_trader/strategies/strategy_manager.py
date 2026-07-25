"""
통합 전략 매니저 v3 — 복리형 초회전 단타 + 일일 손익 관리
=============================================================

★ 핵심 철학 ★
  목표: 1~2% 수익을 빠르게 반복 실현 → 계좌 복리 성장

  우선순위:
    1. 계좌 생존     — DailyPnLGuard (목표수익/Profit Lock/손실한도)
    2. 수익 반납 방지 — SELL SCORE ≥ 6 + 수익 ≥ +1.0% → 즉시 청산
    3. 빠른 익절     — +2.0% 도달 즉시 전량 매도 (예외 없음)
    4. Early Entry   — BUY SCORE ≥ 0.40 → 30% 선진입 / ≥ 0.55 → +70%
    5. 복리 성장     — 실질 수익 중인 종목만 피라미딩 추가

  ★ 진입 필수 조건 (3가지 모두 만족해야 매수)
    A. 거래량 증가   — 직전 봉 대비 +20% 이상 증가 없으면 진입 금지
    B. VWAP 위       — VWAP(없으면 MA20) 아래이면 진입 금지
    C. SELL_SCORE < 5 — SELL_SCORE ≥ 5 이면 진입 금지 (≥ 6 즉시매도와 별개)

  ★ 일일 손익 관리 (국내장)
    목표수익:   +100,000원
    Profit Lock: +50,000원 (최고 실현손익 도달 후 이 이하 하락 시 신규 진입 차단)
    손실 한도:   -50,000원 (즉시 신규 매수 차단)

  핵심 흐름:
    1. 세션 확인 (휴장 → 스킵)
    2. DailyPnLGuard.can_buy 체크 → False면 신규 BUY 차단
    3. OHLCV + 현재가 + 체결강도 + 이전거래량 수집
    4. IndicatorValidator.validate(candles, vwap, prev_volume, strength)
    5. PyramidStrategyManager.evaluate(buy_score_norm, sell_score) → action 판단
    6. sell_urgent + net_pct ≥ 1.0% → 즉시 강제 매도
    7. 14항목 상세 로그 출력
    8. SELL 체결 후 DailyPnLGuard.record(pnl_krw) 손익 기록
    9. 손절 체결 시 회수금 + 재배분

★ 비용 원칙 ★
  - avg_price = 수수료 포함 주당 취득원가
  - 모든 % 판단 = 실질수익률 (net_profit_pct_from_cost)
  - 복리풀 = 실질 순이익(수수료·세금 차감 후)만 적립
"""

import json
import os
import time
from datetime import datetime
from utils.logger import get_logger
from utils.market_session import session_info, is_tradeable, allow_new_buy_now, BUY_CUTOFF_TIME
from config import Config
from strategies.pyramid_strategy   import PyramidStrategyManager
from strategies.indicator_validator import IndicatorValidator
from strategies.daily_pnl_guard     import DailyPnLGuard
from strategies.reentry_guard       import ReentryGuard
from screener.trade_decision        import TradeDecisionEngine
from screener.transaction_cost      import net_profit_pct_from_cost

# ── Phoenix OrderLifecycle + Execution-driven position update ──────────
try:
    from phoenix.lifecycle import OrderLifecycleManager, make_order_lifecycle_id
    from phoenix.execution_driven import ExecutionDrivenPositionUpdater
    _LIFECYCLE_ENABLED = True
except Exception as _lce:
    _LIFECYCLE_ENABLED = False
    import logging as _logging
    _logging.getLogger("StrategyManager").warning(
        f"[Lifecycle] import 실패 — lifecycle 비활성화: {_lce}"
    )

# ── FillObserver + PendingOrderRegistry (Phase 4) ──────────────────────
try:
    from journal.fill_observer import (
        FillObserver, PendingOrderRegistry, PendingStatus,
        poll_pending_orders_once,
    )
    _FILL_OBSERVER_ENABLED = True
except Exception as _foe:
    _FILL_OBSERVER_ENABLED = False
    import logging as _logging
    _logging.getLogger("StrategyManager").warning(
        f"[FillObserver] import 실패 — fill observer 비활성화: {_foe}"
    )

# ── 거래 저널 import (기록 실패 시 매매 루프 무영향) ──────────
try:
    import journal.trading_journal as _jnl
    _JOURNAL_ENABLED = True
except Exception as _je:
    _JOURNAL_ENABLED = False
    import logging as _logging
    _logging.getLogger("StrategyManager").warning(
        f"[Journal] import 실패 — 저널 기록 비활성화: {_je}"
    )

logger = get_logger("StrategyManager")

TRADE_LOG_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "trade_log.json"
)

# ── 일일 손익 관리 파라미터 (국내장) ────────────────────────
# ★ 국내장 목표: +300,000원 달성 시 신규 매수 차단 (KR_PROFIT_LOCK)
DAILY_TARGET_KRW     = 300_000   # ★ 목표 수익 +30만원
DAILY_PROFIT_LOCK    = 300_000   # 목표 달성 즉시 차단 (target과 동일)
DAILY_LOSS_LIMIT_KRW = -300_000  # 손실 한도


class StrategyManager:

    def __init__(self, kis_api):
        self.api        = kis_api
        self.validator  = IndicatorValidator()
        self.decision   = TradeDecisionEngine()
        self.pyramid    = PyramidStrategyManager(
            kis_api,
            max_per_stock=Config.MAX_INVESTMENT_PER_STOCK,
            max_total    =Config.MAX_TOTAL_INVESTMENT,
        )
        os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)

        # ★ 일일 손익 관리 (국내장) — use_us_session=False (KST 날짜 기준 리셋)
        self.pnl_guard = DailyPnLGuard(
            target_krw      = DAILY_TARGET_KRW,
            profit_lock_krw = DAILY_PROFIT_LOCK,
            loss_limit_krw  = DAILY_LOSS_LIMIT_KRW,
            name            = "국내장",
            use_us_session  = False,   # ★ KST 날짜 기준 리셋
        )

        # ★ 재진입 차단 (국내장/미국장 공통 파일 기반)
        self.reentry = ReentryGuard()

        # ★ Phoenix OrderLifecycle 관리 + Execution-driven 포지션 업데이트
        # apply_buy() / apply_sell() 은 FILLED 전이 시에만 정확히 1회 실행.
        self._lifecycle_mgr = None
        self._updater       = None
        # 미체결 대기 BUY meta: order_lifecycle_id → {name, level, using_compound, is_full_add,
        #                                               buy_score, sell_score, ind_score,
        #                                               trend_score, session, reason, trade_id}
        self._pending_buy_meta: dict  = {}
        # 미체결 대기 SELL meta: order_lifecycle_id → {code, name, qty, price, level, is_full,
        #                                               reason, is_forced, buy_score, sell_score,
        #                                               sell_urgent, trend_score, strength,
        #                                               obv_state, vwap_state, bb_state,
        #                                               elapsed_min, avg_price, max_net_pct,
        #                                               vol_change_pct, session, order_label,
        #                                               indicators, trade_id}
        self._pending_sell_meta: dict = {}
        # ★ Phase 4: PendingOrderRegistry + FillObserver (polling 대상 관리)
        self._pending_registry = None
        self._fill_observer    = None
        if _LIFECYCLE_ENABLED:
            try:
                _jnl_db = os.path.join(
                    os.path.dirname(__file__), "..", "data", "trading_journal.db"
                )
                self._lifecycle_mgr = OrderLifecycleManager(_jnl_db)
                self._updater = ExecutionDrivenPositionUpdater(
                    on_buy_filled  = self._handle_buy_filled,
                    on_sell_filled = self._handle_sell_filled,
                )
                logger.info("[Lifecycle] OrderLifecycleManager 초기화 완료")
            except Exception as _le:
                logger.warning(f"[Lifecycle] 초기화 실패 — lifecycle 비활성화: {_le}")
                self._lifecycle_mgr = None
                self._updater       = None

        if _FILL_OBSERVER_ENABLED:
            try:
                self._pending_registry = PendingOrderRegistry()
                self._fill_observer    = FillObserver(
                    kis_api         = kis_api,
                    phoenix_db_path = None,
                )
                logger.info("[FillObserver] PendingOrderRegistry + FillObserver 초기화 완료")
                # ★ 재시작 복원: load_all_active() → pending meta 재구성
                self._restore_pending_meta_from_lifecycle()
            except Exception as _foe2:
                logger.warning(f"[FillObserver] 초기화 실패: {_foe2}")
                self._pending_registry = None
                self._fill_observer    = None

    # ── 하위 호환: daily_loss_krw 프로퍼티 ──────────────────
    @property
    def daily_loss_krw(self) -> float:
        return self.pnl_guard.realized_pnl

    # ── 하위 호환: app.py 에서 positions 접근 ──────────────
    @property
    def positions(self) -> dict:
        return {code: pos.to_dict()
                for code, pos in self.pyramid.positions.items()}

    # ══════════════════════════════════════════════════════════
    # Phoenix Execution-driven 콜백 (FILLED 시 호출)
    # ══════════════════════════════════════════════════════════

    def _handle_buy_filled(self, lc) -> None:
        """BUY FILLED 시 apply_buy() 정확히 1회 호출.

        OrderLifecycleManager.full_fill(on_filled=updater) 경유로만 호출된다.
        이미 FILLED → OrderLifecycle.full_fill() 멱등 → on_filled 미호출 → 중복 없음.
        """
        meta = self._pending_buy_meta.pop(lc.order_lifecycle_id, None)
        if meta is None:
            logger.warning(
                "[BUY FILLED] pending_buy_meta 없음 — apply_buy 스킵: "
                "order_lifecycle_id=%s code=%s",
                lc.order_lifecycle_id, lc.code,
            )
            return

        code       = lc.code
        name       = meta.get("name", code)
        level      = meta.get("level", 1)
        qty        = lc.filled_qty if lc.filled_qty > 0 else meta.get("qty", 0)
        price      = lc.avg_fill_price if lc.avg_fill_price else meta.get("price", 0.0)
        using_cmpd = meta.get("using_compound", 0)
        is_full_add= meta.get("is_full_add", False)

        if qty <= 0 or price <= 0:
            logger.error(
                "[BUY FILLED] 수량/가격 이상 — apply_buy 스킵: "
                "order_lifecycle_id=%s qty=%s price=%s",
                lc.order_lifecycle_id, qty, price,
            )
            return

        # ── ★ 포지션 반영 ──────────────────────────────────
        self.pyramid.apply_buy(
            code, name, level, qty, price,
            using_compound=using_cmpd,
            is_full_add=is_full_add,
        )

        # trade_id → 포지션에 저장
        _trade_id = meta.get("trade_id", "")
        if _trade_id and code in self.pyramid.positions:
            self.pyramid.positions[code].trade_id = _trade_id
            self.pyramid._save()

        logger.info(
            "[BUY FILLED] apply_buy 완료: code=%s qty=%s @%s level=%s "
            "order_lifecycle_id=%s",
            code, qty, price, level, lc.order_lifecycle_id,
        )

        # ── 거래 로그 (FILLED 시각 기준) ──────────────────
        self._log_trade(
            "BUY", code, name, price, qty,
            meta.get("reason", "BUY FILLED"),
            meta.get("session", ""),
            extra={
                "level":         level,
                "buy_score":     meta.get("buy_score"),
                "sell_score":    meta.get("sell_score"),
                "ind_score":     meta.get("ind_score"),
                "trend_score":   meta.get("trend_score"),
                "compound_pool": self.pyramid.compound_pool,
                "realized_pnl":  self.pnl_guard.realized_pnl,
                "pnl_state":     self.pnl_guard.state,
                "fill_driven":   True,
            },
        )

    def _handle_sell_filled(self, lc) -> None:
        """SELL FILLED 시 apply_sell() + DailyPnLGuard + Cooldown + 실현손익 +
        Pyramid 정리 + JSON 저장 정확히 1회 수행.

        OrderLifecycleManager.full_fill(on_filled=updater) 경유로만 호출된다.
        """
        meta = self._pending_sell_meta.pop(lc.order_lifecycle_id, None)
        if meta is None:
            logger.warning(
                "[SELL FILLED] pending_sell_meta 없음 — apply_sell 스킵: "
                "order_lifecycle_id=%s code=%s",
                lc.order_lifecycle_id, lc.code,
            )
            return

        code    = lc.code
        name    = meta.get("name", code)
        qty     = lc.filled_qty if lc.filled_qty > 0 else meta.get("qty", 0)
        price   = lc.avg_fill_price if lc.avg_fill_price else meta.get("price", 0.0)
        level   = meta.get("level")
        is_full = meta.get("is_full", True)
        reason  = meta.get("reason", "SELL FILLED")
        is_forced = meta.get("is_forced", False)

        if qty <= 0 or price <= 0:
            logger.error(
                "[SELL FILLED] 수량/가격 이상 — apply_sell 스킵: "
                "order_lifecycle_id=%s qty=%s price=%s",
                lc.order_lifecycle_id, qty, price,
            )
            return

        # ── ★ 포지션 반영 ──────────────────────────────────
        profit = self.pyramid.apply_sell(
            code, qty, price, level=level, is_full=is_full
        )
        net_pct_actual = profit.get("net_profit_pct", 0.0)
        net_profit_amt = profit.get("net_profit", 0.0)

        # ── ★ DailyPnLGuard 손익 기록 (상태 자동 평가) ────
        self.pnl_guard.record(net_profit_amt)
        pnl_status = self.pnl_guard.status_dict()

        # ── ★ 재진입 차단 등록 (SELL FILLED 직후) ─────────
        _is_sl = is_forced and "손절" in reason
        self.reentry.record_sell(
            market      = "KR",
            code        = code,
            name        = name,
            reason      = reason,
            is_stoploss = _is_sl,
        )

        logger.info(
            "[SELL FILLED] apply_sell 완료: "
            "code=%s qty=%s @%s level=%s "
            "net_pct=%+.2f%% net_profit=%+.0f원 "
            "pnl_state=%s order_lifecycle_id=%s",
            code, qty, price, level,
            net_pct_actual, net_profit_amt,
            pnl_status["state"], lc.order_lifecycle_id,
        )

        # ── 매도 로그 ─────────────────────────────────────
        self._log_trade(
            "SELL", code, name, price, qty,
            reason, meta.get("session", ""),
            extra={
                "level":        level,
                "profit":       profit,
                "net_pct":      net_pct_actual,
                "is_forced":    is_forced,
                "buy_score":    meta.get("buy_score"),
                "sell_score":   meta.get("sell_score"),
                "sell_urgent":  meta.get("sell_urgent"),
                "trend_score":  meta.get("trend_score"),
                "strength":     meta.get("strength"),
                "obv_state":    meta.get("obv_state"),
                "vwap_state":   meta.get("vwap_state"),
                "bb_state":     meta.get("bb_state"),
                "elapsed_min":  meta.get("elapsed_min"),
                "compound_pool":  self.pyramid.compound_pool,
                "realized_pnl":   pnl_status["realized_pnl"],
                "peak_pnl":       pnl_status["peak_pnl"],
                "pnl_state":      pnl_status["state"],
                "fill_driven":    True,
            },
        )

        # ★ Phase 4: SELL FILLED → 재배분 허용
        # 전량 매도(is_full=True) + pnl_guard가 TRADING 상태이면 재배분 신호 발생
        if is_full and self.pnl_guard.can_buy:
            recycled_cash = net_profit_amt + (price * qty * 0.9975)  # 수수료 제거 근사
            try:
                self._try_recycle_to_strong(
                    recycled_cash = recycled_cash,
                    sold_code     = code,
                    reason        = f"SELL_FILLED_RECYCLE:{reason[:60]}",
                )
            except Exception as _re:
                logger.debug("[SELL FILLED] 재배분 스킵 (오류): %s", _re)

    def dispatch_fill(
        self,
        order_lifecycle_id: str,
        filled_qty: int,
        avg_fill_price: float,
        is_full: bool = True,
    ) -> None:
        """FillObserver → ExecutionObservation 수신 후 lifecycle 전이를 트리거.

        FillObserver.poll_pending_orders_once() 가 수집한 ExecutionObservation 을
        처리하기 위해 app.py 의 메인 루프 (또는 별도 폴링 스레드) 에서 호출한다.

        Args:
            order_lifecycle_id: 체결된 주문의 lifecycle ID
            filled_qty:         이번 회 체결 수량 (delta, 누적 아님)
            avg_fill_price:     평균 체결가
            is_full:            True → full_fill, False → partial_fill (이번 단계 포지션 변경 없음)
        """
        if self._lifecycle_mgr is None:
            logger.warning("[dispatch_fill] lifecycle_mgr 없음 — 스킵")
            return

        lc = self._lifecycle_mgr.load(order_lifecycle_id)
        if lc is None:
            logger.warning(
                "[dispatch_fill] OrderLifecycle 조회 실패: "
                "order_lifecycle_id=%s", order_lifecycle_id,
            )
            return

        try:
            if is_full:
                # on_filled=self._updater → BUY/SELL FILLED 시 apply_buy/apply_sell 1회
                self._lifecycle_mgr.full_fill(
                    lc,
                    delta=filled_qty,
                    avg_price=avg_fill_price,
                    on_filled=self._updater,
                )
            else:
                # 부분체결 — 이번 단계에서는 포지션 변경 없음
                self._lifecycle_mgr.partial_fill(lc, filled_qty, avg_fill_price)
        except Exception as exc:
            logger.error(
                "[dispatch_fill] lifecycle 전이 오류: "
                "order_lifecycle_id=%s is_full=%s error=%s",
                order_lifecycle_id, is_full, exc,
            )
            raise

    # ══════════════════════════════════════════════════════════
    # Phase 4: _register_pending_order() — odno 추출 + PendingRegistry 등록
    # ══════════════════════════════════════════════════════════

    def _register_pending_order(
        self,
        market: str,
        trade_id: str,
        code: str,
        side: str,
        order_qty: int,
        order_response: dict,
        lifecycle_id: str,
        exchange: str = None,
        currency: str = "KRW",
    ) -> str:
        """KIS 주문 접수 응답에서 odno를 추출하고 PendingOrderRegistry에 등록.

        KIS 국내 응답 구조: result["output"]["KNO_ORD_NO"]
        KIS 해외 응답 구조: result["output"]["ODNO"]

        Args:
            market:         "KR" | "US"
            trade_id:       journal trade_id (client_order_id)
            code:           종목코드 / ticker
            side:           "BUY" | "SELL"
            order_qty:      주문 수량
            order_response: KIS buy()/sell() 반환 dict
            lifecycle_id:   OrderLifecycle.order_lifecycle_id
            exchange:       US만 사용 (NASD/NYSE/AMEX)
            currency:       "KRW" | "USD"

        Returns:
            odno (str) — 빈 문자열이면 추출 실패
        """
        if self._pending_registry is None or self._lifecycle_mgr is None:
            return ""

        try:
            output = order_response.get("output", {}) or {}
            if market == "KR":
                odno = str(output.get("KNO_ORD_NO", "") or "").strip()
            else:
                # US: result["output"]["ODNO"]
                odno = str(output.get("ODNO", "") or "").strip()

            from datetime import datetime as _dt
            submitted_at = _dt.now().isoformat()

            # PendingOrderRegistry에 등록
            self._pending_registry.register(
                market             = market,
                trade_id           = lifecycle_id,   # lifecycle_id를 trade_id로 사용
                code               = code,
                side               = side,
                order_qty          = order_qty,
                submitted_at       = submitted_at,
                odno               = odno,
                client_order_id    = trade_id,
                raw_order_response = order_response,
                exchange           = exchange,
                currency           = currency,
            )

            # Lifecycle에도 odno 주입 (accept 시 odno 저장)
            lc = self._lifecycle_mgr.load(lifecycle_id)
            if lc is not None:
                self._lifecycle_mgr.accept(lc, odno=odno)
                logger.info(
                    "[PendingRegistry] 등록 완료: market=%s code=%s side=%s "
                    "odno=%r lifecycle_id=%s",
                    market, code, side, odno, lifecycle_id,
                )
            else:
                logger.warning(
                    "[PendingRegistry] lifecycle 조회 실패 — odno만 등록: "
                    "lifecycle_id=%s odno=%r", lifecycle_id, odno,
                )

            return odno

        except Exception as exc:
            logger.error(
                "[PendingRegistry] 등록 오류: market=%s code=%s side=%s error=%s",
                market, code, side, exc,
            )
            return ""

    # ══════════════════════════════════════════════════════════
    # Phase 4: run_fill_poll() — FillObserver → dispatch_fill() 자동 연결
    # ══════════════════════════════════════════════════════════

    def run_fill_poll(self) -> dict:
        """FillObserver.poll_once() → dispatch_fill() 자동 연결.

        ACCEPTED / PARTIALLY_FILLED 상태의 pending_orders를 KIS API로 1회 체결조회.
        전량 체결된 주문은 dispatch_fill(is_full=True)로 lifecycle 전이 트리거.
        부분 체결된 주문은 dispatch_fill(is_full=False)로 PARTIALLY_FILLED 전이.

        app.py 메인 루프(또는 별도 스레드)에서 주기적으로 호출한다.

        Returns:
            {
              "total": int,    # 조회한 pending 주문 수
              "filled": int,   # 전량 체결 처리 수
              "partial": int,  # 부분 체결 처리 수
              "no_change": int,
              "errors": int,
              "dispatched": list,  # dispatch_fill 호출된 lifecycle_id 목록
            }
        """
        if self._fill_observer is None or self._lifecycle_mgr is None:
            return {
                "total": 0, "filled": 0, "partial": 0,
                "no_change": 0, "errors": 0, "dispatched": [],
            }

        dispatched = []
        try:
            poll_result = self._fill_observer.poll_once()
        except Exception as exc:
            logger.error("[run_fill_poll] poll_once 오류: %s", exc)
            return {
                "total": 0, "filled": 0, "partial": 0,
                "no_change": 0, "errors": 1, "dispatched": [],
            }

        for detail in poll_result.get("details", []):
            lifecycle_id = detail.get("trade_id", "")   # pending_orders.trade_id = lifecycle_id
            fill_delta   = detail.get("fill_delta", 0)
            cum_filled   = detail.get("cum_filled", 0)
            status_after = detail.get("status_after", "")
            error        = detail.get("error")

            if error or fill_delta <= 0:
                continue

            if not lifecycle_id:
                logger.warning("[run_fill_poll] lifecycle_id 없음 — 스킵: %s", detail)
                continue

            # pending_orders에서 order_qty 조회
            pending_row = (self._pending_registry.get_by_trade_id(lifecycle_id)
                           if self._pending_registry else None)
            order_qty   = int((pending_row or {}).get("order_qty", 0) or 0)

            # avg_fill_price는 FillObserver가 obs.average_fill_price로 제공하나
            # detail에는 포함 안 됨 → lifecycle load 후 사용 or 0으로 처리
            # 실제 avg_fill_price는 _poll_one 내부에서 registry.update_fill 전에
            # obs.average_fill_price로 계산됨 — 여기서는 registry 재조회
            avg_fill_price = 0.0
            if pending_row:
                # pending_orders에는 avg_fill_price 컬럼이 없으므로
                # lc에서 avg_fill_price 가져오거나 0 사용
                pass

            # lc를 로드해서 avg_fill_price 취득 시도
            try:
                lc = self._lifecycle_mgr.load(lifecycle_id)
                if lc and lc.avg_fill_price:
                    avg_fill_price = lc.avg_fill_price
            except Exception:
                pass

            is_full = (status_after == PendingStatus.FILLED)
            try:
                self.dispatch_fill(
                    order_lifecycle_id = lifecycle_id,
                    filled_qty         = fill_delta,
                    avg_fill_price     = avg_fill_price,
                    is_full            = is_full,
                )
                dispatched.append(lifecycle_id)
                logger.info(
                    "[run_fill_poll] dispatch_fill 완료: lifecycle_id=%s "
                    "fill_delta=%s is_full=%s",
                    lifecycle_id, fill_delta, is_full,
                )
            except Exception as exc:
                logger.error(
                    "[run_fill_poll] dispatch_fill 오류: lifecycle_id=%s error=%s",
                    lifecycle_id, exc,
                )

        summary = {
            "total":     poll_result.get("total",     0),
            "filled":    poll_result.get("filled",    0),
            "partial":   poll_result.get("partial",   0),
            "no_change": poll_result.get("no_change", 0),
            "errors":    poll_result.get("errors",    0),
            "dispatched": dispatched,
        }
        if dispatched:
            logger.info("[run_fill_poll] 완료: dispatched=%s", dispatched)
        return summary

    # ══════════════════════════════════════════════════════════
    # Phase 4: _restore_pending_meta_from_lifecycle() — 재시작 복원
    # ══════════════════════════════════════════════════════════

    def _restore_pending_meta_from_lifecycle(self) -> None:
        """프로세스 재시작 후 ACCEPTED 상태의 OrderLifecycle을 로드하여
        _pending_buy_meta / _pending_sell_meta를 복원한다.

        OrderLifecycle에는 code, side, order_qty, strategy_name이 저장되어 있으므로
        meta의 필수 필드(code, side, qty)는 복원 가능.
        name / price / reason 등 상세 필드는 lifecycle에 없어 기본값으로 채운다.
        """
        if self._lifecycle_mgr is None:
            return

        try:
            active_lifecycles = self._lifecycle_mgr.load_all_active()
        except Exception as exc:
            logger.warning("[RestoreMeta] load_all_active 실패: %s", exc)
            return

        restored_buy  = 0
        restored_sell = 0

        for lc in active_lifecycles:
            lc_id = lc.order_lifecycle_id
            side  = (lc.side or "BUY").upper()

            # 이미 in-memory에 있으면 스킵 (재시작이 아닌 경우 중복 방지)
            if side == "BUY" and lc_id in self._pending_buy_meta:
                continue
            if side == "SELL" and lc_id in self._pending_sell_meta:
                continue

            # 기본 meta 재구성 — 상세 필드 없이 필수만
            base_meta = {
                "code":     lc.code,
                "qty":      lc.filled_qty if lc.filled_qty > 0 else (lc.order_qty or 0),
                "price":    lc.avg_fill_price or 0.0,
                "trade_id": lc.trade_id or "",
                "session":  "",
                "reason":   "restored_on_restart",
            }

            if side == "BUY":
                base_meta.update({
                    "name":            lc.code,
                    "level":           1,
                    "using_compound":  0,
                    "is_full_add":     False,
                    "buy_score":       None,
                    "sell_score":      None,
                    "ind_score":       None,
                    "trend_score":     None,
                })
                self._pending_buy_meta[lc_id] = base_meta
                restored_buy += 1
            else:
                base_meta.update({
                    "name":      lc.code,
                    "level":     None,
                    "is_full":   True,
                    "is_forced": False,
                    "buy_score": None,
                    "sell_score": None,
                    "sell_urgent": None,
                    "trend_score": None,
                    "strength":  None,
                    "obv_state": None,
                    "vwap_state": None,
                    "bb_state":  None,
                    "elapsed_min": None,
                    "avg_price": 0.0,
                    "max_net_pct": 0.0,
                    "vol_change_pct": 0.0,
                    "order_label": "",
                    "indicators": {},
                })
                self._pending_sell_meta[lc_id] = base_meta
                restored_sell += 1

        if restored_buy or restored_sell:
            logger.info(
                "[RestoreMeta] 재시작 복원 완료: BUY=%d SELL=%d lifecycle 복원됨",
                restored_buy, restored_sell,
            )

    # ══════════════════════════════════════════════════════════
    # 메인 실행
    # ══════════════════════════════════════════════════════════
    def run(self, stock: dict, cached_cash: float = None) -> dict:
        """
        단일 종목에 대한 매매 판단 + 실행.

        Args:
            stock:       {code, name, [score_result: AIScorer.score() 반환값]}
            cached_cash: 루프 시작 시 미리 조회한 현금 잔고 (KIS TPS 절약)

        Returns:
            {action, code, name, ...}
        """
        code = stock["code"]
        name = stock["name"]
        sess = session_info()

        # ══════════════════════════════════════════════════════
        # ★ 국내장 세션 판정 로그 (매 루프, 종목별)
        # ══════════════════════════════════════════════════════
        _allow_buy  = sess.get("allow_new_buy", False)
        _sell_only  = sess.get("sell_only", False)
        _block_rsn  = sess.get("buy_block_reason", "")
        logger.info(
            f"[국내장 세션] "
            f"현재 KST={sess['time_kst']} | "
            f"세션={sess['session']} | "
            f"신규매수허용={'True' if _allow_buy else 'False'} | "
            f"매도허용=True | "
            f"ORD_DVSN={sess.get('order_dvsn', 'N/A')} | "
            f"차단사유={_block_rsn if not _allow_buy else '없음'} | "
            f"종목={name}({code})"
        )

        # ── ★ PnL 상태 항상 INFO 출력 (매 루프, 종목별) ─────
        _pnl = self.pnl_guard
        _pnl._check_date_reset()
        _state_icon = {"TRADING": "🟢", "PROFIT_LOCK": "🔒",
                       "LOSS_LIMIT": "🚫", "HALTED": "⛔"}.get(_pnl.state, "❓")
        _buy_str = "매수가능" if _pnl.state == "TRADING" else "🚫매수차단"

        # ── 보유 포지션 평가손익 계산 (현재가 없으면 0) ──────
        _pos_now = self.pyramid.get_position(code)
        _unrealized = 0.0
        if _pos_now and _pos_now.avg_price > 0:
            try:
                _cur_tmp = self.api.get_current_price(code)
                _cp_tmp  = float(_cur_tmp.get("price", 0) or 0)
                if _cp_tmp > 0:
                    _unrealized = (_cp_tmp - _pos_now.avg_price) * _pos_now.total_qty
            except Exception:
                pass

        # LOSS_LIMIT 판정 근거 한 줄 요약
        if _pnl.state == "LOSS_LIMIT":
            _verdict = (
                f"실현({_pnl.realized_pnl:+,.0f}원)"
                f" ≤ 한도({_pnl.loss_limit_krw:,}원) → LOSS_LIMIT"
            )
        elif _pnl.state == "PROFIT_LOCK":
            _verdict = (
                f"peak({_pnl.peak_pnl:+,.0f}원)≥목표 AND "
                f"실현({_pnl.realized_pnl:+,.0f}원)≤Lock → PROFIT_LOCK"
            )
        else:
            _verdict = (
                f"실현({_pnl.realized_pnl:+,.0f}원)"
                f" > 한도({_pnl.loss_limit_krw:,}원) → TRADING"
            )

        logger.info(
            f"[국내장 PnL] {_state_icon} {_pnl.state} | {_buy_str} | "
            f"실현손익={_pnl.realized_pnl:+,.0f}원(★기준) | "
            f"평가손익={_unrealized:+,.0f}원(참고,미포함) | "
            f"최고실현={_pnl.peak_pnl:+,.0f}원 | "
            f"LOSS_LIMIT판정=[{_verdict}] | "
            f"종목={name}({code})"
            + (f" | ⚠️차단: {_pnl.block_reason()}" if not _pnl.can_buy else "")
        )

        # 1) 휴장
        if not sess["tradeable"]:
            return {"action": "SKIP",
                    "reason": f"휴장({sess['session']})",
                    "session": sess["session"]}

        # 2) OHLCV + 현재가
        candles   = self.api.get_ohlcv(code, period="D", count=200)
        if not candles:
            return {"action": "SKIP", "reason": "OHLCV 없음",
                    "session": sess["session"]}

        cur_data  = self.api.get_current_price(code)
        cur_price = float(cur_data.get("price", 0) or candles[-1]["close"])

        # ★ 당일 장중 고가
        today_high = float(candles[-1].get("high", cur_price)) if candles else cur_price
        today_high = max(today_high, cur_price)

        # ★ 이전 거래량 (직전 봉 거래량) — SELL SCORE 거래량 감소 판단
        prev_volume = float(candles[-2]["volume"]) if len(candles) >= 2 else 0.0
        cur_volume  = float(candles[-1].get("volume", 0))

        # ★ VWAP 추정 (당일 OHLCV 가중 평균)
        last = candles[-1]
        vwap_est = float(last.get("vwap", 0))
        if vwap_est <= 0:
            h = float(last.get("high",  cur_price))
            l = float(last.get("low",   cur_price))
            c = float(last.get("close", cur_price))
            vwap_est = (h + l + c) / 3.0  # 전형적 가격 대용

        # ★ 체결강도 (API에서 제공하면 사용, 없으면 0)
        strength = float(cur_data.get("strength", cur_data.get("체결강도", 0)))

        # ★ 5분봉 조회 (돌파 가점 + 추격매수 차단용)
        candles_5m = self.api.get_intraday_5min(code, count=12)
        iv5 = self.validator.validate_5min(candles_5m,
                                           today_high=today_high,
                                           strength=strength)
        breakout_bonus  = iv5["breakout_bonus"]
        breakout_label  = iv5["breakout_label"]
        chase_blocked   = iv5["chase_blocked"]
        chase_reason    = iv5["chase_reason"]

        # 3) 보조지표 + 추세강도 검증 (vwap/prev_volume/strength 전달)
        iv           = self.validator.validate(candles,
                                               vwap=vwap_est,
                                               prev_volume=prev_volume,
                                               strength=strength)
        ind_score    = iv["score"]             # 매수 신호 지표 수 (0~7)
        buy_score    = iv.get("buy_score_norm", ind_score / 7.0)  # 정규화 0~1 (거래량 보너스 포함)

        # ★ 5분봉 돌파 가점 반영 (최대 1.0 캡)
        _score_before = iv.get("buy_score_norm", ind_score / 7.0)
        buy_score = round(min(_score_before + breakout_bonus, 1.0), 2)
        if breakout_bonus > 0:
            logger.info(
                f"[5분봉 돌파가점] 종목={name}({code}) | "
                f"{breakout_label} | "
                f"기본BUY_SCORE={_score_before:.2f} → 최종={buy_score:.2f} | "
                f"vol_ratio={iv5['vol_ratio_5m']:.1f}배 | "
                f"vol_avg4={iv5['vol_avg4_ratio']:.1f}배 | "
                f"체결강도={strength:.0f}"
            )

        sell_score   = iv["sell_score"]        # 가중치 합산 (0~27)
        sell_urgent  = iv.get("sell_urgent", False)   # True if ≥ 6
        trend_score  = iv["trend_score"]
        strong_trend = iv["strong_trend"]
        sell_detail  = iv.get("sell_detail", {})

        # ── ★ 진입 필수 조건 플래그 (indicator_validator에서 계산된 값) ──
        buy_blocked_vol  = iv.get("buy_blocked_vol",  False)   # 거래량 증가 없음
        buy_blocked_vwap = iv.get("buy_blocked_vwap", False)   # VWAP 아래
        buy_blocked_sell = iv.get("buy_blocked_sell", False)   # SELL_SCORE ≥ 5
        vol_label        = iv.get("vol_label", "N/A")
        vwap_above       = iv.get("vwap_above", True)

        # score_result에 정보 합산
        score_result = stock.get("score_result", {})
        score_result.update({
            "cur_price":      cur_price,
            "price_ma20":     iv["detail"].get("MA", {}).get("value", {}).get("MA20", 0),
            "trend_score":    trend_score,
            "strong_trend":   strong_trend,
            "buy_score_norm": buy_score,
            "sell_score":     sell_score,
        })

        # 4) 현금 잔고 확인
        if cached_cash is not None and cached_cash > 0:
            cash = cached_cash
        else:
            balance = self.api.get_balance()
            cash    = float(balance.get("cash", 0))

        # 5) 피라미딩 전략 판단 (buy_score_norm + sell_score 전달)
        # ── [훅 14 준비] evaluate 전 고가/저가 스냅샷 (PRICE_HIGH/LOW_UPDATED 감지용) ──
        _pre_eval_pos  = self.pyramid.positions.get(code)
        _pre_highest   = _pre_eval_pos.highest_price if _pre_eval_pos else 0.0
        _pre_lowest    = _pre_eval_pos.lowest_price  if _pre_eval_pos else float("inf")
        _pre_trade_id  = _pre_eval_pos.trade_id      if _pre_eval_pos else ""

        decision  = self.pyramid.evaluate(
            code, name, cur_price, ind_score, cash,
            today_high=today_high,
            buy_score_norm=buy_score,
            sell_score=sell_score,
        )
        action    = decision.get("action", "HOLD")
        net_pct   = decision.get("net_pct", 0.0)

        # ── [훅 14] PRICE_HIGH_UPDATED / PRICE_LOW_UPDATED ──
        if _JOURNAL_ENABLED and _pre_trade_id:
            _post_pos = self.pyramid.positions.get(code)
            if _post_pos:
                try:
                    if _post_pos.highest_price > _pre_highest:
                        # ★ prev_high 전달 → 1호가 미만 갱신 시 이벤트 기록 생략
                        _jnl.record_price_high(
                            _pre_trade_id, "KR", code,
                            new_high  = _post_pos.highest_price,
                            prev_high = _pre_highest,
                        )
                except Exception as _je:
                    _jnl._inc_error("kr_price_high", _je)
                try:
                    if _post_pos.lowest_price < _pre_lowest:
                        # ★ prev_low 전달 → 1호가 미만 갱신 시 이벤트 기록 생략
                        _jnl.record_price_low(
                            _pre_trade_id, "KR", code,
                            new_low  = _post_pos.lowest_price,
                            prev_low = _pre_lowest,
                        )
                except Exception as _je:
                    _jnl._inc_error("kr_price_low", _je)

        # ── ★ 신규 매수 시 필수 조건 차단 ────────────────────
        # 기존 보유 포지션 매도/홀드는 차단하지 않음 — BUY 계열만 차단
        if action in ("BUY_LEVEL1_EARLY", "BUY_LEVEL1_FULL",
                      "BUY_LEVEL2", "BUY_LEVEL3"):
            if buy_blocked_vol:
                action = "SKIP"
                decision["reason"] = f"⛔거래량 증가 없음 — 진입 금지 ({vol_label})"
            elif buy_blocked_vwap:
                action = "SKIP"
                decision["reason"] = f"⛔VWAP 아래 — 진입 금지 (현재가{cur_price:.0f} < VWAP{vwap_est:.0f})"
            elif buy_blocked_sell:
                action = "SKIP"
                decision["reason"] = f"⛔SELL_SCORE={sell_score} ≥ 5 — 매수 금지"
            elif chase_blocked:
                # 추격매수 차단 (과열 종목 선점 방지)
                action = "SKIP"
                decision["reason"] = f"⛔과열 추격매수 차단 — {chase_reason}"
                logger.warning(
                    f"[과열 추격매수 차단] "
                    f"종목={name}({code}) | "
                    f"15분상승률={iv5['rise_15m_pct']:+.2f}% | "
                    f"5분상승률={iv5['rise_5m_pct']:+.2f}% | "
                    f"연속양봉수={iv5['consec_bull']} | "
                    f"사유={chase_reason}"
                )

        # ── ★ 재진입 차단 체크 (매도 후 24h/72h 쿨다운) ─────────
        # BUY 계열이고 아직 SKIP 되지 않은 경우에만 체크
        if action in ("BUY_LEVEL1_EARLY", "BUY_LEVEL1_FULL",
                      "BUY_LEVEL2", "BUY_LEVEL3"):
            _re_blocked, _re_info = self.reentry.check("KR", code, name)
            # ★ 항상 로그 출력 (blocked/allowed 무관) — 차단 미동작 추적용
            ReentryGuard.log_check("KR", code, name, _re_blocked, _re_info)
            if _re_blocked:
                ReentryGuard.log_block(_re_info)
                action = "SKIP"
                decision["reason"] = (
                    f"⛔재진입 차단 — {_re_info['block_reason']} "
                    f"(잔여 {_re_info['remaining_hours']:.1f}h)"
                )

        # ── 세션별 BUY 지표 기준 ──────────────────────────
        # ★ 복리형 초회전 단타: cutoff=0 (지표 0개도 진입 허용)
        cutoff_map = {
            "장전시간외": 0, "정규장시작": 0,
            "정규장":     0, "정규장마감": 0,
            "장후시간외": 0,
        }
        ind_cutoff = cutoff_map.get(sess["session"], 0)

        # ★ SKIP 이유 표시
        skip_reason = decision.get("reason", "") if action == "SKIP" else ""

        # ── 포지션 정보 수집 (로그용) ─
        pos = self.pyramid.get_position(code)
        avg_price    = pos.avg_price    if pos else 0.0
        total_qty    = pos.total_qty    if pos else 0
        highest_price= pos.highest_price if pos else 0.0
        max_net_pct  = net_profit_pct_from_cost(avg_price, highest_price) if (pos and avg_price > 0 and highest_price > 0) else 0.0

        # 보유 시간
        elapsed_min = decision.get("elapsed_min", 0.0)
        if pos:
            try:
                from datetime import timedelta as _td
                created_at = datetime.fromisoformat(pos.created_at)
                elapsed_min = (datetime.now() - created_at).total_seconds() / 60
            except Exception:
                pass

        # OBV / VWAP / BB 상태 추출
        obv_state  = iv["detail"].get("OBV",  {}).get("signal", "?")
        vwap_state = "ABOVE✅" if vwap_above else "BELOW⛔"
        bb_detail  = iv["detail"].get("BB",   {})
        bb_state   = bb_detail.get("signal", "?")
        bb_sqz     = iv["detail"].get("BB_SQZ", {}).get("signal", "?")

        # 거래량 변화율
        vol_change_pct = ((cur_volume - prev_volume) / prev_volume * 100
                          if prev_volume > 0 else 0.0)

        # ── 14항목 강화 로그 출력 ──────────────────────────
        pnl_status = self.pnl_guard.status_dict()
        logger.info(
            f"[{sess['icon']} {sess['session']}] {name}({code}) | "
            f"현재가={cur_price:,.0f}원 | "
            f"BUY_SCORE={buy_score:.2f}(early≥0.40/full≥0.55) | "
            f"SELL_SCORE={sell_score}/27{'🚨' if sell_urgent else ('⛔매수금지' if buy_blocked_sell else '')} | "
            f"거래량={vol_label} | VWAP={vwap_state} | "
            f"실질수익={net_pct:+.2f}% | 최고수익={max_net_pct:+.2f}% | "
            f"OBV={obv_state} | BB={bb_state}{'(SQZ)' if bb_sqz=='BUY' else ''} | "
            f"경과={elapsed_min:.0f}분 | "
            f"일일손익={pnl_status['realized_pnl']:+,.0f}원 "
            f"(최고={pnl_status['peak_pnl']:+,.0f}원, 상태={pnl_status['state']}) | "
            f"action={action}"
            + (f" [{skip_reason}]" if skip_reason else "")
        )

        # ── ★ DailyPnLGuard: BUY 계열 차단 체크 ─────────
        if action.startswith("BUY") and not self.pnl_guard.can_buy:
            block_msg = self.pnl_guard.block_reason()
            logger.warning(f"🚫 신규 매수 차단 → {name}: {block_msg}")
            return {
                "action":      "SKIP",
                "code":        code, "name": name,
                "reason":      block_msg,
                "session":     sess["session"],
                "pnl_state":   self.pnl_guard.state,
                "realized_pnl": self.pnl_guard.realized_pnl,
                "peak_pnl":    self.pnl_guard.peak_pnl,
            }

        # ── ★ 15:20 이후 신규 매수 절대 차단 (세션 + 시각 이중 방어) ──
        if action.startswith("BUY"):
            _sess_allow = sess.get("allow_new_buy", False)
            _time_allow = allow_new_buy_now()  # 시각 직접 체크 (이중 안전망)
            if not _sess_allow or not _time_allow:
                _blk = sess.get("buy_block_reason") or f"15:20 이후 신규 매수 차단 ({sess['time_kst']} KST)"
                logger.warning(
                    f"🚫 [시간 초과 매수 차단] {name}({code}) → {_blk} | "
                    f"세션={sess['session']} | "
                    f"allow_new_buy(세션)={_sess_allow} | "
                    f"allow_new_buy(시각)={_time_allow} | "
                    f"BUY_CUTOFF={BUY_CUTOFF_TIME.strftime('%H:%M')} KST"
                )
                return {
                    "action":  "SKIP",
                    "code":    code, "name": name,
                    "reason":  _blk,
                    "session": sess["session"],
                }

        # ── ★ SELL SCORE 수익 반납 방지 강제 매도 ─────────
        # sell_urgent(≥6) + 포지션 수익 ≥ +1.0% → 즉시 청산
        if (pos is not None and sell_urgent and
                net_pct >= 1.0 and action == "HOLD"):
            action = "SELL_ALL"
            decision = {
                "action":  "SELL_ALL",
                "qty":     pos.total_qty,
                "price":   cur_price,
                "code":    code,
                "name":    name,
                "level":   pos.current_level,
                "net_pct": round(net_pct, 2),
                "reason":  (f"🚨SELL SCORE 수익반납방지(score={sell_score}≥6, "
                            f"실질{net_pct:.2f}%≥1.0%) — "
                            + ", ".join(
                                f"{k}(+{v['weight']})" for k, v in sell_detail.items()
                                if v.get("triggered")
                            )),
            }
            logger.warning(
                f"🚨 {name} SELL SCORE 즉시 매도! score={sell_score}, "
                f"실질{net_pct:.2f}%"
            )

        # ── BUY 계열 ──────────────────────────────────────
        if action.startswith("BUY"):
            level = decision["level"]

            if ind_score < ind_cutoff:
                return {
                    "action":      "HOLD",
                    "code":        code, "name": name,
                    "price":       cur_price,
                    "buy_score":   buy_score,
                    "sell_score":  sell_score,
                    "ind_score":   ind_score,
                    "trend_score": trend_score,
                    "session":     sess["session"],
                    "reason":      (f"지표 부족({ind_score}/{ind_cutoff}) — "
                                    f"{decision['reason']}"),
                    "indicators":  iv,
                }

            # ★★★ 주문 직전 최종 방어: 15:20 이후 신규 매수 절대 차단 ★★★
            if not allow_new_buy_now():
                _final_blk = (
                    f"주문직전 시각체크 — 15:20 이후 신규 매수 차단 "
                    f"({sess['time_kst']} KST, BUY_CUTOFF={BUY_CUTOFF_TIME.strftime('%H:%M')})"
                )
                logger.warning(
                    f"🚫🚫 [주문직전 최종차단] {name}({code}) → {_final_blk}"
                )
                return {
                    "action":  "SKIP",
                    "code":    code, "name": name,
                    "reason":  _final_blk,
                    "session": sess["session"],
                }

            qty      = decision["qty"]
            price    = decision["price"]
            ord_dvsn = sess["order_dvsn"]

            # ── ★ BUY 주문가격 세팅 ───────────────────────────────
            # 정규장(00):  지정가 → 현재가를 호가단위 올림 처리 (절대 0원 금지)
            # 장전시간외(05): 전일종가 지정가
            # 장후시간외(06): 매수 자체 금지 (_order()에서도 이중 차단)
            # 시장가(01):  0원 고정
            from api.kis_api import KISApi as _KisApi
            if ord_dvsn == "05":
                use_price = price if price > 0 else int(cur_price)
            elif ord_dvsn == "06":
                # 장후시간외 매수 금지 — _order()에서도 이중 차단
                logger.warning(f"⚠️ 장후시간외(06) BUY 차단: {name}({code})")
                return {
                    "action":  "SKIP",
                    "code":    code, "name": name,
                    "reason":  "장후시간외 신규 매수 금지",
                    "session": sess["session"],
                }
            elif ord_dvsn == "01":
                use_price = 0  # 시장가
            else:
                # 정규장 지정가(00): 현재가 기준 1호가 위 올림 → 빠른 체결
                _base_price = int(cur_price) if cur_price > 0 else int(price)
                _tick       = _KisApi.tick_size(_base_price)
                use_price   = _KisApi.round_to_tick(_base_price + _tick, direction=1)
                if use_price <= 0:
                    use_price = _base_price  # 안전망

            # ══════════════════════════════════════════════════════
            # ★ [국내장 BUY 사전검증] 주문 직전 잔고 충분 여부 확인
            # ══════════════════════════════════════════════════════
            _est_order_amt = (use_price if use_price > 0 else int(cur_price)) * qty
            _avail_cash    = cash if cash is not None else 0.0
            _cash_ok       = (_avail_cash >= _est_order_amt * 0.95)  # 5% 슬리피지 허용
            logger.info(
                f"[국내장 BUY 사전검증] 종목={name}({code}) | "
                f"주문수량={qty}주 | ORD_DVSN={ord_dvsn} | ORD_UNPR={use_price}원 | "
                f"현재가={cur_price:.0f}원 | "
                f"예상주문금액={_est_order_amt:,}원 | "
                f"주문가능현금={_avail_cash:,.0f}원 | "
                f"잔고충분={'✅OK' if _cash_ok else '⚠️부족'}"
            )
            if not _cash_ok:
                logger.warning(
                    f"⚠️ [국내장 BUY 잔고부족] {name}({code}) 주문 스킵 | "
                    f"필요={_est_order_amt:,}원 > 가용={_avail_cash:,.0f}원"
                )
                return {
                    "action":  "SKIP",
                    "code":    code, "name": name,
                    "reason":  f"잔고부족 (필요={_est_order_amt:,}원 > 가용={_avail_cash:,.0f}원)",
                    "session": sess["session"],
                }

            # ── [훅 1/2] SIGNAL_CONFIRMED + trade_id 생성 ────────
            _trade_id = ""
            if _JOURNAL_ENABLED:
                try:
                    _trade_id = _jnl.make_trade_id("KR", code)
                    # screener 데이터 조회 (실패해도 NULL 저장)
                    _ai_score, _rs_val = None, None
                    try:
                        from screener.screener_db import ScreenerDB as _SDB
                        _sdb = _SDB()
                        _row = _sdb.get_today_score(code)
                        if _row:
                            _ai_score = _row.get("total_score")
                            _rs_val   = _row.get("rs_value")
                    except Exception:
                        pass
                    _detail = iv.get("detail", {})
                    _rsi_v  = _detail.get("RSI", {}).get("value", {}).get("RSI")
                    _bb_v   = _detail.get("BB",  {}).get("value", {})
                    _atr_v  = _detail.get("ATR", {}).get("value", {})
                    _vol_r  = iv5.get("vol_ratio_5m") if iv5 else None
                    _jnl.record_signal(
                        trade_id        = _trade_id,
                        market          = "KR",
                        code            = code,
                        name            = name,
                        entry_type      = action,
                        signal_price    = cur_price,
                        buy_score       = buy_score,
                        sell_score      = sell_score,
                        rsi             = _rsi_v,
                        bb_upper        = _bb_v.get("상단"),
                        bb_middle       = _bb_v.get("중심"),
                        bb_lower        = _bb_v.get("하단"),
                        atr             = _atr_v.get("ATR14"),
                        volume          = cur_volume,
                        volume_ratio    = _vol_r,
                        ai_total_score  = _ai_score,
                        rs_value        = _rs_val,
                        orderable_cash  = cash,
                        session         = sess["session"],
                        entry_reason    = decision["reason"],
                    )
                except Exception as _je:
                    _jnl._inc_error("kr_signal", _je)

            # ── [훅 3] ORDER_SUBMITTED: api.buy() 호출 직전 ───────
            if _JOURNAL_ENABLED and _trade_id:
                try:
                    _jnl.record_order_submitted(
                        trade_id    = _trade_id,
                        market      = "KR",
                        code        = code,
                        order_price = use_price,
                        order_qty   = qty,
                        payload     = {"ord_dvsn": ord_dvsn, "action": action},
                    )
                except Exception as _je:
                    _jnl._inc_error("kr_order_submitted", _je)

            result = self.api.buy(code, qty, use_price, ord_dvsn=ord_dvsn)
            order_ok = result.get("rt_cd") == "0"

            if not order_ok:
                # ── [훅 4] ORDER_REJECTED ────────────────────────
                if _JOURNAL_ENABLED and _trade_id:
                    try:
                        _is_dry = result.get("_dry_run", False)
                        _jnl.record_order_rejected(
                            trade_id = _trade_id,
                            market   = "KR",
                            code     = code,
                            rt_cd    = result.get("rt_cd", "?"),
                            msg1     = result.get("msg1", ""),
                            payload  = {"_dry_run": _is_dry,
                                        "_live_disabled": result.get("_live_disabled", False)},
                        )
                    except Exception as _je:
                        _jnl._inc_error("kr_order_rejected", _je)

                logger.warning(
                    f"⚠️ {code} 주문 응답 이상 "
                    f"rt_cd={result.get('rt_cd','?')} "
                    f"msg_cd={result.get('msg_cd','?')} "
                    f"msg1={result.get('msg1','?')!r} "
                    f"— 3초 후 잔고 재확인..."
                )
                time.sleep(3)
                try:
                    balance  = self.api.get_balance()
                    holdings = {h["code"]: h for h in balance.get("holdings", [])}
                    if code in holdings:
                        h = holdings[code]
                        actual_qty   = int(h.get("qty", qty))
                        actual_price = float(h.get("avg_price", price))
                        logger.info(
                            f"✅ {code}({name}) 잔고 확인 → 실제 체결됨 "
                            f"{actual_qty}주 @{actual_price:,.0f}원 — 포지션 자동 등록"
                        )
                        if code not in self.pyramid.positions:
                            is_full_add = (action == "BUY_LEVEL1_FULL_ADD")
                            # ── ★ Phase 3: apply_buy 제거 → lifecycle 등록 ──
                            # 잔고 재확인 = 이미 체결 확인됨 → 즉시 full_fill 트리거
                            _lc_id_bal = None
                            if self._lifecycle_mgr is not None:
                                try:
                                    _lc_id_bal = make_order_lifecycle_id("KR", "BUY", code)
                                    _lc_bal = self._lifecycle_mgr.create(
                                        trade_id       = _trade_id or _lc_id_bal,
                                        market         = "KR",
                                        code           = code,
                                        side           = "BUY",
                                        strategy_name  = "StrategyManager",
                                        order_qty      = actual_qty,
                                    )
                                    self._lifecycle_mgr.accept(_lc_bal)
                                    # 잔고 재확인 경로 = 즉시 체결 확인
                                    self._pending_buy_meta[_lc_bal.order_lifecycle_id] = {
                                        "name": name, "level": level,
                                        "using_compound": decision.get("using_compound", 0),
                                        "is_full_add": is_full_add,
                                        "buy_score": buy_score, "sell_score": sell_score,
                                        "ind_score": ind_score, "trend_score": trend_score,
                                        "session": sess["session"],
                                        "reason": decision["reason"] + " [잔고확인 자동등록]",
                                        "trade_id": _trade_id,
                                        "qty": actual_qty, "price": actual_price,
                                    }
                                    # 즉시 full_fill → apply_buy 1회
                                    self._lifecycle_mgr.full_fill(
                                        _lc_bal,
                                        delta=actual_qty,
                                        avg_price=actual_price,
                                        on_filled=self._updater,
                                    )
                                except Exception as _lce2:
                                    logger.warning(
                                        f"[BUY 잔고재확인 Lifecycle] 오류 — "
                                        f"직접 apply_buy 폴백: {_lce2}"
                                    )
                                    # 폴백: lifecycle 실패 시 기존 방식
                                    self.pyramid.apply_buy(
                                        code, name, level, actual_qty, actual_price,
                                        using_compound=decision.get("using_compound", 0),
                                        is_full_add=is_full_add,
                                    )
                                    if _trade_id and code in self.pyramid.positions:
                                        self.pyramid.positions[code].trade_id = _trade_id
                                        self.pyramid._save()
                            else:
                                # lifecycle 비활성 → 기존 방식
                                self.pyramid.apply_buy(
                                    code, name, level, actual_qty, actual_price,
                                    using_compound=decision.get("using_compound", 0),
                                    is_full_add=is_full_add,
                                )
                                # trade_id → 포지션에 저장
                                if _trade_id and code in self.pyramid.positions:
                                    self.pyramid.positions[code].trade_id = _trade_id
                                    self.pyramid._save()
                                self._log_trade(
                                    "BUY", code, name, actual_price, actual_qty,
                                    decision["reason"] + " [잔고확인 자동등록]",
                                    sess["session"],
                                    extra={
                                        "level":          level,
                                        "buy_score":      buy_score,
                                        "sell_score":     sell_score,
                                        "ind_score":      ind_score,
                                        "trend_score":    trend_score,
                                        "compound_pool":  self.pyramid.compound_pool,
                                        "realized_pnl":   self.pnl_guard.realized_pnl,
                                        "pnl_state":      self.pnl_guard.state,
                                    }
                                )
                            # ── [훅 5] ORDER_ACCEPTED (잔고 재확인 = 체결 확인됨) ─
                            if _JOURNAL_ENABLED and _trade_id:
                                try:
                                    _jnl.record_order_accepted(
                                        _trade_id, "KR", code, "0",
                                        "잔고재확인체결",
                                    )
                                    _pos_r = self.pyramid.positions.get(code)
                                    _jnl.record_order_filled(
                                        trade_id       = _trade_id,
                                        market         = "KR",
                                        code           = code,
                                        fill_price     = actual_price,
                                        fill_qty       = actual_qty,
                                        avg_price      = _pos_r.avg_price if _pos_r else actual_price,
                                        buy_commission = decision.get("buy_commission"),
                                        fill_confirmed = True,   # ★ 잔고 재확인 = 체결 확인
                                    )
                                except Exception as _je:
                                    _jnl._inc_error("kr_fill_balance", _je)
                            return {
                                "action":        "BUY",
                                "code":          code, "name": name,
                                "price":         actual_price, "qty": actual_qty,
                                "level":         level,
                                "amount":        actual_price * actual_qty,
                                "buy_score":     buy_score,
                                "sell_score":    sell_score,
                                "ind_score":     ind_score,
                                "trend_score":   trend_score,
                                "strong_trend":  strong_trend,
                                "reason":        decision["reason"] + " [잔고확인 자동등록]",
                                "session":       sess["session"],
                                "order_label":   sess["order_label"],
                                "indicators":    iv,
                                "compound_pool": self.pyramid.compound_pool,
                                "realized_pnl":  self.pnl_guard.realized_pnl,
                                "pnl_state":     self.pnl_guard.state,
                            }
                        else:
                            logger.info(f"ℹ️ {code} 이미 포지션 존재 — 중복 등록 스킵")
                    else:
                        logger.warning(f"❌ {code} 잔고 재확인 결과 미보유 — 주문 실패 처리")
                except Exception as e2:
                    logger.error(f"잔고 재확인 실패 {code}: {e2}")
                return {
                    "action":       "BUY_FAIL",
                    "code":         code,
                    "name":         name,
                    "session":      sess["session"],
                    "reason":       result.get("msg1"),
                    # ── KIS 응답 상세 (웹 화면 출력용) — app.py _log 키와 일치 ──
                    "rt_cd":        result.get("rt_cd", "?"),
                    "msg_cd":       result.get("msg_cd", ""),
                    "msg1":         result.get("msg1", ""),
                    "_http_status": result.get("_http_status", 500 if result.get("rt_cd") == "9" else "?"),
                    "_response_body": result.get("_response_body", ""),
                    "_ord_dvsn":    ord_dvsn,
                    "_ord_unpr":    use_price,
                    "order_qty":    qty,
                    "order_amt":    (use_price if use_price > 0 else int(cur_price)) * qty,
                    "_tr_id":       "TTTC0802U",
                    "account":      Config.KIS_ACCOUNT_NO,
                    "_kst":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }

            # ── 명확한 성공(rt_cd==0) ──────────────────────
            if order_ok:
                is_full_add = (action == "BUY_LEVEL1_FULL_ADD")
                # ── ★ Phase 3: apply_buy 제거 → lifecycle 등록 ──
                # rt_cd=0 = 주문 접수 성공이지 체결이 아님.
                # apply_buy 는 FILLED 전이 후 _handle_buy_filled 에서 1회 호출된다.
                _lc_id_ok = None
                if self._lifecycle_mgr is not None:
                    try:
                        _lc_id_ok = make_order_lifecycle_id("KR", "BUY", code)
                        _lc_ok = self._lifecycle_mgr.create(
                            trade_id      = _trade_id or _lc_id_ok,
                            market        = "KR",
                            code          = code,
                            side          = "BUY",
                            strategy_name = "StrategyManager",
                            order_qty     = qty,
                        )
                        self._lifecycle_mgr.accept(_lc_ok)
                        self._pending_buy_meta[_lc_ok.order_lifecycle_id] = {
                            "name": name, "level": level,
                            "using_compound": decision.get("using_compound", 0),
                            "is_full_add": is_full_add,
                            "buy_score": buy_score, "sell_score": sell_score,
                            "ind_score": ind_score, "trend_score": trend_score,
                            "session": sess["session"],
                            "reason": decision["reason"],
                            "trade_id": _trade_id,
                            "qty": qty, "price": price,
                        }
                        # ★ Phase 4: odno 추출 → PendingRegistry 자동 등록
                        self._register_pending_order(
                            market        = "KR",
                            trade_id      = _trade_id or "",
                            code          = code,
                            side          = "BUY",
                            order_qty     = qty,
                            order_response= result,
                            lifecycle_id  = _lc_ok.order_lifecycle_id,
                        )
                        logger.info(
                            "[BUY ACCEPTED] lifecycle 등록 완료 — "
                            "apply_buy 대기 중: order_lifecycle_id=%s code=%s qty=%s",
                            _lc_ok.order_lifecycle_id, code, qty,
                        )
                    except Exception as _lce3:
                        logger.warning(
                            f"[BUY rt_cd=0 Lifecycle] 오류 — "
                            f"직접 apply_buy 폴백: {_lce3}"
                        )
                        # 폴백: lifecycle 실패 시 기존 방식
                        self.pyramid.apply_buy(
                            code, name, level, qty, price,
                            using_compound=decision.get("using_compound", 0),
                            is_full_add=is_full_add,
                        )
                        if _trade_id and code in self.pyramid.positions:
                            self.pyramid.positions[code].trade_id = _trade_id
                            self.pyramid._save()
                        self._log_trade(
                            "BUY", code, name, price, qty,
                            decision["reason"] + " [fallback]", sess["session"],
                            extra={
                                "level":          level,
                                "buy_score":      buy_score,
                                "sell_score":     sell_score,
                                "ind_score":      ind_score,
                                "trend_score":    trend_score,
                                "compound_pool":  self.pyramid.compound_pool,
                                "realized_pnl":   self.pnl_guard.realized_pnl,
                                "pnl_state":      self.pnl_guard.state,
                            }
                        )
                else:
                    # lifecycle 비활성 → 기존 방식 유지
                    self.pyramid.apply_buy(
                        code, name, level, qty, price,
                        using_compound=decision.get("using_compound", 0),
                        is_full_add=is_full_add,
                    )
                    if _trade_id and code in self.pyramid.positions:
                        self.pyramid.positions[code].trade_id = _trade_id
                        self.pyramid._save()
                    self._log_trade(
                        "BUY", code, name, price, qty,
                        decision["reason"], sess["session"],
                        extra={
                            "level":          level,
                            "buy_score":      buy_score,
                            "sell_score":     sell_score,
                            "ind_score":      ind_score,
                            "trend_score":    trend_score,
                            "compound_pool":  self.pyramid.compound_pool,
                            "realized_pnl":   self.pnl_guard.realized_pnl,
                            "pnl_state":      self.pnl_guard.state,
                        }
                    )
                # ── [훅 6] ORDER_ACCEPTED (rt_cd=0 접수 성공, 체결 미확인) ──
                # ★ ORDER_FILLED 는 실체결 확인 후에만 기록. rt_cd=0 은 접수이지 체결이 아님.
                # ★ fill_price / fill_time 은 NULL 유지. 잔고 재확인 후에 ORDER_FILLED 기록.
                if _JOURNAL_ENABLED and _trade_id:
                    try:
                        _jnl.record_order_accepted(
                            _trade_id, "KR", code,
                            result.get("rt_cd", "0"),
                            result.get("msg1", "주문접수성공"),
                        )
                        # ORDER_FILLED 는 생략 — 실체결 확인 연동 미구현
                        # (KIS 체결조회 또는 잔고 재확인 후 별도 호출 필요)
                    except Exception as _je:
                        _jnl._inc_error("kr_accepted_rtcd0", _je)
                return {
                    "action":        "BUY",
                    "code":          code, "name": name,
                    "price":         price, "qty": qty,
                    "level":         level,
                    "amount":        price * qty,
                    "buy_score":     buy_score,
                    "sell_score":    sell_score,
                    "ind_score":     ind_score,
                    "trend_score":   trend_score,
                    "strong_trend":  strong_trend,
                    "reason":        decision["reason"],
                    "session":       sess["session"],
                    "order_label":   sess["order_label"],
                    "indicators":    iv,
                    "compound_pool": self.pyramid.compound_pool,
                    "realized_pnl":  self.pnl_guard.realized_pnl,
                    "pnl_state":     self.pnl_guard.state,
                    "_lifecycle_id": _lc_id_ok,   # 폴링 루프에서 dispatch_fill 연결용
                }
            return {
                "action":       "BUY_FAIL",
                "code":         code,
                "name":         name,
                "session":      sess["session"],
                "reason":       result.get("msg1"),
                # ── KIS 응답 상세 (웹 화면 출력용) — app.py _log 키와 일치 ──
                "rt_cd":        result.get("rt_cd", "?"),
                "msg_cd":       result.get("msg_cd", ""),
                "msg1":         result.get("msg1", ""),
                "_http_status": result.get("_http_status", 500 if result.get("rt_cd") == "9" else "?"),
                "_response_body": result.get("_response_body", ""),
                "_ord_dvsn":    ord_dvsn,
                "_ord_unpr":    use_price,
                "order_qty":    qty,
                "order_amt":    (use_price if use_price > 0 else int(cur_price)) * qty,
                "_tr_id":       "TTTC0802U",
                "account":      Config.KIS_ACCOUNT_NO,
                "_kst":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        # ── SELL 계열 ─────────────────────────────────────
        elif action in ("SELL_ALL", "SELL_PARTIAL"):
            qty      = decision["qty"]
            price    = decision["price"]
            is_full  = (action == "SELL_ALL")
            ord_dvsn = sess["order_dvsn"]
            use_price = price if ord_dvsn == "05" else 0
            level    = decision.get("level")
            reason   = decision["reason"]

            # ★ 강제 매도 여부 판단
            is_forced = (
                "손절"          in reason
                or "트레일링"   in reason
                or "마감"       in reason
                or "KRW전량익절" in reason
                or "KRW부분익절" in reason
                or "SELL SCORE" in reason
                or "+2%전량익절" in reason   # 구버전 호환
                or "+2.5%무조건전량익절" in reason
                or "+2.0%전량익절" in reason
                or "+1.5%익절"   in reason
                or "시간청산"   in reason
            )

            # ★ 강제 매도가 아닌 일반 매도: sell_score ≥ 1 확인
            if not is_forced and sell_score < 1:
                return {
                    "action":  "HOLD", "code": code, "name": name,
                    "reason":  f"매도지표 전무(SELL_SCORE={sell_score}) — 홀드",
                    "session": sess["session"],
                }

            # ── [훅 8] SELL_SIGNAL_CONFIRMED — 포지션에서 trade_id 조회 ──
            _sell_trade_id = ""
            if _JOURNAL_ENABLED:
                try:
                    _sp = self.pyramid.positions.get(code)
                    _sell_trade_id = _sp.trade_id if (_sp and _sp.trade_id) else ""
                    _jnl.record_sell_signal(
                        _sell_trade_id, "KR", code,
                        sell_price  = price,
                        sell_score  = sell_score,
                        exit_reason = reason,
                        payload     = {"is_forced": is_forced, "is_full": is_full,
                                       "level": level, "session": sess["session"]},
                    )
                except Exception as _je:
                    _jnl._inc_error("kr_sell_signal", _je)

            # ── [훅 9] SELL_ORDER_SUBMITTED — api.sell() 직전 ──
            if _JOURNAL_ENABLED:
                try:
                    _jnl.record_sell_order_submitted(
                        _sell_trade_id, "KR", code,
                        sell_price = use_price,
                        sell_qty   = qty,
                        payload    = {"ord_dvsn": ord_dvsn},
                    )
                except Exception as _je:
                    _jnl._inc_error("kr_sell_submitted", _je)

            result = self.api.sell(code, qty, use_price, ord_dvsn=ord_dvsn)

            if result.get("rt_cd") == "0":
                # ── [훅 10] SELL_ORDER_ACCEPTED — rt_cd=0 수신 후 ──
                if _JOURNAL_ENABLED:
                    try:
                        _jnl.record_sell_order_accepted(
                            _sell_trade_id, "KR", code,
                            rt_cd = result.get("rt_cd", "0"),
                            msg1  = result.get("msg1", "매도주문접수성공"),
                        )
                    except Exception as _je:
                        _jnl._inc_error("kr_sell_accepted", _je)

                # ── ★ Phase 3: apply_sell 제거 → lifecycle 등록 ──
                # SELL ACCEPTED = 접수 성공이지 체결이 아님.
                # apply_sell / DailyPnLGuard.record / reentry.record_sell 은
                # SELL FILLED 시 _handle_sell_filled 에서만 1회 실행된다.
                _sell_lc_id = None
                if self._lifecycle_mgr is not None:
                    try:
                        _sell_lc_id = make_order_lifecycle_id("KR", "SELL", code)
                        _sell_lc = self._lifecycle_mgr.create(
                            trade_id      = _sell_trade_id or _sell_lc_id,
                            market        = "KR",
                            code          = code,
                            side          = "SELL",
                            strategy_name = "StrategyManager",
                            order_qty     = qty,
                        )
                        self._lifecycle_mgr.accept(_sell_lc)
                        # SELL FILLED 시 사용할 컨텍스트 저장
                        self._pending_sell_meta[_sell_lc.order_lifecycle_id] = {
                            "name":        name,
                            "qty":         qty,
                            "price":       price,
                            "level":       level,
                            "is_full":     is_full,
                            "reason":      reason,
                            "is_forced":   is_forced,
                            "buy_score":   buy_score,
                            "sell_score":  sell_score,
                            "sell_urgent": sell_urgent,
                            "trend_score": trend_score,
                            "strength":    strength,
                            "obv_state":   obv_state,
                            "vwap_state":  vwap_state,
                            "bb_state":    bb_state,
                            "elapsed_min": elapsed_min,
                            "avg_price":   avg_price,
                            "max_net_pct": max_net_pct,
                            "vol_change_pct": vol_change_pct,
                            "session":     sess["session"],
                            "order_label": sess["order_label"],
                            "indicators":  iv,
                            "trade_id":    _sell_trade_id,
                        }
                        # ★ Phase 4: odno 추출 → PendingRegistry 자동 등록
                        self._register_pending_order(
                            market        = "KR",
                            trade_id      = _sell_trade_id or "",
                            code          = code,
                            side          = "SELL",
                            order_qty     = qty,
                            order_response= sell_result,
                            lifecycle_id  = _sell_lc.order_lifecycle_id,
                        )
                        logger.info(
                            "[SELL ACCEPTED] lifecycle 등록 완료 — "
                            "apply_sell 대기 중: order_lifecycle_id=%s code=%s qty=%s",
                            _sell_lc.order_lifecycle_id, code, qty,
                        )
                    except Exception as _slce:
                        logger.warning(
                            f"[SELL rt_cd=0 Lifecycle] 오류 — "
                            f"직접 apply_sell 폴백: {_slce}"
                        )
                        # 폴백: lifecycle 실패 시 기존 방식
                        profit = self.pyramid.apply_sell(
                            code, qty, price, level=level, is_full=is_full
                        )
                        net_pct_actual = profit.get("net_profit_pct", 0.0)
                        net_profit_amt = profit.get("net_profit", 0.0)
                        self.pnl_guard.record(net_profit_amt)
                        pnl_status = self.pnl_guard.status_dict()
                        _is_sl = is_forced and "손절" in reason
                        self.reentry.record_sell(
                            market="KR", code=code, name=name,
                            reason=reason, is_stoploss=_is_sl,
                        )
                        self._log_trade(
                            "SELL", code, name, price, qty,
                            reason + " [fallback]", sess["session"],
                            extra={
                                "level": level, "profit": profit,
                                "net_pct": net_pct_actual,
                                "is_forced": is_forced,
                                "buy_score": buy_score, "sell_score": sell_score,
                                "sell_urgent": sell_urgent, "trend_score": trend_score,
                                "strength": strength, "obv_state": obv_state,
                                "vwap_state": vwap_state, "bb_state": bb_state,
                                "elapsed_min": elapsed_min,
                                "compound_pool": self.pyramid.compound_pool,
                                "realized_pnl": pnl_status["realized_pnl"],
                                "peak_pnl": pnl_status["peak_pnl"],
                                "pnl_state": pnl_status["state"],
                            }
                        )
                        return {
                            "action": "SELL", "code": code, "name": name,
                            "price": price, "qty": qty,
                            "profit": profit, "net_pct": net_pct_actual,
                            "is_full": is_full, "level": level, "reason": reason,
                            "is_forced": is_forced,
                            "buy_score": buy_score, "sell_score": sell_score,
                            "session": sess["session"],
                            "order_label": sess["order_label"],
                            "compound_pool": self.pyramid.compound_pool,
                            "indicators": iv, "trend_score": trend_score,
                            "elapsed_min": elapsed_min,
                            "realized_pnl": pnl_status["realized_pnl"],
                            "peak_pnl": pnl_status["peak_pnl"],
                            "pnl_state": pnl_status["state"],
                        }
                else:
                    # lifecycle 비활성 → 기존 방식 유지
                    profit = self.pyramid.apply_sell(
                        code, qty, price, level=level, is_full=is_full
                    )
                    net_pct_actual = profit.get("net_profit_pct", 0.0)
                    net_profit_amt = profit.get("net_profit", 0.0)
                    self.pnl_guard.record(net_profit_amt)
                    pnl_status = self.pnl_guard.status_dict()
                    logger.info(
                        f"[SELL] {name}({code}) | "
                        f"매수가={avg_price:,.0f}원 → 매도가={price:,.0f}원 | "
                        f"실질수익={net_pct_actual:+.2f}% | 최고수익={max_net_pct:+.2f}% | "
                        f"BUY_SCORE={buy_score:.2f} | SELL_SCORE={sell_score}/27 | "
                        f"거래량변화={vol_change_pct:+.1f}% | 체결강도={strength:.1f} | "
                        f"OBV={obv_state} | VWAP={vwap_state} | BB={bb_state} | "
                        f"매도사유={reason} | 보유시간={elapsed_min:.0f}분 | "
                        f"일일손익={pnl_status['realized_pnl']:+,.0f}원 "
                        f"(최고={pnl_status['peak_pnl']:+,.0f}원, 상태={pnl_status['state']})"
                    )
                    self._log_trade(
                        "SELL", code, name, price, qty, reason, sess["session"],
                        extra={
                            "level": level, "profit": profit,
                            "net_pct": net_pct_actual, "is_forced": is_forced,
                            "buy_score": buy_score, "sell_score": sell_score,
                            "sell_urgent": sell_urgent, "trend_score": trend_score,
                            "strength": strength, "obv_state": obv_state,
                            "vwap_state": vwap_state, "bb_state": bb_state,
                            "elapsed_min": elapsed_min,
                            "compound_pool": self.pyramid.compound_pool,
                            "realized_pnl": pnl_status["realized_pnl"],
                            "peak_pnl": pnl_status["peak_pnl"],
                            "pnl_state": pnl_status["state"],
                        }
                    )
                    _is_sl = is_forced and "손절" in reason
                    self.reentry.record_sell(
                        market="KR", code=code, name=name,
                        reason=reason, is_stoploss=_is_sl,
                    )

                # SELL ACCEPTED 반환 — 포지션은 아직 변경되지 않음
                # (lifecycle 활성: FILLED 폴링 후 _handle_sell_filled 에서 반영)
                # (lifecycle 비활성: 이미 위에서 반영 완료)
                sell_result = {
                    "action":        "SELL",
                    "code":          code, "name": name,
                    "price":         price, "qty": qty,
                    "is_full":       is_full,
                    "level":         level,
                    "reason":        reason,
                    "is_forced":     is_forced,
                    "buy_score":     buy_score,
                    "sell_score":    sell_score,
                    "session":       sess["session"],
                    "order_label":   sess["order_label"],
                    "compound_pool": self.pyramid.compound_pool,
                    "indicators":    iv,
                    "trend_score":   trend_score,
                    "elapsed_min":   elapsed_min,
                    "realized_pnl":  self.pnl_guard.realized_pnl,
                    "peak_pnl":      self.pnl_guard.peak_pnl,
                    "pnl_state":     self.pnl_guard.state,
                    "_lifecycle_id": _sell_lc_id,  # 폴링 루프에서 dispatch_fill 연결용
                }

                # ★ 손절 후 즉시 재배분 (lifecycle 활성 시 — 체결 대기 전 재배분은 위험)
                # lifecycle 비활성 경우만 즉시 재배분 (기존 동작 유지)
                if self._lifecycle_mgr is None:
                    if is_forced and "손절" in reason and is_full:
                        recycled = profit.get("net_proceeds", 0.0)
                        realloc  = self._try_recycle_to_strong(
                            recycled, sess, ind_score, ind_cutoff
                        )
                        sell_result["recycled_cash"]     = recycled
                        sell_result["realloc_attempted"] = realloc

                return sell_result

            logger.warning(
                f"⚠️ {code} SELL 실패 "
                f"rt_cd={result.get('rt_cd','?')} "
                f"msg_cd={result.get('msg_cd','?')} "
                f"msg1={result.get('msg1','?')!r}"
            )
            # ── [훅 11] SELL_ORDER_REJECTED ──
            if _JOURNAL_ENABLED:
                try:
                    _jnl.record_sell_order_rejected(
                        _sell_trade_id, "KR", code,
                        rt_cd = result.get("rt_cd", "?"),
                        msg1  = result.get("msg1", ""),
                    )
                except Exception as _je:
                    _jnl._inc_error("kr_sell_rejected", _je)
            # ── ★ SELL_FAIL 경로에서도 재진입 차단 등록 ──────────
            # 이유: ORDER PRICE CHECK 차단 등 첫 시도 실패 후 재시도로
            # 나중에 체결될 수 있음 → 선제적으로 차단 등록 (오늘 자정까지)
            # 재시도 체결 시 record_sell이 다시 호출되어 덮어쓰기됨 (무해)
            try:
                _is_sl_fail = is_forced and "손절" in reason
                self.reentry.record_sell(
                    market      = "KR",
                    code        = code,
                    name        = name,
                    reason      = f"SELL_FAIL_예약차단|{reason}",
                    is_stoploss = _is_sl_fail,
                )
                logger.info(
                    f"[재진입 차단 예약] SELL_FAIL이지만 재진입 차단 선등록: "
                    f"{name}({code})"
                )
            except Exception as _rge:
                logger.debug(f"[재진입 차단 예약] record_sell 실패(무시): {_rge}")
            return {
                "action":       "SELL_FAIL",
                "code":         code,
                "name":         name,
                "session":      sess["session"],
                "reason":       result.get("msg1"),
                # ── KIS 응답 상세 (웹 화면 출력용) — app.py 키와 통일 ──
                "rt_cd":        result.get("rt_cd", "?"),
                "msg_cd":       result.get("msg_cd", ""),
                "msg1":         result.get("msg1", ""),
                "_http_status": result.get("_http_status", 500 if result.get("rt_cd") == "9" else "?"),
                "_response_body": result.get("_response_body", ""),
                "_ord_dvsn":    ord_dvsn,
                "_ord_unpr":    use_price,
                "order_qty":    qty,
                "order_amt":    (use_price if use_price > 0 else int(cur_price)) * qty,
                "_tr_id":       "TTTC0801U",
                "account":      Config.KIS_ACCOUNT_NO,
                "_kst":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        # ── HOLD ──────────────────────────────────────────
        return {
            "action":        "HOLD",
            "code":          code, "name": name,
            "price":         cur_price,
            "buy_score":     buy_score,
            "sell_score":    sell_score,
            "sell_urgent":   sell_urgent,
            "ind_score":     ind_score,
            "ind_cutoff":    ind_cutoff,
            "trend_score":   trend_score,
            "strong_trend":  strong_trend,
            "elapsed_min":   elapsed_min,
            "session":       sess["session"],
            "reason":        decision.get("reason", "홀드"),
            "indicators":    iv,
            "compound_pool": self.pyramid.compound_pool,
            "pyramid_level": decision.get("level", 0),
            "realized_pnl":  self.pnl_guard.realized_pnl,
            "peak_pnl":      self.pnl_guard.peak_pnl,
            "pnl_state":     self.pnl_guard.state,
        }

    # ══════════════════════════════════════════════════════════
    # ★ 손절 회수금 → 강한 종목 재배분
    # ══════════════════════════════════════════════════════════
    def _try_recycle_to_strong(self, recycled_cash: float,
                                sess: dict,
                                ind_score: int,
                                ind_cutoff: int) -> list:
        if recycled_cash <= 0:
            return []

        realloc_results = []
        scored_positions = []

        for code, pos_dict in self.positions.items():
            try:
                cur_data  = self.api.get_current_price(code)
                cur_price = float(cur_data.get("price", 0))
                if not cur_price:
                    continue

                candles = self.api.get_ohlcv(code, period="D", count=200)
                iv_pos  = self.validator.validate(candles) if candles else {}
                ts      = iv_pos.get("trend_score", 0)
                ai      = pos_dict.get("entry_score", 0)
                rs      = pos_dict.get("rs_value", 0.0)

                net_pct = net_profit_pct_from_cost(
                    pos_dict.get("avg_price", 0), cur_price
                )

                scored_positions.append({
                    "code":        code,
                    "name":        pos_dict.get("name", code),
                    "avg_price":   pos_dict.get("avg_price", 0),
                    "cur_price":   cur_price,
                    "total_score": ai,
                    "rs_value":    rs,
                    "trend_score": ts,
                    "added_levels":pos_dict.get("added_levels", []),
                    "qty":         pos_dict.get("total_qty", 0),
                    "net_pct":     net_pct,
                })
            except Exception as e:
                logger.warning(f"재배분 포지션 조회 실패 {code}: {e}")

        if not scored_positions:
            logger.info("재배분 대상 포지션 없음")
            return []

        targets = self.decision.prioritize_reallocation(
            scored_positions, recycled_cash
        )

        ord_dvsn = sess["order_dvsn"]

        for tgt in targets:
            code  = tgt["code"]
            alloc = tgt["alloc_amount"]
            if alloc <= 0:
                continue

            pos = self.pyramid.get_position(code)
            if pos is None:
                continue

            candles = self.api.get_ohlcv(code, period="D", count=200)
            if not candles:
                continue
            iv2 = self.validator.validate(candles)
            if iv2.get("score", 0) < ind_cutoff:
                logger.info(
                    f"재배분 스킵 {tgt['name']}: "
                    f"지표부족 {iv2.get('score',0)}/{ind_cutoff}"
                )
                continue

            cur_data  = self.api.get_current_price(code)
            cur_price = float(cur_data.get("price", 0))
            if not cur_price:
                continue

            balance = self.api.get_balance()
            cash    = float(balance.get("cash", 0))
            add_dec = self.pyramid.evaluate(
                code, tgt["name"], cur_price,
                iv2.get("score", 0), min(cash, alloc)
            )

            if add_dec.get("action", "").startswith("BUY"):
                # ★ 주문 직전 최종 방어: 15:20 이후 추가매수도 차단
                if not allow_new_buy_now():
                    logger.warning(
                        f"🚫 [재배분 추가매수 차단] {tgt['name']}({code}) — "
                        f"15:20 이후 추가매수 불가 | 세션={sess['session']}"
                    )
                    continue
                qty       = add_dec["qty"]
                use_price = cur_price if ord_dvsn == "05" else 0
                res       = self.api.buy(code, qty, use_price, ord_dvsn=ord_dvsn)
                if res.get("rt_cd") == "0":
                    # ── ★ Phase 3: 재배분 추가매수도 lifecycle 등록 ──
                    if self._lifecycle_mgr is not None:
                        try:
                            _realloc_lc_id = make_order_lifecycle_id("KR", "BUY", code)
                            _realloc_lc = self._lifecycle_mgr.create(
                                trade_id      = _realloc_lc_id,
                                market        = "KR",
                                code          = code,
                                side          = "BUY",
                                strategy_name = "StrategyManager_Realloc",
                                order_qty     = qty,
                            )
                            self._lifecycle_mgr.accept(_realloc_lc)
                            self._pending_buy_meta[_realloc_lc.order_lifecycle_id] = {
                                "name": tgt["name"],
                                "level": add_dec["level"],
                                "using_compound": add_dec.get("using_compound", 0),
                                "is_full_add": False,
                                "session": sess["session"],
                                "reason": f"손절재배분→{tgt['reason']}",
                                "trade_id": "",
                                "qty": qty, "price": cur_price,
                            }
                            # ★ Phase 4: odno 추출 → PendingRegistry 자동 등록
                            self._register_pending_order(
                                market        = "KR",
                                trade_id      = _realloc_lc_id,
                                code          = code,
                                side          = "BUY",
                                order_qty     = qty,
                                order_response= res,
                                lifecycle_id  = _realloc_lc.order_lifecycle_id,
                            )
                            logger.info(
                                "[REALLOC BUY ACCEPTED] lifecycle 등록 완료: "
                                "order_lifecycle_id=%s code=%s qty=%s",
                                _realloc_lc.order_lifecycle_id, code, qty,
                            )
                        except Exception as _rlce:
                            logger.warning(
                                f"[REALLOC BUY Lifecycle] 오류 — "
                                f"직접 apply_buy 폴백: {_rlce}"
                            )
                            self.pyramid.apply_buy(
                                code, tgt["name"],
                                add_dec["level"], qty, cur_price,
                                using_compound=add_dec.get("using_compound", 0)
                            )
                    else:
                        # lifecycle 비활성 → 기존 방식
                        self.pyramid.apply_buy(
                            code, tgt["name"],
                            add_dec["level"], qty, cur_price,
                            using_compound=add_dec.get("using_compound", 0)
                        )
                    self._log_trade(
                        "ADD_BUY", code, tgt["name"],
                        cur_price, qty,
                        f"손절재배분→{tgt['reason']}",
                        sess["session"],
                        extra={
                            "recycled_cash":  recycled_cash,
                            "alloc_amount":   alloc,
                            "trend_score":    tgt["trend_score"],
                            "compound_pool":  self.pyramid.compound_pool,
                        }
                    )
                    realloc_results.append({
                        "code":    code,
                        "name":    tgt["name"],
                        "qty":     qty,
                        "price":   cur_price,
                        "alloc":   alloc,
                        "status":  "OK",
                    })
                    logger.info(
                        f"♻️ 손절재배분 완료: {tgt['name']} {qty}주 "
                        f"@{cur_price:,}원 (배분={alloc:,.0f}원)"
                    )
                else:
                    realloc_results.append({
                        "code":   code,
                        "name":   tgt["name"],
                        "status": "FAIL",
                        "reason": res.get("msg1"),
                    })
            else:
                logger.info(
                    f"재배분 스킵 {tgt['name']}: "
                    f"피라미딩 판단={add_dec.get('action')} "
                    f"({add_dec.get('reason','')})"
                )

        return realloc_results

    # ── 거래 로그 ──────────────────────────────────────────
    def _log_trade(self, action, code, name, price, qty,
                   reason, session, extra=None):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action, "session": session,
            "code": code, "name": name,
            "price": price, "qty": qty,
            "amount": price * qty, "reason": reason,
        }
        if extra:
            entry.update(extra)
        try:
            logs = []
            if os.path.exists(TRADE_LOG_FILE):
                with open(TRADE_LOG_FILE) as f:
                    logs = json.load(f)
            logs.append(entry)
            with open(TRADE_LOG_FILE, "w") as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"거래로그 오류: {e}")

    def get_daily_pnl_status(self) -> dict:
        """대시보드용 일일 손익 상태 반환"""
        return self.pnl_guard.status_dict()

    def get_trade_history(self, limit=50) -> list:
        try:
            if os.path.exists(TRADE_LOG_FILE):
                with open(TRADE_LOG_FILE) as f:
                    return list(reversed(json.load(f)))[:limit]
        except Exception:
            pass
        return []
