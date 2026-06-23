"""
재진입 차단 모듈 (ReentryGuard)
=================================
국내장 / 미국장 공통으로 적용되는 종목별 재매수 금지 규칙.

규칙:
  1. 익절/일반 매도  → 24시간 재매수 금지
  2. 손절 매도       → 72시간(3일) 재매수 금지
  3. 당일 동일 종목  → 매도 당일 자정까지 재매수 금지 (규칙 1/2와 중복 적용)

저장 파일: data/reentry_guard.json
형식:
  {
    "KR:035420": {
      "market":       "KR",
      "code":         "035420",
      "name":         "NAVER",
      "sell_reason":  "익절 +2.3%",
      "is_stoploss":  false,
      "sold_at":      "2026-06-12T10:30:00.123456",
      "cooldown_until": "2026-06-13T10:30:00.123456"
    },
    "US:NVDA": { ... }
  }

사용법:
  from strategies.reentry_guard import ReentryGuard
  guard = ReentryGuard()

  # 매도 직후 (strategy_manager / _do_sell 에서 호출)
  guard.record_sell("KR", "035420", "NAVER", reason="익절 +2.3%", is_stoploss=False)

  # 매수 직전 체크 (strategy_manager BUY 블록에서 호출)
  blocked, info = guard.check("KR", "035420", "NAVER")
  if blocked:
      # info = {blocked, market, code, name, sold_at, sell_reason,
      #          cooldown_until, block_reason, remaining_hours}
      logger.warning(f"[재진입 차단] ...")
      return SKIP
"""

import json
import os
from datetime import datetime, date, timedelta

from utils.logger import get_logger

logger = get_logger("ReentryGuard")

# ── 쿨다운 시간 ───────────────────────────────────────────────
COOLDOWN_NORMAL_HOURS   = 24     # 익절 / 일반 매도 → 24h
COOLDOWN_STOPLOSS_HOURS = 72     # 손절 → 72h (3일)

_DATA_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
_GUARD_FILE = os.path.join(_DATA_DIR, "reentry_guard.json")

# 손절 판단 키워드 (strategy_manager / us_strategy_manager reason 문자열 참고)
_STOPLOSS_KEYWORDS = (
    "손절", "stoploss", "stop_loss", "stop loss",
    "강제손절", "손실컷", "loss cut",
)


def _is_stoploss_reason(reason: str) -> bool:
    """매도 사유 문자열에서 손절 여부를 판단합니다."""
    r = reason.lower()
    return any(kw in r for kw in _STOPLOSS_KEYWORDS)


def _key(market: str, code: str) -> str:
    return f"{market.upper()}:{code.upper()}"


def _load() -> dict:
    try:
        if os.path.exists(_GUARD_FILE):
            with open(_GUARD_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[ReentryGuard] 파일 로드 실패: {e}")
    return {}


def _save(data: dict):
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_GUARD_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[ReentryGuard] 파일 저장 실패: {e}")


class ReentryGuard:
    """
    싱글턴 사용 권장 (앱 레벨):
        guard = ReentryGuard()

    매번 new해도 무방 — 파일 기반으로 상태를 공유.
    """

    # ──────────────────────────────────────────────────────────
    # 매도 완료 시 호출 → 쿨다운 등록
    # ──────────────────────────────────────────────────────────
    def record_sell(
        self,
        market: str,          # "KR" or "US"
        code: str,            # 종목코드 / 티커
        name: str,            # 종목명
        reason: str = "",     # 매도 사유 (strategy_manager reason 그대로)
        is_stoploss: bool | None = None,  # None → reason에서 자동 판단
    ):
        """
        매도 완료 후 호출 — 쿨다운 등록.
        is_stoploss=None 이면 reason 키워드로 자동 판단.
        """
        if is_stoploss is None:
            is_stoploss = _is_stoploss_reason(reason)

        hours = COOLDOWN_STOPLOSS_HOURS if is_stoploss else COOLDOWN_NORMAL_HOURS
        now   = datetime.now()

        # 당일 자정 계산
        midnight_today = datetime(now.year, now.month, now.day) + timedelta(days=1)

        # 쿨다운 종료 = max(24h or 72h, 당일 자정) — 오늘 판 종목은 오늘 재진입 절대 금지
        cooldown_until = max(now + timedelta(hours=hours), midnight_today)

        entry = {
            "market":          market.upper(),
            "code":            code,
            "name":            name,
            "sell_reason":     reason,
            "is_stoploss":     is_stoploss,
            "sold_at":         now.isoformat(),
            "cooldown_until":  cooldown_until.isoformat(),
        }

        data = _load()
        k    = _key(market, code)
        data[k] = entry
        _save(data)

        tag = "손절(72h)" if is_stoploss else "일반(24h)"
        logger.info(
            f"[ReentryGuard] 재진입 차단 등록 | "
            f"종목={name}({code}) | 시장={market.upper()} | "
            f"유형={tag} | 쿨다운종료={cooldown_until.strftime('%m/%d %H:%M')} | "
            f"매도사유={reason}"
        )

    # ──────────────────────────────────────────────────────────
    # 매수 직전 호출 → 차단 여부 반환
    # ──────────────────────────────────────────────────────────
    def check(
        self,
        market: str,
        code: str,
        name: str,
    ) -> tuple[bool, dict]:
        """
        Returns:
            (blocked: bool, info: dict)

        info keys when blocked=True:
            blocked, market, code, name,
            sold_at, sell_reason, is_stoploss,
            cooldown_until, block_reason, remaining_hours

        ★ 폴백 로직:
            reentry_guard.json에 없더라도 오늘 trade_log에 SELL 이력이
            있으면 즉시 차단 + guard 파일에 등록.
        """
        data = _load()
        k    = _key(market, code)
        entry = data.get(k)

        # ── ★ trade_log 직접 폴백 체크 ──────────────────────────
        # guard 파일에 없는 경우 trade_log에서 오늘 SELL 이력 검색
        if entry is None:
            tl_entry = self._find_today_sell_in_trade_log(market, code)
            if tl_entry:
                logger.warning(
                    f"[ReentryGuard] guard 파일 미등록이나 trade_log에서 오늘 SELL 발견 "
                    f"→ 즉시 차단 등록: {name}({code}) 사유={tl_entry.get('reason','')}"
                )
                # 즉시 guard 파일에 등록
                self.record_sell(
                    market     = market,
                    code       = code,
                    name       = tl_entry.get("name", name),
                    reason     = tl_entry.get("reason", "오늘매도(trade_log복원)"),
                    is_stoploss= _is_stoploss_reason(tl_entry.get("reason", "")),
                )
                # 방금 등록했으므로 다시 로드
                data  = _load()
                entry = data.get(k)

        if entry is None:
            return False, {}

        cooldown_until_str = entry.get("cooldown_until", "")
        try:
            cooldown_until = datetime.fromisoformat(cooldown_until_str)
        except Exception:
            return False, {}

        now = datetime.now()
        if now >= cooldown_until:
            # 쿨다운 만료 → 항목 삭제 후 허용
            data.pop(k, None)
            _save(data)
            return False, {}

        remaining = (cooldown_until - now).total_seconds() / 3600
        is_stoploss = entry.get("is_stoploss", False)
        block_reason = (
            f"손절 후 {COOLDOWN_STOPLOSS_HOURS}h 재매수 금지"
            if is_stoploss
            else f"매도 후 {COOLDOWN_NORMAL_HOURS}h 재매수 금지"
        )

        info = {
            "blocked":          True,
            "market":           entry.get("market", market),
            "code":             code,
            "name":             name,
            "sold_at":          entry.get("sold_at", ""),
            "sell_reason":      entry.get("sell_reason", ""),
            "is_stoploss":      is_stoploss,
            "cooldown_until":   cooldown_until_str,
            "block_reason":     block_reason,
            "remaining_hours":  round(remaining, 1),
        }
        return True, info

    # ──────────────────────────────────────────────────────────
    # trade_log에서 오늘 해당 종목 SELL 이력 탐색
    # ──────────────────────────────────────────────────────────
    def _find_today_sell_in_trade_log(
        self, market: str, code: str
    ) -> dict | None:
        """
        trade_log.json에서 오늘 날짜의 해당 종목 SELL 레코드를 반환.
        없으면 None.  (KR 시장 코드 비교는 대소문자 무시)
        """
        trade_log_path = os.path.join(_DATA_DIR, "trade_log.json")
        if not os.path.exists(trade_log_path):
            return None
        today = date.today().isoformat()
        try:
            with open(trade_log_path, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception as e:
            logger.debug(f"[ReentryGuard] trade_log 읽기 실패: {e}")
            return None
        # 최신 순으로 탐색 (마지막 SELL 우선)
        for entry in reversed(logs):
            if entry.get("action") != "SELL":
                continue
            if not str(entry.get("timestamp", "")).startswith(today):
                continue
            if str(entry.get("code", "")).upper() == code.upper():
                return entry
        return None

    # ──────────────────────────────────────────────────────────
    # [재진입 차단] 로그 출력 헬퍼 (공통 포맷)
    # ──────────────────────────────────────────────────────────
    @staticmethod
    def log_block(info: dict):
        """[재진입 차단] 표준 로그 출력."""
        sold_at_str = ""
        try:
            sold_at_str = datetime.fromisoformat(
                info.get("sold_at", "")
            ).strftime("%m/%d %H:%M")
        except Exception:
            sold_at_str = info.get("sold_at", "?")

        cooldown_str = ""
        try:
            cooldown_str = datetime.fromisoformat(
                info.get("cooldown_until", "")
            ).strftime("%m/%d %H:%M")
        except Exception:
            cooldown_str = info.get("cooldown_until", "?")

        logger.warning(
            f"[재진입 차단] "
            f"종목={info.get('name','')}({info.get('code','')}) | "
            f"시장={info.get('market','?')} | "
            f"마지막매도시간={sold_at_str} | "
            f"마지막매도사유={info.get('sell_reason','?')} | "
            f"쿨다운종료={cooldown_str} | "
            f"차단사유={info.get('block_reason','?')} | "
            f"잔여={info.get('remaining_hours',0):.1f}h"
        )

    @staticmethod
    def log_check(market: str, code: str, name: str,
                  blocked: bool, info: dict):
        """
        매수 직전 항상 출력하는 재진입 체크 로그.
        blocked=True/False 모두 출력 → 차단 미동작 추적용.
        """
        guard_file = os.path.join(_DATA_DIR, "reentry_guard.json")
        registered = os.path.exists(guard_file) and _key(market, code) in _load()
        result_tag = "BUY_SKIP" if blocked else "BUY_ALLOWED"
        block_reason = info.get("block_reason", "") if blocked else ""
        logger.info(
            f"[{market} 재진입 체크] "
            f"종목={name}({code}) | "
            f"guard_file={guard_file} | "
            f"등록여부={registered} | "
            f"blocked={blocked} | "
            f"차단사유={block_reason} | "
            f"결과={result_tag}"
        )

    # ──────────────────────────────────────────────────────────
    # 서버 시작 시 trade_log 기반 오늘 SELL 이력 복원
    # ──────────────────────────────────────────────────────────
    def restore_from_trade_log(self, trade_log_path: str = None):
        """
        서버 시작 시 호출 — trade_log.json의 오늘 SELL 이력을
        reentry_guard.json에 복원.

        ★ 이미 등록된 항목은 덮어쓰지 않음 (기존 쿨다운 유지).
        """
        if trade_log_path is None:
            trade_log_path = os.path.join(_DATA_DIR, "trade_log.json")
        if not os.path.exists(trade_log_path):
            logger.debug("[ReentryGuard] trade_log.json 없음 → 복원 스킵")
            return

        today = date.today().isoformat()
        try:
            with open(trade_log_path, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception as e:
            logger.warning(f"[ReentryGuard] trade_log 로드 실패: {e}")
            return

        data = _load()
        restored = 0
        skipped  = 0
        for entry in logs:
            if entry.get("action") != "SELL":
                continue
            if not str(entry.get("timestamp", "")).startswith(today):
                continue
            code   = entry.get("code", "")
            name   = entry.get("name", code)
            reason = entry.get("reason", "오늘매도(trade_log복원)")
            # market은 trade_log에 없을 수 있음 → 기본 KR
            market = entry.get("market", "KR")
            k = _key(market, code)
            if k in data:
                skipped += 1
                continue
            # 새로 등록
            self.record_sell(
                market     = market,
                code       = code,
                name       = name,
                reason     = reason,
                is_stoploss= _is_stoploss_reason(reason),
            )
            restored += 1
            logger.info(
                f"[ReentryGuard] trade_log 복원 완료: "
                f"{name}({code}) 사유={reason[:30]}"
            )
        if restored or skipped:
            logger.info(
                f"[ReentryGuard] 복원 결과 — "
                f"신규등록={restored}건, 기존유지={skipped}건 "
                f"(기준일={today})"
            )

    # ──────────────────────────────────────────────────────────
    # 만료 항목 일괄 정리 (주기적으로 호출 가능)
    # ──────────────────────────────────────────────────────────
    def purge_expired(self):
        """만료된 쿨다운 항목 제거 (파일 크기 관리)."""
        data = _load()
        now  = datetime.now()
        before = len(data)
        data = {
            k: v for k, v in data.items()
            if datetime.fromisoformat(v.get("cooldown_until", "2000-01-01"))
               > now
        }
        if len(data) < before:
            _save(data)
            logger.debug(f"[ReentryGuard] 만료 항목 {before - len(data)}개 정리")

    # ──────────────────────────────────────────────────────────
    # 현재 차단 목록 조회 (모니터링용)
    # ──────────────────────────────────────────────────────────
    def get_blocked_list(self) -> list[dict]:
        """현재 쿨다운 중인 종목 목록 반환."""
        data = _load()
        now  = datetime.now()
        result = []
        for k, v in data.items():
            try:
                until = datetime.fromisoformat(v.get("cooldown_until", ""))
                if until > now:
                    remaining = (until - now).total_seconds() / 3600
                    result.append({**v, "remaining_hours": round(remaining, 1)})
            except Exception:
                pass
        return sorted(result, key=lambda x: x.get("cooldown_until", ""))
