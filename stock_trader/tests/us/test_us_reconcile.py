"""US 포지션 정합화 판정 테스트 (§6/§9A/§9B — 순수 로직, fail-safe, 격리)."""
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
    # ── §9A 운영 사고 재현: 교집합 0, 7복원 / 9 broker_absent(격리 후보) ─────
    def test_incident_7_vs_9(self):
        # 완전 스냅샷(complete=True)에서만 broker_absent(격리 후보) 판정
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(KIS_7),
                                  internal_symbols=INTERNAL_9,
                                  complete=True)
        self.assertTrue(r.authoritative)
        self.assertTrue(r.buy_allowed)
        # 교집합 0 → 7 전부 복원, 9 전부 broker_absent(격리 후보 — 삭제 아님)
        self.assertEqual(sorted(x["symbol"] for x in r.to_restore), sorted(KIS_7))
        self.assertEqual(r.broker_absent, sorted(INTERNAL_9))
        self.assertEqual(r.to_reconcile, [])   # 교집합 없음
        self.assertEqual(r.broker_count, 7)
        self.assertEqual(r.internal_count, 9)

    def test_incomplete_snapshot_restore_only_no_broker_absent(self):
        # §5/§1: 완전성 증거 없음(complete=False, 기본) → positive holding 복원만,
        #        broker 부재 판정·격리 금지(broker_absent=[])
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(KIS_7),
                                  internal_symbols=INTERNAL_9)  # complete 기본 False
        self.assertTrue(r.authoritative)
        self.assertTrue(r.buy_allowed)
        self.assertEqual(sorted(x["symbol"] for x in r.to_restore), sorted(KIS_7))
        self.assertEqual(r.broker_absent, [])     # 격리 금지
        self.assertFalse(r.complete)
        self.assertIn("restore positive holdings only", r.reason)

    def test_incomplete_snapshot_intersection_reconcile_no_broker_absent(self):
        # 부분 스냅샷이라도 교집합 정합(qty/avg 갱신)은 허용, broker_absent 만 금지
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(["IONQ", "NNE"]),
                                  internal_symbols=["IONQ", "ASTS"])  # complete=False
        self.assertEqual([x["symbol"] for x in r.to_reconcile], ["IONQ"])
        self.assertEqual([x["symbol"] for x in r.to_restore], ["NNE"])
        self.assertEqual(r.broker_absent, [])   # ASTS 격리/삭제 금지(불완전)

    def test_restore_highest_is_max_avg_cur(self):
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=[{"symbol": "IONQ", "qty": 5,
                                             "avg_price": 40.0, "cur_price": 38.0}],
                                  internal_symbols=[])
        rec = r.to_restore[0]
        self.assertEqual(rec["highest_price"], 40.0)   # max(avg, cur)=40

    def test_qty_zero_not_restored(self):
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(["ACHR"], qty=0),
                                  internal_symbols=[])
        self.assertEqual(r.to_restore, [])

    def test_intersection_reconcile_not_restore_or_absent(self):
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(["IONQ", "NNE"]),
                                  internal_symbols=["IONQ", "ASTS"],
                                  complete=True)
        self.assertEqual([x["symbol"] for x in r.to_reconcile], ["IONQ"])
        self.assertEqual([x["symbol"] for x in r.to_restore], ["NNE"])
        self.assertEqual(r.broker_absent, ["ASTS"])

    # ── 격리(quarantine) 인지 판정 ──────────────────────────────
    def test_quarantined_excluded_from_broker_absent(self):
        # 이미 격리된 심볼은 broker_absent 재판정에서 제외
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(["IONQ"]),
                                  internal_symbols=["IONQ", "GONE", "QUAR"],
                                  complete=True,
                                  quarantined_symbols=["QUAR"])
        # GONE 은 active-broker부재 → 격리후보 / QUAR 은 이미 격리 → 제외
        self.assertEqual(r.broker_absent, ["GONE"])
        self.assertEqual(r.active_internal_count, 2)   # IONQ, GONE
        self.assertEqual(r.quarantined_count, 1)       # QUAR

    def test_quarantined_reappears_in_broker(self):
        # 격리중 심볼이 broker 잔고에 재등장 → reappeared + to_restore(격리해제 대상)
        r = RC.reconcile_decision(ok=True, source="api",
                                  holdings=_holdings(["QUAR"]),
                                  internal_symbols=["IONQ", "QUAR"],
                                  complete=True,
                                  quarantined_symbols=["QUAR"])
        self.assertEqual(r.reappeared, ["QUAR"])
        self.assertEqual([x["symbol"] for x in r.to_restore], ["QUAR"])
        self.assertEqual(r.broker_absent, ["IONQ"])   # IONQ active·broker부재 → 격리후보

    def test_empty_broker_with_internal_active_is_not_authoritative(self):
        # ★ P0: 완전·빈 잔고라도 내부 active 존재 → 권위 0잔고로 확정 금지.
        #   authoritative=False, buy_allowed=False, 격리 후보 없음(내부 유지).
        r = RC.reconcile_decision(ok=True, source="api", holdings=[],
                                  internal_symbols=["AAA", "BBB"], complete=True)
        self.assertFalse(r.authoritative)
        self.assertFalse(r.buy_allowed)
        self.assertFalse(r.authoritative_empty)
        self.assertEqual(r.broker_absent, [])
        self.assertEqual(r.active_internal_count, 2)

    def test_empty_broker_genuinely_empty_account_authoritative(self):
        # 내부 active 도 없고 broker 도 완전-빈 → 진짜 빈 계좌(정상). authoritative_empty=True.
        r = RC.reconcile_decision(ok=True, source="api", holdings=[],
                                  internal_symbols=[], complete=True)
        self.assertTrue(r.authoritative)
        self.assertTrue(r.authoritative_empty)
        self.assertTrue(r.buy_allowed)
        self.assertEqual(r.broker_absent, [])

    def test_nonempty_complete_quarantines_absent(self):
        # 양성 잔고 증거(broker 비어있지 않음) + 완전 스냅샷 → 부재 종목만 격리 후보(정상 경로 유지)
        r = RC.reconcile_decision(
            ok=True, source="api",
            holdings=[{"symbol": "AAA", "qty": 10, "avg_price": 100.0, "cur_price": 101.0}],
            internal_symbols=["AAA", "BBB"], complete=True)
        self.assertTrue(r.authoritative)
        self.assertEqual(r.broker_absent, ["BBB"])   # BBB 만 부재 → 격리 후보

    def test_incomplete_empty_not_authoritative_empty(self):
        # 불완전 빈 잔고 + 내부 active → 권위 0잔고 아님, 격리 금지(P0 규칙)
        r = RC.reconcile_decision(ok=True, source="api", holdings=[],
                                  internal_symbols=["AAA"], complete=False)
        self.assertFalse(r.authoritative)
        self.assertFalse(r.authoritative_empty)
        self.assertEqual(r.broker_absent, [])


class ReconcileFailSafeTest(unittest.TestCase):
    # ── §9B 조회 실패/불명확 → 삭제·격리·복원 금지, BUY만 스킵 ─────
    def _assert_failsafe(self, **kw):
        r = RC.reconcile_decision(internal_symbols=INTERNAL_9, **kw)
        self.assertFalse(r.authoritative)
        self.assertFalse(r.buy_allowed)          # 신규 BUY 스킵
        self.assertEqual(r.broker_absent, [])    # 격리/삭제 금지
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
        self._assert_failsafe(ok=True, source="api", holdings={"unexpected": 1})

    def test_is_authoritative_matrix(self):
        self.assertTrue(RC.is_authoritative(True, "api", []))       # 빈 리스트=정상 빈 잔고
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
        self.assertEqual(h["broker_absent_count"], 9)
        blob = str(h)
        for pii in ("token", "account", "cano", "acnt", "odno"):
            self.assertNotIn(pii, blob.lower())


if __name__ == "__main__":
    unittest.main()
