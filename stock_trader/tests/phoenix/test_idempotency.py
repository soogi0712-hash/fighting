"""멱등/watermark delta 테스트 (Phase 2-2).

- 동일 이벤트 2회 입력 시 1회만 반영
- 누적체결수량 역순 도착 시 delta 0
- 부분체결 누적수량 증가 시 차이만 반영
"""
from ._helpers import PhoenixTestCase
from phoenix import ApplyStatus
from phoenix.models import execution_event, OrderSide

COID = "co-1"
CODE = "005930"


class TestIdempotency(PhoenixTestCase):

    def test_same_event_applied_once(self):
        db, store = self.new_store()
        # 같은 체결 관측(동일 idempotency_key, 다른 event_uuid) 2회
        e1 = execution_event(COID, CODE, OrderSide.BUY, cum_filled_qty=10,
                             price=100.0, ord_qty=10)
        e2 = execution_event(COID, CODE, OrderSide.BUY, cum_filled_qty=10,
                             price=100.0, ord_qty=10)
        self.assertEqual(e1.idempotency_key, e2.idempotency_key)
        self.assertNotEqual(e1.event_uuid, e2.event_uuid)

        r1 = store.apply(e1)
        r2 = store.apply(e2)
        self.assertEqual(r1.status, ApplyStatus.APPLIED)
        self.assertEqual(r2.status, ApplyStatus.ALREADY_APPLIED)

        self.assertEqual(store.get_position(CODE)["qty"], 10)   # 1회만 반영
        self.assertEqual(store.event_count(), 1)                # 중복 append 없음

    def test_reverse_cumulative_delta_zero(self):
        db, store = self.new_store()
        store.apply(execution_event(COID, CODE, OrderSide.BUY, 50, 100.0, ord_qty=100))
        self.assertEqual(store.get_position(CODE)["qty"], 50)

        # 더 작은 누적수량(역순) 도착 → delta 0 → 포지션 불변
        r = store.apply(execution_event(COID, CODE, OrderSide.BUY, 20, 100.0,
                                        ord_qty=100))
        self.assertEqual(r.status, ApplyStatus.APPLIED)  # 관측은 기록되되
        self.assertEqual(store.get_position(CODE)["qty"], 50)  # 반영은 0
        self.assertEqual(store.get_order(COID)["applied_qty"], 50)  # watermark 후퇴 없음

    def test_partial_fill_increments_apply_only_delta(self):
        db, store = self.new_store()
        store.apply(execution_event(COID, CODE, OrderSide.BUY, 20, 100.0, ord_qty=100))
        self.assertEqual(store.get_position(CODE)["qty"], 20)
        self.assertEqual(store.get_order(COID)["state"], "PARTIALLY_FILLED")

        store.apply(execution_event(COID, CODE, OrderSide.BUY, 50, 100.0, ord_qty=100))
        self.assertEqual(store.get_position(CODE)["qty"], 50)   # +30 만

        store.apply(execution_event(COID, CODE, OrderSide.BUY, 100, 100.0, ord_qty=100))
        self.assertEqual(store.get_position(CODE)["qty"], 100)  # +50 만
        self.assertEqual(store.get_order(COID)["state"], "FILLED")
        self.assertEqual(store.get_order(COID)["applied_qty"], 100)
