"""
전략 실험실 — 전략 정의 및 설정
=====================================

전략 계층 (Tier):
  LIVE       — 실전 전략 (실계좌 주문 가능)
  CANDIDATE  — 후보 전략 (검증 중, 주문 불가)
  EXPERIMENT — 실험 전략 (가상 운용만, 주문 절대 금지)

전략 그룹:
  S1~S5  — 손절 실험     (stop-loss)
  T1~T6  — 트레일링스탑   (trailing-stop)
  P1~P4  — 추가매수 실험  (add-buy / pyramiding)
  BASE   — 현재 실전 기준선

전략 평가 점수 가중치:
  수익률   30%
  MDD     25%
  샤프비율 20%
  승률     10%
  손익비   15%

승격 조건 (EXPERIMENT → CANDIDATE):
  - 최근 3개월 수익률 > 0%
  - MDD > -20%
  - 거래횟수 >= 5
  - 샤프비율 >= 0.5

승격 조건 (CANDIDATE → LIVE):
  - 1개월 이상 CANDIDATE 유지
  - 최근 수익률 상위 30%
  - MDD > -15%
  - 승률 >= 50%
"""

from dataclasses import dataclass, field
from typing import List, Optional

# ── 자산군 상수 (asset_universe 에서 재-익스포트) ──────────────
try:
    from screener.asset_universe import (
        ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ,
        ASSET_ETF_GENERAL, ASSET_ETF_LEVERAGE, ASSET_ETF_INVERSE,
        ASSET_CASH,
        WEIGHT_LIMITS,
        REGIME_TARGET_WEIGHTS,
        REGIME_PRIORITY,
        get_asset_type_label,
    )
except ImportError:
    # 독립 실행 환경 폴백
    ASSET_STOCK_KOSPI   = "STOCK_KOSPI"
    ASSET_STOCK_KOSDAQ  = "STOCK_KOSDAQ"
    ASSET_ETF_GENERAL   = "ETF_GENERAL"
    ASSET_ETF_LEVERAGE  = "ETF_LEVERAGE"
    ASSET_ETF_INVERSE   = "ETF_INVERSE"
    ASSET_CASH          = "CASH"
    WEIGHT_LIMITS       = {
        ASSET_ETF_LEVERAGE: 0.20,
        ASSET_ETF_INVERSE:  0.40,
        ASSET_CASH:         0.10,
    }
    REGIME_TARGET_WEIGHTS = {
        "BULL":    {ASSET_STOCK_KOSPI: 0.40, ASSET_STOCK_KOSDAQ: 0.25,
                    ASSET_ETF_LEVERAGE: 0.15, ASSET_ETF_GENERAL: 0.10,
                    ASSET_ETF_INVERSE: 0.00},
        "LATERAL": {ASSET_STOCK_KOSPI: 0.25, ASSET_STOCK_KOSDAQ: 0.15,
                    ASSET_ETF_GENERAL: 0.35, ASSET_ETF_LEVERAGE: 0.00,
                    ASSET_ETF_INVERSE: 0.10},
        "BEAR":    {ASSET_STOCK_KOSPI: 0.05, ASSET_STOCK_KOSDAQ: 0.05,
                    ASSET_ETF_GENERAL: 0.10, ASSET_ETF_LEVERAGE: 0.00,
                    ASSET_ETF_INVERSE: 0.35},
    }
    REGIME_PRIORITY = {
        "BULL":    [ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ, ASSET_ETF_LEVERAGE,
                    ASSET_ETF_GENERAL, ASSET_ETF_INVERSE],
        "LATERAL": [ASSET_ETF_GENERAL, ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ,
                    ASSET_ETF_INVERSE, ASSET_ETF_LEVERAGE],
        "BEAR":    [ASSET_ETF_INVERSE, ASSET_CASH, ASSET_ETF_GENERAL,
                    ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ, ASSET_ETF_LEVERAGE],
    }
    def get_asset_type_label(at): return at  # noqa

# ── 자산군 제약 상수 (전략 실험실용) ────────────────────────────
# 전략 실험실 내에서 참조하는 자산군 비중 제한
ASSET_TYPE_CONSTRAINTS = {
    ASSET_ETF_LEVERAGE: {
        "max_portfolio_ratio": WEIGHT_LIMITS.get(ASSET_ETF_LEVERAGE, 0.20),
        "allowed_regimes":     ["BULL"],          # BULL 에서만 허용
        "min_ai_score":        80.0,              # AI 점수 ≥ 80
        "require_positive_rs": True,              # RS > 0 필수
    },
    ASSET_ETF_INVERSE: {
        "max_portfolio_ratio": WEIGHT_LIMITS.get(ASSET_ETF_INVERSE, 0.40),
        "allowed_regimes":     ["BEAR", "LATERAL"],  # BEAR 우선
        "min_ai_score":        55.0,
        "require_positive_rs": False,
    },
    ASSET_CASH: {
        "min_portfolio_ratio": WEIGHT_LIMITS.get(ASSET_CASH, 0.10),  # ≥ 10%
    },
}

# ── 전략 계층 ────────────────────────────────────────────────
TIER_LIVE       = "LIVE"        # 실전 (실계좌 주문 가능)
TIER_CANDIDATE  = "CANDIDATE"   # 후보 (검증 중)
TIER_EXPERIMENT = "EXPERIMENT"  # 실험 (가상만)

# ── 평가 점수 가중치 ─────────────────────────────────────────
SCORE_WEIGHTS = {
    "total_return":  0.30,
    "mdd":           0.25,   # 절댓값이 작을수록 좋음
    "sharpe":        0.20,
    "win_rate":      0.10,
    "profit_factor": 0.15,
}

# ── 승격 조건 ────────────────────────────────────────────────
PROMOTION_TO_CANDIDATE = {
    "min_return_3m":    0.0,   # 최근 3개월 수익률 > 0%
    "max_mdd":        -20.0,   # MDD > -20%
    "min_trades":       5,     # 거래횟수 >= 5
    "min_sharpe":       0.5,   # 샤프비율 >= 0.5
}
PROMOTION_TO_LIVE = {
    "min_candidate_days": 30,  # CANDIDATE 유지 >= 30일
    "min_return_pct":    15.0, # 수익률 상위 30% (예시)
    "max_mdd":          -15.0, # MDD > -15%
    "min_win_rate":      50.0, # 승률 >= 50%
}


# ── 전략 정의 데이터클래스 ───────────────────────────────────
@dataclass
class StrategyConfig:
    id:            str           # 전략 ID (예: "S1", "T3", "P2")
    name:          str           # 이름
    group:         str           # 그룹 ("STOP", "TRAIL", "PYRAMID", "BASE")
    tier:          str           # LIVE / CANDIDATE / EXPERIMENT
    description:   str           # 설명

    # 손절 설정
    stop_loss_pct:     float = -10.0  # 손절 기준 (%)

    # 트레일링스탑 설정
    trailing_pct:      float = -15.0  # 고점 대비 (%)
    trailing_activate: float =   5.0  # 활성화 조건: 고점이 진입가 대비 +X% 이상

    # 추가매수 설정
    add_buy_levels: List[float] = field(default_factory=lambda: [10.0, 20.0, 35.0])
    add_buy_ratio:  float = 0.20      # 추가매수 시 비중

    # 초기 진입 비중
    entry_ratio:    float = 0.25      # 첫 진입 비중 (전체 가용자금 대비)

    # ── 자산군 설정 (asset_universe 연동) ───────────────────
    # 허용 자산군 목록 (빈 리스트 = 전체 허용)
    allowed_asset_types: List[str] = field(default_factory=list)
    # 자산군별 최대 개별 비중 오버라이드 (기본값: AssetAllocator.MAX_SINGLE_RATIO)
    asset_ratio_overrides: dict = field(default_factory=dict)
    # 레버리지 ETF 허용 여부 (기본: 자산군 제약 따름)
    allow_leverage_etf:  bool = True
    # 인버스 ETF 허용 여부
    allow_inverse_etf:   bool = True

    # 메타
    is_real_order:  bool  = False     # True = 실제 주문 허용 (LIVE만)
    created_at:     str   = ""
    candidate_since: Optional[str] = None   # CANDIDATE 된 날짜


# ── 전략 레지스트리 ──────────────────────────────────────────
def build_strategies() -> dict[str, StrategyConfig]:
    """
    모든 전략을 정의하고 반환한다.
    실전 전략(BASE)는 LIVE 티어로, 나머지는 EXPERIMENT 로 시작.
    """
    strategies = {}

    # ── BASE: 현재 실전 전략 기준선 ─────────────────────────
    strategies["BASE"] = StrategyConfig(
        id="BASE", name="현재 실전 기준",
        group="BASE", tier=TIER_LIVE,
        description="현재 실전 운용 중인 피라미딩 전략 기준선",
        stop_loss_pct=-3.0,
        trailing_pct=-2.0,
        trailing_activate=2.0,
        add_buy_levels=[2.0, 4.0, 6.0],
        add_buy_ratio=0.20,
        entry_ratio=0.25,
        is_real_order=True,
    )

    # ── S 그룹: 손절 실험 ─────────────────────────────────
    stop_configs = [
        ("S1", "손절 -5%",  -5.0),
        ("S2", "손절 -7%",  -7.0),
        ("S3", "손절 -10%", -10.0),
        ("S4", "손절 -12%", -12.0),
        ("S5", "손절 -15%", -15.0),
    ]
    for sid, sname, spct in stop_configs:
        strategies[sid] = StrategyConfig(
            id=sid, name=sname,
            group="STOP", tier=TIER_EXPERIMENT,
            description=f"손절 {spct}% / 트레일링스탑 -15% 고정",
            stop_loss_pct=spct,
            trailing_pct=-15.0,
            trailing_activate=5.0,
            add_buy_levels=[10.0, 20.0, 35.0],
            add_buy_ratio=0.20,
            entry_ratio=0.25,
            is_real_order=False,
        )

    # ── T 그룹: 트레일링스탑 실험 ─────────────────────────
    trail_configs = [
        ("T1", "트레일 -10%", -10.0),
        ("T2", "트레일 -12%", -12.0),
        ("T3", "트레일 -15%", -15.0),
        ("T4", "트레일 -18%", -18.0),
        ("T5", "트레일 -20%", -20.0),
        ("T6", "트레일 -25%", -25.0),
    ]
    for tid, tname, tpct in trail_configs:
        strategies[tid] = StrategyConfig(
            id=tid, name=tname,
            group="TRAIL", tier=TIER_EXPERIMENT,
            description=f"트레일링스탑 {tpct}% / 손절 -10% 고정",
            stop_loss_pct=-10.0,
            trailing_pct=tpct,
            trailing_activate=5.0,
            add_buy_levels=[10.0, 20.0, 35.0],
            add_buy_ratio=0.20,
            entry_ratio=0.25,
            is_real_order=False,
        )

    # ── P 그룹: 추가매수 실험 ─────────────────────────────
    pyramid_configs = [
        ("P1", "추가매수 +10/20/35%", [10.0, 20.0, 35.0], "표준 추가매수"),
        ("P2", "추가매수 +15/30/50%", [15.0, 30.0, 50.0], "공격적 추가매수"),
        ("P3", "추가매수 +20/40/60%", [20.0, 40.0, 60.0], "매우 공격적 추가매수"),
        ("P4", "추가매수 없음",        [],                  "단순 매수·보유"),
    ]
    for pid, pname, plevels, pdesc in pyramid_configs:
        strategies[pid] = StrategyConfig(
            id=pid, name=pname,
            group="PYRAMID", tier=TIER_EXPERIMENT,
            description=pdesc,
            stop_loss_pct=-10.0,
            trailing_pct=-15.0,
            trailing_activate=5.0,
            add_buy_levels=plevels,
            add_buy_ratio=0.20,
            entry_ratio=0.25,
            is_real_order=False,
        )

    return strategies


# 전역 레지스트리 (싱글턴)
STRATEGIES: dict[str, StrategyConfig] = build_strategies()

# 그룹별 ID 목록
GROUP_STOP    = [k for k, v in STRATEGIES.items() if v.group == "STOP"]
GROUP_TRAIL   = [k for k, v in STRATEGIES.items() if v.group == "TRAIL"]
GROUP_PYRAMID = [k for k, v in STRATEGIES.items() if v.group == "PYRAMID"]
GROUP_ALL     = list(STRATEGIES.keys())

# 실험 전략 전용 목록 (실주문 금지 보장)
EXPERIMENT_IDS = [k for k, v in STRATEGIES.items() if not v.is_real_order]

# 시장 국면 정의
REGIME_BULL    = "BULL"     # 상승장
REGIME_BEAR    = "BEAR"     # 하락장
REGIME_LATERAL = "LATERAL"  # 횡보장

def detect_market_regime(index_prices: list[float]) -> str:
    """
    최근 60거래일 지수 가격으로 시장 국면 판단.
    - 60일 수익률 +5% 초과   → 상승장 (BULL)
    - 60일 수익률 -5% 미만   → 하락장 (BEAR)
    - 그 외                  → 횡보장 (LATERAL)
    """
    if len(index_prices) < 20:
        return REGIME_LATERAL
    ret = (index_prices[-1] - index_prices[-min(60, len(index_prices))]) \
          / index_prices[-min(60, len(index_prices))] * 100
    if ret > 5.0:
        return REGIME_BULL
    if ret < -5.0:
        return REGIME_BEAR
    return REGIME_LATERAL


def calc_strategy_score(metrics: dict) -> float:
    """
    전략 종합 점수 (0~100) 계산.
    높을수록 좋음.
    """
    if not metrics or metrics.get("trade_count", 0) < 2:
        return 0.0

    # 수익률 점수 (0~100, 30% 기준 100점)
    ret   = metrics.get("total_return", 0.0)
    s_ret = min(100.0, max(0.0, (ret + 10) / 40 * 100))   # -10%~30% 구간

    # MDD 점수 (0~100, MDD -0%=100, -30%=0)
    mdd   = metrics.get("mdd", -50.0)
    s_mdd = min(100.0, max(0.0, (mdd + 30) / 30 * 100))

    # 샤프 점수 (0~100, 3.0=100점)
    shrp  = metrics.get("sharpe", 0.0)
    s_shrp= min(100.0, max(0.0, shrp / 3.0 * 100))

    # 승률 점수 (0~100)
    wr    = metrics.get("win_rate", 0.0)
    s_wr  = min(100.0, max(0.0, wr))

    # 손익비 점수 (0~100, 3.0=100점)
    pf    = metrics.get("profit_factor", 0.0)
    s_pf  = min(100.0, max(0.0, pf / 3.0 * 100))

    score = (
        s_ret  * SCORE_WEIGHTS["total_return"]
        + s_mdd  * SCORE_WEIGHTS["mdd"]
        + s_shrp * SCORE_WEIGHTS["sharpe"]
        + s_wr   * SCORE_WEIGHTS["win_rate"]
        + s_pf   * SCORE_WEIGHTS["profit_factor"]
    )
    return round(score, 1)


def get_ai_recommendation(strategy_id: str, metrics: dict,
                           regime: str, score: float) -> dict:
    """
    AI 전략 추천 분석 결과 생성.
    Returns: {grade, summary, regime_fit, recommendation}
    """
    if not metrics or metrics.get("trade_count", 0) < 3:
        return {
            "grade": "N/A", "summary": "데이터 부족",
            "regime_fit": "—", "recommendation": "추가 데이터 수집 필요",
        }

    ret  = metrics.get("total_return", 0.0)
    mdd  = metrics.get("mdd", 0.0)
    ret3 = metrics.get("return_3m", 0.0)

    # 추천 등급
    if score >= 80:
        grade = "A"
    elif score >= 65:
        grade = "B"
    elif score >= 50:
        grade = "C"
    elif score >= 35:
        grade = "D"
    else:
        grade = "F"

    # 시장 국면 적합성
    regime_labels = {REGIME_BULL: "상승장", REGIME_BEAR: "하락장", REGIME_LATERAL: "횡보장"}
    regime_kr = regime_labels.get(regime, regime)

    # 전략 그룹별 특성 분석
    cfg = STRATEGIES.get(strategy_id)
    group = cfg.group if cfg else "UNKNOWN"

    if group == "STOP":
        tight = abs(cfg.stop_loss_pct) <= 7
        regime_fit = "하락장" if tight else "상승장"
        strength = "빠른 손절로 하락 방어" if tight else "넓은 손절로 수익 극대화"
    elif group == "TRAIL":
        tight = abs(cfg.trailing_pct) <= 12
        regime_fit = "횡보장" if tight else "상승장"
        strength = "빠른 수익 확정" if tight else "추세 추종 극대화"
    elif group == "PYRAMID":
        if not cfg.add_buy_levels:
            regime_fit = "하락장"
            strength = "단순 보유로 리스크 최소"
        elif cfg.add_buy_levels[0] <= 10:
            regime_fit = "상승장"
            strength = "공격적 추가매수 복리 효과"
        else:
            regime_fit = "횡보장"
            strength = "보수적 추가매수"
    else:
        regime_fit = "—"
        strength = "기준 전략"

    # 성과 요약
    perf = "양호" if ret > 5 else ("적자" if ret < 0 else "보통")
    mdd_str = "양호" if mdd > -10 else ("주의" if mdd > -20 else "위험")

    summary = (
        f"총수익률 {ret:+.1f}% ({perf}) | "
        f"MDD {mdd:.1f}% ({mdd_str}) | "
        f"최근3개월 {ret3:+.1f}%"
    )

    rec_map = {
        "A": "실전 사용 적극 추천",
        "B": "실전 검토 가능",
        "C": "추가 관찰 필요",
        "D": "실전 사용 비추천",
        "F": "즉시 검토·수정 필요",
    }

    return {
        "grade":          grade,
        "summary":        summary,
        "regime_fit":     regime_fit,
        "strength":       strength,
        "current_regime": regime_kr,
        "recommendation": rec_map[grade],
        "score":          score,
    }
