"""
종목 기본 제외 필터
===================
아래 조건에 해당하는 종목은 분석 대상에서 제외한다.

개별주식 제외 조건:
  1. 관리종목 / 거래정지 / 투자주의·경고·위험
  2. 감사의견 비적정
  3. 자본잠식 / 계속기업 불확실성
  4. 시가총액 300억 미만
  5. 일평균 거래대금 30억 미만
  6. 최근 3년 연속 적자 (영업이익)
  7. 우선주 (종목코드 끝자리 5 or 이름에 '우')
  8. SPAC
  9. 상장폐지 위험

ETF 전용 필터:
  - ETF는 StockFilter 를 통과시키고 ETFFilter 에서 별도 판별
  - 일반/레버리지/인버스 ETF 모두 투자 대상 포함
  - 거래정지·관리 ETF만 제외
"""

import re
from utils.logger import get_logger

logger = get_logger("StockFilter")

# ── ETF / ETN / SPAC 키워드 ────────────────────────────────
ETF_KEYWORDS  = ["ETF", "ETN", "인버스", "레버리지", "선물", "KODEX", "TIGER",
                 "ARIRANG", "KINDEX", "KOSEF", "HANARO", "PLUS", "ACE "]
SPAC_KEYWORDS = ["스팩", "SPAC", "기업인수"]
ADMIN_KEYWORDS= ["관리종목", "거래정지", "투자주의", "투자경고", "투자위험",
                 "상장폐지", "감사의견"]

# ── 최소 기준 ───────────────────────────────────────────────
MIN_MARKET_CAP    = 30_000_000_000    # 300억
MIN_DAILY_AMOUNT  = 3_000_000_000     # 30억


# ── ETF 전용 필터 ──────────────────────────────────────────
class ETFFilter:
    """
    ETF 전용 1차 필터.
    - 거래정지 / 관리 ETF 만 제외
    - 일반/레버리지/인버스 ETF 모두 허용
    - 최소 거래대금 50억 (ETF는 기준 완화)
    """
    MIN_ETF_AMOUNT = 5_000_000_000   # 50억

    def filter(self, info: dict) -> tuple[bool, str]:
        if info.get("is_halt"):  return False, "ETF거래정지"
        if info.get("is_admin"): return False, "ETF관리"
        amt = info.get("daily_amount", 0)
        if amt and amt < self.MIN_ETF_AMOUNT:
            return False, f"ETF거래대금미달({amt/1e8:.0f}억<50억)"
        return True, ""

    def batch_filter(self, etf_list: list[dict]) -> tuple[list[dict], list[dict]]:
        passed, excluded = [], []
        for info in etf_list:
            ok, reason = self.filter(info)
            if ok:
                passed.append(info)
            else:
                excluded.append({"code": info.get("code"),
                                  "name": info.get("name"),
                                  "reason": reason})
        return passed, excluded


class StockFilter:
    """
    KIS API 에서 받아온 종목 기본 정보로 1차 제외 필터링을 수행한다.
    kis_api.get_stock_info() 반환 구조 기준:
      {
        code, name, market, sector,
        market_cap, daily_amount, is_admin,
        is_halt, warn_level, audit_opinion,
        capital_erosion, going_concern,
        op_profit_3y: [year3, year2, year1],   # 연간 영업이익
      }
    """

    def filter(self, info: dict) -> tuple[bool, str]:
        """
        returns (is_ok, reason)
          is_ok  = True  → 통과 (분석 대상)
          is_ok  = False → 제외 (reason 에 사유)
        """
        code = info.get("code", "")
        name = info.get("name", "")

        # 1. 우선주 (코드 뒤 5자리 끝이 5 or 이름에 우/B)
        if self._is_preferred(code, name):
            return False, "우선주"

        # 2. ETF → ETFFilter 에서 별도 처리 (StockFilter 에서는 통과)
        #    ETF 로 판별되면 개별주식 필터 나머지 항목 건너뜀
        if self._is_etf_by_name(name) or info.get("asset_type", "").startswith("ETF"):
            # ETF 는 거래정지/관리만 검사
            if info.get("is_halt"):  return False, "거래정지"
            if info.get("is_admin"): return False, "관리종목"
            return True, "ETF"

        # SPAC 제외
        for kw in SPAC_KEYWORDS:
            if kw in name:
                return False, f"SPAC({kw})"

        # 3. 관리종목 / 거래정지
        if info.get("is_admin"):
            return False, "관리종목"
        if info.get("is_halt"):
            return False, "거래정지"

        # 4. 투자 경보 단계 (0=없음, 1=주의, 2=경고, 3=위험)
        warn = info.get("warn_level", 0)
        if warn >= 1:
            labels = {1: "투자주의", 2: "투자경고", 3: "투자위험"}
            return False, labels.get(warn, f"투자경보{warn}")

        # 5. 감사의견 비적정
        audit = info.get("audit_opinion", "")
        if audit and audit not in ("적정", "", "N/A", "해당없음"):
            return False, f"감사의견비적정({audit})"

        # 6. 자본잠식
        if info.get("capital_erosion"):
            return False, "자본잠식"

        # 7. 계속기업 불확실성
        if info.get("going_concern"):
            return False, "계속기업불확실성"

        # 8. 시가총액 300억 미만
        cap = info.get("market_cap", 0)
        if cap and cap < MIN_MARKET_CAP:
            return False, f"시총미달({cap/1e8:.0f}억)"

        # 9. 일평균 거래대금 30억 미만
        amt = info.get("daily_amount", 0)
        if amt and amt < MIN_DAILY_AMOUNT:
            return False, f"거래대금미달({amt/1e8:.0f}억)"

        # 10. 3년 연속 적자
        op_profits = info.get("op_profit_3y", [])
        if len(op_profits) >= 3 and all(p < 0 for p in op_profits[:3]):
            return False, "3년연속적자"

        return True, ""

    # ── 내부 헬퍼 ─────────────────────────────────────────
    def _is_preferred(self, code: str, name: str) -> bool:
        """우선주 판별: 코드 6자리 중 마지막이 5, 또는 이름에 '우'/'B' 포함"""
        if len(code) == 6 and code[5] == "5":
            return True
        if re.search(r"우$|우[BCDE]$|\bB$", name.strip()):
            return True
        return False

    def _is_etf_by_name(self, name: str) -> bool:
        """이름으로 ETF 여부만 판별 (SPAC 제외)"""
        upper = name.upper()
        for kw in ETF_KEYWORDS:
            if kw.upper() in upper:
                return True
        return False

    def _is_etf_etf(self, name: str) -> str:
        """ETF/ETN/SPAC 판별 (하위 호환 유지)"""
        upper = name.upper()
        for kw in ETF_KEYWORDS:
            if kw.upper() in upper:
                return f"ETF/ETN({kw})"
        for kw in SPAC_KEYWORDS:
            if kw in name:
                return f"SPAC({kw})"
        return ""

    def batch_filter(self, stock_list: list[dict]) -> tuple[list[dict], list[dict]]:
        """
        여러 종목을 한번에 필터링
        returns: (통과목록, 제외목록[{code,name,reason}])
        """
        passed, excluded = [], []
        for info in stock_list:
            ok, reason = self.filter(info)
            if ok:
                passed.append(info)
            else:
                excluded.append({
                    "code":   info.get("code"),
                    "name":   info.get("name"),
                    "reason": reason,
                })
                logger.debug(f"  ✗ {info.get('name','?')}({info.get('code','?')}) — {reason}")
        logger.info(f"필터링 결과: 통과 {len(passed)}개 / 제외 {len(excluded)}개 (전체 {len(stock_list)}개)")
        return passed, excluded
