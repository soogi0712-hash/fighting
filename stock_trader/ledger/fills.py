"""
fills.py — 체결(Fill) 확인 소스

원장은 '주문 접수(rt_cd==0)'가 아니라 '실제 체결'을 기록해야 한다.
이 모듈은 실제 체결 수량/체결가/주문번호를 제공하는 소스를 추상화한다.

- Fill:            단일 체결 사실 (order_no, qty, price).
- FillSource:      인터페이스.
- MockFillSource:  테스트/리플레이용 (미리 정의된 체결 반환). 실 API 미사용.
- KisFillSource:   실환경(PHASE 5) 어댑터. 기존 get_order_history() 의
                   '확인된' 필드(tot_ccld_qty=체결수량, avg_prvs=평균체결가,
                   odno=주문번호)를 사용. LIVE_ORDER_ENABLED=false 면 빈 목록 반환
                   (이 단계에서는 절대 호출되지 않음).
"""
import threading
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
    """테스트/리플레이 전용. 큐에 넣은 체결을 순서대로 반환한다. 네트워크 없음."""
    def __init__(self):
        self._queue = []   # list of (matcher_dict, [Fill,...])

    def add(self, market, code, side, fills):
        self._queue.append(({"market": market, "code": code, "side": side}, list(fills)))
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
    누적 체결값(KIS get_order_history 는 tot_ccld_qty/tot_ccld_amt = '누적'을 준다)을
    받아, 아직 반영하지 않은 '신규 델타'만 Fill 로 방출한다.

    - 신규 반영수량 = 현재 누적 체결수량 - 기존 반영 누적수량   (item 6)
    - 델타 평균체결가 = (현재 누적금액 - 기존 누적금액) / 델타수량  (item 7)
      → 동일 주문이 여러 체결가로 나뉘어도 실제 누적금액/수량으로 평균가 산출.
    """
    def __init__(self):
        self._seen = {}   # key -> (cum_qty, cum_amount)
        # ★ 원자성 보호: 두 poll 스레드가 동일 key 로 동시에 update() 를 호출해도
        #   read-modify-write(_seen 갱신)를 직렬화하여 '동일 delta Fill 이중 생성'을 방지.
        #   먼저 임계구역에 든 스레드가 델타를 consume 하면 _seen 이 전진하므로,
        #   뒤이은 스레드는 dq<=0 → None(no-op) 이 된다.
        self._lock = threading.Lock()

    def seed(self, key, cum_qty, cum_amount):
        """재시작 복구 시 이미 반영된 누적값을 주입 — 이후 delta 만 방출되게."""
        with self._lock:
            self._seen[key] = (int(cum_qty), float(cum_amount))

    def update(self, key, cum_qty, cum_amount, order_no=None, ts=None):
        with self._lock:
            prev_q, prev_a = self._seen.get(key, (0, 0.0))
            dq = cum_qty - prev_q
            if dq <= 0:
                return None                  # 신규 반영분 없음
            da = cum_amount - prev_a
            avg = (da / dq) if dq else 0.0   # 이번 delta 평균 체결가(로직 불변)
            self._seen[key] = (cum_qty, cum_amount)
            return Fill(order_no=(order_no or str(key)), qty=dq, price=avg, ts=ts)


class _SeedMixin:
    """재시작 복구용 tracker seed 노출(내부 _tracker 보유 소스 공통)."""
    def seed(self, market, order_no, cum_qty, cum_amount):
        try:
            self._tracker.seed((market, order_no), cum_qty, cum_amount)
        except Exception:
            pass


class KisFillSource(_SeedMixin, FillSource):
    """
    실환경 어댑터 (PHASE 5). 실 API 호출을 포함하므로 이 단계에서는 사용 금지.
    LIVE_ORDER_ENABLED=false 면 네트워크 호출 없이 빈 목록을 반환한다.

    get_order_history 의 '확인된' 필드만 사용:
      qty=tot_ccld_qty(누적), amount=tot_ccld_amt(누적), price=avg_prvs.
    누적값이므로 CumulativeFillTracker 로 델타만 방출(중복/부분체결 안전).
    """
    def __init__(self, api):
        self.api = api
        self._tracker = CumulativeFillTracker()

    def get_fills(self, market, code, side, requested_qty=None,
                  requested_price=None, order_hint=None, ts=None):
        # ★ 마스터 게이트: 비활성 상태에서는 절대 실 API 를 호출하지 않는다.
        try:
            from config import Config
        except Exception:
            from ..config import Config
        if not getattr(Config, "LIVE_ORDER_ENABLED", False):
            return []
        want = "매수" if side.startswith("BUY") else "매도"
        fills = []
        try:
            for od in self.api.get_order_history(days=1):   # 실 API (PHASE 5)
                if od.get("code") != code or od.get("type") != want:
                    continue
                order_no = od.get("order_no") or f"{code}:{side}"
                key = (market, order_no)
                cum_qty = int(od.get("qty", 0))
                cum_amt = float(od.get("amount", 0) or (od.get("price", 0) * cum_qty))
                f = self._tracker.update(key, cum_qty, cum_amt, order_no=order_no,
                                         ts=od.get("time"))
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
    해외주식 실체결 어댑터 (PHASE5). 격리 구현 — 필드명을 단정하지 않고 후보키로
    방어적 매핑. 확신할 수 없으면 체결로 간주하지 않고 빈 목록(=pending 유지).

    - LIVE_ORDER_ENABLED=false 면 실 API 미호출(빈 목록).
    - 누적 체결수량/금액을 CumulativeFillTracker 로 delta 방출(중복/부분체결 안전).
    - 실제 필드명 확인은 verify_us_fill_path.py 가 원본 응답을 덤프해 지원.
    """
    QTY_KEYS   = ("ft_ccld_qty", "ccld_qty", "tot_ccld_qty")
    AMT_KEYS   = ("ft_ccld_amt3", "ft_ccld_amt", "tot_ccld_amt", "ccld_amt")
    PRICE_KEYS = ("ft_ccld_unpr3", "ft_ccld_unpr", "avg_prvs", "ccld_unpr")
    CODE_KEYS  = ("pdno", "ovrs_pdno", "symb")
    ODNO_KEYS  = ("odno", "ODNO", "order_no")

    def __init__(self, api):
        self.api = api
        self._tracker = CumulativeFillTracker()

    def get_fills(self, market, code, side, requested_qty=None,
                  requested_price=None, order_hint=None, ts=None):
        try:
            from config import Config
        except Exception:
            from ..config import Config
        if not getattr(Config, "LIVE_ORDER_ENABLED", False):
            return []
        try:
            rows = self.api.get_us_order_history_raw(days=1)
        except Exception:
            return []
        fills = []
        for od in (rows or []):
            # 종목 일치(후보키)
            _code = next((str(od.get(k)) for k in self.CODE_KEYS if od.get(k)), None)
            if _code != code:
                continue
            # 방향 일치(불명이면 skip — 체결로 간주 금지)
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
            order_no = next((str(od.get(k)) for k in self.ODNO_KEYS if od.get(k)),
                            f"{code}:{side}")
            key = (market, order_no)
            f = self._tracker.update(key, cum_qty, float(cum_amt),
                                     order_no=order_no, ts=od.get("ord_tmd") or od.get("ord_dt"))
            if f:
                fills.append(f)
        return fills
