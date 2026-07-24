"""
engine/execution_engine.py — 주문 실행 엔진 (V2)
==================================================
★ 핵심 원칙:
  1. 매수 전: 주문가능금액 / 재진입 / 장시간 / ORD_DVSN / ORD_UNPR 전부 확인
  2. 매도 전: 실보유수량 확인 — 0주면 즉시 차단
  3. 매도 성공: 즉시 retry 제거 + guard 등록 + 잔고 재조회
  4. 매도 FAIL: 재진입 차단 선제 등록 (SELL_FAIL_예약)
  5. 이중 주문 방지: 종목별 쿨다운 10초

★ GAP2 Feature Flag (ENABLE_GAP2):
  - ENABLE_GAP2=true:  접수 성공 → "BUY_ACCEPTED"/"SELL_ACCEPTED" 반환
                       → ExecutionBridge.register_accept()에 pending 등록
                       → 포지션·손익은 실체결 delta 후 전략 콜백에서 반영
  - ENABLE_GAP2=false: 기존 경로 그대로 ("BUY"/"SELL" 반환, 즉시 포지션 반영)

★ 주문 흐름:
  execute_buy() →  사전검증 → KRBroker.buy() → 결과 처리 → 포지션 등록
  execute_sell() → 사전검증 → KRBroker.sell() → 결과 처리 → guard 등록
"""

import os
import json
import time
from datetime import datetime, date
from typing import Optional

import pytz

from broker.kr_broker  import KRBroker, ORD_MARKET, ORD_LIMIT
from risk.reentry_guard import ReentryGuard
from engine.account_sync import AccountSync
from utils.v2_logger    import get_logger

logger = get_logger("Execution")
KST    = pytz.timezone("Asia/Seoul")

_DATA_DIR    = os.path.join(os.path.dirname(__file__), "..", "data")
_TRADELOG    = os.path.join(_DATA_DIR, "v2_trade_log.json")

_ORDER_COOLDOWN_SEC = 10   # 동일 종목 주문 쿨다운


def _is_gap2_enabled() -> bool:
    """GAP2 Feature Flag — ENABLE_GAP2=true 이면 체결 기반 경로 활성화."""
    return os.environ.get("ENABLE_GAP2", "false").lower() == "true"


class ExecutionEngine:
    """
    매수 / 매도 주문 실행 엔진.

    Parameters:
        broker:    KRBroker
        account:   AccountSync
        reentry:   ReentryGuard
    """

    def __init__(self,
                 broker:  KRBroker,
                 account: AccountSync,
                 reentry: ReentryGuard):
        self.broker  = broker
        self.account = account
        self.reentry = reentry

        # 이중 주문 방지 쿨다운 {code: last_order_ts}
        self._order_cooldown: dict = {}

        os.makedirs(_DATA_DIR, exist_ok=True)
        if not os.path.exists(_TRADELOG):
            with open(_TRADELOG, "w", encoding="utf-8") as f:
                json.dump([], f)

    # ════════════════════════════════════════════════════════════
    # 1. 매수 실행
    # ════════════════════════════════════════════════════════════

    def execute_buy(self,
                    code:      str,
                    name:      str,
                    price:     int,
                    qty:       int,
                    reason:    str = "",
                    ord_dvsn:  str = ORD_MARKET,
                    session:   str = "",
                    exch_cd:   str = "") -> dict:
        """
        매수 실행.
        ① 재진입 차단 확인
        ② 이중 주문 쿨다운
        ③ 자금 가용 여부
        ④ 주문 전송
        ⑤ 잔고 재조회
        """
        # ① 재진입 차단 — market은 session으로 판별 (KR/US)
        market = "US" if session and session.upper() == "US" else "KR"
        blocked, info = self.reentry.check(market, code, name)
        if blocked:
            return self._skip(code, name, f"재진입차단({info['block_reason']})")

        # ② 이중 주문 쿨다운
        last_t = self._order_cooldown.get(code, 0)
        if time.time() - last_t < _ORDER_COOLDOWN_SEC:
            return self._skip(code, name, f"주문쿨다운({_ORDER_COOLDOWN_SEC}s)")

        # ③ 자금 가용 여부 (주문가능금액 > 0)
        if self.account.orderable_cash <= 0:
            return self._skip(code, name, "주문가능금액 없음")
        if self.account.cash < 0:
            return self._skip(code, name, f"예수금 음수({self.account.cash:,.0f}원)")

        # ④ 주문 전송
        logger.info(
            f"[BUY] {name}({code}) qty={qty} price={price:,} "
            f"ord_dvsn={ord_dvsn} 사유={reason}"
        )
        # ★ US 세션: USBroker.buy(code, qty, price, exch_cd, ord_dvsn)
        #    KR 세션: KRBroker.buy(code, qty, price, ord_dvsn)
        if session and session.upper() == "US" and exch_cd:
            result = self.broker.buy(code, qty, price, exch_cd, ord_dvsn)
        else:
            result = self.broker.buy(code, qty, price, ord_dvsn)
        self._order_cooldown[code] = time.time()

        if result.get("rt_cd") != "0":
            logger.warning(
                f"[BUY_FAIL] {name}({code}) "
                f"rt_cd={result.get('rt_cd')} msg1={result.get('msg1','?')!r}"
            )
            return {
                "action": "BUY_FAIL",
                "code": code, "name": name,
                "reason": result.get("msg1", ""),
                "rt_cd": result.get("rt_cd", "?"),
            }

        # ⑤ 성공 → 잔고 재조회 (캐시 무효화)
        time.sleep(0.5)
        self.account.sync(force=True)

        entry_price = price if price > 0 else self._get_current_price(code)
        actual_qty  = qty

        self._log_trade("BUY", code, name, entry_price, actual_qty,
                        reason, session)

        logger.info(
            f"[BUY_OK] {name}({code}) "
            f"qty={actual_qty} @{entry_price:,}원 | {reason}"
        )
        # ── GAP2 Feature Flag ────────────────────────────────────
        # ENABLE_GAP2=true: 접수 성공 → BUY_ACCEPTED 반환
        #   포지션은 실체결 delta 콜백(on_buy_fill)에서 등록.
        # ENABLE_GAP2=false: 기존 경로 → BUY 반환, 전략이 즉시 포지션 등록.
        _action = "BUY_ACCEPTED" if _is_gap2_enabled() else "BUY"
        return {
            "action":       _action,
            "code":         code,
            "name":         name,
            "price":        entry_price,
            "qty":          actual_qty,
            "reason":       reason,
            "session":      session,
            "order_no":     result.get("output", {}).get("ODNO", ""),
        }

    # ════════════════════════════════════════════════════════════
    # 2. 매도 실행
    # ════════════════════════════════════════════════════════════

    def execute_sell(self,
                     code:     str,
                     name:     str,
                     qty:      int,
                     price:    float = 0,   # ★ float: KR=원(int), US=USD(float)
                     reason:   str   = "",
                     ord_dvsn: str   = ORD_MARKET,
                     session:  str   = "",
                     is_stoploss:    bool = False,
                     is_profit_exit: bool = False,
                     exch_cd:  str   = "") -> dict:
        """
        매도 실행.
        ① 실보유수량 확인
        ② 주문 전송
        ③ 성공: guard 등록 + 잔고 재조회
        ④ 실패: guard 선제 등록 (SELL_FAIL 예약)
        """
        # ① 실보유수량 확인
        # ★ US 세션(exch_cd 전달): account.holdings는 KRBroker 기반 국내잔고
        #   → 미국 종목이 없어서 항상 held_qty=0 → SELL 차단되는 버그
        #   → exch_cd가 있으면 US 종목으로 판단, 파라미터 qty를 그대로 신뢰
        _is_us_session = bool(exch_cd)
        if _is_us_session:
            # US: 전달받은 qty를 그대로 사용 (이미 KIS 실잔고 기준으로 검증됨)
            sell_qty = qty
            if sell_qty <= 0:
                logger.warning(f"[SELL차단] {name}({code}) US qty=0 — SELL 차단")
                return self._skip(code, name, "보유수량 없음 — SELL 차단", action="SELL_SKIP")
        else:
            # KR: 기존 방식 — account.holdings에서 실보유수량 확인
            holdings  = self.account.holdings
            held_item = next((h for h in holdings if h["code"] == code), None)
            held_qty  = held_item["qty"] if held_item else 0

            if held_qty <= 0:
                logger.warning(f"[SELL차단] {name}({code}) 보유수량 없음")
                return self._skip(code, name, "보유수량 없음 — SELL 차단", action="SELL_SKIP")

            sell_qty = min(qty, held_qty)
            if sell_qty != qty:
                logger.warning(
                    f"[SELL보정] {name}({code}) {qty}→{sell_qty}주 (보유={held_qty})"
                )

        # ② 주문 전송
        logger.info(
            f"[SELL] {name}({code}) qty={sell_qty} price={price:.2f} "
            f"ord_dvsn={ord_dvsn} 사유={reason}"
        )
        # ★ US 세션: USBroker.sell(code, qty, price, exch_cd, ord_dvsn)
        if session and session.upper() == "US" and exch_cd:
            result = self.broker.sell(code, sell_qty, price, exch_cd, ord_dvsn)
        else:
            result = self.broker.sell(code, sell_qty, price, ord_dvsn)
        self._order_cooldown[code] = time.time()

        # market 판별 (session 파라미터 기준)
        market = "US" if session and session.upper() == "US" else "KR"

        if result.get("rt_cd") != "0":
            # ④ SELL_FAIL → 재진입 차단 선제 등록
            logger.warning(
                f"[SELL_FAIL] {name}({code}) "
                f"rt_cd={result.get('rt_cd')} msg1={result.get('msg1','?')!r}"
            )
            self.reentry.record_sell(
                market, code, name,
                reason         = f"SELL_FAIL예약|{reason}",
                is_stoploss    = is_stoploss,
                is_profit_exit = is_profit_exit,
                pnl_pct        = 0.0,
                label          = "SELL_FAIL",
            )
            return {
                "action": "SELL_FAIL",
                "code": code, "name": name,
                "reason": result.get("msg1", ""),
                "rt_cd":  result.get("rt_cd", "?"),
            }

        # ③ 성공 → guard 등록 + 잔고 재조회
        # ★ US 세션: held_item은 정의되지 않음 (KR 분기에서만 설정) → 0 처리
        _held_item_safe = locals().get("held_item", None)
        avg_price = _held_item_safe.get("avg_price", 0) if _held_item_safe else 0
        net_pct   = self._calc_net_pct(avg_price, price if price > 0 else avg_price)
        profit_amt = int((price - avg_price) * sell_qty) if price > 0 and avg_price > 0 else 0

        self.reentry.record_sell(
            market, code, name,
            reason         = reason,
            is_stoploss    = is_stoploss,
            is_profit_exit = is_profit_exit,
            pnl_pct        = net_pct,
            label          = "SELL",
        )

        time.sleep(0.5)
        self.account.sync(force=True)

        self._log_trade("SELL", code, name,
                        price if price > 0 else avg_price,
                        sell_qty, reason, session,
                        extra={"net_pct": net_pct, "profit_amt": profit_amt})

        logger.info(
            f"[SELL_OK] {name}({code}) "
            f"qty={sell_qty} net={net_pct:+.2f}% | {reason}"
        )
        # ── GAP2 Feature Flag ────────────────────────────────────
        # ENABLE_GAP2=true: 접수 성공 → SELL_ACCEPTED 반환
        #   포지션 차감·손익은 실체결 delta 콜백(on_sell_fill)에서 반영.
        # ENABLE_GAP2=false: 기존 경로 → SELL 반환, 전략이 즉시 포지션·손익 처리.
        _action = "SELL_ACCEPTED" if _is_gap2_enabled() else "SELL"
        return {
            "action":    _action,
            "code":      code,
            "name":      name,
            "price":     price,
            "qty":       sell_qty,
            "reason":    reason,
            "net_pct":   net_pct,
            "profit_amt": profit_amt,
            "session":   session,
            "order_no":  result.get("output", {}).get("ODNO", ""),
        }

    # ════════════════════════════════════════════════════════════
    # 3. 미체결 BUY 전량 취소 (15:20 cron)
    # ════════════════════════════════════════════════════════════

    def cancel_all_pending_buy(self) -> int:
        """미체결 매수 전량 취소. 취소 건수 반환."""
        n = self.broker.cancel_all_buy_orders()
        if n > 0:
            logger.info(f"[ExecutionEngine] 미체결 BUY {n}건 취소 완료")
        return n

    # ════════════════════════════════════════════════════════════
    # 4. 유틸
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _skip(code: str, name: str, reason: str,
              action: str = "SKIP") -> dict:
        logger.info(f"[{action}] {name}({code}) → {reason}")
        return {"action": action, "code": code, "name": name, "reason": reason}

    @staticmethod
    def _calc_net_pct(avg_price: float, sell_price: float) -> float:
        if avg_price <= 0 or sell_price <= 0:
            return 0.0
        gross = (sell_price - avg_price) / avg_price * 100
        fee   = 0.015 * 2 + 0.20
        return round(gross - fee, 3)

    def _get_current_price(self, code: str) -> int:
        try:
            d = self.broker.get_price(code)
            return d.get("price", 0)
        except Exception:
            return 0

    def _log_trade(self, action: str, code: str, name: str,
                   price: float, qty: int, reason: str, session: str,
                   extra: dict = None):
        # ★ B5 수정: session 기반 동적 market 판별 (이전 "KR" 하드코딩 제거)
        market = "US" if session and session.upper() == "US" else "KR"
        record = {
            "timestamp": datetime.now().isoformat(),
            "action":    action,
            "market":    market,
            "code":      code,
            "name":      name,
            "price":     price,
            "qty":       qty,
            "amount":    price * qty,
            "reason":    reason,
            "session":   session,
            **(extra or {}),
        }
        try:
            with open(_TRADELOG, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception:
            logs = []
        logs.append(record)
        with open(_TRADELOG, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
