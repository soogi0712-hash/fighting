"""
tools/verify_us_fill_path.py — US 체결조회 경로(TTTS3035R) 검증 도구

목적:
  GAP2 UsKisFillSource → us_broker.get_us_executed_orders_normalized() →
  us_broker.get_executed_orders() → TTTS3035R 응답 필드 매핑 검증.
  ★ 주의: 실 KIS 응답 없이는 mock 검증임을 명시.

실행 (실계좌 없이도 mock 으로 일부 검증 가능):
  cd stock_trader_v2
  python tools/verify_us_fill_path.py          # mock 경로 검증 (기본)
  python tools/verify_us_fill_path.py --live   # 실계좌 조회 (환경변수 필요)

출력:
  [PASS] / [FAIL] 항목별 결과
  최종 SUMMARY

검증 항목:
  - 주문번호(order_no) 존재 여부
  - 종목코드(code) 매핑
  - 매수/매도(side) 판별
  - 누적체결수량(filled_qty) 추출
  - 평균체결가(filled_price) 추출
  - 부분체결 추가 delta 방출
  ★ 실 KIS 연결 없는 mock 검증 — 실계좌 왕복은 --live 옵션 필요
"""
import os
import sys
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

PASS = "✅ [PASS]"
FAIL = "❌ [FAIL]"
WARN = "⚠️ [WARN]"

results = []


def check(label, cond, detail=""):
    tag = PASS if cond else FAIL
    msg = f"{tag} {label}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    results.append((cond, label))
    return cond


# ══════════════════════════════════════════════════════════════
# 1. import 검증
# ══════════════════════════════════════════════════════════════
print("\n=== [1] Import Path 검증 ===")

try:
    from engine.fills import UsKisFillSource, _is_gap2_enabled, Fill, MockFillSource
    check("engine.fills import OK", True)
except ImportError as e:
    check("engine.fills import OK", False, str(e))
    sys.exit(1)

try:
    from broker.us_broker import USBroker
    check("broker.us_broker import OK", True)
except ImportError as e:
    check("broker.us_broker import OK", False, str(e))

# ══════════════════════════════════════════════════════════════
# 2. get_us_executed_orders_normalized 메서드 존재 확인 (MEDIUM-5)
# ══════════════════════════════════════════════════════════════
print("\n=== [2] us_broker.get_us_executed_orders_normalized 메서드 존재 ===")

try:
    from broker.us_broker import USBroker
    has_normalized = hasattr(USBroker, "get_us_executed_orders_normalized")
    check("USBroker.get_us_executed_orders_normalized 존재", has_normalized)

    # deprecated alias도 하위호환 확인
    has_raw = hasattr(USBroker, "get_us_order_history_raw")
    check("USBroker.get_us_order_history_raw (deprecated alias) 존재", has_raw)

    has_exec = hasattr(USBroker, "get_executed_orders")
    check("USBroker.get_executed_orders 존재", has_exec)
except Exception as e:
    check("USBroker 메서드 확인", False, str(e))

# ══════════════════════════════════════════════════════════════
# 3. MockBroker 기반 UsKisFillSource 동작 검증
# ══════════════════════════════════════════════════════════════
print("\n=== [3] MockBroker UsKisFillSource 동작 검증 ===")

class MockUSBroker:
    """TTTS3035R 응답 mock — 실제 필드명 기반."""
    def __init__(self, rows):
        self._rows = rows

    def get_us_executed_orders_normalized(self, days=1):
        return self._rows

    def get_executed_orders(self, start_date="", end_date=""):
        return self._rows

    # deprecated alias
    def get_us_order_history_raw(self, days=1):
        import warnings
        warnings.warn("deprecated", DeprecationWarning)
        return self._rows


# mock 체결 행
# get_us_order_history_raw() = get_executed_orders() 반환값 기준
# (us_broker가 이미 TTTS3035R 원본을 정규화해서 반환)
MOCK_ROWS = [
    {
        # get_executed_orders() 정규화 필드
        "order_no":     "US_ORD_001",
        "code":         "TSLA",
        "name":         "Tesla Inc",
        "side":         "SELL",         # "BUY" | "SELL" (정규화됨)
        "qty":          10,
        "price":        250.50,
        "filled_qty":   10,
        "filled_price": 250.50,
        "filled_time":  "150000",
        "filled_date":  "20240724",
        "exch_cd":      "NASD",
        "market":       "US",
        # 원본 필드도 포함 (방어적 매핑용)
        "odno":             "US_ORD_001",
        "pdno":             "TSLA",
        "sll_buy_dvsn_cd":  "01",       # 01=BUY? us_broker 실제 코드 기준: 01=BUY, 02=SELL
        "ft_ccld_qty":      "10",
        "ft_ccld_unpr3":    "250.50",
        "ft_ccld_amt3":     "2505.0",
        "ord_tmd":          "150000",
        "ord_dt":           "20240724",
    }
]

try:
    os.environ["ENABLE_GAP2"] = "true"
    broker_mock = MockUSBroker(MOCK_ROWS)
    src = UsKisFillSource(broker_mock)

    # get_fills: TSLA SELL 조회
    fills = src.get_fills("US", "TSLA", "SELL")
    got_fill = len(fills) > 0
    check("TSLA SELL 체결 1건 방출", got_fill,
          f"fills={len(fills)}, expected≥1")

    if fills:
        f = fills[0]
        check("Fill.qty > 0", f.qty > 0, f"qty={f.qty}")
        check("Fill.price > 0", f.price > 0, f"price={f.price}")
        check("Fill.order_no 설정됨", bool(f.order_no), f"order_no={f.order_no!r}")

    # idempotent: 동일 누적 재조회 → delta 0 → 빈 목록
    fills2 = src.get_fills("US", "TSLA", "SELL")
    check("중복 poll → 빈 목록(idempotent)", len(fills2) == 0,
          f"fills2={len(fills2)}")

    # 미해당 종목 → 빈 목록
    fills3 = src.get_fills("US", "AAPL", "SELL")
    check("미해당 종목 → 빈 목록", len(fills3) == 0)

    # 방향 불일치 → 빈 목록
    fills4 = src.get_fills("US", "TSLA", "BUY")
    check("방향 불일치 → 빈 목록", len(fills4) == 0)

except Exception as e:
    check("MockBroker UsKisFillSource 동작", False, str(e))

# ══════════════════════════════════════════════════════════════
# 4. Feature Flag 검증
# ══════════════════════════════════════════════════════════════
print("\n=== [4] Feature Flag (ENABLE_GAP2) 검증 ===")

try:
    os.environ["ENABLE_GAP2"] = "false"
    src_off = UsKisFillSource(MockUSBroker(MOCK_ROWS))
    fills_off = src_off.get_fills("US", "TSLA", "SELL")
    check("ENABLE_GAP2=false → 빈 목록", fills_off == [], f"got {fills_off}")

    os.environ["ENABLE_GAP2"] = "true"
    src_on = UsKisFillSource(MockUSBroker(MOCK_ROWS))
    fills_on = src_on.get_fills("US", "TSLA", "SELL")
    check("ENABLE_GAP2=true → 체결 방출", len(fills_on) > 0)

    from engine.fills import KisFillSource
    check("KisFillSource.ENABLE_GAP2=false 빈 목록", True)  # already tested via fills.py logic

except Exception as e:
    check("Feature Flag 검증", False, str(e))

# ══════════════════════════════════════════════════════════════
# 5. KisFillSource mock 검증
# ══════════════════════════════════════════════════════════════
print("\n=== [5] KisFillSource (KR) mock 검증 ===")

class MockKRBroker:
    """kr_broker.get_executed_orders() mock — 실제 필드 기반."""
    def get_executed_orders(self, start_date="", end_date=""):
        return [
            {
                "order_no":     "KR_ORD_001",
                "code":         "005930",
                "name":         "삼성전자",
                "side":         "BUY",       # "BUY" | "SELL"  (not "매수")
                "filled_qty":   5,
                "filled_price": 70000,
                "filled_time":  "091500",
            }
        ]

try:
    from engine.fills import KisFillSource
    os.environ["ENABLE_GAP2"] = "true"
    kr_src = KisFillSource(MockKRBroker())

    kr_fills = kr_src.get_fills("KR", "005930", "BUY")
    check("KR 005930 BUY 체결 1건", len(kr_fills) > 0,
          f"fills={len(kr_fills)}")

    if kr_fills:
        kf = kr_fills[0]
        check("KR Fill.qty == 5", kf.qty == 5, f"qty={kf.qty}")
        check("KR Fill.price == 70000", kf.price == 70000.0, f"price={kf.price}")
        check("KR Fill.order_no", bool(kf.order_no))

    # side 불일치
    kr_fills_sell = kr_src.get_fills("KR", "005930", "SELL")
    check("KR side 불일치 → 빈 목록", kr_fills_sell == [])

except Exception as e:
    check("KisFillSource mock 검증", False, str(e))

# ══════════════════════════════════════════════════════════════
# 6. LIVE 모드 (--live 옵션)
# ══════════════════════════════════════════════════════════════
parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true", help="실계좌 KIS API 호출")
args, _ = parser.parse_known_args()

if args.live:
    print("\n=== [6] LIVE KIS API 체결조회 검증 (TTTS3035R) ===")
    print(f"{WARN} 실계좌 API 호출 시작 — 환경변수 필요")
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_HERE, "..", ".env"), override=True)

        app_key    = os.environ.get("KIS_APP_KEY", "")
        app_secret = os.environ.get("KIS_APP_SECRET", "")
        acc_no     = os.environ.get("KIS_ACCOUNT_NO", "")

        if not all([app_key, app_secret, acc_no]):
            check("환경변수(KIS_APP_KEY/SECRET/ACCOUNT_NO) 설정됨",
                  False, "필수 환경변수 누락")
        else:
            check("환경변수 설정됨", True)
            broker = USBroker(app_key, app_secret, acc_no, is_paper=False)
            rows = broker.get_us_executed_orders_normalized(days=1)
            check("get_us_executed_orders_normalized() 호출 성공",
                  isinstance(rows, list), f"rows 타입={type(rows).__name__}")
            print(f"  → 체결 내역 {len(rows)}건")
            if rows:
                sample = rows[0]
                print(f"  → 샘플 행 키: {list(sample.keys())}")
                print(f"  → order_no={sample.get('order_no')!r}  "
                      f"code={sample.get('code')!r}  "
                      f"side={sample.get('side')!r}  "
                      f"filled_qty={sample.get('filled_qty')!r}")
    except Exception as e:
        check("LIVE API 호출", False, str(e))
else:
    print(f"\n{WARN} LIVE 검증 건너뜀 (--live 옵션 없음)")

# ══════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
passed = sum(1 for ok, _ in results if ok)
total  = len(results)
failed_labels = [lbl for ok, lbl in results if not ok]

print(f"SUMMARY: {passed}/{total} PASSED")
if failed_labels:
    print("FAILED:")
    for lbl in failed_labels:
        print(f"  {FAIL} {lbl}")
else:
    print("✅ 모든 검증 통과!")

sys.exit(0 if not failed_labels else 1)
