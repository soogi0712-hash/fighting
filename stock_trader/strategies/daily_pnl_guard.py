"""
일일 손익 관리 (DailyPnLGuard)
================================

★ 목표수익 달성 시 자동 정지 시스템
  - 국내장: +300,000원 도달 시 → KR_PROFIT_LOCK (신규 매수 금지)
  - 미국장: +300,000원 도달 시 → US_PROFIT_LOCK (신규 매수 금지)
  ★ 두 시장은 완전히 독립적으로 운영

★ 중요: 미국장 세션 기준 리셋
  - 미국장 손익은 미국장 개장 세션(ET 09:30) 기준으로 초기화
  - 한국 날짜가 바뀌어도 미국장 세션이 바뀌기 전까지 손익 누적 유지
  - 예: 23:50 +15만원 → 01:00 +25만원 → 02:00 +31만원 → US_PROFIT_LOCK
        (날짜가 바뀌어도 한 세션으로 계속 누적)

★ 국내장 세션 기준 리셋
  - 국내장 개장(09:00 KST) 기준으로 다음 거래일 시작 시 초기화

★ 상태 전이
  TRADING     : 정상 거래 중
  PROFIT_LOCK : 목표 수익 달성 → 신규 진입 차단, 기존 포지션만 청산
  LOSS_LIMIT  : 일일 손실 한도 도달 → 신규 매수 차단
  HALTED      : 장 종료 또는 외부 halt
"""

import pytz
from datetime import datetime
from utils.logger import get_logger

logger = get_logger("DailyPnLGuard")

KST         = pytz.timezone("Asia/Seoul")
US_EASTERN  = pytz.timezone("America/New_York")


def _get_us_session_date() -> str:
    """
    미국장 세션 기준 날짜 키 반환.
    ★ 한국 날짜 기준이 아닌 미국장 개장일(ET 날짜) 기준.
    예) KST 2024-01-16 01:30 = ET 2024-01-15 11:30 → "2024-01-15"
    """
    now_et = datetime.now(US_EASTERN)
    # ET 기준 정규장 세션 날짜 반환
    return now_et.strftime("%Y-%m-%d")


def _get_kr_session_date() -> str:
    """
    국내장 세션 기준 날짜 키 반환 (KST 날짜 기준).
    다음날 09:00 이전까지는 동일 거래일로 간주.
    """
    now_kst = datetime.now(KST)
    return now_kst.strftime("%Y-%m-%d")


class DailyPnLGuard:
    """
    국내장 / 미국장 공용 일일 손익 관리자.

    파라미터
    --------
    target_krw      : 목표 수익 (KRW 기준, 예: 300_000)
                      ★ 목표 도달 시 즉시 PROFIT_LOCK — 신규 매수 완전 차단
    profit_lock_krw : 사용하지 않음 (target_krw와 동일하게 처리)
    loss_limit_krw  : 일일 손실 한도 (음수, 예: -300_000)
    name            : "국내장" or "미국장" (로그 구분용)
    use_us_session  : True이면 미국장 세션(ET 날짜) 기준으로 리셋 (날짜 변경 무관)
                      False이면 KST 날짜 기준 리셋 (국내장)
    """

    # ── 상태 상수 ──────────────────────────────────────────
    STATE_TRADING     = "TRADING"       # 정상 거래
    STATE_PROFIT_LOCK = "PROFIT_LOCK"   # 목표 수익 달성 → 신규 진입 차단
    STATE_LOSS_LIMIT  = "LOSS_LIMIT"    # 손실 한도 도달 → 신규 매수 차단
    STATE_HALTED      = "HALTED"        # 장 종료/외부 중단

    def __init__(
        self,
        target_krw: float      = 300_000,
        profit_lock_krw: float = None,    # None이면 target_krw 사용
        loss_limit_krw: float  = -300_000,
        name: str              = "장",
        use_us_session: bool   = False,   # True=미국장 세션 기준, False=KST 날짜 기준
    ):
        self.target_krw      = target_krw
        # profit_lock_krw = target_krw 와 동일하게 처리 (목표 달성 즉시 차단)
        self.profit_lock_krw = profit_lock_krw if profit_lock_krw is not None else target_krw
        self.loss_limit_krw  = loss_limit_krw
        self.name            = name
        self.use_us_session  = use_us_session

        self._session_key    = ""   # 세션 기준 날짜 키 (리셋 기준)
        self.realized_pnl    = 0.0  # 당일 누적 실현 손익 (KRW)
        self.peak_pnl        = 0.0  # 당일 최고 실현 손익 (KRW)
        self.state           = self.STATE_TRADING

        # ── 상태 변경 이력 (로그·디버깅용) ─────────────────
        self.state_history: list[dict] = []

    def _get_current_session_key(self) -> str:
        """현재 세션 기준 날짜 키 반환"""
        if self.use_us_session:
            return _get_us_session_date()
        return _get_kr_session_date()

    # ── 날짜 리셋 ──────────────────────────────────────────
    def _check_date_reset(self):
        current_key = self._get_current_session_key()
        if self._session_key != current_key:
            prev = self._session_key
            self._session_key = current_key
            self.realized_pnl = 0.0
            self.peak_pnl     = 0.0
            self.state        = self.STATE_TRADING
            self.state_history.clear()
            if prev:  # 첫 초기화 제외
                session_label = "미국장 세션" if self.use_us_session else "날짜"
                logger.info(
                    f"[{self.name}] 📅 {session_label} 변경 ({prev}→{current_key}) "
                    f"— 일일 손익 리셋"
                )

    # ── 실현 손익 기록 ─────────────────────────────────────
    def record(self, pnl_krw: float):
        """
        매도 체결 후 실현 손익(KRW)을 기록하고 상태를 재평가.

        ★ 반드시 실현손익(매도 체결분)만 전달할 것.
          평가손익(미체결 보유분 평가)은 절대 사용 금지.

        Args:
            pnl_krw: 이번 매도의 실질 순손익 (KRW, 수수료·세금 차감 후)
                     미국장은 USD PnL × 환율로 KRW 환산 후 전달
        """
        self._check_date_reset()

        prev_pnl      = self.realized_pnl
        self.realized_pnl += pnl_krw

        # 최고 실현 손익 갱신
        if self.realized_pnl > self.peak_pnl:
            self.peak_pnl = self.realized_pnl

        session_label = "미국장세션" if self.use_us_session else "날짜"
        logger.info(
            f"[{self.name}] 📊 [실현손익 기록] "
            f"{prev_pnl:+,.0f}원 → {self.realized_pnl:+,.0f}원 "
            f"(이번 매도: {pnl_krw:+,.0f}원) | "
            f"최고실현: {self.peak_pnl:+,.0f}원 | "
            f"목표: {self.target_krw:,}원 | "
            f"상태: {self.state} | "
            f"기준: {session_label}({self._session_key}) | "
            f"※ 평가손익 미포함(실현만)"
        )

        # ── [KR/US PROFIT TARGET] 로그 출력 ──────────────
        market_tag = "US PROFIT TARGET" if self.use_us_session else "KR PROFIT TARGET"
        _state_disp = {
            self.STATE_TRADING:     "거래중",
            self.STATE_PROFIT_LOCK: "목표달성-매수차단",
            self.STATE_LOSS_LIMIT:  "손실한도-매수차단",
            self.STATE_HALTED:      "중단",
        }.get(self.state, self.state)
        logger.info(
            f"[{market_tag}]\n"
            f"  실현손익= {self.realized_pnl:+,.0f}원\n"
            f"  목표=    +{self.target_krw:,}원\n"
            f"  상태=    {_state_disp}"
        )

        self._evaluate()

    # ── 상태 평가 ──────────────────────────────────────────
    def _evaluate(self):
        prev_state = self.state

        # 1) 손실 한도 체크 (최우선)
        if self.realized_pnl <= self.loss_limit_krw:
            new_state = self.STATE_LOSS_LIMIT

        # 2) 목표 수익 달성 → PROFIT_LOCK (즉시 신규 매수 차단)
        #    ★ peak_pnl이 target_krw 이상이면 무조건 PROFIT_LOCK
        #    (수익이 줄어도 이미 목표 달성했으면 차단 유지)
        elif self.peak_pnl >= self.target_krw:
            new_state = self.STATE_PROFIT_LOCK

        # 3) 정상 거래
        else:
            if self.state in (self.STATE_PROFIT_LOCK, self.STATE_LOSS_LIMIT):
                new_state = self.STATE_TRADING
            else:
                new_state = self.state

        if new_state != prev_state:
            self._transition(prev_state, new_state)

    def _transition(self, old: str, new: str):
        self.state = new
        entry = {
            "ts":   datetime.now().isoformat(),
            "from": old,
            "to":   new,
            "pnl":  round(self.realized_pnl, 0),
            "peak": round(self.peak_pnl, 0),
        }
        self.state_history.append(entry)

        if new == self.STATE_LOSS_LIMIT:
            logger.warning(
                f"[{self.name}] 🚫 LOSS_LIMIT 발동!\n"
                f"  ┌─ LOSS_LIMIT 판정 근거 ──────────────────\n"
                f"  │  판정 기준  : 실현손익 (★평가손익 제외)\n"
                f"  │  실현손익   : {self.realized_pnl:+,.0f}원\n"
                f"  │  손실 한도  : {self.loss_limit_krw:,}원\n"
                f"  │  판정 결과  : {self.realized_pnl:+,.0f} ≤ {self.loss_limit_krw:,} → LOSS_LIMIT\n"
                f"  └─ 신규 매수 즉시 차단 (기존 포지션 청산만 허용)"
            )
        elif new == self.STATE_PROFIT_LOCK:
            logger.warning(
                f"[{self.name}] 🎯 목표 수익 달성! PROFIT_LOCK 발동!\n"
                f"  ┌─ PROFIT_LOCK 판정 근거 ─────────────────\n"
                f"  │  최고실현손익: {self.peak_pnl:+,.0f}원\n"
                f"  │  목표수익    : +{self.target_krw:,}원\n"
                f"  │  현재실현    : {self.realized_pnl:+,.0f}원\n"
                f"  │  판정 결과  : 목표 달성 → 신규 매수 차단\n"
                f"  └─ 기존 보유종목 익절/손절만 허용"
            )
        elif new == self.STATE_TRADING:
            logger.info(
                f"[{self.name}] ✅ 거래 재개 "
                f"(실현손익={self.realized_pnl:+,.0f}원, "
                f"이전상태={old})"
            )

    # ── 외부 인터페이스 ────────────────────────────────────
    @property
    def can_buy(self) -> bool:
        """신규 매수 가능 여부"""
        self._check_date_reset()
        return self.state == self.STATE_TRADING

    @property
    def can_hold(self) -> bool:
        """기존 포지션 청산 대기 가능 여부 (신규 진입 차단 시에도 포지션 유지)"""
        self._check_date_reset()
        return self.state in (self.STATE_TRADING, self.STATE_PROFIT_LOCK,
                               self.STATE_LOSS_LIMIT)

    @property
    def is_profit_locked(self) -> bool:
        self._check_date_reset()
        return self.state == self.STATE_PROFIT_LOCK

    @property
    def is_loss_limited(self) -> bool:
        self._check_date_reset()
        return self.state == self.STATE_LOSS_LIMIT

    def block_reason(self) -> str:
        """신규 매수 차단 이유 문자열 (can_buy=False 시 호출)"""
        self._check_date_reset()
        if self.state == self.STATE_LOSS_LIMIT:
            return (
                f"[{self.name}] 일일 손실 한도 도달 "
                f"({self.realized_pnl:+,.0f}원 ≤ {self.loss_limit_krw:,}원)"
            )
        if self.state == self.STATE_PROFIT_LOCK:
            return (
                f"[{self.name}] 목표수익 달성 — 신규 매수 차단 "
                f"(목표+{self.target_krw:,}원 달성, "
                f"최고실현={self.peak_pnl:+,.0f}원)"
            )
        return ""

    def inject(self, realized_pnl: float, peak_pnl: float = None):
        """
        서버 재시작 후 이전 손익을 수동 주입.

        ★ 반드시 실현손익(당일 체결된 매도의 순손익 합계)만 전달할 것.
          평가손익(현재 보유 포지션의 미실현 손익)은 절대 전달 금지.

        Args:
            realized_pnl: 주입할 당일 실현 손익 (KRW) — 매도 체결분만
            peak_pnl:     주입할 당일 최고 실현 손익 (None이면 realized_pnl로 추정)
        """
        self._check_date_reset()
        old_pnl   = self.realized_pnl
        old_state = self.state

        self.realized_pnl = float(realized_pnl)
        self.peak_pnl     = float(peak_pnl) if peak_pnl is not None \
                            else max(self.peak_pnl, self.realized_pnl)

        logger.warning(
            f"[{self.name}] 💉 손익 수동 주입\n"
            f"  ┌─ 주입 내용 ─────────────────────────────\n"
            f"  │  주입 종류  : 실현손익 (★평가손익 아님)\n"
            f"  │  이전값     : {old_pnl:+,.0f}원\n"
            f"  │  주입값     : {self.realized_pnl:+,.0f}원\n"
            f"  │  최고실현   : {self.peak_pnl:+,.0f}원\n"
            f"  └──────────────────────────────────────────"
        )
        self._evaluate()

        if self.state != old_state:
            logger.warning(
                f"[{self.name}] ⚡ 주입 후 상태 변경: {old_state} → {self.state}"
            )
        self.log_status()

    def log_status(self, unrealized_pnl: float = None):
        """
        현재 PnL 상태를 INFO 레벨로 즉시 출력 (진단/확인용).
        """
        self._check_date_reset()
        state_icon = {
            self.STATE_TRADING:     "🟢",
            self.STATE_PROFIT_LOCK: "🎯",
            self.STATE_LOSS_LIMIT:  "🚫",
            self.STATE_HALTED:      "⛔",
        }.get(self.state, "❓")

        can_buy_str = "✅ 매수 가능" if self.state == self.STATE_TRADING \
                      else "🚫 매수 차단"

        # 상태 판정 요약
        if self.state == self.STATE_LOSS_LIMIT:
            verdict = (
                f"실현손익({self.realized_pnl:+,.0f}원) "
                f"≤ 손실한도({self.loss_limit_krw:,}원) → LOSS_LIMIT"
            )
        elif self.state == self.STATE_PROFIT_LOCK:
            verdict = (
                f"최고실현({self.peak_pnl:+,.0f}원) ≥ 목표({self.target_krw:,}원) → PROFIT_LOCK"
            )
        else:
            verdict = (
                f"실현손익({self.realized_pnl:+,.0f}원) < 목표({self.target_krw:,}원) → TRADING"
            )

        unreal_line = ""
        if unrealized_pnl is not None:
            unreal_line = f"  평가손익   : {unrealized_pnl:+,.0f}원  ※ 판단에 미포함\n"

        session_label = "미국장세션(ET날짜)" if self.use_us_session else "KST날짜"

        # [KR/US PROFIT TARGET] 형식 로그
        market_tag = "US PROFIT TARGET" if self.use_us_session else "KR PROFIT TARGET"
        _state_disp = {
            self.STATE_TRADING:     "거래중",
            self.STATE_PROFIT_LOCK: "목표달성-매수차단",
            self.STATE_LOSS_LIMIT:  "손실한도-매수차단",
            self.STATE_HALTED:      "중단",
        }.get(self.state, self.state)
        logger.info(
            f"[{market_tag}]\n"
            f"  실현손익= {self.realized_pnl:+,.0f}원\n"
            f"  목표=    +{self.target_krw:,}원\n"
            f"  상태=    {_state_disp}"
        )

        logger.info(
            f"[{self.name}] {state_icon} PnL 상태 진단\n"
            f"  상태       : {self.state}  ({can_buy_str})\n"
            f"  실현손익   : {self.realized_pnl:+,.0f}원  ★판단 기준\n"
            + unreal_line +
            f"  최고실현   : {self.peak_pnl:+,.0f}원\n"
            f"  손실한도   : {self.loss_limit_krw:,}원  "
            f"({self.realized_pnl / abs(self.loss_limit_krw) * 100:.1f}% 도달)\n"
            f"  목표수익   : {self.target_krw:,}원  "
            f"(달성률 {self.realized_pnl / self.target_krw * 100:.1f}%)\n"
            f"  세션기준   : {session_label}({self._session_key})\n"
            f"  판정 근거  : {verdict}"
            + (f"\n  차단사유   : {self.block_reason()}" if not self.can_buy else "")
        )

    def status_dict(self) -> dict:
        """대시보드·로그용 상태 딕셔너리"""
        self._check_date_reset()
        return {
            "name":            self.name,
            "state":           self.state,
            "can_buy":         self.can_buy,
            "realized_pnl":    round(self.realized_pnl, 0),
            "peak_pnl":        round(self.peak_pnl, 0),
            "target_krw":      self.target_krw,
            "profit_lock_krw": self.profit_lock_krw,
            "loss_limit_krw":  self.loss_limit_krw,
            "session_key":     self._session_key,
            "use_us_session":  self.use_us_session,
            # 진행률 (손실 방향)
            "loss_pct":   round(
                self.realized_pnl / abs(self.loss_limit_krw) * 100
                if self.loss_limit_krw != 0 else 0, 1
            ),
            # 목표 달성률
            "target_pct": round(
                self.realized_pnl / self.target_krw * 100
                if self.target_krw != 0 else 0, 1
            ),
        }
