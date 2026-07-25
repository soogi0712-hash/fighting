"""
한국투자증권 KIS Open API 연동 모듈
- OAuth2 토큰 발급 / 갱신
- 주식 현재가 조회 (국내 + 해외)
- 매수 / 매도 주문 (국내 + 해외)
- 잔고 조회 (국내 + 해외)
- 체결 내역 조회
- yfinance 폴백: KIS 500/403 에러 시 자동 대체
"""
import json
import time
import hashlib
import requests
import pytz
from datetime import datetime, timedelta
from utils.logger import get_logger
from config import Config

KST = pytz.timezone("Asia/Seoul")

# ── yfinance 폴백 캐시 ──────────────────────────────────────────
# KIS 500/403 에러 발생 종목: {symbol: (fail_count, last_fail_ts)}
_kis_fail_cache: dict = {}
_KIS_FAIL_THRESHOLD  = 3     # 연속 N회 실패 → yfinance 자동 전환
_KIS_COOLDOWN_SEC    = 1800  # 30분 쿨다운 후 KIS 재시도

logger = get_logger("KIS_API")


class KISApi:
    def __init__(self):
        self.app_key    = Config.KIS_APP_KEY
        self.app_secret = Config.KIS_APP_SECRET
        self.account_no = Config.KIS_ACCOUNT_NO
        self.base_url   = Config.BASE_URL

        self._access_token    = None
        self._token_expired   = None
        self._approval_key    = None   # 웹소켓 실시간용

        # ── 잔고 캐시 (최대 5분) ─────────────────────────────────
        self._balance_cache: dict = {}
        self._balance_cache_ts: float = 0.0
        self._BALANCE_CACHE_TTL: float = 300.0  # 5분 캐시 (루프에서 직접 관리)

        # ── 현재가 캐시 (종목별 3초) ──────────────────────────────
        # {code: (price_dict, timestamp)}
        self._price_cache: dict = {}
        self._PRICE_CACHE_TTL: float = 3.0   # 3초

        # ── OHLCV 캐시 (종목별 30초) ─────────────────────────────
        # {code: (candles, timestamp)}
        self._ohlcv_cache: dict = {}
        self._OHLCV_CACHE_TTL: float = 30.0  # 30초

        # ── TPS 제어 ─────────────────────────────────────────────
        # KIS 실전: 초당 20건 제한 → 최소 0.35초 간격 (여유 포함)
        self._last_api_call_ts: float = 0.0
        self._API_MIN_INTERVAL: float = 0.35  # 350ms → 초당 최대 ~2.8건

        # ── adaptive backoff 상태 ────────────────────────────────
        # EGW00201(TPS초과) / 500 연속 발생 시 동적으로 간격 늘림
        self._backoff_until: float = 0.0     # 이 시각까지 추가 대기
        self._consecutive_errors: int = 0    # 연속 에러 횟수

        # ── 주문 쿨다운 (종목별, 중복 주문 방지) ─────────────────
        # {code: last_order_ts}
        self._order_cooldown: dict = {}
        self._ORDER_COOLDOWN_SEC: float = 10.0  # 같은 종목 10초 쿨다운

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

    def _rate_limit(self):
        """
        ★ KIS TPS 공통 rate-limit + adaptive backoff
        - 기본 간격: 350ms (초당 최대 ~2.8건, KIS 20건 제한 대비 7배 여유)
        - EGW00201 / 500 연속 에러 시: 3s → 5s → 10s 단계 backoff
        - 쿨다운 중에는 추가 대기 후 진행
        """
        now = time.time()

        # ① adaptive backoff 중이면 대기
        if now < self._backoff_until:
            wait = self._backoff_until - now
            logger.debug(f"[RateLimit] backoff 대기 {wait:.1f}s")
            time.sleep(wait)

        # ② 최소 간격 보장
        elapsed = time.time() - self._last_api_call_ts
        if elapsed < self._API_MIN_INTERVAL:
            time.sleep(self._API_MIN_INTERVAL - elapsed)

        self._last_api_call_ts = time.time()

    def _on_api_success(self):
        """API 성공 시 에러 카운터 리셋"""
        self._consecutive_errors = 0

    def _on_api_error(self, status_code: int, msg_cd: str = "", msg1: str = ""):
        """
        API 에러 발생 시 adaptive backoff 설정.
        - EGW00201(TPS초과): 즉시 5초 backoff
        - 500 연속 3회:      10초 backoff
        - 500 최초/2회:      3초 backoff
        """
        self._consecutive_errors += 1
        n = self._consecutive_errors

        if msg_cd == "EGW00201":
            # TPS 초과: 즉각 5초 대기
            wait = 5.0
            logger.warning(
                f"[RateLimit] EGW00201 TPS초과 → {wait}s backoff "
                f"(연속 {n}회)"
            )
        elif status_code == 500:
            # 500 에러 단계별 backoff
            wait = 3.0 if n <= 2 else (5.0 if n <= 4 else 10.0)
            logger.warning(
                f"[RateLimit] 500에러 연속 {n}회 → {wait}s backoff"
            )
        else:
            wait = 1.0

        self._backoff_until = time.time() + wait

    def _diagnose_500(self, url: str, tr_id: str, resp) -> str:
        """
        500 Server Error 원인 진단 로그.
        단순 '서버문제'가 아닌 가능한 원인을 모두 출력.
        """
        try:
            body = resp.json()
            msg_cd = body.get("msg_cd", "")
            msg1   = body.get("msg1", "")
            rt_cd  = body.get("rt_cd", "")
        except Exception:
            msg_cd, msg1, rt_cd = "", "", ""

        acc = self.account_no
        is_real = "9443" in self.base_url or "openapi.koreainvestment.com" in self.base_url

        logger.error(
            f"[500진단] tr_id={tr_id} rt_cd={rt_cd} msg_cd={msg_cd} "
            f"msg1={msg1!r} url={url}"
        )
        # 원인 추정 목록 출력
        if msg_cd == "EGW00201":
            logger.error("  ▶ 원인: TPS 초과 (초당 20건 제한)")
        elif not msg_cd and not msg1:
            logger.error(
                f"  ▶ 원인 후보: "
                f"① 요청빈도초과 ② endpoint 오류 "
                f"③ 실전/모의 tr_id 불일치(현재 base_url={'실전' if is_real else '모의'}) "
                f"④ 계좌번호 오류(account_no={acc!r})"
            )
        elif "계좌" in msg1 or "account" in msg1.lower():
            logger.error(f"  ▶ 원인: 계좌번호/상품코드 오류 — account_no={acc!r}")
        elif "권한" in msg1 or "auth" in msg1.lower():
            logger.error(f"  ▶ 원인: 권한 없음 (모의투자 tr_id를 실전에 사용?) tr_id={tr_id}")
        else:
            logger.error(f"  ▶ 원인: 불명 (msg_cd={msg_cd!r}, msg1={msg1!r})")
        return msg_cd

    # ──────────────────────────────────────────────────────────
    # 2. 시세 조회
    # ──────────────────────────────────────────────────────────
    def get_current_price(self, stock_code: str) -> dict:
        """
        주식 현재가 조회 (3초 캐시 적용)
        같은 종목을 3초 내에 재조회하면 캐시값 반환 → KIS TPS 절감
        """
        # ── 캐시 확인 ───────────────────────────────────────────
        cached = self._price_cache.get(stock_code)
        if cached:
            data_c, ts_c = cached
            if time.time() - ts_c < self._PRICE_CACHE_TTL:
                return data_c

        url   = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        tr_id = "FHKST01010100"
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
        }
        try:
            self._rate_limit()
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            # 500 에러 진단
            if resp.status_code == 500:
                msg_cd = self._diagnose_500(url, tr_id, resp)
                self._on_api_error(500, msg_cd)
                return self._price_cache.get(stock_code, (None,))[0] or {}
            resp.raise_for_status()
            data   = resp.json()
            output = data.get("output", {})
            # rt_cd 확인 후 EGW00201 처리
            if data.get("rt_cd") != "0":
                msg_cd = data.get("msg_cd", "")
                if msg_cd == "EGW00201":
                    self._on_api_error(200, msg_cd, data.get("msg1", ""))
                return self._price_cache.get(stock_code, (None,))[0] or {}
            result = {
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
            self._price_cache[stock_code] = (result, time.time())
            self._on_api_success()
            return result
        except Exception as e:
            logger.error(f"현재가 조회 실패 {stock_code}: {e}")
            return self._price_cache.get(stock_code, (None,))[0] or {}

    def get_ohlcv(self, stock_code: str, period: str = "D",
                  count: int = 100) -> list[dict]:
        """
        일/주/월봉 조회 (30초 캐시 적용)
        period: D(일), W(주), M(월)
        실패 시 캐시된 이전 데이터 반환 → 이번 루프 SKIP 방지
        """
        cache_key = f"{stock_code}_{period}_{count}"
        cached = self._ohlcv_cache.get(cache_key)
        if cached:
            data_c, ts_c = cached
            if time.time() - ts_c < self._OHLCV_CACHE_TTL:
                return data_c

        url   = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
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
            self._rate_limit()
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            if resp.status_code == 500:
                msg_cd = self._diagnose_500(url, tr_id, resp)
                self._on_api_error(500, msg_cd)
                # 캐시 있으면 반환, 없으면 빈 리스트 → 호출자가 SKIP 처리
                old = self._ohlcv_cache.get(cache_key)
                return old[0] if old else []
            resp.raise_for_status()
            data   = resp.json()
            if data.get("rt_cd") != "0":
                msg_cd = data.get("msg_cd", "")
                if msg_cd == "EGW00201":
                    self._on_api_error(200, msg_cd, data.get("msg1", ""))
                old = self._ohlcv_cache.get(cache_key)
                return old[0] if old else []
            output  = data.get("output2", [])
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
            result = candles[-count:]
            if result:
                self._ohlcv_cache[cache_key] = (result, time.time())
                self._on_api_success()
            return result
        except Exception as e:
            logger.error(f"OHLCV 조회 실패 {stock_code}: {e}")
            old = self._ohlcv_cache.get(cache_key)
            return old[0] if old else []

    def get_intraday_5min(self, stock_code: str, count: int = 12) -> list[dict]:
        """
        국내주식 5분봉 조회 (FHKST03010200 — 분봉)
        반환: [{time, open, high, low, close, volume}, ...] 최신 순 정렬(오래된→최신)
        count: 가져올 봉 수 (최대 30, 기본 12 = 1시간)
        캐시: 60초
        """
        cache_key = f"5min_{stock_code}"
        cached = self._ohlcv_cache.get(cache_key)
        if cached:
            data_c, ts_c = cached
            if time.time() - ts_c < 60:   # 60초 캐시
                return data_c

        url   = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        tr_id = "FHKST03010200"
        now_str = datetime.now().strftime("%H%M%S")
        params = {
            "fid_etc_cls_code":      "",
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd":        stock_code,
            "fid_input_hour_1":      now_str,
            "fid_pw_data_incu_yn":   "N",  # 과거 데이터 포함 여부
        }
        try:
            self._rate_limit()
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=8)
            if resp.status_code == 500:
                logger.warning(f"[5분봉] {stock_code} 500 오류 — 캐시 반환")
                old = self._ohlcv_cache.get(cache_key)
                return old[0] if old else []
            resp.raise_for_status()
            data = resp.json()
            if data.get("rt_cd") != "0":
                old = self._ohlcv_cache.get(cache_key)
                return old[0] if old else []
            output = data.get("output2", [])
            candles = []
            for row in output[:count]:
                candles.append({
                    "time":   row.get("stck_cntg_hour", ""),
                    "open":   int(row.get("stck_oprc", 0) or 0),
                    "high":   int(row.get("stck_hgpr", 0) or 0),
                    "low":    int(row.get("stck_lwpr", 0) or 0),
                    "close":  int(row.get("stck_prpr", 0) or 0),
                    "volume": int(row.get("cntg_vol", 0) or 0),
                })
            # output2는 최신→과거 순 → 뒤집어서 오래된→최신 순으로
            candles = list(reversed(candles))
            if candles:
                self._ohlcv_cache[cache_key] = (candles, time.time())
            return candles
        except Exception as e:
            logger.warning(f"[5분봉] {stock_code} 조회 실패: {e}")
            old = self._ohlcv_cache.get(cache_key)
            return old[0] if old else []

    @staticmethod
    def tick_size(price: int) -> int:
        """
        KIS 국내주식 호가 단위 (2023년 기준)
        https://securities.koreainvestment.com/main/bond/regulate/TF04ae010100P0.jsp
        """
        if price < 1_000:      return 1
        if price < 5_000:      return 5
        if price < 10_000:     return 10
        if price < 50_000:     return 50
        if price < 100_000:    return 100
        if price < 500_000:    return 500
        return 1_000

    @staticmethod
    def round_to_tick(price: int, direction: int = 1) -> int:
        """
        호가 단위로 반올림.
        direction: +1 = 올림(매수 공격적), -1 = 내림(매도), 0 = 반올림
        """
        if price <= 0:
            return price
        tick = KISApi.tick_size(price)
        if direction >= 1:
            return ((price + tick - 1) // tick) * tick   # 올림
        elif direction <= -1:
            return (price // tick) * tick                 # 내림
        else:
            return round(price / tick) * tick             # 반올림

    # ──────────────────────────────────────────────────────────
    # 3. 주문
    # ──────────────────────────────────────────────────────────
    # ── 유효 ORD_DVSN + ORD_UNPR 조합표 ─────────────────────────────
    # (ord_dvsn, price > 0): True = 유효
    _VALID_ORD_COMBO = {
        ("00", True):  True,   # 지정가: 가격 필수
        ("00", False): False,  # 지정가: 가격 0이면 오류
        ("01", False): True,   # 시장가: 가격 0 필수
        ("01", True):  True,   # 시장가: KIS는 가격 있어도 허용 (무시)
        ("05", True):  True,   # 장전시간외: 전일종가 필수
        ("05", False): False,  # 장전시간외: 가격 0이면 오류
        ("06", False): True,   # 장후시간외: 반드시 0 (KIS 자동)
        ("06", True):  False,  # 장후시간외: 가격 넘기면 IGW00007
    }

    def _pre_validate_kr_order(
        self,
        stock_code: str,
        order_type: str,
        qty: int,
        ord_dvsn: str,
        order_price: int,
        acc_no: str,
    ) -> dict | None:
        """
        국내장 주문 전 사전 검증 4가지.
        문제 없으면 None 반환, 차단 시 {"rt_cd": "9", "msg1": "..."} 반환.

        ① 현재 시간이 신규매수 허용 시간인지 (매수 전용)
        ② ORD_DVSN / ORD_UNPR 조합 유효성
        ③ TPS 제한(backoff) 상태 확인
        ④ 수량 > 0 기본 유효성
        """
        now_kst = datetime.now(KST)
        kst_str = now_kst.strftime("%Y-%m-%d %H:%M:%S")

        # ① 매수 시간 체크 (BUY 전용)
        if order_type == "BUY":
            _t = now_kst.time()
            # 09:00 ~ 15:20 사이만 신규매수 허용 (장전시간외 05 제외)
            from datetime import time as _time
            _open  = _time(9, 0)
            _close = _time(15, 20)
            # 05(장전시간외)는 08:30~09:00 구간이므로 허용
            if ord_dvsn not in ("05", "06"):
                if not (_open <= _t <= _close):
                    msg = (
                        f"[주문사전검증-①시간] BUY 차단 | 종목={stock_code} | "
                        f"현재KST={kst_str} | 신규매수허용시간=09:00~15:20 | "
                        f"ORD_DVSN={ord_dvsn}"
                    )
                    logger.warning(msg)
                    return {"rt_cd": "9", "msg1": f"매수 불가 시간대 ({kst_str})"}

        # ② ORD_DVSN / ORD_UNPR 조합 유효성
        has_price = order_price > 0
        combo_ok  = self._VALID_ORD_COMBO.get((ord_dvsn, has_price), True)
        if not combo_ok:
            msg = (
                f"[주문사전검증-②ORD조합] 오류 | 종목={stock_code} | "
                f"ORD_DVSN={ord_dvsn} | ORD_UNPR={order_price} | "
                f"has_price={has_price} → 유효하지 않은 조합"
            )
            logger.error(msg)
            return {
                "rt_cd": "9",
                "msg1":  f"ORD_DVSN={ord_dvsn} + ORD_UNPR={order_price} 조합 오류",
            }

        # ③ TPS 제한(backoff) 상태 확인
        backoff_remain = self._backoff_until - time.time()
        if backoff_remain > 0:
            msg = (
                f"[주문사전검증-③TPS] backoff 중 | 종목={stock_code} | "
                f"남은대기={backoff_remain:.1f}s | 연속에러={self._consecutive_errors}회 | "
                f"현재KST={kst_str}"
            )
            logger.warning(msg)
            # backoff 중에는 대기 후 진행 (차단하지 않음 — rate_limit()가 처리)
            # 단, 10초 초과 시에는 차단
            if backoff_remain > 10:
                return {
                    "rt_cd": "9",
                    "msg1":  f"TPS backoff 중 ({backoff_remain:.0f}s 남음) — 주문 스킵",
                }

        # ④ 수량 기본 유효성
        if qty <= 0:
            msg = (
                f"[주문사전검증-④수량] 오류 | 종목={stock_code} | "
                f"qty={qty} ≤ 0 | {order_type}"
            )
            logger.error(msg)
            return {"rt_cd": "9", "msg1": f"주문수량 오류 qty={qty}"}

        return None  # 모든 검증 통과

    # ══════════════════════════════════════════════════════════
    # 실주문 전역 킬스위치 + 주문가능현금 조회
    # ══════════════════════════════════════════════════════════
    def _live_order_guard(self, desc: str) -> dict | None:
        """LIVE_ORDER_ENABLED=false 이면 실주문 API 호출 없이 dry-run 응답 반환.

        반환값이 None 이 아니면 호출부는 즉시 그 dict 를 반환해야 한다
        (requests.post 등 실주문 네트워크 호출에 절대 도달하지 않음).
        rt_cd='9' 이므로 다운스트림은 '미체결'로 처리 → 포지션/손익 오변경 없음.
        """
        if not Config.LIVE_ORDER_ENABLED:
            logger.warning(f"🚫 [LIVE_ORDER_ENABLED=false] 실주문 미제출(dry-run): {desc}")
            return {"rt_cd": "9",
                    "msg1": "LIVE_ORDER_ENABLED=false — 주문 미제출(dry-run)",
                    "_dry_run": True, "_live_disabled": True}
        return None

    def get_orderable_cash(self) -> float:
        """실제 주문가능현금(ord_psbl_cash, 원). 조회 실패 시 -1.

        inquire-psbl-order(TTTC8908R)의 ord_psbl_cash 를 우선 사용한다
        (예수금 dnca_tot_amt 와 달리 미체결 예약금·정산을 반영한 값)."""
        return float(self._get_cash_from_psbl_api())

    def _reject_if_cash_exceeded(self, stock_code: str, qty: int,
                                 price: float) -> dict | None:
        """BUY 지정가 총주문금액(매수수수료 포함)이 주문가능현금을 초과하면
        차단 dict 반환, 아니면 None. (신용·미수 미사용 — 현금 초과 원천 차단)"""
        try:
            from screener.transaction_cost import BUY_COMMISSION_RATE as _comm
        except Exception:
            _comm = 0.00015
        orderable = self.get_orderable_cash()
        if orderable is None or orderable < 0:
            # 조회 실패 → 사이징 계층 방어에 위임(차단하지 않되 경고)
            logger.warning(
                f"[현금가드] 주문가능현금 조회 실패 — 사이징 방어에 위임 ({stock_code})")
            return None
        order_amt = price * qty * (1 + _comm)
        if order_amt > orderable:
            logger.error(
                f"🚫 [현금초과 차단] {stock_code} 주문금액 {order_amt:,.0f}원 > "
                f"주문가능현금 {orderable:,.0f}원 — 미수 방지 위해 제출 차단")
            return {"rt_cd": "9",
                    "msg1": (f"현금초과 차단(주문 {order_amt:,.0f}원 > "
                             f"가능 {orderable:,.0f}원)"),
                    "_cash_guard": True}
        return None

    def _order(self, stock_code: str, order_type: str,
               qty: int, price: int = 0,
               ord_dvsn: str = None) -> dict:
        """
        국내주식 매수/매도 주문
        order_type : BUY | SELL
        price      : 0 이면 시장가
        ord_dvsn   : 00 지정가 / 01 시장가 / 05 장전시간외 / 06 장후시간외
                     None → price 값으로 자동 선택
        ★ 쿨다운: 같은 종목 10초 내 중복 주문 차단
        ★ 사전검증: 시간/ORD조합/TPS/수량 4가지 체크
        ★ 상세 로그: tr_id / rt_cd / msg_cd / msg1 모두 출력
        ★ adaptive backoff: 500/EGW00201 발생 시 자동 간격 조정
        ★ 500 재시도: 3초 대기 → 10초 대기 → 종목 스킵 (즉시재시도 없음)
        """
        # ── 실주문 킬스위치 (LIVE_ORDER_ENABLED=false → 미제출) ──
        _guard = self._live_order_guard(f"{order_type} {stock_code} {qty}주 @{price}")
        if _guard is not None:
            return _guard

        # ── 현금초과(미수) 사전 차단 — BUY 지정가에서만 검증 가능 ──
        # 신용·미수 미사용: 총 주문금액이 실제 주문가능현금을 초과하면 제출 차단.
        if order_type == "BUY" and price and price > 0 and qty and qty > 0:
            _blocked = self._reject_if_cash_exceeded(stock_code, qty, price)
            if _blocked is not None:
                return _blocked

        # ── 중복 주문 쿨다운 체크 ────────────────────────────────
        last_order_ts = self._order_cooldown.get(stock_code, 0)
        elapsed_since_order = time.time() - last_order_ts
        if elapsed_since_order < self._ORDER_COOLDOWN_SEC:
            remain = self._ORDER_COOLDOWN_SEC - elapsed_since_order
            logger.warning(
                f"[쿨다운] {stock_code} {order_type} 주문 차단 "
                f"— 직전 주문 후 {elapsed_since_order:.1f}s 경과 "
                f"(쿨다운 {remain:.1f}s 남음)"
            )
            return {"rt_cd": "9", "msg1": f"쿨다운 중 ({remain:.1f}s 남음)"}

        url   = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        from config import Config as _cfg
        if _cfg.KIS_IS_REAL:
            tr_id = "TTTC0012U" if order_type == "BUY" else "TTTC0011U"
        else:
            tr_id = "VTTC0012U" if order_type == "BUY" else "VTTC0011U"

        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")

        if ord_dvsn is None:
            ord_dvsn = "01" if price == 0 else "00"

        # ── 시간외 ORD_UNPR 강제 보정 ─────────────────────────────
        # KIS 규칙:
        #   05(장전시간외) → ORD_UNPR = 전일종가 (price > 0 필수)
        #   06(장후시간외) → ORD_UNPR = "0" 고정 (KIS가 당일종가 자동 적용)
        # ORD_DVSN=06인데 가격을 넘기면 IGW00007 발생!
        if ord_dvsn == "06":
            order_price = 0   # 장후 시간외: 반드시 0 (종가 자동)
        elif ord_dvsn == "05":
            order_price = price if price > 0 else 0  # 장전 시간외: 전일종가 전달
        elif ord_dvsn == "01":
            # 시장가: KIS는 ORD_UNPR=0 필수
            order_price = 0
        else:
            order_price = price  # 정규장 지정가(00): 호출자가 넘긴 값

        # ── ★ [ORDER PRICE CHECK] 주문가격 검증 및 보정 ─────────
        _orig_price   = order_price
        _tick         = self.tick_size(order_price) if order_price > 0 else 0
        _corrected    = False
        _block_reason = ""

        if ord_dvsn == "00":
            # 지정가: 가격 0 → 절대 금지 (KIS 500 원인)
            if order_price <= 0:
                _block_reason = f"지정가(00) ORD_UNPR={order_price} ≤ 0 — 주문 차단"
            else:
                # 호가단위 정렬 (매수=올림, 매도=내림)
                aligned = self.round_to_tick(order_price,
                                             direction=1 if order_type == "BUY" else -1)
                if aligned != order_price:
                    order_price = aligned
                    _corrected = True
        elif ord_dvsn == "01":
            # 시장가: ORD_UNPR ≠ 0 이면 0으로 강제 보정
            if order_price != 0:
                order_price = 0
                _corrected = True
        elif ord_dvsn == "06" and order_type == "BUY":
            # 장후시간외 신규매수 금지
            _block_reason = "장후시간외(06) 신규 매수 금지"

        # [ORDER PRICE CHECK] 로그
        try:
            from utils.market_session import session_info as _sf
            _sess_label = _sf().get("session", "?")
        except Exception:
            _sess_label = "?"
        logger.info(
            f"[ORDER PRICE CHECK] "
            f"종목={stock_code} | {'매수' if order_type=='BUY' else '매도'} | "
            f"세션={_sess_label} | "
            f"ORD_DVSN={ord_dvsn} | "
            f"ORD_UNPR(원본)={_orig_price} | "
            f"현재가≈{_orig_price} | "
            f"호가단위={_tick} | "
            f"보정가격={order_price if _corrected else '없음'} | "
            f"결과={'⛔차단:'+_block_reason if _block_reason else ('✅보정완료' if _corrected else '✅통과')}"
        )

        if _block_reason:
            logger.error(f"[ORDER PRICE CHECK] ⛔ 주문 차단 — {_block_reason} | 종목={stock_code}")
            return {
                "rt_cd": "9",
                "msg1":  _block_reason,
                "_http_status": "BLOCKED",
                "_response_body": _block_reason,
            }

        # ══════════════════════════════════════════════════════
        # ★ 국내장 주문 전 사전 검증 4가지
        # ══════════════════════════════════════════════════════
        _pre_err = self._pre_validate_kr_order(
            stock_code, order_type, qty, ord_dvsn, order_price, acc_no
        )
        if _pre_err is not None:
            return _pre_err

        body = {
            "CANO":         acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "PDNO":         stock_code,
            "ORD_DVSN":     ord_dvsn,
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     str(order_price) if order_price > 0 else "0",
        }

        # ── 세션 정보 (로그용) ────────────────────────────────
        try:
            from utils.market_session import session_info as _sess_fn
            _sess = _sess_fn().get("session", "?")
        except Exception:
            _sess = "?"
        _kst_now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        _order_amt = order_price * qty  # 주문금액 (시장가=0이면 0)

        logger.info(
            f"[주문요청] {order_type} | tr_id={tr_id} | "
            f"종목={stock_code} | 시장=국내 | 현재KST={_kst_now} | 세션={_sess} | "
            f"ORD_DVSN={ord_dvsn} | ORD_UNPR={order_price}원 | "
            f"주문수량={qty}주 | 주문금액={_order_amt:,}원 | "
            f"계좌번호={acc_no}-{acc_prod}"
        )

        # ── 500 재시도 대기 시간: attempt 0→3초, attempt 1→10초, attempt 2→스킵
        _500_waits = [3, 10]

        # ── retry (최대 2회 재시도, 총 3회 시도) ────────────────
        for attempt in range(3):
            try:
                self._rate_limit()
                resp = requests.post(
                    url,
                    headers=self._headers(tr_id, use_hash=True, body=body),
                    json=body, timeout=10,
                )

                # ── 500 에러 → 상세 로그 + 단계별 대기 재시도 ──────
                if resp.status_code == 500:
                    # 500 응답 body 파싱
                    try:
                        _body500 = resp.json()
                        _msg_cd  = _body500.get("msg_cd", "")
                        _msg1    = _body500.get("msg1", "")
                        _rt_cd   = _body500.get("rt_cd", "")
                    except Exception:
                        _body500 = {}
                        _msg_cd, _msg1, _rt_cd = "", "", ""

                    logger.error(
                        f"[KIS 주문실패 상세]\n"
                        f"  종목={stock_code} | 시장=국내 | "
                        f"현재KST={datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} | "
                        f"세션={_sess}\n"
                        f"  ORD_DVSN={ord_dvsn} | ORD_UNPR={order_price}원 | "
                        f"주문수량={qty}주 | 주문금액={_order_amt:,}원\n"
                        f"  계좌번호={acc_no}-{acc_prod} | tr_id={tr_id} | "
                        f"HTTP status={resp.status_code}\n"
                        f"  KIS response body={resp.text[:500]}\n"
                        f"  msg_cd={_msg_cd!r} | msg1={_msg1!r} | rt_cd={_rt_cd!r}"
                    )

                    self._diagnose_500(url, tr_id, resp)
                    self._on_api_error(500, _msg_cd)

                    if attempt < 2:
                        _wait = _500_waits[attempt]
                        logger.warning(
                            f"[500재시도] {stock_code} → {_wait}초 대기 후 재시도 "
                            f"(attempt {attempt + 1}/2)"
                        )
                        time.sleep(_wait)
                        continue

                    # 2회 재시도 후에도 실패 → 스킵
                    logger.error(
                        f"[500스킵] {stock_code} — 2회 재시도 후에도 500 지속 → 해당 종목 주문 스킵"
                    )
                    return {
                        "rt_cd":            "9",
                        "msg1":             "500 Server Error (재시도 2회 후 종목 스킵)",
                        "msg_cd":           _msg_cd,
                        "_http_status":     500,
                        "_response_body":   resp.text[:500],
                        "_ord_dvsn":        ord_dvsn,
                        "_ord_unpr":        order_price,
                        "_sess":            _sess,
                        "_kst":             datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
                        "_tr_id":           tr_id,
                        "_acc":             f"{acc_no}-{acc_prod}",
                    }

                resp.raise_for_status()
                result = resp.json()
                rt_cd  = result.get("rt_cd", "")
                msg_cd = result.get("msg_cd", "")
                msg1   = result.get("msg1", "")

                if rt_cd == "0":
                    logger.info(
                        f"✅ {order_type} 주문 성공 | tr_id={tr_id} "
                        f"| {stock_code} {qty}주 {price}원 "
                        f"| rt_cd={rt_cd} msg_cd={msg_cd}"
                    )
                    self._order_cooldown[stock_code] = time.time()
                    self._on_api_success()
                    return result
                else:
                    # EGW00201: TPS 초과 → backoff 후 재시도
                    if msg_cd == "EGW00201":
                        self._on_api_error(200, msg_cd, msg1)
                        logger.warning(
                            f"주문 EGW00201 TPS초과 {stock_code} "
                            f"(재시도 {attempt+1}/3)"
                        )
                        if attempt < 2:
                            continue
                    # 주문 실패 상세 로그
                    _kst_fail = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
                    logger.error(
                        f"[KIS 주문실패 상세]\n"
                        f"  종목={stock_code} | 시장=국내 | "
                        f"현재KST={_kst_fail} | "
                        f"세션={_sess}\n"
                        f"  ORD_DVSN={ord_dvsn} | ORD_UNPR={order_price}원 | "
                        f"주문수량={qty}주 | 주문금액={_order_amt:,}원\n"
                        f"  계좌번호={acc_no}-{acc_prod} | tr_id={tr_id} | "
                        f"HTTP status={resp.status_code}\n"
                        f"  KIS response body={resp.text[:500]}\n"
                        f"  msg_cd={msg_cd!r} | msg1={msg1!r} | rt_cd={rt_cd!r}"
                    )
                    # 웹 화면 전달용 상세 필드를 result에 병합
                    result["_http_status"]   = resp.status_code
                    result["_response_body"] = resp.text[:500]
                    result["_ord_dvsn"]      = ord_dvsn
                    result["_ord_unpr"]      = order_price
                    result["_sess"]          = _sess
                    result["_kst"]           = _kst_fail
                    result["_tr_id"]         = tr_id
                    result["_acc"]           = f"{acc_no}-{acc_prod}"
                    return result

            except Exception as e:
                if attempt < 2:
                    wait = (attempt + 1) * 2.0
                    logger.warning(
                        f"주문 오류 {stock_code} (재시도 {attempt+1}/3) "
                        f"tr_id={tr_id}: {e} → {wait:.1f}초 대기"
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        f"주문 오류 {stock_code} (3회 실패) "
                        f"tr_id={tr_id}: {e}"
                    )
                    return {
                        "rt_cd":          "9",
                        "msg1":           str(e),
                        "_http_status":   "Exception",
                        "_response_body": str(e),
                        "_ord_dvsn":      ord_dvsn,
                        "_ord_unpr":      order_price,
                        "_sess":          _sess,
                        "_kst":           datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
                        "_tr_id":         tr_id,
                        "_acc":           f"{acc_no}-{acc_prod}",
                    }

    def buy(self, stock_code: str, qty: int, price: int = 0,
           ord_dvsn: str = None) -> dict:
        return self._order(stock_code, "BUY", qty, price, ord_dvsn)

    def sell(self, stock_code: str, qty: int, price: int = 0,
            ord_dvsn: str = None) -> dict:
        return self._order(stock_code, "SELL", qty, price, ord_dvsn)

    # ──────────────────────────────────────────────────────────
    # 3-1. 미체결 주문 조회 + 취소
    # ──────────────────────────────────────────────────────────
    def get_open_orders(self, order_type: str = "BUY") -> list:
        """
        국내 미체결 주문 조회 (TTTC8036R)
        order_type: "BUY" → 매수 미체결만, "SELL" → 매도 미체결만, "ALL" → 전체
        반환: [{"order_no": str, "stock_code": str, "stock_name": str,
                "ord_qty": int, "ord_unpr": int, "ord_dvsn": str,
                "ord_dvsn_name": str, "ord_time": str}, ...]
        """
        url   = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
        from config import Config as _cfg
        if not _cfg.KIS_IS_REAL:
            logger.warning(
                "[미체결조회] 모의투자 환경에서는 inquire-psbl-rvsecncl 미지원"
                " — 빈 리스트 반환"
            )
            return []
        tr_id = "TTTC0084R"
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")

        # ORD_DVSN 필터: 00=매도, 01=매수
        buy_sell_dvsn = "02" if order_type == "BUY" else ("01" if order_type == "SELL" else "00")

        params = {
            "CANO":           acc_no,
            "ACNT_PRDT_CD":   acc_prod,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1":    "",
            "INQR_DVSN_2":    buy_sell_dvsn,
        }
        try:
            self._rate_limit()
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("output", []) or []
            result = []
            for item in items:
                # sll_buy_dvsn_cd: 01=매도, 02=매수
                sll_buy = item.get("sll_buy_dvsn_cd", "")
                if order_type == "BUY" and sll_buy != "02":
                    continue
                if order_type == "SELL" and sll_buy != "01":
                    continue
                result.append({
                    "order_no":    item.get("odno", ""),
                    "stock_code":  item.get("pdno", ""),
                    "stock_name":  item.get("prdt_name", ""),
                    "ord_qty":     int(item.get("ord_qty", 0)),
                    "unexec_qty":  int(item.get("rmn_qty", 0)),   # 미체결 잔여 수량
                    "ord_unpr":    int(item.get("ord_unpr", 0)),
                    "ord_dvsn":    item.get("ord_dvsn", ""),
                    "ord_dvsn_name": item.get("ord_dvsn_name", ""),
                    "ord_time":    item.get("ord_tmd", ""),        # HHMMSS
                    "sll_buy_dvsn_cd": sll_buy,
                })
            self._on_api_success()
            return result
        except Exception as e:
            logger.error(f"미체결 조회 실패: {e}")
            return []

    def cancel_order(self, order_no: str, stock_code: str,
                     unexec_qty: int, ord_unpr: int,
                     ord_dvsn: str = "00") -> dict:
        """
        국내 주문 취소 (TTTC0803U)
        order_no  : 주문번호 (odno)
        stock_code: 종목코드
        unexec_qty: 미체결 잔여 수량 (전량 취소)
        ord_unpr  : 주문단가 (취소 시에도 원래 주문가 입력)
        ord_dvsn  : 주문구분 (원래 주문과 동일하게)
        반환: KIS API 응답 dict (rt_cd=="0" 이면 취소 성공)
        """
        _guard = self._live_order_guard(f"CANCEL {stock_code} odno={order_no}")
        if _guard is not None:
            return _guard
        url   = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
        from config import Config as _cfg
        tr_id = "TTTC0013U" if _cfg.KIS_IS_REAL else "VTTC0013U"
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")

        body = {
            "CANO":         acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "KRX_FWDG_ORD_ORGNO": "",     # 한국거래소전송주문조직번호 (공백)
            "ORGN_ODNO":    order_no,       # 원주문번호
            "ORD_DVSN":     ord_dvsn,
            "RVSE_CNCL_DVSN_CD": "02",    # 02=취소
            "ORD_QTY":      str(unexec_qty),
            "ORD_UNPR":     str(ord_unpr) if ord_unpr > 0 else "0",
            "PDNO":         stock_code,
            "QTY_ALL_ORD_YN": "Y",        # 전량 취소
        }

        kst_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            f"[주문취소] 종목={stock_code} | 주문번호={order_no} | "
            f"미체결수량={unexec_qty}주 | ORD_DVSN={ord_dvsn} | KST={kst_str}"
        )
        try:
            self._rate_limit()
            resp = requests.post(
                url,
                headers=self._headers(tr_id, use_hash=True, body=body),
                json=body, timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()
            rt_cd  = result.get("rt_cd", "")
            msg_cd = result.get("msg_cd", "")
            msg1   = result.get("msg1", "")
            if rt_cd == "0":
                logger.info(
                    f"✅ 주문취소 성공 | 종목={stock_code} 주문번호={order_no} "
                    f"{unexec_qty}주 | msg_cd={msg_cd}"
                )
                self._on_api_success()
            else:
                logger.warning(
                    f"⚠️ 주문취소 실패 | 종목={stock_code} 주문번호={order_no} "
                    f"rt_cd={rt_cd} msg_cd={msg_cd} msg1={msg1!r}"
                )
            return result
        except Exception as e:
            logger.error(f"주문취소 오류 {stock_code} 주문번호={order_no}: {e}")
            return {"rt_cd": "9", "msg1": str(e)}

    # ──────────────────────────────────────────────────────────
    # 4. 잔고 조회
    # ──────────────────────────────────────────────────────────
    def _get_cash_from_psbl_api(self) -> int:
        """
        ★ 예수금 조회 전용 API (TTTC8908R) — inquire-balance 500에러 우회용
        성공 시 주문 가능 현금(원) 반환, 실패 시 -1 반환
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        from config import Config as _cfg
        tr_id = "TTTC8908R" if _cfg.KIS_IS_REAL else "VTTC8908R"
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")
        params = {
            "CANO":         acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "PDNO":         "005930",  # 삼성전자 더미 (필수 파라미터)
            "ORD_UNPR":     "0",
            "ORD_DVSN":     "01",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }
        try:
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            cash = int(data.get("output", {}).get("ord_psbl_cash", -1))
            if cash >= 0:
                logger.info(f"💰 예수금 조회 성공 (주문가능: {cash:,}원)")
            return cash
        except Exception as e:
            logger.warning(f"예수금 조회 실패: {e}")
            return -1

    def get_balance(self) -> dict:
        """보유 주식 및 예수금 조회
        ★ KIS 500 에러 시 캐시된 직전 성공값 반환 (cash=0 SKIP 방지)
        ★ 캐시도 없으면 예수금 전용 API로 cash만 가져와서 합성 반환
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        from config import Config as _cfg
        tr_id = "TTTC8434R" if _cfg.KIS_IS_REAL else "VTTC8434R"
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
        # ── retry 3회 (adaptive backoff) ───────────────────────
        for attempt in range(3):
            try:
                self._rate_limit()
                resp = requests.get(url, headers=self._headers(tr_id),
                                    params=params, timeout=10)
                # 500 에러 진단
                if resp.status_code == 500:
                    msg_cd = self._diagnose_500(url, tr_id, resp)
                    self._on_api_error(500, msg_cd)
                    if attempt < 2:
                        logger.warning(f"잔고 500에러 (재시도 {attempt+1}/3)")
                        continue
                    break  # 3회 실패 → 캐시/폴백으로
                resp.raise_for_status()
                data = resp.json()
                # EGW00201 처리
                if data.get("rt_cd") != "0":
                    msg_cd = data.get("msg_cd", "")
                    msg1   = data.get("msg1", "")
                    if msg_cd == "EGW00201":
                        self._on_api_error(200, msg_cd, msg1)
                        if attempt < 2:
                            continue
                    logger.warning(
                        f"잔고조회 rt_cd={data.get('rt_cd')} "
                        f"msg_cd={msg_cd} msg1={msg1!r}"
                    )
                    break
                holdings = []
                for item in data.get("output1", []):
                    qty = int(item.get("hldg_qty", 0))
                    if qty > 0:
                        pnl_pct = float(item.get("evlu_pfls_rt", 0))
                        pnl_amt = int(item.get("evlu_pfls_amt", 0))
                        holdings.append({
                            "code":       item.get("pdno", ""),
                            "name":       item.get("prdt_name", ""),
                            "qty":        qty,
                            "avg_price":  float(item.get("pchs_avg_pric", 0)),
                            "cur_price":  int(item.get("prpr", 0)),
                            "pnl_pct":    pnl_pct,
                            "pnl_amt":    pnl_amt,
                            "profit_pct": pnl_pct,
                            "profit_amt": pnl_amt,
                        })
                summary = data.get("output2", [{}])[0]
                # ── output2 필드 설명 (KIS TTTC8434R) ──────────────
                # tot_evlu_amt       : 총평가금액 = 보유평가+예수금+미체결정산 ★ MTS 총자산
                # scts_evlu_amt      : 유가증권 평가금액 (보유종목 평가액만)
                # dnca_tot_amt       : 예수금 총금액 (주문가능현금)
                # prvs_rcdl_excc_amt : 전일매도정산금 (미체결→정산 대기)
                # evlu_pfls_smtl_amt : 평가손익 합계 (보유 종목 기준)
                # tot_evlu_pfls_rt   : 총평가손익률
                # pchs_amt_smtl_amt  : 매입금액 합계 (보유 종목 매입 총액)
                # ──────────────────────────────────────────────────
                scts_evlu     = int(summary.get("scts_evlu_amt",      0))   # 보유종목 평가금액
                cash_amt      = int(summary.get("dnca_tot_amt",       0))   # 예수금(주문가능현금)
                prvs_rcdl     = int(summary.get("prvs_rcdl_excc_amt", 0))   # 전일매도정산금
                nxdy_excc     = int(summary.get("nxdy_excc_amt",      0))   # 익일정산금
                pchs_amt      = int(summary.get("pchs_amt_smtl_amt",  0))   # 매입금액 합계
                evlu_pfls     = int(summary.get("evlu_pfls_smtl_amt", 0))   # 평가손익 합계
                tot_evlu      = int(summary.get("tot_evlu_amt",       0))   # 총평가금액(MTS 총자산)
                result = {
                    "holdings":          holdings,
                    "total_eval":        tot_evlu,       # MTS 총자산과 동일
                    "cash":              cash_amt,       # 예수금(주문가능현금)
                    "scts_eval":         scts_evlu,      # 보유종목 평가금액
                    "prev_sell_settle":  prvs_rcdl,      # 전일매도정산금
                    "next_day_settle":   nxdy_excc,      # 익일정산금
                    "purchase_amt":      pchs_amt,       # 매입금액 합계
                    "total_profit":      evlu_pfls,      # 평가손익 합계
                    "total_profit_pct":  float(summary.get("tot_evlu_pfls_rt", 0)),
                }
                if result["cash"] > 0:
                    self._balance_cache    = result
                    self._balance_cache_ts = time.time()
                self._on_api_success()
                return result
            except Exception as e:
                if attempt < 2:
                    wait = (attempt + 1) * 1.5
                    logger.warning(
                        f"잔고 조회 실패(재시도 {attempt+1}/3) "
                        f"tr_id={tr_id}: {e} → {wait:.1f}초 대기"
                    )
                    time.sleep(wait)
                else:
                    logger.error(f"잔고 조회 실패(3회 모두) tr_id={tr_id}: {e}")
        # ★ 3회 모두 실패 → 1) 캐시 반환 2) 예수금API 3) 기본값
        if self._balance_cache and (time.time() - self._balance_cache_ts) < 300:
            logger.warning(f"⚠️ 잔고 조회 실패 → 캐시값 사용 (cash={self._balance_cache.get('cash',0):,}원)")
            return self._balance_cache
        # ★ 캐시 없음 → 예수금 전용 API 시도
        psbl_cash = self._get_cash_from_psbl_api()
        if psbl_cash >= 0:
            synth = {"holdings": [], "total_eval": 0, "cash": psbl_cash,
                     "total_profit": 0, "total_profit_pct": 0}
            self._balance_cache    = synth
            self._balance_cache_ts = time.time()
            logger.warning(f"⚠️ 잔고조회 실패 → 예수금API 사용 cash={psbl_cash:,}원")
            return synth
        logger.error("잔고 조회 완전 실패 & 캐시 없음 → 기본값 반환")
        return {"holdings": [], "total_eval": 0, "cash": 0,
                "total_profit": 0, "total_profit_pct": 0}

    # ──────────────────────────────────────────────────────────
    # 5. 체결 내역 조회
    # ──────────────────────────────────────────────────────────
    # ──────────────────────────────────────────────────────────
    # ★ 해외주식 API (미국/홍콩 등)
    # ──────────────────────────────────────────────────────────

    # 거래소 코드 매핑 (KIS 기준)
    # NASD=나스닥 NYSE=뉴욕 AMEX=아멕스 SEHK=홍콩 SHAA=상하이A SZAA=선전A TKSE=도쿄
    US_EXCD = {
        "NASD": "나스닥",
        "NYSE": "뉴욕증권거래소",
        "AMEX": "아멕스",
    }

    def get_us_current_price(self, symbol: str, excd: str = "NASD") -> dict:
        """
        해외주식 현재가 조회
        symbol: 티커 (AAPL, NVDA ...)
        excd  : 거래소코드 (NASD/NYSE/AMEX)
        """
        url   = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        tr_id = "HHDFS00000300"
        params = {
            "AUTH":  "",
            "EXCD":  excd,
            "SYMB":  symbol,
        }
        try:
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            resp.raise_for_status()
            data   = resp.json()
            rt_cd  = data.get("rt_cd", "")
            msg_cd = data.get("msg_cd", "")

            # ★ OPSQ2001: 미국 장외시간 또는 유효하지 않은 거래소코드
            #   → yfinance 폴백 (조용히, 에러 아님)
            if rt_cd != "0" or msg_cd == "OPSQ2001":
                logger.debug(
                    f"[해외현재가] {symbol}({excd}) KIS rt_cd={rt_cd} "
                    f"msg_cd={msg_cd} → yfinance 폴백"
                )
                return self._get_us_current_price_yfinance(symbol, excd)

            output = data.get("output", {})
            price  = float(output.get("last", 0) or 0)
            if price <= 0:
                logger.debug(f"[해외현재가] {symbol} KIS price=0 → yfinance 폴백")
                return self._get_us_current_price_yfinance(symbol, excd)

            return {
                "symbol":       symbol,
                "excd":         excd,
                "price":        price,
                "open":         float(output.get("open",  0) or 0),
                "high":         float(output.get("high",  0) or 0),
                "low":          float(output.get("low",   0) or 0),
                "volume":       int(output.get("tvol",    0) or 0),
                "change_rate":  float(output.get("rate",  0) or 0),
                "change_price": float(output.get("diff",  0) or 0),
                "market_cap":   output.get("mktcap", ""),
                "currency":     "USD",
                "source":       "KIS",
            }
        except Exception as e:
            logger.warning(f"해외현재가 조회 실패 {symbol}: {e} → yfinance 폴백")
            return self._get_us_current_price_yfinance(symbol, excd)

    def _get_us_current_price_yfinance(self, symbol: str, excd: str = "NASD") -> dict:
        """yfinance로 해외 현재가 조회 (KIS 실패 폴백)"""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            info   = ticker.fast_info
            price  = float(getattr(info, "last_price", 0) or 0)
            if price <= 0:
                # fast_info 실패 시 history 1일치로 대체
                df = ticker.history(period="2d", interval="1m", auto_adjust=True)
                if df is not None and not df.empty:
                    price = float(df["Close"].iloc[-1])
            if price <= 0:
                return {}
            open_  = float(getattr(info, "open", 0) or 0)
            high_  = float(getattr(info, "day_high",  0) or 0)
            low_   = float(getattr(info, "day_low",   0) or 0)
            vol_   = int(getattr(info, "last_volume", 0) or 0)
            logger.info(f"[yfinance] ✅ {symbol} 현재가: ${price:.2f}")
            return {
                "symbol":       symbol,
                "excd":         excd,
                "price":        price,
                "open":         open_,
                "high":         high_,
                "low":          low_,
                "volume":       vol_,
                "change_rate":  0.0,
                "change_price": 0.0,
                "market_cap":   "",
                "currency":     "USD",
                "source":       "yfinance",
            }
        except Exception as e:
            logger.error(f"[yfinance] {symbol} 현재가 폴백 실패: {e}")
            return {}

    def get_us_ohlcv(self, symbol: str, excd: str = "NASD",
                     count: int = 100) -> list[dict]:
        """
        해외주식 일봉 OHLCV 조회 (최근 count일)
        KIS 실패 시 yfinance 자동 폴백
        """
        global _kis_fail_cache

        # ── KIS 쿨다운 중인 종목 → 즉시 yfinance 폴백 ──────────
        now_ts = time.time()
        cache  = _kis_fail_cache.get(symbol, (0, 0))
        fail_cnt, last_fail = cache
        if fail_cnt >= _KIS_FAIL_THRESHOLD:
            elapsed = now_ts - last_fail
            if elapsed < _KIS_COOLDOWN_SEC:
                remaining = int(_KIS_COOLDOWN_SEC - elapsed)
                logger.debug(f"[yf폴백] {symbol} KIS 쿨다운 중 (잔여 {remaining}s) → yfinance")
                return self._get_us_ohlcv_yfinance(symbol, count)
            else:
                # 쿨다운 만료 → KIS 재시도 카운트 리셋
                _kis_fail_cache[symbol] = (0, 0)

        # ── KIS API 시도 ────────────────────────────────────────
        url   = f"{self.base_url}/uapi/overseas-price/v1/quotations/dailyprice"
        tr_id = "HHDFS76240000"
        end_dt = datetime.now().strftime("%Y%m%d")
        params = {
            "AUTH":  "",
            "EXCD":  excd,
            "SYMB":  symbol,
            "GUBN":  "0",        # 0:일 1:주 2:월
            "BYMD":  end_dt,
            "MODP":  "1",        # 수정주가
            "KEYB":  "",
        }
        try:
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            resp.raise_for_status()
            data   = resp.json()
            output = data.get("output2", [])
            candles = []
            for row in output:
                c = float(row.get("clos", 0) or 0)
                if c <= 0:
                    continue
                candles.append({
                    "date":   row.get("xymd", ""),
                    "open":   float(row.get("open", 0) or 0),
                    "high":   float(row.get("high", 0) or 0),
                    "low":    float(row.get("low",  0) or 0),
                    "close":  c,
                    "volume": int(row.get("tvol", 0) or 0),
                })
            candles.sort(key=lambda x: x["date"])
            result = candles[-count:]

            if result:
                # KIS 성공 → 실패 카운트 리셋
                if symbol in _kis_fail_cache:
                    _kis_fail_cache.pop(symbol, None)
                return result
            else:
                # 빈 결과 → yfinance 폴백 (rt_cd 에러 또는 종목 미지원)
                logger.warning(f"[KIS] {symbol} 빈 데이터 → yfinance 폴백")
                self._kis_record_fail(symbol)
                return self._get_us_ohlcv_yfinance(symbol, count)

        except Exception as e:
            err_str = str(e)
            # 500/403/404 에러: 실패 카운트 누적 → 임계치 초과 시 yfinance
            self._kis_record_fail(symbol)
            new_cnt = _kis_fail_cache.get(symbol, (0, 0))[0]
            if new_cnt >= _KIS_FAIL_THRESHOLD:
                logger.warning(f"[KIS→yf] {symbol} KIS {new_cnt}회 연속 실패 → yfinance 폴백")
                return self._get_us_ohlcv_yfinance(symbol, count)
            else:
                logger.error(f"해외 OHLCV 조회 실패 {symbol} ({new_cnt}/{_KIS_FAIL_THRESHOLD}): {err_str}")
                return []

    def _kis_record_fail(self, symbol: str):
        """KIS 실패 카운트 누적"""
        global _kis_fail_cache
        cnt, _ = _kis_fail_cache.get(symbol, (0, 0))
        _kis_fail_cache[symbol] = (cnt + 1, time.time())

    def _get_us_ohlcv_yfinance(self, symbol: str, count: int = 100) -> list[dict]:
        """
        yfinance 폴백: KIS 미지원 종목 OHLCV 취득
        - 레버리지 ETF, 소형 바이오 등 KIS 500 에러 종목
        """
        try:
            import yfinance as yf
            # count일치 데이터 확보 위해 넉넉하게 요청
            period_days = max(count * 2, 200)
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=f"{period_days}d", interval="1d", auto_adjust=True)
            if df is None or df.empty:
                logger.warning(f"[yfinance] {symbol} 데이터 없음")
                return []
            candles = []
            for idx, row in df.iterrows():
                c = float(row.get("Close", 0) or 0)
                if c <= 0:
                    continue
                candles.append({
                    "date":   idx.strftime("%Y%m%d"),
                    "open":   float(row.get("Open",   0) or 0),
                    "high":   float(row.get("High",   0) or 0),
                    "low":    float(row.get("Low",    0) or 0),
                    "close":  c,
                    "volume": int(row.get("Volume", 0) or 0),
                })
            candles.sort(key=lambda x: x["date"])
            result = candles[-count:]
            if result:
                logger.info(f"[yfinance] ✅ {symbol} {len(result)}개 캔들 취득 성공")
            return result
        except Exception as e:
            logger.error(f"[yfinance] {symbol} 폴백 실패: {e}")
            return []

    # ──────────────────────────────────────────────────────────
    # ★ 해외주식 원화 주문 관련
    # ──────────────────────────────────────────────────────────

    def get_us_krw_available(self) -> float:
        """
        KIS 해외주식 원화 주문가능금액 조회 (TTTS3007R)

        반환: 원화 주문가능금액 (KRW float)
        핵심 필드: ovrs_ord_psbl_amt  (해외주식 원화 주문가능금액)
                   (USD 예수금 부족해도 원화 자동환전으로 주문 가능한 금액)

        KIS API 경로: /uapi/overseas-stock/v1/trading/inquire-psamount
        TR_ID       : TTTS3007R (실전), VTTS3007R (모의)
        """
        url   = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        from config import Config as _cfg
        tr_id = "TTTS3007R" if _cfg.KIS_IS_REAL else "VTTS3007R"
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")
        # ★ ITEM_CD 필수: 빈값이면 APBN0746 ('상품이 없습니다') 에러
        # 잔고에 보유 종목 있으면 첫 번째 종목 코드 사용, 없으면 대표 종목 사용
        _item_cd = ""
        try:
            _b = self._balance_cache.get("holdings", [])
            if _b:
                _item_cd = _b[0].get("symbol", "")
        except Exception:
            pass
        if not _item_cd:
            _item_cd = "AAPL"   # 기본값 (NASD 상장 대표 종목)

        params = {
            "CANO":          acc_no,
            "ACNT_PRDT_CD":  acc_prod,
            "OVRS_EXCG_CD":  "NASD",
            "OVRS_ORD_UNPR": "0",
            "ITEM_CD":       _item_cd,   # ★ 필수 — 빈값 금지
        }
        try:
            resp = requests.get(
                url,
                headers=self._headers(tr_id),
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            data   = resp.json()
            output = data.get("output", {})
            if isinstance(output, list):
                output = output[0] if output else {}

            # frcr_ord_psbl_amt1: USD 주문가능 (원화→환전 포함)
            # ovrs_ord_psbl_amt : 원화주문가능금액 (KRW)
            # exrt              : 적용 환율
            krw_avail = float(output.get("ovrs_ord_psbl_amt",  0) or 0)
            usd_avail = float(output.get("frcr_ord_psbl_amt1", 0) or 0)

            if data.get("rt_cd") == "0":
                logger.info(
                    f"✅ 해외주식 주문가능금액 조회 | "
                    f"USD: ${usd_avail:.2f} | 환율: {output.get('exrt','?')}원 | "
                    f"최대: {output.get('ovrs_max_ord_psbl_qty','?')}주"
                )
            else:
                logger.warning(
                    f"⚠️ 해외주식 주문가능금액 조회 실패 | "
                    f"rt_cd={data.get('rt_cd')} msg={data.get('msg1')}"
                )
            return krw_avail

        except Exception as e:
            logger.error(f"해외주식 원화 주문가능금액 조회 오류: {e}")
            return 0.0

    def get_us_available_amounts(self, symbol: str = "", excd: str = "") -> dict:
        """
        해외주식 주문가능금액 전체 조회 (원화 + USD 동시)
        symbol/excd 를 지정하면 해당 종목 기준으로 조회 (더 정확한 ovrs_ord_psbl_amt)
        반환: {"krw": float, "usd": float, "raw": dict}
        """
        url   = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        from config import Config as _cfg
        tr_id = "TTTS3007R" if _cfg.KIS_IS_REAL else "VTTS3007R"
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")

        # ★ ITEM_CD 결정 우선순위: 호출자 지정 > 잔고 첫 종목 > BBAI(NYSE)
        if symbol:
            _item_cd2 = symbol
            _excd     = excd or "NASD"
        else:
            _item_cd2 = ""
            _excd     = "NYSE"
            try:
                _b2 = self._balance_cache.get("holdings", [])
                if _b2:
                    _item_cd2 = _b2[0].get("symbol", "")
                    _excd     = _b2[0].get("excd", "NYSE")
            except Exception:
                pass
            if not _item_cd2:
                _item_cd2 = "BBAI"
                _excd     = "NYSE"

        params = {
            "CANO":          acc_no,
            "ACNT_PRDT_CD":  acc_prod,
            "OVRS_EXCG_CD":  _excd,
            "OVRS_ORD_UNPR": "0",
            "ITEM_CD":       _item_cd2,   # ★ 필수
        }
        try:
            resp = requests.get(
                url,
                headers=self._headers(tr_id),
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            data   = resp.json()
            output = data.get("output", {})
            if isinstance(output, list):
                output = output[0] if output else {}

            krw = float(output.get("ovrs_ord_psbl_amt",  0) or 0)
            usd = float(output.get("frcr_ord_psbl_amt1", 0) or 0)
            return {"krw": krw, "usd": usd, "raw": output}

        except Exception as e:
            logger.error(f"해외주식 주문가능금액 조회 오류: {e}")
            return {"krw": 0.0, "usd": 0.0, "raw": {}}

    def buy_us(self, symbol: str, qty: int, price: float = 0,
               excd: str = "NASD",
               allow_krw_order: bool = True) -> dict:
        """
        해외주식 매수 (실전)
        ──────────────────────────────────────────────
        price=0       → 시장가
        allow_krw_order=True (기본값):
          ① USD 주문 시도
          ② 잔고부족 오류 발생 시 → 원화 주문가능금액 확인
          ③ 원화로 환산했을 때 충분하면 → 원화 주문(자동환전) 재시도
        ──────────────────────────────────────────────
        KIS 해외 매수 TR_ID:
          TTTT1002U  : USD 주문 (기본)
          TTTT1006U  : 매도
          원화 주문도 TTTT1002U 동일하나, ORD_DVSN을 시장가(00)로
          설정하고 OVRS_ORD_UNPR을 "0"으로 두면 자동환전으로 처리됨.
          단, KIS에서 원화결제 계좌 설정 필요.
        """
        _guard = self._live_order_guard(f"BUY_US {symbol} {qty}주 @{price}")
        if _guard is not None:
            return _guard
        url   = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        from config import Config as _cfg
        tr_id = "TTTT1002U" if _cfg.KIS_IS_REAL else "VTTT1002U"   # 해외주식 매수
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")

        # 지정가 주문 (KIS 해외주식은 지정가 권장)
        ord_price = f"{price:.2f}" if price > 0 else "0"
        ord_dvsn  = "00"   # 00=지정가 (price>0), 시장가는 실제 체결 어려움

        body = {
            "CANO":            acc_no,
            "ACNT_PRDT_CD":    acc_prod,
            "OVRS_EXCG_CD":    excd,
            "PDNO":            symbol,
            "ORD_DVSN":        ord_dvsn,
            "ORD_QTY":         str(qty),
            "OVRS_ORD_UNPR":   ord_price,
            "ORD_SVR_DVSN_CD": "0",
        }
        try:
            resp = requests.post(
                url,
                headers=self._headers(tr_id, use_hash=True, body=body),
                json=body,
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("rt_cd") == "0":
                logger.info(
                    f"✅ 해외 BUY(USD) 성공 | {symbol}({excd}) {qty}주 ${price:.2f}"
                )
                return result

            fail_msg = result.get("msg1", "")
            logger.warning(f"⚠️ 해외 BUY(USD) 실패 | {symbol} | {fail_msg}")

            # ── ② 잔고부족 오류 → 원화 주문 시도 ──────────────
            _is_balance_err = any(
                kw in fail_msg
                for kw in ("잔고", "부족", "초과", "주문가능", "한도", "금액")
            )
            if _is_balance_err and allow_krw_order:
                return self._buy_us_krw_fallback(
                    symbol, qty, price, excd,
                    acc_no, acc_prod, fail_msg
                )

            return result

        except Exception as e:
            logger.error(f"해외 매수 오류 {symbol}: {e}")
            return {"rt_cd": "9", "msg1": str(e)}

    def _buy_us_krw_fallback(
        self,
        symbol: str,
        qty: int,
        price: float,
        excd: str,
        acc_no: str,
        acc_prod: str,
        usd_fail_msg: str,
    ) -> dict:
        """
        USD 잔고부족 시 원화 주문가능금액 확인 → 원화 자동환전 주문 재시도
        ─────────────────────────────────────────────────────────────────────
        KIS 원화결제 해외주식 주문:
          - TTTT1002U (동일 TR_ID)
          - OVRS_ORD_UNPR = "0" + ORD_DVSN = "00" 으로 원화결제 트리거
            (KIS 내부에서 당일 환율로 자동환전 처리)
          - 또는 ORD_DVSN = "02" (최유리지정가) 사용
        KRW 주문가능금액 기준:
          - 필요 KRW = price(USD) × 환율 × qty × 1.005 (수수료/환전 마진 0.5%)
        ─────────────────────────────────────────────────────────────────────
        """
        _guard = self._live_order_guard(f"BUY_US_KRW {symbol} {qty}주 @{price}")
        if _guard is not None:
            return _guard
        logger.info(
            f"💱 [{symbol}] USD 잔고부족 → 원화 주문 시도 | 원인: {usd_fail_msg}"
        )

        # ① 주문가능금액 조회
        # usd = frcr_ord_psbl_amt1 (환전 포함 USD 가능)
        # krw = ovrs_ord_psbl_amt  (원화 결제 한도 — 계좌 설정에 따라 0일 수 있음)
        try:
            avail = self.get_us_available_amounts()
            krw_avail = avail["krw"]   # ovrs_ord_psbl_amt
            usd_avail = avail["usd"]   # frcr_ord_psbl_amt1
        except Exception as e:
            logger.error(f"[{symbol}] 주문가능금액 조회 실패: {e}")
            return {"rt_cd": "9", "msg1": f"원화주문가능금액조회실패: {e}"}

        # ★ ovrs_ord_psbl_amt=0 이어도 frcr_ord_psbl_amt1이 0이면 진짜 부족
        # (frcr_ord_psbl_amt1은 이미 USD+원화환전 통합 가능금액임)
        if usd_avail <= 0 and krw_avail <= 0:
            logger.warning(
                f"💸 [{symbol}] USD+원화 모두 0 — 매수 불가 "
                f"(frcr_ord_psbl=${usd_avail:.2f} / ovrs_ord_psbl={krw_avail:,.0f}원)"
            )
            return {
                "rt_cd": "9",
                "msg1":  f"USD부족+원화부족: USD${usd_avail:.2f} KRW{krw_avail:,.0f}원",
            }

        # ovrs_ord_psbl_amt=0 이지만 frcr_ord_psbl_amt1>0 이면 USD 주문으로 진행
        # (이 경우 _buy_us_krw_fallback이 아닌 일반 USD 주문으로 처리됨)
        from config import Config as _cfg
        _tr_id_buy = "TTTT1002U" if _cfg.KIS_IS_REAL else "VTTT1002U"
        if krw_avail <= 0 and usd_avail > 0:
            logger.info(
                f"💱 [{symbol}] ovrs_ord_psbl=0 but frcr_ord_psbl=${usd_avail:.2f} → USD직접 재시도"
            )
            # 수량을 가용 USD에 맞게 축소 후 일반 USD 주문 재시도
            if price > 0:
                max_qty = max(1, int(usd_avail * 0.98 / price))
                if max_qty < qty:
                    logger.info(f"📉 [{symbol}] 수량 축소: {qty}주 → {max_qty}주 (USD${usd_avail:.2f} 기준)")
                    qty = max_qty
            url2  = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
            body2 = {
                "CANO":            acc_no,
                "ACNT_PRDT_CD":    acc_prod,
                "OVRS_EXCG_CD":    excd,
                "PDNO":            symbol,
                "ORD_DVSN":        "00",
                "ORD_QTY":         str(qty),
                "OVRS_ORD_UNPR":   f"{price:.2f}" if price > 0 else "0",
                "ORD_SVR_DVSN_CD": "0",
            }
            try:
                r2 = requests.post(url2,
                                   headers=self._headers(tr_id, use_hash=True, body=body2),
                                   json=body2, timeout=10)
                r2.raise_for_status()
                result2 = r2.json()
                if result2.get("rt_cd") == "0":
                    logger.info(f"✅ 해외 BUY(USD재시도) 성공 | {symbol} {qty}주 ${price:.2f}")
                else:
                    logger.warning(f"⚠️ USD재시도 실패 | {symbol} | {result2.get('msg1','')}")
                return result2
            except Exception as e2:
                return {"rt_cd": "9", "msg1": str(e2)}

        # ② 환율 조회 + 필요 KRW 계산
        try:
            fx_rate = self.get_usd_exchange_rate()
        except Exception:
            fx_rate = 1350.0   # 안전 기본값 (높게 설정해 과주문 방지)

        need_usd = price * qty if price > 0 else 0.0
        need_krw = need_usd * fx_rate * 1.005   # 수수료/환전 마진 0.5% 포함
        logger.info(
            f"💱 [{symbol}] 환율 {fx_rate:.2f}원 | "
            f"필요금액 ${need_usd:.2f} ≈ {need_krw:,.0f}원 | "
            f"원화가능 {krw_avail:,.0f}원"
        )

        if krw_avail < need_krw:
            # 원화도 부족 → 매수 가능 수량 계산 후 축소 주문 시도
            max_qty = int(krw_avail / (price * fx_rate * 1.005)) if price > 0 else 0
            if max_qty <= 0:
                logger.warning(
                    f"💸 [{symbol}] 원화도 부족 | "
                    f"필요 {need_krw:,.0f}원 > 가용 {krw_avail:,.0f}원"
                )
                return {
                    "rt_cd": "9",
                    "msg1":  (f"원화부족: 필요{need_krw:,.0f}원 "
                              f"> 가용{krw_avail:,.0f}원"),
                }
            logger.info(
                f"📉 [{symbol}] 수량 축소: {qty}주 → {max_qty}주 "
                f"(가용 {krw_avail:,.0f}원 기준)"
            )
            qty = max_qty

        # ③ 원화 자동환전 주문 (KIS: TTTT1002U/VTTT1002U, 원화결제 모드)
        #    KIS에서 원화결제 주문: ORD_DVSN="00", OVRS_ORD_UNPR=지정가 그대로
        #    (KIS 서버가 원화잔고에서 자동환전 처리)
        url   = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        tr_id = _tr_id_buy   # 이미 위에서 실전/모의 분기 완료
        ord_price = f"{price:.2f}" if price > 0 else "0"

        body = {
            "CANO":            acc_no,
            "ACNT_PRDT_CD":    acc_prod,
            "OVRS_EXCG_CD":    excd,
            "PDNO":            symbol,
            "ORD_DVSN":        "00",    # 지정가 (원화결제도 동일)
            "ORD_QTY":         str(qty),
            "OVRS_ORD_UNPR":   ord_price,
            "ORD_SVR_DVSN_CD": "0",
        }
        try:
            resp = requests.post(
                url,
                headers=self._headers(tr_id, use_hash=True, body=body),
                json=body,
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("rt_cd") == "0":
                logger.info(
                    f"✅ 해외 BUY(원화환전) 성공 | "
                    f"{symbol}({excd}) {qty}주 ${price:.2f} "
                    f"≈ {price * fx_rate * qty:,.0f}원 자동환전"
                )
            else:
                logger.warning(
                    f"⚠️ 해외 BUY(원화환전) 실패 | "
                    f"{symbol} | {result.get('msg1')}"
                )
            return result

        except Exception as e:
            logger.error(f"해외 원화환전 매수 오류 {symbol}: {e}")
            return {"rt_cd": "9", "msg1": str(e)}

    def sell_us(self, symbol: str, qty: int, price: float = 0,
                excd: str = "NASD",
                ord_dvsn: str = "00") -> dict:
        """
        해외주식 매도 (실전)
        ORD_DVSN:
          "00" : 지정가 (price 필수 — OVRS_ORD_UNPR에 실제 가격 입력)
          "00" + price>0 : 지정가 매도 (미국 기본, 현재가 지정)
        ★ KIS 해외주식은 시장가(price=0) 미지원 → 반드시 현재가 지정가로 주문
        """
        _guard = self._live_order_guard(f"SELL_US {symbol} {qty}주 @{price}")
        if _guard is not None:
            return _guard
        url   = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        from config import Config as _cfg
        tr_id = "TTTT1006U" if _cfg.KIS_IS_REAL else "VTTT1001U"   # 해외주식 매도 (모의: 1001, 실전: 1006)
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")

        # ★ KIS 해외주식: price=0 시장가 불가 → 0이면 호출부에서 현재가를 넣어줘야 함
        if price <= 0:
            logger.warning(f"[sell_us] {symbol} price=0 → 지정가 주문 불가. 호출부에서 현재가 전달 필요")
            return {"rt_cd": "9", "msg_cd": "PRICE_ZERO",
                    "msg1": "KIS 해외주식 시장가 미지원 — 현재가 지정가로 주문하세요",
                    "tr_id": tr_id}

        ord_price = f"{price:.2f}"

        body = {
            "CANO":         acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "OVRS_EXCG_CD": excd,
            "PDNO":         symbol,
            "ORD_DVSN":     ord_dvsn,
            "ORD_QTY":      str(qty),
            "OVRS_ORD_UNPR": ord_price,
            "ORD_SVR_DVSN_CD": "0",
        }
        try:
            resp = requests.post(url,
                                 headers=self._headers(tr_id, use_hash=True, body=body),
                                 json=body, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("rt_cd") == "0":
                logger.info(f"✅ 해외 SELL 성공 | {symbol}({excd}) {qty}주 ${price:.2f}")
            else:
                logger.warning(f"⚠️ 해외 SELL 실패 | {result.get('msg1')}")
            return result
        except Exception as e:
            logger.error(f"해외 매도 오류 {symbol}: {e}")
            return {"rt_cd": "9", "msg1": str(e)}

    def get_us_balance(self) -> dict:
        """
        해외주식 보유잔고 + 예수금 조회 (실전)
        """
        url   = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        from config import Config as _cfg
        tr_id = "TTTS3012R" if _cfg.KIS_IS_REAL else "VTTS3012R"   # 해외주식 잔고
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")
        params = {
            "CANO":         acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "OVRS_EXCG_CD": "NASD",   # 전체 조회용 (KIS 전체 잔고 반환)
            "TR_CRCY_CD":   "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        try:
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            holdings = []
            for item in data.get("output1", []):
                # ★ 실전 TTTS3012R 실제 필드명 기준
                # ovrs_cblc_qty : 보유수량 (ccld_qty_smtl 필드 없음)
                # ovrs_pdno     : 종목코드 (pdno 필드 없음)
                # ovrs_item_name: 종목명   (prdt_name 필드 없음)
                qty = int(
                    item.get("ovrs_cblc_qty",  # 실제 필드
                    item.get("ccld_qty_smtl",  # 구버전 호환
                    item.get("ord_psbl_qty", 0))) or 0
                )
                if qty <= 0:
                    continue
                # ★ 매도가능수량: ord_psbl_qty (T+2 결제중 수량 제외한 실제 매도가능)
                # ─── T+2 버그 수정 ─────────────────────────────────────────────
                # 구버전: int(item.get("ord_psbl_qty", qty) or qty)
                #   문제: ord_psbl_qty="0" 이면 int("0")=0, 0 or qty → qty 로 폴백
                #         → T+2 미결제 종목(ord_psbl_qty=0)이 전량 매도가능으로 오표기
                # 수정: 키 존재 여부를 명시적으로 구분
                _raw_psbl = item.get("ord_psbl_qty", None)
                if _raw_psbl is None:
                    # KIS 응답에 ord_psbl_qty 필드 자체가 없으면 qty 폴백
                    sell_qty = qty
                else:
                    # 필드가 존재하면 실제 값 사용 (0이면 T+2 미결제 → 0 그대로 유지)
                    sell_qty = int(_raw_psbl) if str(_raw_psbl).strip() != "" else qty
                holdings.append({
                    "symbol":    item.get("ovrs_pdno",      item.get("pdno", "")),
                    "name":      item.get("ovrs_item_name", item.get("prdt_name", "")),
                    "excd":      item.get("ovrs_excg_cd", "NASD"),
                    "qty":       qty,
                    "sell_qty":  sell_qty,   # ★ 실제 매도가능수량 (T+2 차감 후)
                    "avg_price": float(item.get("pchs_avg_pric", 0) or 0),
                    "cur_price": float(item.get("now_pric2",     0) or 0),
                    "pnl_pct":   float(item.get("evlu_pfls_rt",  0) or 0),
                    "pnl_amt":   float(item.get("frcr_evlu_pfls_amt",
                                       item.get("ovrs_stck_evlu_amt", 0)) or 0),
                    "currency":  "USD",
                    "is_overseas": True,
                })
            output2 = data.get("output2", {})
            if isinstance(output2, list):
                output2 = output2[0] if output2 else {}

            # ★ 총평가금액 계산 (실전 TTTS3012R output2 실제 필드 기준)
            # tot_evlu_pfls_amt : 총 평가금액 (매입+평가손익 합산, USD)
            # ovrs_tot_pfls     : 평가손익 합계 (USD)
            # frcr_dncl_amt_2   : TTTS3012R에 없음 → 0 처리 (예수금은 TTTS3007R exrt로 역산)
            raw_eval   = float(output2.get("tot_evlu_pfls_amt",
                               output2.get("ovrs_stck_evlu_amt", 0)) or 0)
            raw_profit = float(output2.get("ovrs_tot_pfls", 0) or 0)
            # USD 예수금: TTTS3012R output2에 없는 필드 — 0으로 반환 (별도 조회 필요)
            cash_usd   = float(output2.get("frcr_dncl_amt_2",
                               output2.get("dncl_amt", 0)) or 0)

            # raw_eval 보정: output2도 0이면 보유종목 평가금액 직접 합산
            if raw_eval <= 0 and holdings:
                raw_eval = sum(
                    float(h.get("cur_price", 0)) * int(h.get("qty", 0))
                    for h in holdings
                )

            return {
                "holdings":     holdings,
                "total_eval":   raw_eval,
                "cash_usd":     cash_usd,
                "total_profit": raw_profit,
            }
        except Exception as e:
            logger.error(f"해외 잔고 조회 실패: {e}")
            return {"holdings": [], "total_eval": 0, "cash_usd": 0, "total_profit": 0}

    def get_usd_exchange_rate(self) -> float:
        """
        USD/KRW 환율 조회 (KST 기준 당일 환율)

        방법 1: KIS API  inquire-daily-chartprice
          - TR_ID : FHKST03030100
          - 심볼  : FX@KRWKFTC  (서울외국환중개 기준율, 가장 정확)
          - 응답  : output2[0].ovrs_nmix_prpr  (종가 = 당일 환율)
          - output1.ovrs_nmix_prpr 도 시도 (장중 현재값)

        방법 2: yfinance  USDKRW=X  (KIS 실패 시 폴백)
        """
        # ── 방법 1: KIS API ──────────────────────────────────────
        url   = f"{self.base_url}/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
        tr_id = "FHKST03030100"
        today = datetime.now().strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "X",
            "FID_INPUT_ISCD":          "FX@KRWKFTC",   # ← 서울외국환중개 USD/KRW
            "FID_INPUT_DATE_1":        today,
            "FID_INPUT_DATE_2":        today,
            "FID_PERIOD_DIV_CODE":     "D",
        }
        try:
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=5)
            resp.raise_for_status()
            body = resp.json()

            # output1 → 당일 현재 환율 (장중 업데이트)
            out1 = body.get("output1", {})
            if isinstance(out1, list):
                out1 = out1[0] if out1 else {}
            rate = float(out1.get("ovrs_nmix_prpr", 0) or 0)
            if rate > 500:
                logger.info(f"✅ KIS 환율(output1) 조회 성공: {rate:.2f}")
                return rate

            # output2 → 최근 일봉 (output1 없을 때 최신 종가 사용)
            out2 = body.get("output2", [])
            if isinstance(out2, list) and out2:
                rate = float(out2[0].get("ovrs_nmix_prpr", 0) or 0)
                if rate > 500:
                    logger.info(f"✅ KIS 환율(output2) 조회 성공: {rate:.2f}")
                    return rate

            logger.warning(f"⚠️ KIS 환율 응답 이상 (rt_cd={body.get('rt_cd')}, msg={body.get('msg1')})")
        except Exception as e:
            logger.warning(f"⚠️ KIS 환율 조회 실패: {e}")

        # ── 방법 2: yfinance 폴백 ─────────────────────────────────
        try:
            import yfinance as yf
            ticker = yf.Ticker("USDKRW=X")
            # fast_info 우선 (빠름), 실패 시 history
            rate = float(ticker.fast_info.get("last_price") or 0)
            if rate > 500:
                logger.info(f"✅ yfinance 환율 조회 성공: {rate:.2f}")
                return rate
            hist = ticker.history(period="2d")
            if not hist.empty:
                rate = float(hist["Close"].iloc[-1])
                if rate > 500:
                    logger.info(f"✅ yfinance(history) 환율 조회 성공: {rate:.2f}")
                    return rate
        except Exception as e:
            logger.warning(f"⚠️ yfinance 환율 조회 실패: {e}")

        # ── 방법 3: 외부 공개 API (최후 수단) ────────────────────
        try:
            resp = requests.get(
                "https://api.exchangerate-api.com/v4/latest/USD",
                timeout=5
            )
            resp.raise_for_status()
            rate = float(resp.json().get("rates", {}).get("KRW", 0))
            if rate > 500:
                logger.info(f"✅ ExchangeRate-API 환율 조회 성공: {rate:.2f}")
                return rate
        except Exception as e:
            logger.warning(f"⚠️ ExchangeRate-API 실패: {e}")

        logger.error("❌ 모든 환율 조회 실패 → 기본값 1300 사용")
        return 1300.0    # 최후 안전 기본값

    def get_order_history(self, days: int = 7) -> list[dict]:
        """최근 체결 내역 조회"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        tr_id = "TTTC0081R"  # 3개월이내 실전 (신버전, UI 전용)
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

    # ──────────────────────────────────────────────────────────
    # 6-A. 국내주식 당일 주문·체결 조회 (단일 ODNO 필터 지원)
    # ──────────────────────────────────────────────────────────
    def get_kr_ccld_by_odno(self, odno: str = "", code: str = "") -> dict:
        """국내주식 당일 주문·체결 조회 (TTTC8001R, 실전).

        특정 주문번호(odno)를 지정하면 해당 주문만 반환한다.
        ondo 미지정 시 종목코드(code) 또는 전체 당일 체결 목록을 반환한다.

        반환 dict:
          {
            "odno":           str,    # 주문번호 (KIS odno)
            "code":           str,    # 종목코드
            "side":           str,    # "BUY" | "SELL"
            "order_qty":      int,    # 총 주문수량
            "cum_filled_qty": int,    # 누적 체결수량 (tot_ccld_qty)
            "unfilled_qty":   int,    # 미체결 잔여 수량 (ord_qty - cum_filled_qty)
            "avg_fill_price": float,  # 평균 체결가 (avg_prvs)
            "order_status":   str,    # KIS ord_stts_name 원본
            "order_time":     str,    # HHMMSS (ord_tmd)
            "order_date":     str,    # YYYYMMDD (ord_dt)
            "raw":            dict,   # KIS output1 원본 첫 번째 레코드
          }
          응답 없거나 오류 시 {} 반환.

        실전/모의 TR_ID:
          KIS_IS_REAL=True  → TTTC0081R
          KIS_IS_REAL=False → VTTC0081R
        """
        from config import Config as _Cfg
        tr_id = "TTTC0081R" if _Cfg.KIS_IS_REAL else "VTTC0081R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")
        today = datetime.now().strftime("%Y%m%d")
        params = {
            "CANO":             acc_no,
            "ACNT_PRDT_CD":     acc_prod,
            "INQR_STRT_DT":     today,
            "INQR_END_DT":      today,
            "SLL_BUY_DVSN_CD":  "00",   # 00=전체 (01=매도, 02=매수)
            "INQR_DVSN":        "00",   # 00=역순
            "PDNO":             code,   # 종목코드 (빈값=전체)
            "CCLD_DVSN":        "01",   # 01=체결분만
            "ORD_GNO_BRNO":     "",
            "ODNO":             odno,   # 특정 주문번호 필터 (빈값=전체)
            "INQR_DVSN_3":      "00",
            "INQR_DVSN_1":      "",
            "CTX_AREA_FK100":   "",
            "CTX_AREA_NK100":   "",
        }
        try:
            self._rate_limit()
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("rt_cd") != "0":
                logger.warning(
                    f"[KR체결조회] rt_cd={data.get('rt_cd')} "
                    f"msg_cd={data.get('msg_cd')} msg1={data.get('msg1')} "
                    f"odno={odno!r} code={code!r}"
                )
                return {}
            items = data.get("output1", []) or []
            if not items:
                return {}
            # ODNO 필터: KIS가 파라미터를 무시할 경우 로컬 필터 보완
            if odno:
                items = [i for i in items if i.get("odno", "") == odno]
            if not items:
                return {}
            # 누적 집계 (부분체결 여러 레코드 가능)
            first = items[0]
            sll_buy = first.get("sll_buy_dvsn_cd", "02")
            side = "BUY" if sll_buy == "02" else "SELL"
            order_qty     = int(first.get("ord_qty",      0) or 0)
            cum_filled    = sum(int(i.get("tot_ccld_qty", 0) or 0) for i in items)
            total_amt     = sum(int(i.get("tot_ccld_amt", 0) or 0) for i in items)
            avg_price     = (total_amt / cum_filled) if cum_filled > 0 else 0.0
            unfilled      = max(0, order_qty - cum_filled)
            self._on_api_success()
            return {
                "odno":           first.get("odno",         ""),
                "code":           first.get("pdno",         code),
                "side":           side,
                "order_qty":      order_qty,
                "cum_filled_qty": cum_filled,
                "unfilled_qty":   unfilled,
                "avg_fill_price": float(avg_price),
                "order_status":   first.get("ord_stts_name", ""),
                "order_time":     first.get("ord_tmd",       ""),
                "order_date":     first.get("ord_dt",        today),
                "raw":            first,
            }
        except Exception as e:
            logger.error(f"[KR체결조회] 오류 odno={odno!r}: {e}")
            return {}

    # ──────────────────────────────────────────────────────────
    # 6-B. 미국주식 주문·체결 조회 (TTTS3035R)
    # ──────────────────────────────────────────────────────────
    def get_us_ccld(self, odno: str = "", symbol: str = "",
                    excd: str = "NASD") -> dict:
        """미국주식 주문·체결 조회 (TTTS3035R, 실전).

        특정 주문번호(odno) 또는 종목(symbol)으로 필터 가능.
        KIS TTTS3035R 응답에서 **실제 존재가 확인된 필드**만 사용한다.
        불명확한 필드는 raw에 보존하고 해당 키는 None 처리한다.

        반환 dict:
          {
            "odno":           str | None,
            "code":           str | None,   # 티커 심볼
            "exchange":       str | None,   # 거래소 코드
            "side":           str | None,   # "BUY" | "SELL" | None (필드 불명확 시)
            "order_qty":      int | None,
            "cum_filled_qty": int | None,
            "unfilled_qty":   int | None,
            "avg_fill_price": float | None,
            "order_status":   str | None,
            "order_time":     str | None,
            "currency":       str,          # "USD"
            "raw":            dict,         # KIS output1 원본 첫 번째 레코드
          }
          응답 없거나 오류 시 {} 반환.

        실전/모의 TR_ID:
          KIS_IS_REAL=True  → TTTS3035R
          KIS_IS_REAL=False → VTTS3035R
        주의:
          - 이 TR_ID의 응답 필드명은 KIS 공식 포털에서 검증이 필요하다.
            따라서 필드명이 확인되지 않은 경우 None 을 반환하고 raw에 보존한다.
          - KIS 해외주식 체결조회 응답의 sll_buy_dvsn_cd, ord_qty, rmn_qty 등
            필드 존재 여부를 실계좌에서 반드시 확인해야 한다.
        """
        from config import Config as _Cfg
        tr_id = "TTTS3035R" if _Cfg.KIS_IS_REAL else "VTTS3035R"
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-ccnl"
        acc_no, acc_prod = self.account_no.split("-") \
            if "-" in self.account_no else (self.account_no, "01")
        today = datetime.now().strftime("%Y%m%d")
        params = {
            "CANO":             acc_no,
            "ACNT_PRDT_CD":     acc_prod,
            "OVRS_EXCG_CD":     excd,
            "PDNO":             symbol,     # 종목코드 (빈값=전체)
            "ODNO":             odno,       # 주문번호 (빈값=전체)
            "ORD_STRT_DT":      today,
            "ORD_END_DT":       today,
            "SLL_BUY_DVSN_CD":  "00",       # 00=전체
            "CCLD_NCCS_DVSN":   "01",       # 01=체결분만
            "CTX_AREA_FK200":   "",
            "CTX_AREA_NK200":   "",
        }
        try:
            self._rate_limit()
            resp = requests.get(url, headers=self._headers(tr_id),
                                params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("rt_cd") != "0":
                logger.warning(
                    f"[US체결조회] rt_cd={data.get('rt_cd')} "
                    f"msg_cd={data.get('msg_cd')} msg1={data.get('msg1')} "
                    f"odno={odno!r} symbol={symbol!r}"
                )
                return {}
            items = data.get("output1", []) or []
            if not items:
                return {}
            # ODNO 로컬 필터 보완
            if odno:
                items = [i for i in items if i.get("odno", "") == odno]
            if not items:
                return {}
            first = items[0]

            # ★ 필드명은 KIS 실계좌 검증 필요 — 아래는 KIS 공식 문서 기준 추정값
            # 불명확한 필드: None 반환 + raw 보존
            _raw_side = first.get("sll_buy_dvsn_cd", None)
            if _raw_side == "02":
                side = "BUY"
            elif _raw_side == "01":
                side = "SELL"
            else:
                side = None   # ★ 필드 불명확 — raw에서 확인 필요

            # ★ 주문수량 필드: KIS US는 "ord_qty" 또는 "ft_ord_qty" 가능
            _ord_qty_raw = (first.get("ft_ord_qty")
                            or first.get("ord_qty")
                            or None)
            order_qty = int(_ord_qty_raw) if _ord_qty_raw is not None else None

            # ★ 누적체결수량: "ft_ccld_qty" 또는 "ccld_qty" 가능
            _cum_raw = (first.get("ft_ccld_qty")
                        or first.get("ccld_qty")
                        or None)
            cum_filled = int(_cum_raw) if _cum_raw is not None else None

            # ★ 미체결: "rmn_qty" 가능
            _unf_raw = first.get("rmn_qty", None)
            unfilled = int(_unf_raw) if _unf_raw is not None else (
                max(0, order_qty - cum_filled)
                if (order_qty is not None and cum_filled is not None) else None
            )

            # ★ 평균체결가: "ft_ccld_unpr3" 또는 "ccld_unpr" 가능
            _price_raw = (first.get("ft_ccld_unpr3")
                          or first.get("ccld_unpr")
                          or first.get("avg_prvs")
                          or None)
            avg_price = float(_price_raw) if _price_raw else None

            # ★ 주문상태: "ord_stts_name" 또는 "ord_stat_name" 가능
            order_status = (first.get("ord_stts_name")
                            or first.get("ord_stat_name")
                            or None)

            # ★ 주문시각: "ord_tmd" 또는 "ord_tmmd" 가능
            order_time = (first.get("ord_tmd")
                          or first.get("ord_tmmd")
                          or None)

            # ★ 종목코드: "pdno" 또는 "ovrs_pdno" 가능
            code_out = (first.get("ovrs_pdno")
                        or first.get("pdno")
                        or symbol or None)

            # ★ 거래소: "ovrs_excg_cd" 가능
            exchange = first.get("ovrs_excg_cd", excd or None)

            self._on_api_success()
            return {
                "odno":           first.get("odno", odno or None),
                "code":           code_out,
                "exchange":       exchange,
                "side":           side,
                "order_qty":      order_qty,
                "cum_filled_qty": cum_filled,
                "unfilled_qty":   unfilled,
                "avg_fill_price": avg_price,
                "order_status":   order_status,
                "order_time":     order_time,
                "currency":       "USD",
                "raw":            first,
            }
        except Exception as e:
            logger.error(f"[US체결조회] 오류 odno={odno!r}: {e}")
            return {}
