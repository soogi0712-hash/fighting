#!/usr/bin/env python3
"""
force_sell_hims.py — HIMS 보유 포지션 강제 수동 청산
사용법: python3 scripts/force_sell_hims.py [--dry-run]

배경:
  - 06-26 22:33 KST: HIMS 8주 SELL_FAIL(rt_cd=7)
  - 원인: 기존 미체결 SELL 주문이 ord_psbl_qty=0으로 만든 상태
  - 현재: KIS 잔고에 HIMS 8주 보유 (또는 기존 SELL 주문으로 이미 청산됐을 수 있음)
  - 월요일 장 시작 전 반드시 확인 필요
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    dry_run = "--dry-run" in sys.argv
    
    print("=== HIMS 강제청산 스크립트 ===")
    print(f"모드: {'DRY-RUN (실제 주문 없음)' if dry_run else '실제 주문 실행'}")
    print()
    
    try:
        from broker.us_broker import USBroker
        broker = USBroker()
        
        # 현재 HIMS 잔고 조회
        balance = broker.get_stock_balance()
        hims_qty = 0
        hims_avg = 0.0
        
        for item in balance:
            code = item.get("pdno", "") or item.get("ovrs_pdno", "")
            if code == "HIMS":
                hims_qty = int(item.get("cblc_qty13", 0) or item.get("ord_psbl_qty", 0))
                hims_avg = float(item.get("pchs_avg_pric", 0))
                break
        
        print(f"KIS 잔고 조회 결과: HIMS {hims_qty}주 @ ${hims_avg:.2f}")
        
        if hims_qty == 0:
            print("✅ HIMS 잔고 없음 — 이미 청산되었거나 미체결 주문으로 처리 중")
            print("   KIS 앱에서 미체결 주문 확인 후 수동 취소하세요")
            return
        
        # 미체결 주문 취소
        print(f"\n1단계: HIMS 미체결 주문 취소 시도...")
        cancel_result = broker.cancel_all_pending_orders("HIMS", "NASD")
        print(f"   취소 결과: {cancel_result}")
        
        if dry_run:
            print(f"\n[DRY-RUN] HIMS {hims_qty}주 시장가 매도 주문 (실제 미실행)")
            return
        
        # 시장가 매도
        import time
        time.sleep(1)
        print(f"\n2단계: HIMS {hims_qty}주 시장가 매도...")
        result = broker.place_sell_order(
            code="HIMS", exch_cd="NASD",
            qty=hims_qty, price=0, ord_dvsn="01"  # 시장가
        )
        print(f"   주문 결과: {result}")
        
    except Exception as e:
        print(f"❌ 오류: {e}")
        import traceback
        traceback.print_exc()
        print()
        print("=== 수동 처리 가이드 ===")
        print("1. KIS 앱 → 해외주식 → 미체결 → HIMS 취소")
        print("2. KIS 앱 → 해외주식 → 잔고 확인 → HIMS 수량 확인")  
        print("3. 월요일 프리마켓 시작 시(KST 22:00) 시장가 매도")
        print("4. HIMS avg=$32.935, 현재 약 $32~33 예상")

if __name__ == "__main__":
    main()
