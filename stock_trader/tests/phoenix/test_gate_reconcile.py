"""복구 게이트 / reconciliation 테스트 (Phase 2-2).

- recovery 완료 전 BUY 차단
- recovery 완료 전 추가매수 차단
- 잔고 확정 전 SELL 차단
- broker 잔고와 event projection 불일치 시 reconciliation
"""
from ._helpers import PhoenixTestCase
from phoenix import OrderGate, OrderIntent, Recovery, Reconciler
from phoenix.models import IntentKind, OrderSide, execution_event

CODE = "005930"
COID = "co-1"


class TestRecoveryGate(PhoenixTestCase):

    def test_buy_blocked_before_recovery_complete(self):
        db, store = self.new_store()   # 기본 recovery_state=RECOVERING
        gate = OrderGate(db)
        d = gate.check(OrderIntent(CODE, OrderSide.BUY, 10, IntentKind.NEW_BUY))
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "RECOVERY_INCOMPLETE")

    def test_add_buy_blocked_before_recovery_complete(self):
        db, store = self.new_store()
        gate = OrderGate(db)
        d = gate.check(OrderIntent(CODE, OrderSide.BUY, 10, IntentKind.ADD_BUY))
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "RECOVERY_INCOMPLETE")

    def test_sell_blocked_before_balance_confirmed(self):
        db, store = self.new_store()
        gate = OrderGate(db)
        d = gate.check(OrderIntent(CODE, OrderSide.SELL, 10, IntentKind.SELL))
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "BALANCE_UNCONFIRMED")

    def test_buy_allowed_after_recovery_complete(self):
        db, store = self.new_store()
        Recovery(db).complete()
        gate = OrderGate(db)
        d = gate.check(OrderIntent(CODE, OrderSide.BUY, 10, IntentKind.NEW_BUY))
        self.assertTrue(d.allowed)

    def test_sell_allowed_after_balance_confirmed(self):
        db, store = self.new_store()
        # broker 잔고로 포지션 확증
        Reconciler(db, store).reconcile(
            [{"code": CODE, "qty": 10, "avg_price": 100.0}], token="t1")
        gate = OrderGate(db)
        d = gate.check(OrderIntent(CODE, OrderSide.SELL, 10, IntentKind.SELL))
        self.assertTrue(d.allowed)

    def test_safe_halt_blocks_everything(self):
        db, store = self.new_store()
        Recovery(db).complete()
        Reconciler(db, store).reconcile(
            [{"code": CODE, "qty": 10, "avg_price": 100.0}], token="t1")
        store.enter_safe_halt("test")
        gate = OrderGate(db)
        self.assertFalse(gate.check(
            OrderIntent(CODE, OrderSide.BUY, 10, IntentKind.NEW_BUY)).allowed)
        self.assertFalse(gate.check(
            OrderIntent(CODE, OrderSide.SELL, 10, IntentKind.LIQUIDATION)).allowed)


class TestReconciliation(PhoenixTestCase):

    def test_broker_wins_on_quantity_mismatch(self):
        db, store = self.new_store()
        # projection: 30주
        store.apply(execution_event(COID, CODE, OrderSide.BUY, 30, 100.0, ord_qty=30))
        self.assertEqual(store.get_position(CODE)["qty"], 30)

        # broker 는 25주 → broker 우선, 이벤트로 반영
        report = Reconciler(db, store).reconcile(
            [{"code": CODE, "qty": 25, "avg_price": 101.0}], token="t1")

        pos = store.get_position(CODE)
        self.assertEqual(pos["qty"], 25)
        self.assertTrue(pos["balance_confirmed"])
        self.assertTrue(any(d.action == "ADJUSTED" and d.code == CODE
                            for d in report))

    def test_projection_position_absent_in_broker_zeroed(self):
        db, store = self.new_store()
        store.apply(execution_event(COID, CODE, OrderSide.BUY, 10, 100.0, ord_qty=10))
        # broker 잔고에 해당 종목 없음 → 0 으로 보정
        Reconciler(db, store).reconcile([], token="t1")
        self.assertEqual(store.get_position(CODE)["qty"], 0)
