"""
risk/reentry_guard.py — 재진입 차단 엔진 (V2)
===============================================
설계 원칙:
  1. guard 파일 + trade_log 이중 폴백
  2. SELL_FAIL 포함 모든 매도 시도 즉시 등록
  3. 서버 시작 시 trade_log 복원
  4. check() 항상 로그 출력

쿨다운 (2026-06-17 개정 — 최대 1거래일):
  ① 익절 매도                        → 60분
  ② 약손절 / 돌파봉저가이탈 / 조건이탈  → 4시간
  ③ 손실이 큰 경우 (-3% 이상)          → 당일 자정 (다음 거래일 자동 해제)
  ④ 하루 2회 이상 손실 종료            → 당일 자정 (다음 거래일 자동 해제)
  ⑤ 모든 쿨다운 상한: 다음날 자정 (최대 1거래일)

  ★ 3일(72h) 차단 완전 폐지
"""

import json
import os
from datetime import datetime, date, timedelta
from typing import Optional

from utils.v2_logger import get_logger

logger = get_logger("ReentryGuard")

# ── 쿨다운 상수 ─────────────────────────────────────────────────
COOLDOWN_PROFIT_H     = 1    # ① 익절: 60분
COOLDOWN_SOFT_H       = 3    # ② [개선 2026-06-28] 약손절/이탈: 4h→3h (오전 손절 후 오후 재진입 허용)
COOLDOWN_EOD_H        = 24   # ③④ 당일 자정 (실제는 다음날 00:00으로 cap)
MAX_COOLDOWN_DAYS     = 1    # 최대 1거래일 (다음날 자정)

# 큰 손실 기준 (net %)
BIG_LOSS_PCT          = -3.0  # -3% 이상 손실이면 EOD 차단

_DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
_GUARD_FILE = os.path.join(_DATA_DIR, "v2_reentry_guard.json")

# ── reason 키워드 분류 ────────────────────────────────────────────

# 익절 키워드
_PROFIT_KW = (
    "익절", "전량익절", "수익보호", "profit", "take_profit",
    "트레일", "trailing", "익절보호",
)

# 약손절 / 이탈 키워드 (작은 손실 — 4시간 쿨다운)
_SOFT_LOSS_KW = (
    "돌파봉저가이탈", "돌파봉 저가이탈", "돌파실패",
    "조건이탈", "약손절", "soft",
    "오버나이트방지", "오버나이트예방",  # 작은 손실 청산
    "15:20 강제청산", "15:20강제청산",  # 당일 강제청산 → 경과 짧음
)

# 강한 손절 키워드 (큰 손실 — EOD 차단)
_HARD_LOSS_KW = (
    "에어백손절", "stoploss", "stop_loss", "stop loss",
    "강제손절", "손실컷", "loss cut",
)


def _classify_reason(reason: str) -> str:
    """
    매도 reason을 분류:
      "profit"    → ① 익절
      "soft_loss" → ② 약손절/이탈 (4h)
      "hard_loss" → ③ 큰손실 (EOD)
      "normal"    → 기타 (4h, 분류 실패 시 보수적)
    """
    r = reason.lower()
    if any(kw in r for kw in _PROFIT_KW):
        return "profit"
    if any(kw in r for kw in _HARD_LOSS_KW):
        return "hard_loss"
    if any(kw in r for kw in _SOFT_LOSS_KW):
        return "soft_loss"
    # 'stoploss' 플래그 키워드도 없고 익절도 아니면 soft로 취급
    return "soft_loss"


def _is_stoploss(reason: str) -> bool:
    """하위 호환 — hard_loss인지 여부."""
    return _classify_reason(reason) == "hard_loss"


def _is_profit_exit(reason: str) -> bool:
    r = reason.lower()
    return any(kw in r for kw in _PROFIT_KW)


def _next_midnight() -> datetime:
    """오늘 자정 (내일 00:00:00) datetime 반환."""
    now = datetime.now()
    return datetime(now.year, now.month, now.day) + timedelta(days=1)


def _max_cooldown_cap(dt: datetime) -> datetime:
    """최대 1거래일 상한 — 다음날 자정을 넘으면 다음날 자정으로 cap."""
    cap = _next_midnight()
    return dt if dt <= cap else cap


def _key(market: str, code: str) -> str:
    return f"{market.upper()}:{code.upper()}"


def _load() -> dict:
    try:
        if os.path.exists(_GUARD_FILE):
            with open(_GUARD_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[Guard] 파일 로드 실패: {e}")
    return {}


def _save(data: dict):
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_GUARD_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[Guard] 파일 저장 실패: {e}")


class ReentryGuard:

    def __init__(self):
        # 종목별 당일 손실 매도 횟수 추적 (하루 2회 이상 → EOD 차단)
        # 구조: {"KR:005930": {"date": "2026-06-17", "loss_count": 2}}
        self._daily_loss_count: dict = {}

    # ────────────────────────────────────────────────────────
    # 매도 완료 / SELL_FAIL 시 즉시 등록
    # ────────────────────────────────────────────────────────

    def record_sell(self, market: str, code: str, name: str,
                    reason: str = "",
                    is_stoploss: Optional[bool] = None,
                    is_profit_exit: Optional[bool] = None,
                    pnl_pct: float = 0.0,
                    label: str = "SELL"):
        """
        매도 이후 재진입 차단 등록.

        쿨다운 결정 로직 (최대 1거래일 상한 엄수):
          ① is_profit_exit=True        → 60분
          ② 약손절 / 이탈 (soft_loss)  → 4시간
          ③ 큰 손실 (pnl_pct ≤ -3%)   → 당일 자정
          ④ 당일 동종목 손실 2회 이상  → 당일 자정
          ⑤ 나머지 hard_loss           → 당일 자정
          ★ 모든 쿨다운 ≤ 다음날 자정 (1거래일 상한)
        """
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        k = _key(market, code)

        # ── 손익 분류 override ──────────────────────────────
        category = _classify_reason(reason)
        if is_profit_exit is True:
            category = "profit"
        elif is_stoploss is True and category == "soft_loss":
            # 외부에서 명시적 stoploss=True면 hard_loss로 격상
            category = "hard_loss"

        # ── 당일 손실 횟수 갱신 ─────────────────────────────
        if category in ("soft_loss", "hard_loss"):
            rec = self._daily_loss_count.get(k, {"date": "", "loss_count": 0})
            if rec["date"] != today_str:
                rec = {"date": today_str, "loss_count": 0}
            rec["loss_count"] += 1
            self._daily_loss_count[k] = rec
            loss_count_today = rec["loss_count"]
        else:
            loss_count_today = 0

        # ── 쿨다운 계산 ─────────────────────────────────────
        midnight = _next_midnight()

        if category == "profit":
            cooldown_until = now + timedelta(hours=COOLDOWN_PROFIT_H)
            tag = f"익절({COOLDOWN_PROFIT_H}h)"
        elif category == "soft_loss" and pnl_pct > BIG_LOSS_PCT and loss_count_today < 2:
            # 약손절이고 손실이 작고 오늘 첫 손실 → 4시간
            cooldown_until = now + timedelta(hours=COOLDOWN_SOFT_H)
            tag = f"약손절/이탈({COOLDOWN_SOFT_H}h)"
        else:
            # hard_loss / 큰 손실 / 2회 이상 손실
            # [개선 2026-06-28] 19개 동시 손절→당일자정 차단→오후진입 전면불가 구조 개선
            # 당일 2회 이상 손실이고 pnl≤-3%인 경우만 자정까지 차단
            # 그 외 hard_loss는 3시간 차단 (오전 손절 후 오후 재진입 가능)
            if loss_count_today >= 2 and pnl_pct <= BIG_LOSS_PCT:
                cooldown_until = midnight
                tag = f"중대손실{pnl_pct:.1f}%+{loss_count_today}회(당일자정)"
            elif loss_count_today >= 3:
                cooldown_until = midnight
                tag = f"당일{loss_count_today}회이상손실(당일자정)"
            else:
                # 첫 번째 hard_loss 또는 2회지만 손실 작음 → 3시간
                cooldown_until = now + timedelta(hours=COOLDOWN_SOFT_H)
                if loss_count_today >= 2:
                    tag = f"당일{loss_count_today}회손실({COOLDOWN_SOFT_H}h)"
                elif pnl_pct <= BIG_LOSS_PCT:
                    tag = f"큰손실{pnl_pct:.1f}%({COOLDOWN_SOFT_H}h)"
                else:
                    tag = f"손절({COOLDOWN_SOFT_H}h)"

        # ★ 최대 1거래일 상한 적용 (다음날 자정 초과 불가)
        cooldown_until = _max_cooldown_cap(cooldown_until)

        # ── 등록 ────────────────────────────────────────────
        entry = {
            "market":           market.upper(),
            "code":             code,
            "name":             name,
            "sell_reason":      reason,
            "label":            label,
            "category":         category,
            "is_stoploss":      (category == "hard_loss"),
            "is_profit_exit":   (category == "profit"),
            "pnl_pct":          round(pnl_pct, 2),
            "loss_count_today": loss_count_today,
            "sold_at":          now.isoformat(),
            "cooldown_until":   cooldown_until.isoformat(),
        }
        data = _load()
        data[k] = entry
        _save(data)

        reentry_time_str = cooldown_until.strftime("%m/%d %H:%M")

        # ── [REENTRY_POLICY] 로그 ────────────────────────────
        logger.info(
            f"[REENTRY_POLICY] 종목={name}({code}) | "
            f"시장={market} | "
            f"직전매도사유={reason[:40]} | "
            f"직전손익={pnl_pct:+.1f}% | "
            f"적용쿨다운={tag} | "
            f"재진입가능시각={reentry_time_str}"
        )
        logger.info(
            f"[ReentryGuard] 차단등록({label}) | "
            f"{name}({code}) | {market} | {tag} | "
            f"쿨다운={reentry_time_str} | "
            f"사유={reason[:40]}"
        )

    # ────────────────────────────────────────────────────────
    # 매수 직전 체크 — 항상 로그 출력
    # ────────────────────────────────────────────────────────

    def check(self, market: str, code: str, name: str) -> tuple[bool, dict]:
        """
        Returns (blocked: bool, info: dict).
        ★ guard 파일에 없어도 trade_log에 오늘 SELL 이력이면 즉시 차단.
        """
        data  = _load()
        k     = _key(market, code)
        entry = data.get(k)

        # ── trade_log 폴백 ──────────────────────────────────
        if entry is None:
            tl_entry = self._find_today_sell_in_tradelog(market, code)
            if tl_entry:
                logger.warning(
                    f"[ReentryGuard] guard 미등록 → trade_log 폴백 차단: "
                    f"{name}({code})"
                )
                self.record_sell(
                    market,
                    code,
                    tl_entry.get("name", name),
                    reason    = tl_entry.get("reason", "trade_log복원"),
                    pnl_pct   = tl_entry.get("pnl_pct", 0.0),
                    label     = "TRADELOG_RESTORE",
                )
                data  = _load()
                entry = data.get(k)

        if entry is None:
            self._log_check(market, code, name, False, {})
            return False, {}

        try:
            until = datetime.fromisoformat(entry["cooldown_until"])
        except Exception:
            self._log_check(market, code, name, False, {})
            return False, {}

        now = datetime.now()
        if now >= until:
            data.pop(k, None)
            _save(data)
            self._log_check(market, code, name, False, {})
            return False, {}

        remaining = (until - now).total_seconds() / 3600
        category  = entry.get("category", "")
        is_sl     = entry.get("is_stoploss", False)
        is_pe     = entry.get("is_profit_exit", False)

        if is_pe and not is_sl:
            block_reason = f"익절 후 {COOLDOWN_PROFIT_H}h 쿨다운"
        elif category == "soft_loss":
            block_reason = f"약손절/이탈 후 {COOLDOWN_SOFT_H}h 쿨다운"
        else:
            block_reason = "손절 후 당일 재진입 차단"

        info = {
            "blocked":          True,
            "market":           entry.get("market", market),
            "code":             code,
            "name":             name,
            "category":         category,
            "sold_at":          entry.get("sold_at", ""),
            "sell_reason":      entry.get("sell_reason", ""),
            "is_stoploss":      is_sl,
            "is_profit_exit":   is_pe,
            "pnl_pct":          entry.get("pnl_pct", 0.0),
            "cooldown_until":   entry["cooldown_until"],
            "block_reason":     block_reason,
            "remaining_hours":  round(remaining, 1),
        }
        self._log_check(market, code, name, True, info)
        self._log_block(info)
        return True, info

    # ────────────────────────────────────────────────────────
    # 로그 헬퍼
    # ────────────────────────────────────────────────────────

    @staticmethod
    def _log_check(market: str, code: str, name: str,
                   blocked: bool, info: dict):
        result = "BUY_SKIP" if blocked else "BUY_ALLOWED"
        logger.info(
            f"[{market} 재진입 체크] "
            f"종목={name}({code}) | "
            f"blocked={blocked} | "
            f"차단사유={info.get('block_reason', '')} | "
            f"결과={result}"
        )

    @staticmethod
    def _log_block(info: dict):
        try:
            sold_at_str = datetime.fromisoformat(
                info.get("sold_at", "")).strftime("%m/%d %H:%M")
        except Exception:
            sold_at_str = "?"
        try:
            until_str = datetime.fromisoformat(
                info.get("cooldown_until", "")).strftime("%m/%d %H:%M")
        except Exception:
            until_str = "?"
        logger.warning(
            f"[재진입 차단] "
            f"종목={info.get('name','?')}({info.get('code','')}) | "
            f"매도={sold_at_str} | "
            f"사유={info.get('sell_reason','?')[:30]} | "
            f"쿨다운종료={until_str} | "
            f"잔여={info.get('remaining_hours',0):.1f}h"
        )

    # ────────────────────────────────────────────────────────
    # trade_log 직접 탐색
    # ────────────────────────────────────────────────────────

    def _find_today_sell_in_tradelog(self, market: str, code: str) -> Optional[dict]:
        today = date.today().isoformat()
        for fname in ("v2_trade_log.json", "trade_log.json"):
            log_file = os.path.join(_DATA_DIR, fname)
            if not os.path.exists(log_file):
                continue
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except Exception:
                continue
            for entry in reversed(logs):
                if entry.get("action") != "SELL":
                    continue
                if not str(entry.get("timestamp", "")).startswith(today):
                    continue
                if str(entry.get("code", "")).upper() == code.upper():
                    return entry
        return None

    # ────────────────────────────────────────────────────────
    # 서버 시작 시 복원
    # ────────────────────────────────────────────────────────

    def restore_from_tradelog(self, log_path: str = None):
        """
        서버 시작 시 오늘 trade_log SELL → guard 파일 복원.
        기존 등록 항목은 덮어쓰지 않음.
        """
        if log_path is None:
            # v2_trade_log.json 우선, 없으면 trade_log.json 폴백
            for fname in ("v2_trade_log.json", "trade_log.json"):
                _p = os.path.join(_DATA_DIR, fname)
                if os.path.exists(_p):
                    log_path = _p
                    break
        if log_path is None or not os.path.exists(log_path):
            return

        today = date.today().isoformat()
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception as e:
            logger.warning(f"[Guard] trade_log 로드 실패: {e}")
            return

        data = _load()
        n_new = 0
        for entry in logs:
            if entry.get("action") != "SELL":
                continue
            if not str(entry.get("timestamp", "")).startswith(today):
                continue
            code   = entry.get("code", "")
            name   = entry.get("name", code)
            reason = entry.get("reason", "trade_log복원")
            market = entry.get("market", "KR")
            pnl_pct = float(entry.get("pnl_pct", entry.get("profit_pct", 0.0)))
            k      = _key(market, code)
            if k in data:
                continue
            self.record_sell(market, code, name,
                             reason=reason, pnl_pct=pnl_pct,
                             label="RESTORE")
            n_new += 1
        if n_new:
            logger.info(f"[Guard] trade_log 복원 완료: {n_new}건 (기준={today})")

    # ────────────────────────────────────────────────────────
    # 관리 유틸
    # ────────────────────────────────────────────────────────

    def purge_expired(self):
        data = _load()
        now  = datetime.now()
        before = len(data)
        data = {
            k: v for k, v in data.items()
            if datetime.fromisoformat(v.get("cooldown_until", "2000-01-01")) > now
        }
        if len(data) < before:
            _save(data)

    def get_blocked_list(self) -> list[dict]:
        data = _load()
        now  = datetime.now()
        result = []
        for k, v in data.items():
            try:
                until = datetime.fromisoformat(v.get("cooldown_until", ""))
                if until > now:
                    rem = (until - now).total_seconds() / 3600
                    result.append({**v, "remaining_hours": round(rem, 1)})
            except Exception:
                pass
        return sorted(result, key=lambda x: x.get("cooldown_until", ""))
