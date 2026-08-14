"""US 실보유 정합화 + 복원 보호 + 손실회복 트레일링 — 통합 테스트 (§9/§10/§11).

★ 순수 모듈 단위테스트(test_us_recovery/reconcile/position_store)와 달리, 이 파일은
  **실 스택**을 연결해 검증한다:
    - 실 USStrategyManager (run()이 호출하는 _manage_position 매도 판정 경로)
    - 실 USPositionManager + 실 AtomicPositionStore(us_positions.json 원자 저장)
    - 실 us_reconcile.reconcile_decision (스케줄러 잡이 호출하는 us_reconcile_positions)
    - 실 PendingOrderRegistry + OrderLifecycle (crash-safe submit-intent → SELL 파이프라인)
  KIS API 만 Fake 로 대체한다(주문/잔고). 저널·lifecycle·registry·포지션 파일은
  모두 임시 디렉터리로 격리해 실 DB/파일을 오염시키지 않는다.

시나리오(§10 14종 + §9 7-vs-9 사고 재현):
  A app-start 7종목 복원 / B 복원 직후 SELL 0 / C 다음 루프 전 종목 evaluate
  D -5% RECOVERY 저장 / E 재시작 후 RECOVERY 유지 / F 반등 후 고점-0.70% 실매도 1회
  G -6% 실매도 1회 / H 15분 경계 실매도 1회 / I 타임아웃 후 중복 SELL 없음
  J 체결 후에만 포지션 제거 / K 불완전 스냅샷 → stale 0삭제 / L 완전 스냅샷만 stale 정리
  M 불일치 → BUY 스킵·SELL 정상 / N KR·US 동시 실행 무데드락 / §9 7-vs-9 사고 재현
"""
import os
import sys
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import strategies.us_strategy_manager as USM
import strategies.us_recovery as R
import journal.fill_observer as FO
from phoenix.lifecycle import OrderLifecycleManager


# ══════════════════════════════════════════════════════════════
# Fake KIS API — 주문/잔고만 대체(부수효과 없음, 호출 기록)
# ══════════════════════════════════════════════════════════════
class FakeUSApi:
    def __init__(self):
        self.sell_calls  = []
        self.sell_rt_cd  = "0"       # "0"=접수(ODNO) / "9"=타임아웃(UNKNOWN) / "reject"
        self.sell_odno   = "ODNO-TEST-1"
        self.balance_full = {"ok": True, "source": "api", "complete": True,
                             "holdings": []}

    # 정합화용 완전 스냅샷
    def get_us_balance_full(self, max_pages: int = 20):
        return json.loads(json.dumps(self.balance_full))   # deep copy

    # 구식 복원 폴백용
    def get_us_balance(self):
        return {"holdings": self.balance_full.get("holdings", [])}

    def get_usd_exchange_rate(self):
        return 1300.0

    def sell_us(self, symbol, qty, price, excd):
        self.sell_calls.append({"symbol": symbol, "qty": qty,
                                "price": price, "excd": excd})
        if self.sell_rt_cd == "0":
            return {"rt_cd": "0", "msg1": "매도접수성공",
                    "output": {"ODNO": self.sell_odno}}
        if self.sell_rt_cd == "9":
            return {"rt_cd": "9", "msg1": "timeout(예외 래핑)"}   # UNKNOWN_CONFIRM
        return {"rt_cd": "1", "msg1": "매도가능수량 부족"}         # 명확 거절


def _holding(sym, qty=10, avg=100.0, cur=100.0, name=None, excd="NASD"):
    return {"symbol": sym, "qty": qty, "avg_price": avg, "cur_price": cur,
            "name": name or sym, "excd": excd}


def _iv(sell_score=0, intraday=0.0, rsi=50.0, vol=2.0, above_vwap=True,
        macd_above=True, ema9=0.0):
    # _manage_position/_check_entry 가 참조하는 최소 지표 셋(추세매도 미유발 기본값)
    return {"sell_score": sell_score, "intraday_pct": intraday, "rsi": rsi,
            "vol_ratio": vol, "above_vwap": above_vwap, "macd_above": macd_above,
            "ema9": ema9, "buy_score": 0, "vwap": 0.0}


SESS = {"session": "미국정규장", "tradeable": True}


class USIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pos_file = os.path.join(self.tmp, "us_positions.json")
        self.jnl_db   = os.path.join(self.tmp, "trading_journal.db")

        # ── 실 파일/DB 격리 ─────────────────────────────────
        self._orig_pos_file   = USM.US_POSITIONS_FILE
        self._orig_jnl_path   = FO._JOURNAL_DB_PATH
        self._orig_journal_en = USM._US_JOURNAL_ENABLED
        self._orig_OLM        = USM.OrderLifecycleManager
        self._orig_outbox     = USM._USFillOutbox
        self._orig_ledger     = USM._USAppEffectLedger

        USM.US_POSITIONS_FILE = self.pos_file
        FO._JOURNAL_DB_PATH   = self.jnl_db
        FO._local             = threading.local()   # thread-local conn 재생성 유도
        USM._US_JOURNAL_ENABLED = False              # 저널 훅 비활성(격리)
        # lifecycle/outbox/ledger 를 임시 DB 로 강제(구성자 인자 무시)
        _db = self.jnl_db
        USM.OrderLifecycleManager = lambda _p, _d=_db: self._orig_OLM(_d)
        USM._USFillOutbox         = lambda _p, _d=_db: self._orig_outbox(_d)
        USM._USAppEffectLedger    = lambda _p, _d=_db: self._orig_ledger(_d)

        self.api = FakeUSApi()
        self.mgr = USM.USStrategyManager(self.api)

    def tearDown(self):
        USM.US_POSITIONS_FILE   = self._orig_pos_file
        FO._JOURNAL_DB_PATH     = self._orig_jnl_path
        FO._local               = threading.local()
        USM._US_JOURNAL_ENABLED = self._orig_journal_en
        USM.OrderLifecycleManager = self._orig_OLM
        USM._USFillOutbox         = self._orig_outbox
        USM._USAppEffectLedger    = self._orig_ledger
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 헬퍼 ────────────────────────────────────────────────
    def _new_mgr(self):
        """재시작 시뮬: 동일 파일/DB 로 새 매니저 인스턴스."""
        return USM.USStrategyManager(self.api)

    def _manage(self, sym, cur, sell_score=0, iv=None):
        pos = self.mgr.pos_mgr.positions[sym]
        return self.mgr._manage_position(pos, sym, pos.name, pos.excd, cur,
                                         iv or _iv(sell_score=sell_score), SESS)

    # ══════════════════════════════════════════════════════════
    # §9 사고 재현 + A: app-start 7종목 복원
    # ══════════════════════════════════════════════════════════
    def test_A_and_incident_7_vs_9(self):
        KIS_7 = ["ACHR", "BLNK", "IONQ", "NNE", "NVTS", "QUBT", "RGTI"]
        INTERNAL_9 = ["ASTS", "BBAI", "BLZE", "CCJ", "CEG",
                      "CRSP", "IOVA", "LEU", "RKLB"]
        # 내부 원장에 9개(사고 당시 유령) 존재
        for s in INTERNAL_9:
            self.mgr.pos_mgr.add(USM.USPosition(s, s, "NASD", 5, 50.0))
        # KIS 잔고 = 실제 7개(완전 스냅샷)
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding(s, 10, 100.0, 101.0) for s in KIS_7]}
        # 완전+삭제허용 → 7 복원, 9 stale 정리(사고 재현: 교집합 0)
        h = self.mgr.us_reconcile_positions(allow_stale_delete=True)
        self.assertTrue(h["authoritative"])
        self.assertEqual(h["restored"], 7)
        self.assertEqual(h["stale_removed"], 9)
        got = sorted(self.mgr.pos_mgr.positions.keys())
        self.assertEqual(got, sorted(KIS_7))
        # 복원 종목은 recovered=True, 복원 직후 HOLD(NORMAL), highest>=avg
        for s in KIS_7:
            p = self.mgr.pos_mgr.positions[s]
            self.assertTrue(p.recovered)
            self.assertEqual(p.management_mode, R.MODE_NORMAL)
            self.assertGreaterEqual(p.highest_price, p.avg_price)
        # 원자 저장 파일에 실제로 기록되었는지(영속 검증)
        self.assertTrue(os.path.exists(self.pos_file))
        with open(self.pos_file, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(sorted(disk.keys()), sorted(KIS_7))
        self.assertTrue(disk["IONQ"]["mgmt"]["recovered"])

    # ══════════════════════════════════════════════════════════
    # B: 복원 직후 SELL 0 / C: 다음 루프 전 종목 evaluate
    # ══════════════════════════════════════════════════════════
    def test_B_no_sell_right_after_restore_and_C_all_evaluate(self):
        KIS_7 = ["ACHR", "BLNK", "IONQ", "NNE", "NVTS", "QUBT", "RGTI"]
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding(s, 10, 100.0, 100.0) for s in KIS_7]}
        self.mgr.us_reconcile_positions(allow_stale_delete=True)
        # 복원 직후 동일가(net≈-0.25%) 관리 판정 → 전 종목 HOLD, 매도 0
        results = []
        for s in KIS_7:
            results.append(self._manage(s, 100.0))
        self.assertEqual(self.api.sell_calls, [])                 # SELL 0
        self.assertEqual(len(results), 7)                          # 전 종목 evaluate
        for r in results:
            self.assertIn(r["action"], ("HOLD",))
            self.assertIn("관리", r.get("reason", ""))            # 관리모드가 판정

    # ══════════════════════════════════════════════════════════
    # D: -5% → RECOVERY 저장 / E: 재시작 후 RECOVERY 유지
    # ══════════════════════════════════════════════════════════
    def test_D_enter_recovery_and_E_persist_across_restart(self):
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding("IONQ", 10, 100.0, 100.0)]}
        self.mgr.us_reconcile_positions(allow_stale_delete=True)
        # net = (95-100)/100*100 - 0.25 = -5.25 ≤ -5 → RECOVERY 진입(HOLD, 매도 없음)
        r = self._manage("IONQ", 95.0)
        self.assertEqual(r["action"], "HOLD")
        self.assertEqual(self.mgr.pos_mgr.positions["IONQ"].management_mode, R.MODE_RECOVERY)
        self.assertEqual(self.api.sell_calls, [])
        # 재시작: 새 매니저가 동일 파일에서 RECOVERY 를 복원
        mgr2 = self._new_mgr()
        p2 = mgr2.pos_mgr.positions["IONQ"]
        self.assertEqual(p2.management_mode, R.MODE_RECOVERY)
        self.assertIsNotNone(p2.mgmt["recovery_started_at"])
        self.assertIsNotNone(p2.mgmt["recovery_high_price"])

    # ══════════════════════════════════════════════════════════
    # F: 반등 후 회복고점 대비 -0.70% → 실매도 정확히 1회
    # ══════════════════════════════════════════════════════════
    def test_F_recovery_high_drop_sells_once(self):
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding("IONQ", 10, 100.0, 100.0)]}
        self.mgr.us_reconcile_positions(allow_stale_delete=True)
        self._manage("IONQ", 95.0)   # 진입(recovery_high=95)
        self._manage("IONQ", 97.0)   # 반등 → recovery_high=97 (net-3.25, exit -2 미달로 유지)
        self.assertEqual(self.mgr.pos_mgr.positions["IONQ"].mgmt["recovery_high_price"], 97.0)
        r = self._manage("IONQ", 96.0)   # (96-97)/97=-1.03% ≤ -0.7 → SELL_ALL
        self.assertEqual(len(self.api.sell_calls), 1)
        self.assertEqual(self.api.sell_calls[0]["symbol"], "IONQ")
        self.assertEqual(self.mgr.pos_mgr.positions["IONQ"].management_mode, R.MODE_EXIT)
        self.assertEqual(r["action"], "SELL_ACCEPTED")

    # ══════════════════════════════════════════════════════════
    # G: -6% 하드손절 → 실매도 1회(진입 다음 루프)
    # ══════════════════════════════════════════════════════════
    def test_G_hard_stop_sells_once(self):
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding("NNE", 10, 100.0, 100.0)]}
        self.mgr.us_reconcile_positions(allow_stale_delete=True)
        # net(94)= -6.25. 1st: RECOVERY 진입(즉시매도 금지) → 매도 0
        self._manage("NNE", 94.0)
        self.assertEqual(self.api.sell_calls, [])
        # 2nd: RECOVERY 에서 하드손절(net ≤ -6) → SELL 1회
        self._manage("NNE", 94.0)
        self.assertEqual(len(self.api.sell_calls), 1)

    # ══════════════════════════════════════════════════════════
    # H: 15분 경과 + net ≤ -4 → 실매도 1회
    # ══════════════════════════════════════════════════════════
    def test_H_time_stop_sells_once(self):
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding("QUBT", 10, 100.0, 100.0)]}
        self.mgr.us_reconcile_positions(allow_stale_delete=True)
        self._manage("QUBT", 95.0)   # 진입(net-5.25) recovery_high=95
        # recovery_started_at 을 16분 전으로 되돌림(시간청산 경계 초과)
        p = self.mgr.pos_mgr.positions["QUBT"]
        p.mgmt["recovery_started_at"] = (datetime.now() - timedelta(minutes=16)).isoformat()
        # net(95)=-5.25 ≤ -4, 경과≥15분 → 시간청산 SELL 1회 (고점 95 동일 → high_drop 0)
        self._manage("QUBT", 95.0)
        self.assertEqual(len(self.api.sell_calls), 1)

    # ══════════════════════════════════════════════════════════
    # I: 타임아웃/불명확 후 중복 SELL 없음(EXIT_PENDING 유지)
    # ══════════════════════════════════════════════════════════
    def test_I_no_duplicate_sell_after_timeout(self):
        self.api.sell_rt_cd = "9"    # UNKNOWN_CONFIRM(타임아웃) — 접수 가능/불명확
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding("RGTI", 10, 100.0, 100.0)]}
        self.mgr.us_reconcile_positions(allow_stale_delete=True)
        self._manage("RGTI", 94.0)   # 진입
        self._manage("RGTI", 94.0)   # 하드손절 → SELL 시도(UNKNOWN_CONFIRM)
        self.assertEqual(len(self.api.sell_calls), 1)
        self.assertEqual(self.mgr.pos_mgr.positions["RGTI"].management_mode, R.MODE_EXIT)
        # 다음 루프들: EXIT_PENDING → 재제출 금지(중복 SELL 없음)
        self._manage("RGTI", 94.0)
        self._manage("RGTI", 93.0)
        self.assertEqual(len(self.api.sell_calls), 1)   # 여전히 1회

    # ══════════════════════════════════════════════════════════
    # J: 포지션은 '실제 체결' 시에만 제거(접수만으로는 유지)
    # ══════════════════════════════════════════════════════════
    def test_J_position_removed_only_after_fill(self):
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding("ACHR", 10, 100.0, 100.0)]}
        self.mgr.us_reconcile_positions(allow_stale_delete=True)
        self._manage("ACHR", 94.0)   # 진입
        self._manage("ACHR", 94.0)   # SELL 접수(ACCEPTED) — 체결 아님
        self.assertIn("ACHR", self.mgr.pos_mgr.positions)   # 접수만으로 제거 안 함
        self.assertEqual(self.mgr.pos_mgr.positions["ACHR"].management_mode, R.MODE_EXIT)
        # 실제 체결(전량) 반영 = 매니저의 실제 포지션 감소 경로
        self.mgr._us_pos_reduce("ACHR", 10)
        self.assertNotIn("ACHR", self.mgr.pos_mgr.positions)  # 체결 후에만 제거

    # ══════════════════════════════════════════════════════════
    # K: 불완전 스냅샷 → stale 0 삭제(복원만) / L: 완전 스냅샷만 정리
    # ══════════════════════════════════════════════════════════
    def test_K_incomplete_snapshot_no_stale_delete(self):
        # 내부 3종목, KIS 는 그중 1종목만 + 신규 1종목(불완전 스냅샷)
        for s in ["AAA", "BBB", "CCC"]:
            self.mgr.pos_mgr.add(USM.USPosition(s, s, "NASD", 5, 50.0))
        self.api.balance_full = {"ok": True, "source": "api", "complete": False,
                                 "holdings": [_holding("AAA", 10, 100.0, 101.0),
                                              _holding("DDD", 10, 100.0, 101.0)]}
        h = self.mgr.us_reconcile_positions(allow_stale_delete=True)
        self.assertTrue(h["authoritative"])
        self.assertEqual(h["stale_removed"], 0)               # 불완전 → 삭제 금지
        # BBB/CCC(내부 유령 후보) 보존, DDD 복원, AAA 정합
        for s in ["AAA", "BBB", "CCC", "DDD"]:
            self.assertIn(s, self.mgr.pos_mgr.positions)
        self.assertTrue(self.mgr.pos_mgr.positions["DDD"].recovered)

    def test_L_complete_snapshot_stale_cleanup(self):
        for s in ["AAA", "BBB"]:
            self.mgr.pos_mgr.add(USM.USPosition(s, s, "NASD", 5, 50.0))
        # 완전 스냅샷: KIS 는 AAA 만 보유 → BBB stale
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding("AAA", 10, 100.0, 101.0)]}
        h = self.mgr.us_reconcile_positions(allow_stale_delete=True)
        self.assertEqual(h["stale_removed"], 1)
        self.assertNotIn("BBB", self.mgr.pos_mgr.positions)
        self.assertIn("AAA", self.mgr.pos_mgr.positions)

    def test_L2_complete_but_delete_disabled_default_preserves(self):
        # 완전 스냅샷이라도 기본(allow_stale_delete=False)이면 삭제 보류(안전 기본값)
        for s in ["AAA", "BBB"]:
            self.mgr.pos_mgr.add(USM.USPosition(s, s, "NASD", 5, 50.0))
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding("AAA", 10, 100.0, 101.0)]}
        h = self.mgr.us_reconcile_positions()   # 기본 False
        self.assertEqual(h["stale_removed"], 0)
        self.assertIn("BBB", self.mgr.pos_mgr.positions)    # 보존

    # ══════════════════════════════════════════════════════════
    # M: 비권위(불일치) → 신규 BUY 스킵 · 기존 SELL 정상
    # ══════════════════════════════════════════════════════════
    def test_M_unauthoritative_skips_buy_but_sells_normally(self):
        self.mgr.pos_mgr.add(USM.USPosition("IONQ", "IONQ", "NASD", 10, 100.0))
        # 조회 실패(ok=False) → 비권위: 삭제/복원 없음, BUY 게이트만 내림
        self.api.balance_full = {"ok": False, "source": None,
                                 "complete": False, "holdings": []}
        h = self.mgr.us_reconcile_positions()
        self.assertFalse(h["authoritative"])
        self.assertFalse(self.mgr._us_buy_gate_ok)
        self.assertIn("IONQ", self.mgr.pos_mgr.positions)    # 삭제 안 함
        # 신규 진입 시도 → 게이트로 HOLD(신규 BUY 스킵)
        entry = self.mgr._check_entry("TSLA", "TSLA", "NASD", 250.0,
                                      _iv(), [], {}, SESS)
        self.assertEqual(entry["action"], "HOLD")
        self.assertIn("비권위", entry["reason"])
        # 기존 보유 SELL 은 게이트와 무관하게 정상(하드손절 2루프)
        self._manage("IONQ", 94.0)
        self._manage("IONQ", 94.0)
        self.assertEqual(len(self.api.sell_calls), 1)

    # ══════════════════════════════════════════════════════════
    # N: KR 루프 + US 정합화 동시 실행 — 데드락/예외 없음(비재진입 락)
    # ══════════════════════════════════════════════════════════
    def test_N_concurrent_reconcile_no_deadlock(self):
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding("IONQ", 10, 100.0, 101.0),
                                              _holding("NNE", 5, 50.0, 51.0)]}
        errors = []
        def worker():
            try:
                for _ in range(30):
                    self.mgr.us_reconcile_positions(allow_stale_delete=True)
            except Exception as e:      # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads))   # 무데드락
        self.assertEqual(errors, [])
        # 최종 원장 정합: IONQ/NNE 존재
        self.assertIn("IONQ", self.mgr.pos_mgr.positions)
        self.assertIn("NNE", self.mgr.pos_mgr.positions)

    # ══════════════════════════════════════════════════════════
    # 추가: 신규(비복원) 포지션은 기존 익절/손절 정책 그대로(면제 아님)
    # ══════════════════════════════════════════════════════════
    def test_new_position_keeps_existing_takeprofit(self):
        # 복원이 아닌 신규 매수 포지션(recovered=False)
        self.mgr.pos_mgr.add(USM.USPosition("IONQ", "IONQ", "NASD", 10, 100.0))
        # net(103)= +2.75 ≥ +2.5 → 기존 ① 무조건 전량익절 발동(관리모드는 DEFER)
        r = self._manage("IONQ", 103.0)
        self.assertEqual(len(self.api.sell_calls), 1)
        self.assertEqual(r["action"], "SELL_ACCEPTED")

    def test_recovered_position_exempt_from_fixed_takeprofit(self):
        # 복원 포지션은 고정 익절(①②③) 면제 — 수익 트레일링만
        self.api.balance_full = {"ok": True, "source": "api", "complete": True,
                                 "holdings": [_holding("IONQ", 10, 100.0, 100.0)]}
        self.mgr.us_reconcile_positions(allow_stale_delete=True)
        # net(103)= +2.75 이지만 recovered=True → 고정익절 면제, 트레일 미발동 → HOLD
        r = self._manage("IONQ", 103.0)
        self.assertEqual(self.api.sell_calls, [])
        self.assertEqual(r["action"], "HOLD")


if __name__ == "__main__":
    unittest.main()
