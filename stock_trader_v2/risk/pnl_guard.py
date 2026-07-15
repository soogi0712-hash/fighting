"""
risk/pnl_guard.py — 일일 수익·손실 관리 (V2)
==============================================
국내장 / 미국장 각각 독립 관리.
매 거래 후 record() 호출 → 상태 자동 갱신.

상태:
  TRADING      — 정상 거래 가능
  PROFIT_LOCK  — 목표수익 달성 → 신규 매수 중단
  LOSS_LIMIT   — 손실한도 초과 → 신규 매수 중단
"""

import os
import json
from datetime import datetime, date
from typing import Literal

from utils.v2_logger import get_logger

logger = get_logger("PnLGuard")

State = Literal["TRADING", "PROFIT_LOCK", "LOSS_LIMIT"]

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


class DailyPnLGuard:
    """
    일일 손익 관리 싱글 인스턴스.

    Parameters:
        market:           "KR" | "US"
        profit_lock_krw:  실현손익 이 금액 도달 시 신규매수 차단
        loss_limit_krw:   음수. 실현손익 이 금액 이하 시 신규매수 차단
    """

    def __init__(self,
                 market: str        = "KR",
                 profit_lock_krw: float = 300_000,
                 loss_limit_krw:  float = -300_000):
        self.market           = market.upper()
        self.profit_lock_krw  = profit_lock_krw
        self.loss_limit_krw   = loss_limit_krw

        self._file = os.path.join(
            _DATA_DIR, f"v2_pnl_{market.lower()}.json"
        )
        self._today: str   = ""
        self.realized_pnl: float = 0.0
        self.state:        State = "TRADING"
        self._load()

    # ── 파일 I/O ─────────────────────────────────────────────

    def _load(self):
        today = date.today().isoformat()
        try:
            if os.path.exists(self._file):
                with open(self._file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("date") == today:
                    self.realized_pnl = float(d.get("realized_pnl", 0))
                    self.state        = d.get("state", "TRADING")
                    self._today       = today
                    return
        except Exception:
            pass
        # 날짜 리셋
        self._today       = today
        self.realized_pnl = 0.0
        self.state        = "TRADING"
        self._save()

    def _save(self):
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump({
                    "date":         self._today,
                    "market":       self.market,
                    "realized_pnl": self.realized_pnl,
                    "state":        self.state,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[PnLGuard] 저장 실패: {e}")

    # ── 날짜 체크 (KST 자정 자동 리셋) ─────────────────────

    def _check_reset(self):
        today = date.today().isoformat()
        if self._today != today:
            logger.info(
                f"[PnLGuard] {self.market} 날짜 변경 → 손익 리셋 "
                f"({self._today} → {today})"
            )
            self._today       = today
            self.realized_pnl = 0.0
            self.state        = "TRADING"
            self._save()

    # ── 손익 기록 ─────────────────────────────────────────────

    def record(self, pnl_krw: float):
        """
        매도 체결 후 호출.
        pnl_krw: 수수료·세금 차감 후 실질 순손익 (원화 기준)
        """
        self._check_reset()
        self.realized_pnl += pnl_krw
        self._update_state()
        self._save()
        logger.info(
            f"[PnLGuard] {self.market} | "
            f"오늘손익={self.realized_pnl:+,.0f}원 | "
            f"상태={self.state}"
        )

    def _update_state(self):
        if self.realized_pnl >= self.profit_lock_krw:
            self.state = "PROFIT_LOCK"
        elif self.realized_pnl <= self.loss_limit_krw:
            self.state = "LOSS_LIMIT"
        else:
            self.state = "TRADING"

    # ── 매수 가능 여부 ────────────────────────────────────────

    @property
    def can_buy(self) -> bool:
        self._check_reset()
        return self.state == "TRADING"

    def block_reason(self) -> str:
        if self.state == "PROFIT_LOCK":
            return (
                f"일일 목표수익 달성 "
                f"(실현={self.realized_pnl:+,.0f}원 ≥ {self.profit_lock_krw:,.0f}원)"
            )
        if self.state == "LOSS_LIMIT":
            return (
                f"일일 손실한도 초과 "
                f"(실현={self.realized_pnl:+,.0f}원 ≤ {self.loss_limit_krw:,.0f}원)"
            )
        return ""

    # ── 상태 dict ────────────────────────────────────────────

    def status(self) -> dict:
        self._check_reset()
        return {
            "market":       self.market,
            "date":         self._today,
            "realized_pnl": self.realized_pnl,
            "state":        self.state,
            "can_buy":      self.can_buy,
            "block_reason": self.block_reason(),
        }
