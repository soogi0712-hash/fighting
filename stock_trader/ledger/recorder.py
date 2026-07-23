"""
recorder.py — LedgerRecorder: 실거래 기록 모듈 (KR/US 공통)

공개 API:
  on_buy_fill(...)   매수 체결 확정 시 호출 → OPEN 생성 또는 진입 집계 갱신(피라미딩)
  on_sell_fill(...)  매도 체결 확정 시 호출 → 청산 집계 갱신 → PARTIAL 또는 CLOSED
  on_tick(...)       보유 중 감시틱마다 호출 → MFE/MAE 러닝 갱신

원칙:
  - 이 모듈은 원장에 '기록'만 한다. 주문/조회 API 를 import 하지 않는다.
  - 순손익(net_pnl)은 실제 체결가 기준으로 calc_trade_result() 로 계산한다.
    실제 체결가에 슬리피지가 이미 포함되므로, slippage 를 net_pnl 에서 다시 빼지 않는다.
    slippage 필드는 decision_price 대비 체결 품질 분석용으로만 저장한다.
  - 신 수수료율을 임의로 가정하지 않는다. rates 미지정 시 transaction_cost 기본값(SSOT)을 사용.
  - 평균원가법 + '완전청산(flat) 시에만 CLOSE' 로 피라미딩·분할청산을 한 행에 표현.
"""
import json
from datetime import datetime

from .ledger_db import init_ledger

# 비용 계산 SSOT 재활용
try:
    from screener.transaction_cost import calc_trade_result
except ImportError:  # 패키지 상대 경로 폴백
    from ..screener.transaction_cost import calc_trade_result


def _now_iso(ts=None) -> str:
    return ts if ts else datetime.now().isoformat()


def _dumps(obj):
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, ensure_ascii=False)


def _hold_seconds(entry_time: str, exit_time: str):
    try:
        return int((datetime.fromisoformat(exit_time)
                    - datetime.fromisoformat(entry_time)).total_seconds())
    except Exception:
        return None


class LedgerRecorder:
    def __init__(self, db_path: str = None):
        self.conn = init_ledger(db_path)

    # ──────────────────────────────────────────────────────────
    # 내부 헬퍼
    # ──────────────────────────────────────────────────────────
    def _get_open(self, market: str, code: str):
        """OPEN 또는 PARTIAL 상태의 현재 포지션 1건 반환 (없으면 None)."""
        return self.conn.execute(
            "SELECT * FROM trades WHERE market=? AND code=? AND status IN ('OPEN','PARTIAL') "
            "ORDER BY id DESC LIMIT 1",
            (market, code),
        ).fetchone()

    @staticmethod
    def _seen(raw_json, side, order_no) -> bool:
        """동일 (side, order_no) 이미 기록됨? → 멱등(중복주문 재기록 방지)."""
        if not raw_json or order_no is None:
            return False
        try:
            for o in json.loads(raw_json):
                if o.get("side") == side and o.get("order_no") == order_no:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _append_raw(raw_json, entry: dict) -> str:
        try:
            arr = json.loads(raw_json) if raw_json else []
        except Exception:
            arr = []
        arr.append(entry)
        return json.dumps(arr, ensure_ascii=False)

    # ──────────────────────────────────────────────────────────
    # 매수 체결
    # ──────────────────────────────────────────────────────────
    def on_buy_fill(self, market, code, name, order_no, fill_price, fill_qty, *,
                    decision_price=None, strategy_version=None, param_snapshot=None,
                    entry_reason=None, entry_indicators=None, ts=None):
        if fill_qty is None or fill_qty <= 0 or fill_price is None or fill_price <= 0:
            raise ValueError(f"invalid buy fill: price={fill_price} qty={fill_qty}")
        now = _now_iso(ts)
        row = self._get_open(market, code)

        if row is None:
            # 신규 OPEN
            raw = self._append_raw(None, {"side": "BUY", "order_no": order_no,
                                          "price": fill_price, "qty": fill_qty, "ts": now})
            slip = (fill_price - decision_price) if decision_price else None
            self.conn.execute("""
                INSERT INTO trades(
                  market, code, name, status, entry_order_no,
                  strategy_version, param_snapshot_json,
                  entry_reason, entry_indicators_json, entry_time,
                  entry_decision_price, avg_entry_price, entry_qty_total, entry_fill_count,
                  entry_slippage, mfe_pct, mae_pct, raw_orders_json,
                  created_at, updated_at)
                VALUES (?,?,?,'OPEN',?, ?,?, ?,?,?, ?,?,?,1, ?, 0, 0, ?, ?, ?)
            """, (market, code, name, order_no,
                  strategy_version, _dumps(param_snapshot),
                  entry_reason, _dumps(entry_indicators), now,
                  decision_price, fill_price, fill_qty,
                  slip, raw, now, now))
            self.conn.commit()
            return

        # 멱등: 동일 매수주문 재기록 방지
        if self._seen(row["raw_orders_json"], "BUY", order_no):
            return

        # 진입 집계 갱신 (피라미딩)
        new_qty = row["entry_qty_total"] + fill_qty
        new_avg = (row["avg_entry_price"] * row["entry_qty_total"]
                   + fill_price * fill_qty) / new_qty
        dprice = row["entry_decision_price"] if row["entry_decision_price"] is not None else decision_price
        slip = (new_avg - dprice) if dprice else None
        raw = self._append_raw(row["raw_orders_json"], {"side": "BUY", "order_no": order_no,
                                                        "price": fill_price, "qty": fill_qty, "ts": now})
        # MFE/MAE 갱신 (신규 평균 기준 현재 체결가)
        pct = (fill_price - new_avg) / new_avg * 100
        mfe = max(row["mfe_pct"] or 0.0, pct)
        mae = min(row["mae_pct"] or 0.0, pct)
        self.conn.execute("""
            UPDATE trades SET
              avg_entry_price=?, entry_qty_total=?, entry_fill_count=entry_fill_count+1,
              entry_decision_price=?, entry_slippage=?, raw_orders_json=?,
              mfe_pct=?, mae_pct=?, updated_at=?
            WHERE id=?
        """, (new_avg, new_qty, dprice, slip, raw, mfe, mae, now, row["id"]))
        self.conn.commit()

    # ──────────────────────────────────────────────────────────
    # 매도 체결
    # ──────────────────────────────────────────────────────────
    def on_sell_fill(self, market, code, order_no, fill_price, fill_qty, *,
                     decision_price=None, exit_reason=None, exit_indicators=None,
                     ts=None, rates=None):
        if fill_qty is None or fill_qty <= 0 or fill_price is None or fill_price <= 0:
            raise ValueError(f"invalid sell fill: price={fill_price} qty={fill_qty}")
        now = _now_iso(ts)
        row = self._get_open(market, code)
        if row is None:
            # 진입 없는 매도는 원장 대상 아님 (실계좌에선 발생 안 함) — 방어적 무시
            return
        if self._seen(row["raw_orders_json"], "SELL", order_no):
            return

        new_exit_qty = row["exit_qty_total"] + fill_qty
        if new_exit_qty > row["entry_qty_total"]:
            # 보유수량 초과 매도 — 데이터 이상. 진입수량까지만 반영(방어)
            fill_qty = row["entry_qty_total"] - row["exit_qty_total"]
            new_exit_qty = row["entry_qty_total"]
            if fill_qty <= 0:
                return
        prev_exit_amt = (row["avg_exit_price"] or 0.0) * row["exit_qty_total"]
        new_exit_avg = (prev_exit_amt + fill_price * fill_qty) / new_exit_qty
        dprice = row["exit_decision_price"] if row["exit_decision_price"] is not None else decision_price
        raw = self._append_raw(row["raw_orders_json"], {"side": "SELL", "order_no": order_no,
                                                        "price": fill_price, "qty": fill_qty, "ts": now})
        pct = (fill_price - row["avg_entry_price"]) / row["avg_entry_price"] * 100
        mfe = max(row["mfe_pct"] or 0.0, pct)
        mae = min(row["mae_pct"] or 0.0, pct)

        if new_exit_qty < row["entry_qty_total"]:
            # 부분청산 → PARTIAL 유지 (net 미확정)
            self.conn.execute("""
                UPDATE trades SET
                  status='PARTIAL', exit_order_no=?, exit_reason=?, exit_indicators_json=?,
                  exit_time=?, exit_decision_price=?, avg_exit_price=?, exit_qty_total=?,
                  exit_fill_count=exit_fill_count+1, mfe_pct=?, mae_pct=?,
                  raw_orders_json=?, updated_at=?
                WHERE id=?
            """, (order_no, exit_reason, _dumps(exit_indicators), now, dprice,
                  new_exit_avg, new_exit_qty, mfe, mae, raw, now, row["id"]))
            self.conn.commit()
            return

        # 완전청산 → CLOSED, net 확정 (실제 체결가 기준, calc_trade_result SSOT)
        kw = rates or {}
        tr = calc_trade_result(row["avg_entry_price"], row["entry_qty_total"], new_exit_avg, **kw)
        fees = tr.buy_cost.commission + tr.sell_proceeds.commission
        tax  = tr.sell_proceeds.transaction_tax + tr.sell_proceeds.other_cost
        exit_slip = (new_exit_avg - dprice) if dprice else None
        hold = _hold_seconds(row["entry_time"], now)
        self.conn.execute("""
            UPDATE trades SET
              status='CLOSED', exit_order_no=?, exit_reason=?, exit_indicators_json=?,
              exit_time=?, exit_decision_price=?, avg_exit_price=?, exit_qty_total=?,
              exit_fill_count=exit_fill_count+1,
              realized_pnl=?, net_pnl=?, fees=?, tax=?, exit_slippage=?,
              mfe_pct=?, mae_pct=?, hold_seconds=?, raw_orders_json=?, updated_at=?
            WHERE id=?
        """, (order_no, exit_reason, _dumps(exit_indicators), now, dprice,
              new_exit_avg, new_exit_qty,
              tr.gross_profit, tr.net_profit, fees, tax, exit_slip,
              mfe, mae, hold, raw, now, row["id"]))
        self.conn.commit()

    # ──────────────────────────────────────────────────────────
    # 감시틱 (MFE/MAE)
    # ──────────────────────────────────────────────────────────
    def on_tick(self, market, code, last_price, ts=None):
        if last_price is None or last_price <= 0:
            return
        row = self._get_open(market, code)
        if row is None or not row["avg_entry_price"]:
            return
        pct = (last_price - row["avg_entry_price"]) / row["avg_entry_price"] * 100
        mfe = max(row["mfe_pct"] or 0.0, pct)
        mae = min(row["mae_pct"] or 0.0, pct)
        if mfe != (row["mfe_pct"] or 0.0) or mae != (row["mae_pct"] or 0.0):
            self.conn.execute(
                "UPDATE trades SET mfe_pct=?, mae_pct=?, updated_at=? WHERE id=?",
                (mfe, mae, _now_iso(ts), row["id"]),
            )
            self.conn.commit()
