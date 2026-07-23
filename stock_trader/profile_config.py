"""
profile_config.py — KIS 계좌 프로필 SSOT (legacy / kakao)

목적:
  - 기존 계좌(legacy, 과거자료 조회 전용)와 신규 계좌(kakao, 향후 운용)를 논리적으로 분리.
  - 키/시크릿/계좌번호를 코드에 하드코딩하지 않고 환경변수 네임스페이스로만 읽는다.
  - 프로필별 토큰 캐시 키·ledger 경로·mode(READ_ONLY/LIVE)를 한 곳에서 관리.

안전 원칙:
  - legacy 는 환경변수와 무관하게 항상 READ_ONLY.
  - 활성 프로필은 프로세스 시작 시 1회 확정(runtime 전환 불가).
  - 로그에는 app_key/secret/전체 계좌번호를 절대 출력하지 않는다(마스킹만).
  - 실제 키 값은 이 파일에 없다. .env(로컬)에서만 읽는다.
"""
import os
import hashlib
from dataclasses import dataclass

from utils.logger import get_logger

logger = get_logger("Profile")

_VALID_PROFILES = ("legacy", "kakao")
_active_cache = None   # 프로세스 1회 확정(runtime 고정)


def _mask_acct(acct: str) -> str:
    s = str(acct or "")
    if len(s) <= 4:
        return "****"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _fingerprint(app_key: str) -> str:
    """app_key 원문 대신 지문(해시 앞 8자)만 — 원문 로그/캐시 저장 금지."""
    if not app_key:
        return "none"
    return hashlib.sha256(app_key.encode()).hexdigest()[:8]


class ProfileError(RuntimeError):
    pass


@dataclass
class KisProfile:
    profile_name: str
    app_key: str
    app_secret: str
    account_no: str
    product_code: str
    mode: str            # 'READ_ONLY' | 'LIVE'

    @property
    def is_read_only(self) -> bool:
        # legacy 는 무조건 read-only. mode 가 LIVE 가 아니면 read-only.
        return self.profile_name == "legacy" or self.mode != "LIVE"

    def token_cache_key(self) -> str:
        # 원문 키 없이: 프로필명 + app_key 지문 + 계좌 마스킹
        return f"{self.profile_name}:{_fingerprint(self.app_key)}:{_mask_acct(self.account_no)}"

    def ledger_path(self) -> str:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        return os.path.join(base, f"ledger_{self.profile_name}.db")

    def safe_summary(self) -> dict:
        """로그/상태 노출용 — 민감정보 없음."""
        return {
            "active_profile": self.profile_name,
            "mode": "READ_ONLY" if self.is_read_only else "LIVE",
            "account": _mask_acct(self.account_no),
            "key_fp": _fingerprint(self.app_key),
            "ledger_path": os.path.basename(self.ledger_path()),
        }


def _require(name: str, val: str, profile: str):
    if not val:
        raise ProfileError(f"프로필 '{profile}' 필수 환경변수 누락: {name}")
    return val


def _load(name: str) -> KisProfile:
    prefix = {"legacy": "KIS_LEGACY_", "kakao": "KIS_KAKAO_"}[name]
    app_key = _require(prefix + "APP_KEY",    os.getenv(prefix + "APP_KEY", ""), name)
    secret  = _require(prefix + "APP_SECRET", os.getenv(prefix + "APP_SECRET", ""), name)
    acct    = _require(prefix + "ACCOUNT",    os.getenv(prefix + "ACCOUNT", ""), name)
    prod    = os.getenv(prefix + "PRODUCT_CODE", "01")
    if name == "legacy":
        mode = "READ_ONLY"                                   # 강제
    else:
        mode = os.getenv(prefix + "MODE", "READ_ONLY").upper()
        if mode not in ("READ_ONLY", "LIVE"):
            raise ProfileError(f"프로필 'kakao' MODE 값 오류: {mode} (READ_ONLY|LIVE)")
    return KisProfile(name, app_key, secret, acct, prod, mode)


def _load_compat() -> KisProfile:
    """
    하위호환: 단일 KIS_APP_KEY 등만 있는 기존 사용자.
    - deprecated 경고, 기본 READ_ONLY, legacy/kakao 로 자동추정 금지(중립 'default').
    """
    app_key = os.getenv("KIS_APP_KEY", "")
    if not app_key:
        raise ProfileError(
            "활성 프로필 미설정: KIS_ACTIVE_PROFILE=legacy|kakao 를 설정하거나 "
            "(하위호환) KIS_APP_KEY 등을 설정하세요.")
    logger.warning("[Profile] ⚠️ deprecated: 단일 KIS_APP_KEY 사용 → 'default'(READ_ONLY)로 로드. "
                   "KIS_ACTIVE_PROFILE 사용 권장(legacy/kakao 자동추정 안 함).")
    return KisProfile(
        profile_name="default",
        app_key=app_key,
        app_secret=os.getenv("KIS_APP_SECRET", ""),
        account_no=os.getenv("KIS_ACCOUNT_NO", os.getenv("KIS_ACCOUNT", "")),
        product_code=os.getenv("KIS_PRODUCT_CODE", "01"),
        mode="READ_ONLY",   # 하위호환은 항상 read-only
    )


def resolve_profile(force_reload: bool = False) -> KisProfile:
    """활성 프로필 확정(1회 캐시, runtime 전환 불가). force_reload 는 테스트 전용."""
    global _active_cache
    if _active_cache is not None and not force_reload:
        return _active_cache
    name = os.getenv("KIS_ACTIVE_PROFILE", "").strip().lower()
    if name in _VALID_PROFILES:
        prof = _load(name)
    elif name == "":
        prof = _load_compat()
    else:
        raise ProfileError(f"알 수 없는 프로필: '{name}' (허용: {_VALID_PROFILES} 또는 미설정)")
    _active_cache = prof
    logger.info(f"[Profile] active={prof.safe_summary()}")
    return prof


def reset_for_test():
    """테스트 전용: 캐시 초기화."""
    global _active_cache
    _active_cache = None
