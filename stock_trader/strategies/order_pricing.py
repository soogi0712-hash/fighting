"""주문 가격(ORD_UNPR) 결정 — 매수/매도 대칭 로직 (pandas-free, 단위테스트 가능).

P0-1: 정규장 지정가(00) 매도에 ORD_UNPR=0 이 들어가 KIS가 주문을 차단하던 버그 수정.
매수는 이미 정규장 지정가에 유효 가격을 넣는데, 매도만 0을 넣어 비대칭이었다.
"""

# market_session 과 동일 의미의 주문구분 상수
ORD_LIMIT = "00"   # 지정가
ORD_MARKET = "01"  # 시장가
ORD_PRE = "05"     # 장전시간외 단일가
ORD_POST = "06"    # 장후시간외 단일가


def select_sell_price(ord_dvsn: str, decision_price, cur_price) -> int:
    """SELL 주문의 ORD_UNPR 결정.

    - 시장가(01) / 장후시간외(06): 0 (KIS 규칙)
    - 장전시간외(05): 유효 가격 필수
    - 정규장 지정가(00): 유효 지정가(>0) 필수 — decision_price 우선, 없으면 현재가
      (0 을 넣으면 kis_api 의 ORDER PRICE CHECK 가 '지정가 ORD_UNPR≤0' 로 차단함)
    """
    dp = int(decision_price) if decision_price and decision_price > 0 else 0
    cp = int(cur_price) if cur_price and cur_price > 0 else 0
    if ord_dvsn == ORD_MARKET:
        return 0
    if ord_dvsn == ORD_POST:
        return 0
    if ord_dvsn == ORD_PRE:
        return dp or cp
    # ORD_LIMIT("00") 정규장 지정가 → 절대 0 이 되지 않도록 현재가라도 사용
    return dp or cp
