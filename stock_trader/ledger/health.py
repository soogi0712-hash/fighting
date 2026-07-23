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
    ledger 와 trade_log.json 의 '정보성' 카운트 비교.

    ★ 주의(거짓 mismatch 방지):
      - trade_log.json 은 '주문 접수' 이벤트(BUY/SELL 접수)를 기록한다.
      - ledger 는 '실제 체결'된 라운드트립(CLOSED)을 기록한다.
      - 두 기준(접수 vs 체결)이 다르고, 부분청산이면 한 CLOSED 에 여러 SELL 접수가
        대응될 수 있으므로 '건수 일치'를 기대해선 안 된다.
      → 따라서 단순 SELL 건수 비교로 mismatch 를 절대 플래그하지 않는다(match=None).
      실제 정합성은 ledger vs KIS 체결내역(get_order_history)로 대조한다(PHASE 5).
    반환: 정보성 카운트만.
    """
    import os, json
    log_buy = log_sell = 0
    try:
        if os.path.exists(trade_log_path):
            with open(trade_log_path) as f:
                for e in json.load(f):
                    a = str(e.get("action", ""))
                    if a.startswith("SELL"):
                        log_sell += 1
                    elif a.startswith("BUY") or a.startswith("ADD"):
                        log_buy += 1
    except Exception:
        log_buy = log_sell = -1
    try:
        ledger_closed = recorder.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='CLOSED'").fetchone()[0]
        ledger_open = recorder.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status IN ('OPEN','PARTIAL')").fetchone()[0]
    except Exception:
        ledger_closed = ledger_open = -1
    return {
        "ledger_closed": ledger_closed,
        "ledger_open": ledger_open,
        "log_buy_accepted": log_buy,
        "log_sell_accepted": log_sell,
        "match": None,   # 기준이 다르므로(접수 vs 체결) 건수 일치를 판정하지 않음
        "note": "trade_log=주문접수, ledger=실체결 — 건수 비교로 mismatch 판정 안 함",
        "checked_at": datetime.now().isoformat(),
    }
