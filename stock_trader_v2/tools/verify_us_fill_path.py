"""
tools/verify_us_fill_path.py — US 체결조회 경로 검증 도구 (GAP2)
실제 KIS 연결 없이 필드 구조·파싱 논리를 검증.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["ENABLE_GAP2"] = "true"

def test_us_fill_source_field_mapping():
    """UsKisFillSource가 get_us_order_history_raw() 응답을 올바르게 파싱하는지 검증."""
    from engine.fills import UsKisFillSource, Fill

    MOCK_ROWS = [
        {
            "odno":           "12345678",
            "pdno":           "AAPL",
            "prdt_name":      "Apple Inc.",
            "sll_buy_dvsn_cd": "01",          # 01=BUY (US broker 기준)
            "ft_ccld_qty":    "5",
            "ft_ccld_unpr3":  "185.50",
            "ft_ccld_amt3":   "927.50",
            "ord_tmd":        "143000",
        },
        {
            "odno":           "99999999",
            "pdno":           "TSLA",
            "sll_buy_dvsn_cd": "02",          # 02=SELL
            "ft_ccld_qty":    "3",
            "ft_ccld_unpr3":  "250.00",
            "ft_ccld_amt3":   "750.00",
        },
    ]

    class MockBroker:
        def get_us_order_history_raw(self, days=1):
            return MOCK_ROWS

    src   = UsKisFillSource(MockBroker())
    fills = src.get_fills("US", "AAPL", "BUY")
    assert len(fills) == 1, f"AAPL BUY fill 1개 기대, got {len(fills)}"
    assert fills[0].qty   == 5,      f"qty=5 기대, got {fills[0].qty}"
    assert fills[0].price == 185.50, f"price=185.50 기대, got {fills[0].price}"
    assert fills[0].order_no == "12345678"
    print("  OK  AAPL BUY fill mapping")

    # TSLA SELL
    fills2 = src.get_fills("US", "TSLA", "SELL")
    assert len(fills2) == 1
    assert fills2[0].qty == 3
    print("  OK  TSLA SELL fill mapping")

    # AAPL SELL = 없음
    fills3 = src.get_fills("US", "AAPL", "SELL")
    assert len(fills3) == 0
    print("  OK  AAPL SELL fill = 0 (올바른 방향 필터)")

    print("\n[verify_us_fill_path] 모든 검증 통과 ✓")
    print("⚠️  실제 KIS TTTS3035R API 연결은 미검증 (실계좌 없음)")

def test_get_us_order_history_raw_exists():
    """us_broker.USBroker에 get_us_order_history_raw() 메서드가 존재하는지 확인."""
    from broker.us_broker import USBroker
    assert hasattr(USBroker, "get_us_order_history_raw"), \
        "USBroker.get_us_order_history_raw() 없음!"
    print("  OK  USBroker.get_us_order_history_raw() 메서드 존재")

if __name__ == "__main__":
    print("=== verify_us_fill_path.py ===")
    test_get_us_order_history_raw_exists()
    test_us_fill_source_field_mapping()
