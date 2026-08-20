"""US 실보유 정합화 + 복원 보호 + 손실회복 트레일링 + 격리 — 통합 테스트 (§9/§10/§11).

★ 순수 모듈 단위테스트와 달리 **실 스택**을 연결한다:
    - 실 USStrategyManager (run()이 호출하는 _manage_position 매도 판정 경로)
    - 실 USPositionManager + 실 AtomicPositionStore(us_positions.json 원자 저장)
    - 실 us_reconcile.reconcile_decision (스케줄러 잡이 호출하는 us_reconcile_positions)
    - 실 PendingOrderRegistry + OrderLifecycle (crash-safe submit-intent → SELL 파이프라인)
  KIS API 만 Fake 로 대체. 저널·lifecycle·registry·포지션 파일은 임시 디렉터리로 격리.
"""
import os
import sys
import json
import types
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import strategies.us_strategy_manager as USM
import strategies.us_recovery as R
import journal.fill_observer as FO
from phoenix.lifecycle import OrderLifecycleManager


class FakeUSApi:
    def __init__(self):
        self.sell_calls  = []
        self.sell_rt_cd  = "0"       # "0"=접수(ODNO) / "9"=타임아웃(UNKNOWN) / "reject"
        self.sell_odno   = "ODNO-TEST-1"
        self.sell_reject_msg = "거래불가"   # '수량' 미포함 → 유령제거 아님(쿨다운 경로)
        self.balance_full = {"ok": True, "source": "api", "complete": True,
                             "authoritative_empty": False, "holdings": []}
        self.intraday_bars = []      # [{"ts": datetime, "close": float}, ...]

    def get_us_intraday_bars(self, symbol, excd="NASD", lookback_min=90):
        return list(self.intraday_bars)

    def get_us_balance_full(self, max_pages: int = 20):
        return json.loads(json.dumps(self.balance_full))

    def get_us_balance(self):
        return {"holdings": self.balance_full.get("holdings", [])}

    def get_usd_exchange_rate(self):
        return 1300.0

    def sell_us(self, symbol, qty, price, excd):
        self.sell_calls.append({"symbol": symbol, "qty": qty})
        if self.sell_rt_cd == "0":
            return {"rt_cd": "0", "msg1": "매도접수성공", "output": {"ODNO": self.sell_odno}}
        if self.sell_rt_cd == "9":
            return {"rt_cd": "9", "msg1": "timeout(예외 래핑)"}
        return {"rt_cd": "1", "msg1": self.sell_reject_msg}


def _holding(sym, qty=10, avg=100.0, cur=100.0, name=None, excd="NASD"):
    return {"symbol": sym, "qty": qty, "avg_price": avg, "cur_price": cur,
            "name": name or sym, "excd": excd}


def _snap(holdings, complete=True, ok=True):
    return {"ok": ok, "source": ("api" if ok else None), "complete": complete,
            "authoritative_empty": bool(ok and complete and not holdings),
            "holdings": holdings}


def _iv(sell_score=0, ema9=0.0, ema9_rising=False, atr_pct=0.0):
    return {"sell_score": sell_score, "intraday_pct": 0.0, "rsi": 50.0,
            "vol_ratio": 2.0, "above_vwap": True, "macd_above": True,
            "ema9": ema9, "ema9_rising": ema9_rising, "atr_pct": atr_pct,
            "buy_score": 0, "vwap": 0.0}


SESS = {"session": "미국정규장", "tradeable": True}
KIS_7 = ["ACHR", "BLNK", "IONQ", "NNE", "NVTS", "QUBT", "RGTI"]
INTERNAL_9 = ["ASTS", "BBAI", "BLZE", "CCJ", "CEG", "CRSP", "IOVA", "LEU", "RKLB"]


class USIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pos_file = os.path.join(self.tmp, "us_positions.json")
        self.jnl_db   = os.path.join(self.tmp, "trading_journal.db")

        self._orig_pos_file   = USM.US_POSITIONS_FILE
        self._orig_jnl_path   = FO._JOURNAL_DB_PATH
        self._orig_journal_en = USM._US_JOURNAL_ENABLED
        self._orig_OLM        = USM.OrderLifecycleManager
        self._orig_outbox     = USM._USFillOutbox
        self._orig_ledger     = USM._USAppEffectLedger

        USM.US_POSITIONS_FILE = self.pos_file
        FO._JOURNAL_DB_PATH   = self.jnl_db
        FO._local             = threading.local()
        USM._US_JOURNAL_ENABLED = False
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

    def _new_mgr(self):
        return USM.USStrategyManager(self.api)

    def _manage(self, sym, cur, sell_score=0, ema9=0.0, ema9_rising=False, atr_pct=0.0):
        pos = self.mgr.pos_mgr.positions[sym]
        return self.mgr._manage_position(
            pos, sym, pos.name, pos.excd, cur,
            _iv(sell_score, ema9=ema9, ema9_rising=ema9_rising, atr_pct=atr_pct), SESS)

    def _set_bars(self, closes, minute0=0):
        """과거(항상 '마감 완료') 1분봉 주입. closes=[c1,c2,...] → 분 단위 증가."""
        base = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)
        self.api.intraday_bars = [
            {"ts": base + timedelta(minutes=minute0 + i), "close": float(c)}
            for i, c in enumerate(closes)
        ]

    def _set_5m_bars(self, bucket_closes):
        """서로 다른 '마감 완료' 5분봉을 주입(각 원소 = 한 5분버킷 종가).
        completed_bar_context 는 마지막 완료 5분봉을 반환 → 호출 간 연속 확인 가능."""
        base = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)
        self.api.intraday_bars = [
            {"ts": base + timedelta(minutes=5 * i), "close": float(c)}
            for i, c in enumerate(bucket_closes)
        ]

    def _force_loss_sell(self, sym):
        """손실 매도 2조건(구조붕괴 연속 2봉 AND 종목위험)을 충족시켜 1회 매도 유도.
        계좌위험(DailyPnLGuard)은 개별 종목 매도 게이트가 아니므로 설정하지 않는다.
        진입가 95 기준 recovery_high=95, ATR=1.0(유효, 구조거리 3%)."""
        self.mgr.US_MAX_LOSS_PER_SYMBOL_USD = 10.0     # 종목위험 쉽게(손실>$10)
        self._manage(sym, 95.0, atr_pct=1.0)           # RECOVERY_WAIT 진입 high=95
        self._set_5m_bars([92.0]); self._manage(sym, 92.0, atr_pct=1.0)   # 구조 count1
        self._set_5m_bars([92.0, 91.8])                # 연속 구조 count2
        return self._manage(sym, 91.8, atr_pct=1.0)    # 2조건 충족 → SELL

    def _seed_internal(self, syms, qty=5, avg=50.0):
        for s in syms:
            self.mgr.pos_mgr.add(USM.USPosition(s, s, "NASD", qty, avg))

    # ══════════════════════════════════════════════════════════
    # §9 사고 재현 + 격리: KIS 7 + 내부 stale 9 (stale-delete=false)
    #   → 정상관리 7, quarantine 9, 매도평가 대상 정확히 7
    # ══════════════════════════════════════════════════════════
    def test_incident_7_vs_9_quarantine_not_delete(self):
        self._seed_internal(INTERNAL_9)
        self.api.balance_full = _snap([_holding(s, 10, 100.0, 101.0) for s in KIS_7])
        h = self.mgr.us_reconcile_positions()   # 기본: allow_stale_delete=False
        self.assertTrue(h["authoritative"])
        self.assertEqual(h["restored"], 7)
        self.assertEqual(h["quarantined"], 9)
        self.assertEqual(h["stale_deleted"], 0)          # 삭제 0
        # 실보유(active) = 정확히 7, 격리 = 9
        active = self.mgr.pos_mgr.active_positions()
        quar   = self.mgr.pos_mgr.quarantined_positions()
        self.assertEqual(sorted(active), sorted(KIS_7))
        self.assertEqual(sorted(quar), sorted(INTERNAL_9))
        # 7 전부 recovered=True, 복원 직후 NORMAL(HOLD)
        for s in KIS_7:
            self.assertTrue(active[s].recovered)
            self.assertEqual(active[s].management_mode, R.MODE_NORMAL)
        # 파일에는 16개 모두 유지(삭제 0), 9개는 quarantined 표식 + 감사정보
        with open(self.pos_file, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(len(disk), 16)
        for s in INTERNAL_9:
            self.assertTrue(disk[s]["mgmt"]["quarantined"])
            q = disk[s]["mgmt"]["quarantine"]
            self.assertEqual(set(q.keys()), {"symbol", "quarantined_at", "reason", "snapshot_id"})

    def test_quarantine_zero_sell_submissions(self):
        # 격리 9종목에서 실제 SELL 제출 0회 (매도판정 대상 아님)
        self._seed_internal(INTERNAL_9)
        self.api.balance_full = _snap([_holding(s) for s in KIS_7])
        self.mgr.us_reconcile_positions()
        # 격리 종목은 active 에 없어 run() 매도판정 경로로 못 들어간다
        for s in INTERNAL_9:
            self.assertIsNone(self.mgr.pos_mgr.active_positions().get(s))
        # 방어적으로 격리 pos 를 직접 관리 호출해도 SELL 0 (HOLD)
        for s in INTERNAL_9:
            p = self.mgr.pos_mgr.positions[s]
            r = self.mgr._us_apply_management(p, s, s, "NASD", 10.0,
                                              p.net_pct(10.0), 0.0, 0.0, SESS)
            self.assertEqual(r["action"], "HOLD")
        self.assertEqual(self.api.sell_calls, [])

    def test_quarantine_excluded_from_count_budget_dedup(self):
        self._seed_internal(INTERNAL_9)
        self.api.balance_full = _snap([_holding(s) for s in KIS_7])
        self.mgr.us_reconcile_positions()
        # 보유수(외부 노출 .positions) = 격리 제외 → 7
        self.assertEqual(len(self.mgr.positions), 7)
        self.assertEqual(len(self.mgr.pos_mgr.active_positions()), 7)
        # 중복매수 판정: 격리 심볼은 '실보유' 아님 → active 조회 None(=매수 가능 취급)
        for s in INTERNAL_9:
            self.assertNotIn(s, self.mgr.positions)
        # 격리 감사정보는 별도 뷰로만 노출
        self.assertEqual(len(self.mgr.us_quarantine_audit()), 9)

    def test_quarantine_reappears_restored(self):
        # 다음 KIS 스냅샷에 격리 종목 재등장 → 즉시 정상 복구(격리 해제)
        self._seed_internal(["GONE"])
        self.api.balance_full = _snap([_holding("IONQ")])   # GONE 부재 → 격리
        self.mgr.us_reconcile_positions()
        self.assertTrue(self.mgr.pos_mgr.positions["GONE"].is_quarantined)
        # 다음 스냅샷에 GONE 재등장
        self.api.balance_full = _snap([_holding("IONQ"), _holding("GONE", 4, 20.0, 21.0)])
        h = self.mgr.us_reconcile_positions()
        self.assertEqual(h["unquarantined"], 1)
        self.assertFalse(self.mgr.pos_mgr.positions["GONE"].is_quarantined)
        self.assertIn("GONE", self.mgr.pos_mgr.active_positions())
        self.assertEqual(self.mgr.pos_mgr.positions["GONE"].qty, 4)

    def test_no_file_deletion_before_operator_approval(self):
        self._seed_internal(INTERNAL_9)
        self.api.balance_full = _snap([_holding(s) for s in KIS_7])
        # 여러 번 정합화해도(운영자 미승인) 삭제 0
        for _ in range(3):
            self.mgr.us_reconcile_positions()
        with open(self.pos_file, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(len(disk), 16)   # 삭제 0회

    def test_operator_approval_final_delete(self):
        # 운영자 승인(allow_stale_delete=True) + 완전 스냅샷 → 최종 삭제
        self._seed_internal(["AAA", "BBB"])
        self.api.balance_full = _snap([_holding("AAA", 10, 100.0, 101.0)])
        h = self.mgr.us_reconcile_positions(allow_stale_delete=True)
        self.assertEqual(h["stale_deleted"], 1)
        self.assertNotIn("BBB", self.mgr.pos_mgr.positions)

    def test_incomplete_snapshot_no_new_quarantine(self):
        # 불완전 스냅샷 → 신규 격리 0, 삭제 0, positive holding 복원만(§1)
        self._seed_internal(["AAA", "BBB", "CCC"])
        self.api.balance_full = _snap([_holding("AAA", 10, 100.0, 101.0),
                                       _holding("DDD", 10, 100.0, 101.0)],
                                      complete=False)
        h = self.mgr.us_reconcile_positions(allow_stale_delete=True)
        self.assertTrue(h["authoritative"])
        self.assertEqual(h["quarantined"], 0)     # 격리 신규 생성 0
        self.assertEqual(h["stale_deleted"], 0)
        self.assertFalse(h["buy_allowed"])        # 불완전 → 이번 스캔 BUY 스킵
        # AAA 정합, DDD 복원, BBB/CCC 보존(격리 아님)
        for s in ["AAA", "BBB", "CCC", "DDD"]:
            self.assertIn(s, self.mgr.pos_mgr.positions)
            self.assertFalse(self.mgr.pos_mgr.positions[s].is_quarantined)

    def test_authoritative_empty_quarantines_all(self):
        # 정상 complete empty(보유 0) → 내부 active 전부 격리
        self._seed_internal(["AAA", "BBB"])
        self.api.balance_full = _snap([], complete=True)   # authoritative_empty
        h = self.mgr.us_reconcile_positions()
        self.assertTrue(h["authoritative"])
        self.assertTrue(h["authoritative_empty"])
        self.assertEqual(h["quarantined"], 2)
        self.assertEqual(len(self.mgr.pos_mgr.active_positions()), 0)

    def test_error_empty_no_quarantine(self):
        # 오류 empty(ok=False) → 비권위: 격리/삭제 없음, BUY 스킵
        self._seed_internal(["AAA", "BBB"])
        self.api.balance_full = _snap([], ok=False)
        h = self.mgr.us_reconcile_positions()
        self.assertFalse(h["authoritative"])
        self.assertFalse(h["authoritative_empty"])
        self.assertEqual(h["quarantined"], 0)
        self.assertEqual(len(self.mgr.pos_mgr.active_positions()), 2)  # 보존
        self.assertFalse(self.mgr._us_buy_gate_ok)

    def test_buy_gate_true_only_after_complete_restore_quarantine(self):
        self._seed_internal(INTERNAL_9)
        self.api.balance_full = _snap([_holding(s) for s in KIS_7])
        h = self.mgr.us_reconcile_positions()
        self.assertTrue(h["buy_allowed"])   # 완전+복원+격리완료 → BUY 허용
        self.assertTrue(self.mgr._us_buy_gate_ok)

    # ══════════════════════════════════════════════════════════
    # 복원 직후 SELL 0 / recovered 7종목만 run() 평가 진입
    # ══════════════════════════════════════════════════════════
    def test_restore_no_sell_and_only_recovered_evaluate(self):
        self._seed_internal(INTERNAL_9)
        self.api.balance_full = _snap([_holding(s, 10, 100.0, 100.0) for s in KIS_7])
        self.mgr.us_reconcile_positions()
        evaluated = []
        for s in list(self.mgr.pos_mgr.active_positions().keys()):
            r = self._manage(s, 100.0)
            evaluated.append(s)
            self.assertEqual(r["action"], "HOLD")
        self.assertEqual(sorted(evaluated), sorted(KIS_7))   # 정확히 7만 평가
        self.assertEqual(self.api.sell_calls, [])             # SELL 0

    # ══════════════════════════════════════════════════════════
    # recovered 종목 정책 (item3)
    # ══════════════════════════════════════════════════════════
    def test_recovered_exempt_but_recovery_and_persist_and_monotonic(self):
        self.api.balance_full = _snap([_holding("IONQ", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        p = self.mgr.pos_mgr.positions["IONQ"]
        self.assertTrue(p.recovered)
        # 고정익절 면제: net +2.75 이어도 SELL 안 함
        r = self._manage("IONQ", 103.0)
        self.assertEqual(r["action"], "HOLD")
        self.assertEqual(self.api.sell_calls, [])
        # net<=-5 회복모드 진입
        self._manage("IONQ", 95.0)
        self.assertEqual(self.mgr.pos_mgr.positions["IONQ"].management_mode, R.MODE_RECOVERY)
        # 재시작 후 recovered + RECOVERY 유지
        mgr2 = self._new_mgr()
        p2 = mgr2.pos_mgr.positions["IONQ"]
        self.assertTrue(p2.recovered)
        self.assertEqual(p2.management_mode, R.MODE_RECOVERY)
        # 반복 정합화로 최고가 하락 금지(monotonic)
        mgr2.api.balance_full = _snap([_holding("IONQ", 10, 100.0, 130.0)])
        mgr2.us_reconcile_positions()
        hi = mgr2.pos_mgr.positions["IONQ"].highest_price
        mgr2.api.balance_full = _snap([_holding("IONQ", 10, 100.0, 90.0)])
        mgr2.us_reconcile_positions()
        self.assertGreaterEqual(mgr2.pos_mgr.positions["IONQ"].highest_price, hi)

    # ══════════════════════════════════════════════════════════
    # 손실 관리(RECOVERY_WAIT): 고정손절 제거 / 구조붕괴·계좌위험만 매도
    # ══════════════════════════════════════════════════════════
    def test_minus6_is_warning_not_sell(self):
        # -5 진입 → -6 이어도 매도 안 함(경고만). 고정 -6 손절 제거.
        self.api.balance_full = _snap([_holding("NNE", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self._manage("NNE", 95.0)   # 진입 RECOVERY_WAIT
        self._manage("NNE", 94.0)   # -6.25% → 경고, 매도 없음
        self._manage("NNE", 93.5)   # 더 하락해도 매도 없음
        self.assertEqual(self.api.sell_calls, [])
        p = self.mgr.pos_mgr.positions["NNE"]
        self.assertEqual(p.management_mode, R.MODE_RECOVERY_WAIT)
        self.assertTrue(p.mgmt["recovery_warn"])

    def test_recovery_simple_drop_holds(self):
        # recovery high 대비 '단순 하락'만으로는 매도하지 않는다(0% 회복 기회)
        self.api.balance_full = _snap([_holding("QUBT", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self._manage("QUBT", 95.0)                 # 진입 high=95
        self._set_5m_bars([94.0])                  # 5분봉 -1.05%(<3% 구조거리) → 미이탈
        r = self._manage("QUBT", 94.0)
        self.assertEqual(self.api.sell_calls, [])
        self.assertEqual(r["action"], "HOLD")

    def test_two_conditions_sell_once(self):
        # 구조 붕괴 연속 2봉 AND 종목위험 → SELL 1회 (계좌한도 미초과여도 매도)
        self.api.balance_full = _snap([_holding("NNE", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self.assertNotEqual(self.mgr.pnl_guard.state, "LOSS_LIMIT")   # 계좌한도 미초과
        self._manage("NNE", 95.0, atr_pct=1.0)     # 진입 high=95 (종목손실 -50 > -$60? no)
        self.mgr.US_MAX_LOSS_PER_SYMBOL_USD = 20.0 # 종목위험: 손실>$20 (91.x → -80)
        self._set_5m_bars([91.5]); self._manage("NNE", 91.5, atr_pct=1.0)   # count1
        self.assertEqual(self.api.sell_calls, [])  # 1봉만 → HOLD
        self._set_5m_bars([91.5, 91.3])            # 연속 2봉 → count2 → SELL
        r = self._manage("NNE", 91.3, atr_pct=1.0)
        self.assertEqual(len(self.api.sell_calls), 1)   # 계좌한도 무관하게 매도
        self.assertEqual(self.mgr.pos_mgr.positions["NNE"].management_mode, R.MODE_EXIT)

    def test_struct_only_holds(self):
        # 구조 2봉 충족이나 종목손실 한도 미초과 → HOLD (임의 매도 안 함)
        self.api.balance_full = _snap([_holding("BLZE", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self.mgr.US_MAX_LOSS_PER_SYMBOL_USD = 500.0    # 종목위험 매우 큼(미초과)
        self._manage("BLZE", 95.0, atr_pct=1.0)
        self._set_5m_bars([91.5]); self._manage("BLZE", 91.5, atr_pct=1.0)
        self._set_5m_bars([91.5, 91.3]); self._manage("BLZE", 91.3, atr_pct=1.0)
        self.assertEqual(self.api.sell_calls, [])      # 종목한도 미초과 → HOLD
        self.assertEqual(self.mgr.pos_mgr.positions["BLZE"].mgmt["struct_breach_closes"], 2)

    def test_symbol_only_holds(self):
        # 종목한도 초과이나 구조 미확인 → HOLD
        self.api.balance_full = _snap([_holding("CCJ", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self.mgr.US_MAX_LOSS_PER_SYMBOL_USD = 5.0      # 종목위험 쉽게 초과
        self._manage("CCJ", 95.0, atr_pct=1.0)
        self._set_5m_bars([91.5]); self._manage("CCJ", 91.5, atr_pct=1.0)  # 구조 1봉
        self.assertEqual(self.api.sell_calls, [])

    def test_structural_non_consecutive_holds(self):
        # 종목위험이어도 구조가 연속 아니면 매도 없음(정상 5분봉 끼면 초기화)
        self.api.balance_full = _snap([_holding("RGTI", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self.mgr.US_MAX_LOSS_PER_SYMBOL_USD = 5.0
        self._manage("RGTI", 95.0, atr_pct=1.0)
        self._set_5m_bars([91.5]);        self._manage("RGTI", 91.5, atr_pct=1.0)  # count1
        self._set_5m_bars([91.5, 94.5]);  self._manage("RGTI", 94.5, atr_pct=1.0)  # 초기화
        self._set_5m_bars([91.5, 94.5, 91.5]); self._manage("RGTI", 91.5, atr_pct=1.0)
        self.assertEqual(self.api.sell_calls, [])

    def test_account_over_limit_blocks_buy_not_sell(self):
        # 계좌한도 초과지만 개별(구조·종목) 조건 미충족 → 임의 SELL 없음, 신규 BUY만 차단
        self.api.balance_full = _snap([_holding("NVTS", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self.api.intraday_bars = []
        self.mgr.pnl_guard.state = "LOSS_LIMIT"        # 계좌한도 초과
        self._manage("NVTS", 95.0, atr_pct=1.0)        # 진입
        self._manage("NVTS", 90.0, atr_pct=1.0)        # 구조봉 없음 → HOLD(임의 매도 없음)
        self.assertEqual(self.api.sell_calls, [])
        # 계좌위험 신호는 존재(신규 BUY 차단·포트폴리오 위험축소 용도) — 개별 매도 게이트 아님
        self.assertTrue(self.mgr._us_account_risk_exceeded())

    # ── 위험기반 사이징(최초 BUY·ADD 공통 최종 권위) ─────────────────
    def test_risk_sizing_blocks_on_atr_missing(self):
        qty, blk = self.mgr._us_risk_size_or_block(
            "X", "X", 100.0, 10, {"atr_pct": 0.0}, {"session": ""})
        self.assertEqual(qty, 0)
        self.assertEqual(blk["action"], "BUY_BLOCKED")   # ATR 결측 → fail-safe 차단

    def test_risk_sizing_blocks_zero_not_min1(self):
        self.mgr.US_MAX_LOSS_PER_SYMBOL_USD = 1.0        # 매우 작음
        qty, blk = self.mgr._us_risk_size_or_block(
            "X", "X", 1000.0, 10, {"atr_pct": 5.0}, {"session": ""})
        self.assertEqual(qty, 0)                          # 최소 1주 강제 안 함
        self.assertIsNotNone(blk)

    def test_risk_sizing_caps_authoritatively(self):
        self.mgr.US_MAX_LOSS_PER_SYMBOL_USD = 30.0
        qty, blk = self.mgr._us_risk_size_or_block(
            "X", "X", 100.0, 100, {"atr_pct": 1.0}, {"session": ""})   # 거리3%, per-share3
        self.assertIsNone(blk)
        self.assertEqual(qty, 10)                         # cap=30/3=10, 예산100 초과 안 함

    def test_both_buy_paths_use_risk_sizing(self):
        import inspect
        src_buy = inspect.getsource(type(self.mgr)._do_buy)
        src_add = inspect.getsource(type(self.mgr)._do_add_buy)
        self.assertIn("_us_risk_size_or_block", src_buy)   # 최초 BUY 경유
        self.assertIn("_us_risk_size_or_block", src_add)   # ADD_BUY 경유
        self.assertNotIn("max(1, int(INVEST_PER_TRADE_USD * ADD_BUY_RATIO", src_add)

    def test_fx_failure_blocks_buy_source(self):
        # 환율 실패 시 원화환산 추측 없이 BUY 차단 경로 존재
        import inspect
        src = inspect.getsource(type(self.mgr)._do_buy)
        self.assertIn("환율 조회 실패", src)
        self.assertIn("원화환산 불가로 매수 차단", src)

    def test_analytics_concurrent_writes_no_corruption_no_pii(self):
        pos = USM.USPosition("IONQ", "IONQ", "NASD", 10, 100.0)
        outcome = {"outcome": "recovered", "entry_net_pct": -5.0,
                   "max_drawdown_net_pct": -6.0, "warn6_at": None, "started_at": None,
                   "ended_at": None, "recovery_seconds": 10.0, "final_net_pct": 0.1,
                   "final_price": 100.1, "hypothetical_early_stop_net_pct": -5.0}

        def worker():
            for _ in range(20):
                self.mgr._us_record_recovery_analytics("IONQ", "IONQ", pos, dict(outcome))
        ts = [threading.Thread(target=worker) for _ in range(6)]
        for t in ts: t.start()
        for t in ts: t.join()
        path = os.path.join(self.tmp, "us_recovery_analytics.jsonl")
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        self.assertEqual(len(lines), 120)          # 6×20 — 유실/중복 없음
        for ln in lines:
            json.loads(ln)                         # 각 줄 유효 JSON(줄 깨짐 없음)
            low = ln.lower()
            for pii in ("token", "cano", "acnt", "account", "odno", "password", "secret"):
                self.assertNotIn(pii, low)         # 개인정보/계좌·토큰 노출 없음

    def test_recovery_recovers_to_normal_no_sell(self):
        # -5 진입 후 0% 회복 → NORMAL, 손실 매도 0회
        self.api.balance_full = _snap([_holding("CEG", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self._manage("CEG", 95.0)                  # 진입
        self._manage("CEG", 98.0)                  # 회복 중
        self._manage("CEG", 100.3)                 # 0%+ 회복 → NORMAL
        self.assertEqual(self.api.sell_calls, [])
        self.assertEqual(self.mgr.pos_mgr.positions["CEG"].management_mode, R.MODE_NORMAL)

    def test_recovery_analytics_recorded_on_recovered(self):
        # 회복 시 매매일지 분석 JSONL 에 레코드 기록(최대하락·회복시간·최종손익·가상손익)
        self.api.balance_full = _snap([_holding("CEG", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self._manage("CEG", 95.0)                  # 진입(-5)
        self._manage("CEG", 93.0)                  # 최대하락 갱신(-7)
        self._manage("CEG", 100.3)                 # 회복 → NORMAL, 레코드 기록
        path = os.path.join(self.tmp, "us_recovery_analytics.jsonl")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
        self.assertTrue(any(r["outcome"] == "recovered" and r["symbol"] == "CEG"
                            for r in rows))
        rec = [r for r in rows if r["symbol"] == "CEG"][-1]
        self.assertLessEqual(rec["max_drawdown_net_pct"], -7.0)     # 최대하락 기록
        self.assertIsNotNone(rec["hypothetical_early_stop_pnl_usd"])  # 조기손절 가상손익
        self.assertIsNotNone(rec["saved_vs_early_stop_usd"])
        # 소비 후 제거(재기록 방지)
        self.assertIsNone(self.mgr.pos_mgr.positions["CEG"].mgmt.get("recovery_last_outcome"))

    # ══════════════════════════════════════════════════════════
    # EXIT_PENDING (item4/item5) + 체결 (item6)
    # ══════════════════════════════════════════════════════════
    def test_exit_pending_kept_across_restart_no_resubmit(self):
        # SELL 접수 후 crash(재시작) → EXIT_PENDING 유지, 재제출 0 (트리거=3조건)
        self.api.balance_full = _snap([_holding("ACHR", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self._force_loss_sell("ACHR")               # 3조건 → SELL 접수 → EXIT_PENDING
        self.assertEqual(len(self.api.sell_calls), 1)
        n_before = len(self.api.sell_calls)
        mgr2 = self._new_mgr()       # 재시작
        self.assertEqual(mgr2.pos_mgr.positions["ACHR"].management_mode, R.MODE_EXIT)
        pos = mgr2.pos_mgr.positions["ACHR"]
        r = mgr2._us_apply_management(pos, "ACHR", "ACHR", "NASD", 91.8,
                                      pos.net_pct(91.8), 0.0, 0.0, SESS)
        self.assertEqual(r["action"], "HOLD")
        self.assertEqual(len(self.api.sell_calls), n_before)   # 재제출 0

    def test_exit_pending_reverts_when_begin_fails(self):
        # SELL_ALL → EXIT_PENDING 저장 후 begin 실패(KIS 미호출) → 직전 상태 원상복구
        self.api.balance_full = _snap([_holding("RGTI", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self.mgr.US_MAX_LOSS_PER_SYMBOL_USD = 10.0
        self.mgr.pnl_guard.state = "LOSS_LIMIT"
        self._manage("RGTI", 95.0)      # RECOVERY_WAIT 진입 high=95
        self._set_5m_bars([92.0]); self._manage("RGTI", 92.0)   # 구조 count1
        p = self.mgr.pos_mgr.positions["RGTI"]
        started = p.mgmt["recovery_started_at"]
        high    = p.mgmt["recovery_high_price"]
        self.mgr._us_begin_submit_intent = lambda *a, **k: False   # begin 실패 주입
        self._set_5m_bars([92.0, 91.8])
        self._manage("RGTI", 91.8)      # 3조건 SELL 판정 → begin 실패
        self.assertEqual(self.api.sell_calls, [])               # KIS 미호출
        p = self.mgr.pos_mgr.positions["RGTI"]
        self.assertEqual(p.management_mode, R.MODE_RECOVERY_WAIT)   # 원상복구
        self.assertEqual(p.mgmt["recovery_started_at"], started)
        self.assertEqual(p.mgmt["recovery_high_price"], high)
        self.assertIsNone(p.mgmt["exit_pending_ref"])

    def test_clear_reject_cooldown(self):
        # 명확 거절 → 쿨다운 동안 재제출 없음, 쿨다운 후 재시도 (트리거=3조건)
        self.api.sell_rt_cd = "reject"
        self.api.balance_full = _snap([_holding("BLNK", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self._force_loss_sell("BLNK")   # SELL 시도 → 명확거절 → 쿨다운
        self.assertEqual(len(self.api.sell_calls), 1)
        p = self.mgr.pos_mgr.positions["BLNK"]
        self.assertIsNotNone(p.mgmt["sell_cooldown_until"])
        self.assertEqual(p.management_mode, R.MODE_RECOVERY_WAIT)   # EXIT 고착 아님
        self._manage("BLNK", 91.8, atr_pct=1.0)  # 쿨다운 중 재판정(2조건 유지) → 재제출 없음
        self.assertEqual(len(self.api.sell_calls), 1)
        p.mgmt["sell_cooldown_until"] = (datetime.now() - timedelta(seconds=1)).isoformat()
        self._manage("BLNK", 91.8, atr_pct=1.0)  # 쿨다운 만료 → 재시도(2회차)
        self.assertEqual(len(self.api.sell_calls), 2)

    def test_no_duplicate_sell_after_timeout(self):
        self.api.sell_rt_cd = "9"       # UNKNOWN_CONFIRM
        self.api.balance_full = _snap([_holding("RGTI", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self._force_loss_sell("RGTI")   # SELL 시도(UNKNOWN) → EXIT_PENDING 유지
        self.assertEqual(len(self.api.sell_calls), 1)
        self.assertEqual(self.mgr.pos_mgr.positions["RGTI"].management_mode, R.MODE_EXIT)
        self._manage("RGTI", 91.0)
        self.assertEqual(len(self.api.sell_calls), 1)   # 재제출 없음

    def test_position_removed_only_after_fill_partial_then_full(self):
        self.api.balance_full = _snap([_holding("ACHR", 10, 100.0, 100.0)])
        self.mgr.us_reconcile_positions()
        self._force_loss_sell("ACHR")              # SELL 접수 — 체결 아님
        self.assertIn("ACHR", self.mgr.pos_mgr.positions)
        # 부분 체결(3주) → delta 만 감소, 제거 안 함
        self.mgr._us_pos_reduce("ACHR", 3)
        self.assertEqual(self.mgr.pos_mgr.positions["ACHR"].qty, 7)
        # 완전 체결(잔량 7) → 최종 제거
        self.mgr._us_pos_reduce("ACHR", 7)
        self.assertNotIn("ACHR", self.mgr.pos_mgr.positions)

    def test_reconcile_does_not_book_pnl(self):
        # 정합화(qty/avg 갱신)가 실현손익을 임의 부킹하지 않는다
        self.mgr.pos_mgr.add(USM.USPosition("IONQ", "IONQ", "NASD", 10, 100.0))
        realized_before = self.mgr.pnl_guard.realized_pnl
        self.api.balance_full = _snap([_holding("IONQ", 12, 95.0, 96.0)])  # 수량/평단 변경
        self.mgr.us_reconcile_positions()
        self.assertEqual(self.mgr.pos_mgr.positions["IONQ"].qty, 12)
        self.assertEqual(self.mgr.pnl_guard.realized_pnl, realized_before)  # 손익 무변경

    # ══════════════════════════════════════════════════════════
    # 신규(비복원) 포지션: 고정 +2.5/+2.0 익절 비활성 → 동적 트레일이 지배
    # ══════════════════════════════════════════════════════════
    def test_fixed_takeprofit_disabled_dynamic_trail_governs(self):
        self.mgr.pos_mgr.add(USM.USPosition("IONQ", "IONQ", "NASD", 10, 100.0))
        # net +2.75(고점=103) → 예전 ① 무조건익절이면 즉시 SELL 이지만, 이제 비활성.
        r = self._manage("IONQ", 103.0)
        self.assertEqual(self.api.sell_calls, [])
        self.assertEqual(r["action"], "HOLD")
        self.assertTrue(self.mgr.pos_mgr.positions["IONQ"].mgmt["profit_trail_active"])
        # 실제 완료봉 종가 101.8(고점103 대비 -1.16% 이탈) + EMA9 하향(cur<ema9)
        #   → 확정봉 1개 빠른 SELL. 실시간가는 급락 아님(101.9)이라 봉이 판정 근거.
        self._set_bars([101.8])
        r2 = self._manage("IONQ", 101.9, ema9=102.5)
        self.assertEqual(len(self.api.sell_calls), 1)
        self.assertEqual(r2["action"], "SELL_ACCEPTED")

    def test_profit_trail_panic_immediate_sell(self):
        # 동적 트레일보다 +0.5%p 이상 급락 → 봉 없이 실시간가로 즉시 SELL(데이터 없음)
        self.mgr.pos_mgr.add(USM.USPosition("NNE", "NNE", "NASD", 10, 100.0))
        self._manage("NNE", 103.0)                  # 활성화(고점 103)
        self.api.intraday_bars = []                 # 봉 데이터 없음
        r = self._manage("NNE", 101.3, ema9=100.0, ema9_rising=True)  # live -1.65% panic
        self.assertEqual(len(self.api.sell_calls), 1)
        self.assertEqual(r["action"], "SELL_ACCEPTED")

    def test_completed_bar_below_trail_sells(self):
        # 실제 완료봉 종가가 트레일 아래 → 동적 트레일이 지배해 SELL(완료봉 근거)
        self.mgr.pos_mgr.add(USM.USPosition("BBAI", "BBAI", "NASD", 10, 100.0))
        self._manage("BBAI", 103.0)                 # 활성화(고점 103)
        self._set_bars([101.7])                     # 완료봉 -1.26% 이탈
        r = self._manage("BBAI", 101.9)             # 실시간가는 급락 아님 → 봉이 근거
        self.assertEqual(len(self.api.sell_calls), 1)
        self.assertEqual(r["action"], "SELL_ACCEPTED")

    def test_data_missing_no_confirmed_sell(self):
        # 데이터 없음 + 실시간가 이탈이나 급락 아님 → 확정봉 SELL 없음(HOLD)
        self.mgr.pos_mgr.add(USM.USPosition("LEU", "LEU", "NASD", 10, 100.0))
        self._manage("LEU", 103.0)
        self.api.intraday_bars = []
        r = self._manage("LEU", 101.9, ema9=102.5)  # live -1.07%(급락 아님), 봉 없음
        self.assertEqual(self.api.sell_calls, [])
        self.assertEqual(r["action"], "HOLD")

    def test_minus6_no_sell_without_structural_or_account_risk(self):
        # 고정 -6 손절 제거: -6 이어도 구조붕괴·계좌위험 없으면 데이터 없어도 매도 0
        self.mgr.pos_mgr.add(USM.USPosition("NVTS", "NVTS", "NASD", 10, 100.0))
        self.api.intraday_bars = []
        self._manage("NVTS", 94.0)                   # 진입(-6.25) → 경고, HOLD
        self._manage("NVTS", 93.0)                   # 더 하락해도 매도 없음
        self.assertEqual(self.api.sell_calls, [])
        self.assertTrue(self.mgr.pos_mgr.positions["NVTS"].mgmt["recovery_warn"])

    # ══════════════════════════════════════════════════════════
    # §5 단일 매도판정 권위: 고정익절/금액익절/SELL_SCORE/시간/MACD 매도 0회
    # ══════════════════════════════════════════════════════════
    def test_no_fixed_or_amount_or_score_sell_in_profit_range(self):
        # +1.5~+10% 구간, 고점 유지(이탈 없음) → 매도 0 (고정익절·금액익절·SCORE 무효)
        self.mgr.pos_mgr.add(USM.USPosition("BLNK", "BLNK", "NASD", 100, 100.0))
        for pct in (1.75, 2.25, 2.75, 3.5, 5.5, 8.0, 10.25):
            cur = 100.0 * (1 + (pct + 0.25) / 100.0)   # net≈pct, 고점 계속 갱신(이탈 없음)
            r = self._manage("BLNK", cur, sell_score=8)   # SELL_SCORE 최대여도 무효
            self.assertEqual(r["action"], "HOLD")
        self.assertEqual(self.api.sell_calls, [])          # 매도 0회

    def test_no_time_exit_sell(self):
        # 60분 경과만으로 매도 0 (시간청산 분기 비활성)
        self.mgr.pos_mgr.add(USM.USPosition("CEG", "CEG", "NASD", 10, 100.0))
        p = self.mgr.pos_mgr.positions["CEG"]
        p.created_at = (datetime.now() - timedelta(minutes=120)).isoformat()
        r = self._manage("CEG", 100.3)   # 2시간 경과, 소폭 수익, 비활성 → HOLD
        self.assertEqual(self.api.sell_calls, [])
        self.assertEqual(r["action"], "HOLD")

    def test_no_macd_ema_vwap_only_sell(self):
        # MACD 역전/EMA9 이탈/VWAP 하회 지표만으로 매도 0 (트레일 미활성)
        self.mgr.pos_mgr.add(USM.USPosition("CCJ", "CCJ", "NASD", 10, 100.0))
        # 지표는 매도 신호이나 profit trail 미활성(net<+1.5), net>-5 → HOLD
        r = self._manage("CCJ", 100.5, sell_score=8, ema9=105.0)  # cur<ema9, macd역전 등
        self.assertEqual(self.api.sell_calls, [])
        self.assertEqual(r["action"], "HOLD")

    def test_single_run_sell_exactly_once_through_submit_intent(self):
        # 실제 run()→_manage_position→_us_apply_management→_do_sell→submit-intent 1회
        self.mgr.pos_mgr.add(USM.USPosition("RGTI", "RGTI", "NASD", 10, 100.0))
        self._manage("RGTI", 103.0)                 # 활성화
        r = self._manage("RGTI", 101.3, ema9=100.0, ema9_rising=True)  # panic → SELL
        self.assertEqual(len(self.api.sell_calls), 1)
        self.assertEqual(r["action"], "SELL_ACCEPTED")
        self.assertEqual(self.mgr.pos_mgr.positions["RGTI"].management_mode, R.MODE_EXIT)
        # 재판정해도 EXIT_PENDING → 재제출 0
        self._manage("RGTI", 101.0, ema9=100.0)
        self.assertEqual(len(self.api.sell_calls), 1)

    # ══════════════════════════════════════════════════════════
    # 동시성: KR 루프 + US 정합화 무데드락
    # ══════════════════════════════════════════════════════════
    def test_concurrent_reconcile_no_deadlock(self):
        self._seed_internal(INTERNAL_9)
        self.api.balance_full = _snap([_holding(s) for s in KIS_7])
        errors = []
        def worker():
            try:
                for _ in range(20):
                    self.mgr.us_reconcile_positions()
            except Exception as e:      # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(self.mgr.pos_mgr.active_positions()), 7)


# ══════════════════════════════════════════════════════════════
# get_us_balance_full 완전성 집계 (거래소/페이지/부분실패/빈잔고 구분)
# ══════════════════════════════════════════════════════════════
class BalanceFullCompletenessTest(unittest.TestCase):
    def _call(self, per_exchange, exchanges):
        from api.kis_api import KISApi
        fake = types.SimpleNamespace()
        fake._fetch_us_balance_exchange = lambda excd, max_pages=20: per_exchange[excd]
        old = os.environ.get("US_BALANCE_EXCHANGES")
        os.environ["US_BALANCE_EXCHANGES"] = exchanges
        try:
            return KISApi.get_us_balance_full(fake)
        finally:
            if old is None:
                os.environ.pop("US_BALANCE_EXCHANGES", None)
            else:
                os.environ["US_BALANCE_EXCHANGES"] = old

    def test_all_exchanges_complete(self):
        per = {"NASD": {"ok": True, "complete": True,
                        "holdings": [_holding("IONQ")], "pages": 1},
               "NYSE": {"ok": True, "complete": True,
                        "holdings": [_holding("BA")], "pages": 1}}
        r = self._call(per, "NASD,NYSE")
        self.assertTrue(r["ok"])
        self.assertTrue(r["complete"])
        self.assertEqual(sorted(h["symbol"] for h in r["holdings"]), ["BA", "IONQ"])

    def test_partial_exchange_failure_incomplete(self):
        per = {"NASD": {"ok": True, "complete": True,
                        "holdings": [_holding("IONQ")], "pages": 1},
               "NYSE": {"ok": False, "complete": False, "holdings": [], "pages": 0}}
        r = self._call(per, "NASD,NYSE")
        self.assertTrue(r["ok"])            # NASD 는 성공
        self.assertFalse(r["complete"])     # NYSE 실패 → 전체 미완전
        self.assertFalse(r["authoritative_empty"])

    def test_authoritative_empty(self):
        per = {"NASD": {"ok": True, "complete": True, "holdings": [], "pages": 1}}
        r = self._call(per, "NASD")
        self.assertTrue(r["ok"])
        self.assertTrue(r["complete"])
        self.assertTrue(r["authoritative_empty"])

    def test_error_empty_not_authoritative(self):
        per = {"NASD": {"ok": False, "complete": False, "holdings": [], "pages": 0}}
        r = self._call(per, "NASD")
        self.assertFalse(r["ok"])
        self.assertIsNone(r["source"])
        self.assertFalse(r["complete"])
        self.assertFalse(r["authoritative_empty"])

    def test_dedup_across_exchanges(self):
        per = {"NASD": {"ok": True, "complete": True,
                        "holdings": [_holding("IONQ")], "pages": 1},
               "NYSE": {"ok": True, "complete": True,
                        "holdings": [_holding("IONQ")], "pages": 1}}
        r = self._call(per, "NASD,NYSE")
        self.assertEqual(len([h for h in r["holdings"] if h["symbol"] == "IONQ"]), 1)


if __name__ == "__main__":
    unittest.main()
