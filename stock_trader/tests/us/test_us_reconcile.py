"""US 포지션 정합화 판정 테스트 (§6/§9A/§9B — 순수 로직, fail-safe)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import strategies.us_reconcile as RC

# 운영 사고 재현 데이터
KIS_7 = ["ACHR", "BLNK", "IONQ", "NNE", "NVTS", "QUBT", "RGTI"]
INTERNAL_9 = ["ASTS", "BBAI", "BLZE", "CCJ", "CEG", "CRSP", "IOVA", "LEU", "RKLB"]


def _holdings(syms, qty=10, avg=50.0, cur=52.0):
    return [{"symbol": s, "qty": qty, "avg_price": avg, "cur_price": cur,
             "name": s, "excd": "NASD"} for s in syms]


class ReconcileIncidentTest(unittest.TestCase):
    # ── §9A 운영 사고 재현: 교집합 0, 7복원 / 9 stale ─────────
    def test_incident_7_vs_9(self):
        # 완전 스냅샷(complete=True)에서만 stale 삭제 허용
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(KIS_7),
                                  internal_symbols=INTERNAL_9,
                                  complete=True)
        self.assertTrue(r.authoritative)
        self.assertTrue(r.buy_allowed)
        # 교집합 0 → 7 전부 복원, 9 전부 stale
        self.assertEqual(sorted(x["symbol"] for x in r.to_restore), sorted(KIS_7))
        self.assertEqual(r.to_stale_remove, sorted(INTERNAL_9))
        self.assertEqual(r.to_reconcile, [])   # 교집합 없음
        self.assertEqual(r.broker_count, 7)
        self.assertEqual(r.internal_count, 9)

    def test_incomplete_snapshot_restore_only_no_delete(self):
        # §5: 완전성 증거 없음(complete=False, 기본) → 복원만, stale 삭제 0
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(KIS_7),
                                  internal_symbols=INTERNAL_9)  # complete 기본 False
        self.assertTrue(r.authoritative)
        self.assertTrue(r.buy_allowed)
        # 7 전부 복원은 그대로 허용
        self.assertEqual(sorted(x["symbol"] for x in r.to_restore), sorted(KIS_7))
        # 그러나 stale 삭제는 금지(부분 스냅샷일 수 있음)
        self.assertEqual(r.to_stale_remove, [])
        self.assertIn("restore-only", r.reason)

    def test_incomplete_snapshot_intersection_reconcile_no_delete(self):
        # 부분 스냅샷이라도 교집합 정합(qty/avg 갱신)은 허용, stale 삭제만 금지
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(["IONQ", "NNE"]),
                                  internal_symbols=["IONQ", "ASTS"])  # complete=False
        self.assertEqual([x["symbol"] for x in r.to_reconcile], ["IONQ"])
        self.assertEqual([x["symbol"] for x in r.to_restore], ["NNE"])
        self.assertEqual(r.to_stale_remove, [])   # ASTS 삭제 금지

    def test_restore_highest_is_max_avg_cur(self):
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=[{"symbol": "IONQ", "qty": 5,
                                             "avg_price": 40.0, "cur_price": 38.0}],
                                  internal_symbols=[])
        rec = r.to_restore[0]
        # highest_price = max(avg, cur) = 40 (현재가가 더 낮아도 avg 로)
        self.assertEqual(rec["highest_price"], 40.0)

    def test_qty_zero_not_restored(self):
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(["ACHR"], qty=0),
                                  internal_symbols=[])
        self.assertEqual(r.to_restore, [])   # qty<=0 은 보유 아님

    def test_intersection_reconcile_not_restore_or_stale(self):
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(["IONQ", "NNE"]),
                                  internal_symbols=["IONQ", "ASTS"],
                                  complete=True)
        self.assertEqual([x["symbol"] for x in r.to_reconcile], ["IONQ"])
        self.assertEqual([x["symbol"] for x in r.to_restore], ["NNE"])
        self.assertEqual(r.to_stale_remove, ["ASTS"])


class ReconcileFailSafeTest(unittest.TestCase):
    # ── §9B 조회 실패/불명확 → 삭제·복원 금지, BUY만 스킵 ─────
    def _assert_failsafe(self, **kw):
        r = RC.reconcile_decision(internal_symbols=INTERNAL_9, **kw)
        self.assertFalse(r.authoritative)
        self.assertFalse(r.buy_allowed)          # 신규 BUY 스킵
        self.assertEqual(r.to_stale_remove, [])  # 삭제 금지
        self.assertEqual(r.to_restore, [])       # 복원 강제 안 함
        self.assertEqual(r.internal_count, 9)    # 내부 보존
        return r

    def test_api_error(self):
        self._assert_failsafe(ok=False, source="api", holdings=[])

    def test_source_cache(self):
        self._assert_failsafe(ok=True, source="cache", holdings=_holdings(KIS_7))

    def test_source_none(self):
        self._assert_failsafe(ok=True, source=None, holdings=_holdings(KIS_7))

    def test_holdings_not_list_parse_fail(self):
        self._assert_failsafe(ok=True, source="api", holdings=None)

    def test_empty_dict_response(self):
        # 빈/불명확 응답(형태 이상) → 비권위
        self._assert_failsafe(ok=True, source="api", holdings={"unexpected": 1})

    def test_is_authoritative_matrix(self):
        self.assertTrue(RC.is_authoritative(True, "api", []))
        self.assertFalse(RC.is_authoritative(True, "API_CACHE", []))
        self.assertFalse(RC.is_authoritative(False, "api", []))
        self.assertFalse(RC.is_authoritative(True, "api", None))

    def test_health_snapshot_no_pii(self):
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(KIS_7),
                                  internal_symbols=INTERNAL_9,
                                  complete=True)
        h = r.health()
        self.assertEqual(h["restored_count"], 7)
        self.assertEqual(h["stale_removed_count"], 9)
        # PII 필드 부재
        blob = str(h)
        for pii in ("token", "account", "cano", "acnt", "odno"):
            self.assertNotIn(pii, blob.lower())


if __name__ == "__main__":
    unittest.main()
