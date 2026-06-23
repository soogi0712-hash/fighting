"""
broker/kis_base.py — KIS API 공통 베이스 레이어 (V2)
======================================================
역할:
  - OAuth2 토큰 발급 / 자동 갱신
  - 공통 헤더 생성
  - TPS rate-limit (adaptive backoff)
  - HTTP retry 로직 (3회, 지수 대기)
  - 에러 분류 / 로그

★ 상속 구조:
    KISBase
    ├── KRBroker  (국내장 시세·잔고·주문)
    └── USBroker  (해외장 시세·잔고·주문)  ← 추후 추가
"""

import os
import time
import json
import hashlib
import requests
import pytz
from datetime import datetime, timedelta
from typing import Optional

from utils.v2_logger import get_logger

logger = get_logger("KISBase")

KST = pytz.timezone("Asia/Seoul")

# 토큰 파일 경로 (재시작 후 재사용으로 1분 레이트리밋 회피)
_TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "kis_token_cache.json")

# ── TPS 제어 ────────────────────────────────────────────────────
_API_MIN_INTERVAL_SEC = 0.10      # 100ms → 초당 최대 ~10건 (KIS 실전 제한 20건/초, 여유 충분)
_MAX_BACKOFF_SEC      = 60.0      # 최대 backoff 1분
_BACKOFF_STEP_SEC     = [3, 10, 30, 60]  # 연속 에러 횟수별 단계


class KISBase:
    """
    KIS Open API 공통 베이스.
    서브클래스에서 super().__init__()로 초기화 후 사용.
    """

    BASE_URL = "https://openapi.koreainvestment.com:9443"

    def __init__(self, app_key: str, app_secret: str, account_no: str):
        self.app_key    = app_key
        self.app_secret = app_secret
        self.account_no = account_no   # "XXXXXXXXXX-01" 형태

        # 계좌번호 / 상품코드 분리
        if "-" in account_no:
            self._acc_no, self._acc_prod = account_no.split("-", 1)
        else:
            self._acc_no, self._acc_prod = account_no, "01"

        # ── 토큰 상태 ────────────────────────────────────────────
        self._access_token:   Optional[str]      = None
        self._token_expires:  Optional[datetime] = None

        # ── TPS / Backoff ────────────────────────────────────────
        self._last_call_ts:      float = 0.0
        self._backoff_until:     float = 0.0
        self._consecutive_errors: int  = 0

    # ════════════════════════════════════════════════════════════
    # 1. OAuth2 토큰
    # ════════════════════════════════════════════════════════════

    def token(self) -> str:
        """유효한 액세스 토큰 반환 (만료 전 자동 재발급, 파일 캐시 재사용)."""
        now = datetime.now()
        if self._access_token and self._token_expires and now < self._token_expires:
            return self._access_token
        # 파일 캐시에서 재사용 시도 (재시작 후 1분 레이트리밋 회피)
        cached = self._load_token_cache()
        if cached:
            return cached
        return self._issue_token()

    def _load_token_cache(self) -> Optional[str]:
        """파일에서 토큰 캐시 로드 (유효 시 반환, 만료 시 None)."""
        try:
            cache_path = os.path.normpath(_TOKEN_CACHE_FILE)
            if not os.path.exists(cache_path):
                return None
            with open(cache_path, "r") as f:
                data = json.load(f)
            expires_at = datetime.fromisoformat(data["expires_at"])
            if datetime.now() >= expires_at:
                return None
            self._access_token  = data["access_token"]
            self._token_expires = expires_at
            logger.info("✅ KIS 토큰 캐시 재사용 (잔여 %d분)" %
                        int((expires_at - datetime.now()).total_seconds() / 60))
            return self._access_token
        except Exception:
            return None

    def _save_token_cache(self, token: str, expires: datetime) -> None:
        """토큰을 파일에 캐시 저장."""
        try:
            cache_path = os.path.normpath(_TOKEN_CACHE_FILE)
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump({"access_token": token,
                           "expires_at":   expires.isoformat()}, f)
        except Exception as e:
            logger.warning(f"토큰 캐시 저장 실패: {e}")

    def _issue_token(self) -> str:
        url  = f"{self.BASE_URL}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey":     self.app_key,
            "appsecret":  self.app_secret,
        }
        try:
            resp = requests.post(url, json=body, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            self._access_token  = data["access_token"]
            expires_in          = int(data.get("expires_in", 86400))
            self._token_expires = datetime.now() + timedelta(seconds=expires_in - 300)
            self._save_token_cache(self._access_token, self._token_expires)
            logger.info("✅ KIS 액세스 토큰 발급 성공")
            return self._access_token
        except Exception as e:
            logger.error(f"❌ 토큰 발급 실패: {e}")
            raise

    # ════════════════════════════════════════════════════════════
    # 2. 헤더 생성
    # ════════════════════════════════════════════════════════════

    def headers(self, tr_id: str, use_hash: bool = False, body: dict = None) -> dict:
        """KIS API 공통 요청 헤더."""
        h = {
            "content-type":  "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token()}",
            "appkey":         self.app_key,
            "appsecret":      self.app_secret,
            "tr_id":          tr_id,
            "custtype":       "P",
        }
        if use_hash and body:
            h["hashkey"] = self._hashkey(body)
        return h

    def _hashkey(self, body: dict) -> str:
        url  = f"{self.BASE_URL}/uapi/hashkey"
        hdrs = {
            "content-type": "application/json",
            "appkey":        self.app_key,
            "appsecret":     self.app_secret,
        }
        try:
            resp = requests.post(url, headers=hdrs, json=body, timeout=5)
            return resp.json().get("HASH", "")
        except Exception:
            return ""

    # ════════════════════════════════════════════════════════════
    # 3. TPS Rate-limit & Adaptive Backoff
    # ════════════════════════════════════════════════════════════

    def _rate_limit(self):
        """API 호출 전 최소 간격 보장 + backoff 대기."""
        # backoff 대기 (연속 에러 시)
        remain = self._backoff_until - time.time()
        if remain > 0:
            logger.debug(f"[RateLimit] backoff 대기 {remain:.1f}s")
            time.sleep(min(remain, _MAX_BACKOFF_SEC))

        # 최소 간격 보장
        elapsed = time.time() - self._last_call_ts
        if elapsed < _API_MIN_INTERVAL_SEC:
            time.sleep(_API_MIN_INTERVAL_SEC - elapsed)
        self._last_call_ts = time.time()

    def _on_success(self):
        """API 성공 시 에러 카운터 리셋."""
        self._consecutive_errors = 0
        self._backoff_until      = 0.0

    def _on_error(self, status_code: int = 0, msg_cd: str = ""):
        """API 에러 시 backoff 단계 증가."""
        self._consecutive_errors += 1
        step_idx = min(self._consecutive_errors - 1, len(_BACKOFF_STEP_SEC) - 1)
        wait     = _BACKOFF_STEP_SEC[step_idx]
        self._backoff_until = time.time() + wait
        logger.warning(
            f"[RateLimit] {status_code} 에러 연속 {self._consecutive_errors}회 "
            f"→ {wait}s backoff (msg_cd={msg_cd})"
        )

    # ════════════════════════════════════════════════════════════
    # 4. HTTP 요청 래퍼 (GET / POST, retry 3회)
    # ════════════════════════════════════════════════════════════

    def _get(self, url: str, tr_id: str, params: dict,
             max_retry: int = 3) -> dict:
        """GET 요청 with retry."""
        for attempt in range(max_retry):
            try:
                self._rate_limit()
                resp = requests.get(url, headers=self.headers(tr_id),
                                    params=params, timeout=10)

                if resp.status_code == 500:
                    self._on_error(500, self._extract_msg_cd(resp))
                    if attempt < max_retry - 1:
                        time.sleep(_BACKOFF_STEP_SEC[min(attempt, 3)])
                        continue
                    return {"rt_cd": "9", "msg1": "HTTP 500", "_http_status": 500}

                resp.raise_for_status()
                data = resp.json()

                if data.get("rt_cd") != "0":
                    mc = data.get("msg_cd", "")
                    if mc == "EGW00201" and attempt < max_retry - 1:   # TPS 초과
                        self._on_error(200, mc)
                        continue
                    # OPSQ0002: 장외시간 조회 불가 — 정상 폴백 상황, DEBUG로 낮춤
                    # OPSQ2001: KIS 야간 시세조회 불가 — yfinance 폴백 처리됨, DEBUG
                    if mc in ("OPSQ0002", "OPSQ2001"):
                        logger.debug(
                            f"[GET] rt_cd={data.get('rt_cd')} "
                            f"msg_cd={mc} msg1={data.get('msg1','?')!r}"
                        )
                    else:
                        logger.warning(
                            f"[GET] rt_cd={data.get('rt_cd')} "
                            f"msg_cd={mc} msg1={data.get('msg1','?')!r}"
                        )
                    return data

                self._on_success()
                return data

            except requests.RequestException as e:
                if attempt < max_retry - 1:
                    wait = (attempt + 1) * 2.0
                    logger.warning(f"[GET] 요청 실패(재시도 {attempt+1}): {e} → {wait}s 대기")
                    time.sleep(wait)
                else:
                    logger.error(f"[GET] 요청 최종 실패: {e}")
                    return {"rt_cd": "9", "msg1": str(e)}
        return {"rt_cd": "9", "msg1": "max_retry 초과"}

    def _post(self, url: str, tr_id: str, body: dict,
              use_hash: bool = True, max_retry: int = 3) -> dict:
        """POST 요청 with retry (주문용)."""
        for attempt in range(max_retry):
            try:
                self._rate_limit()
                resp = requests.post(
                    url,
                    headers=self.headers(tr_id, use_hash=use_hash, body=body),
                    json=body,
                    timeout=10,
                )

                if resp.status_code == 500:
                    self._on_error(500, self._extract_msg_cd(resp))
                    if attempt < max_retry - 1:
                        time.sleep(_BACKOFF_STEP_SEC[min(attempt, 3)])
                        continue
                    return {"rt_cd": "9", "msg1": "HTTP 500", "_http_status": 500}

                resp.raise_for_status()
                data = resp.json()

                if data.get("rt_cd") != "0":
                    mc = data.get("msg_cd", "")
                    if mc in ("EGW00201", "EGW00121") and attempt < max_retry - 1:
                        self._on_error(200, mc)
                        continue
                    # APBK1672: 해외ETP 미신청 계좌 — 블랙리스트 처리 필요 (재시도 불필요)
                    # APBK0656: KIS 종목정보 없음 — 재시도 무의미
                    if mc in ("APBK1672", "APBK0656"):
                        logger.error(
                            f"[POST] 주문영구차단 rt_cd={data.get('rt_cd')} "
                            f"msg_cd={mc} msg1={data.get('msg1','?')!r} "
                            f"→ 블랙리스트 추가 필요"
                        )
                        return data   # 즉시 반환 (재시도 불필요)
                    logger.warning(
                        f"[POST] rt_cd={data.get('rt_cd')} "
                        f"msg_cd={mc} msg1={data.get('msg1','?')!r}"
                    )
                    return data

                self._on_success()
                return data

            except requests.RequestException as e:
                if attempt < max_retry - 1:
                    wait = (attempt + 1) * 2.0
                    logger.warning(f"[POST] 요청 실패(재시도 {attempt+1}): {e} → {wait}s 대기")
                    time.sleep(wait)
                else:
                    logger.error(f"[POST] 요청 최종 실패: {e}")
                    return {"rt_cd": "9", "msg1": str(e)}
        return {"rt_cd": "9", "msg1": "max_retry 초과"}

    @staticmethod
    def _extract_msg_cd(resp: requests.Response) -> str:
        try:
            return resp.json().get("msg_cd", "")
        except Exception:
            return ""
