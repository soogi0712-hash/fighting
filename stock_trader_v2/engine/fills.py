"""
engine/fills.py — 체결(Fill) 확인 소스 (GAP2)

원장은 '주문 접수(rt_cd==0)'가 아니라 '실제 체결'을 기록해야 한다.
이 모듈은 실제 체결 수량/체결가/주문번호를 제공하는 소스를 추상화한다.

GAP2 stock_trader/ledger/fills.py 에서 이식.
v2_rebuild 브로커 인터페이스(kr_broker.py, us_broker.py)에 맞게 연결.

- Fill:               단일 체결 사실 (order_no, qty, price).
- FillSource:         인터페이스.
- MockFillSource:     테스트/리플레이용.
- KisFillSource:      KR 체결 어댑터. kr_broker.get_executed_orders() 사용 (TTTC8001R).
- UsKisFillSource:    US 체결 어댑터. us_broker.get_us_order_history_raw() 사용.
- CumulativeFillTracker: 누적→delta 방출, idempotent.
"""
import threading
import os
from dataclasses import dataclass


@dataclass
class Fill:
    order_no: str
    qty:      int
    price:    float
    ts:       str = None


class FillSource:
    def get_fills(self, market, code, side, requested_qty=None,
                  requested_price=None, order_hint=None, ts=None):
        """확정된 체결 목록(list[Fill]) 반환. 미체결이면 빈 목록."""
        raise NotImplementedError


class MockFillSource(FillSource):
    """테스트/리플레이 전용. 큐에 넣은 체결을 순서대로 반환한다."""
    def __init__(self):
        self._queue = []   # list of (matcher_dict, [Fill,...])

    def add(self, market, code, side, fills):
        self._queue.append(({
            "market": market, "code": code, "side": side
        }, list(fills)))
        return self

    def get_fills(self, market, code, side, requested_qty=None,
                  requested_price=None, order_hint=None, ts=None):
        for i, (m, fills) in enumerate(self._queue):
            if m == {"market": market, "code": code, "side": side}:
                self._queue.pop(i)
                return fills
        return []


class CumulativeFillTracker:
    """
    누적 체결값(KIS는 tot_ccld_qty / tot_ccld_amt = '누적'을 줌)을 받아,
    아직 반영하지 않은 '신규 델타'만 Fill로 방출한다.

    - 신규 반영수량 = 현재 누적 체결수량 - 기존 반영 누적수량
    - 델타 평균체결가 = (현재 누적금액 - 기존 누적금액) / 델타수량
    - Lock으로 read-modify-write 원자화 → 이중 반영 방지
    """
    def __init__(self):
        self._seen = {}   # key -> (cum_qty, cum_amount)
        self._lock = threading.Lock()

    def seed(self, key, cum_qty, cum_amount):
        """재시작 복구 시 이미 반영된 누적값을 주입 — 이후 delta만 방출."""
        with self._lock:
            self._seen[key] = (int(cum_qty), float(cum_amount))

    def update(self, key, cum_qty, cum_amount, order_no=None, ts=None):
        with self._lock:
            prev_q, prev_a = self._seen.get(key, (0, 0.0))
            dq = cum_qty - prev_q
            if dq <= 0:
                return None
            da = cum_amount - prev_a
            avg = (da / dq) if dq else 0.0
            self._seen[key] = (cum_qty, cum_amount)
            return Fill(order_no=(order_no or str(key)), qty=dq, price=avg, ts=ts)


class _SeedMixin:
    """재시작 복구용 tracker seed 노출(내부 _tracker 보유 소스 공통)."""
    def seed(self, market, order_no, cum_qty, cum_amount):
        try:
            self._tracker.seed((market, order_no), cum_qty, cum_amount)
        except Exception:
            pass


def _is_gap2_enabled() -> bool:
    return os.environ.get("ENABLE_GAP2", "false").lower() == "true"


class KisFillSource(_SeedMixin, FillSource):
    """
    KR 실환경 어댑터.
    kr_broker.get_executed_orders() 반환 구조 (TTTC8001R):
      [{"order_no": odno, "code": pdno, "side": "BUY"|"SELL",
        "filled_qty": tot_ccld_qty(누적), "filled_price": avg_prvs,
        "filled_time": ord_tmd, ...}, ...]

    ★ v2_rebuild 브로커 실제 메서드·필드에 맞게 수정:
       - get_order_history() → get_executed_orders()
       - "type"("매수"/"매도") → "side"("BUY"/"SELL")
       - "qty"(누적체결) → "filled_qty"
       - "price"(체결가) → "filled_price"
       - "time" → "filled_time"

    ENABLE_GAP2=false면 빈 목록 반환(GAP2 비활성).
    """
    def __init__(self, broker):
        self.broker = broker
        self._tracker = CumulativeFillTracker()

    def get_fills(self, market, code, side, requested_qty=None,
                  requested_price=None, order_hint=None, ts=None):
        if not _is_gap2_enabled():
            return []
        # kr_broker.get_executed_orders()는 "side"="BUY"|"SELL" 반환
        want_side = "BUY" if side.startswith("BUY") else "SELL"
        fills = []
        try:
            for od in self.broker.get_executed_orders():
                if od.get("code") != code or od.get("side") != want_side:
                    continue
                order_no  = od.get("order_no") or f"{code}:{side}"
                key       = (market, order_no)
                # filled_qty = 누적 체결수량 (KIS TTTC8001R: tot_ccld_qty)
                cum_qty   = int(od.get("filled_qty", 0))
                # filled_price = 평균체결가 → 누적금액 추산
                fil_price = float(od.get("filled_price", 0) or 0)
                cum_amt   = fil_price * cum_qty
                f = self._tracker.update(
                    key, cum_qty, cum_amt,
                    order_no=order_no, ts=od.get("filled_time")
                )
                if f:
                    fills.append(f)
        except Exception:
            return []
        return fills


def _first_num(d: dict, keys, default=0):
    """후보 키들 중 처음 발견되는 숫자를 반환(방어적 매핑)."""
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return default


def _us_row_side(d: dict):
    """행의 매수/매도 구분을 방어적으로 판별. 불명이면 None."""
    for k in ("sll_buy_dvsn_cd", "sll_buy_dvsn_cd_name", "trad_dvsn_name"):
        v = str(d.get(k, "")).strip()
        if not v:
            continue
        if v in ("02",) or "매수" in v or v.upper() in ("BUY",):
            return "BUY"
        if v in ("01",) or "매도" in v or v.upper() in ("SELL",):
            return "SELL"
    return None


class UsKisFillSource(_SeedMixin, FillSource):
    """
    US 실환경 어댑터 (TTTS3035R 원본 응답 기반).
    필드명을 단정하지 않고 후보키로 방어적 매핑.
    확신할 수 없으면 체결로 간주하지 않고 빈 목록(=pending 유지).

    ENABLE_GAP2=false면 빈 목록 반환.
    """
    QTY_KEYS   = ("ft_ccld_qty", "ccld_qty", "tot_ccld_qty")
    AMT_KEYS   = ("ft_ccld_amt3", "ft_ccld_amt", "tot_ccld_amt", "ccld_amt")
    PRICE_KEYS = ("ft_ccld_unpr3", "ft_ccld_unpr", "avg_prvs", "ccld_unpr")
    CODE_KEYS  = ("pdno", "ovrs_pdno", "symb")
    ODNO_KEYS  = ("odno", "ODNO", "order_no")

    def __init__(self, broker):
        self.broker = broker
        self._tracker = CumulativeFillTracker()

    def get_fills(self, market, code, side, requested_qty=None,
                  requested_price=None, order_hint=None, ts=None):
        if not _is_gap2_enabled():
            return []
        try:
            rows = self.broker.get_us_order_history_raw(days=1)
        except Exception:
            return []
        fills = []
        for od in (rows or []):
            _code = next((str(od.get(k)) for k in self.CODE_KEYS if od.get(k)), None)
            if _code != code:
                continue
            _side = _us_row_side(od)
            if _side is None or _side != side:
                continue
            cum_qty = _first_num(od, self.QTY_KEYS, 0)
            if cum_qty <= 0:
                continue
            cum_amt = _first_num(od, self.AMT_KEYS, 0)
            if cum_amt <= 0:
                price = _first_num(od, self.PRICE_KEYS, 0)
                cum_amt = price * cum_qty
            order_no = next(
                (str(od.get(k)) for k in self.ODNO_KEYS if od.get(k)),
                f"{code}:{side}"
            )
            key = (market, order_no)
            f = self._tracker.update(
                key, cum_qty, float(cum_amt),
                order_no=order_no,
                ts=od.get("ord_tmd") or od.get("ord_dt")
            )
            if f:
                fills.append(f)
        return fills
