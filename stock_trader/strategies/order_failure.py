"""주문 실패 분류 — 타임아웃(ambiguous) vs 거절(definitive) 구분 (pandas-free).

P0-4: rt_cd≠0 을 무조건 '실패'로 보고 맹목 재시도하면, 실제로는 거래소에
접수/체결됐을 수 있는 '타임아웃' 주문을 재전송해 이중매도가 발생한다.

분류 규칙(SELL/BUY 공통):
  - REJECTED : 주문이 거래소에 **접수되지 않았음이 확정** — 재시도 안전.
      · 우리 측 가드가 사전 차단(_http_status="BLOCKED")
      · KIS 업무 거절(HTTP 200 응답 + rt_cd≠0, 예: 수량초과 APBK0988, IGW 오류)
      · HTTP 4xx (요청 자체 거절)
  - TIMEOUT  : 접수 여부 **불명** — 반드시 KIS 조회로 확인해야 함(맹목 재시도 금지).
      · 네트워크 예외(_http_status="Exception")
      · HTTP 5xx (서버 오류 — 게이트웨이가 받았을 수 있음)
      · 명시적 타임아웃(_http_status="TIMEOUT")
"""

FAIL_REJECTED = "REJECTED"
FAIL_TIMEOUT = "TIMEOUT"


def classify_order_failure(result: dict) -> str:
    """실패한 주문 응답(rt_cd≠0)을 REJECTED / TIMEOUT 으로 분류한다.

    Args:
        result: KIS buy()/sell() 반환 dict (rt_cd != "0" 인 경우).

    Returns:
        FAIL_REJECTED | FAIL_TIMEOUT
    """
    if not isinstance(result, dict):
        # 응답 자체가 없음 = 네트워크/예외 → 불명(보수적으로 TIMEOUT)
        return FAIL_TIMEOUT

    http = result.get("_http_status", None)

    # 우리 측 사전 가드 차단 → 절대 전송 안 됨 → 안전(거절)
    if http == "BLOCKED":
        return FAIL_REJECTED

    # 네트워크 예외 / 명시적 타임아웃 → 불명
    if http in ("Exception", "TIMEOUT"):
        return FAIL_TIMEOUT

    # HTTP 상태코드가 숫자면: 5xx=서버측(불명), 그 외(4xx 등)=요청거절(안전)
    if isinstance(http, int):
        return FAIL_TIMEOUT if http >= 500 else FAIL_REJECTED
    if isinstance(http, str) and http.isdigit():
        return FAIL_TIMEOUT if int(http) >= 500 else FAIL_REJECTED

    # _http_status 정보가 없으면: rt_cd 로 판정.
    #  rt_cd=="9" 는 kis_api 가 예외/서버오류/차단에 붙이는 코드이나,
    #  _http_status 가 없으면 원인 불명 → 보수적으로 TIMEOUT(검증 유도).
    if str(result.get("rt_cd", "")) == "9":
        return FAIL_TIMEOUT

    # 그 외(HTTP 200 업무 거절 등) → 접수 안 됨(거절)
    return FAIL_REJECTED
