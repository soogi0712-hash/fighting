"""
ledger — 실거래 원장 (Real-trade ledger)

목적:
  - 실제 체결된 거래를 정확히 기록하고, 수수료·세금 반영 후 순손익(net_pnl)을 계산한다.
  - 진입/청산 이유·지표, MFE/MAE, 보유시간을 남겨 손실 원인 분석의 기반을 만든다.

원칙:
  - Shadow(strategy_lab/lab.db)와 물리적으로 완전 분리된 별도 파일 data/ledger.db 사용.
  - 이 모듈은 '기록'만 한다. 주문 API 를 import 하지 않으므로 구조적으로 실주문이 불가능하다.
  - 비용 계산은 screener.transaction_cost.calc_trade_result() (SSOT) 를 재활용한다.
"""
from .ledger_db import LEDGER_DB_PATH, init_ledger, get_conn
from .recorder import LedgerRecorder

__all__ = ["LEDGER_DB_PATH", "init_ledger", "get_conn", "LedgerRecorder"]
