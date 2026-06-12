"""
일일 종목 분석 스케줄러 (오케스트레이터)
==========================================
매일 장마감 후 (기본 16:05) 자동 실행:

흐름:
  1.   전체 종목 목록 수집
  2.   기본 제외 필터 적용 (StockFilter)
  2.5  ETF 목록 수집 및 ETFFilter 적용
  3.   시장 국면(Regime) 판단
  4.   AI 점수화 — 개별주식 (AIScorer)
  4.5  ETF 점수화 (ETFScorer, 국면 반영)
  5.   전체(주식+ETF) 통합 배분 계획 (AssetAllocator)
  6.   등급별 분류 + 감시 목록 정리
       - 전체 감시 200개 (주식)
       - 집중 감시 30개
       - 매수 후보 10개 (주식)
       - ETF 후보 목록 (자산군별)
  7.   DB 저장 + 요약 반환

수동 실행:
  python -m screener.daily_screener
  python -m screener.daily_screener --demo
  python -m screener.daily_screener --demo --code 069500
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
from datetime import datetime, date

from utils.logger import get_logger
from screener.stock_filter import StockFilter, ETFFilter
from screener.ai_scorer    import AIScorer
from screener.screener_db  import (
    init_db, upsert_score, save_summary,
    save_candidates, update_watchlist,
)
from screener.asset_universe import (
    ASSET_STOCK_KOSPI, ASSET_STOCK_KOSDAQ,
    ASSET_ETF_GENERAL, ASSET_ETF_LEVERAGE, ASSET_ETF_INVERSE,
    ASSET_CASH,
    classify_asset_type,
    get_asset_type_label,
)
from screener.asset_allocator import (
    ETFScorer,
    get_allocation_plan,
    AllocationContext,
)

logger = get_logger("DailyScreener")


# ── 시장 국면 판단 헬퍼 ────────────────────────────────────────
def _detect_regime(kospi: dict, kosdaq: dict) -> str:
    """
    KOSPI MA 위치 + 20일 수익률로 시장 국면 판단.

    판단 기준 (AND 조건):
      BULL    : price > ma60 > ma120  AND  return_20d ≥ 2%
      BEAR    : price < ma60 AND return_20d ≤ -2%
      LATERAL : 그 외

    Returns: "BULL" | "LATERAL" | "BEAR"
    """
    price  = kospi.get("price",      2500.0)
    ma60   = kospi.get("ma60",       price)
    ma120  = kospi.get("ma120",      price)
    ret20  = kospi.get("return_20d", 0.0)

    # BULL: 정배열 + 상승 추세
    if price > ma60 and ma60 > ma120 and ret20 >= 2.0:
        return "BULL"
    # BEAR: 역배열 + 하락 추세
    if price < ma60 and ret20 <= -2.0:
        return "BEAR"
    # LATERAL: 그 외 (경계선 포함)
    return "LATERAL"


class DailyScreener:
    """
    매일 장마감 후 전체 종목 + ETF 분석 파이프라인.

    투자 대상:
      - 코스피 / 코스닥 개별주식
      - 일반 ETF / 레버리지 ETF / 인버스 ETF

    국면별 자산 우선순위:
      BULL    → 개별주식 > 레버리지ETF > 일반ETF > 인버스ETF
      LATERAL → 일반ETF > 개별주식 > 인버스ETF  (레버리지 차단)
      BEAR    → 인버스ETF > 현금 > 일반ETF > 개별주식
    """

    def __init__(self, fetcher=None, demo_mode: bool = False):
        # demo_mode 인자는 하위 호환성 유지 — 항상 실전 모드
        self.demo_mode  = False
        self.fetcher    = fetcher
        self.filter     = StockFilter()
        self.etf_filter = ETFFilter()
        self.scorer     = AIScorer()
        self.etf_scorer = ETFScorer()
        init_db()

    def _get_fetcher(self):
        if self.fetcher:
            return self.fetcher
        from screener.kis_data_fetcher import KISDataFetcher
        self.fetcher = KISDataFetcher(demo_mode=False)
        return self.fetcher

    # ══════════════════════════════════════════════════════════
    # 메인 실행
    # ══════════════════════════════════════════════════════════
    def run(self) -> dict:
        """
        전체 분석 파이프라인 실행.
        Returns: 분석 결과 요약 dict (주식 + ETF + 배분계획 포함)
        """
        fetcher   = self._get_fetcher()
        today_str = date.today().isoformat()
        start_t   = datetime.now()

        logger.info(f"{'='*60}")
        logger.info(f"  📊 일일 종목 스크리닝 시작: {today_str}")
        logger.info(f"{'='*60}")

        # ── STEP 1: 시장 지수 수집 ─────────────────────────────
        logger.info("STEP 1: 시장 지수 수집 중...")
        kospi  = fetcher.get_market_index("KOSPI")
        kosdaq = fetcher.get_market_index("KOSDAQ")
        market_info = {
            "return_20d":  min(kospi["return_20d"], kosdaq["return_20d"]),
            "price":       kospi["price"],
            "ma60":        kospi["ma60"],
            "ma120":       kospi["ma120"],
            "fell_today":  kospi["fell_today"] or kosdaq["fell_today"],
        }
        logger.info(
            f"  코스피 {kospi['price']:,.0f}pt ({kospi['return_20d']:+.1f}%) | "
            f"코스닥 {kosdaq['price']:,.0f}pt ({kosdaq['return_20d']:+.1f}%)"
        )

        # ── 시장 국면 판단 (이후 모든 스텝에서 공유) ───────────
        current_regime = _detect_regime(kospi, kosdaq)
        logger.info(f"  📍 시장 국면: {current_regime}")

        # ── STEP 2: 전체 종목 목록 수집 ───────────────────────
        logger.info("STEP 2: 전체 종목 목록 수집 중...")
        raw_list = fetcher.get_stock_list("ALL")
        logger.info(f"  총 {len(raw_list)}개 종목 수집")

        # ── STEP 2.5: ETF 목록 수집 및 ETFFilter ─────────────
        logger.info("STEP 2.5: ETF 목록 수집 및 필터 적용 중...")
        try:
            raw_etf_list = fetcher.get_etf_list()
            etf_passed, etf_excluded = self.etf_filter.batch_filter(raw_etf_list)
            logger.info(
                f"  ETF 수집: {len(raw_etf_list)}개 → "
                f"통과: {len(etf_passed)}개 / 제외: {len(etf_excluded)}개"
            )
        except Exception as e:
            logger.warning(f"  ETF 목록 수집 실패 (계속 진행): {e}")
            raw_etf_list = []
            etf_passed   = []
            etf_excluded = []

        # ── STEP 3: 주식 기본 필터 적용 ────────────────────────
        logger.info("STEP 3: 기본 제외 필터 적용 중...")
        passed, excluded = self.filter.batch_filter(raw_list)
        logger.info(f"  통과: {len(passed)}개 / 제외: {len(excluded)}개")

        # StockFilter 통과 중 asset_type=ETF 태그된 종목 → etf_passed 로 이동
        stock_passed = []
        for s in passed:
            reason = s.get("reason", "")
            atype  = s.get("asset_type", "")
            if reason == "ETF" or atype.startswith("ETF"):
                # ETF 로 이미 판별됨 — etf_passed 에 추가 (중복 방지)
                codes_already = {e["code"] for e in etf_passed}
                if s["code"] not in codes_already:
                    etf_passed.append(s)
            else:
                stock_passed.append(s)
        logger.info(
            f"  주식 통과: {len(stock_passed)}개 | "
            f"ETF(합산): {len(etf_passed)}개"
        )

        # 제외 종목 DB 저장
        for exc in excluded:
            upsert_score(today_str, {
                "code": exc["code"], "name": exc["name"],
                "excluded": True, "exclude_reason": exc["reason"],
                "total_score": 0, "grade": "EXCLUDED",
                "buy_eligible": False, "buy_reason": exc["reason"],
            })

        # ── STEP 4: 주식 AI 점수화 ─────────────────────────────
        logger.info(f"STEP 4: 주식 AI 점수화 시작 ({len(stock_passed)}개)...")
        scored_stocks = []
        errors = 0

        for i, stock in enumerate(stock_passed):
            try:
                code = stock["code"]
                detail_data = fetcher.get_stock_detail(code, market_info)
                if not detail_data:
                    continue

                merged = {
                    **stock, **detail_data,
                    "market_return_20":  market_info["return_20d"],
                    "market_fell_today": market_info["fell_today"],
                    "index_price":       market_info["price"],
                    "index_ma60":        market_info["ma60"],
                    "index_ma120":       market_info["ma120"],
                }

                result = self.scorer.score(merged)
                result["market"]       = stock.get("market", "")
                result["sector"]       = stock.get("sector", "기타")
                result["cur_price"]    = detail_data.get("price_now", 0)
                result["market_cap"]   = stock.get("market_cap", 0)
                result["daily_amount"] = stock.get("daily_amount", 0)
                result["excluded"]     = False

                # 자산군 태그 (KOSPI / KOSDAQ)
                mkt = stock.get("market", "").upper()
                result["asset_type"] = (
                    ASSET_STOCK_KOSPI if mkt in ("KOSPI", "KPI")
                    else ASSET_STOCK_KOSDAQ
                )

                scored_stocks.append(result)
                upsert_score(today_str, result)

                if (i + 1) % 50 == 0:
                    logger.info(f"  진행: {i+1}/{len(stock_passed)}...")

            except Exception as e:
                logger.error(f"  점수화 오류 {stock.get('code','?')}: {e}")
                errors += 1

        logger.info(f"  주식 점수화 완료: {len(scored_stocks)}개 (오류 {errors}개)")

        # ── STEP 4.5: ETF 점수화 (국면 반영) ────────────────────
        logger.info(f"STEP 4.5: ETF 점수화 시작 ({len(etf_passed)}개, 국면={current_regime})...")
        scored_etfs = []
        etf_errors  = 0

        for etf in etf_passed:
            try:
                code = etf["code"]
                name = etf.get("name", code)

                # 자산군 분류 (ETFFilter 통과본은 asset_type 이미 있을 수 있음)
                asset_type = etf.get("asset_type") or \
                             classify_asset_type(code, name, "ETF")

                # ETF 상세 데이터 수집
                detail = fetcher.get_etf_detail(code, asset_type)
                if not detail:
                    continue

                # ETFScorer 점수화
                etf_data = {
                    **etf, **detail,
                    "asset_type": asset_type,
                    "market_return_20":  market_info["return_20d"],
                    "market_fell_today": market_info["fell_today"],
                    "index_price":       market_info["price"],
                    "index_ma60":        market_info["ma60"],
                    "index_ma120":       market_info["ma120"],
                }
                scored = self.etf_scorer.score_etf(etf_data, asset_type, current_regime)
                scored["asset_type"]    = asset_type
                scored["asset_type_kr"] = get_asset_type_label(asset_type)
                scored["excluded"]      = False
                scored["is_etf"]        = True
                scored["market"]        = "ETF"
                scored["sector"]        = asset_type
                scored["cur_price"]     = detail.get("price_now", 0)
                scored["daily_amount"]  = etf.get("daily_amount", 0)

                scored_etfs.append(scored)

            except Exception as e:
                logger.error(f"  ETF 점수화 오류 {etf.get('code','?')}: {e}")
                etf_errors += 1

        # ETF도 점수 내림차순 정렬
        scored_etfs.sort(key=lambda x: x.get("total_score", 0), reverse=True)
        logger.info(
            f"  ETF 점수화 완료: {len(scored_etfs)}개 "
            f"(오류 {etf_errors}개)"
        )

        # ── STEP 5: 등급 분류 (주식) ───────────────────────────
        logger.info("STEP 5: 등급 분류 중...")
        scored_stocks.sort(key=lambda x: x["total_score"], reverse=True)

        by_grade = {
            "BUY_CANDIDATE": [], "WATCH_HIGH": [], "WATCH": [],
            "HOLD_ONLY": [], "EXCLUDE": [],
        }
        for r in scored_stocks:
            g = r.get("grade", "EXCLUDE")
            by_grade.setdefault(g, []).append(r)

        # ── STEP 5.5: 통합 배분 계획 ──────────────────────────
        logger.info("STEP 5.5: 자산군 통합 배분 계획 수립 중...")
        try:
            # 전체 스크리닝 결과 통합 (주식 + ETF)
            all_screened = scored_stocks + scored_etfs

            # 배분 계획 실행 (기본 자본 1,000만원, 전액 현금 → 신규 배분 시뮬)
            allocation_plan = get_allocation_plan(
                screened      = all_screened,
                regime        = current_regime,
                total_capital = 10_000_000,
                cash          = 10_000_000,  # 100% 현금 상태에서 계획
                positions     = {},
            )
            buyable   = [d for d in allocation_plan.buy_list if d.can_buy]
            not_buyable = [d for d in allocation_plan.buy_list if not d.can_buy]
            alloc_summary = {
                "regime":         allocation_plan.regime,
                "buyable_count":  len(buyable),
                "blocked_count":  len(not_buyable),
                "target_weights": {
                    k: round(v, 4)
                    for k, v in allocation_plan.target_weights.items()
                },
                "warnings":       allocation_plan.warnings,
                "rebalance_needed": allocation_plan.rebalance_needed,
            }
            logger.info(
                f"  배분 계획: 매수가능 {alloc_summary['buyable_count']}개 | "
                f"차단 {alloc_summary['blocked_count']}개"
            )
        except Exception as e:
            logger.warning(f"  배분 계획 수립 실패 (계속 진행): {e}")
            allocation_plan = None
            alloc_summary   = {"regime": current_regime, "error": str(e)}

        # ── STEP 6: 감시 목록 정리 ─────────────────────────────
        logger.info("STEP 6: 감시 목록 정리 중...")

        # 주식 감시 목록 (기존과 동일)
        watchlist_200 = scored_stocks[:200]
        focus_30 = (by_grade["BUY_CANDIDATE"] + by_grade["WATCH_HIGH"])[:30]
        candidates_10 = [r for r in scored_stocks if r.get("buy_eligible")][:10]

        update_watchlist(watchlist_200, focus_30)
        save_candidates(today_str, candidates_10)

        # ETF 후보 목록 — 자산군별 상위 분리
        etf_candidates_by_type: dict[str, list] = {
            ASSET_ETF_GENERAL:   [],
            ASSET_ETF_LEVERAGE:  [],
            ASSET_ETF_INVERSE:   [],
        }
        for e in scored_etfs:
            atype = e.get("asset_type", ASSET_ETF_GENERAL)
            if atype in etf_candidates_by_type:
                etf_candidates_by_type[atype].append(e)

        # 배분 계획에서 ETF 매수 후보 추출
        etf_buy_candidates = []
        if allocation_plan:
            for bd in allocation_plan.buy_list:
                if bd.can_buy and bd.asset_type in (
                    ASSET_ETF_GENERAL, ASSET_ETF_LEVERAGE, ASSET_ETF_INVERSE
                ):
                    etf_buy_candidates.append({
                        "code":            bd.code,
                        "name":            bd.name,
                        "asset_type":      bd.asset_type,
                        "asset_type_kr":   get_asset_type_label(bd.asset_type),
                        "ai_score":        round(bd.ai_score, 1),
                        "rs_value":        round(bd.rs_value, 2),
                        "suggested_ratio": round(bd.suggested_ratio, 4),
                        "max_amount":      round(bd.max_amount, 0),
                        "can_buy":         bd.can_buy,
                        "reason":          bd.reason,
                    })

        logger.info(
            f"  주식 후보: {len(candidates_10)}개 | "
            f"ETF 후보: {len(etf_buy_candidates)}개 "
            f"(일반{len(etf_candidates_by_type[ASSET_ETF_GENERAL])} | "
            f"레버리지{len(etf_candidates_by_type[ASSET_ETF_LEVERAGE])} | "
            f"인버스{len(etf_candidates_by_type[ASSET_ETF_INVERSE])})"
        )

        # 자산군별 ETF 등급 분류
        etf_by_grade: dict[str, list] = {
            "BUY_CANDIDATE": [], "WATCH_HIGH": [], "WATCH": [], "EXCLUDE": [],
        }
        for e in scored_etfs:
            g = e.get("grade", "WATCH")
            etf_by_grade.setdefault(g, []).append(e)

        # ── STEP 7: 요약 저장 ─────────────────────────────────
        logger.info("STEP 7: 요약 저장 중...")

        # 자산군별 통계
        asset_type_breakdown = {
            "STOCK_KOSPI":   len([s for s in scored_stocks
                                  if s.get("asset_type") == ASSET_STOCK_KOSPI]),
            "STOCK_KOSDAQ":  len([s for s in scored_stocks
                                  if s.get("asset_type") == ASSET_STOCK_KOSDAQ]),
            "ETF_GENERAL":   len(etf_candidates_by_type[ASSET_ETF_GENERAL]),
            "ETF_LEVERAGE":  len(etf_candidates_by_type[ASSET_ETF_LEVERAGE]),
            "ETF_INVERSE":   len(etf_candidates_by_type[ASSET_ETF_INVERSE]),
        }

        summary = {
            # 주식 분석 통계
            "total_analyzed":    len(scored_stocks),
            "total_excluded":    len(excluded) + errors,
            "buy_candidate":     len(by_grade["BUY_CANDIDATE"]),
            "watch_high":        len(by_grade["WATCH_HIGH"]),
            "watch":             len(by_grade["WATCH"]),
            "hold_only":         len(by_grade["HOLD_ONLY"]),
            "exclude":           len(by_grade["EXCLUDE"]),
            # 시장 지수
            "market_kospi_ret":  kospi["return_20d"],
            "market_kosdaq_ret": kosdaq["return_20d"],
            # 국면 정보
            "regime":            current_regime,
            # ETF 분석 통계
            "etf_total":         len(scored_etfs),
            "etf_excluded":      len(etf_excluded) + etf_errors,
            "etf_buy_candidate": len(etf_by_grade["BUY_CANDIDATE"]),
            "etf_watch_high":    len(etf_by_grade["WATCH_HIGH"]),
            # 자산군 분포
            "asset_type_breakdown": asset_type_breakdown,
            # 배분 계획 요약
            "allocation": alloc_summary,
        }
        save_summary(today_str, summary)

        elapsed = (datetime.now() - start_t).total_seconds()
        logger.info(f"{'='*60}")
        logger.info(f"  ✅ 스크리닝 완료! ({elapsed:.1f}초)")
        logger.info(f"  📊 국면: {current_regime}")
        logger.info(f"  주식: 분석 {summary['total_analyzed']}개 | 제외 {summary['total_excluded']}개")
        logger.info(f"  ETF:  분석 {summary['etf_total']}개   | 제외 {summary['etf_excluded']}개")
        logger.info(f"  BUY_CANDIDATE(주식): {summary['buy_candidate']}개")
        logger.info(f"  BUY_CANDIDATE(ETF):  {summary['etf_buy_candidate']}개")
        logger.info(f"  📋 배분 계획: 매수가능 {alloc_summary.get('buyable_count',0)}개")
        if candidates_10:
            logger.info(f"  📈 주식 매수 후보 Top5:")
            for i, c in enumerate(candidates_10[:5], 1):
                logger.info(
                    f"    {i}. {c['name']}({c['code']}) "
                    f"점수={c['total_score']:.0f} "
                    f"RS={c.get('rs_value',0):+.1f}% [{c['grade']}]"
                )
        if etf_buy_candidates:
            logger.info(f"  📦 ETF 매수 후보:")
            for e in etf_buy_candidates[:5]:
                logger.info(
                    f"    · {e['name']}({e['code']}) "
                    f"점수={e['ai_score']:.0f} "
                    f"[{e['asset_type_kr']}] "
                    f"비중={e['suggested_ratio']*100:.1f}%"
                )
        logger.info(f"{'='*60}")

        return {
            "date":            today_str,
            "summary":         summary,
            "regime":          current_regime,
            # 주식
            "candidates_10":   candidates_10,
            "focus_30":        focus_30[:30],
            "watchlist_200":   watchlist_200[:200],
            # ETF
            "etf_candidates":  etf_buy_candidates,
            "etf_scored":      scored_etfs,
            "etf_by_type":     {
                k: v[:10] for k, v in etf_candidates_by_type.items()
            },
            # 배분 계획
            "allocation_plan": alloc_summary,
            "allocation_plan_detail": (
                {
                    "buy_list": [
                        {
                            "code":            bd.code,
                            "name":            bd.name,
                            "asset_type":      bd.asset_type,
                            "asset_type_kr":   get_asset_type_label(bd.asset_type),
                            "ai_score":        round(bd.ai_score, 1),
                            "rs_value":        round(bd.rs_value, 2),
                            "can_buy":         bd.can_buy,
                            "reason":          bd.reason,
                            "suggested_ratio": round(bd.suggested_ratio, 4),
                            "max_amount":      round(bd.max_amount, 0),
                            "priority_rank":   bd.priority_rank,
                        }
                        for bd in allocation_plan.buy_list
                        if bd.can_buy
                    ],
                    "blocked_list": [
                        {
                            "code":       bd.code,
                            "name":       bd.name,
                            "asset_type": bd.asset_type,
                            "reason":     bd.reason,
                            "ai_score":   round(bd.ai_score, 1),
                        }
                        for bd in allocation_plan.buy_list
                        if not bd.can_buy
                    ],
                    "warnings":        allocation_plan.warnings,
                    "target_weights":  allocation_plan.target_weights,
                    "current_weights": allocation_plan.current_weights,
                    "summary":         allocation_plan.summary,
                }
                if allocation_plan else {}
            ),
            # 시장 지수
            "kospi":           kospi,
            "kosdaq":          kosdaq,
            "elapsed_sec":     round(elapsed, 1),
        }

    # ══════════════════════════════════════════════════════════
    # 단일 종목 실시간 재분석
    # ══════════════════════════════════════════════════════════
    def analyze_single(self, code: str, name: str = "") -> dict:
        """
        단일 종목/ETF 즉시 분석 (장중 실시간 재평가용).
        ETF 코드 입력 시 ETFScorer 로 점수화.
        """
        fetcher = self._get_fetcher()
        kospi   = fetcher.get_market_index("KOSPI")
        market_info = {
            "return_20d":  kospi["return_20d"],
            "price":       kospi["price"],
            "ma60":        kospi["ma60"],
            "ma120":       kospi["ma120"],
            "fell_today":  kospi["fell_today"],
        }
        current_regime = _detect_regime(kospi, kospi)

        # ETF 여부 판별
        resolved_name = name or code
        asset_type = classify_asset_type(code, resolved_name, "")

        if asset_type.startswith("ETF"):
            # ETF 점수화 경로
            detail = fetcher.get_etf_detail(code, asset_type)
            if not detail:
                return {"error": f"ETF 데이터 없음: {code}"}
            etf_data = {
                **detail,
                "code": code,
                "name": resolved_name,
                "asset_type": asset_type,
                "market_return_20":  market_info["return_20d"],
                "market_fell_today": market_info["fell_today"],
                "index_price":       market_info["price"],
                "index_ma60":        market_info["ma60"],
                "index_ma120":       market_info["ma120"],
            }
            result = self.etf_scorer.score_etf(etf_data, asset_type, current_regime)
            result["asset_type"]    = asset_type
            result["asset_type_kr"] = get_asset_type_label(asset_type)
            result["cur_price"]     = detail.get("price_now", 0)
            result["is_etf"]        = True
            result["regime"]        = current_regime
            result["market_info"]   = {"kospi": kospi}
            return result
        else:
            # 주식 점수화 경로 (기존 동일)
            detail = fetcher.get_stock_detail(code, market_info)
            if not detail:
                return {"error": f"데이터 없음: {code}"}
            detail["code"]   = code
            detail["name"]   = resolved_name
            result = self.scorer.score(detail)
            result["cur_price"]    = detail.get("price_now", 0)
            result["asset_type"]   = asset_type
            result["regime"]       = current_regime
            result["market_info"]  = {"kospi": kospi}
            return result


# ── CLI 직접 실행 ────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="일일 종목 스크리닝 (주식+ETF)")
    parser.add_argument("--demo",   action="store_true", help="데모 모드 (KIS API 없이)")
    parser.add_argument("--code",   type=str,            help="단일 종목/ETF 분석")
    parser.add_argument("--name",   type=str, default="",help="종목명 (단일 분석 시 선택)")
    parser.add_argument("--etf",    action="store_true", help="ETF 목록만 출력")
    args = parser.parse_args()

    screener = DailyScreener(demo_mode=args.demo or True)

    if args.code:
        result = screener.analyze_single(args.code, args.name)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.etf:
        fetcher = screener._get_fetcher()
        etf_list = fetcher.get_etf_list()
        passed, excluded = screener.etf_filter.batch_filter(etf_list)
        print(f"\n=== ETF 목록 (통과: {len(passed)}개 / 제외: {len(excluded)}개) ===")
        for e in passed[:20]:
            atype = e.get("asset_type", "?")
            print(f"  {e['code']} {e.get('name',''):20s} [{atype}]")

    else:
        result = screener.run()
        s = result["summary"]
        print(f"\n{'='*60}")
        print(f"  분석 완료 | 국면: {result['regime']}")
        print(f"{'='*60}")
        print(f"  주식: {s['total_analyzed']}개 분석 | {s['buy_candidate']}개 BUY_CANDIDATE")
        print(f"  ETF:  {s['etf_total']}개 분석   | {s['etf_buy_candidate']}개 BUY_CANDIDATE")
        print(f"  배분 계획: 매수가능 {s['allocation'].get('buyable_count',0)}개")

        print(f"\n  📈 주식 매수 후보 Top 10:")
        for i, c in enumerate(result["candidates_10"], 1):
            print(
                f"  {i:2d}. {c['name']:12s} "
                f"점수={c['total_score']:.0f} "
                f"RS={c.get('rs_value',0):+5.1f}% [{c['grade']}]"
            )

        print(f"\n  📦 ETF 매수 후보:")
        for e in result["etf_candidates"]:
            print(
                f"    · {e['name']:20s} ({e['code']}) "
                f"점수={e['ai_score']:.0f} "
                f"[{e['asset_type_kr']}] "
                f"비중={e['suggested_ratio']*100:.1f}%"
            )

        if result.get("allocation_plan_detail"):
            tw = result["allocation_plan_detail"].get("target_weights", {})
            print(f"\n  🎯 자산군 타겟 비중:")
            for atype, wt in tw.items():
                label = get_asset_type_label(atype)
                print(f"    {label:12s}: {wt*100:.1f}%")
