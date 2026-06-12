"""
KIS API 확장 — 스크리너 전용 데이터 수집
==========================================
기존 kis_api.py를 상속하여 스크리너에 필요한
추가 API 호출 메서드를 제공한다.

주요 추가 메서드:
  get_market_index()      — 코스피/코스닥 지수 + MA
  get_stock_list()        — 전체 종목 기본 정보 목록
  get_stock_detail()      — 종목 상세 (시총, 거래대금, 재무지표)
  get_investor_trend()    — 기관/외인 순매수 동향
  get_52w_high_low()      — 52주(60일) 신고가·신저가
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timedelta
import requests
from api.kis_api import KISApi
from utils.logger import get_logger

logger = get_logger("KISDataFetcher")

# 더미 데이터 생성용 (KIS API 키가 없을 때 테스트용)
import random, math


class KISDataFetcher(KISApi):
    """
    KIS API 확장 클래스. 실전 전용.
    """

    def __init__(self, demo_mode: bool = False):
        # demo_mode 인자는 하위 호환성 유지용 — 항상 실전 모드
        self.demo_mode = False
        try:
            super().__init__()
        except Exception as e:
            logger.warning(f"KIS API 초기화 실패: {e}")

    # ── 시장 지수 ──────────────────────────────────────────────
    def get_market_index(self, market: str = "KOSPI") -> dict:
        """
        코스피 / 코스닥 지수 현재가 + 이동평균 + 수익률
        market: "KOSPI" | "KOSDAQ"
        """
        code_map = {"KOSPI": "0001", "KOSDAQ": "1001"}
        code = code_map.get(market, "0001")
        url  = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-index-price"
        try:
            resp = requests.get(url,
                headers=self._headers("FHPUP02100000"),
                params={"FID_COND_MRKT_DIV_CODE": "U",
                        "FID_INPUT_ISCD": code},
                timeout=10
            )
            resp.raise_for_status()
            out = resp.json().get("output", {})
            # OHLCV 일봉으로 MA 계산
            candles = self.get_index_ohlcv(market, 130)
            ma60 = ma120 = 0
            closes = [c["close"] for c in candles]
            if len(closes) >= 60:
                ma60  = sum(closes[-60:])  / 60
            if len(closes) >= 120:
                ma120 = sum(closes[-120:]) / 120
            ret_20 = 0
            if len(closes) >= 20:
                ret_20 = (closes[-1] - closes[-20]) / closes[-20] * 100
            cur = float(out.get("bstp_nmix_prpr", 0))
            return {
                "market": market, "price": cur,
                "ma60": ma60, "ma120": ma120,
                "return_20d": round(ret_20, 2),
                "fell_today": float(out.get("bstp_nmix_prdy_vrss", 0)) < 0,
            }
        except Exception as e:
            logger.error(f"지수 조회 실패({market}): {e}")
            return self._demo_market_index(market)

    def get_index_ohlcv(self, market: str = "KOSPI", count: int = 130) -> list:
        """지수 일봉 데이터"""
        code_map = {"KOSPI": "0001", "KOSDAQ": "1001"}
        code = code_map.get(market, "0001")
        url  = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
        end_dt   = datetime.now().strftime("%Y%m%d")
        start_dt = (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d")
        try:
            resp = requests.get(url,
                headers=self._headers("FHKUP03500100"),
                params={
                    "FID_COND_MRKT_DIV_CODE": "U",
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_DATE_1": start_dt,
                    "FID_INPUT_DATE_2": end_dt,
                    "FID_PERIOD_DIV_CODE": "D",
                },
                timeout=10
            )
            resp.raise_for_status()
            output = resp.json().get("output2", [])
            candles = [{"date": r.get("stck_bsop_date",""),
                        "close": float(r.get("bstp_nmix_prpr", 0))}
                       for r in output]
            candles.sort(key=lambda x: x["date"])
            return candles[-count:]
        except Exception as e:
            logger.error(f"지수 OHLCV 실패({market}): {e}")
            return self._demo_ohlcv_index(market, count)

    # ── 전체 종목 목록 ─────────────────────────────────────────
    def get_stock_list(self, market: str = "ALL") -> list[dict]:
        """
        코스피 + 코스닥 전체 종목 기본 목록 반환 (실전 전용)
        """
        results = []
        for mkt, code in [("KOSPI", "J"), ("KOSDAQ", "Q")]:
            if market not in ("ALL", mkt):
                continue
            url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price-2"
            # 실제 구현: 페이지 반복 조회 필요
            # 여기서는 간소화 — 실전에서 페이징 처리 필요
            try:
                resp = requests.get(url,
                    headers=self._headers("FHPST01710000"),
                    params={
                        "fid_cond_mrkt_div_code": code,
                        "fid_cond_scr_div_code":  "20171",
                        "fid_input_iscd":          "0000",
                        "fid_div_cls_code":        "0",
                        "fid_blng_cls_code":       "0",
                        "fid_trgt_cls_code":       "111111111",
                        "fid_trgt_exls_cls_code":  "000000",
                        "fid_input_price_1":       "",
                        "fid_input_price_2":       "",
                        "fid_vol_cnt":             "",
                        "fid_input_date_1":        "",
                    },
                    timeout=30
                )
                resp.raise_for_status()
                for item in resp.json().get("output", []):
                    results.append({
                        "code":   item.get("mksc_shrn_iscd", ""),
                        "name":   item.get("hts_kor_isnm", ""),
                        "market": mkt,
                        "price":  int(item.get("stck_prpr", 0)),
                        "market_cap": int(item.get("stck_avls", 0)) * 100_000_000,
                        "daily_amount": float(item.get("acml_tr_pbmn", 0)),
                        "is_admin": item.get("mrkt_warn_cls_code", "00") != "00",
                        "is_halt":  item.get("trht_yn", "N") == "Y",
                        "warn_level": int(item.get("mrkt_warn_cls_code", "0") or 0),
                    })
            except Exception as e:
                logger.error(f"종목목록 조회 실패({mkt}): {e}")
        return results or []

    # ── 종목 상세 데이터 ──────────────────────────────────────
    def get_stock_detail(self, code: str, market_info: dict = None) -> dict:
        """
        스크리너에 필요한 종목 전체 데이터 수집
        (OHLCV + 투자자 + 재무 통합)
        """
        try:
            # 일봉 데이터
            candles = self.get_ohlcv(code, period="D", count=130)
            if len(candles) < 65:
                return {}
            closes  = [c["close"] for c in candles]
            volumes = [c["volume"] for c in candles]

            cur_price   = closes[-1]
            price_1m    = closes[-22] if len(closes) >= 22 else closes[0]
            price_3m    = closes[-66] if len(closes) >= 66 else closes[0]
            vol_avg20   = sum(volumes[-20:]) / 20
            vol_now     = volumes[-1]
            high_60d    = max(closes[-60:]) if len(closes) >= 60 else max(closes)
            ma20        = sum(closes[-20:]) / 20

            # 현재가 기본 정보
            cur_data = self.get_current_price(code)

            # 투자자 동향 (기관/외인)
            inv = self._get_investor_simple(code)

            mkt_info = market_info or {}

            return {
                "code":             code,
                "price_now":        cur_price,
                "price_1m":         price_1m,
                "price_3m":         price_3m,
                "price_ma20":       round(ma20),
                "volume_avg_20":    vol_avg20,
                "volume_now":       vol_now,
                "amount_avg_20":    cur_data.get("volume", 0) * cur_price,
                "amount_now":       cur_data.get("volume", 0) * cur_price,
                "high_60d":         high_60d,
                "inst_net_20":      inv.get("inst_net", 0),
                "foreign_net_20":   inv.get("foreign_net", 0),
                "inst_net_streak":  inv.get("inst_streak", 0),
                "foreign_net_streak": inv.get("foreign_streak", 0),
                "op_profit_now":    None,
                "op_profit_prev":   None,
                "op_profit_3y":     [],
                "roe":              None,
                "market_return_20": mkt_info.get("return_20d", 0),
                "market_fell_today":mkt_info.get("fell_today", False),
                "price_change_today": cur_data.get("change_rate", 0),
                "index_price":      mkt_info.get("price", 0),
                "index_ma60":       mkt_info.get("ma60", 0),
                "index_ma120":      mkt_info.get("ma120", 0),
                "market_cap":       int(cur_data.get("market_cap", "0").replace(",", "") or 0),
                "daily_amount":     cur_data.get("volume", 0) * cur_price,
            }
        except Exception as e:
            logger.error(f"종목 상세 수집 실패 {code}: {e}")
            return {}

    def _get_investor_simple(self, code: str) -> dict:
        """기관/외인 간략 조회"""
        try:
            url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
            resp = requests.get(url,
                headers=self._headers("FHKST01010900"),
                params={"fid_cond_mrkt_div_code": "J",
                        "fid_input_iscd": code},
                timeout=10
            )
            resp.raise_for_status()
            out = resp.json().get("output", [])
            inst_net = sum(int(r.get("orgn_ntby_qty", 0) or 0) for r in out[:20])
            frn_net  = sum(int(r.get("frgn_ntby_qty", 0) or 0) for r in out[:20])
            # 연속 순매수 계산
            inst_streak = frn_streak = 0
            for r in out:
                if int(r.get("orgn_ntby_qty", 0) or 0) > 0:
                    inst_streak += 1
                else:
                    break
            for r in out:
                if int(r.get("frgn_ntby_qty", 0) or 0) > 0:
                    frn_streak += 1
                else:
                    break
            return {"inst_net": inst_net, "foreign_net": frn_net,
                    "inst_streak": inst_streak, "foreign_streak": frn_streak}
        except Exception:
            return {"inst_net": 0, "foreign_net": 0,
                    "inst_streak": 0, "foreign_streak": 0}

    # ══════════════════════════════════════════════════════════
    # 데모 데이터 생성
    # ══════════════════════════════════════════════════════════

    def _demo_market_index(self, market: str) -> dict:
        # 데모: 시장이 MA60 위에 있고 20일 수익률 완만 (감점 최소화)
        base  = {"KOSPI": 2700, "KOSDAQ": 880}.get(market, 2700)
        price = base + (50 if market == "KOSPI" else 15)
        ma60  = base * 0.98   # 지수 > MA60 → 감점 없음
        ma120 = base * 0.96   # 지수 > MA120 → 감점 없음
        return {
            "market": market, "price": price,
            "ma60": ma60, "ma120": ma120,
            "return_20d": -1.5,   # 소폭 하락 → -5pt 구간 미달 → 감점 없음
            "fell_today": False,
        }

    def _demo_ohlcv_index(self, market: str, count: int) -> list:
        base  = {"KOSPI": 2700, "KOSDAQ": 880}.get(market, 2700)
        today = datetime.now()
        rows  = []
        price = base * 0.85
        for i in range(count, 0, -1):
            price *= (1 + random.uniform(-0.01, 0.012))
            dt = (today - timedelta(days=i)).strftime("%Y%m%d")
            rows.append({"date": dt, "close": round(price, 2)})
        return rows

    def _demo_stock_list(self) -> list[dict]:
        """테스트용 대표 종목 목록"""
        stocks = [
            ("005930","삼성전자","KOSPI","반도체"),
            ("000660","SK하이닉스","KOSPI","반도체"),
            ("035420","NAVER","KOSPI","IT서비스"),
            ("035720","카카오","KOSPI","IT서비스"),
            ("373220","LG에너지솔루션","KOSPI","2차전지"),
            ("051910","LG화학","KOSPI","화학"),
            ("006400","삼성SDI","KOSPI","2차전지"),
            ("207940","삼성바이오로직스","KOSPI","바이오"),
            ("068270","셀트리온","KOSPI","바이오"),
            ("105560","KB금융","KOSPI","금융"),
            ("055550","신한지주","KOSPI","금융"),
            ("032830","삼성생명","KOSPI","보험"),
            ("096770","SK이노베이션","KOSPI","정유"),
            ("017670","SK텔레콤","KOSPI","통신"),
            ("030200","KT","KOSPI","통신"),
            ("003550","LG","KOSPI","지주"),
            ("012330","현대모비스","KOSPI","자동차"),
            ("005380","현대차","KOSPI","자동차"),
            ("000270","기아","KOSPI","자동차"),
            ("028260","삼성물산","KOSPI","건설"),
            ("247540","에코프로비엠","KOSDAQ","2차전지"),
            ("086520","에코프로","KOSDAQ","2차전지"),
            ("091990","셀트리온헬스케어","KOSDAQ","바이오"),
            ("263750","펄어비스","KOSDAQ","게임"),
            ("293490","카카오게임즈","KOSDAQ","게임"),
            ("112040","위메이드","KOSDAQ","게임"),
            ("041510","에스엠","KOSDAQ","엔터"),
            ("352820","하이브","KOSPI","엔터"),
            ("122870","와이지엔터","KOSDAQ","엔터"),
            ("180640","한진칼","KOSPI","운송"),
        ]
        results = []
        random.seed(1234)
        for code, name, mkt, sector in stocks:
            base_price = random.randint(10000, 150000)
            mktcap = base_price * random.randint(5000, 100000) * 10
            results.append({
                "code": code, "name": name, "market": mkt, "sector": sector,
                "price": base_price,
                "market_cap":   mktcap,
                "daily_amount": random.randint(5, 500) * 1_000_000_000,
                "is_admin":  False, "is_halt": False, "warn_level": 0,
                "audit_opinion": "적정", "capital_erosion": False,
                "going_concern": False,
                "op_profit_3y": [random.randint(-100, 500) * 1e8 for _ in range(3)],
            })
        return results

    def _demo_stock_detail(self, code: str) -> dict:
        """
        테스트용 종목 상세 데이터.
        코드별 고정 시드로 재현 가능한 다양한 분포를 생성한다.
        일부 종목은 고점수(85+), 나머지는 낮은 점수로 자연스러운 분포.
        """
        rng = random.Random(hash(code) % 99991)

        # 종목별 '퀄리티' 티어 (코드 해시 기반)
        tier = hash(code) % 5   # 0=최상, 1=상, 2=중, 3=하, 4=최하

        base = rng.randint(15000, 180000)

        # 티어별 파라미터
        if tier == 0:   # 최상급 — BUY_CANDIDATE 범위
            ret_1m  = rng.uniform(8, 20)     # 강한 1개월 수익률
            ret_3m  = rng.uniform(18, 40)
            vol_mul = rng.uniform(1.8, 3.5)  # 거래량 폭발
            amt_mul = rng.uniform(2.0, 4.0)
            inst_net= rng.randint(100_000, 600_000)
            frn_net = rng.randint(80_000,  500_000)
            inst_streak = rng.randint(3, 8)
            frn_streak  = rng.randint(2, 6)
            op_now  = rng.uniform(500, 2500) * 1e8
            op_prev = rng.uniform(200, 1200) * 1e8
            roe     = rng.uniform(18, 35)
            hi_pct  = rng.uniform(0.98, 1.02)  # 신고가 돌파
            mkt_ret = -1.5
        elif tier == 1:  # 상급 — WATCH_HIGH 범위
            ret_1m  = rng.uniform(4, 12)
            ret_3m  = rng.uniform(10, 25)
            vol_mul = rng.uniform(1.3, 2.2)
            amt_mul = rng.uniform(1.3, 2.5)
            inst_net= rng.randint(30_000, 200_000)
            frn_net = rng.randint(20_000, 180_000)
            inst_streak = rng.randint(2, 5)
            frn_streak  = rng.randint(1, 4)
            op_now  = rng.uniform(300, 1500) * 1e8
            op_prev = rng.uniform(150,  900) * 1e8
            roe     = rng.uniform(12, 22)
            hi_pct  = rng.uniform(0.93, 1.01)
            mkt_ret = -1.5
        elif tier == 2:  # 중급 — WATCH 범위
            ret_1m  = rng.uniform(1, 7)
            ret_3m  = rng.uniform(3, 15)
            vol_mul = rng.uniform(1.0, 1.6)
            amt_mul = rng.uniform(0.9, 1.5)
            inst_net= rng.randint(-30_000, 100_000)
            frn_net = rng.randint(-40_000,  80_000)
            inst_streak = rng.randint(0, 3)
            frn_streak  = rng.randint(0, 2)
            op_now  = rng.uniform(100, 800) * 1e8
            op_prev = rng.uniform(80,  700) * 1e8
            roe     = rng.uniform(6, 15)
            hi_pct  = rng.uniform(0.85, 0.96)
            mkt_ret = -1.5
        else:  # 하/최하 — HOLD / EXCLUDE
            ret_1m  = rng.uniform(-8, 3)
            ret_3m  = rng.uniform(-15, 5)
            vol_mul = rng.uniform(0.5, 1.2)
            amt_mul = rng.uniform(0.4, 1.1)
            inst_net= rng.randint(-200_000, 30_000)
            frn_net = rng.randint(-250_000, 20_000)
            inst_streak = 0
            frn_streak  = 0
            op_now  = rng.uniform(-200, 400) * 1e8
            op_prev = rng.uniform(-100, 600) * 1e8
            roe     = rng.uniform(-5, 8)
            hi_pct  = rng.uniform(0.70, 0.90)
            mkt_ret = -1.5

        cur = base
        # 1개월 전 가격 역산
        price_1m = round(cur / (1 + ret_1m / 100))
        # 3개월 전 가격 역산
        price_3m = round(cur / (1 + ret_3m / 100))
        # MA20 — 현재가 대비 소폭 차이
        price_ma20 = round(cur * rng.uniform(0.94, 0.99) if tier < 3 else cur * rng.uniform(0.99, 1.06))

        vol_avg = rng.randint(1_000_000, 8_000_000)
        vol_now = int(vol_avg * vol_mul)

        base_amt  = vol_avg * cur
        amt_avg   = base_amt * rng.uniform(0.8, 1.2)
        amt_now   = base_amt * amt_mul

        # 60일 고가 — tier0/1은 신고가 돌파, 나머지는 이전 고가보다 낮음
        high_60d = round(cur * hi_pct)
        if high_60d < cur:
            high_60d = round(cur * 1.001)  # 최소한 현재가 이상

        # 시장 정보 (데모: 감점 없는 시장 상태)
        index_price = 2750
        index_ma60  = 2690
        index_ma120 = 2620

        return {
            "code":               code,
            "price_now":          round(cur),
            "price_1m":           price_1m,
            "price_3m":           price_3m,
            "price_ma20":         price_ma20,
            "volume_avg_20":      vol_avg,
            "volume_now":         vol_now,
            "amount_avg_20":      amt_avg,
            "amount_now":         amt_now,
            "high_60d":           high_60d,
            "inst_net_20":        inst_net,
            "foreign_net_20":     frn_net,
            "inst_net_streak":    inst_streak,
            "foreign_net_streak": frn_streak,
            "op_profit_now":      op_now,
            "op_profit_prev":     op_prev,
            "op_profit_3y":       [op_prev * 0.9, op_prev, op_now],
            "roe":                round(roe, 1),
            "market_return_20":   mkt_ret,
            "market_fell_today":  False,
            "price_change_today": round(ret_1m / 22, 2),   # 일평균 추정
            "index_price":        index_price,
            "index_ma60":         index_ma60,
            "index_ma120":        index_ma120,
            "market_cap":         int(cur * rng.randint(30000, 500000)),
            "daily_amount":       amt_now,
        }

    # ══════════════════════════════════════════════════════════
    # ETF 전용 데이터 수집
    # ══════════════════════════════════════════════════════════

    def get_etf_list(self) -> list[dict]:
        """
        투자 대상 ETF 목록 반환 (실전 전용 KIS API).
        """
        try:
            return self._real_etf_list()
        except Exception as e:
            logger.error(f"ETF 목록 조회 실패: {e}")
            from screener.asset_universe import get_demo_etf_list
            return get_demo_etf_list()

    def _real_etf_list(self) -> list[dict]:
        """실전 KIS API ETF 목록 조회 (ETF 전용 API)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-etf-price"
        results = []
        try:
            resp = requests.get(
                url,
                headers=self._headers("FHPST02400000"),
                params={
                    "fid_cond_mrkt_div_code": "E",
                    "fid_input_iscd": "0000",
                },
                timeout=30,
            )
            resp.raise_for_status()
            for item in resp.json().get("output", []):
                code = item.get("mksc_shrn_iscd", "")
                name = item.get("hts_kor_isnm", "")
                from screener.asset_universe import classify_asset_type, get_etf_info
                asset_type = classify_asset_type(code, name, "ETF")
                etf_meta   = get_etf_info(code) or {}
                results.append({
                    "code":       code,
                    "name":       name,
                    "market":     "ETF",
                    "asset_type": asset_type,
                    "price":      int(item.get("stck_prpr", 0)),
                    "daily_amount": float(item.get("acml_tr_pbmn", 0)),
                    "is_admin":   False,
                    "is_halt":    item.get("trht_yn", "N") == "Y",
                    "warn_level": 0,
                    "multiplier": etf_meta.get("multiplier", 1.0),
                    "index":      etf_meta.get("index", ""),
                })
        except Exception as e:
            logger.error(f"실전 ETF 목록 조회 실패: {e}")
        if results:
            return results
        # KIS API가 ETF 전체 목록 조회를 지원하지 않을 경우 → 정적 ETF 목록 사용
        logger.info("ETF 목록 KIS API 미지원 → 정적 ETF 목록(asset_universe) 사용")
        from screener.asset_universe import get_demo_etf_list
        return get_demo_etf_list()

    def get_etf_detail(self, code: str, asset_type: str = None) -> dict:
        """
        ETF 1종목 상세 데이터 (ETFScorer 입력용). 실전 전용.
        """
        try:
            return self.get_stock_detail(code)
        except Exception as e:
            logger.error(f"ETF 상세 조회 실패({code}): {e}")
            return self._demo_etf_detail(code, asset_type)

    def _demo_etf_detail(self, code: str, asset_type: str = None) -> dict:
        """
        데모용 ETF 상세 데이터 생성.
        자산군(asset_type)에 따라 성격이 다른 데이터 생성:
          - LEVERAGE : 지수 2배 움직임, 상승 추세
          - INVERSE  : 지수 역방향 움직임
          - GENERAL  : 안정적 추세
        """
        from screener.asset_universe import (
            ASSET_ETF_LEVERAGE, ASSET_ETF_INVERSE, ASSET_ETF_GENERAL,
            ETF_CODE_MAP, classify_asset_type,
        )
        rng = random.Random(hash(code) % 88881)

        # 자산군 결정
        etf_meta = ETF_CODE_MAP.get(code, {})
        if asset_type is None:
            asset_type = etf_meta.get("asset_type") or \
                         classify_asset_type(code, etf_meta.get("name",""), "ETF")
        multiplier = etf_meta.get("multiplier", 1.0)

        # 기초지수 수익률 (demo)
        idx_ret_20d = -1.5   # 시장 20일 수익률 (소폭 하락)

        # 자산군별 수익률 범위 설정
        if asset_type == ASSET_ETF_LEVERAGE:
            # 레버리지: 지수의 약 2배 움직임 + 알파
            ret_1m = idx_ret_20d * abs(multiplier) + rng.uniform(-3, 8)
            ret_3m = rng.uniform(5, 25)
            above_ma20 = rng.random() > 0.3     # 주로 MA20 위
            above_ma60 = rng.random() > 0.4
            vol_mul = rng.uniform(1.5, 3.0)
            amt_mul = rng.uniform(1.5, 3.5)
        elif asset_type == ASSET_ETF_INVERSE:
            # 인버스: 지수 하락 시 상승
            ret_1m = -idx_ret_20d * abs(multiplier) + rng.uniform(-2, 5)
            ret_3m = rng.uniform(-5, 15)
            above_ma20 = rng.random() > 0.5
            above_ma60 = rng.random() > 0.5
            vol_mul = rng.uniform(1.2, 2.5)
            amt_mul = rng.uniform(1.2, 2.8)
        else:  # GENERAL
            ret_1m = rng.uniform(-3, 10)
            ret_3m = rng.uniform(0, 18)
            above_ma20 = rng.random() > 0.35
            above_ma60 = rng.random() > 0.40
            vol_mul = rng.uniform(0.8, 2.0)
            amt_mul = rng.uniform(0.8, 2.2)

        base = rng.choice([5000, 8000, 10000, 12000, 15000, 20000, 25000])
        cur  = base
        price_1m  = round(cur / (1 + ret_1m / 100))
        price_3m  = round(cur / (1 + ret_3m / 100))
        price_ma20 = round(cur * (0.96 if above_ma20 else 1.03))
        price_ma60 = round(cur * (0.92 if above_ma60 else 1.06))

        vol_avg = rng.randint(500_000, 5_000_000)
        vol_now = int(vol_avg * vol_mul)
        base_amt = vol_avg * cur
        amt_avg  = base_amt * rng.uniform(0.8, 1.2)
        amt_now  = base_amt * amt_mul

        high_60d = round(cur * rng.uniform(0.96, 1.05))

        return {
            "code":               code,
            "name":               etf_meta.get("name", code),
            "asset_type":         asset_type,
            "price_now":          cur,
            "price_1m":           price_1m,
            "price_3m":           price_3m,
            "price_ma20":         price_ma20,
            "price_ma60":         price_ma60,
            "volume_avg_20":      vol_avg,
            "volume_now":         vol_now,
            "amount_avg_20":      amt_avg,
            "amount_now":         amt_now,
            "high_60d":           high_60d,
            "inst_net_20":        0,   # ETF는 기관순매수 N/A
            "foreign_net_20":     0,
            "inst_net_streak":    0,
            "foreign_net_streak": 0,
            "op_profit_now":      None,  # ETF는 재무 N/A
            "op_profit_prev":     None,
            "op_profit_3y":       [],
            "roe":                None,
            "market_return_20":   idx_ret_20d,
            "market_fell_today":  False,
            "price_change_today": round(ret_1m / 22, 2),
            "index_price":        2750,
            "index_ma60":         2690,
            "index_ma120":        2620,
            "market_cap":         int(cur * rng.randint(5000, 200000)),
            "daily_amount":       amt_now,
            "multiplier":         multiplier,
        }
