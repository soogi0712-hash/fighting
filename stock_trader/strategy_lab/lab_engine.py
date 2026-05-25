"""
전략 실험실 엔진 — 오케스트레이터
=====================================

★ 역할 ★
  1. 모든 실험 전략의 Shadow Portfolio 관리
  2. 매매 신호 수신 시 → 모든 실험 전략에 동시 가상 적용
  3. 시장 국면(BULL/BEAR/LATERAL) 감지
  4. 매주 전략 랭킹 자동 계산
  5. 승격/강등 규칙 자동 적용
  6. AI 추천 생성

★ 핵심 철학 ★
  - 목표: 장기 CAGR 최대화
  - 제약: 계좌 파산 확률 최소화
  - 우선순위: 1.생존 > 2.복리성장 > 3.수익률 극대화
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import random
from datetime import datetime, date, timedelta
from typing import Optional

from utils.logger import get_logger
from strategy_lab.lab_config import (
    STRATEGIES, TIER_LIVE, TIER_CANDIDATE, TIER_EXPERIMENT,
    PROMOTION_TO_CANDIDATE, PROMOTION_TO_LIVE,
    detect_market_regime, calc_strategy_score, get_ai_recommendation,
    EXPERIMENT_IDS,
)
from strategy_lab.shadow_portfolio import ShadowPortfolio

logger = get_logger("StrategyLab")

# DB 연동 (import 실패 시 graceful fallback)
try:
    from strategy_lab import lab_db as _lab_db
    _DB_ENABLED = True
except Exception as _e:
    _lab_db = None
    _DB_ENABLED = False
    logger.warning(f"[StrategyLab] lab_db import 실패 (DB 연동 비활성): {_e}")

# 영속 저장 경로
LAB_STATE_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "lab_state.json"
)
LAB_RANKING_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "lab_ranking.json"
)


class StrategyLabEngine:
    """
    전략 실험실 오케스트레이터.
    실험 전략들의 Shadow Portfolio 를 동시에 관리한다.
    """

    def __init__(self):
        # 전략별 Shadow Portfolio
        self.portfolios: dict[str, ShadowPortfolio] = {}
        # 전략 계층 (STRATEGIES 에서 복사, 동적 변경 가능)
        self.tiers: dict[str, str] = {k: v.tier for k, v in STRATEGIES.items()}
        # 주간 랭킹 캐시
        self.last_ranking: list[dict] = []
        # 시장 국면
        self.market_regime: str = "LATERAL"
        # 지수 가격 이력 (KOSPI)
        self._index_prices: list[float] = []
        # 마지막 랭킹 날짜
        self._last_rank_date: str = ""

        os.makedirs(os.path.dirname(LAB_STATE_FILE), exist_ok=True)
        self._init_portfolios()
        self._load_state()

    # ── 초기화 ───────────────────────────────────────────
    def _init_portfolios(self):
        """실험/후보 전략 Portfolio 초기화"""
        for sid, cfg in STRATEGIES.items():
            if not cfg.is_real_order:   # LIVE 전략은 Shadow Portfolio 불필요
                self.portfolios[sid] = ShadowPortfolio(cfg)
        logger.info(f"[StrategyLab] 초기화: {len(self.portfolios)}개 실험 전략 포트폴리오 생성")

    # ── 상태 저장/로드 ───────────────────────────────────
    def _save_state(self):
        state = {
            "tiers":          self.tiers,
            "market_regime":  self.market_regime,
            "last_rank_date": self._last_rank_date,
            "portfolios": {
                sid: pf.to_dict()
                for sid, pf in self.portfolios.items()
            },
        }
        with open(LAB_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def _load_state(self):
        if not os.path.exists(LAB_STATE_FILE):
            return
        try:
            with open(LAB_STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)

            self.tiers         = state.get("tiers", self.tiers)
            self.market_regime = state.get("market_regime", "LATERAL")
            self._last_rank_date = state.get("last_rank_date", "")

            for sid, pf_data in state.get("portfolios", {}).items():
                if sid in STRATEGIES and sid in self.portfolios:
                    self.portfolios[sid] = ShadowPortfolio.from_dict(
                        pf_data, STRATEGIES[sid]
                    )
            logger.info("[StrategyLab] 상태 로드 완료")
        except Exception as e:
            logger.error(f"[StrategyLab] 상태 로드 실패: {e}")

    # ══════════════════════════════════════════════════════
    # 핵심 API: 매매 신호 처리
    # ══════════════════════════════════════════════════════

    # ── DB 헬퍼 ─────────────────────────────────────────────
    def _db_trade(self, sid: str, code: str, name: str,
                  action: str, price: float, qty: int,
                  date_str: str, profit: float = 0.0,
                  profit_pct: float = 0.0, hold_days: int = 0,
                  reason: str = ""):
        """가상 거래 DB 기록 (실패 시 무시)"""
        if not _DB_ENABLED:
            return
        try:
            _lab_db.insert_trade(
                strategy_id=sid, code=code, name=name,
                action=action, price=price, qty=qty,
                trade_date=date_str, profit=profit,
                profit_pct=profit_pct, hold_days=hold_days,
                reason=reason,
            )
        except Exception as e:
            logger.debug(f"[StrategyLab] DB 거래 기록 실패: {e}")

    def on_buy_signal(self, code: str, name: str,
                      price: float,
                      date_str: Optional[str] = None) -> dict:
        """
        매수 신호 발생 시 — 모든 실험 전략에 가상 매수 적용.
        실전 전략은 별도 처리 (이 메서드에서 제외).
        """
        today = date_str or date.today().isoformat()
        results = {}
        for sid, pf in self.portfolios.items():
            r = pf.virtual_buy(code, name, price, today,
                               reason=f"매수신호 [{sid}]")
            results[sid] = r
            if r["ok"]:
                logger.debug(f"[{sid}] 가상매수 {name} {r['qty']}주 @{price:,}")
                self._db_trade(sid, code, name, "BUY", price,
                               r.get("qty", 0), today, reason=f"매수신호")
        self._save_state()
        return results

    def on_price_tick(self, code: str, name: str,
                      price: float,
                      date_str: Optional[str] = None) -> dict:
        """
        가격 업데이트 시 — 모든 실험 전략 포지션 청산 조건 확인.
        추가매수 조건도 동시 확인.
        반환: {sid: {action: "SELL"/"ADD_BUY"/"HOLD", ...}}
        """
        today = date_str or date.today().isoformat()
        actions = {}

        for sid, pf in self.portfolios.items():
            if code not in pf.positions:
                continue

            # 추가매수 확인 (청산 전에 먼저)
            add_result = pf.virtual_add_buy(code, price, today)
            if add_result.get("ok"):
                self._db_trade(sid, code, name, "ADD_BUY", price,
                               add_result.get("qty", 0), today,
                               reason=f"+{add_result.get('level_pct',0):.0f}% 추가매수")
                actions[sid] = {
                    "action": "ADD_BUY",
                    "level_pct": add_result.get("level_pct"),
                    "qty": add_result.get("qty"),
                }
                continue

            # 청산 조건 확인
            exit_signal = pf.check_exit(code, price, today)
            if exit_signal:
                sell_result = pf.virtual_sell(
                    code, price, today,
                    reason=exit_signal["reason"]
                )
                profit     = sell_result.get("profit", 0)
                profit_pct = sell_result.get("profit_pct", 0)
                hold_days  = sell_result.get("hold_days", 0)
                self._db_trade(
                    sid, code, name, "SELL", price,
                    sell_result.get("qty", 0), today,
                    profit=profit, profit_pct=profit_pct,
                    hold_days=hold_days, reason=exit_signal["reason"],
                )
                actions[sid] = {
                    "action": exit_signal["action"],
                    "profit": profit,
                    "hold_days": hold_days,
                    "reason": exit_signal["reason"],
                }
            else:
                pos = pf.positions[code]
                actions[sid] = {
                    "action": "HOLD",
                    "unrealized_pct": pos.unrealized_pct(price),
                }

        if any(v.get("action") != "HOLD" for v in actions.values()):
            self._save_state()

        return actions

    def on_sell_signal(self, code: str, price: float,
                       date_str: Optional[str] = None,
                       reason: str = "매도신호") -> dict:
        """
        강제 매도 신호 — 실전 전략과 동일 시점에 모든 가상 포지션 청산.
        """
        today = date_str or date.today().isoformat()
        results = {}
        for sid, pf in self.portfolios.items():
            if code in pf.positions:
                pos = pf.positions[code]
                name_cached = pos.name
                r = pf.virtual_sell(code, price, today, reason=reason)
                results[sid] = r
                self._db_trade(
                    sid, code, name_cached, "SELL", price,
                    r.get("qty", 0), today,
                    profit=r.get("profit", 0),
                    profit_pct=r.get("profit_pct", 0),
                    hold_days=r.get("hold_days", 0),
                    reason=reason,
                )
        if results:
            self._save_state()
        return results

    def update_index(self, index_price: float):
        """
        지수 가격 업데이트 → 시장 국면 자동 감지.
        """
        self._index_prices.append(index_price)
        if len(self._index_prices) > 130:
            self._index_prices = self._index_prices[-130:]
        self.market_regime = detect_market_regime(self._index_prices)

    def snapshot_all(self, price_map: dict[str, float],
                     snap_date: Optional[str] = None):
        """
        일별 자산 스냅샷 저장 (equity_curve 업데이트 + DB 기록).
        price_map: {code: price}
        """
        today = snap_date or date.today().isoformat()
        for sid, pf in self.portfolios.items():
            eq = pf.snapshot_equity(price_map)
            # DB 저장
            if _DB_ENABLED and eq is not None:
                try:
                    _lab_db.upsert_equity(
                        strategy_id  = sid,
                        snap_date    = today,
                        equity       = eq,
                        cash         = pf.capital,
                        position_cnt = len(pf.positions),
                    )
                except Exception as e:
                    logger.debug(f"[StrategyLab] equity DB 저장 실패: {e}")

    # ══════════════════════════════════════════════════════
    # 전략 랭킹
    # ══════════════════════════════════════════════════════

    def calc_ranking(self) -> list[dict]:
        """
        모든 실험 전략 성과 계산 → 점수 내림차순 랭킹 반환.
        매주 월요일 자동 호출 (APScheduler).
        """
        ranking = []

        for sid, pf in self.portfolios.items():
            cfg     = STRATEGIES[sid]
            metrics = pf.get_metrics()
            score   = calc_strategy_score(metrics)
            ai_rec  = get_ai_recommendation(sid, metrics, self.market_regime, score)

            ranking.append({
                "rank":          0,       # 나중에 채움
                "strategy_id":   sid,
                "name":          cfg.name,
                "group":         cfg.group,
                "tier":          self.tiers.get(sid, cfg.tier),
                "score":         score,
                "metrics":       metrics,
                "ai":            ai_rec,
                "stop_loss_pct": cfg.stop_loss_pct,
                "trailing_pct":  cfg.trailing_pct,
                "add_buy_levels":cfg.add_buy_levels,
                "calc_date":     date.today().isoformat(),
            })

        # 점수 내림차순 정렬 후 순위 부여
        ranking.sort(key=lambda x: x["score"], reverse=True)
        for i, r in enumerate(ranking, 1):
            r["rank"] = i

        self.last_ranking    = ranking
        self._last_rank_date = date.today().isoformat()

        # 파일 저장
        with open(LAB_RANKING_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "calc_date": self._last_rank_date,
                "regime":    self.market_regime,
                "ranking":   ranking,
            }, f, ensure_ascii=False, indent=2)

        logger.info(
            f"[StrategyLab] 랭킹 계산 완료: {len(ranking)}개 전략 | "
            f"시장국면={self.market_regime}"
        )
        if ranking:
            top = ranking[0]
            logger.info(f"  🥇 1위: {top['name']} ({top['score']:.1f}점)")

        self._apply_promotion_rules(ranking)
        return ranking

    # ══════════════════════════════════════════════════════
    # 승격/강등 규칙
    # ══════════════════════════════════════════════════════

    def _apply_promotion_rules(self, ranking: list[dict]):
        """
        EXPERIMENT → CANDIDATE → LIVE 승격 자동 적용.
        """
        today = date.today().isoformat()

        for r in ranking:
            sid     = r["strategy_id"]
            tier    = self.tiers.get(sid, TIER_EXPERIMENT)
            metrics = r["metrics"]
            cfg     = STRATEGIES.get(sid)
            if cfg is None or cfg.is_real_order:
                continue

            # EXPERIMENT → CANDIDATE 승격
            if tier == TIER_EXPERIMENT:
                if self._can_promote_to_candidate(metrics):
                    self.tiers[sid] = TIER_CANDIDATE
                    STRATEGIES[sid].candidate_since = today
                    logger.info(
                        f"⬆ [{sid}] CANDIDATE 승격: "
                        f"return={metrics.get('return_3m',0):.1f}% "
                        f"MDD={metrics.get('mdd',0):.1f}% "
                        f"trades={metrics.get('trade_count',0)}"
                    )

            # CANDIDATE → LIVE 승격 (운영자 수동 확인 필요 — 플래그만 설정)
            elif tier == TIER_CANDIDATE:
                if self._can_promote_to_live(metrics, cfg):
                    # 실제 LIVE 승격은 운영자가 수동 확인 후 결정
                    logger.info(
                        f"🏆 [{sid}] LIVE 승격 조건 충족 — 운영자 승인 대기 중"
                    )
                    # 플래그 설정 (UI에서 표시)
                    r["promotion_ready"] = True

        self._save_state()

    def _can_promote_to_candidate(self, metrics: dict) -> bool:
        p = PROMOTION_TO_CANDIDATE
        return (
            metrics.get("return_3m", -999) > p["min_return_3m"]
            and metrics.get("mdd", -999) > p["max_mdd"]
            and metrics.get("trade_count", 0) >= p["min_trades"]
            and metrics.get("sharpe", 0) >= p["min_sharpe"]
        )

    def _can_promote_to_live(self, metrics: dict, cfg) -> bool:
        p = PROMOTION_TO_LIVE
        if not cfg.candidate_since:
            return False
        try:
            cand_days = (date.today() - date.fromisoformat(cfg.candidate_since)).days
        except Exception:
            cand_days = 0
        return (
            cand_days >= p["min_candidate_days"]
            and metrics.get("total_return", 0) >= p["min_return_pct"]
            and metrics.get("mdd", -999) > p["max_mdd"]
            and metrics.get("win_rate", 0) >= p["min_win_rate"]
        )

    # ══════════════════════════════════════════════════════
    # 조회 API
    # ══════════════════════════════════════════════════════

    def get_all_status(self) -> dict:
        """모든 전략 현황 반환 (대시보드용)"""
        result = {}
        for sid, pf in self.portfolios.items():
            cfg = STRATEGIES[sid]
            result[sid] = {
                "strategy_id":    sid,
                "name":           cfg.name,
                "group":          cfg.group,
                "tier":           self.tiers.get(sid, cfg.tier),
                "capital":        round(pf.capital, 0),
                "position_count": len(pf.positions),
                "trade_count":    len([t for t in pf.trades if t.action == "SELL"]),
                "equity":         pf.equity_curve[-1] if pf.equity_curve else pf.INITIAL_CAPITAL,
                "total_return":   round(
                    (pf.equity_curve[-1] / pf.INITIAL_CAPITAL - 1) * 100, 2
                ) if pf.equity_curve else 0.0,
                "positions":      {
                    k: {
                        "name":           v.name,
                        "avg_price":      round(v.avg_price, 0),
                        "qty":            v.qty,
                        "entry_date":     v.entry_date,
                        "add_buy_done":   v.add_buy_done,
                    }
                    for k, v in pf.positions.items()
                },
            }
        return result

    def get_ranking(self) -> list[dict]:
        """랭킹 반환 (캐시 또는 파일)"""
        if self.last_ranking:
            return self.last_ranking
        if os.path.exists(LAB_RANKING_FILE):
            try:
                with open(LAB_RANKING_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                self.last_ranking = data.get("ranking", [])
                return self.last_ranking
            except Exception:
                pass
        return []

    def get_strategy_detail(self, sid: str) -> dict:
        """단일 전략 상세 (메트릭 + 포지션 + 거래내역)"""
        pf = self.portfolios.get(sid)
        if not pf:
            return {}
        cfg = STRATEGIES[sid]
        metrics = pf.get_metrics()
        score   = calc_strategy_score(metrics)
        return {
            "strategy_id":   sid,
            "name":          cfg.name,
            "group":         cfg.group,
            "tier":          self.tiers.get(sid, cfg.tier),
            "description":   cfg.description,
            "config": {
                "stop_loss_pct":     cfg.stop_loss_pct,
                "trailing_pct":      cfg.trailing_pct,
                "trailing_activate": cfg.trailing_activate,
                "add_buy_levels":    cfg.add_buy_levels,
                "entry_ratio":       cfg.entry_ratio,
            },
            "metrics":       metrics,
            "score":         score,
            "ai":            get_ai_recommendation(sid, metrics, self.market_regime, score),
            "positions":     {k: v.to_dict() for k, v in pf.positions.items()},
            "trades":        [t.to_dict() for t in pf.trades[-50:]],
            "equity_curve":  pf.equity_curve[-120:],
        }

    def get_market_regime(self) -> str:
        return self.market_regime

    def get_regime_analysis(self) -> dict:
        """시장 국면별 전략 성과 비교"""
        result = {}
        for sid, pf in self.portfolios.items():
            cfg = STRATEGIES[sid]
            metrics = pf.get_metrics()
            result[sid] = {
                "name":  cfg.name,
                "group": cfg.group,
                "return": metrics.get("total_return", 0),
                "mdd":    metrics.get("mdd", 0),
                "sharpe": metrics.get("sharpe", 0),
            }
        return {
            "current_regime": self.market_regime,
            "strategies":     result,
        }

    # ══════════════════════════════════════════════════════
    # 데모 시뮬레이션
    # ══════════════════════════════════════════════════════

    def run_demo_simulation(self, days: int = 60) -> dict:
        """
        데모 모드: 가상 가격으로 모든 전략을 시뮬레이션한다.
        실계좌와 완전 무관한 순수 테스트용.
        """
        logger.info(f"[StrategyLab] 데모 시뮬레이션 시작: {days}일")

        # 가상 종목 3개
        stocks = [
            {"code": "005930", "name": "삼성전자",   "base": 75000},
            {"code": "000660", "name": "SK하이닉스", "base": 180000},
            {"code": "035420", "name": "NAVER",      "base": 210000},
        ]

        rng = random.Random(42)
        today = date.today()

        # 가상 가격 생성 (GBM 모델)
        def gen_prices(base, n, mu=0.0003, sigma=0.018):
            prices = [base]
            for _ in range(n):
                ret = rng.gauss(mu, sigma)
                prices.append(round(prices[-1] * (1 + ret)))
            return prices

        all_prices = {
            s["code"]: gen_prices(s["base"], days)
            for s in stocks
        }

        # 날짜별 시뮬레이션
        for day_i in range(1, days + 1):
            d_str = (today - timedelta(days=days - day_i)).isoformat()
            price_map = {s["code"]: all_prices[s["code"]][day_i] for s in stocks}

            # 인덱스 업데이트
            self.update_index(price_map["005930"] * 36)  # 삼성전자 기반 지수 근사

            # 매수 조건: 20일 MA 돌파 (간단 근사)
            for s in stocks:
                code  = s["code"]
                name  = s["name"]
                price = all_prices[code][day_i]
                prev  = all_prices[code][max(0, day_i - 1)]

                # 5일 평균 계산
                window = all_prices[code][max(0, day_i-5):day_i+1]
                ma5    = sum(window) / len(window)

                # 매수: 현재가 > MA5 이고 포지션 없을 때
                if price > ma5 and price > prev * 1.01:
                    self.on_buy_signal(code, name, price, d_str)

                # 가격 틱 처리 (청산 조건)
                self.on_price_tick(code, name, price, d_str)

            # 일별 자산 스냅샷
            self.snapshot_all(price_map)

        # 랭킹 계산
        ranking = self.calc_ranking()
        self._save_state()

        logger.info(f"[StrategyLab] 데모 시뮬레이션 완료: {days}일")
        return {
            "days":    days,
            "ranking": ranking[:5],
            "regime":  self.market_regime,
        }
