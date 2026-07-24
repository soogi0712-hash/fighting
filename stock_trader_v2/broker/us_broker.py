"""
broker/us_broker.py — 미국장 KIS API 브로커 (V2)
==================================================
역할:
  - 미국 주식 현재가 조회        (HHDFS00000300)
  - 미국 주식 1분봉/일봉 조회    (HHDFS76240000 / HHDFS76410000)
  - 미국 잔고 조회               (TTTS3012R) → tot_evlu_amt 포함
  - USD 주문가능금액 조회        (FHKST03030200)
  - 미국 매수 / 매도 주문        (TTTT1002U / TTTT1006U)
  - 미체결 주문 조회 / 취소      (TTTS3035R / TTTT1004U)

★ 설계 원칙:
  1. KST 기준으로 미국 정규장 시간 관리
     - 서머타임(EDT, UTC-4): 22:30 ~ 05:00 (KST)
     - 표준시간(EST, UTC-5): 23:30 ~ 06:00 (KST)
  2. ORD_DVSN:
     - 00=지정가(가격필수), 01=시장가(가격=0), 32=LOO(시가주문)
  3. 통화: USD — 주문금액은 USD, 환산은 tot_evlu_amt 내부 포함
  4. 매수 전 orderable_usd 확인 → 미수 방지
  5. 장후시간외 BUY 절대 금지 (EXCH_CD != NASD/NYSE/AMEX 주간)

★ 미국장 KIS API 특이사항:
  - TR_ID: 실전 TTTT, 모의 VTTT
  - OVRS_EXCG_CD: NASD(나스닥)/NYSE(뉴욕)/AMEX(아멕스)/NAS(나스닥소형)
  - 환율: tot_evlu_amt가 KRW 환산 포함 (원화환산총자산)
  - 5분봉: HHDFS76240000 (해외현재가 조회 후 직전봉 사용)
"""

import time
from datetime import datetime, date, time as dtime
from typing import Optional, List
import pytz

from broker.kis_base import KISBase
from utils.v2_logger  import get_logger

logger = get_logger("USBroker")
KST    = pytz.timezone("Asia/Seoul")
UTC    = pytz.utc

# ── 거래소 코드 ───────────────────────────────────────────────
EXCH_NASD = "NASD"   # 나스닥
EXCH_NYSE = "NYSE"   # 뉴욕
EXCH_AMEX = "AMEX"   # 아멕스
EXCH_NAS  = "NAS"    # 나스닥 소형
SUPPORTED_EXCHANGES = (EXCH_NASD, EXCH_NYSE, EXCH_AMEX, EXCH_NAS)

# ── 주문 유형 ────────────────────────────────────────────────
ORD_LIMIT    = "00"   # 지정가 (가격 필수)
ORD_MARKET   = "01"   # 시장가 (가격 = 0)
ORD_LOO      = "32"   # 시가 주문 (LOO, 가격 = 0)

# ── 미국 정규장 시간 (KST, 서머타임 EDT 기준) ────────────────
# 서머타임(3월 둘째 일 ~ 11월 첫째 일): 22:30 KST 개장
# 표준시  (11월 ~ 3월):                23:30 KST 개장
US_MKT_OPEN_EDT  = dtime(22, 30)   # KST 기준 서머타임 개장
US_MKT_CLOSE_EDT = dtime(5,  0)    # KST 기준 서머타임 마감 (익일 05:00)
US_MKT_OPEN_EST  = dtime(23, 30)   # KST 기준 표준시 개장
US_MKT_CLOSE_EST = dtime(6,  0)    # KST 기준 표준시 마감 (익일 06:00)

# 신규 매수 금지 기준: 마감 30분 전
US_BUY_CUT_BEFORE_CLOSE_MIN = 30

# 캐시 TTL
_PRICE_TTL      = 20.0   # 현재가 20초 캐시 (배치 prefetch 후 루프 내내 유효)
_CANDLE_TTL     = 120.0  # 봉 120초 캐시
_BALANCE_TTL    = 60.0   # 잔고 60초 캐시
_ORDERABLE_TTL  = 300.0  # [개선 2026-06-28] 주문가능금액 5분 캐시 (60초→300초: last_ok 장시간 유효 유지)


def _is_dst_kst(dt_kst: datetime) -> bool:
    """KST 기준으로 미국 서머타임(EDT) 여부 판별."""
    # 미국 EDT: 3월 두 번째 일요일 ~ 11월 첫 번째 일요일
    month = dt_kst.month
    if 4 <= month <= 10:
        return True
    if month == 3:
        # 두 번째 일요일 이후
        first_sunday = 7 - (date(dt_kst.year, 3, 1).weekday() + 1) % 7
        second_sunday = first_sunday + 7
        # KST 기준 서머타임 전환은 EST→EDT 당일 이후 오후(KST 익일)
        return dt_kst.day > second_sunday
    if month == 11:
        # 첫 번째 일요일 이전
        first_sunday = 7 - (date(dt_kst.year, 11, 1).weekday() + 1) % 7
        return dt_kst.day <= first_sunday
    return False


class USBroker(KISBase):
    """
    미국장 전용 브로커.
    KISBase를 상속받아 미국 주식 시세·잔고·주문을 처리한다.
    """

    def __init__(self, app_key: str, app_secret: str, account_no: str,
                 paper_trading: bool = False):
        super().__init__(app_key, app_secret, account_no)
        # 모의투자 여부 (TR_ID prefix 변경)
        self._paper = paper_trading

        # 캐시
        self._price_cache:      dict  = {}
        self._candle_cache:     dict  = {}
        self._balance_cache:    dict  = {}
        self._balance_ts:       float = 0.0
        self._orderable_cache:  float = 0.0   # 주문가능금액 캐시 (60초 TTL)
        self._orderable_ts:     float = 0.0   # 마지막 조회 시각
        self._orderable_last_ok: float = 0.0  # ★ 마지막 성공값 (장외시간 폴백용)
        # ★ 영구차단 종목 (APBK0656/APBK1672: 재시도해도 무조건 실패)
        self._perm_banned:      set   = set() # {"ACHR", ...}

    # ─────────────────────────────────────────────────────────
    # 내부 헬퍼: TR_ID prefix
    # ─────────────────────────────────────────────────────────

    def _tr(self, real_id: str, paper_id: str) -> str:
        """실전/모의 TR_ID 선택."""
        return paper_id if self._paper else real_id

    # ════════════════════════════════════════════════════════════
    # 1. 장 시간 유틸리티
    # ════════════════════════════════════════════════════════════

    def is_market_open(self, now_kst: Optional[datetime] = None) -> bool:
        """
        미국 정규장 개장 여부 (KST 기준).
        Returns True if currently in regular trading hours.
        """
        if now_kst is None:
            now_kst = datetime.now(KST)
        t = now_kst.time()
        is_dst = _is_dst_kst(now_kst)
        if is_dst:
            # EDT: 22:30 ~ 익일 05:00 (KST)
            return t >= US_MKT_OPEN_EDT or t < US_MKT_CLOSE_EDT
        else:
            # EST: 23:30 ~ 익일 06:00 (KST)
            return t >= US_MKT_OPEN_EST or t < US_MKT_CLOSE_EST

    def is_buy_allowed(self, now_kst: Optional[datetime] = None) -> bool:
        """
        신규 매수 허용 여부.
        정규장 개장 중 + 마감 30분 전까지만 허용.
        """
        if now_kst is None:
            now_kst = datetime.now(KST)
        if not self.is_market_open(now_kst):
            return False
        t = now_kst.time()
        is_dst = _is_dst_kst(now_kst)
        close_t = US_MKT_CLOSE_EDT if is_dst else US_MKT_CLOSE_EST
        # 마감 30분 전 체크
        # close_t 가 05:00 or 06:00 → 새벽 시간이라 비교 방향 주의
        # 새벽 마감(t < close_t): 마감 30분 전 = close_t - 30분
        from datetime import timedelta
        close_dt = datetime.combine(now_kst.date(), close_t)
        cutoff_t = (close_dt - timedelta(minutes=US_BUY_CUT_BEFORE_CLOSE_MIN)).time()
        # 개장 구간(22:30 이후) 또는 새벽(00:00~05:00) 구분
        if is_dst:
            # 22:30~23:59: cutoff = 04:30 (다음날) → 허용
            # 00:00~05:00: cutoff = 04:30 → t < 04:30 이면 허용
            if t >= US_MKT_OPEN_EDT:
                return True   # 22:30~23:59는 항상 허용 (마감까지 6h 이상)
            else:
                return t < cutoff_t
        else:
            if t >= US_MKT_OPEN_EST:
                return True
            else:
                return t < cutoff_t

    # ════════════════════════════════════════════════════════════
    # 2. 현재가 조회
    # ════════════════════════════════════════════════════════════

    def get_price(self, code: str, exch_cd: str = EXCH_NASD,
                  force: bool = False) -> dict:
        """
        미국 주식 현재가 조회.
        API: HHDFS00000300 (해외주식 현재가상세)

        Returns:
            {
              "code":        str,
              "name":        str,
              "cur_price":   float,   # 현재가 (USD)
              "open":        float,
              "high":        float,
              "low":         float,
              "prev_close":  float,
              "change_pct":  float,   # 등락률 %
              "volume":      int,
              "exch_cd":     str,
              "ok":          bool,
            }
        """
        cache_key = f"{exch_cd}:{code}"
        now_ts = time.time()
        if not force and cache_key in self._price_cache:
            data, ts = self._price_cache[cache_key]
            if now_ts - ts < _PRICE_TTL:
                return data

        url = f"{self.BASE_URL}/uapi/overseas-price/v1/quotations/price"
        params = {
            "AUTH":         "",
            "EXCD":         exch_cd,
            "SYMB":         code,
        }
        resp = self._get(url, self._tr("HHDFS00000300", "HHDFS00000300"), params)

        # OPSQ2001: KIS 야간시간(= 미국장 시간) 시세조회 불가 → yfinance 폴백
        msg_cd = resp.get("msg_cd", "")
        if resp.get("rt_cd") != "0":
            if msg_cd == "OPSQ2001":
                logger.debug(f"[USBroker] KIS 야간모드({code}) → yfinance 폴백")
                return self._get_price_yfinance(code, exch_cd, now_ts)
            logger.warning(f"[USBroker] 현재가 조회 실패 {code}: {resp.get('msg1')}")
            return {"code": code, "ok": False, "cur_price": 0.0}

        o = resp.get("output", {})
        try:
            cur_price  = float(o.get("last", 0) or 0)
            open_p     = float(o.get("open", 0) or 0)
            high_p     = float(o.get("high", 0) or 0)
            low_p      = float(o.get("low",  0) or 0)
            prev_close = float(o.get("base", 0) or 0)
            change_pct = float(o.get("rate", 0) or 0)
            volume     = int(float(o.get("tvol", 0) or 0))
            name       = o.get("rsym", code)
        except (ValueError, TypeError):
            return {"code": code, "ok": False, "cur_price": 0.0}

        # KIS 정상 응답이어도 가격=0이면 yfinance 폴백
        if cur_price <= 0:
            logger.debug(f"[USBroker] KIS price=0({code}) → yfinance 폴백")
            return self._get_price_yfinance(code, exch_cd, now_ts)

        result = {
            "code":       code,
            "name":       name,
            "cur_price":  cur_price,
            "open":       open_p,
            "high":       high_p,
            "low":        low_p,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "volume":     volume,
            "exch_cd":    exch_cd,
            "ok":         True,
            "source":     "KIS",
        }
        self._price_cache[cache_key] = (result, now_ts)
        return result

    def _get_price_yfinance(self, code: str, exch_cd: str, now_ts: float) -> dict:
        """yfinance 폴백 현재가 조회 (KIS OPSQ2001 / price=0 시 사용)."""
        try:
            import yfinance as yf
            ticker = yf.Ticker(code)
            fi     = ticker.fast_info
            cur_price = float(getattr(fi, "last_price", 0) or 0)
            if cur_price <= 0:
                hist = ticker.history(period="1d", interval="1m")
                if not hist.empty:
                    cur_price = float(hist["Close"].iloc[-1])
            if cur_price <= 0:
                return {"code": code, "ok": False, "cur_price": 0.0}

            result = {
                "code":       code,
                "name":       code,
                "cur_price":  round(cur_price, 4),
                "open":       float(getattr(fi, "open", 0) or 0),
                "high":       float(getattr(fi, "day_high", 0) or 0),
                "low":        float(getattr(fi, "day_low",  0) or 0),
                "prev_close": float(getattr(fi, "previous_close", 0) or 0),
                "change_pct": 0.0,
                "volume":     int(getattr(fi, "three_month_average_volume", 0) or 0),
                "exch_cd":    exch_cd,
                "ok":         True,
                "source":     "yfinance",
            }
            cache_key = f"{exch_cd}:{code}"
            self._price_cache[cache_key] = (result, now_ts)
            return result
        except Exception as e:
            logger.warning(f"[USBroker] yfinance 폴백 실패 {code}: {e}")
            return {"code": code, "ok": False, "cur_price": 0.0}

    def prefetch_prices(self, stocks: list) -> int:
        """
        ★ 배치 현재가 프리패치 — 루프 시작 전 1회 호출.
        yfinance download()로 전 종목을 한 번에 조회해 캐시에 채운다.
        개별 get_price()가 캐시 히트로 즉시 반환되어 루프 속도 대폭 향상.

        Args:
            stocks: [{"code": "NVDA", "exch_cd": "NASD"}, ...]
        Returns:
            성공적으로 캐시된 종목 수
        """
        if not stocks:
            return 0
        codes    = [s["code"].upper() for s in stocks]
        exch_map = {s["code"].upper(): s.get("exch_cd", EXCH_NASD) for s in stocks}
        now_ts   = time.time()

        # 이미 캐시가 모두 유효하면 skip (TTL의 절반 이상 남아있으면)
        half_ttl = _PRICE_TTL * 0.5
        all_cached = all(
            f"{exch_map.get(c, EXCH_NASD)}:{c}" in self._price_cache
            and now_ts - self._price_cache[f"{exch_map.get(c, EXCH_NASD)}:{c}"][1] < half_ttl
            for c in codes
        )
        if all_cached:
            return len(codes)

        try:
            import yfinance as yf
            tickers_str = " ".join(codes)
            # auto_adjust=False: 수정주가 미적용, 빠른 응답
            df = yf.download(
                tickers_str,
                period="1d",
                interval="1m",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
            if df is None or df.empty:
                logger.warning("[USBroker.prefetch] yfinance download 결과 없음")
                return 0

            # 멀티티커 or 단일티커 처리
            import pandas as pd
            filled = 0
            for code in codes:
                exch_cd   = exch_map.get(code, EXCH_NASD)
                cache_key = f"{exch_cd}:{code}"
                try:
                    # 멀티티커: df["Close"][code], 단일티커: df["Close"]
                    if isinstance(df.columns, pd.MultiIndex):
                        col_close = df["Close"][code] if code in df["Close"].columns else None
                        col_open  = df["Open"][code]  if code in df["Open"].columns  else None
                        col_high  = df["High"][code]  if code in df["High"].columns  else None
                        col_low   = df["Low"][code]   if code in df["Low"].columns   else None
                        col_vol   = df["Volume"][code] if code in df["Volume"].columns else None
                    else:
                        col_close = df["Close"]  if len(codes) == 1 else None
                        col_open  = df["Open"]   if len(codes) == 1 else None
                        col_high  = df["High"]   if len(codes) == 1 else None
                        col_low   = df["Low"]    if len(codes) == 1 else None
                        col_vol   = df["Volume"] if len(codes) == 1 else None

                    if col_close is None or col_close.dropna().empty:
                        continue

                    cur_price  = float(col_close.dropna().iloc[-1])
                    open_p     = float(col_open.dropna().iloc[-1])  if col_open  is not None and not col_open.dropna().empty  else 0.0
                    high_p     = float(col_high.dropna().max())     if col_high  is not None and not col_high.dropna().empty  else 0.0
                    low_p      = float(col_low.dropna().min())      if col_low   is not None and not col_low.dropna().empty   else 0.0
                    volume     = int(col_vol.dropna().iloc[-1])     if col_vol   is not None and not col_vol.dropna().empty   else 0

                    if cur_price <= 0:
                        continue

                    result = {
                        "code":       code,
                        "name":       code,
                        "cur_price":  round(cur_price, 4),
                        "open":       round(open_p, 4),
                        "high":       round(high_p, 4),
                        "low":        round(low_p,  4),
                        "prev_close": 0.0,
                        "change_pct": 0.0,
                        "volume":     volume,
                        "exch_cd":    exch_cd,
                        "ok":         True,
                        "source":     "yfinance_batch",
                    }
                    self._price_cache[cache_key] = (result, now_ts)
                    filled += 1
                except Exception as _e:
                    logger.debug(f"[USBroker.prefetch] {code} 파싱 실패: {_e}")
                    continue

            logger.info(
                f"[USBroker.prefetch] 배치 현재가 {filled}/{len(codes)}개 캐시 완료 "
                f"(yfinance download)"
            )
            return filled

        except Exception as e:
            logger.warning(f"[USBroker.prefetch] 배치 조회 실패: {e} — 개별 조회로 폴백")
            return 0

    # ════════════════════════════════════════════════════════════
    # 3. 분봉(1분) 조회
    # ════════════════════════════════════════════════════════════

    def get_1min_candles(self, code: str, exch_cd: str = EXCH_NASD,
                         force: bool = False) -> List[dict]:
        """
        미국 주식 1분봉 조회.
        API: HHDFS76240000 (해외주식 분봉조회)

        Returns: 최신봉이 [0], 과거 순 정렬 (최대 30봉)
            [{
              "time":   str,      # "HHmmss"
              "open":   float,
              "high":   float,
              "low":    float,
              "close":  float,
              "volume": int,
            }, ...]
        """
        cache_key = f"1m:{exch_cd}:{code}"
        now_ts = time.time()
        if not force and cache_key in self._candle_cache:
            data, ts = self._candle_cache[cache_key]
            if now_ts - ts < _CANDLE_TTL:
                return data

        url = f"{self.BASE_URL}/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
        params = {
            "AUTH":    "",
            "EXCD":    exch_cd,
            "SYMB":    code,
            "NMIN":    "1",     # 1분봉
            "PINC":    "1",     # 전일 포함
            "NEXT":    "",
            "NREC":    "30",    # 최대 30건
            "FILL":    "",
            "KEYB":    "",
        }
        resp = self._get(url, "HHDFS76240000", params)
        if resp.get("rt_cd") != "0":
            msg_cd = resp.get("msg_cd", "")
            if msg_cd == "OPSQ2001":
                logger.debug(f"[USBroker] 1분봉 KIS 야간모드({code}) → yfinance 폴백")
                return self._get_1min_candles_yfinance(code, exch_cd, now_ts)
            logger.warning(f"[USBroker] 1분봉 조회 실패 {code}: {resp.get('msg1')}")
            return []

        candles = []
        for item in resp.get("output2", []):
            try:
                candles.append({
                    "time":   item.get("kymd", "") + item.get("khms", ""),
                    "open":   float(item.get("open", 0) or 0),
                    "high":   float(item.get("high", 0) or 0),
                    "low":    float(item.get("low",  0) or 0),
                    "close":  float(item.get("last", 0) or 0),
                    "volume": int(float(item.get("evol", 0) or 0)),
                })
            except (ValueError, TypeError):
                continue

        # ★ KIS rt_cd=0이어도 output2 비어있으면 yfinance fallback
        # 개장 직후 / 야간 잔고조회 구간에서 빈 응답이 옴
        if not candles:
            logger.debug(
                f"[USBroker] 1분봉 KIS 빈응답({code}) rt_cd=0 output2=[] → yfinance 폴백"
            )
            return self._get_1min_candles_yfinance(code, exch_cd, now_ts)

        self._candle_cache[cache_key] = (candles, now_ts)
        return candles

    def _get_1min_candles_yfinance(self, code: str, exch_cd: str, now_ts: float) -> list:
        """yfinance 1분봉 폴백 (KIS OPSQ2001 / 빈응답 시 사용). 최근 75개 반환.

        ★ period='5d' 사용 이유:
           period='1d'는 개장 직후(09:30 ET 직후 18분 이내 등)에 데이터가
           극소수(4개 이하)만 반환되어 _CANDLE_MIN=5 조건 미달 → 전종목 SKIP.
           period='5d'로 넉넉히 가져와 최신 75봉만 사용.

        ★ 75봉 이유 [개선 2026-06-30]:
           1분봉 30개 → 5분봉 6개 → RSI 계산 불가(period+1=15봉 미만) → RSI=50.0 고정
           1분봉 75개 → 5분봉 15개 → RSI(14기간) 정상 계산 가능
        """
        try:
            import yfinance as yf
            hist = yf.Ticker(code).history(period="5d", interval="1m")
            if hist.empty:
                return []
            candles = []
            for ts_idx, row in hist.iloc[::-1].head(75).iterrows():  # 30→75
                candles.append({
                    "time":   ts_idx.strftime("%Y%m%d%H%M%S"),
                    "open":   float(row["Open"]),
                    "high":   float(row["High"]),
                    "low":    float(row["Low"]),
                    "close":  float(row["Close"]),
                    "volume": int(row["Volume"]),
                })
            cache_key = f"1m:{exch_cd}:{code}"
            self._candle_cache[cache_key] = (candles, now_ts)
            logger.debug(
                f"[USBroker] yfinance 1분봉 폴백 성공 {code}: {len(candles)}봉"
            )
            return candles
        except Exception as e:
            logger.warning(f"[USBroker] yfinance 1분봉 폴백 실패 {code}: {e}")
            return []

    # ════════════════════════════════════════════════════════════
    # 4. 5분봉 (1분봉 그룹핑)
    # ════════════════════════════════════════════════════════════

    def get_5min_candles(self, code: str, exch_cd: str = EXCH_NASD,
                         force: bool = False) -> List[dict]:
        """
        미국 주식 5분봉 (1분봉 5개 집계).
        KIS 해외주식 API는 1분봉만 지원하므로 직접 집계.

        Returns: 최신봉 [0] 기준, 최대 15개 (약 75분)
            [{
              "open":   float,
              "high":   float,
              "low":    float,
              "close":  float,
              "volume": int,
            }, ...]
        """
        cache_key = f"5m:{exch_cd}:{code}"
        now_ts = time.time()
        if not force and cache_key in self._candle_cache:
            data, ts = self._candle_cache[cache_key]
            if now_ts - ts < _CANDLE_TTL:
                return data

        candles_1m = self.get_1min_candles(code, exch_cd, force=force)
        if not candles_1m:
            return []

        # 5분 단위 그룹핑 (최신봉[0]부터 역순)
        result: List[dict] = []
        chunk: List[dict] = []
        for c in candles_1m:
            chunk.append(c)
            if len(chunk) == 5:
                result.append(self._merge_candles(chunk))
                chunk = []
        if chunk:
            result.append(self._merge_candles(chunk))

        self._candle_cache[cache_key] = (result, now_ts)
        return result

    @staticmethod
    def _merge_candles(candles: List[dict]) -> dict:
        """복수 봉 → 1개 집계봉."""
        if not candles:
            return {}
        return {
            "open":   candles[-1]["open"],      # 가장 오래된 봉의 시가
            "high":   max(c["high"] for c in candles),
            "low":    min(c["low"]  for c in candles),
            "close":  candles[0]["close"],      # 가장 최신 봉의 종가
            "volume": sum(c["volume"] for c in candles),
        }

    # ════════════════════════════════════════════════════════════
    # 5. 잔고 조회 (총자산 포함)
    # ════════════════════════════════════════════════════════════

    def get_balance(self, force: bool = False) -> dict:
        """
        미국 주식 잔고 조회.
        API: TTTS3012R (해외주식 잔고조회)

        ★ 핵심 필드:
          total_asset   ← tot_evlu_amt  (KRW 환산 총평가금액)
          usd_balance   ← frcr_evlu_pfls_amt (USD 외화잔고)
          holdings      ← 보유 종목 리스트

        Returns:
            {
              "holdings":    list,     # 보유 종목 [{code, name, qty, avg_price, ...}]
              "total_asset": int,      # KRW 환산 총자산 (tot_evlu_amt)
              "usd_balance": float,    # USD 예수금
              "usd_eval":    float,    # 보유주식 USD 평가금액
              "krw_total":   int,      # 원화환산 총평가금액
              "total_pnl":   float,    # 총 평가손익 (KRW)
              "ok":          bool,
            }
        """
        now_ts = time.time()
        if not force and self._balance_cache and now_ts - self._balance_ts < _BALANCE_TTL:
            return self._balance_cache

        url = f"{self.BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {
            "CANO":          self._acc_no,
            "ACNT_PRDT_CD":  self._acc_prod,
            "OVRS_EXCG_CD":  "%" ,   # 전체 거래소
            "TR_CRCY_CD":    "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        resp = self._get(url, self._tr("TTTS3012R", "VTTS3012R"), params)

        if resp.get("rt_cd") != "0":
            logger.warning(f"[USBroker] 잔고 조회 실패: {resp.get('msg1')}")
            return {"holdings": [], "total_asset": 0, "ok": False}

        holdings = []
        for item in resp.get("output1", []):
            try:
                # ★ BUG FIX: KIS API 실제 수량 필드는 "ovrs_cblc_qty"
                #   "cblc_qty13"은 이 API에 존재하지 않아 항상 N/A → qty=0 → 보유종목=0개 오판정
                qty = int(float(item.get("ovrs_cblc_qty", 0) or 0))
                if qty <= 0:
                    continue
                # ★ ord_psbl_qty: 주문가능수량 — TTTS3012R output1에서 제공 (미체결 차감 후 잔량)
                #   KIS rt_cd=7 원인: held_qty>0이지만 미체결 SELL 주문이 있으면 ord_psbl_qty=0
                #   → sell() 에서 ord_psbl_qty를 우선 사용해 주문 차단 사전 감지
                ord_psbl = int(float(item.get("ord_psbl_qty", qty) or qty))
                holdings.append({
                    "code":         item.get("ovrs_pdno", ""),
                    "name":         item.get("ovrs_item_name", ""),
                    "exch_cd":      item.get("ovrs_excg_cd", EXCH_NASD),
                    "qty":          qty,
                    "ord_psbl_qty": ord_psbl,   # ★ 주문가능수량 (미체결 차감 후)
                    "avg_price":    float(item.get("pchs_avg_pric", 0) or 0),
                    "cur_price":    float(item.get("now_pric2", 0) or 0),
                    "eval_amt":     float(item.get("ovrs_stck_evlu_amt", 0) or 0),  # USD
                    "pnl_pct":      float(item.get("evlu_pfls_rt", 0) or 0),
                })
            except (ValueError, TypeError):
                continue

        o2 = resp.get("output2", {})
        if isinstance(o2, list):
            o2 = o2[0] if o2 else {}

        try:
            # ★ TTTS3012R output2 실제 필드 설명:
            #   frcr_pchs_amt1   = 외화매수금액1 (보유종목 평균단가×수량 합계 — 매수원가, 예수금 아님!)
            #   tot_evlu_pfls_amt = 총평가손익금액 (보유주식 평가금액 + 손익, USD)
            #   ovrs_tot_pfls    = 해외평가손익 합계 (USD)
            # ★ 외화예수금(현금)은 TTTS3012R output2에 존재하지 않음
            #   → TTTS3011R(get_orderable_usd)이 유일한 정확한 USD 예수금 소스
            #   → tot_evlu_pfls_amt를 US 자산 총액 근사값으로 사용
            total_asset = int(float(o2.get("tot_evlu_pfls_amt", 0) or 0))  # USD (주식평가+손익)
            usd_eval    = total_asset  # USD 주식 평가금액 (현금 제외)
            usd_balance = usd_eval     # ★ 예수금 별도 필드 없음 → 평가금액으로 대체 (보수적 추정)
            total_pnl   = float(o2.get("ovrs_tot_pfls", 0) or 0)           # USD 평가손익
            # KRW 환산 총평가금액: TTTS3012R에는 원화필드 없음 → 0으로 초기화 (caller에서 환율환산)
            krw_total   = 0
        except (ValueError, TypeError):
            total_asset = 0
            usd_balance = 0.0
            usd_eval    = 0.0
            total_pnl   = 0.0
            krw_total   = 0

        # ── holdings에서 보유주식 USD 평가합산 ────────────────────────────
        # output1의 ovrs_stck_evlu_amt 합산 → 주식평가 USD
        holdings_eval_usd = sum(
            h.get("eval_amt", 0.0) for h in holdings
        )
        # tot_evlu_pfls_amt(USD) >= holdings_eval_usd 이므로 차이가 현금에 근접
        # 단, TTTS3011R(orderable)이 정확한 USD 예수금 — 여기선 근사값만 제공
        if holdings_eval_usd > 0 and total_asset >= holdings_eval_usd:
            usd_balance = round(total_asset - holdings_eval_usd, 6)  # 현금 추정 (USD)

        result = {
            "holdings":    holdings,
            "total_asset": total_asset,   # ★ USD 총평가금액 (주식+손익)
            "usd_balance": usd_balance,   # USD 현금 추정 (주식평가 제외분 or tot_evlu_pfls_amt)
            "usd_eval":    holdings_eval_usd,  # 보유주식 USD 평가금액
            "krw_total":   krw_total,
            "total_pnl":   total_pnl,
            "ok":          True,
        }
        self._balance_cache = result
        self._balance_ts    = now_ts
        return result

    # ════════════════════════════════════════════════════════════
    # 6. USD 주문가능금액 조회
    # ════════════════════════════════════════════════════════════

    def get_orderable_usd(self, code: str = "",
                          exch_cd: str = EXCH_NASD,
                          price: float = 0.0,
                          force: bool = False) -> float:
        """
        USD 주문가능금액 조회.
        API: TTTS3011R (해외주식 매수가능금액조회)

        ★ 60초 TTL 캐시 적용 — 20종목 루프에서 매 종목 API 호출하던 것을 차단.
          루프 1바퀴(약 30~90초) 중 최초 1회만 API 호출, 이후는 캐시 반환.

        Returns: 주문가능 USD (float)
        """
        now_ts = time.time()
        # 캐시 유효 시 즉시 반환 (force=True면 무시)
        if not force and now_ts - self._orderable_ts < _ORDERABLE_TTL:
            return self._orderable_cache

        url = f"{self.BASE_URL}/uapi/overseas-stock/v1/trading/inquire-psamount"
        params = {
            "CANO":         self._acc_no,
            "ACNT_PRDT_CD": self._acc_prod,
            "OVRS_EXCG_CD": exch_cd,
            "OVRS_ORD_UNPR": str(price) if price > 0 else "0",
            "ITEM_CD":       code,
        }
        resp = self._get(url, self._tr("TTTS3011R", "VTTS3011R"), params)
        if resp.get("rt_cd") != "0":
            msg_cd = resp.get("msg_cd", "")
            msg1   = resp.get("msg1", "")
            # OPSQ0002: 미국장 시간 외 호출 — 정상적인 폴백 상황, DEBUG로 낮춤
            if msg_cd == "OPSQ0002":
                # ★ 장외시간: TTL 캐시를 갱신하지 않고, 마지막 성공값이 있으면 그대로 반환
                if self._orderable_last_ok > 0:
                    logger.debug(
                        f"[USBroker] 주문가능금액 조회 불가(장외시간): {msg1} "
                        f"→ 마지막 성공값 ${self._orderable_last_ok:.2f} 사용"
                    )
                    # TTL 캐시도 갱신하여 다음 루프에서 재조회 안 하도록 설정
                    self._orderable_cache = self._orderable_last_ok
                    self._orderable_ts    = now_ts
                    return self._orderable_last_ok
                else:
                    logger.debug(f"[USBroker] 주문가능금액 조회 불가(장외시간): {msg1} (이전 성공값 없음)")
            else:
                logger.warning(f"[USBroker] 주문가능금액 조회 실패({msg_cd}): {msg1}")
            return 0.0
        o = resp.get("output", {})
        try:
            result = float(o.get("ord_psbl_frcr_amt", 0) or 0)
            # 캐시 갱신 (성공 시에만) + 영속 마지막 성공값 저장
            self._orderable_cache    = result
            self._orderable_ts       = now_ts
            if result > 0:
                self._orderable_last_ok = result   # ★ 장외 폴백용 영속 저장
                logger.debug(f"[USBroker] 주문가능금액 조회 성공: ${result:.2f} → 영속캐시 갱신")
            return result
        except (ValueError, TypeError):
            return 0.0

    # ════════════════════════════════════════════════════════════
    # 7. 매수 주문
    # ════════════════════════════════════════════════════════════

    def buy(self, code: str, qty: int, price: float,
            exch_cd: str = EXCH_NASD,
            ord_dvsn: str = ORD_LIMIT) -> dict:
        """
        미국 주식 매수 주문.
        API: TTTT1002U (해외주식 주문)

        Args:
            code:     종목코드 (예: "AAPL")
            qty:      주문수량
            price:    주문가격 (USD, 시장가=0.0)
            exch_cd:  거래소 코드
            ord_dvsn: "00"=지정가, "01"=시장가

        Returns:
            {
              "ok":      bool,
              "ord_no":  str,
              "msg":     str,
              "rt_cd":   str,
            }
        """
        # 사전 검증
        err = self._validate_order(code, qty, price, ord_dvsn, exch_cd, side="BUY")
        if err:
            return err

        url  = f"{self.BASE_URL}/uapi/overseas-stock/v1/trading/order"
        tr_id = self._tr("TTTT1002U", "VTTT1002U")
        body = {
            "CANO":          self._acc_no,
            "ACNT_PRDT_CD":  self._acc_prod,
            "OVRS_EXCG_CD":  exch_cd,
            "PDNO":          code,
            "ORD_DVSN":      ord_dvsn,
            "ORD_QTY":       str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}" if price > 0 else "0",
            "ORD_SVR_DVSN_CD": "0",
        }
        resp = self._post(url, tr_id, body)

        ok = resp.get("rt_cd") == "0"
        o  = resp.get("output", {})
        msg_cd = resp.get("msg_cd", "")
        result = {
            "ok":     ok,
            "ord_no": o.get("ODNO", ""),
            "msg":    resp.get("msg1", ""),
            "rt_cd":  resp.get("rt_cd", "9"),
            "msg_cd": msg_cd,
        }
        if ok:
            logger.info(
                f"✅ [BUY] {code} {qty}주 @ ${price:.2f} "
                f"ord_dvsn={ord_dvsn} ord_no={result['ord_no']}"
            )
            self._balance_ts = 0.0   # 잔고 캐시 무효화
        else:
            # ★ 영구차단 에러(APBK0656=종목정보없음, APBK1672=ETP미신청) → 워치리스트 제거용 플래그
            if msg_cd in ("APBK0656", "APBK1672"):
                self._perm_banned.add(code)
                logger.error(
                    f"❌ [BUY PERM_BAN] {code} — {msg_cd}({result['msg']}) "
                    f"→ _perm_banned 등록, 워치리스트에서 영구 제거 필요"
                )
            else:
                logger.warning(
                    f"❌ [BUY FAIL] {code} {qty}주 @ ${price:.2f} "
                    f"rt_cd={result['rt_cd']} msg_cd={msg_cd} msg={result['msg']!r}"
                )
        return result

    # ════════════════════════════════════════════════════════════
    # 8. 매도 주문
    # ════════════════════════════════════════════════════════════

    def sell(self, code: str, qty: int, price: float,
             exch_cd: str = EXCH_NASD,
             ord_dvsn: str = ORD_MARKET) -> dict:
        """
        미국 주식 매도 주문.
        API: TTTT1006U (해외주식 매도주문)

        실보유수량 확인 후 자동 보정:
          실보유 < qty → 실보유로 수량 조정
          실보유 = 0   → 주문 차단
        """
        # 실보유 수량 확인
        # ★ 캐시 우선 → 캐시 0이면 force=True 재조회 (API 타임아웃 후 캐시 stale 방어)
        bal = self.get_balance(force=False)
        held_qty     = 0
        ord_psbl_qty = 0   # ★ 주문가능수량 (미체결 매도 차감 후)
        held_exch    = exch_cd
        for h in bal.get("holdings", []):
            if h["code"].upper() == code.upper():
                held_qty     = h["qty"]
                ord_psbl_qty = h.get("ord_psbl_qty", held_qty)
                held_exch    = h.get("exch_cd", exch_cd)
                break

        if held_qty <= 0:
            # 캐시에 없으면 강제 재조회 (타임아웃 후 stale 캐시 대비)
            bal2 = self.get_balance(force=True)
            for h in bal2.get("holdings", []):
                if h["code"].upper() == code.upper():
                    held_qty     = h["qty"]
                    ord_psbl_qty = h.get("ord_psbl_qty", held_qty)
                    held_exch    = h.get("exch_cd", exch_cd)
                    break

        # 재조회 후에도 0이면 호출자(us_strategy)가 전달한 qty를 그대로 신뢰
        # (us_strategy는 KIS 실잔고 기준으로 qty를 검증한 후 전달함)
        if held_qty <= 0:
            logger.warning(
                f"[USBroker] 매도 수량 미확인 — {code} KIS캐시 held=0, "
                f"호출자 qty={qty} 신뢰 진행"
            )
            held_qty     = qty   # 호출자 전달값 신뢰
            ord_psbl_qty = qty
            held_exch    = exch_cd

        exch_cd = held_exch

        if qty > held_qty:
            logger.warning(f"[USBroker] 수량 조정 — {code}: {qty}주 → {held_qty}주")
            qty = held_qty

        # ★ rt_cd=7 사전 감지: 주문가능수량(ord_psbl_qty)이 0이면 미체결 매도 주문 존재 가능성
        # KIS는 동일 종목 매도 주문 중복 불가 → 미체결 주문 있으면 orderable=0 반환
        # sell()은 주문만 발행; 미체결 주문 취소는 호출자(_kis_hard_stop_airbag)가 담당
        if ord_psbl_qty <= 0 and held_qty > 0:
            logger.warning(
                f"[USBroker] rt_cd=7 위험 — {code} held={held_qty} "
                f"but ord_psbl_qty={ord_psbl_qty} (미체결 SELL 주문 존재 가능)"
            )

        # 사전 검증
        err = self._validate_order(code, qty, price, ord_dvsn, exch_cd, side="SELL")
        if err:
            return err

        url   = f"{self.BASE_URL}/uapi/overseas-stock/v1/trading/order"
        tr_id = self._tr("TTTT1006U", "VTTT1006U")
        body  = {
            "CANO":            self._acc_no,
            "ACNT_PRDT_CD":    self._acc_prod,
            "OVRS_EXCG_CD":    exch_cd,
            "PDNO":            code,
            "ORD_DVSN":        ord_dvsn,
            "ORD_QTY":         str(qty),
            "OVRS_ORD_UNPR":   f"{price:.2f}" if price > 0 else "0",
            "ORD_SVR_DVSN_CD": "0",
        }
        resp = self._post(url, tr_id, body)

        ok = resp.get("rt_cd") == "0"
        o  = resp.get("output", {})
        result = {
            "ok":           ok,
            "ord_no":       o.get("ODNO", ""),
            "msg":          resp.get("msg1", ""),
            "rt_cd":        resp.get("rt_cd", "9"),
            "qty":          qty,
            "held_qty":     held_qty,
            "ord_psbl_qty": ord_psbl_qty,  # ★ 주문가능수량 (rt_cd=7 진단용)
        }
        if ok:
            logger.info(
                f"✅ [SELL] {code} {qty}주 @ ${price:.2f} "
                f"ord_dvsn={ord_dvsn} ord_no={result['ord_no']}"
            )
            self._balance_ts = 0.0
        else:
            logger.warning(
                f"❌ [SELL FAIL] {code} {qty}주 @ ${price:.2f} "
                f"rt_cd={result['rt_cd']} msg={result['msg']!r}"
            )
        return result

    # ════════════════════════════════════════════════════════════
    # 9. 미체결 주문 조회 / 취소
    # ════════════════════════════════════════════════════════════

    def get_pending_orders(self) -> List[dict]:
        """
        미체결 주문 조회.
        API: TTTS3035R (해외주식 미체결내역)
        """
        url = f"{self.BASE_URL}/uapi/overseas-stock/v1/trading/inquire-nccs"
        params = {
            "CANO":          self._acc_no,
            "ACNT_PRDT_CD":  self._acc_prod,
            "OVRS_EXCG_CD":  "%",
            "SORT_SQN":      "DS",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        resp = self._get(url, self._tr("TTTS3035R", "VTTS3035R"), params)
        if resp.get("rt_cd") != "0":
            return []

        orders = []
        for item in resp.get("output", []):
            try:
                remaining = int(float(item.get("rmn_qty", 0) or 0))
                if remaining <= 0:
                    continue
                orders.append({
                    "ord_no":  item.get("odno", ""),
                    "code":    item.get("pdno", ""),
                    "name":    item.get("prdt_name", ""),
                    "exch_cd": item.get("ovrs_excg_cd", ""),
                    "side":    item.get("sll_buy_dvsn_cd", ""),  # "01"=BUY, "02"=SELL
                    "qty":     int(float(item.get("ord_qty", 0) or 0)),
                    "remaining": remaining,
                    "price":   float(item.get("ft_ord_unpr3", 0) or 0),
                    "ord_dvsn": item.get("ord_dvsn", ""),
                })
            except (ValueError, TypeError):
                continue
        return orders

    def cancel_order(self, ord_no: str, code: str,
                     exch_cd: str, qty: int) -> bool:
        """
        미체결 주문 취소.
        API: TTTT1004U (해외주식 주문취소)
        """
        url   = f"{self.BASE_URL}/uapi/overseas-stock/v1/trading/order-rvsecncl"
        tr_id = self._tr("TTTT1004U", "VTTT1004U")
        body  = {
            "CANO":           self._acc_no,
            "ACNT_PRDT_CD":   self._acc_prod,
            "OVRS_EXCG_CD":   exch_cd,
            "PDNO":           code,
            "ORGN_ODNO":      ord_no,
            "ORD_DVSN":       "00",
            "RVSE_CNCL_DVSN_CD": "02",   # 02=취소
            "ORD_QTY":        str(qty),
            "OVRS_ORD_UNPR":  "0",
            "ORD_SVR_DVSN_CD": "0",
        }
        resp = self._post(url, tr_id, body)
        ok = resp.get("rt_cd") == "0"
        if ok:
            logger.info(f"✅ [CANCEL] ord_no={ord_no} {code} {qty}주")
        else:
            logger.warning(f"❌ [CANCEL FAIL] ord_no={ord_no}: {resp.get('msg1')}")
        return ok

    def cancel_all_buy_orders(self) -> int:
        """미체결 BUY 주문 전량 취소. Returns: 취소 성공 건수."""
        orders  = self.get_pending_orders()
        buy_orders = [o for o in orders if o["side"] == "01"]
        n = 0
        for o in buy_orders:
            if self.cancel_order(o["ord_no"], o["code"], o["exch_cd"], o["remaining"]):
                n += 1
            time.sleep(0.3)
        if n:
            logger.info(f"[USBroker] 미체결 BUY {n}건 취소 완료")
        return n

    # ════════════════════════════════════════════════════════════
    # 9-2. 체결내역 조회 (당일)
    # ════════════════════════════════════════════════════════════

    def get_executed_orders(self, start_date: str = "", end_date: str = "") -> List[dict]:
        """
        당일 미국 체결내역 조회.
        API: TTTS3035R (해외주식 주문 체결내역 — filled 포함)
        실무: 별도 체결조회 API CTRP6548R 대신 잔고 기반으로 보완

        Returns:
            [{"order_no", "code", "name", "side", "qty", "price",
              "filled_qty", "filled_price", "filled_time", "market", ...}, ...]
        """
        from datetime import date as _date
        today = _date.today().strftime("%Y%m%d")
        if not start_date:
            start_date = today

        url = f"{self.BASE_URL}/uapi/overseas-stock/v1/trading/inquire-ccnl"
        params = {
            "CANO":           self._acc_no,
            "ACNT_PRDT_CD":   self._acc_prod,
            "PDNO":           "",
            "ORD_STRT_DT":    start_date,
            "ORD_END_DT":     end_date or today,
            "SLL_BUY_DVSN_CD": "00",   # 00=전체
            "CCLD_NCCS_DVSN": "01",    # 01=체결
            "OVRS_EXCG_CD":   "%",
            "SORT_SQN":       "DS",
            "ORD_DT":         "",
            "ORD_GNO_BRNO":   "",
            "ODNO":           "",
            "CTX_AREA_NK200": "",
            "CTX_AREA_FK200": "",
        }
        resp = self._get(url, self._tr("TTTS3035R", "VTTS3035R"), params)
        if resp.get("rt_cd") != "0":
            msg_cd = resp.get('msg_cd', '?')
            msg1   = resp.get('msg1',   '?')
            # OPSQ0002/OPSQ2001 = 장외시간 조회불가 (정상 폴백)
            if msg_cd in ("OPSQ0002", "OPSQ2001"):
                logger.debug(f"[USBroker] 체결내역 조회 장외시간: {msg_cd} {msg1}")
            else:
                logger.warning(f"[USBroker] 체결내역 조회 실패: msg_cd={msg_cd} | {msg1}")
            return []

        results = []
        for item in resp.get("output", []):
            try:
                filled_qty = int(float(item.get("ft_ccld_qty", 0) or 0))
                if filled_qty <= 0:
                    continue
                side_cd = item.get("sll_buy_dvsn_cd", "")  # "01"=BUY, "02"=SELL
                side    = "BUY" if side_cd == "01" else "SELL"
                results.append({
                    "order_no":      item.get("odno",         ""),
                    "code":          item.get("pdno",         "").strip(),
                    "name":          item.get("prdt_name",    "").strip(),
                    "side":          side,
                    "qty":           int(float(item.get("ord_qty", 0) or 0)),
                    "price":         float(item.get("ft_ord_unpr3", 0) or 0),
                    "filled_qty":    filled_qty,
                    "filled_price":  float(item.get("ft_ccld_unpr3", 0) or 0),
                    "filled_time":   item.get("ord_tmd", ""),      # HHMMSS
                    "filled_date":   item.get("ord_dt",  today),
                    "exch_cd":       item.get("ovrs_excg_cd", EXCH_NASD),
                    "status":        "FILLED",
                    "market":        "US",
                })
            except (ValueError, TypeError):
                continue
        return results

    def get_us_executed_orders_normalized(self, days: int = 1) -> list:
        """
        GAP2 UsKisFillSource용 — get_executed_orders() 정규화 결과 반환 (래퍼).

        ★ 명칭 변경 이유 (MEDIUM-5):
          이전 이름 get_us_order_history_raw()는 "raw" 응답을 암시하지만,
          실제 반환값은 get_executed_orders()가 정규화한 dict이다.
          혼동 방지를 위해 get_us_executed_orders_normalized()로 변경.

        반환 구조 (get_executed_orders()와 동일):
          list[dict] — fields: order_no, code, name, side, qty, price,
                       filled_qty, filled_price, filled_time, exch_cd, market
        """
        from datetime import date as _date
        today = _date.today().strftime("%Y%m%d")
        if days > 1:
            from datetime import timedelta
            start = (_date.today() - timedelta(days=days - 1)).strftime("%Y%m%d")
        else:
            start = today
        return self.get_executed_orders(start_date=start, end_date=today)

    # 하위 호환성 alias (deprecated — 사용 금지, get_us_executed_orders_normalized 사용)
    def get_us_order_history_raw(self, days: int = 1) -> list:
        """Deprecated alias → get_us_executed_orders_normalized() 호출."""
        import warnings
        warnings.warn(
            "get_us_order_history_raw() is deprecated. "
            "Use get_us_executed_orders_normalized() instead.",
            DeprecationWarning, stacklevel=2,
        )
        return self.get_us_executed_orders_normalized(days=days)

    # ════════════════════════════════════════════════════════════
    # 10. 주문 사전 검증
    # ════════════════════════════════════════════════════════════

    def _validate_order(self, code: str, qty: int, price: float,
                         ord_dvsn: str, exch_cd: str, side: str) -> Optional[dict]:
        """
        주문 전 필수 검증. 오류 시 error dict 반환, 정상이면 None.

        V1: 수량 ≥ 1
        V2: 거래소 코드 유효성
        V3: ORD_DVSN / 가격 조합
        V4: BUY 시 장후시간외 금지
        V5: BUY 시 price > 0 (지정가) 또는 0 (시장가)
        """
        # V1: 수량
        if qty < 1:
            msg = f"[주문 차단] 수량 {qty} < 1"
            logger.warning(msg)
            return {"ok": False, "msg": msg, "rt_cd": "9"}

        # V2: 거래소 코드
        if exch_cd not in SUPPORTED_EXCHANGES:
            msg = f"[주문 차단] 지원하지 않는 거래소: {exch_cd}"
            logger.warning(msg)
            return {"ok": False, "msg": msg, "rt_cd": "9"}

        # V3: ORD_DVSN / 가격 조합
        has_price = price > 0
        if ord_dvsn == ORD_LIMIT and not has_price:
            msg = f"[주문 차단] 지정가(00)인데 가격=0 — {code}"
            logger.warning(msg)
            return {"ok": False, "msg": msg, "rt_cd": "9"}
        if ord_dvsn == ORD_MARKET and has_price:
            msg = f"[주문 차단] 시장가(01)인데 가격={price} — {code}"
            logger.warning(msg)
            return {"ok": False, "msg": msg, "rt_cd": "9"}

        # V4: BUY 시 장 시간 검증
        if side == "BUY":
            now_kst = datetime.now(KST)
            if not self.is_buy_allowed(now_kst):
                msg = f"[주문 차단] 미국장 BUY 불가 시간 {now_kst.strftime('%H:%M KST')}"
                logger.warning(msg)
                return {"ok": False, "msg": msg, "rt_cd": "9"}

        return None

    # ════════════════════════════════════════════════════════════
    # 11. 틱 사이즈 (미국: SEC 규정 Reg NMS sub-penny rule)
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def tick_size(price: float) -> float:
        """
        미국 주식 최소 호가 단위.
        $1 이상: $0.01, $1 미만: $0.0001
        """
        return 0.01 if price >= 1.0 else 0.0001

    @classmethod
    def round_to_tick(cls, price: float, direction: str = "down") -> float:
        """
        호가 단위로 반올림.
        direction: "up" | "down"
        """
        tick = cls.tick_size(price)
        if tick == 0:
            return price
        if direction == "down":
            return float(int(price / tick) * tick)
        else:
            import math
            return float(math.ceil(price / tick) * tick)
