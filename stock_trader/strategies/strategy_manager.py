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
from screener.trade_decision        import TradeDecisionEngine
from screener.transaction_cost      import net_profit_pct_from_cost

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
        decision  = self.pyramid.evaluate(
            code, name, cur_price, ind_score, cash,
            today_high=today_high,
            buy_score_norm=buy_score,
            sell_score=sell_score,
        )
        action    = decision.get("action", "HOLD")
        net_pct   = decision.get("net_pct", 0.0)

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

            result = self.api.buy(code, qty, use_price, ord_dvsn=ord_dvsn)
            order_ok = result.get("rt_cd") == "0"

            if not order_ok:
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
                            self.pyramid.apply_buy(
                                code, name, level, actual_qty, actual_price,
                                using_compound=decision.get("using_compound", 0),
                                is_full_add=is_full_add,
                            )
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
                self.pyramid.apply_buy(
                    code, name, level, qty, price,
                    using_compound=decision.get("using_compound", 0),
                    is_full_add=is_full_add,
                )
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

            result = self.api.sell(code, qty, use_price, ord_dvsn=ord_dvsn)

            if result.get("rt_cd") == "0":
                profit  = self.pyramid.apply_sell(
                    code, qty, price, level=level, is_full=is_full
                )
                net_pct_actual = profit.get("net_profit_pct", 0.0)
                net_profit_amt = profit.get("net_profit", 0.0)

                # ★ DailyPnLGuard 손익 기록 (상태 자동 평가)
                self.pnl_guard.record(net_profit_amt)
                pnl_status = self.pnl_guard.status_dict()

                # ── 매도 로그 (매도 사유 포함 14항목) ─
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
                    "SELL", code, name, price, qty,
                    reason, sess["session"],
                    extra={
                        "level":          level,
                        "profit":         profit,
                        "net_pct":        net_pct_actual,
                        "is_forced":      is_forced,
                        "buy_score":      buy_score,
                        "sell_score":     sell_score,
                        "sell_urgent":    sell_urgent,
                        "trend_score":    trend_score,
                        "strength":       strength,
                        "obv_state":      obv_state,
                        "vwap_state":     vwap_state,
                        "bb_state":       bb_state,
                        "elapsed_min":    elapsed_min,
                        "compound_pool":   self.pyramid.compound_pool,
                        "realized_pnl":    pnl_status["realized_pnl"],
                        "peak_pnl":        pnl_status["peak_pnl"],
                        "pnl_state":       pnl_status["state"],
                    }
                )

                sell_result = {
                    "action":        "SELL",
                    "code":          code, "name": name,
                    "price":         price, "qty": qty,
                    "profit":        profit,
                    "net_pct":       net_pct_actual,
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
                    "realized_pnl":  pnl_status["realized_pnl"],
                    "peak_pnl":      pnl_status["peak_pnl"],
                    "pnl_state":     pnl_status["state"],
                }

                # ★ 손절 후 즉시 재배분
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
