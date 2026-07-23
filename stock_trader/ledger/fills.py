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


class KisFillSource(FillSource):
    """
    실환경 어댑터 (PHASE 5). 실 API 호출을 포함하므로 이 단계에서는 사용 금지.
    LIVE_ORDER_ENABLED=false 면 네트워크 호출 없이 빈 목록을 반환한다.
    """
    def __init__(self, api):
        self.api = api

    def get_fills(self, market, code, side, requested_qty=None,
                  requested_price=None, order_hint=None, ts=None):
        # ★ 마스터 게이트: 비활성 상태에서는 절대 실 API 를 호출하지 않는다.
        try:
            from config import Config
        except Exception:
            from ..config import Config
        if not getattr(Config, "LIVE_ORDER_ENABLED", False):
            return []
        # ── 이하 실환경(PHASE 5)에서만 동작 — get_order_history 확인 필드 사용 ──
        want = "매수" if side.startswith("BUY") else "매도"
        fills = []
        try:
            for od in self.api.get_order_history(days=1):   # 실 API (PHASE 5)
                if od.get("code") == code and od.get("type") == want:
                    q = int(od.get("qty", 0))
                    if q > 0:
                        fills.append(Fill(order_no=str(od.get("order_no", od.get("odno", ""))),
                                          qty=q, price=float(od.get("price", 0)), ts=od.get("time")))
        except Exception:
            return []
        return fills
