"""
broker/kr_broker.py — 국내장 KIS API 브로커 (V2)
==================================================
역할:
  - 국내 주식 현재가 조회
  - 5분봉 조회
  - 잔고 조회 (tot_evlu_amt 포함 — 실계좌 총자산)
  - 매수 / 매도 주문 (ORD_DVSN / ORD_UNPR 완전 검증)
  - 미체결 주문 조회 / 취소
  - 주문가능금액 조회
  - 호가단위 보정

★ 설계 원칙:
  1. 매 호출마다 실계좌 기준
  2. ORD_DVSN=00(지정가)는 반드시 가격 > 0
  3. ORD_DVSN=01(시장가)는 가격 = 0
  4. 장후시간외(06) BUY 절대 금지
  5. 모든 주문 전 수량/가격 사전 검증 → 차단 즉시 반환
"""

import time
from datetime import datetime, time as dtime
from typing import Optional
import pytz

from broker.kis_base import KISBase
from utils.v2_logger  import get_logger

logger = get_logger("KRBroker")
KST    = pytz.timezone("Asia/Seoul")

# ── 매수 허용 시간 ────────────────────────────────────────────────
BUY_OPEN_TIME  = dtime(9,  0)    # 09:00
BUY_CLOSE_TIME = dtime(14, 30)   # 14:30 (신규매수 금지 기준)
BUY_HARD_STOP  = dtime(15, 20)   # 15:20 미체결 취소 기준

# ── 주문 유형 코드 ────────────────────────────────────────────────
ORD_LIMIT      = "00"   # 지정가 (가격 필수)
ORD_MARKET     = "01"   # 시장가 (가격 = 0)
ORD_PRE_MKT    = "05"   # 장전시간외
ORD_POST_MKT   = "06"   # 장후시간외 (BUY 금지)

# ── 유효한 (ORD_DVSN, has_price) 조합 ───────────────────────────
_VALID_COMBO = {
    (ORD_LIMIT,    True):  True,    # 지정가 + 가격
    (ORD_LIMIT,    False): False,   # ❌ 지정가 + 0원
    (ORD_MARKET,   False): True,    # 시장가 + 0원
    (ORD_MARKET,   True):  False,   # ❌ 시장가 + 가격
    (ORD_PRE_MKT,  False): True,    # 장전 + 0원
    (ORD_POST_MKT, False): True,    # 장후 + 0원 (SELL 전용)
}

# ── 5분봉 캐시 TTL ────────────────────────────────────────────────
_5MIN_CACHE_TTL = 60   # 60초


class KRBroker(KISBase):

    def __init__(self, app_key: str, app_secret: str, account_no: str):
        super().__init__(app_key, app_secret, account_no)

        # 현재가 캐시 {code: (data, ts)}
        self._price_cache:  dict = {}
        self._PRICE_TTL:    float = 3.0

        # 5분봉 캐시 {code: (candles, ts)}
        self._5min_cache:   dict = {}

        # 잔고 캐시
        self._balance_cache: dict = {}
        self._balance_ts:    float = 0.0
        self._BALANCE_TTL:   float = 60.0   # V2: 60초 (매 루프 갱신 기준)

    # ════════════════════════════════════════════════════════════
    # 1. 현재가 조회
    # ════════════════════════════════════════════════════════════

    def get_price(self, code: str, force: bool = False) -> dict:
        """
        국내 주식 현재가 조회.
        Returns:
            {price, open, high, low, volume, strength, vwap,
             prev_close, prev_volume, change_pct}
        """
        now = time.time()
        if not force:
            cached = self._price_cache.get(code)
            if cached and (now - cached[1]) < self._PRICE_TTL:
                return cached[0]

        url    = f"{self.BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
        tr_id  = "FHKST01010100"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}

        data = self._get(url, tr_id, params)
        if data.get("rt_cd") != "0":
            logger.warning(f"[현재가] {code} 조회 실패: {data.get('msg1','')}")
            return {}

        o = data.get("output", {})
        result = {
            "price":        int(o.get("stck_prpr",   0) or 0),
            "open":         int(o.get("stck_oprc",   0) or 0),
            "high":         int(o.get("stck_hgpr",   0) or 0),
            "low":          int(o.get("stck_lwpr",   0) or 0),
            "volume":       int(o.get("acml_vol",    0) or 0),
            "strength":     float(o.get("seln_cnqn_smtn", 0) or 0),  # 체결강도
            "vwap":         float(o.get("stck_vwap",  0) or 0),
            "prev_close":   int(o.get("stck_sdpr",   0) or 0),
            "prev_volume":  int(o.get("acml_prdy_vol", 0) or 0),
            "change_pct":   float(o.get("prdy_ctrt",  0) or 0),
        }
        self._price_cache[code] = (result, now)
        return result

    # ════════════════════════════════════════════════════════════
    # 2. 5분봉 조회
    # ════════════════════════════════════════════════════════════

    def get_5min_candles(self, code: str, count: int = 20) -> list[dict]:
        """
        5분봉 캔들 조회 (최대 count개, 최신순 → 오름차순으로 반환).
        """
        now = time.time()
        cached = self._5min_cache.get(code)
        if cached and (now - cached[1]) < _5MIN_CACHE_TTL:
            return cached[0]

        url   = f"{self.BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        tr_id = "FHKST03010200"
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":   code,
            "FID_INPUT_HOUR_1": "153000",
            "FID_PW_DATA_INCU_YN": "N",
        }
        data = self._get(url, tr_id, params)
        if data.get("rt_cd") != "0":
            logger.warning(f"[5분봉] {code} 조회 실패: {data.get('msg1','')}")
            return []

        candles = []
        for item in reversed(data.get("output2", [])):
            try:
                candles.append({
                    "time":   item.get("stck_bsop_date", "") + item.get("stck_cntg_hour", ""),
                    "open":   int(item.get("stck_oprc", 0) or 0),
                    "high":   int(item.get("stck_hgpr", 0) or 0),
                    "low":    int(item.get("stck_lwpr", 0) or 0),
                    "close":  int(item.get("stck_prpr", 0) or 0),
                    "volume": int(item.get("cntg_vol",  0) or 0),
                })
            except Exception:
                continue
        candles = candles[-count:] if len(candles) > count else candles
        self._5min_cache[code] = (candles, now)
        return candles

    # ════════════════════════════════════════════════════════════
    # 3. 실계좌 잔고 조회 (총자산 포함)
    # ════════════════════════════════════════════════════════════

    def get_balance(self, force: bool = False) -> dict:
        """
        실계좌 잔고 조회.
        ★ tot_evlu_amt = KIS MTS '총자산' (보유평가 + 예수금 + 정산금 등)

        Returns:
            {
              holdings: [{code, name, qty, avg_price, cur_price, pnl_pct, pnl_amt}],
              total_asset:   KIS tot_evlu_amt — 총자산 (복리 계산 기준),
              cash:          주문가능 예수금,
              scts_eval:     보유종목 평가금액,
              prev_settle:   전일매도정산금,
              purchase_amt:  매입금액 합계,
              total_pnl:     평가손익 합계,
            }
        """
        now_ts = time.time()
        if not force and self._balance_cache:
            if (now_ts - self._balance_ts) < self._BALANCE_TTL:
                return self._balance_cache

        url   = f"{self.BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = "TTTC8434R"
        params = {
            "CANO":            self._acc_no,
            "ACNT_PRDT_CD":    self._acc_prod,
            "AFHR_FLPR_YN":    "N",
            "OFL_YN":          "",
            "INQR_DVSN":       "02",
            "UNPR_DVSN":       "01",
            "FUND_STTL_ICLD_YN":     "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN":       "01",
            "CTX_AREA_FK100":  "",
            "CTX_AREA_NK100":  "",
        }
        data = self._get(url, tr_id, params, max_retry=3)
        if data.get("rt_cd") != "0":
            # 폴백: 캐시 반환
            if self._balance_cache:
                logger.warning("[잔고] 조회 실패 → 캐시 사용")
                return self._balance_cache
            return self._empty_balance()

        holdings = []
        for item in data.get("output1", []):
            qty = int(item.get("hldg_qty", 0) or 0)
            if qty > 0:
                holdings.append({
                    "code":      item.get("pdno", ""),
                    "name":      item.get("prdt_name", ""),
                    "qty":       qty,
                    "avg_price": float(item.get("pchs_avg_pric", 0) or 0),
                    "cur_price": int(item.get("prpr", 0) or 0),
                    "pnl_pct":   float(item.get("evlu_pfls_rt",  0) or 0),
                    "pnl_amt":   int(item.get("evlu_pfls_amt",  0) or 0),
                })

        s = data.get("output2", [{}])[0]
        result = {
            "holdings":     holdings,
            "total_asset":  int(s.get("tot_evlu_amt",       0) or 0),  # ★ MTS 총자산
            "cash":         int(s.get("dnca_tot_amt",        0) or 0),  # 예수금
            "scts_eval":    int(s.get("scts_evlu_amt",       0) or 0),  # 보유평가
            "prev_settle":  int(s.get("prvs_rcdl_excc_amt",  0) or 0),  # 전일정산금
            "purchase_amt": int(s.get("pchs_amt_smtl_amt",   0) or 0),  # 매입금액
            "total_pnl":    int(s.get("evlu_pfls_smtl_amt",  0) or 0),  # 평가손익
        }

        # 캐시 갱신 (cash > 0 이면 신뢰)
        if result["cash"] >= 0:
            self._balance_cache = result
            self._balance_ts    = now_ts
        return result

    @staticmethod
    def _empty_balance() -> dict:
        return {"holdings": [], "total_asset": 0, "cash": 0,
                "scts_eval": 0, "prev_settle": 0,
                "purchase_amt": 0, "total_pnl": 0}

    # ════════════════════════════════════════════════════════════
    # 4. 주문가능금액 조회
    # ════════════════════════════════════════════════════════════

    def get_orderable_cash(self) -> int:
        """실계좌 기준 주문가능금액 조회 (미수 방지)."""
        url   = f"{self.BASE_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        tr_id = "TTTC8908R"
        params = {
            "CANO":         self._acc_no,
            "ACNT_PRDT_CD": self._acc_prod,
            "PDNO":         "005930",   # 더미 종목코드 (API 필수)
            "ORD_UNPR":     "0",
            "ORD_DVSN":     ORD_MARKET,
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN":          "N",
        }
        data = self._get(url, tr_id, params)
        if data.get("rt_cd") != "0":
            # 폴백: 잔고 캐시에서 cash
            return self._balance_cache.get("cash", 0)
        return int(data.get("output", {}).get("ord_psbl_cash", 0) or 0)

    # ════════════════════════════════════════════════════════════
    # 5. 주문 (BUY / SELL)  — 완전 사전 검증 포함
    # ════════════════════════════════════════════════════════════

    def _validate_order(self,
                        code: str,
                        order_type: str,
                        qty: int,
                        price: int,
                        ord_dvsn: str) -> Optional[dict]:
        """
        주문 전 사전 검증. 통과 시 None, 차단 시 에러 dict 반환.

        검증 항목:
          V1. 수량 > 0
          V2. ORD_DVSN / ORD_UNPR 조합
          V3. 장후시간외(06) BUY 금지
          V4. 매수 시간 체크 (09:00 ~ 14:30)
          V5. SELL 시 보유수량 확인 (0주 → 차단)
        """
        now_kst = datetime.now(KST)
        t       = now_kst.time()

        # V1. 수량
        if qty <= 0:
            return {"rt_cd": "9", "msg1": f"주문수량 오류 qty={qty}"}

        # V2. ORD_DVSN + 가격 조합
        has_price = price > 0
        ok = _VALID_COMBO.get((ord_dvsn, has_price))
        if ok is False:
            return {
                "rt_cd": "9",
                "msg1": f"ORD_DVSN={ord_dvsn} + ORD_UNPR={price} 조합 오류",
            }

        # V3. 장후시간외 BUY 금지
        if order_type == "BUY" and ord_dvsn == ORD_POST_MKT:
            return {"rt_cd": "9", "msg1": "장후시간외(06) BUY 절대 금지"}

        # V4. 매수 시간 체크
        if order_type == "BUY" and ord_dvsn not in (ORD_PRE_MKT, ORD_POST_MKT):
            if now_kst.weekday() >= 5:
                return {"rt_cd": "9", "msg1": "주말 — 매수 불가"}
            if not (BUY_OPEN_TIME <= t <= BUY_CLOSE_TIME):
                return {
                    "rt_cd": "9",
                    "msg1":  f"매수 불가 시간 ({t.strftime('%H:%M')}) — 허용=09:00~14:30",
                }

        # V5. SELL: 보유수량 조회로 0주 차단
        if order_type == "SELL":
            holdings  = self.get_balance().get("holdings", [])
            held_item = next((h for h in holdings if h["code"] == code), None)
            held_qty  = held_item["qty"] if held_item else 0
            if held_qty <= 0:
                return {"rt_cd": "9", "msg1": f"{code} 보유수량 없음 — SELL 차단"}
            if qty > held_qty:
                logger.warning(
                    f"[주문검증] {code} 주문수량({qty}) > 보유수량({held_qty}) → 보유수량으로 보정"
                )
                # qty 보정은 호출자에서 처리 (여기선 통과)

        return None   # 검증 통과

    def _place_order(self, code: str, order_type: str,
                     qty: int, price: int = 0,
                     ord_dvsn: str = ORD_MARKET) -> dict:
        """
        실제 주문 전송.
        order_type: "BUY" | "SELL"
        """
        # 사전 검증
        err = self._validate_order(code, order_type, qty, price, ord_dvsn)
        if err:
            logger.error(
                f"[주문차단] {order_type} {code} qty={qty} price={price} "
                f"ord_dvsn={ord_dvsn} → {err['msg1']}"
            )
            return err

        tr_id = "TTTC0802U" if order_type == "BUY" else "TTTC0801U"
        url   = f"{self.BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"

        body = {
            "CANO":         self._acc_no,
            "ACNT_PRDT_CD": self._acc_prod,
            "PDNO":         code,
            "ORD_DVSN":     ord_dvsn,
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     str(price),
        }

        logger.info(
            f"[{order_type}] {code} qty={qty} price={price:,} "
            f"ord_dvsn={ord_dvsn} tr_id={tr_id}"
        )
        result = self._post(url, tr_id, body, use_hash=True)
        if result.get("rt_cd") == "0":
            # 주문 성공 시 잔고 캐시 무효화 (다음 조회에서 실계좌 재조회)
            self._balance_ts = 0.0
            logger.info(
                f"[{order_type}✅] {code} 주문번호={result.get('output', {}).get('ODNO', '?')}"
            )
        return result

    def buy(self, code: str, qty: int, price: int = 0,
            ord_dvsn: str = ORD_MARKET) -> dict:
        """매수 주문."""
        return self._place_order(code, "BUY", qty, price, ord_dvsn)

    def sell(self, code: str, qty: int, price: int = 0,
             ord_dvsn: str = ORD_MARKET) -> dict:
        """매도 주문. qty는 실보유수량 이하로 자동 보정."""
        # 실보유수량 확인 및 보정
        holdings  = self.get_balance().get("holdings", [])
        held_item = next((h for h in holdings if h["code"] == code), None)
        held_qty  = held_item["qty"] if held_item else 0

        if held_qty <= 0:
            logger.warning(f"[SELL차단] {code} 보유수량 없음 (held={held_qty})")
            return {"rt_cd": "9", "msg1": f"{code} 보유수량 없음"}

        sell_qty = min(qty, held_qty)
        if sell_qty != qty:
            logger.warning(f"[SELL보정] {code} qty {qty}→{sell_qty} (보유={held_qty})")

        return self._place_order(code, "SELL", sell_qty, price, ord_dvsn)

    # ════════════════════════════════════════════════════════════
    # 6. 미체결 주문 조회 / 취소
    # ════════════════════════════════════════════════════════════

    def get_open_orders(self, order_type: str = "BUY") -> list[dict]:
        """미체결 주문 목록 조회."""
        url   = f"{self.BASE_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
        tr_id = "TTTC8036R"
        params = {
            "CANO":           self._acc_no,
            "ACNT_PRDT_CD":   self._acc_prod,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1":    "1",
            "INQR_DVSN_2":    "0",
        }
        data = self._get(url, tr_id, params)
        if data.get("rt_cd") != "0":
            return []

        orders = []
        for item in data.get("output", []):
            otype = item.get("sll_buy_dvsn_cd", "")  # "01"=SELL, "02"=BUY
            if order_type == "BUY"  and otype != "02": continue
            if order_type == "SELL" and otype != "01": continue
            remaining = int(item.get("rmn_qty", 0) or 0)
            if remaining <= 0: continue
            orders.append({
                "order_no":  item.get("odno", ""),
                "code":      item.get("pdno", ""),
                "name":      item.get("prdt_name", ""),
                "qty":       int(item.get("ord_qty", 0) or 0),
                "remaining": remaining,
                "price":     int(item.get("ord_unpr", 0) or 0),
                "ord_dvsn":  item.get("ord_dvsn", ""),
                "type":      "BUY" if otype == "02" else "SELL",
            })
        return orders

    def cancel_order(self, order_no: str, code: str,
                     qty: int, ord_dvsn: str) -> dict:
        """미체결 주문 취소."""
        url   = f"{self.BASE_URL}/uapi/domestic-stock/v1/trading/order-rvsecncl"
        tr_id = "TTTC0803U"
        body  = {
            "CANO":         self._acc_no,
            "ACNT_PRDT_CD": self._acc_prod,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO":    order_no,
            "ORD_DVSN":     ord_dvsn,
            "RVSE_CNCL_DVSN_CD": "02",   # 02=취소
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     "0",
            "QTY_ALL_ORD_YN": "Y",
        }
        result = self._post(url, tr_id, body, use_hash=True)
        if result.get("rt_cd") == "0":
            logger.info(f"[취소✅] 주문번호={order_no} {code} qty={qty}")
        else:
            logger.warning(
                f"[취소실패] 주문번호={order_no} {code}: {result.get('msg1','?')}"
            )
        return result

    def cancel_all_buy_orders(self) -> int:
        """미체결 매수 주문 전량 취소. 취소 건수 반환."""
        orders  = self.get_open_orders("BUY")
        n_cancel = 0
        for o in orders:
            r = self.cancel_order(o["order_no"], o["code"],
                                  o["remaining"], o["ord_dvsn"])
            if r.get("rt_cd") == "0":
                n_cancel += 1
        if n_cancel:
            logger.info(f"[미체결취소] BUY {n_cancel}건 취소 완료")
        return n_cancel

    # ════════════════════════════════════════════════════════════
    # 6-2. 체결내역 조회 (당일)
    # ════════════════════════════════════════════════════════════

    def get_executed_orders(self, start_date: str = "", end_date: str = "") -> list:
        """
        당일 체결된 주문 목록 조회.
        API: TTTC8001R (주식 일별 주문 체결 조회)

        Returns:
            [{"order_no", "code", "name", "side", "qty", "price",
              "filled_qty", "filled_price", "filled_time", "status"}, ...]
        """
        from datetime import date as _date
        today = _date.today().strftime("%Y%m%d")
        if not start_date:
            start_date = today
        if not end_date:
            end_date = today

        url   = f"{self.BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        tr_id = "TTTC8001R"
        params = {
            "CANO":           self._acc_no,
            "ACNT_PRDT_CD":   self._acc_prod,
            "INQR_STRT_DT":   start_date,
            "INQR_END_DT":    end_date,
            "SLL_BUY_DVSN_CD": "00",   # 00=전체, 01=매도, 02=매수
            "INQR_DVSN":      "00",    # 00=역순
            "PDNO":           "",
            "CCLD_DVSN":      "01",    # 01=체결, 02=미체결, 00=전체 (→ 체결만)
            "ORD_GNO_BRNO":   "",
            "ODNO":           "",
            "INQR_DVSN_3":    "00",
            "INQR_DVSN_1":    "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        resp = self._get(url, tr_id, params)
        if resp.get("rt_cd") != "0":
            msg_cd = resp.get('msg_cd', '?')
            msg1   = resp.get('msg1',   '?')
            # OPSQ0002/OPSQ2001 = 장외시간 조회불가 (정상 폴백)
            if msg_cd in ("OPSQ0002", "OPSQ2001"):
                logger.debug(f"[KRBroker] 체결내역 조회 장외시간: {msg_cd} {msg1}")
            else:
                logger.warning(f"[KRBroker] 체결내역 조회 실패: msg_cd={msg_cd} | {msg1}")
            return []

        results = []
        for item in resp.get("output1", []):
            try:
                filled_qty = int(item.get("tot_ccld_qty", 0) or 0)
                if filled_qty <= 0:
                    continue
                side_cd = item.get("sll_buy_dvsn_cd", "")  # "01"=매도,"02"=매수
                side    = "BUY" if side_cd == "02" else "SELL"
                results.append({
                    "order_no":      item.get("odno",         ""),
                    "code":          item.get("pdno",         "").strip(),
                    "name":          item.get("prdt_name",    "").strip(),
                    "side":          side,
                    "qty":           int(item.get("ord_qty",  0) or 0),
                    "price":         int(float(item.get("ord_unpr",    0) or 0)),
                    "filled_qty":    filled_qty,
                    "filled_price":  int(float(item.get("avg_prvs",    0) or 0)),
                    "filled_time":   item.get("ord_tmd",      ""),   # HHMMSS
                    "filled_date":   item.get("ord_dt",       today),
                    "status":        "FILLED",
                    "market":        "KR",
                })
            except (ValueError, TypeError):
                continue
        return results

    # ════════════════════════════════════════════════════════════
    # 7. 호가단위 / 가격 보정
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def tick_size(price: int) -> int:
        """KRX 호가단위 반환."""
        if   price <     2_000: return 1
        elif price <     5_000: return 5
        elif price <    20_000: return 10
        elif price <    50_000: return 50
        elif price <   200_000: return 100
        elif price <   500_000: return 500
        else:                   return 1_000

    @classmethod
    def round_to_tick(cls, price: int, direction: int = 1) -> int:
        """
        호가단위에 맞게 가격 보정.
        direction: +1=올림(BUY), -1=내림(SELL)
        """
        if price <= 0:
            return 0
        tick = cls.tick_size(price)
        remainder = price % tick
        if remainder == 0:
            return price
        if direction >= 0:
            return price + (tick - remainder)   # 올림
        else:
            return price - remainder             # 내림
