"""P0 긴급: 국내 보유종목 복원 + 보유종목 매도 루프 배선 검증.

운영 증거: pyramid_positions.json={}, [익절판정] 0건, 트레일링 미작동.
근본원인: (1) 관심목록에만 스캔 루프가 걸려 관심목록 밖 보유종목이 매도판정을
받지 못함. (2) 복원 포지션의 초기 고가·복원 플래그 처리 필요.

검증:
  - KIS 보유 3종목 + 로컬 {} → 3종목 복원(recovered=True)
  - 평단·수량 정확 + 초기고가=max(평단,현재가)
  - 기존 highest_price/created_at 보존(누락 종목만 추가)
  - 재시작·반복복원에도 중복 포지션 없음(idempotent)
  - 복원 직후 evaluate → [익절판정] 로그 출력
  - +1.5% 미만이면 트레일링 매도 없음
  - +1.5% 도달 후 고점대비 -1.0% → SELL(트레일링)
  - +2.0% → 즉시 전량익절(SELL_ALL) — buy_score 무관
  - recovered 포지션은 시간청산 미적용(실제 매수시각 불명)
  - 관심목록 밖 보유종목이 스캔 리스트에 포함(_build_kr_scan_list)
  - _sync_positions_from_balance: _source!="api"(캐시/오류/빈응답)면 정합화 스킵
"""
import os
import sys
import ast
import shutil
import tempfile
import logging
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import strategies.pyramid_strategy as ps                       # noqa: E402
from strategies.pyramid_strategy import (                       # noqa: E402
    PyramidStrategyManager, TRAILING_ACTIVATE_PCT, TIME_EXIT_40_MIN,
)
from screener.transaction_cost import (                         # noqa: E402
    price_for_net_pct_from_cost, net_profit_pct_from_cost,
)


def H(qty, avg, name=None, cur=0):
    return {"qty": qty, "avg_price": avg, "name": name, "cur_price": cur}


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kr-restore-")
        self._orig_py = ps.PYRAMID_FILE
        self._orig_cp = ps.COMPOUND_FILE
        ps.PYRAMID_FILE  = os.path.join(self.tmp, "pyramid_positions.json")
        ps.COMPOUND_FILE = os.path.join(self.tmp, "compound_pool.json")
        self.mgr = PyramidStrategyManager(None, max_per_stock=1e9, max_total=1e9)

    def tearDown(self):
        ps.PYRAMID_FILE  = self._orig_py
        ps.COMPOUND_FILE = self._orig_cp
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 복원 ─────────────────────────────────────────────────
    def test_restore_three_holdings_from_empty(self):
        """KIS 보유 3종목 + 로컬 {} → 3종목 복원(recovered=True)."""
        self.assertEqual(self.mgr.positions, {})
        broker = {
            "005930": H(10, 70000, "삼성전자", cur=71000),
            "000660": H(3, 180000, "SK하이닉스", cur=179000),
            "035720": H(5, 50000, "카카오", cur=50000),
        }
        rep = self.mgr.reconcile_from_broker(broker, active_codes=set())
        self.assertEqual(len(self.mgr.positions), 3)
        self.assertEqual(len(rep["added"]), 3)
        for code in ("005930", "000660", "035720"):
            self.assertIn(code, self.mgr.positions)
            self.assertTrue(self.mgr.positions[code].recovered)

    def test_restored_qty_avg_and_initial_high(self):
        """평단·수량 정확 + 초기고가=max(평단,현재가), 현재가<평단이면 평단."""
        broker = {
            "005930": H(10, 70000, "삼성전자", cur=71000),   # 현재가>평단
            "000660": H(3, 180000, "SK하이닉스", cur=179000),  # 현재가<평단
            "035720": H(5, 50000, "카카오", cur=0),          # 현재가 조회실패
        }
        self.mgr.reconcile_from_broker(broker, active_codes=set())
        p1 = self.mgr.positions["005930"]
        self.assertEqual(p1.total_qty, 10)
        self.assertEqual(p1.avg_price, 70000)
        self.assertEqual(p1.highest_price, 71000)     # max(70000, 71000)
        p2 = self.mgr.positions["000660"]
        self.assertEqual(p2.highest_price, 180000)    # max(180000, 179000)=평단
        p3 = self.mgr.positions["035720"]
        self.assertEqual(p3.highest_price, 50000)     # 현재가 0 → 평단

    def test_existing_metadata_preserved(self):
        """기존 포지션 highest_price/created_at 보존 — 누락 종목만 추가."""
        # 기존 로컬 포지션(정상 매수 이력) 심기
        self.mgr.positions["005930"] = self.mgr._build_reconciled_position(
            "005930", "삼성전자", 10, 70000, cur_price=70000)
        p = self.mgr.positions["005930"]
        p.recovered = False
        p.highest_price = 99999          # 과거 실제 고가
        p.created_at = "2020-01-01T09:00:00"
        broker = {
            "005930": H(10, 70000, "삼성전자", cur=71000),   # 이미 존재(교정 불필요)
            "000660": H(3, 180000, "SK하이닉스", cur=181000),  # 신규
        }
        self.mgr.reconcile_from_broker(broker, active_codes=set())
        # 기존 메타 보존
        self.assertEqual(self.mgr.positions["005930"].highest_price, 99999)
        self.assertEqual(self.mgr.positions["005930"].created_at, "2020-01-01T09:00:00")
        self.assertFalse(self.mgr.positions["005930"].recovered)
        # 신규만 추가
        self.assertIn("000660", self.mgr.positions)
        self.assertTrue(self.mgr.positions["000660"].recovered)

    def test_idempotent_no_duplicate_on_repeat_restore(self):
        """반복 복원·재시작에도 중복 포지션 없음 + 저장/로드 왕복 일관."""
        broker = {"005930": H(10, 70000, "삼성전자", cur=71000)}
        self.mgr.reconcile_from_broker(broker, active_codes=set())
        rep2 = self.mgr.reconcile_from_broker(broker, active_codes=set())
        self.assertTrue(rep2["unchanged"])
        self.assertEqual(len(self.mgr.positions), 1)
        # 재시작 모사: 새 매니저(동일 파일) 로드 → recovered 플래그 유지
        mgr2 = PyramidStrategyManager(None, max_per_stock=1e9, max_total=1e9)
        self.assertIn("005930", mgr2.positions)
        self.assertTrue(mgr2.positions["005930"].recovered)
        self.assertEqual(len(mgr2.positions), 1)
        rep3 = mgr2.reconcile_from_broker(broker, active_codes=set())
        self.assertTrue(rep3["unchanged"])
        self.assertEqual(len(mgr2.positions), 1)

    # ── 매도판정(evaluate) ────────────────────────────────────
    def _restore_one(self, avg=70000, qty=10, cur=70000):
        broker = {"005930": H(qty, avg, "삼성전자", cur=cur)}
        self.mgr.reconcile_from_broker(broker, active_codes=set())
        return self.mgr.positions["005930"]

    def test_evaluate_emits_profit_log(self):
        """복원 직후 evaluate → [익절판정] 로그 출력(보유 인식)."""
        self._restore_one(avg=70000, cur=70000)
        with self.assertLogs("PyramidStrategy", level="INFO") as cm:
            self.mgr.evaluate("005930", "삼성전자", 70100, 0, 0.0,
                              today_high=70100, buy_score_norm=0.0, sell_score=0)
        self.assertTrue(any("[익절판정]" in m for m in cm.output))

    def test_no_trailing_below_activate(self):
        """+1.5% 활성화 미달이면 트레일링 매도 없음(HOLD)."""
        pos = self._restore_one(avg=70000, cur=70000)
        # 고가를 +1.0%(활성화 임계 미달)로 설정, 현재가는 고가 대비 -2%
        pos.highest_price = price_for_net_pct_from_cost(70000, 1.0)
        cur = pos.highest_price * 0.98
        d = self.mgr.evaluate("005930", "삼성전자", cur, 0, 0.0,
                              today_high=pos.highest_price, buy_score_norm=0.0,
                              sell_score=0)
        self.assertNotIn(d["action"], ("SELL_ALL", "SELL_PARTIAL"))

    def test_trailing_fires_after_activate(self):
        """+1.5% 도달(고가) 후 고점대비 -1.0% 하락 → SELL(트레일링)."""
        pos = self._restore_one(avg=70000, cur=70000)
        # 고가를 +2.0%(활성화 임계 초과)로, 현재가는 고가 대비 -1.1%
        pos.highest_price = price_for_net_pct_from_cost(70000, 2.0)
        cur = pos.highest_price * (1.0 - 0.011)
        d = self.mgr.evaluate("005930", "삼성전자", cur, 0, 0.0,
                              today_high=pos.highest_price, buy_score_norm=0.0,
                              sell_score=0)
        self.assertEqual(d["action"], "SELL_ALL")
        self.assertIn("트레일링", d["reason"])

    def test_full_profit_immediate_regardless_of_buy_score(self):
        """+2.0% → 즉시 전량익절(SELL_ALL). buy_score/indicator 조건과 무관."""
        self._restore_one(avg=70000, cur=70000)
        cur = price_for_net_pct_from_cost(70000, 2.2)   # 실질 +2.2%
        d = self.mgr.evaluate("005930", "삼성전자", cur, 0, 0.0,
                              today_high=cur, buy_score_norm=0.0, sell_score=0)
        self.assertEqual(d["action"], "SELL_ALL")
        self.assertGreaterEqual(net_profit_pct_from_cost(70000, cur), 2.0)

    def test_recovered_skips_time_exit(self):
        """recovered 포지션은 시간청산 미적용 — 40분 경과·저수익이어도 HOLD."""
        from datetime import datetime, timedelta
        pos = self._restore_one(avg=70000, cur=70000)
        # 실제 매수시각 불명 → recovered=True. created_at 을 과거로(오인 소지) 설정
        pos.created_at = (datetime.now() - timedelta(minutes=TIME_EXIT_40_MIN + 30)).isoformat()
        pos.highest_price = 70050    # 트레일링 미활성(활성 임계 미달)
        cur = 70050                  # 실질 ~0% (시간청산 대상 net)
        d = self.mgr.evaluate("005930", "삼성전자", cur, 0, 0.0,
                              today_high=cur, buy_score_norm=0.0, sell_score=0)
        self.assertNotIn(d["action"], ("SELL_ALL", "SELL_PARTIAL"))

    def test_non_recovered_still_time_exits(self):
        """대조군: 비-recovered 포지션은 동일 조건에서 시간청산 실행."""
        from datetime import datetime, timedelta
        pos = self._restore_one(avg=70000, cur=70000)
        pos.recovered = False
        pos.created_at = (datetime.now() - timedelta(minutes=TIME_EXIT_40_MIN + 30)).isoformat()
        pos.highest_price = 70050
        cur = 70050
        d = self.mgr.evaluate("005930", "삼성전자", cur, 0, 0.0,
                              today_high=cur, buy_score_norm=0.0, sell_score=0)
        self.assertEqual(d["action"], "SELL_ALL")
        self.assertIn("시간청산", d["reason"])


# ── app.py 순수함수 AST 추출(플라스크 미설치 → import 대신 실행) ──────────
class AppWiringTest(unittest.TestCase):
    _APP = os.path.join(_ROOT, "app.py")

    def _extract(self, names, extra_globals=None):
        with open(self._APP, encoding="utf-8") as f:
            mod = ast.parse(f.read())
        fns = [n for n in mod.body
               if isinstance(n, ast.FunctionDef) and n.name in names]
        got = {n.name for n in fns}
        for want in names:
            self.assertIn(want, got, f"app.py 에 {want} 없음")
        ns = {}
        ns.update(extra_globals or {})
        exec(compile(ast.Module(fns, []), "<appfns>", "exec"), ns)
        return ns

    def test_build_kr_scan_list_includes_held_only(self):
        """관심목록 밖 보유종목이 스캔 리스트에 매도판정 대상으로 포함된다."""
        ns = self._extract(["_build_kr_scan_list"])
        build = ns["_build_kr_scan_list"]

        class _P:
            def __init__(self, name):
                self.name = name
        watch = [{"code": "005930", "name": "삼성전자"}]
        positions = {"005930": _P("삼성전자"),   # 이미 관심목록
                     "000660": _P("SK하이닉스"),  # 관심목록 밖 보유
                     "035720": _P("카카오")}       # 관심목록 밖 보유
        scan, held_only = build(watch, positions)
        codes = [s["code"] for s in scan]
        self.assertIn("000660", codes)
        self.assertIn("035720", codes)
        self.assertEqual(sorted(held_only), ["000660", "035720"])
        # held-only 항목은 매도스캔 표식
        held_items = [s for s in scan if s.get("_held_sell_scan")]
        self.assertEqual({s["code"] for s in held_items}, {"000660", "035720"})
        # 관심목록 종목은 중복되지 않음
        self.assertEqual(codes.count("005930"), 1)

    def test_build_kr_scan_list_empty_positions(self):
        ns = self._extract(["_build_kr_scan_list"])
        build = ns["_build_kr_scan_list"]
        watch = [{"code": "005930", "name": "삼성전자"}]
        scan, held_only = build(watch, {})
        self.assertEqual(held_only, [])
        self.assertEqual([s["code"] for s in scan], ["005930"])

    def test_sync_skips_non_api_source(self):
        """_source!='api'(캐시/오류/빈응답) 이면 reconcile 호출 안 함(삭제 금지)."""
        calls = {"reconcile": 0}

        class _Pyr:
            def reconcile_from_broker(self, *a, **k):
                calls["reconcile"] += 1
                return {"added": [], "removed": [], "qty_fixed": [],
                        "avg_fixed": [], "protected": [], "unchanged": True}

        class _SM:
            pyramid = _Pyr()
            def active_order_codes(self, m):
                return set()

        class _Api:
            def __init__(self, src):
                self._src = src
            def get_balance(self):
                return {"_source": self._src, "holdings": []}

        for src in ("cache", "error", "empty", "psbl"):
            calls["reconcile"] = 0
            ns = self._extract(
                ["_sync_positions_from_balance"],
                extra_globals={
                    "_strategy_mgr": _SM(), "_api": _Api(src),
                    "logger": logging.getLogger("t"),
                    "_log": lambda *a, **k: None,
                })
            ns["_sync_positions_from_balance"]()
            self.assertEqual(calls["reconcile"], 0,
                             f"_source={src} 인데 reconcile 호출됨(삭제 위험)")

    def test_sync_calls_reconcile_on_api_source(self):
        """_source=='api' 이면 reconcile 호출(cur_price 포함 브로커맵 전달)."""
        seen = {}

        class _Pyr:
            def reconcile_from_broker(self, broker, active_codes=None, **k):
                seen["broker"] = broker
                return {"added": ["x"], "removed": [], "qty_fixed": [],
                        "avg_fixed": [], "protected": [], "unchanged": False}

        class _SM:
            pyramid = _Pyr()
            def active_order_codes(self, m):
                return set()

        class _Api:
            def get_balance(self):
                return {"_source": "api", "holdings": [
                    {"code": "005930", "name": "삼성전자", "qty": 10,
                     "avg_price": 70000, "cur_price": 71000}]}
        ns = self._extract(
            ["_sync_positions_from_balance"],
            extra_globals={"_strategy_mgr": _SM(), "_api": _Api(),
                           "logger": logging.getLogger("t"),
                           "_log": lambda *a, **k: None})
        ns["_sync_positions_from_balance"]()
        self.assertIn("005930", seen.get("broker", {}))
        self.assertEqual(seen["broker"]["005930"]["cur_price"], 71000)


if __name__ == "__main__":
    unittest.main()
