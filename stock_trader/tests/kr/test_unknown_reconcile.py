"""UNKNOWN 정합화(reconciliation) — 시간경과 자동해제 금지, 증거 기반 해소."""
import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from journal.unknown_order_ledger import UnknownOrderLedger        # noqa: E402
from journal.unknown_order_reconciler import reconcile_unknown_orders  # noqa: E402


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = UnknownOrderLedger(os.path.join(self.tmp, "j.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, code="005930", qty=1, price=70000):
        return self.led.record("ACC-01", "KR", code, "BUY", qty, price, "00",
                               created_at="2026-08-07T10:00:00", created_hhmmss="100000")

    def test_order_found_promotes_accepted(self):
        """주문 발견(ODNO, 미체결) → 승격 성공 시 RESOLVED_ACCEPTED(차단 해제)."""
        rid = self._seed()
        promoted = []
        prov = lambda row: {"query_ok": True, "candidates": [
            {"odno": "0007", "qty": 1, "price": 70000,
             "cum_filled_qty": 0, "unfilled_qty": 1, "order_status": "접수"}]}
        res = reconcile_unknown_orders(
            self.led, prov, on_promote=lambda r, c: promoted.append(c["odno"]) or True,
            now_iso="t2")
        self.assertEqual(res, [(rid, "RESOLVED_ACCEPTED")])
        self.assertEqual(promoted, ["0007"])
        self.assertEqual(self.led.get(rid)["odno"], "0007")
        self.assertFalse(self.led.has_active("ACC-01", "KR", "005930", "BUY"))

    def test_fill_found_books_once(self):
        """체결 발견 → 부킹 성공 시 정확히 1회 부킹 + RESOLVED_FILLED."""
        rid = self._seed()
        booked = []
        prov = lambda row: {"query_ok": True, "candidates": [
            {"odno": "0008", "qty": 1, "price": 70000,
             "cum_filled_qty": 1, "unfilled_qty": 0, "avg_fill_price": 70050}]}
        res = reconcile_unknown_orders(
            self.led, prov, on_fill=lambda r, c: booked.append(c["odno"]) or True,
            now_iso="t2")
        self.assertEqual(res, [(rid, "RESOLVED_FILLED")])
        self.assertEqual(len(booked), 1)               # 정확히 1회
        # 재실행: 이미 RESOLVED → 다시 부킹하지 않음
        res2 = reconcile_unknown_orders(self.led, prov,
                                        on_fill=lambda r, c: booked.append(c) or True)
        self.assertEqual(res2, [])
        self.assertEqual(len(booked), 1)

    def test_single_zero_candidate_keeps_blocked(self):
        """단발 0건 → UNKNOWN_NOT_FOUND 로 계속 차단(자동 미접수 확정 금지)."""
        rid = self._seed()
        prov = lambda row: {"query_ok": True, "candidates": []}
        res = reconcile_unknown_orders(self.led, prov, now_iso="t2")
        self.assertEqual(res, [(rid, "UNKNOWN_NOT_FOUND")])
        self.assertTrue(self.led.has_active("ACC-01", "KR", "005930", "BUY"))
        self.assertEqual(self.led.get(rid)["status"], "UNKNOWN_NOT_FOUND")
        self.assertEqual(self.led.get(rid)["not_found_streak"], 1)

    def test_zero_candidate_100x_never_auto_resolves(self):
        """동일조건 0건을 100회 조회해도 자동해제 없음 — UNKNOWN 유지·BUY 차단."""
        rid = self._seed()
        prov = lambda row: {"query_ok": True, "candidates": []}
        for i in range(100):
            res = reconcile_unknown_orders(self.led, prov, now_iso=f"t{i}")
            self.assertEqual(res, [(rid, "UNKNOWN_NOT_FOUND")])
        # 100회 후에도 여전히 차단, RESOLVED_* 로 전이되지 않음
        self.assertTrue(self.led.has_active("ACC-01", "KR", "005930", "BUY"))
        row = self.led.get(rid)
        self.assertEqual(row["status"], "UNKNOWN_NOT_FOUND")
        self.assertFalse(row["status"].startswith("RESOLVED_"))
        self.assertEqual(row["resolved_at"], "")
        self.assertEqual(row["not_found_streak"], 100)

    def test_candidate_after_zero_streak_promotes(self):
        """0건 누적 도중 후보가 늦게 나타나면(조회지연) 승격으로 해소."""
        rid = self._seed()
        zero = lambda row: {"query_ok": True, "candidates": []}
        reconcile_unknown_orders(self.led, zero, now_iso="t1")
        reconcile_unknown_orders(self.led, zero, now_iso="t2")
        self.assertEqual(self.led.get(rid)["status"], "UNKNOWN_NOT_FOUND")
        self.assertEqual(self.led.get(rid)["not_found_streak"], 2)
        # 이후 후보 발견 → 승격(재점검 대상이므로 UNKNOWN_NOT_FOUND 도 처리됨)
        found = lambda row: {"query_ok": True, "candidates": [
            {"odno": "0007", "qty": 1, "price": 70000, "cum_filled_qty": 0}]}
        res = reconcile_unknown_orders(
            self.led, found, on_promote=lambda r, c: True, now_iso="t3")
        self.assertEqual(res, [(rid, "RESOLVED_ACCEPTED")])
        self.assertFalse(self.led.has_active("ACC-01", "KR", "005930", "BUY"))

    def test_multiple_candidates_ambiguous_stays_blocked(self):
        """동일조건 후보 다수 → 식별 불가 → AMBIGUOUS_MATCH(자동해제·재주문 금지, 계속 차단)."""
        rid = self._seed()
        prov = lambda row: {"query_ok": True, "candidates": [
            {"odno": "1", "qty": 1, "price": 70000, "cum_filled_qty": 0},
            {"odno": "2", "qty": 1, "price": 70000, "cum_filled_qty": 0}]}
        res = reconcile_unknown_orders(self.led, prov, now_iso="t2")
        self.assertEqual(res, [(rid, "AMBIGUOUS_MATCH")])
        self.assertTrue(self.led.has_active("ACC-01", "KR", "005930", "BUY"))  # 계속 차단
        # AMBIGUOUS_MATCH 는 재실행에서 자동 변경 안 함(수동확인)
        res2 = reconcile_unknown_orders(self.led, prov, now_iso="t3")
        self.assertEqual(res2, [])

    def test_query_fail_keeps_pending(self):
        """조회 실패 → 시간경과 자동해제 없이 PENDING 유지(계속 차단)."""
        rid = self._seed()
        prov = lambda row: {"query_ok": False, "candidates": []}
        res = reconcile_unknown_orders(self.led, prov, now_iso="t2")
        self.assertEqual(res, [(rid, "KEEP_PENDING_QUERY_FAIL")])
        self.assertTrue(self.led.has_active("ACC-01", "KR", "005930", "BUY"))
        self.assertEqual(self.led.get(rid)["last_checked_at"], "t2")

    def test_promote_fail_stays_blocked(self):
        """주문 발견했으나 승격 실패 → MANUAL_REVIEW(차단 유지, 무단 해제 금지)."""
        rid = self._seed()
        prov = lambda row: {"query_ok": True, "candidates": [
            {"odno": "9", "qty": 1, "price": 70000, "cum_filled_qty": 0}]}
        res = reconcile_unknown_orders(self.led, prov,
                                       on_promote=lambda r, c: False, now_iso="t2")
        self.assertEqual(res, [(rid, "MANUAL_REVIEW")])
        self.assertTrue(self.led.has_active("ACC-01", "KR", "005930", "BUY"))

    def test_qty_price_mismatch_not_matched(self):
        """수량·가격이 다르면 후보로 매칭하지 않음(다른 주문) → 매칭 0건 = UNKNOWN 유지."""
        rid = self._seed(qty=1, price=70000)
        prov = lambda row: {"query_ok": True, "candidates": [
            {"odno": "5", "qty": 2, "price": 70000, "cum_filled_qty": 0},  # 수량 다름
            {"odno": "6", "qty": 1, "price": 71000, "cum_filled_qty": 0}]}  # 가격 다름
        # 매칭 0건 → 자동해제 금지, UNKNOWN_NOT_FOUND 로 계속 차단
        res = reconcile_unknown_orders(self.led, prov, now_iso="t2")
        self.assertEqual(res, [(rid, "UNKNOWN_NOT_FOUND")])
        self.assertTrue(self.led.has_active("ACC-01", "KR", "005930", "BUY"))

    def test_manual_release_requires_reason(self):
        """수동 해제: 빈 사유는 거부(ValueError), 명시적 사유가 있어야만 해제."""
        rid = self._seed()
        with self.assertRaises(ValueError):
            self.led.release_unknown(rid, "", operator="op1")
        with self.assertRaises(ValueError):
            self.led.release_unknown(rid, "   ", operator="op1")
        self.assertTrue(self.led.has_active("ACC-01", "KR", "005930", "BUY"))
        # 명시적 사유 → 해제
        ok = self.led.release_unknown(rid, "브로커 확인: 미접수 확정", operator="op1",
                                      ts="t9")
        self.assertTrue(ok)
        self.assertFalse(self.led.has_active("ACC-01", "KR", "005930", "BUY"))
        row = self.led.get(rid)
        self.assertEqual(row["status"], "RESOLVED_MANUAL")
        self.assertEqual(row["resolved_at"], "t9")
        # 이력에 이전 상태·사유 기록
        import json as _j
        hist = _j.loads(row["history"])
        last = hist[-1]
        self.assertEqual(last["event"], "RESOLVED_MANUAL")
        self.assertIn("prev=PENDING", last["note"])
        self.assertIn("브로커 확인", last["note"])

    def test_manual_release_after_not_found_unblocks_buy(self):
        """UNKNOWN_NOT_FOUND 도 명시적 사유 수동해제 후에만 BUY 허용."""
        rid = self._seed()
        zero = lambda row: {"query_ok": True, "candidates": []}
        reconcile_unknown_orders(self.led, zero, now_iso="t1")
        self.assertTrue(self.led.has_active("ACC-01", "KR", "005930", "BUY"))
        ok = self.led.release_unknown(rid, "수기 확인: 접수 안 됨", operator="op2",
                                      ts="t2")
        self.assertTrue(ok)
        self.assertFalse(self.led.has_active("ACC-01", "KR", "005930", "BUY"))
        # 중복 해제 방지: 이미 해소 → False
        self.assertFalse(self.led.release_unknown(rid, "재시도", operator="op2"))


class TestKisCandidateProviderAndReconcile(unittest.TestCase):
    """_kr_list_orders_today 파싱 + reconcile_kr_unknowns 통합(주문 발견 승격)."""

    def setUp(self):
        import api.kis_api as kmod
        from api.kis_api import KISApi
        self.kmod = kmod
        self._g = kmod.requests.get
        self.tmp = tempfile.mkdtemp()
        api = object.__new__(KISApi)
        api.base_url = "https://mock"
        api.account_no = "12345678-01"
        api._headers = lambda *a, **k: {}
        api._rate_limit = lambda *a, **k: None
        api._on_api_success = lambda: None
        api._unknown_ledger = UnknownOrderLedger(os.path.join(self.tmp, "j.db"))
        self.api = api

    def tearDown(self):
        self.kmod.requests.get = self._g
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_orders_parses_and_filters_by_time(self):
        class _R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"rt_cd": "0", "output1": [
                    {"odno": "A1", "pdno": "005930", "ord_qty": "1",
                     "ord_unpr": "70000", "tot_ccld_qty": "0", "rmn_qty": "1",
                     "ord_tmd": "101500", "ord_stts_name": "접수"},
                    {"odno": "OLD", "pdno": "005930", "ord_qty": "1",
                     "ord_unpr": "70000", "tot_ccld_qty": "0", "rmn_qty": "1",
                     "ord_tmd": "095000", "ord_stts_name": "접수"}]}
        self.kmod.requests.get = lambda *a, **k: _R()
        out = self.api._kr_list_orders_today("005930", "BUY", after_hhmmss="100000")
        self.assertTrue(out["query_ok"])
        # 발생시각(10:00:00) 이전 OLD 는 제외, A1 만
        self.assertEqual([c["odno"] for c in out["candidates"]], ["A1"])
        self.assertEqual(out["candidates"][0]["price"], 70000)

    def test_reconcile_kr_unknowns_promotes(self):
        rid = self.api._unknown_ledger.record(
            "12345678-01", "KR", "005930", "BUY", 1, 70000, "00",
            created_at="t", created_hhmmss="100000")

        class _R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"rt_cd": "0", "output1": [
                    {"odno": "Z9", "pdno": "005930", "ord_qty": "1",
                     "ord_unpr": "70000", "tot_ccld_qty": "0", "rmn_qty": "1",
                     "ord_tmd": "101000", "ord_stts_name": "접수"}]}
        self.kmod.requests.get = lambda *a, **k: _R()
        promoted = []
        res = self.api.reconcile_kr_unknowns(
            on_promote=lambda r, c: promoted.append(c["odno"]) or True)
        self.assertEqual(res, [(rid, "RESOLVED_ACCEPTED")])
        self.assertEqual(promoted, ["Z9"])
        self.assertFalse(self.api._unknown_ledger.has_active(
            "12345678-01", "KR", "005930", "BUY"))

    def test_query_fail_keeps_blocked(self):
        self.api._unknown_ledger.record(
            "12345678-01", "KR", "005930", "BUY", 1, 70000, "00",
            created_at="t", created_hhmmss="100000")

        class _R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"rt_cd": "1", "msg1": "조회오류"}
        self.kmod.requests.get = lambda *a, **k: _R()
        res = self.api.reconcile_kr_unknowns(on_promote=lambda r, c: True)
        self.assertEqual(res[0][1], "KEEP_PENDING_QUERY_FAIL")
        self.assertTrue(self.api._unknown_ledger.has_active(
            "12345678-01", "KR", "005930", "BUY"))   # 계속 차단


if __name__ == "__main__":
    unittest.main()
