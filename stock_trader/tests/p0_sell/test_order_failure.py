"""P0-4 주문 실패 분류 — classify_order_failure 단위 테스트."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from strategies.order_failure import (  # noqa: E402
    classify_order_failure, FAIL_TIMEOUT, FAIL_REJECTED,
)


class OrderFailureTest(unittest.TestCase):
    def test_http_500_is_timeout(self):
        self.assertEqual(
            classify_order_failure({"rt_cd": "9", "_http_status": 500}),
            FAIL_TIMEOUT)

    def test_exception_is_timeout(self):
        self.assertEqual(
            classify_order_failure({"rt_cd": "9", "_http_status": "Exception"}),
            FAIL_TIMEOUT)

    def test_explicit_timeout_is_timeout(self):
        self.assertEqual(
            classify_order_failure({"rt_cd": "9", "_http_status": "TIMEOUT"}),
            FAIL_TIMEOUT)

    def test_blocked_is_rejected(self):
        # 우리 측 가드 차단 → 전송 안 됨 → 안전(거절)
        self.assertEqual(
            classify_order_failure({"rt_cd": "9", "_http_status": "BLOCKED"}),
            FAIL_REJECTED)

    def test_http_400_is_rejected(self):
        self.assertEqual(
            classify_order_failure({"rt_cd": "9", "_http_status": 400}),
            FAIL_REJECTED)

    def test_business_rejection_http200_is_rejected(self):
        # HTTP 200 + rt_cd=1 (수량초과 등) → 접수 안 됨 → 거절
        self.assertEqual(
            classify_order_failure({"rt_cd": "1", "msg_cd": "APBK0988",
                                    "msg1": "주문가능수량초과"}),
            FAIL_REJECTED)

    def test_rtcd9_no_http_is_timeout(self):
        # _http_status 없음 + rt_cd=9 → 원인불명 → 보수적 TIMEOUT
        self.assertEqual(
            classify_order_failure({"rt_cd": "9"}), FAIL_TIMEOUT)

    def test_none_result_is_timeout(self):
        self.assertEqual(classify_order_failure(None), FAIL_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
