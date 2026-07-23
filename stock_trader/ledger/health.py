"""
health.py — 원장 기록 건강상태 (ledger_health)

- 기록 실패가 조용히 사라지지 않도록 실패를 집계/노출한다.
- 매매 흐름을 막지 않는다(기록 실패는 주문을 중단시키지 않음).
- 상태 API/대시보드가 읽을 수 있도록 프로세스 전역 싱글턴을 제공한다.
"""
import threading
from datetime import datetime
from collections import deque


class LedgerHealth:
    def __init__(self, keep: int = 50):
        self._lock = threading.Lock()
        self.fail_count = 0
        self.record_count = 0
        self.last_error = None
        self._recent = deque(maxlen=keep)
        self.last_mismatch = None

    def record_ok(self):
        with self._lock:
            self.record_count += 1

    def record_fail(self, market, code, action, order_no, exception):
        """구조화된 실패 기록: market/code/action/order_no/exception."""
        entry = {
            "ts": datetime.now().isoformat(),
            "market": market, "code": code, "action": action,
            "order_no": order_no, "exception": str(exception),
        }
        with self._lock:
            self.fail_count += 1
            self.last_error = entry
            self._recent.append(entry)

    def set_mismatch(self, info):
        with self._lock:
            self.last_mismatch = info

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "record_count": self.record_count,
                "fail_count":   self.fail_count,
                "last_error":   self.last_error,
                "last_mismatch": self.last_mismatch,
                "recent_fails": list(self._recent),
                "healthy":      self.fail_count == 0 and self.last_mismatch is None,
            }


# 프로세스 전역 싱글턴 (상태 API/대시보드에서 참조)
LEDGER_HEALTH = LedgerHealth()


def check_consistency(recorder, trade_log_path) -> dict:
    """
    ledger 와 trade_log.json 의 대략적 정합성 비교(경량).
    - trade_log 의 SELL 건수 vs ledger 의 CLOSED 건수를 비교.
    - trade_log 에는 net 손익이 없으므로 금액 비교는 하지 않는다(건수 기준).
    반환: {'ledger_closed', 'log_sell', 'match', ...}
    """
    import os, json
    log_sell = 0
    try:
        if os.path.exists(trade_log_path):
            with open(trade_log_path) as f:
                for e in json.load(f):
                    if str(e.get("action", "")).startswith("SELL"):
                        log_sell += 1
    except Exception:
        log_sell = -1
    try:
        ledger_closed = recorder.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='CLOSED'").fetchone()[0]
    except Exception:
        ledger_closed = -1
    result = {
        "ledger_closed": ledger_closed,
        "log_sell": log_sell,
        "match": (ledger_closed == log_sell) if (ledger_closed >= 0 and log_sell >= 0) else None,
        "checked_at": datetime.now().isoformat(),
    }
    if result["match"] is False:
        LEDGER_HEALTH.set_mismatch(result)
    return result
