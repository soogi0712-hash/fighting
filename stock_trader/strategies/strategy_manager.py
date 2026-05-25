"""
통합 전략 매니저
=================
  핵심 전략: 피라미딩 + 복리 재투자 + 무차입 투자
  검증 레이어: 보조지표 6개 (MA, RSI, MACD, BB, ATR, OBV)

흐름:
  1. 세션 확인 (휴장이면 스킵)
  2. 보조지표 6개 계산 → indicator_score 산출
  3. PyramidStrategyManager.evaluate() 호출
     → 피라미딩 단계 판단 (진입/추가/익절/손절/홀드)
  4. 세션별 진입 기준 score_cutoff 적용
     (지표 점수가 기준에 못 미치면 홀드)
  5. 매매 실행 → 포지션 반영 → 복리풀 업데이트
"""
import json, os
from datetime import datetime
from utils.logger import get_logger
from utils.market_session import session_info, is_tradeable
from config import Config
from strategies.pyramid_strategy   import PyramidStrategyManager
from strategies.indicator_validator import IndicatorValidator

logger = get_logger("StrategyManager")

TRADE_LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "trade_log.json")


class StrategyManager:

    def __init__(self, kis_api):
        self.api       = kis_api
        self.validator = IndicatorValidator()
        self.pyramid   = PyramidStrategyManager(
            kis_api,
            max_per_stock=Config.MAX_INVESTMENT_PER_STOCK,
            max_total    =Config.MAX_TOTAL_INVESTMENT,
        )
        os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)

    # ── 하위 호환: app.py 에서 positions 접근 ──────────────
    @property
    def positions(self) -> dict:
        return {code: pos.to_dict()
                for code, pos in self.pyramid.positions.items()}

    # ── 메인 실행 ─────────────────────────────────────────
    def run(self, stock: dict) -> dict:
        code = stock["code"]
        name = stock["name"]
        sess = session_info()

        # 1) 휴장
        if not sess["tradeable"]:
            return {"action": "SKIP", "reason": f"휴장({sess['session']})",
                    "session": sess["session"]}

        # 2) 데이터 수집
        candles   = self.api.get_ohlcv(code, period="D", count=200)
        if not candles:
            return {"action": "SKIP", "reason": "OHLCV 없음",
                    "session": sess["session"]}

        cur_data  = self.api.get_current_price(code)
        cur_price = float(cur_data.get("price", 0) or candles[-1]["close"])

        # 3) 보조지표 검증
        iv        = self.validator.validate(candles)
        ind_score = iv["score"]        # 매수 신호 지표 수 (0~6)
        sell_score= iv["sell_score"]   # 매도 신호 지표 수 (0~6)

        # 4) 피라미딩 전략 판단
        balance   = self.api.get_balance()
        cash      = float(balance.get("cash", 0))
        decision  = self.pyramid.evaluate(code, name, cur_price, ind_score, cash)
        action    = decision.get("action", "HOLD")

        # ── 세션별 지표 기준 적용 ─────────────────────────
        # 진입/추가 매수는 세션 cutoff 충족해야 실행
        cutoff_map = {
            "장전시간외": 3, "정규장시작": 3,
            "정규장": 2,  "정규장마감": 4,
            "장후시간외": 3,
        }
        ind_cutoff = cutoff_map.get(sess["session"], 2)

        logger.info(
            f"[{sess['icon']} {sess['session']}] {name} "
            f"지표BUY={ind_score}/6 지표SELL={sell_score}/6 "
            f"기준≥{ind_cutoff} | action={action}"
        )

        # ── 매수 계열 처리 ────────────────────────────────
        if action.startswith("BUY"):
            level = decision["level"]

            # 지표 기준 미달 → 홀드
            if ind_score < ind_cutoff:
                return {
                    "action":     "HOLD",
                    "code":       code, "name": name,
                    "price":      cur_price,
                    "ind_score":  ind_score,
                    "ind_cutoff": ind_cutoff,
                    "session":    sess["session"],
                    "reason":     (f"지표 부족({ind_score}/{ind_cutoff}) — "
                                   f"{decision['reason']}"),
                    "indicators": iv,
                }

            # 마감 직전(정규장마감)엔 신규 1단계 진입 막기
            if sess["session"] == "정규장마감" and level == 1:
                return {"action": "HOLD", "code": code, "name": name,
                        "reason": "마감전 신규진입 억제", "session": sess["session"]}

            # 매수 실행
            qty       = decision["qty"]
            price     = decision["price"]
            ord_dvsn  = sess["order_dvsn"]
            use_price = price if ord_dvsn in ("05", "06") else 0

            result = self.api.buy(code, qty, use_price, ord_dvsn=ord_dvsn)

            if result.get("rt_cd") == "0":
                self.pyramid.apply_buy(
                    code, name, level, qty, price,
                    using_compound=decision.get("using_compound", 0)
                )
                self._log_trade("BUY", code, name, price, qty,
                                decision["reason"], sess["session"],
                                extra={"level": level, "ind_score": ind_score,
                                       "compound_pool": self.pyramid.compound_pool})
                return {
                    "action":       "BUY",
                    "code":         code, "name": name,
                    "price":        price, "qty": qty,
                    "level":        level,
                    "amount":       price * qty,
                    "ind_score":    ind_score,
                    "reason":       decision["reason"],
                    "session":      sess["session"],
                    "order_label":  sess["order_label"],
                    "indicators":   iv,
                    "compound_pool":self.pyramid.compound_pool,
                }
            return {"action": "BUY_FAIL", "code": code,
                    "reason": result.get("msg1"), "session": sess["session"]}

        # ── 매도 계열 처리 ────────────────────────────────
        elif action in ("SELL_ALL", "SELL_PARTIAL"):
            qty       = decision["qty"]
            price     = decision["price"]
            is_full   = (action == "SELL_ALL")
            ord_dvsn  = sess["order_dvsn"]
            use_price = price if ord_dvsn in ("05", "06") else 0
            level     = decision.get("level")

            # 매도 신호 검증: 지표 2개 이상 매도 OR 손절/트레일링은 무조건
            reason = decision["reason"]
            is_forced = ("손절" in reason or "트레일링" in reason or "마감" in reason)
            if not is_forced and sell_score < 2:
                return {"action": "HOLD", "code": code, "name": name,
                        "reason": f"매도지표 부족({sell_score}/2) — 홀드",
                        "session": sess["session"]}

            result = self.api.sell(code, qty, use_price, ord_dvsn=ord_dvsn)

            if result.get("rt_cd") == "0":
                profit = self.pyramid.apply_sell(code, qty, price,
                                                 level=level, is_full=is_full)
                self._log_trade("SELL", code, name, price, qty,
                                reason, sess["session"],
                                extra={"level": level, "profit": profit,
                                       "compound_pool": self.pyramid.compound_pool})
                return {
                    "action":       "SELL",
                    "code":         code, "name": name,
                    "price":        price, "qty": qty,
                    "profit":       profit,
                    "is_full":      is_full,
                    "level":        level,
                    "reason":       reason,
                    "session":      sess["session"],
                    "order_label":  sess["order_label"],
                    "compound_pool":self.pyramid.compound_pool,
                    "indicators":   iv,
                }
            return {"action": "SELL_FAIL", "code": code,
                    "reason": result.get("msg1"), "session": sess["session"]}

        # ── 홀드 ─────────────────────────────────────────
        return {
            "action":       "HOLD",
            "code":         code, "name": name,
            "price":        cur_price,
            "ind_score":    ind_score,
            "sell_score":   sell_score,
            "ind_cutoff":   ind_cutoff,
            "session":      sess["session"],
            "reason":       decision.get("reason", "홀드"),
            "indicators":   iv,
            "compound_pool":self.pyramid.compound_pool,
            "pyramid_level":decision.get("level", 0),
        }

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

    def get_trade_history(self, limit=50) -> list:
        try:
            if os.path.exists(TRADE_LOG_FILE):
                with open(TRADE_LOG_FILE) as f:
                    return list(reversed(json.load(f)))[:limit]
        except Exception:
            pass
        return []
