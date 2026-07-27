"""P0-3 포지션 동기화 — PyramidStrategyManager.reconcile_from_broker 단위 테스트.

원칙 검증:
  - 회계 무개입(apply_buy/apply_sell/손익/compound/재진입 미개입).
  - ACTIVE 주문 종목은 접수→체결 창의 정상 수량차이로 보호(삭제/생성/교정 금지).
  - 수량·존재·평단만 구조적으로 정합화.
  - idempotent (2회 실행 결과 동일).

pyramid_strategy 의 PYRAMID_FILE / COMPOUND_FILE 를 임시 경로로 격리해
실데이터(pyramid_positions.json 등)를 오염시키지 않는다.
"""
import os
import sys
import shutil
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import strategies.pyramid_strategy as ps  # noqa: E402
from strategies.pyramid_strategy import PyramidStrategyManager, PyramidPosition  # noqa: E402


def H(qty, avg, name=None):
    return {"qty": qty, "avg_price": avg, "name": name}


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p0recon-")
        self._orig_py = ps.PYRAMID_FILE
        self._orig_cp = ps.COMPOUND_FILE
        ps.PYRAMID_FILE  = os.path.join(self.tmp, "pyramid_positions.json")
        ps.COMPOUND_FILE = os.path.join(self.tmp, "compound_pool.json")
        # kis_api 는 reconcile 에서 사용하지 않으므로 None
        self.mgr = PyramidStrategyManager(None, max_per_stock=1e9, max_total=1e9)

    def tearDown(self):
        ps.PYRAMID_FILE  = self._orig_py
        ps.COMPOUND_FILE = self._orig_cp
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, code, name, qty, avg):
        """내부 포지션을 구조적으로 심는다(회계 무개입)."""
        self.mgr.positions[code] = self.mgr._build_reconciled_position(
            code, name, qty, avg
        )

    # ── 1. 내부 유령 포지션 제거 ─────────────────────────────────────────
    def test_ghost_position_removed(self):
        self._seed("005930", "삼성전자", 7, 54000)   # 내부에만 존재
        broker = {"000660": H(3, 180000, "SK하이닉스")}   # KIS 엔 다른 종목만
        rep = self.mgr.reconcile_from_broker(broker, active_codes=set())
        self.assertIn("005930", rep["removed"])
        self.assertNotIn("005930", self.mgr.positions)
        self.assertIn("000660", self.mgr.positions)   # 신규 복원

    # ── 2. KIS 보유 포지션 복원 (누락) ───────────────────────────────────
    def test_missing_position_restored(self):
        broker = {"005930": H(10, 54000, "삼성전자")}
        rep = self.mgr.reconcile_from_broker(broker, active_codes=set())
        self.assertTrue(any("005930" in s for s in rep["added"]))
        pos = self.mgr.positions["005930"]
        self.assertEqual(pos.total_qty, 10)
        self.assertEqual(pos.avg_price, 54000)
        # 회계 무개입 확인: compound_pool 불변
        self.assertEqual(self.mgr.compound_pool, 0.0)

    # ── 3. 수량 불일치 교정 ──────────────────────────────────────────────
    def test_qty_mismatch_corrected(self):
        self._seed("005930", "삼성전자", 5, 54000)
        broker = {"005930": H(8, 54000, "삼성전자")}
        rep = self.mgr.reconcile_from_broker(broker, active_codes=set())
        self.assertTrue(rep["qty_fixed"])
        self.assertEqual(self.mgr.positions["005930"].total_qty, 8)

    # ── 4. ACTIVE SELL 중 포지션 조기 삭제 방지 ──────────────────────────
    def test_active_sell_prevents_early_removal(self):
        self._seed("005930", "삼성전자", 7, 54000)
        # KIS 잔고엔 없음(접수→체결 창) + ACTIVE SELL 존재
        broker = {}
        rep = self.mgr.reconcile_from_broker(broker, active_codes={"005930"})
        self.assertNotIn("005930", rep["removed"])
        self.assertIn("005930", self.mgr.positions)          # 보존됨
        self.assertEqual(self.mgr.positions["005930"].total_qty, 7)
        self.assertIn("005930", rep["protected"])

    # ── 5. ACTIVE BUY 중 중복 포지션 생성 방지 ───────────────────────────
    def test_active_buy_prevents_duplicate_create(self):
        # 내부엔 없고 KIS 엔 있음 + ACTIVE BUY → 생성 금지(체결 시 반영)
        broker = {"005930": H(10, 54000, "삼성전자")}
        rep = self.mgr.reconcile_from_broker(broker, active_codes={"005930"})
        self.assertEqual(rep["added"], [])
        self.assertNotIn("005930", self.mgr.positions)
        self.assertIn("005930", rep["protected"])

    # ── 6. 부분체결 수량 유지 (ACTIVE 는 교정 금지) ──────────────────────
    def test_partial_fill_qty_preserved(self):
        self._seed("005930", "삼성전자", 10, 54000)
        # 부분체결로 KIS 는 4주만 반영, 그러나 ACTIVE(부분체결 중)
        broker = {"005930": H(4, 54000, "삼성전자")}
        rep = self.mgr.reconcile_from_broker(broker, active_codes={"005930"})
        self.assertEqual(rep["qty_fixed"], [])
        self.assertEqual(self.mgr.positions["005930"].total_qty, 10)  # 유지
        self.assertIn("005930", rep["protected"])

    # ── 7. 잔고 조회 실패 시 기존 포지션 보존 ────────────────────────────
    def test_balance_failure_preserves_positions(self):
        self._seed("005930", "삼성전자", 7, 54000)
        self._seed("000660", "SK하이닉스", 3, 180000)
        before = {c: p.total_qty for c, p in self.mgr.positions.items()}
        # 조회 실패 = broker_holdings None (app._sync 는 예외 시 reconcile 호출 안 함;
        #   reconcile 자체도 None 을 '데이터 없음 → 전체 보존' 으로 방어)
        rep = self.mgr.reconcile_from_broker(None, active_codes=set())
        self.assertTrue(rep["unchanged"])
        after = {c: p.total_qty for c, p in self.mgr.positions.items()}
        self.assertEqual(before, after)   # 그대로 보존

    # ── 3b. 교정 시 level_entries / full_entry_done 보존(전략 무손실) ─────
    def test_correction_preserves_level_entries_and_full_entry(self):
        # 다단계 + Full Entry 완료 마크가 있는 기존 포지션
        pos = PyramidPosition("005930", "삼성전자", 50000)
        pos.add_level(1, 3, 50000)
        pos.add_level(2, 2, 51000)
        pos.level_entries["full_entry_done"] = {"price": 51000, "qty": 2,
                                                "added_at": "2026-07-27T09:00:00"}
        pos.current_level = 2
        self.mgr.positions["005930"] = pos
        entries_before = {k: dict(v) for k, v in pos.level_entries.items()}
        level_before   = pos.current_level

        # KIS 가 수량·평단을 다르게 보고 → 교정 발생
        broker = {"005930": H(5, 52000, "삼성전자")}
        rep = self.mgr.reconcile_from_broker(broker, active_codes=set())

        # 요약값만 교정
        self.assertEqual(self.mgr.positions["005930"].total_qty, 5)
        self.assertEqual(self.mgr.positions["005930"].avg_price, 52000)
        # ★ level_entries / full_entry_done / current_level 은 그대로 보존
        self.assertIn("full_entry_done", self.mgr.positions["005930"].level_entries)
        self.assertEqual(self.mgr.positions["005930"].level_entries, entries_before)
        self.assertEqual(self.mgr.positions["005930"].current_level, level_before)
        self.assertTrue(rep["qty_fixed"] or rep["avg_fixed"])

    # ── 8. 재시작 후 동일 동기화 2회 실행 시 결과 동일 (idempotent) ───────
    def test_idempotent_double_run(self):
        self._seed("005930", "삼성전자", 5, 50000)   # qty/avg 불일치 예정
        self._seed("999999", "유령",     1, 1000)    # 유령(제거 예정)
        broker = {
            "005930": H(8, 54000, "삼성전자"),          # 교정
            "000660": H(3, 180000, "SK하이닉스"),       # 복원
        }
        active = set()
        rep1 = self.mgr.reconcile_from_broker(broker, active_codes=active)
        self.assertFalse(rep1["unchanged"])   # 1회차: 변경 발생
        snap1 = {c: p.to_dict() for c, p in self.mgr.positions.items()}

        rep2 = self.mgr.reconcile_from_broker(broker, active_codes=active)
        self.assertTrue(rep2["unchanged"])    # 2회차: 변경 없음
        snap2 = {c: p.to_dict() for c, p in self.mgr.positions.items()}

        # 상태 완전 동일 (idempotent)
        self.assertEqual(set(snap1), {"005930", "000660"})
        self.assertEqual(snap1, snap2)


if __name__ == "__main__":
    unittest.main()
