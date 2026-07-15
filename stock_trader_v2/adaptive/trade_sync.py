"""
adaptive/trade_sync.py — KIS 실체결 동기화 엔진 (V2) ★ 버그픽스 v3
=====================================================
★ 역할:
  1. 매 루프(≈30초) 또는 최소 60초마다 KIS 체결내역 조회
  2. V2 내부 거래기록(DB)과 KIS 실체결 비교
  3. 내부 기록에 없는 체결 → [LOG_MISSING_TRADE] ERROR + DB 복구
  4. 체결 발생 시 → [TRADE_SYNC] INFO 로그 출력
  5. 지연 측정 → [EXECUTION_LATENCY] 로그 출력

★ 버그픽스 v3 변경사항 (2026-06-16):
  - _find_db_trade(): entry_time을 KST 기준으로 비교 (timezone 통일)
    * 서버 entry_time은 UTC+0(또는 로컬)로 저장됨
    * KIS filled_time은 KST 기준 HHMMSS
    * 비교 전 entry_time→KST 변환 후 ±10분 window 적용
  - _process_fills(): order_no 직접 매칭 우선
    * order_no로 DB에 이미 레코드 있으면 → 중복 복구 절대 차단
    * order_no 없는 경우만 시간 window로 보조 매칭
  - _check_duplicate(): KIS 체결 1건 = DB 1건 보장
    * order_no OR (code+date+side) 기준 중복 확인
  - [TRADE_MATCH] 로그: 매칭 방법/timezone 추적

★ [TRADE_SYNC] 로그 형식:
  [TRADE_SYNC] 종목=NVDA | 방향=BUY | 체결시각=14:30:25 |
               체결가=$134.50 | 수량=2 | 실현손익=N/A | 주문번호=123456 | 출처=KIS실체결

★ [LOG_MISSING_TRADE] 로그 형식:
  [LOG_MISSING_TRADE] KIS체결이력 있음/V2기록 없음 |
                      종목=NVDA | 방향=BUY | 체결가=$134.50 | 수량=2 | 복구완료

★ [ORDER_NO_TRACE] / [TRADE_MATCH] 로그:
  [TRADE_MATCH] 종목=LG화학(051910) | order_no_match=True | time_match=False |
                timezone=KST | 결과=ORDER_NO_MATCHED

★ [EXECUTION_LATENCY] 로그 형식:
  [EXECUTION_LATENCY] 종목=NVDA | 신호시각=14:30:20 | 주문요청시각=14:30:21 |
                      체결시각=14:30:25 | 전체지연=5.0s | 주문→체결=4.0s |
                      신호가격=$134.00 | 체결가격=$134.50 | 가격괴리율=+0.37%
"""

import os
import time
import threading
from datetime import datetime, date, timedelta
from typing import Optional

import pytz

from utils.v2_logger import get_logger
from adaptive.trade_recorder import TradeRecorder, _get_conn, _DATA_DIR

logger = get_logger("TradeSync")

KST = pytz.timezone("Asia/Seoul")

_SYNC_INTERVAL_SEC = 60.0   # 최소 동기화 간격 (초)
_sync_lock = threading.Lock()


def _to_kst(dt_iso: str) -> Optional[datetime]:
    """
    ISO datetime 문자열 → KST aware datetime 변환.
    timezone 정보 없으면 로컬 시간으로 간주 후 KST 변환 시도.
    서버가 UTC로 저장했다면 UTC→KST (+9h) 변환.
    서버가 KST로 저장했다면 그대로 반환.
    """
    if not dt_iso:
        return None
    try:
        dt = datetime.fromisoformat(dt_iso)
        if dt.tzinfo is None:
            # timezone 정보 없음 → 서버 로컬 시간
            # KIS entry_time과의 괴리가 9시간이면 UTC, 0시간이면 KST
            # 안전하게: UTC로 가정하고 KST 변환 (실제 서버는 UTC 기준)
            # 단, entry_time이 9시 이전(새벽~오전9시 KST)이면 UTC 가능성 높음
            # → 일관성을 위해 항상 UTC로 간주하여 +9h
            dt_utc = pytz.utc.localize(dt)
            return dt_utc.astimezone(KST)
        else:
            return dt.astimezone(KST)
    except Exception:
        return None


class TradeSyncEngine:
    """
    KIS 실체결 ↔ V2 내부 DB 동기화 엔진.

    사용법:
        sync = TradeSyncEngine(broker_kr, broker_us, recorder)
        # 메인 루프에서 매 루프마다 호출
        sync.run_sync()
    """

    def __init__(self,
                 broker_kr,            # KRBroker (Optional)
                 broker_us,            # USBroker (Optional)
                 recorder: TradeRecorder):
        self.broker_kr  = broker_kr
        self.broker_us  = broker_us
        self.recorder   = recorder
        self._last_sync: float = 0.0
        self._today_str: str   = ""

        # ★ kis_synced=1 완료된 건만 skip set에 로드
        self._synced_order_nos: set = set()
        # order_no → trade_id 빠른 매핑
        self._order_no_map: dict = {}
        self._load_synced_from_db()

    def _load_synced_from_db(self):
        """
        재시작 시 DB에서 order_no 매핑 복원.
        ★ kis_synced=1 인 건만 _synced_order_nos에 추가
        ★ kis_synced=0 이면 order_no가 있어도 재처리 허용
        """
        try:
            conn = _get_conn()
            # 완전 동기화 완료된 건
            rows = conn.execute(
                "SELECT order_no FROM trades "
                "WHERE order_no IS NOT NULL AND order_no != '' AND kis_synced=1"
            ).fetchall()
            for r in rows:
                if r["order_no"]:
                    self._synced_order_nos.add(r["order_no"])

            # order_no → trade_id 전체 매핑 (kis_synced 무관)
            rows2 = conn.execute(
                "SELECT trade_id, code, order_no, kis_synced FROM trades "
                "WHERE order_no IS NOT NULL AND order_no != ''"
            ).fetchall()
            for r in rows2:
                if r["order_no"]:
                    self._order_no_map[r["order_no"]] = r["trade_id"]

            conn.close()
            logger.info(
                f"[TradeSync] 초기화: 동기화완료={len(self._synced_order_nos)}건 | "
                f"order_no매핑={len(self._order_no_map)}건"
            )
        except Exception as e:
            logger.warning(f"[TradeSync] 초기화 복원 실패: {e}")

    # ═══════════════════════════════════════════════════════════
    # 메인 동기화 진입점 — 매 루프에서 호출
    # ═══════════════════════════════════════════════════════════

    def run_sync(self, force: bool = False) -> int:
        """
        KIS 체결내역 조회 + 내부 DB 비교 + 로그 출력.
        Returns: 신규 동기화된 체결 수
        """
        now_ts   = time.time()
        today_s  = date.today().isoformat()

        # 날짜 바뀌면 리셋
        if today_s != self._today_str:
            self._today_str = today_s
            self._synced_order_nos.clear()
            self._order_no_map.clear()
            self._load_synced_from_db()
            logger.info(f"[TradeSync] 날짜 변경 → 동기화 캐시 리셋 ({today_s})")

        # 최소 간격 체크
        if not force and now_ts - self._last_sync < _SYNC_INTERVAL_SEC:
            return 0

        self._last_sync = now_ts
        total_new = 0

        # ── KR 동기화 ─────────────────────────────────────────
        if self.broker_kr:
            try:
                kr_filled = self.broker_kr.get_executed_orders()
                logger.info(
                    f"[TradeSync] KR 체결조회 결과: {len(kr_filled)}건"
                )
                total_new += self._process_fills(kr_filled, "KR")
            except Exception as e:
                logger.warning(f"[TradeSync] KR 체결조회 예외: {e}")

        # ── US 동기화 ─────────────────────────────────────────
        if self.broker_us:
            try:
                us_filled = self.broker_us.get_executed_orders()
                logger.info(
                    f"[TradeSync] US 체결조회 결과: {len(us_filled)}건"
                )
                total_new += self._process_fills(us_filled, "US")
            except Exception as e:
                logger.warning(f"[TradeSync] US 체결조회 예외: {e}")

        if total_new > 0:
            logger.info(f"[TradeSync] ✅ 신규 체결 동기화 {total_new}건 완료")
        else:
            logger.debug(f"[TradeSync] 신규 체결 없음 (기존 동기화 완료 {len(self._synced_order_nos)}건)")

        return total_new

    # ═══════════════════════════════════════════════════════════
    # 체결내역 처리
    # ═══════════════════════════════════════════════════════════

    def _process_fills(self, fills: list, market: str) -> int:
        """
        KIS 체결내역 리스트를 처리 → TRADE_SYNC 로그 + 누락 복구.

        ★ v3 수정:
          1. order_no가 DB에 이미 있으면(kis_synced=0 포함) → 중복 복구 절대 차단
          2. order_no 직접 매칭 우선
          3. order_no 없을 때만 KST 변환 후 시간 window 매칭
          4. [TRADE_MATCH] 로그로 매칭 방법 추적
        """
        new_count = 0
        with _sync_lock:
            for fill in fills:
                order_no = fill.get("order_no", "").strip()
                code     = fill.get("code",     "").strip()
                name     = fill.get("name",     code)
                side     = fill.get("side",     "")   # BUY / SELL
                f_qty    = fill.get("filled_qty",   0)
                f_price  = fill.get("filled_price", 0)
                f_time   = fill.get("filled_time",  "")  # HHMMSS
                f_date   = fill.get("filled_date",  date.today().strftime("%Y%m%d"))

                if not code or f_qty <= 0:
                    continue

                # 체결 시각 KST ISO 변환
                fill_dt_kst_iso = self._to_kst_iso(f_date, f_time)

                # ★ v3: kis_synced=1 완료된 건 스킵
                if order_no and order_no in self._synced_order_nos:
                    continue

                # ★ v3: order_no가 DB에 이미 존재하는지 먼저 확인
                #   (kis_synced=0 이어도 레코드 자체가 있으면 중복 복구 차단)
                db_trade = None
                match_method = "NONE"

                # ── 1순위: order_no 직접 매칭 ─────────────────
                if order_no:
                    if order_no in self._order_no_map:
                        trade_id = self._order_no_map[order_no]
                        db_trade = self._get_trade_by_id(trade_id)
                        if db_trade:
                            match_method = "ORDER_NO_CACHE"
                    if not db_trade:
                        # DB에서 직접 order_no 검색 (캐시 미스 대비)
                        db_trade = self._get_trade_by_order_no(order_no)
                        if db_trade:
                            match_method = "ORDER_NO_DB"
                            self._order_no_map[order_no] = db_trade["trade_id"]

                # ── 2순위: 시간 window 매칭 (order_no 없거나 매칭 실패 시만) ───
                if not db_trade:
                    db_trade = self._find_db_trade_kst(code, side, fill_dt_kst_iso, market)
                    if db_trade:
                        match_method = "TIME_WINDOW_KST"

                # [TRADE_MATCH] 로그
                logger.info(
                    f"[TRADE_MATCH] 종목={name}({code}) | "
                    f"order_no={order_no!r} | "
                    f"order_no_match={'True' if 'ORDER_NO' in match_method else 'False'} | "
                    f"time_match={'True' if 'TIME_WINDOW' in match_method else 'False'} | "
                    f"timezone=KST | "
                    f"결과={match_method if db_trade else 'NO_MATCH'}"
                )

                if db_trade:
                    # 정상: 내부 기록 있음 → order_no 업데이트 + TRADE_SYNC 로그
                    self._update_order_no(
                        db_trade["trade_id"], order_no, fill_dt_kst_iso
                    )
                    # order_no 매핑 갱신
                    if order_no:
                        self._order_no_map[order_no] = db_trade["trade_id"]

                    self._log_trade_sync(
                        code=code, name=name, side=side,
                        fill_time=fill_dt_kst_iso, fill_price=f_price,
                        qty=f_qty, order_no=order_no,
                        pnl_krw=db_trade.get("pnl_krw"),
                        trade_id=db_trade["trade_id"],
                    )
                    self._log_latency(db_trade, fill_dt_kst_iso, f_price)
                else:
                    # ★ v3: order_no로 DB 전수 검색 후 없을 때만 복구
                    #   (TIME_WINDOW 매칭 실패했어도 order_no로 한번 더 확인)
                    if order_no and self._db_has_order_no(order_no):
                        # DB에 이미 있음 → 중복 복구 차단, order_no만 synced 처리
                        logger.info(
                            f"[TradeSync] 중복복구 차단: order_no={order_no} "
                            f"종목={code} DB에 이미 존재 (매칭만 실패)"
                        )
                        self._synced_order_nos.add(order_no)
                        new_count += 1
                        continue

                    # ★ 누락: KIS에 체결 있는데 V2 내부 기록 없음
                    self._handle_missing_trade(
                        fill=fill, market=market,
                        fill_dt_iso=fill_dt_kst_iso,
                        code=code, name=name, side=side,
                        f_qty=f_qty, f_price=f_price, order_no=order_no,
                    )

                # 이번 처리 완료 → synced set에 추가
                if order_no:
                    self._synced_order_nos.add(order_no)
                new_count += 1

        return new_count

    # ═══════════════════════════════════════════════════════════
    # DB 검색
    # ═══════════════════════════════════════════════════════════

    def _get_trade_by_id(self, trade_id: str) -> Optional[dict]:
        """trade_id로 직접 거래 조회."""
        try:
            conn = _get_conn()
            row = conn.execute(
                "SELECT * FROM trades WHERE trade_id=?", (trade_id,)
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.debug(f"[TradeSync] trade_id 조회 실패: {e}")
            return None

    def _get_trade_by_order_no(self, order_no: str) -> Optional[dict]:
        """order_no로 직접 거래 조회."""
        if not order_no:
            return None
        try:
            conn = _get_conn()
            row = conn.execute(
                "SELECT * FROM trades WHERE order_no=? LIMIT 1", (order_no,)
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.debug(f"[TradeSync] order_no 직접조회 실패: {e}")
            return None

    def _db_has_order_no(self, order_no: str) -> bool:
        """order_no가 DB에 존재하는지 확인 (중복 복구 방지)."""
        if not order_no:
            return False
        try:
            conn = _get_conn()
            row = conn.execute(
                "SELECT trade_id FROM trades WHERE order_no=? LIMIT 1", (order_no,)
            ).fetchone()
            conn.close()
            return row is not None
        except Exception:
            return False

    def _find_db_trade_kst(self, code: str, side: str, fill_dt_kst_iso: str,
                            market: str) -> Optional[dict]:
        """
        KIS 체결 → 내부 DB 거래 매칭 (±10분 시간 window).
        ★ v3: KST 기준으로 비교 (entry_time을 UTC→KST 변환 후 비교)

        BUY: status='open', entry_time(KST 변환) ±10분
        SELL: status='closed', exit_time(KST 변환) ±10분
        """
        try:
            conn = _get_conn()

            # KIS fill_dt는 이미 KST
            if fill_dt_kst_iso:
                try:
                    fill_dt_kst = datetime.fromisoformat(fill_dt_kst_iso)
                    if fill_dt_kst.tzinfo:
                        fill_dt_kst = fill_dt_kst.replace(tzinfo=None)
                    window_start = (fill_dt_kst - timedelta(minutes=10))
                    window_end   = (fill_dt_kst + timedelta(minutes=10))
                except Exception:
                    conn.close()
                    return None
            else:
                conn.close()
                return None

            # DB의 entry_time은 UTC로 저장돼 있음 → KST 변환: +9시간
            # SQLite에서 직접 변환: datetime(entry_time, '+9 hours')
            if side == "BUY":
                row = conn.execute("""
                    SELECT *,
                           datetime(entry_time, '+9 hours') as entry_kst
                    FROM trades
                    WHERE code=? AND market=? AND status='open'
                      AND datetime(entry_time, '+9 hours') >= ?
                      AND datetime(entry_time, '+9 hours') <= ?
                      AND (kis_synced IS NULL OR kis_synced=0)
                      AND (order_no IS NULL OR order_no = '')
                    ORDER BY entry_time DESC LIMIT 1
                """, (code, market,
                      window_start.strftime("%Y-%m-%d %H:%M:%S"),
                      window_end.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
            else:  # SELL
                row = conn.execute("""
                    SELECT *,
                           datetime(exit_time, '+9 hours') as exit_kst
                    FROM trades
                    WHERE code=? AND market=? AND status='closed'
                      AND datetime(exit_time, '+9 hours') >= ?
                      AND datetime(exit_time, '+9 hours') <= ?
                      AND (kis_synced IS NULL OR kis_synced=0)
                      AND (order_no IS NULL OR order_no = '')
                    ORDER BY exit_time DESC LIMIT 1
                """, (code, market,
                      window_start.strftime("%Y-%m-%d %H:%M:%S"),
                      window_end.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()

            conn.close()
            if row:
                logger.debug(
                    f"[TradeSync] KST window 매칭: {code} {side} "
                    f"fill_kst={fill_dt_kst_iso[11:19]} "
                    f"window={window_start.strftime('%H:%M')}~{window_end.strftime('%H:%M')}"
                )
            return dict(row) if row else None
        except Exception as e:
            logger.debug(f"[TradeSync] KST DB 검색 실패 {code}: {e}")
            return None

    # ═══════════════════════════════════════════════════════════
    # 누락 거래 복구
    # ═══════════════════════════════════════════════════════════

    def _handle_missing_trade(self, fill: dict, market: str, fill_dt_iso: str,
                               code: str, name: str, side: str,
                               f_qty: int, f_price: float, order_no: str):
        """KIS에는 있는데 V2 내부 기록 없는 체결 → 즉시 복구"""
        logger.error(
            f"[LOG_MISSING_TRADE] KIS체결이력 있음/V2기록 없음 | "
            f"종목={name}({code}) | 방향={side} | "
            f"체결가={f_price} | 수량={f_qty} | "
            f"체결시각={fill_dt_iso} | 주문번호={order_no} | "
            f"출처=KIS실체결 → 내부 DB 복구 시작"
        )

        if side == "BUY":
            try:
                trade_id = self.recorder.record_entry(
                    market=market,
                    code=code,
                    name=name,
                    price=f_price,
                    qty=f_qty,
                    reason=f"[KIS_SYNC복구] order_no={order_no}",
                    iv={},
                    stage="",
                    signal_time=fill_dt_iso,
                    order_time=fill_dt_iso,
                    order_no=order_no,
                    price_source="KIS실체결",
                )
                # kis_synced=1 즉시 표시
                self._mark_synced(trade_id, order_no, fill_dt_iso)
                logger.error(
                    f"[LOG_MISSING_TRADE] ✅ BUY 복구완료 | "
                    f"종목={code} trade_id={trade_id}"
                )
            except Exception as e:
                logger.error(f"[LOG_MISSING_TRADE] ❌ BUY 복구실패 {code}: {e}")

        elif side == "SELL":
            open_trade = self._find_open_trade_for_sell(code, market)
            if open_trade:
                try:
                    ep  = float(open_trade.get("entry_price", 0) or 0)
                    pct = (f_price - ep) / ep * 100 if ep > 0 else 0.0
                    self.recorder.record_exit(
                        code=code,
                        exit_price=f_price,
                        exit_qty=f_qty,
                        exit_reason=f"[KIS_SYNC복구] order_no={order_no}",
                        exit_pct=pct,
                        pnl_krw=0.0,
                        trade_id=open_trade["trade_id"],
                        fill_time=fill_dt_iso,
                    )
                    self._mark_synced(open_trade["trade_id"], order_no, fill_dt_iso)
                    logger.error(
                        f"[LOG_MISSING_TRADE] ✅ SELL 복구완료 | "
                        f"종목={code} pct={pct:+.2f}%"
                    )
                except Exception as e:
                    logger.error(f"[LOG_MISSING_TRADE] ❌ SELL 복구실패 {code}: {e}")
            else:
                logger.error(
                    f"[LOG_MISSING_TRADE] ⚠️ SELL 복구불가(BUY기록없음) | "
                    f"종목={code} | 주문번호={order_no}"
                )

    def _find_open_trade_for_sell(self, code: str, market: str) -> Optional[dict]:
        """SELL 복구용: open 상태 거래 검색."""
        try:
            conn = _get_conn()
            row = conn.execute("""
                SELECT * FROM trades
                WHERE code=? AND market=? AND status='open'
                ORDER BY entry_time DESC LIMIT 1
            """, (code, market)).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception:
            return None

    # ═══════════════════════════════════════════════════════════
    # 로그 출력 헬퍼
    # ═══════════════════════════════════════════════════════════

    def _log_trade_sync(self, code: str, name: str, side: str,
                        fill_time: str, fill_price: float,
                        qty: int, order_no: str,
                        pnl_krw: Optional[float], trade_id: str):
        """[TRADE_SYNC] 로그 출력."""
        price_str = (
            f"${fill_price:.4f}" if fill_price < 10000
            else f"{fill_price:,.0f}원"
        )
        pnl_str = f"{pnl_krw:+,.0f}원" if pnl_krw is not None else "N/A"
        ft_str  = fill_time[11:19] if len(fill_time) > 10 else fill_time

        logger.info(
            f"[TRADE_SYNC] "
            f"종목={name}({code}) | "
            f"방향={side} | "
            f"체결시각={ft_str} | "
            f"체결가={price_str} | "
            f"수량={qty}주 | "
            f"실현손익={pnl_str} | "
            f"주문번호={order_no} | "
            f"출처=KIS실체결"
        )

    def _log_latency(self, db_trade: dict, fill_dt_iso: str, fill_price: float):
        """[EXECUTION_LATENCY] 로그 출력."""
        try:
            signal_time  = db_trade.get("signal_time",  "")
            order_time   = db_trade.get("order_time",   "")
            entry_price  = db_trade.get("entry_price",  0)

            if not signal_time or not fill_dt_iso:
                return

            sig_dt  = _to_kst(signal_time)
            fill_dt = _to_kst(fill_dt_iso)
            if not sig_dt or not fill_dt:
                return

            total_delay = (fill_dt - sig_dt).total_seconds()

            ord_delay = None
            if order_time:
                try:
                    ord_dt    = _to_kst(order_time)
                    if ord_dt:
                        ord_delay = (fill_dt - ord_dt).total_seconds()
                except Exception:
                    pass

            price_gap = 0.0
            if entry_price and fill_price:
                price_gap = (fill_price - entry_price) / entry_price * 100

            sig_str  = signal_time[11:19]  if len(signal_time)  > 10 else signal_time
            ord_str  = order_time[11:19]   if len(order_time)   > 10 else order_time
            fill_str = fill_dt_iso[11:19]  if len(fill_dt_iso)  > 10 else fill_dt_iso
            ep_str   = (f"${entry_price:.4f}" if entry_price < 10000
                        else f"{entry_price:,.0f}원")
            fp_str   = (f"${fill_price:.4f}"  if fill_price < 10000
                        else f"{fill_price:,.0f}원")

            logger.info(
                f"[EXECUTION_LATENCY] "
                f"종목={db_trade.get('code','?')} | "
                f"신호시각={sig_str} | "
                f"주문요청시각={ord_str} | "
                f"체결시각={fill_str} | "
                f"전체지연={total_delay:.1f}s"
                + (f" | 주문→체결={ord_delay:.1f}s" if ord_delay is not None else "")
                + f" | 신호가격={ep_str} | 체결가격={fp_str} | "
                f"가격괴리율={price_gap:+.3f}%"
            )
        except Exception as e:
            logger.debug(f"[TradeSync] latency 계산 오류: {e}")

    # ═══════════════════════════════════════════════════════════
    # DB 업데이트 헬퍼
    # ═══════════════════════════════════════════════════════════

    def _update_order_no(self, trade_id: str, order_no: str, fill_time: str):
        """내부 거래에 KIS 주문번호 + 체결시각 업데이트, kis_synced=1 표시."""
        try:
            conn = _get_conn()
            conn.execute("""
                UPDATE trades
                SET order_no=?, fill_time=?, kis_synced=1
                WHERE trade_id=?
            """, (order_no, fill_time, trade_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"[TradeSync] order_no 업데이트 실패: {e}")

    def _mark_synced(self, trade_id: str, order_no: str, fill_time: str):
        """복구 완료 후 kis_synced=1 표시 (중복 복구 방지)."""
        self._update_order_no(trade_id, order_no, fill_time)
        if order_no:
            self._synced_order_nos.add(order_no)
            self._order_no_map[order_no] = trade_id

    @staticmethod
    def _to_kst_iso(date_str: str, time_str: str) -> str:
        """
        YYYYMMDD + HHMMSS (KST 기준) → KST ISO datetime 문자열.
        KIS API 체결 시각은 KST 기준이므로 그대로 사용.
        """
        try:
            d = datetime.strptime(date_str, "%Y%m%d")
            t = time_str.zfill(6)
            naive_dt = d.replace(
                hour=int(t[0:2]),
                minute=int(t[2:4]),
                second=int(t[4:6])
            )
            # KST aware datetime
            kst_dt = KST.localize(naive_dt)
            return kst_dt.isoformat()
        except Exception:
            return datetime.now(KST).isoformat()

    # ═══════════════════════════════════════════════════════════
    # 포지션 최고/최저 수익률 업데이트 (루프마다 호출)
    # ═══════════════════════════════════════════════════════════

    def update_position_extremes(self, code: str, cur_price: float,
                                  entry_price: float, market: str):
        """
        보유 중 포지션의 max_pct / min_pct 갱신.
        strategy의 run() 루프에서 호출.
        """
        if entry_price <= 0 or cur_price <= 0:
            return
        cur_pct = (cur_price - entry_price) / entry_price * 100
        try:
            conn = _get_conn()
            row = conn.execute("""
                SELECT trade_id, max_pct, min_pct FROM trades
                WHERE code=? AND market=? AND status='open'
                ORDER BY entry_time DESC LIMIT 1
            """, (code, market)).fetchone()
            if not row:
                conn.close()
                return
            tid     = row["trade_id"]
            new_max = max(cur_pct, row["max_pct"] if row["max_pct"] is not None else cur_pct)
            new_min = min(cur_pct, row["min_pct"] if row["min_pct"] is not None else cur_pct)
            conn.execute("""
                UPDATE trades SET max_pct=?, min_pct=? WHERE trade_id=?
            """, (new_max, new_min, tid))
            conn.commit()
            conn.close()
        except Exception as _e:
            import logging as _lg
            _lg.getLogger("v2.TradeSync").debug(
                f"[update_position_extremes] {market}:{code} cur={cur_pct:+.2f}% 갱신 오류: {_e}"
            )
