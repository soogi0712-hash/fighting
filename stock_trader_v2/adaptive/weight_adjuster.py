"""
adaptive/weight_adjuster.py — EV 기반 전략 가중치 자동 조정
============================================================
strategy_stats.json의 EV를 읽어 각 signal_type의 가중치를 조정.
strategy_weights.json에 저장 → KRStrategy / USStrategy가 참조.

★ 절대 변경 금지 항목 (안전장치):
  - 거래시간 (09:00~14:30)
  - 장마감 규칙 (15:20 강제청산)
  - 재진입 제한 (72h)
  - 수익 목표 (V2_PROFIT_LOCK_KRW)
  - 총 노출한도 / 주문가능금액 / 손실한도
  위 항목들은 이 모듈의 조정 대상이 아님.
  Adaptive Engine은 진입 신호 가중치만 조정함.

가중치 조정 알고리즘:
  EV > +0.5%  → weight += STEP_UP   (저장 상한 MAX_WEIGHT)
  EV > 0%     → weight += STEP_SMALL
  EV ≤ -0.3%  → weight -= STEP_DOWN (저장 하한 MIN_WEIGHT)
  EV ≤ -0.5%  → weight = 0.0 (BLOCK)
  거래 수 < MIN_TRADES → 조정 없음 (데이터 부족)

실전 적용 설계 (Adaptive = 위험 축소 시스템):
  BUY_SCORE 차감 금지 — 진입 점수는 그대로 유지.
  대신 진입 수량(qty_scale)만 축소 → 학습/검증 거래는 계속 진행.

  NORMAL  → qty_scale = 1.0  (full size)
  WARNING → qty_scale = 0.3  (30%  축소, 학습 유지)
  BLOCK   → qty_scale = 0.0  (진입 완전 차단)

★ ADAPTIVE_READONLY 모드 (긴급 안정화):
  True 로 설정하면 adjust()가 분석·로그만 하고 파일 저장·가중치 변경 없음.
  live_w / BUY_SCORE / 진입조건 자동 변경 완전 차단.
  get_effective_weight(), get_adaptive_qty_scale() 은 그대로 동작.

변경 이력:
  2026-06-24: WARNING_SCALE(BUY_SCORE 차감) 제거
              → get_adaptive_qty_scale() 추가 (qty_scale 반환)
              → DISABLED 상태명 → BLOCK으로 변경
  2026-06-25: ADAPTIVE_READONLY 플래그 추가
              → adjust() 에서 가중치 파일 저장 완전 차단
"""

import os
import json
from datetime import datetime
from typing import Optional

from utils.v2_logger import get_logger
from adaptive.strategy_analyzer import StrategyAnalyzer, _DATA_DIR

logger = get_logger("WeightAdjuster")

_WEIGHTS_FILE = os.path.join(_DATA_DIR, "strategy_weights.json")

# ★ [긴급 안정화] Adaptive 읽기전용 모드
# True: adjust()가 분석/로그만 하고 가중치 파일 저장·변경 완전 차단
# False: 정상 모드 (EV 기반 가중치 자동 조정 + 저장)
ADAPTIVE_READONLY: bool = True

# ── 내부 저장 가중치 범위 (장기 학습 누적용) ──────────────────
MIN_WEIGHT  = 0.1
MAX_WEIGHT  = 2.0
DEFAULT_W   = 1.0

# ── 실전 적용 클램핑 범위 (첫 실전 적용 안전 제한) ───────────
LIVE_MIN_WEIGHT = 0.80   # 어떤 전략이 나빠도 -20% 이상 약화 금지
LIVE_MAX_WEIGHT = 1.20   # 어떤 전략이 좋아도 +20% 이상 강화 금지

# WARNING / BLOCK qty_scale 상수
WARNING_QTY_SCALE = 0.30   # WARNING 전략: 진입금액 30%로 축소 (학습 유지)
BLOCK_QTY_SCALE   = 0.0    # BLOCK 전략:   진입 완전 차단

# 조정 스텝
STEP_UP     = 0.10    # EV > +0.5% 시 +10%
STEP_SMALL  = 0.05    # EV > 0%   시 +5%
STEP_DOWN   = 0.10    # EV ≤ -0.3% 시 -10%

# 조정 발동 최소 거래 수
MIN_TRADES  = 20


class WeightAdjuster:
    """
    StrategyAnalyzer 결과를 기반으로 전략 가중치를 자동 조정.

    사용법:
        adjuster = WeightAdjuster(analyzer)
        report   = adjuster.adjust()   # 조정 실행 + 보고서 반환

        # 전략 매수 시 실전 적용 가중치 조회 (클램핑 + WARNING 축소 적용)
        w = adjuster.get_effective_weight("KR", "폭발돌파")

        # 신호 허용 여부 + 상태 조회
        allowed, status = adjuster.check_signal("KR", "폭발돌파")
    """

    def __init__(self, analyzer: StrategyAnalyzer):
        self.analyzer = analyzer
        self._weights: dict = self._load_weights()

    # ── 메인 조정 실행 ────────────────────────────────────────

    def adjust(self, market: Optional[str] = None) -> list[dict]:
        """
        전체 또는 특정 시장 전략 가중치 자동 조정.
        Returns: 조정 내역 리스트 [{signal_type, old_w, new_w, ev, reason}]

        ★ ADAPTIVE_READONLY=True 이면 분석·로그만 하고 가중치 저장 없음.
        """
        # ★ [긴급 안정화] 읽기전용 모드 체크
        if ADAPTIVE_READONLY:
            logger.info(
                "[WeightAdjuster] ADAPTIVE_READONLY=True — "
                "분석/로그만 실행, 가중치 저장·변경 차단"
            )

        # 최신 분석 실행
        self.analyzer.run(market=market)
        stats = self.analyzer.get_signal_stats(market)

        report = []
        for key, s in stats.items():
            mkt = s.get("market", "KR")
            sig = s.get("signal_type", key)
            ev  = s.get("ev", 0.0)
            n   = s.get("trade_count", 0)
            status = s.get("status", "ACTIVE")

            old_w = self._weights.get(key, DEFAULT_W)
            new_w = old_w
            reason = "변동없음"

            # DISABLED → 가중치 0
            if status == "DISABLED":
                new_w  = 0.0
                reason = f"DISABLED (EV={ev:+.3f}%, n={n})"

            elif n < MIN_TRADES:
                reason = f"데이터부족 (n={n} < {MIN_TRADES})"

            elif ev > 0.5:
                new_w  = min(old_w + STEP_UP, MAX_WEIGHT)
                reason = f"EV={ev:+.3f}% 우수 → +{STEP_UP*100:.0f}%"

            elif ev > 0:
                new_w  = min(old_w + STEP_SMALL, MAX_WEIGHT)
                reason = f"EV={ev:+.3f}% 양호 → +{STEP_SMALL*100:.0f}%"

            elif ev <= -0.5 and n >= 50:
                new_w  = max(old_w - STEP_DOWN * 2, MIN_WEIGHT)
                reason = f"EV={ev:+.3f}% 매우 나쁨 → -{STEP_DOWN*200:.0f}%"

            elif ev <= -0.3:
                new_w  = max(old_w - STEP_DOWN, MIN_WEIGHT)
                reason = f"EV={ev:+.3f}% 부진 → -{STEP_DOWN*100:.0f}%"

            new_w = round(new_w, 3)

            # ★ READONLY: 가중치 메모리 반영/파일 저장 차단 (로그만 출력)
            if ADAPTIVE_READONLY:
                if abs(new_w - old_w) > 0.001 or status == "DISABLED":
                    live_w = self._apply_live_clamp(new_w, status)
                    logger.info(
                        f"[WeightAdjuster][READONLY] {mkt}:{sig} "
                        f"저장가중치 유지={old_w:.2f} (조정 억제: {old_w:.2f}→{new_w:.2f}) "
                        f"| 실전가중치 유지={self._apply_live_clamp(old_w, status):.2f} "
                        f"| {reason} | READONLY모드"
                    )
                continue  # 저장·메모리 반영 없이 다음 항목으로

            # 변동이 있을 때만 기록
            if abs(new_w - old_w) > 0.001 or status == "DISABLED":
                self._weights[key] = new_w
                # 실전 적용 가중치도 함께 계산해서 로그
                live_w = self._apply_live_clamp(new_w, status)
                report.append({
                    "market":        mkt,
                    "signal_type":   sig,
                    "old_weight":    round(old_w, 3),
                    "new_weight":    new_w,
                    "live_weight":   live_w,
                    "ev":            ev,
                    "win_rate":      s.get("win_rate", 0),
                    "trade_count":   n,
                    "status":        status,
                    "reason":        reason,
                    "adjusted_at":   datetime.now().isoformat(),
                })
                logger.info(
                    f"[WeightAdjuster] {mkt}:{sig} "
                    f"저장가중치 {old_w:.2f} → {new_w:.2f} "
                    f"| 실전가중치 {live_w:.2f} | {reason}"
                )

        # ★ READONLY: 파일 저장 차단
        if not ADAPTIVE_READONLY:
            self._save_weights()
        else:
            logger.info(
                "[WeightAdjuster] READONLY — strategy_weights.json 저장 차단 완료"
            )
        return report

    # ── 실전 가중치 조회 (핵심 API) ──────────────────────────

    def get_effective_weight(self, market: str, signal_type: str) -> float:
        """
        실전 매매에 사용하는 유효 가중치 반환.

        적용 규칙:
          1. BLOCK → 0.0  (진입 불가)
          2. 저장 가중치를 LIVE_MIN~LIVE_MAX 범위로 클램핑
          3. WARNING/NORMAL 모두 BUY_SCORE 차감 없음 (qty_scale로 대체)

        Returns: float (0.0 또는 LIVE_MIN ~ LIVE_MAX 범위)
        """
        key    = f"{market}:{signal_type}"
        status = self.analyzer.get_status(market, signal_type)

        if status in ("BLOCK", "DISABLED"):   # DISABLED 하위호환 유지
            return 0.0

        raw_w  = self._weights.get(key, DEFAULT_W)
        return self._apply_live_clamp(raw_w, status)

    def get_adaptive_qty_scale(self, market: str, signal_type: str) -> float:
        """
        Adaptive 상태에 따른 진입 수량 배율 반환.

        설계 원칙 — Adaptive = 위험 축소 시스템:
          BUY_SCORE는 차감하지 않음. 진입 여부는 원래 기준 그대로.
          단, 상태에 따라 진입 수량만 줄여서 위험을 통제.

        Returns:
          NORMAL  → 1.0  (full size)
          WARNING → 0.3  (30% 축소, 학습/검증 거래 유지)
          BLOCK   → 0.0  (진입 금지)
        """
        status = self.analyzer.get_status(market, signal_type)
        if status in ("BLOCK", "DISABLED"):
            return BLOCK_QTY_SCALE
        if status == "WARNING":
            return WARNING_QTY_SCALE
        return 1.0  # ACTIVE

    def check_signal(self, market: str, signal_type: str) -> tuple:
        """
        신호 허용 여부와 상태를 동시에 반환.

        Returns:
            (allowed: bool, status: str)
            allowed = False  → BLOCK, 진입 금지
            allowed = True   → ACTIVE 또는 WARNING (qty_scale 축소, BUY_SCORE 그대로)
        """
        status  = self.analyzer.get_status(market, signal_type)
        allowed = status not in ("BLOCK", "DISABLED")
        return allowed, status

    # ── 레거시 호환 API ───────────────────────────────────────

    def get_weight(self, market: str, signal_type: str) -> float:
        """
        [레거시] 저장 가중치 반환. DISABLED=0.0, 미등록=1.0.
        실전 매매에는 get_effective_weight()를 사용할 것.
        """
        key    = f"{market}:{signal_type}"
        status = self.analyzer.get_status(market, signal_type)
        if status == "DISABLED":
            return 0.0
        return self._weights.get(key, DEFAULT_W)

    def is_signal_allowed(self, market: str, signal_type: str) -> bool:
        """DISABLED 전략 진입 차단 여부."""
        return self.analyzer.is_active(market, signal_type)

    def get_all_weights(self) -> dict:
        return dict(self._weights)

    # ── 내부 클램핑 헬퍼 ─────────────────────────────────────

    @staticmethod
    def _apply_live_clamp(raw_w: float, status: str) -> float:
        """
        저장 가중치 → 실전 가중치 변환.
          1) LIVE_MIN ~ LIVE_MAX 클램핑
          2) WARNING/NORMAL 모두 BUY_SCORE 차감 없음 (qty_scale로 위험 관리)
        """
        # LIVE 클램핑만 적용 — WARNING이어도 BUY_SCORE 차감 없음
        clamped = max(LIVE_MIN_WEIGHT, min(raw_w, LIVE_MAX_WEIGHT))
        return round(clamped, 3)

    # ── 파일 I/O ─────────────────────────────────────────────

    def _load_weights(self) -> dict:
        try:
            if os.path.exists(_WEIGHTS_FILE):
                with open(_WEIGHTS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_weights(self):
        data = dict(self._weights)
        data["_updated_at"]         = datetime.now().isoformat()
        data["_live_min"]            = LIVE_MIN_WEIGHT
        data["_live_max"]            = LIVE_MAX_WEIGHT
        data["_warning_qty_scale"]   = WARNING_QTY_SCALE
        data["_block_qty_scale"]     = BLOCK_QTY_SCALE
        with open(_WEIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
