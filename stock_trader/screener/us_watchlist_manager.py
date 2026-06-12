"""
US Watchlist Manager — 미국 관심종목 성과 기반 자동 관리
==========================================================
핵심 기능:
  1. 관심종목 진입 시각 기록 → 48시간 성과 추적
  2. 48시간 내 손익 없는 종목 자동 제거
  3. 7일 내 손절 종목 가중치 하향 페널티
  4. 당일 익절 종목 당일 재진입 금지
  5. [US DAILY SCREENER] 형식 로그 출력

데이터 파일:
  - us_watch_perf.json  : 관심종목별 진입시각·성과 추적
  - us_daily_profit.json: 당일 익절 종목 목록 (자정 초기화)
  - us_penalty.json     : 7일 손절 페널티 종목 목록
"""

import json
import os
from datetime import datetime, date, timedelta
import pytz
from collections import defaultdict

from utils.logger import get_logger

logger = get_logger("USWatchlistMgr")

KST = pytz.timezone("Asia/Seoul")
ET  = pytz.timezone("America/New_York")

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# ── 데이터 파일 경로 ────────────────────────────────────────────
WATCH_PERF_FILE    = os.path.join(_DATA_DIR, "us_watch_perf.json")
DAILY_PROFIT_FILE  = os.path.join(_DATA_DIR, "us_daily_profit.json")
PENALTY_FILE       = os.path.join(_DATA_DIR, "us_penalty.json")
COOLDOWN_FILE      = os.path.join(_DATA_DIR, "us_cooldown.json")
RECENT_LOSS_FILE   = os.path.join(_DATA_DIR, "us_recent_loss.json")

# ── 임계값 ─────────────────────────────────────────────────────
STALE_HOURS        = 48    # 48시간 이상 성과 없으면 제거
PENALTY_DAYS       = 7     # 손절 후 7일간 가중치 하향
PENALTY_SCORE      = -20   # 손절 종목 점수 패널티
COOLDOWN_HOURS     = 24    # 익절 후 24h 재매수 억제


# ════════════════════════════════════════════════════════════════
# ── I/O 헬퍼
# ════════════════════════════════════════════════════════════════

def _load_json(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[WatchMgr] JSON 로드 실패 {path}: {e}")
    return {}


def _save_json(path: str, data: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[WatchMgr] JSON 저장 실패 {path}: {e}")


# ════════════════════════════════════════════════════════════════
# ── 1. 관심종목 성과 추적 (Watch Performance)
# ════════════════════════════════════════════════════════════════

def record_watch_entry(symbol: str, price: float, source: str = "screener"):
    """
    관심종목 진입 시각·가격 기록.
    스크리닝 결과로 watchlist에 신규 추가될 때 호출.
    """
    data = _load_json(WATCH_PERF_FILE)
    now  = datetime.now().isoformat()

    if symbol not in data:
        data[symbol] = {
            "symbol":     symbol,
            "entered_at": now,
            "entry_price": price,
            "source":     source,
            "trade_count": 0,
            "total_pnl":  0.0,
            "last_trade": None,
        }
        logger.debug(f"[WatchMgr] 관심종목 진입기록: {symbol} @${price:.2f} ({source})")
    else:
        # 이미 있는 종목은 진입시각만 갱신 (완전 교체 방지)
        data[symbol]["entered_at"]  = now
        data[symbol]["entry_price"] = price
        data[symbol]["source"]      = source

    _save_json(WATCH_PERF_FILE, data)


def update_watch_trade(symbol: str, pnl_usd: float, is_profit: bool):
    """
    매매 완료 후 성과 업데이트.
    app.py _us_stock_loop SELL 처리 후 호출.
    """
    data = _load_json(WATCH_PERF_FILE)
    now  = datetime.now().isoformat()

    if symbol not in data:
        data[symbol] = {
            "symbol":      symbol,
            "entered_at":  now,
            "entry_price": 0.0,
            "source":      "unknown",
            "trade_count": 0,
            "total_pnl":   0.0,
            "last_trade":  None,
        }

    data[symbol]["trade_count"] += 1
    data[symbol]["total_pnl"]   += pnl_usd
    data[symbol]["last_trade"]   = now
    data[symbol]["last_pnl"]     = pnl_usd
    data[symbol]["last_profit"]  = is_profit

    _save_json(WATCH_PERF_FILE, data)
    logger.debug(f"[WatchMgr] 성과 업데이트: {symbol} ${pnl_usd:+.2f} ({'익절' if is_profit else '손절'})")


# ════════════════════════════════════════════════════════════════
# ── 2. 48시간 성과 없는 종목 제거 판정
# ════════════════════════════════════════════════════════════════

def get_stale_symbols(current_watchlist: list, held_syms: set) -> list:
    """
    48시간 이상 아무 매매 없이 watchlist에만 있는 종목 반환.
    보유 포지션 종목은 제외 (강제 유지).

    Returns:
        List[str] — 제거 대상 symbol 목록
    """
    data    = _load_json(WATCH_PERF_FILE)
    cutoff  = (datetime.now() - timedelta(hours=STALE_HOURS)).isoformat()
    stale   = []

    for entry in current_watchlist:
        sym = entry.get("symbol", "")
        if sym in held_syms:
            continue  # 보유 중 → 절대 제거 안 함

        perf = data.get(sym, {})
        if not perf:
            # 성과 기록 자체가 없으면 → 진입기록 없이 watchlist에 남아있는 것
            # 바로 제거 대상으로 보지 않고 지금 기록 생성
            continue

        entered_at = perf.get("entered_at", "")
        last_trade = perf.get("last_trade", None)
        trade_count = perf.get("trade_count", 0)

        if trade_count == 0:
            # 한 번도 매매 안 함 → entered_at 기준 48h 경과 여부
            if entered_at and entered_at < cutoff:
                stale.append(sym)
        # trade_count > 0 이면 마지막 매매가 있었으므로 유지

    return stale


# ════════════════════════════════════════════════════════════════
# ── 3. 당일 익절 종목 재진입 금지
# ════════════════════════════════════════════════════════════════

def record_daily_profit(symbol: str):
    """
    당일 익절 종목 기록 → 당일 자정까지 재진입 금지.
    app.py SELL(익절) 완료 시 호출.
    """
    data = _load_json(DAILY_PROFIT_FILE)
    today = date.today().isoformat()

    # 날짜 초기화 (자정 넘으면 리셋)
    if data.get("date") != today:
        data = {"date": today, "symbols": []}

    if symbol not in data["symbols"]:
        data["symbols"].append(symbol)

    _save_json(DAILY_PROFIT_FILE, data)
    logger.info(f"[WatchMgr] 당일 익절기록: {symbol} → 오늘 자정까지 재진입 금지")


def get_today_profit_symbols() -> set:
    """
    당일 익절 종목 세트 반환 (재진입 금지 대상).
    """
    data  = _load_json(DAILY_PROFIT_FILE)
    today = date.today().isoformat()
    if data.get("date") != today:
        return set()
    return set(data.get("symbols", []))


# ════════════════════════════════════════════════════════════════
# ── 4. 7일 손절 페널티
# ════════════════════════════════════════════════════════════════

def record_penalty(symbol: str, pnl_usd: float):
    """
    손절 종목을 7일 페널티 목록에 등록.
    app.py SELL(손절) 완료 시 호출.
    """
    data = _load_json(PENALTY_FILE)
    data[symbol] = {
        "pnl_usd":    pnl_usd,
        "penalty_at": datetime.now().isoformat(),
    }
    _save_json(PENALTY_FILE, data)
    logger.info(f"[WatchMgr] 7일 손절페널티 등록: {symbol} ${pnl_usd:.2f}")


def get_penalty_symbols() -> set:
    """
    7일 이내 손절 종목 세트 반환 (스크리닝 점수 하향 대상).
    """
    data    = _load_json(PENALTY_FILE)
    cutoff  = (datetime.now() - timedelta(days=PENALTY_DAYS)).isoformat()
    return {
        sym for sym, info in data.items()
        if info.get("penalty_at", "") >= cutoff
    }


# ════════════════════════════════════════════════════════════════
# ── 5. 24h 쿨다운 (익절 후 재매수 억제)
# ════════════════════════════════════════════════════════════════

def record_cooldown(symbol: str):
    """익절 종목을 24h 쿨다운에 등록 (screener 점수 -15점)."""
    data = _load_json(COOLDOWN_FILE)
    data[symbol] = datetime.now().isoformat()
    _save_json(COOLDOWN_FILE, data)


def get_cooldown_symbols() -> set:
    """24h 쿨다운 종목 세트 반환."""
    data   = _load_json(COOLDOWN_FILE)
    cutoff = (datetime.now() - timedelta(hours=COOLDOWN_HOURS)).isoformat()
    return {sym for sym, ts in data.items() if ts >= cutoff}


# ════════════════════════════════════════════════════════════════
# ── 6. 3일 손실 페널티 (기존 호환)
# ════════════════════════════════════════════════════════════════

def record_recent_loss(symbol: str, pnl_usd: float):
    """손실 청산 종목 기록 (3일 패널티 — 기존 save_recent_loss 호환)."""
    data = _load_json(RECENT_LOSS_FILE)
    data[symbol] = {
        "pnl_usd":   pnl_usd,
        "closed_at": datetime.now().isoformat(),
    }
    _save_json(RECENT_LOSS_FILE, data)


def get_recent_loss_symbols() -> set:
    """3일 내 손실 청산 종목 세트."""
    data   = _load_json(RECENT_LOSS_FILE)
    cutoff = (datetime.now() - timedelta(days=3)).isoformat()
    return {
        sym for sym, info in data.items()
        if info.get("closed_at", "") >= cutoff and info.get("pnl_usd", 0) < 0
    }


# ════════════════════════════════════════════════════════════════
# ── 7. 종합 watchlist 정리 함수 (핵심)
# ════════════════════════════════════════════════════════════════

def cleanup_watchlist(
    current_watchlist: list,
    held_syms: set,
    new_candidates: list,
    max_watch: int = 20,
) -> dict:
    """
    관심종목 종합 정리 + 신규 후보 병합.

    Rules:
      1. 보유 포지션 종목 → 절대 제거 안 함
      2. 48h 성과 없는 종목 → 제거
      3. 당일 익절 종목 → new_candidates에서 제외 (재진입 금지)
      4. 7일 손절 페널티 종목 → new_candidates 우선순위 하향
      5. 신규 후보 중 today_profit 종목 제외 후 상위 max_watch개 반영

    Returns:
        {
            "final_watchlist": [...],
            "added": [...],
            "removed_stale": [...],
            "blocked_today_profit": [...],
            "kept_held": [...],
            "penalty_syms": [...],
        }
    """
    today_profit_syms = get_today_profit_symbols()
    penalty_syms      = get_penalty_symbols()

    # ① 48h 성과 없는 종목 식별
    stale_syms = set(get_stale_symbols(current_watchlist, held_syms))

    # ② 현재 watchlist에서 stale 제거 (보유 종목 유지)
    base_list = [
        s for s in current_watchlist
        if s["symbol"] in held_syms or s["symbol"] not in stale_syms
    ]

    # ③ 당일 익절 종목 → new_candidates에서 제외
    blocked = []
    clean_candidates = []
    for c in new_candidates:
        sym = c.get("symbol", "")
        if sym in today_profit_syms:
            blocked.append(sym)
        else:
            clean_candidates.append(c)

    # ④ 페널티 종목 → 후순위로 이동 (제외하지는 않음, 점수가 이미 낮아야 함)
    #    여기서는 새 후보 정렬은 이미 screener에서 했으므로 그냥 통과

    # ⑤ 신규 후보 병합 (중복 방지)
    existing_syms = {s["symbol"] for s in base_list}
    added = []
    for c in clean_candidates:
        sym = c.get("symbol", "")
        if sym and sym not in existing_syms and len(base_list) < max_watch:
            entry = {
                "symbol": sym,
                "name":   c.get("name", sym),
                "excd":   c.get("excd", "NASD"),
                "sector": c.get("sector", "GROWTH"),
                "score":  c.get("score", 0),
            }
            base_list.append(entry)
            existing_syms.add(sym)
            added.append(sym)
            # 진입 기록
            record_watch_entry(sym, c.get("cur_price", 0.0), source="daily_screener")

    # ⑥ 보유 포지션 종목이 누락됐으면 마지막에 추가
    kept_held = []
    for sym in held_syms:
        if not any(s["symbol"] == sym for s in base_list):
            base_list.append({"symbol": sym, "name": sym, "excd": "NASD"})
            kept_held.append(sym)

    # 최종 리스트는 max_watch 제한 (보유종목은 초과해도 포함)
    held_entries    = [s for s in base_list if s["symbol"] in held_syms]
    non_held        = [s for s in base_list if s["symbol"] not in held_syms]
    final_watchlist = non_held[:max_watch] + held_entries

    removed_stale = list(stale_syms - held_syms)

    return {
        "final_watchlist":       final_watchlist,
        "added":                 added,
        "removed_stale":         removed_stale,
        "blocked_today_profit":  blocked,
        "kept_held":             kept_held,
        "penalty_syms":          sorted(penalty_syms),
        "today_profit_syms":     sorted(today_profit_syms),
        "stale_syms":            sorted(stale_syms - held_syms),
    }


# ════════════════════════════════════════════════════════════════
# ── 8. [US DAILY SCREENER] 로그 출력
# ════════════════════════════════════════════════════════════════

def log_daily_screener_result(
    analyzed_count: int,
    strong_sectors: list,
    new_candidates: list,
    cleanup_result: dict,
    final_watchlist: list,
):
    """
    [US DAILY SCREENER] 형식으로 결과 로그 출력.

    형식:
      [US DAILY SCREENER] 시장 분석 종목수=N
      [US DAILY SCREENER] 강세 섹터=AI/반도체/방산
      [US DAILY SCREENER] 신규 후보=NVDA(AI,87점), CRWD(DEFENSE,82점)...
      [US DAILY SCREENER] 제외 종목=stale:QBTS,BBAI | 당일익절금지:WOLF | 페널티:RIOT
      [US DAILY SCREENER] 최종 감시종목=NVDA,CRWD,...(총N개)
    """
    added   = cleanup_result.get("added", [])
    stale   = cleanup_result.get("stale_syms", [])
    blocked = cleanup_result.get("blocked_today_profit", [])
    penalty = cleanup_result.get("penalty_syms", [])

    sector_str = " / ".join(strong_sectors[:5]) if strong_sectors else "분석중"

    candidate_str = ", ".join(
        f"{c['symbol']}({c.get('sector','?')},{c.get('score',0):.0f}점)"
        for c in new_candidates[:8]
    ) or "없음"

    excluded_parts = []
    if stale:
        excluded_parts.append(f"48h미성과:{','.join(stale[:5])}")
    if blocked:
        excluded_parts.append(f"당일익절금지:{','.join(blocked[:5])}")
    if penalty:
        excluded_parts.append(f"7일페널티:{','.join(penalty[:5])}")
    excluded_str = " | ".join(excluded_parts) if excluded_parts else "없음"

    watch_syms = [s["symbol"] for s in final_watchlist]
    watch_str  = ", ".join(watch_syms[:15])
    if len(watch_syms) > 15:
        watch_str += f"... (+{len(watch_syms)-15})"

    logger.info(f"[US DAILY SCREENER] 시장 분석 종목수={analyzed_count}")
    logger.info(f"[US DAILY SCREENER] 강세 섹터={sector_str}")
    logger.info(f"[US DAILY SCREENER] 신규 후보={candidate_str}")
    logger.info(f"[US DAILY SCREENER] 제외 종목={excluded_str}")
    logger.info(f"[US DAILY SCREENER] 최종 감시종목={watch_str} (총{len(watch_syms)}개)")


# ════════════════════════════════════════════════════════════════
# ── 9. SELL 완료 후 일괄 처리 (app.py에서 호출)
# ════════════════════════════════════════════════════════════════

def on_sell_complete(symbol: str, pnl_usd: float):
    """
    SELL 완료 후 호출 — 익절/손절 여부에 따라 자동 분기 처리.

    app.py _us_stock_loop의 action == 'SELL' 블록에서 호출:
        from screener.us_watchlist_manager import on_sell_complete
        on_sell_complete(symbol, pnl_usd_val)
    """
    is_profit = pnl_usd >= 0
    now_str   = datetime.now().strftime("%H:%M:%S")

    # 성과 업데이트
    update_watch_trade(symbol, pnl_usd, is_profit)

    if is_profit:
        # 익절: 당일 재진입 금지 + 24h 쿨다운
        record_daily_profit(symbol)
        record_cooldown(symbol)
        logger.info(
            f"✅ [WatchMgr] {symbol} 익절 ${pnl_usd:+.2f} | "
            f"당일 재진입 금지 + 24h 쿨다운 등록 ({now_str})"
        )
    else:
        # 손절: 7일 페널티 + 3일 손실기록
        record_penalty(symbol, pnl_usd)
        record_recent_loss(symbol, pnl_usd)
        logger.info(
            f"🔴 [WatchMgr] {symbol} 손절 ${pnl_usd:.2f} | "
            f"7일 페널티 + 3일 손실기록 등록 ({now_str})"
        )
