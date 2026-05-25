"""
한국투자증권 KIS Open API 연동 모듈
- OAuth2 토큰 발급 / 갱신
- 주식 현재가 조회
- 매수 / 매도 주문
- 잔고 조회
- 체결 내역 조회
"""
import json
import time
import hashlib
import requests
from datetime import datetime, timedelta
from utils.logger import get_logger
from config import Config

logger = get_logger("KIS_API")


class KISApi:
    def __init__(self):
        self.app_key    = Config.KIS_APP_KEY
        self.app_secret = Config.KIS_APP_SECRET
        self.account_no = Config.KIS_ACCOUNT_NO
        self.base_url   = Config.BASE_URL
        self.is_real    = Config.KIS_IS_REAL

        self._access_token    = None
        self._token_expired   = None
        self._approval_key    = None   # 웹소켓 실시간용

    # ──────────────────────────────────────────────────────────
    # 1. OAuth2 토큰 관리
    # ──────────────────────────────────────────────────────────
    def _get_token(self) -> str:
        """액세스 토큰 발급 (만료 전 자동 갱신)"""
        now = datetime.now()
        if self._access_token and self._token_expired and now < self._token_expired:
            return self._access_token

        url  = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey":     self.app_key,
            "appsecret":  self.app_secret,
        }
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        self._access_token  = data["access_token"]
        expires_in          = int(data.get("expires_in", 86400))
        self._token_expired = now + timedelta(seconds=expires_in - 60)
        logger.info("✅ KIS 액세스 토큰 발급 성공")
        return self._access_token

    def _hashkey(self, body: dict) -> str:
        """매수/매도 주문용 HashKey 생성"""
        url  = f"{self.base_url}/uapi/hashkey"
        hdrs = {
            "content-type": "application/json",
            "appkey":        self.app_key,
            "appsecret":     self.app_secret,
        }
        resp = requests.post(url, headers=hdrs, json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()["HASH"]

    def _headers(self, tr_id: str, use_hash: bool = False, body: dict = None) -> dict:
        token = self._get_token()
        h = {
            "content-type":  "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey":         self.app_key,
            "appsecret":      self.app_secret,
            "tr_id":          tr_id,
            "custtype":       "P",
        }
        if use_hash and body:
            h["hashkey"] = self._hashkey(body)
        return h

    # ──────────────────────────────────────────────────────────
    # 2. 시세 조회
    # ──────────────────────────────────────────────────────────
    def get_current_price(self, stock_code: str) -> dict:
        """주식 현재가 조회"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        tr_id = "FHKST01010100"
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
        }
        try:
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            output = data.get("output", {})
            return {
                "code":          stock_code,
                "price":         int(output.get("stck_prpr", 0)),
                "open":          int(output.get("stck_oprc", 0)),
                "high":          int(output.get("stck_hgpr", 0)),
                "low":           int(output.get("stck_lwpr", 0)),
                "volume":        int(output.get("acml_vol", 0)),
                "change_rate":   float(output.get("prdy_ctrt", 0)),
                "change_price":  int(output.get("prdy_vrss", 0)),
                "market_cap":    output.get("hts_avls", "0"),
            }
        except Exception as e:
            logger.error(f"현재가 조회 실패 {stock_code}: {e}")
            return {}

    def get_ohlcv(self, stock_code: str, period: str = "D",
                  count: int = 100) -> list[dict]:
        """
        일/주/월봉 조회
        period: D(일), W(주), M(월)
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        tr_id = "FHKST03010100"
        end_dt   = datetime.now().strftime("%Y%m%d")
        start_dt = (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d")
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd":         stock_code,
            "fid_input_date_1":       start_dt,
            "fid_input_date_2":       end_dt,
            "fid_period_div_code":    period,
            "fid_org_adj_prc":        "0",
        }
        try:
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            resp.raise_for_status()
            data   = resp.json()
            output = data.get("output2", [])
            candles = []
            for row in output:
                candles.append({
                    "date":   row.get("stck_bsop_date", ""),
                    "open":   int(row.get("stck_oprc", 0)),
                    "high":   int(row.get("stck_hgpr", 0)),
                    "low":    int(row.get("stck_lwpr", 0)),
                    "close":  int(row.get("stck_clpr", 0)),
                    "volume": int(row.get("acml_vol", 0)),
                })
            candles.sort(key=lambda x: x["date"])
            return candles[-count:]
        except Exception as e:
            logger.error(f"OHLCV 조회 실패 {stock_code}: {e}")
            return []

    # ──────────────────────────────────────────────────────────
    # 3. 주문
    # ──────────────────────────────────────────────────────────
    def _order(self, stock_code: str, order_type: str,
               qty: int, price: int = 0,
               ord_dvsn: str = None) -> dict:
        """
        order_type : BUY | SELL
        price      : 0 이면 시장가
        ord_dvsn   : KIS 주문유형 코드
                     00 지정가 / 01 시장가
                     05 장전시간외 / 06 장후시간외
                     None → price 값으로 자동 선택
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        # 실전: TTTC0802U(매수) / TTTC0801U(매도)
        # 모의: VTTC0802U(매수) / VTTC0801U(매도)
        if self.is_real:
            tr_id = "TTTC0802U" if order_type == "BUY" else "TTTC0801U"
        else:
            tr_id = "VTTC0802U" if order_type == "BUY" else "VTTC0801U"

        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")

        # 주문 유형 코드 자동 결정
        if ord_dvsn is None:
            ord_dvsn = "01" if price == 0 else "00"

        body = {
            "CANO":         acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "PDNO":         stock_code,
            "ORD_DVSN":     ord_dvsn,
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     str(price) if price > 0 else "0",
        }
        try:
            resp = requests.post(url,
                                 headers=self._headers(tr_id, use_hash=True, body=body),
                                 json=body, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("rt_cd") == "0":
                logger.info(f"✅ {order_type} 주문 성공 | {stock_code} {qty}주 {price}원")
            else:
                logger.warning(f"⚠️ {order_type} 주문 실패 | {result.get('msg1')}")
            return result
        except Exception as e:
            logger.error(f"주문 오류 {stock_code}: {e}")
            return {"rt_cd": "9", "msg1": str(e)}

    def buy(self, stock_code: str, qty: int, price: int = 0,
           ord_dvsn: str = None) -> dict:
        return self._order(stock_code, "BUY", qty, price, ord_dvsn)

    def sell(self, stock_code: str, qty: int, price: int = 0,
            ord_dvsn: str = None) -> dict:
        return self._order(stock_code, "SELL", qty, price, ord_dvsn)

    # ──────────────────────────────────────────────────────────
    # 4. 잔고 조회
    # ──────────────────────────────────────────────────────────
    def get_balance(self) -> dict:
        """보유 주식 및 예수금 조회"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = "TTTC8434R" if self.is_real else "VTTC8434R"
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")
        params = {
            "CANO":         acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "AFHR_FLPR_YN": "N",
            "OFL_YN":       "",
            "INQR_DVSN":    "02",
            "UNPR_DVSN":    "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN":    "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        try:
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            holdings = []
            for item in data.get("output1", []):
                qty = int(item.get("hldg_qty", 0))
                if qty > 0:
                    holdings.append({
                        "code":       item.get("pdno", ""),
                        "name":       item.get("prdt_name", ""),
                        "qty":        qty,
                        "avg_price":  float(item.get("pchs_avg_pric", 0)),
                        "cur_price":  int(item.get("prpr", 0)),
                        "profit_pct": float(item.get("evlu_pfls_rt", 0)),
                        "profit_amt": int(item.get("evlu_pfls_amt", 0)),
                    })
            summary = data.get("output2", [{}])[0]
            return {
                "holdings":       holdings,
                "total_eval":     int(summary.get("tot_evlu_amt", 0)),
                "cash":           int(summary.get("dnca_tot_amt", 0)),
                "total_profit":   int(summary.get("evlu_pfls_smtl_amt", 0)),
                "total_profit_pct": float(summary.get("tot_evlu_pfls_rt", 0)),
            }
        except Exception as e:
            logger.error(f"잔고 조회 실패: {e}")
            return {"holdings": [], "total_eval": 0, "cash": 0,
                    "total_profit": 0, "total_profit_pct": 0}

    # ──────────────────────────────────────────────────────────
    # 5. 체결 내역 조회
    # ──────────────────────────────────────────────────────────
    def get_order_history(self, days: int = 7) -> list[dict]:
        """최근 체결 내역 조회"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        tr_id = "TTTC8001R" if self.is_real else "VTTC8001R"
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        end   = datetime.now().strftime("%Y%m%d")
        params = {
            "CANO":         acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "INQR_STRT_DT": start,
            "INQR_END_DT":  end,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN":    "00",
            "PDNO":         "",
            "CCLD_DVSN":    "01",
            "ORD_GNO_BRNO":  "",
            "ODNO":         "",
            "INQR_DVSN_3":  "00",
            "INQR_DVSN_1":  "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        try:
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            orders = []
            for item in data.get("output1", []):
                orders.append({
                    "date":    item.get("ord_dt", ""),
                    "time":    item.get("ord_tmd", ""),
                    "code":    item.get("pdno", ""),
                    "name":    item.get("prdt_name", ""),
                    "type":    "매수" if item.get("sll_buy_dvsn_cd") == "02" else "매도",
                    "qty":     int(item.get("tot_ccld_qty", 0)),
                    "price":   int(item.get("avg_prvs", 0)),
                    "amount":  int(item.get("tot_ccld_amt", 0)),
                    "status":  item.get("ord_stts_name", ""),
                })
            return orders
        except Exception as e:
            logger.error(f"체결 내역 조회 실패: {e}")
            return []
