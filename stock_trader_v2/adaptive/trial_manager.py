"""
adaptive/trial_manager.py — 제한 실험 계획 관리자
==================================================
공격안 등 실험적 파라미터 변경을 '7거래 / 하루 / -10,000원 손절' 조건 하에
제한적으로 AUTO_APPLY하고, 조건 충족 후 자동 롤백하는 엔진.

흐름:
  [활성화]  TrialPlanManager.activate(plan_dict)
              → data/active_plan.json 기록
              → 파라미터 패치 즉시 반영

  [거래 시]  kr_strategy.py / position_guard.py 에서
              TrialPlanManager.get_active_plan() 조회
              → qty_scale, breakout_tolerance, weak_entry_cut 등 읽어서 적용

  [거래 완료 후]  TrialPlanManager.on_trade_closed(pnl_krw, exit_pct) 호출
              → trade_count 증가, cum_pnl_krw 누적
              → 롤백 조건 충족 시 → rollback() 자동 호출

  [롤백]    active_plan.json → status = "rolled_back"
              → 파라미터 원상 복구
              → TRIAL_RESULT 로그 출력

설계 원칙:
  - active_plan.json 이 유일한 진실의 근원 (멀티프로세스 안전: 파일 잠금 사용)
  - 코드 수정 없이 파라미터만 오버라이드
  - 롤백 후에는 더 이상 active_plan이 없는 상태가 됨 (재활성화 필요)

변경 이력:
  2026-06-24: 초기 작성
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, date
from typing import Any, Optional

import pytz

from utils.v2_logger import get_logger

logger = get_logger("TrialManager")
KST    = pytz.timezone("Asia/Seoul")

_DATA_DIR       = os.path.join(os.path.dirname(__file__), "..", "data")
_ACTIVE_PLAN    = os.path.join(_DATA_DIR, "active_plan.json")
_TRIAL_HISTORY  = os.path.join(_DATA_DIR, "trial_history.json")

# ── 롤백 조건 상수 (activate() 호출 시 plan에 없으면 이 기본값 사용) ──
_DEFAULT_MAX_TRADES   = 7        # 최대 적용 거래수
_DEFAULT_MAX_LOSS_KRW = -10_000  # 누적 실현손실 한도 (원)
_DEFAULT_EXPIRE_DATE  = None     # 만료일 (None = 당일만)

# ── 실험 파라미터 기본값 (롤백 후 복구 목표) ──────────────────────
_PARAM_DEFAULTS = {
    "qty_scale":            1.0,    # 진입 수량 배율 (1.0 = 변경 없음)
    "breakout_tolerance":   0.0,    # 돌파봉저가 허용폭 (0.0 = 현행)
    "weak_entry_cut_pct":  -0.7,    # WEAK_ENTRY_EXIT 기준 (현행)
    "weak_entry_max_min":   5.0,    # WEAK_ENTRY 적용 시간 상한 (현행)
    "buy_score_early":      0.40,   # BUY_SCORE_EARLY (현행)
}


# ════════════════════════════════════════════════════════════════
class TrialPlanManager:
    """
    제한 실험 계획 관리자 (싱글턴 패턴 권장).

    사용법:
        from adaptive.trial_manager import TrialPlanManager
        tm = TrialPlanManager()

        # 공격안 활성화
        tm.activate({
            "plan_name":      "공격안",
            "source":         "AUTO_APPLIED",
            "max_trades":     7,
            "max_loss_krw":   -10_000,
            "params": {
                "qty_scale":          0.30,
                "breakout_tolerance": 0.003,
                "weak_entry_cut_pct": -1.0,
            },
        })

        # 진입 시 파라미터 조회
        plan = tm.get_active_plan()
        qty_scale = plan["params"]["qty_scale"] if plan else 1.0

        # 거래 완료 후 체크
        tm.on_trade_closed(pnl_krw=-3500, exit_pct=-0.85)
    """

    # ── 싱글턴 ───────────────────────────────────────────────────
    _instance: Optional["TrialPlanManager"] = None

    def __new__(cls) -> "TrialPlanManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ────────────────────────────────────────────────────────────
    # 활성화
    # ────────────────────────────────────────────────────────────

    def activate(self, plan: dict) -> dict:
        """
        실험 계획을 활성화한다.

        plan 필드:
            plan_name     str   예: "공격안"
            source        str   예: "AUTO_APPLIED" | "MANUAL"
            max_trades    int   (기본 7)
            max_loss_krw  int   (기본 -10000)
            expire_date   str   (기본 오늘 날짜 "YYYY-MM-DD")
            params        dict  오버라이드할 파라미터 키-값
                  qty_scale            float  진입금액 배율 (0.30 = 30%)
                  breakout_tolerance   float  돌파봉저가 허용폭 (비율, e.g. 0.003)
                  weak_entry_cut_pct   float  WEAK_ENTRY 기준 (e.g. -1.0)
                  weak_entry_max_min   float  WEAK_ENTRY 적용 분 (e.g. 7.0)
                  buy_score_early      float  BUY_SCORE_EARLY (e.g. 0.44)

        Returns:
            저장된 active_plan dict
        """
        now_str     = datetime.now(KST).isoformat()
        today_str   = datetime.now(KST).strftime("%Y-%m-%d")

        # 파라미터 기본값 병합
        merged_params = dict(_PARAM_DEFAULTS)
        merged_params.update(plan.get("params", {}))

        active = {
            "plan_name":    plan.get("plan_name", "실험안"),
            "source":       plan.get("source",    "MANUAL"),
            "status":       "ACTIVE",
            "activated_at": now_str,
            "expire_date":  plan.get("expire_date", today_str),
            "max_trades":   int(plan.get("max_trades",   _DEFAULT_MAX_TRADES)),
            "max_loss_krw": int(plan.get("max_loss_krw", _DEFAULT_MAX_LOSS_KRW)),
            "trade_count":  0,
            "cum_pnl_krw":  0,
            "cum_ev_pct":   0.0,
            "win_count":    0,
            "trades":       [],         # {exit_pct, pnl_krw, closed_at}
            "params":       merged_params,
            "rollback_reason": "",
            "rolled_back_at":  "",
        }

        self._save(active)
        logger.info(
            f"[ACTIVE_PLAN] 채택안={active['plan_name']} | "
            f"적용상태={active['source']} | "
            f"적용기간={active['max_trades']}거래 | "
            f"롤백조건=EV개선없음 또는 누적손실{active['max_loss_krw']:,}원 | "
            f"파라미터: {merged_params}"
        )
        return active

    # ────────────────────────────────────────────────────────────
    # 조회
    # ────────────────────────────────────────────────────────────

    def get_active_plan(self) -> Optional[dict]:
        """
        현재 유효한 ACTIVE 플랜을 반환.
        없거나 만료·롤백됐으면 None.
        """
        plan = self._load()
        if not plan:
            return None
        if plan.get("status") != "ACTIVE":
            return None

        # 날짜 만료 체크
        today = datetime.now(KST).strftime("%Y-%m-%d")
        expire = plan.get("expire_date", today)
        if expire and today > expire:
            self._expire(plan, f"만료일 경과 ({expire})")
            return None

        return plan

    def is_active(self) -> bool:
        """유효한 실험 계획이 진행 중인지 여부."""
        return self.get_active_plan() is not None

    def get_param(self, key: str, default: Any = None) -> Any:
        """
        현재 활성 플랜의 파라미터 값을 반환.
        플랜 없으면 default 반환.
        """
        plan = self.get_active_plan()
        if plan is None:
            return default if default is not None else _PARAM_DEFAULTS.get(key)
        return plan["params"].get(key, _PARAM_DEFAULTS.get(key, default))

    # ────────────────────────────────────────────────────────────
    # 거래 완료 통보 (on_trade_closed) — 롤백 조건 체크
    # ────────────────────────────────────────────────────────────

    def on_trade_closed(self, pnl_krw: float, exit_pct: float) -> dict:
        """
        거래 1건 완료 시 호출. 누적 집계 후 롤백 조건 자동 체크.

        Args:
            pnl_krw:  실현손익 (원, 음수=손실)
            exit_pct: 손익률 (%, 음수=손실)

        Returns:
            {
              "status":   "ACTIVE" | "ROLLED_BACK" | "COMPLETED" | "INACTIVE",
              "rollback": bool,
              "reason":   str,
              "summary":  dict,   # 지금까지 누적 통계
            }
        """
        plan = self._load()
        if not plan or plan.get("status") != "ACTIVE":
            return {"status": "INACTIVE", "rollback": False,
                    "reason": "플랜 없음", "summary": {}}

        now_str = datetime.now(KST).isoformat()

        # 누적
        plan["trade_count"] += 1
        plan["cum_pnl_krw"] += int(pnl_krw)
        plan["cum_ev_pct"]   = round(
            plan["cum_ev_pct"] + exit_pct, 3
        )
        if exit_pct > 0:
            plan["win_count"] += 1
        plan["trades"].append({
            "exit_pct":   round(exit_pct, 3),
            "pnl_krw":    int(pnl_krw),
            "closed_at":  now_str,
        })

        n        = plan["trade_count"]
        cum_krw  = plan["cum_pnl_krw"]
        max_n    = plan["max_trades"]
        max_loss = plan["max_loss_krw"]

        summary = {
            "trade_count": n,
            "win_count":   plan["win_count"],
            "win_rate":    round(plan["win_count"] / n * 100, 1) if n else 0,
            "cum_pnl_krw": cum_krw,
            "cum_ev_pct":  plan["cum_ev_pct"],
            "avg_ev_pct":  round(plan["cum_ev_pct"] / n, 3) if n else 0,
        }

        logger.info(
            f"[ACTIVE_PLAN] 거래완료 #{n}/{max_n} | "
            f"손익={pnl_krw:+,.0f}원({exit_pct:+.2f}%) | "
            f"누적={cum_krw:+,.0f}원 | "
            f"avgEV={summary['avg_ev_pct']:+.3f}%"
        )

        # ── 롤백 조건 1: 누적 손실 한도 ──────────────────────
        if cum_krw <= max_loss:
            reason = (
                f"누적손실 {cum_krw:+,}원 ≤ 한도 {max_loss:,}원 → 즉시 롤백"
            )
            self._rollback(plan, reason, summary)
            return {"status": "ROLLED_BACK", "rollback": True,
                    "reason": reason, "summary": summary}

        # ── 롤백 조건 2: 거래수 소진 → EV 검증 ──────────────
        if n >= max_n:
            baseline_ev = -0.536   # 실험 전 기준 EV (DB 기준, 향후 동적 조회 가능)
            avg_ev = summary["avg_ev_pct"]
            if avg_ev <= baseline_ev:
                reason = (
                    f"{n}거래 완료 | avgEV={avg_ev:+.3f}% ≤ 기준EV={baseline_ev:+.3f}% "
                    f"→ EV 미개선 롤백"
                )
                self._rollback(plan, reason, summary)
                return {"status": "ROLLED_BACK", "rollback": True,
                        "reason": reason, "summary": summary}
            else:
                reason = (
                    f"{n}거래 완료 | avgEV={avg_ev:+.3f}% > 기준EV={baseline_ev:+.3f}% "
                    f"→ EV 개선 확인 — 계획 유지(COMPLETED)"
                )
                plan["status"] = "COMPLETED"
                self._save(plan)
                self._print_trial_result(plan, summary, verdict="유지")
                logger.info(f"[ACTIVE_PLAN] {reason}")
                return {"status": "COMPLETED", "rollback": False,
                        "reason": reason, "summary": summary}

        # 계속 진행
        self._save(plan)
        return {"status": "ACTIVE", "rollback": False,
                "reason": f"진행 중 ({n}/{max_n})", "summary": summary}

    # ────────────────────────────────────────────────────────────
    # 롤백
    # ────────────────────────────────────────────────────────────

    def rollback(self, reason: str = "수동 롤백") -> None:
        """외부에서 강제 롤백 호출."""
        plan = self._load()
        if not plan:
            logger.info("[ACTIVE_PLAN] 롤백할 플랜 없음")
            return
        summary = self._make_summary(plan)
        self._rollback(plan, reason, summary)

    def _rollback(self, plan: dict, reason: str, summary: dict) -> None:
        plan["status"]          = "ROLLED_BACK"
        plan["rollback_reason"] = reason
        plan["rolled_back_at"]  = datetime.now(KST).isoformat()
        self._save(plan)
        self._append_history(plan, summary)
        self._print_trial_result(plan, summary, verdict="롤백")
        logger.warning(
            f"[ACTIVE_PLAN] 🔴 롤백 완료 | 사유: {reason} | "
            f"파라미터 원상복구 → 기본값 적용"
        )

    def _expire(self, plan: dict, reason: str) -> None:
        plan["status"]          = "EXPIRED"
        plan["rollback_reason"] = reason
        plan["rolled_back_at"]  = datetime.now(KST).isoformat()
        self._save(plan)
        logger.info(f"[ACTIVE_PLAN] 만료 처리: {reason}")

    # ────────────────────────────────────────────────────────────
    # TRIAL_RESULT 로그 출력
    # ────────────────────────────────────────────────────────────

    def _print_trial_result(
        self, plan: dict, summary: dict, verdict: str
    ) -> None:
        SEP = "─" * 60
        n   = summary.get("trade_count", 0)
        wr  = summary.get("win_rate",    0)
        ev  = summary.get("avg_ev_pct",  0)
        krw = summary.get("cum_pnl_krw", 0)

        lines = [
            "",
            "═" * 60,
            f"  [TRIAL_RESULT]  {plan['plan_name']} ({plan['source']})",
            SEP,
            f"  거래수:    {n}건 / {plan['max_trades']}건",
            f"  승률:      {wr:.1f}%",
            f"  EV:        {ev:+.3f}%/거래",
            f"  실현손익:  {krw:+,.0f}원",
            f"  판정:      {verdict}",
            f"  사유:      {plan.get('rollback_reason', '')}",
            SEP,
        ]
        if plan.get("trades"):
            lines.append("  거래 내역:")
            for i, t in enumerate(plan["trades"], 1):
                sign = "✅" if t["exit_pct"] > 0 else "❌"
                lines.append(
                    f"    #{i:02d} {sign} {t['exit_pct']:+.2f}%  "
                    f"{t['pnl_krw']:+,.0f}원  "
                    f"{t.get('closed_at','')[:16]}"
                )
        lines.append("═" * 60)
        for line in lines:
            logger.info(line)

    # ────────────────────────────────────────────────────────────
    # 유틸
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _make_summary(plan: dict) -> dict:
        n = plan.get("trade_count", 0)
        return {
            "trade_count": n,
            "win_count":   plan.get("win_count", 0),
            "win_rate":    round(plan.get("win_count", 0) / n * 100, 1) if n else 0,
            "cum_pnl_krw": plan.get("cum_pnl_krw", 0),
            "cum_ev_pct":  plan.get("cum_ev_pct",  0.0),
            "avg_ev_pct":  round(plan.get("cum_ev_pct", 0) / n, 3) if n else 0,
        }

    # ── 파일 I/O (fcntl 잠금으로 멀티프로세스 안전) ─────────────

    @staticmethod
    def _load() -> Optional[dict]:
        if not os.path.exists(_ACTIVE_PLAN):
            return None
        try:
            with open(_ACTIVE_PLAN, "r", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f, fcntl.LOCK_UN)
            return data
        except Exception as e:
            logger.warning(f"[TrialManager] active_plan.json 로드 실패: {e}")
            return None

    @staticmethod
    def _save(plan: dict) -> None:
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_ACTIVE_PLAN, "w", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                json.dump(plan, f, ensure_ascii=False, indent=2)
                fcntl.flock(f, fcntl.LOCK_UN)
        except Exception as e:
            logger.warning(f"[TrialManager] active_plan.json 저장 실패: {e}")

    @staticmethod
    def _append_history(plan: dict, summary: dict) -> None:
        """롤백/완료된 계획을 trial_history.json에 누적 저장."""
        try:
            history = {}
            if os.path.exists(_TRIAL_HISTORY):
                with open(_TRIAL_HISTORY, "r", encoding="utf-8") as f:
                    history = json.load(f)
            key = f"{plan.get('activated_at','')[:10]}_{plan['plan_name']}"
            history[key] = {**plan, "summary": summary}
            # 최근 100건만 보존
            if len(history) > 100:
                oldest = sorted(history.keys())[:len(history) - 90]
                for k in oldest:
                    del history[k]
            with open(_TRIAL_HISTORY, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[TrialManager] trial_history.json 저장 실패: {e}")


# ── 전역 싱글턴 ────────────────────────────────────────────────
_trial_manager: Optional[TrialPlanManager] = None


def get_trial_manager() -> TrialPlanManager:
    """전역 TrialPlanManager 싱글턴 반환."""
    global _trial_manager
    if _trial_manager is None:
        _trial_manager = TrialPlanManager()
    return _trial_manager
