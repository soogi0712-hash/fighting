"""
engine/account_sync.py — 실계좌 동기화 엔진 (V2)
==================================================
설계 원칙:
  ★ 매 루프마다 실계좌를 조회하고 내부 상태와 비교
  ★ 불일치 발생 시 실계좌 기준으로 강제 동기화
  ★ 총자산 기준으로 매수 비중 자동 계산

총자산 계산 기준 (KR + US 합산):
  국내 예수금 (KRBroker dnca_tot_amt)
  + 국내주식 평가금액 (KRBroker scts_evlu_amt)
  + 외화 예수금 원화환산 (USBroker usd_cash_krw)
  + 해외주식 평가금액 (USBroker us_eval_krw)

주문금액 산정:
  1회 진입금액 = min(
      총자산 × entry_ratio,   (20~30%)
      실제 주문가능금액
  )
"""

import os
import json
import time
from datetime import datetime, date
from typing import Optional

from utils.v2_logger import get_logger

logger = get_logger("AccountSync")

_DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
_STATE_FILE = os.path.join(_DATA_DIR, "v2_account_state.json")

# 1회 진입 비중 (총자산 대비)
ENTRY_RATIO_MIN  = 0.20
ENTRY_RATIO_BASE = 0.25
ENTRY_RATIO_MAX  = 0.30


class AccountSync:
    """
    실계좌 상태 동기화 + 자금관리.

    Parameters:
        broker_kr:       KRBroker 인스턴스
        broker_us:       USBroker 인스턴스 (총자산 합산용, None 허용)
        usd_krw:         USD/KRW 환율 (총자산 합산 시 사용)
        initial_asset:   최초 기준자산 (복리 수익률 분모)
        entry_ratio:     1회 진입 비중 (0.20~0.30)
    """

    def __init__(self,
                 broker_kr,
                 broker_us=None,
                 usd_krw:       float = 1_350.0,
                 initial_asset: float = 5_000_000,
                 entry_ratio:   float = ENTRY_RATIO_BASE):
        # broker 별칭 — 기존 코드 호환 (self.broker = KRBroker)
        self.broker        = broker_kr
        self.broker_kr     = broker_kr
        self.broker_us     = broker_us
        self.usd_krw       = usd_krw
        self.initial_asset = initial_asset
        self.entry_ratio   = max(ENTRY_RATIO_MIN, min(entry_ratio, ENTRY_RATIO_MAX))

        # ── 내부 상태 ────────────────────────────────────────
        self.total_asset:    float = 0.0   # KR + US 합산 총자산 (KRW)
        self.cash:           float = 0.0   # KR 국내 예수금
        self.orderable_cash: float = 0.0   # KR 주문가능금액
        self.holdings:       list  = []    # KR 실보유 종목 리스트

        # 세부 자산 항목 (대시보드 표시용)
        self.kr_cash:       float = 0.0   # 국내 예수금
        self.kr_eval:       float = 0.0   # 국내주식 평가금액
        self.us_cash_krw:   float = 0.0   # 외화 예수금 원화환산
        self.us_eval_krw:   float = 0.0   # 해외주식 평가금액 원화환산
        self.kr_orderable:  float = 0.0   # KR 주문가능금액
        self.us_orderable_usd: float = 0.0  # US 주문가능 USD

        self.last_sync_ts: float = 0.0

        self._load_state()

    # ════════════════════════════════════════════════════════════
    # 1. 실계좌 동기화 (KR + US 합산)
    # ════════════════════════════════════════════════════════════

    def sync(self, force: bool = False) -> dict:
        """
        KR + US 실계좌 조회 → 합산 총자산 계산 → 내부 상태 갱신.

        총자산 = 국내예수금 + 국내평가 + 외화예수금(KRW환산) + 해외평가(KRW환산)

        Returns: 현재 계좌 상태 dict (대시보드 표시용 세부 항목 포함)
        """
        prev_total = self.total_asset

        # ── KR 잔고 조회 ─────────────────────────────────────
        kr_bal = self.broker_kr.get_balance(force=force)
        self.kr_cash   = float(kr_bal.get("cash",       0))   # 국내 예수금
        self.kr_eval   = float(kr_bal.get("scts_eval",  0))   # 국내주식 평가금액
        self.holdings  = kr_bal.get("holdings", [])
        self.cash      = self.kr_cash   # 기존 호환

        # KR 주문가능금액 (별도 조회 — 미수방지)
        try:
            self.orderable_cash = float(self.broker_kr.get_orderable_cash())
            self.kr_orderable   = self.orderable_cash
        except Exception:
            self.orderable_cash = self.kr_cash
            self.kr_orderable   = self.kr_cash

        # ── US 잔고 조회 (broker_us 있을 때만) ───────────────
        self.us_cash_krw  = 0.0
        self.us_eval_krw  = 0.0
        self.us_orderable_usd = 0.0

        if self.broker_us is not None:
            try:
                us_bal = self.broker_us.get_balance(force=force)
                if us_bal.get("ok", False):
                    # ★ TTTS3012R output2 에는 외화예수금(현금) 필드가 없음
                    #   usd_balance = tot_evlu_pfls_amt - usd_eval (holdings 평가) 추정값
                    #   usd_eval    = holdings ovrs_stck_evlu_amt 합산
                    usd_eval_usd = float(us_bal.get("usd_eval", 0))  # 보유주식 USD 평가
                    usd_cash_usd = float(us_bal.get("usd_balance", 0))  # USD 현금 추정
                    # US 주문가능 USD (TTTS3011R — 장중에만 정확, 장외시간 0)
                    try:
                        self.us_orderable_usd = float(
                            self.broker_us.get_orderable_usd(force=False)
                        )
                    except Exception:
                        self.us_orderable_usd = usd_cash_usd
                    # ★ us_orderable_usd가 있으면 그 값이 가장 정확한 USD 예수금
                    #   없으면(장외시간) usd_balance(추정값) 사용
                    effective_usd_cash = (
                        self.us_orderable_usd if self.us_orderable_usd > 0
                        else usd_cash_usd
                    )
                    # KRW 환산
                    _rate = self.usd_krw if self.usd_krw > 0 else 1_350.0
                    self.us_cash_krw = round(effective_usd_cash * _rate)
                    self.us_eval_krw = round(usd_eval_usd * _rate)
                    logger.debug(
                        f"[AccountSync] US잔고 | "
                        f"orderable_usd={self.us_orderable_usd:.2f} | "
                        f"usd_cash_추정={usd_cash_usd:.2f} | "
                        f"usd_eval={usd_eval_usd:.2f} | "
                        f"채택cash_usd={effective_usd_cash:.2f}"
                    )
            except Exception as e:
                logger.debug(f"[AccountSync] US 잔고 조회 실패 (무시): {e}")

        # ── 합산 총자산 계산 ──────────────────────────────────
        # 총자산 = 국내예수금 + 국내평가 + 외화예수금(원화) + 해외평가(원화)
        self.total_asset = (
            self.kr_cash
            + self.kr_eval
            + self.us_cash_krw
            + self.us_eval_krw
        )
        # 예외: 국내만 운용 중이고 US 데이터가 0이면 KIS tot_evlu_amt 그대로 사용
        if self.total_asset == 0 and kr_bal.get("total_asset", 0) > 0:
            self.total_asset = float(kr_bal.get("total_asset", 0))

        self.last_sync_ts = time.time()

        # ── [ASSET_SYNC] 로그 ─────────────────────────────────
        if abs(prev_total - self.total_asset) > 1000 or prev_total == 0:
            logger.info(
                f"[ASSET_SYNC] "
                f"총자산={self.total_asset:,.0f}원 | "
                f"국내예수금={self.kr_cash:,.0f}원 | "
                f"외화예수금={self.us_cash_krw:,.0f}원 | "
                f"국내평가={self.kr_eval:,.0f}원 | "
                f"해외평가={self.us_eval_krw:,.0f}원 | "
                f"KR주문가능={self.kr_orderable:,.0f}원 | "
                f"US주문가능={self.us_orderable_usd:.2f}USD"
            )

        self._save_state()
        return self._status()

    def update_usd_krw(self, rate: float):
        """환율 갱신 (main 루프에서 주기적으로 호출)."""
        if rate > 0:
            self.usd_krw = rate

    # ════════════════════════════════════════════════════════════
    # 2. 포지션 동기화 검증 (내부 vs 실계좌 불일치 감지)
    # ════════════════════════════════════════════════════════════

    def verify_position(self, code: str, internal_qty: int) -> dict:
        """
        특정 종목의 내부 보유수량 vs 실계좌 불일치 감지.

        Returns:
            {"match": bool, "real_qty": int, "internal_qty": int,
             "action": "OK"|"FORCE_SYNC"|"GHOST_POSITION"}
        """
        real_item = next((h for h in self.holdings if h["code"] == code), None)
        real_qty  = real_item["qty"] if real_item else 0

        if real_qty == internal_qty:
            return {"match": True, "real_qty": real_qty,
                    "internal_qty": internal_qty, "action": "OK"}

        if real_qty == 0 and internal_qty > 0:
            logger.error(
                f"[AccountSync] 고스트 포지션 감지! "
                f"{code} 내부={internal_qty}주 but 실계좌=0주 → FORCE_SYNC"
            )
            return {"match": False, "real_qty": 0,
                    "internal_qty": internal_qty, "action": "GHOST_POSITION"}

        logger.warning(
            f"[AccountSync] 수량 불일치 {code} | "
            f"내부={internal_qty}주 vs 실계좌={real_qty}주 → FORCE_SYNC"
        )
        return {"match": False, "real_qty": real_qty,
                "internal_qty": internal_qty, "action": "FORCE_SYNC"}

    # ════════════════════════════════════════════════════════════
    # 3. 자금관리 — 1회 진입금액 산정
    # ════════════════════════════════════════════════════════════

    def calc_entry_amount(self, price: int,
                           market: str = "KR",
                           usd_krw: float = 0.0) -> dict:
        """
        총자산 기준 1회 진입금액 산정.

        Parameters:
            price:    1주 가격 (원). US의 경우 USD 가격을 환산한 원화 값.
            market:   "KR" | "US" — US일 때 us_orderable_usd 도 상한으로 반영.
            usd_krw:  US 호출 시 사용할 환율 (매개변수 없으면 기본값 사용).

        Returns:
            {
              entry_amount_krw:  실제 주문금액 (원),
              max_qty:           주문 수량,
              total_asset:       현재 합산 총자산,
              orderable_cash:    KR 주문가능금액,
              ratio_used:        실제 비중,
              compound_ratio:    복리수익률 (초기자산 대비),
              can_enter:         bool,
              block_reason:      str,
            }
        """
        if self.orderable_cash <= 0:
            return self._cant_enter("주문가능금액 없음 (예수금 부족 / 미수)")

        if self.cash < 0:
            return self._cant_enter(f"예수금 음수 = {self.cash:,.0f}원 (미수)")

        compound_ratio = (
            (self.total_asset / self.initial_asset - 1) * 100
            if self.initial_asset > 0 else 0.0
        )
        ratio = self.entry_ratio
        if compound_ratio >= 20:
            ratio = ENTRY_RATIO_MAX
        elif compound_ratio <= -10:
            ratio = ENTRY_RATIO_MIN

        target_amt = self.total_asset * ratio

        # ★ US 거래시 us_orderable_usd를 KRW 환산해 상한에 포함
        if market == "US" and self.us_orderable_usd > 0:
            _rate = usd_krw if usd_krw > 0 else getattr(self, "usd_krw", 1_350.0)
            us_orderable_krw = self.us_orderable_usd * _rate
            # KR 예수금과 US 주문가능금액 중 큰 쪽을 상한으로 사용
            effective_orderable = max(self.orderable_cash, us_orderable_krw)
            logger.debug(
                f"[calc_entry] US 모드 | "
                f"KR주문가능={self.orderable_cash:,.0f}원 | "
                f"US주문가능={self.us_orderable_usd:.2f}USD→{us_orderable_krw:,.0f}원 | "
                f"유효상한={effective_orderable:,.0f}원"
            )
        else:
            effective_orderable = self.orderable_cash

        entry_amt = min(target_amt, effective_orderable)

        if price <= 0 or entry_amt <= 0:
            return self._cant_enter(f"진입금액 계산 불가 (price={price}, amt={entry_amt:.0f})")

        max_qty = int(entry_amt // price)
        if max_qty <= 0:
            if market == "US":
                # ★ US 모드: 기본배정으로 1주 미만이어도 can_enter=True 반환
                # orderable_usd 기준 재계산은 _place_buy()에서 담당
                logger.debug(
                    f"[자금관리] US 고가주 — 기본배정으로 수량0이나 "
                    f"orderable_usd 기준 재계산 위임 | "
                    f"진입금액={entry_amt:,.0f}원 < 주가={price:,}원"
                )
            else:
                return self._cant_enter(
                    f"수량=0 (진입금액={entry_amt:,.0f}원 < 주가={price:,}원)"
                )

        logger.info(
            f"[자금관리] 총자산={self.total_asset:,.0f}원 | "
            f"초기={self.initial_asset:,.0f}원 | "
            f"복리수익={compound_ratio:+.1f}% | "
            f"비중={ratio:.0%} | "
            f"진입금액={entry_amt:,.0f}원 | "
            f"주당={price:,}원 | 수량={max_qty}주"
        )

        return {
            "entry_amount_krw":  int(entry_amt),
            "max_qty":           max_qty,
            "total_asset":       self.total_asset,
            "orderable_cash":    self.orderable_cash,
            "us_orderable_usd":  self.us_orderable_usd,   # ★ US 주문가능 USD (strategy 직접 참조용)
            "ratio_used":        ratio,
            "compound_ratio":    compound_ratio,
            "can_enter":         True,
            "block_reason":      "",
        }

    @staticmethod
    def _cant_enter(reason: str) -> dict:
        logger.warning(f"[자금관리] 진입 차단: {reason}")
        return {
            "entry_amount_krw": 0,
            "max_qty":          0,
            "total_asset":      0,
            "orderable_cash":   0,
            "ratio_used":       0,
            "compound_ratio":   0,
            "can_enter":        False,
            "block_reason":     reason,
        }

    # ════════════════════════════════════════════════════════════
    # 4. 상태 조회
    # ════════════════════════════════════════════════════════════

    def _status(self) -> dict:
        compound = (
            (self.total_asset / self.initial_asset - 1) * 100
            if self.initial_asset > 0 else 0.0
        )
        return {
            # 합산 총자산
            "total_asset":        self.total_asset,
            "initial_asset":      self.initial_asset,
            "compound_ratio":     compound,
            # 세부 자산 항목 (대시보드 표시)
            "kr_cash":            self.kr_cash,
            "kr_eval":            self.kr_eval,
            "us_cash_krw":        self.us_cash_krw,
            "us_eval_krw":        self.us_eval_krw,
            "kr_orderable":       self.kr_orderable,
            "us_orderable_usd":   self.us_orderable_usd,
            # 기존 호환 키
            "cash":               self.cash,
            "orderable_cash":     self.orderable_cash,
            "holdings_count":     len(self.holdings),
            "holdings":           self.holdings,
            "last_sync":          datetime.fromtimestamp(self.last_sync_ts).strftime(
                                      "%H:%M:%S") if self.last_sync_ts else "미동기",
        }

    def status(self) -> dict:
        return self._status()

    # ════════════════════════════════════════════════════════════
    # 5. 상태 파일 영속화
    # ════════════════════════════════════════════════════════════

    def _load_state(self):
        try:
            if os.path.exists(_STATE_FILE):
                with open(_STATE_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("date") == date.today().isoformat():
                    self.total_asset    = float(d.get("total_asset",    0))
                    self.cash           = float(d.get("cash",           0))
                    self.kr_cash        = float(d.get("kr_cash",        0))
                    self.kr_eval        = float(d.get("kr_eval",        0))
                    self.us_cash_krw    = float(d.get("us_cash_krw",    0))
                    self.us_eval_krw    = float(d.get("us_eval_krw",    0))
                    self.orderable_cash = float(d.get("orderable_cash", 0))
                    self.kr_orderable   = float(d.get("kr_orderable",   0))
        except Exception:
            pass

    def _save_state(self):
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "date":             date.today().isoformat(),
                    "total_asset":      self.total_asset,
                    "cash":             self.cash,
                    "kr_cash":          self.kr_cash,
                    "kr_eval":          self.kr_eval,
                    "us_cash_krw":      self.us_cash_krw,
                    "us_eval_krw":      self.us_eval_krw,
                    "orderable_cash":   self.orderable_cash,
                    "kr_orderable":     self.kr_orderable,
                    "us_orderable_usd": self.us_orderable_usd,
                    "initial_asset":    self.initial_asset,
                    "last_sync":        datetime.fromtimestamp(
                                            self.last_sync_ts).isoformat()
                                        if self.last_sync_ts else "",
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[AccountSync] 상태 저장 실패: {e}")
