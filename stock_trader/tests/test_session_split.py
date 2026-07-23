"""
test_session_split.py — [E] 세션 기반 intraday/overnight 분류 검증 (오프라인)
실행: python3 tests/test_session_split.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.session_split import classify_session, session_report


def test_kr_intraday_same_kst_date():
    assert classify_session("KR", "2026-07-23T10:00:00+09:00", "2026-07-23T14:00:00+09:00") == "intraday"
    print("✓ KR 같은 KST 거래일 → intraday")


def test_kr_overnight_next_kst_date():
    assert classify_session("KR", "2026-07-23T14:00:00+09:00", "2026-07-24T10:00:00+09:00") == "overnight"
    print("✓ KR 다음 KST 거래일 → overnight")


def test_kr_tz_conversion_crosses_date():
    # 14:30Z = 23:30 KST(23일), 15:30Z = 00:30 KST(24일) → KST 날짜 다름 → overnight
    assert classify_session("KR", "2026-07-23T14:30:00Z", "2026-07-23T15:30:00Z") == "overnight"
    print("✓ KR UTC→KST 변환으로 날짜 넘어감 → overnight (timezone 명시 사용)")


def test_us_intraday_afterhours_same_et_date():
    # 20:00Z=16:00 ET(정규 마감), 23:30Z=19:30 ET(애프터) 같은 ET 날짜 → intraday
    assert classify_session("US", "2026-07-23T20:00:00Z", "2026-07-23T23:30:00Z") == "intraday"
    print("✓ US 애프터마켓 같은 ET 날짜 → intraday")


def test_us_overnight_next_et_date():
    # 20:00Z=16:00 ET(23일), 13:35Z(24일)=09:35 ET(24일) → overnight
    assert classify_session("US", "2026-07-23T20:00:00Z", "2026-07-24T13:35:00Z") == "overnight"
    print("✓ US 다음 ET 거래일 → overnight")


def test_naive_timestamp_is_unknown():
    # tz 정보 없는 naive → 임의 분류 금지 → unknown
    assert classify_session("KR", "2026-07-23T10:00:00", "2026-07-23T14:00:00") == "unknown"
    print("✓ naive(타임존 없음) → unknown (임의 분류 안 함)")


def test_missing_or_bad_ts_unknown():
    assert classify_session("KR", None, "2026-07-23T14:00:00+09:00") == "unknown"
    assert classify_session("KR", "bad", "also-bad") == "unknown"
    assert classify_session("JP", "2026-07-23T10:00:00+09:00", "2026-07-23T14:00:00+09:00") == "unknown"
    print("✓ 결측/파싱실패/미지원시장 → unknown")


def test_report_groups_and_metrics():
    trades = [
        {"market": "KR", "entry_ts": "2026-07-23T10:00:00+09:00", "exit_ts": "2026-07-23T10:40:00+09:00", "net_pnl": 5000, "hold_seconds": 2400},
        {"market": "KR", "entry_ts": "2026-07-23T14:00:00+09:00", "exit_ts": "2026-07-24T10:00:00+09:00", "net_pnl": -3000, "hold_seconds": 72000},
        {"market": "KR", "entry_ts": "2026-07-23T10:00:00", "exit_ts": "2026-07-23T11:00:00", "net_pnl": 1000},  # naive→unknown
    ]
    rep = session_report(trades)
    assert rep["intraday"]["count"] == 1 and rep["intraday"]["win_rate"] == 100.0
    assert rep["overnight"]["count"] == 1 and rep["overnight"]["total_net"] == -3000
    assert rep["unknown"]["count"] == 1
    print("✓ 리포트: intraday/overnight/unknown 분리 + 지표 산출")


def _run():
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
    print("\n=== E tests passed ===")


if __name__ == "__main__":
    _run()
