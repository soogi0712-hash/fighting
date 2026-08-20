"""US 인트라데이 봉 정규화 — 마감 완료 봉 판정 + 타임존 정규화 테스트."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import strategies.us_bars as B

UTC = timezone.utc
ET  = timezone(timedelta(hours=-4))   # EDT
KST = timezone(timedelta(hours=9))


def _bar(y, mo, d, h, mi, close, tz=UTC):
    return {"ts": datetime(y, mo, d, h, mi, tzinfo=tz), "close": close}


class NormalizeTest(unittest.TestCase):
    def test_utc_et_kst_same_bar(self):
        # 같은 실제 순간(UTC 14:31)을 ET/UTC/KST 로 표현 → 1개 봉으로 정규화
        et  = {"ts": datetime(2026, 8, 14, 10, 31, tzinfo=ET),  "close": 50.0}
        utc = {"ts": datetime(2026, 8, 14, 14, 31, tzinfo=UTC), "close": 50.0}
        kst = {"ts": datetime(2026, 8, 14, 23, 31, tzinfo=KST), "close": 50.0}
        bars = B.normalize_bars([et, utc, kst])
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0][0], datetime(2026, 8, 14, 14, 31, tzinfo=UTC))

    def test_late_correction_last_wins(self):
        b1 = _bar(2026, 8, 14, 14, 31, 50.0)
        b2 = _bar(2026, 8, 14, 14, 31, 51.5)   # 같은 봉 늦은 정정
        bars = B.normalize_bars([b1, b2])
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0][1], 51.5)

    def test_naive_assumed_utc(self):
        b = {"ts": datetime(2026, 8, 14, 14, 31), "close": 10.0}   # naive
        bars = B.normalize_bars([b])
        self.assertEqual(bars[0][0].tzinfo, UTC)

    def test_skip_missing(self):
        bars = B.normalize_bars([{"ts": None, "close": 1.0}, {"close": 2.0},
                                 _bar(2026, 8, 14, 14, 31, 3.0)])
        self.assertEqual(len(bars), 1)


class Completed1mTest(unittest.TestCase):
    def setUp(self):
        # 22:30, 22:31, 22:32 (UTC) 1분봉
        self.bars = B.normalize_bars([
            _bar(2026, 8, 14, 22, 30, 100.0),
            _bar(2026, 8, 14, 22, 31, 98.0),
            _bar(2026, 8, 14, 22, 32, 97.0),
        ])

    def test_in_progress_bar_excluded(self):
        # now=22:31:10 → 22:31봉은 22:32에 마감(미완성) → 마지막 완료봉=22:30
        now = datetime(2026, 8, 14, 22, 31, 10, tzinfo=UTC)
        ts, close = B.last_completed_1m(self.bars, now)
        self.assertEqual(close, 100.0)
        self.assertEqual(ts, datetime(2026, 8, 14, 22, 30, tzinfo=UTC).isoformat())

    def test_completed_bar(self):
        # now=22:32:05 → 22:31봉 완료(22:32<=now) → 마지막 완료봉=22:31
        now = datetime(2026, 8, 14, 22, 32, 5, tzinfo=UTC)
        ts, close = B.last_completed_1m(self.bars, now)
        self.assertEqual(close, 98.0)

    def test_none_when_all_in_progress(self):
        now = datetime(2026, 8, 14, 22, 30, 30, tzinfo=UTC)
        ts, close = B.last_completed_1m(self.bars, now)
        self.assertIsNone(ts)   # 22:30봉도 22:31에 마감 → 완료봉 없음


class Completed5mTest(unittest.TestCase):
    def setUp(self):
        # 22:30~22:34 (버킷 22:30), 22:35 (버킷 22:35)
        self.bars = B.normalize_bars([
            _bar(2026, 8, 14, 22, 30, 100.0),
            _bar(2026, 8, 14, 22, 31, 99.0),
            _bar(2026, 8, 14, 22, 33, 97.0),
            _bar(2026, 8, 14, 22, 34, 96.0),
            _bar(2026, 8, 14, 22, 35, 95.0),
        ])

    def test_completed_5m_close(self):
        # now=22:35:30 → 버킷 22:30 완료(22:35<=now), 종가=22:34봉=96.0
        now = datetime(2026, 8, 14, 22, 35, 30, tzinfo=UTC)
        ts, close = B.last_completed_5m(self.bars, now)
        self.assertEqual(close, 96.0)
        self.assertEqual(ts, datetime(2026, 8, 14, 22, 30, tzinfo=UTC).isoformat())

    def test_in_progress_5m_excluded(self):
        # now=22:34:30 → 버킷 22:30 은 22:35 마감(미완성) → 완료 5분봉 없음
        now = datetime(2026, 8, 14, 22, 34, 30, tzinfo=UTC)
        ts, close = B.last_completed_5m(self.bars, now)
        self.assertIsNone(ts)


class ContextTest(unittest.TestCase):
    def test_context_summary(self):
        bars = [_bar(2026, 8, 14, 22, 30, 100.0), _bar(2026, 8, 14, 22, 31, 98.0)]
        now = datetime(2026, 8, 14, 22, 32, 5, tzinfo=UTC)
        ctx = B.completed_bar_context(bars, now)
        self.assertEqual(ctx["last_completed_1m_close"], 98.0)
        self.assertEqual(ctx["bar_count"], 2)

    def test_empty_bars(self):
        ctx = B.completed_bar_context([], datetime(2026, 8, 14, 22, 32, tzinfo=UTC))
        self.assertIsNone(ctx["last_completed_1m_bar_at"])
        self.assertIsNone(ctx["last_completed_5m_bar_at"])


if __name__ == "__main__":
    unittest.main()
