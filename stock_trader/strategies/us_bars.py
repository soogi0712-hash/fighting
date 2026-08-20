"""US 인트라데이 봉 정규화 — '실제 마감 완료 봉' 판정(순수 로직·부수효과 없음).

목적:
  wall-clock now.floor() 로 만든 가짜 봉 타임스탬프를 폐기하고, **실제 OHLCV 봉의
  timestamp/close** 로부터 '마지막으로 마감 완료된' 1분봉·5분봉을 결정론적으로 뽑는다.
  진행 중(미완성)인 현재 봉은 제외한다.

timezone:
  서버 KST·미국 ET·데이터 UTC 가 섞여도 **동일 실제 봉을 중복 계산하지 않도록**,
  모든 타임스탬프를 **UTC tz-aware** 로 정규화한다. 봉의 정체성(dedup 키)은 UTC 순간이다.
  naive datetime 은 UTC 로 간주한다(호출부가 tz 를 부여하는 것이 원칙).

'마감 완료' 정의:
  · 1분봉: 봉 시작 ts 의 구간 [ts, ts+1m). now(UTC) >= ts+1m 이면 완료.
  · 5분봉: 1분봉을 5분 버킷(floor 5m)으로 묶어, 버킷 [start, start+5m) 이 now 이하로
           끝났으면 완료. 5분봉 종가 = 그 버킷의 '가장 늦은 1분봉' 종가.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def to_utc(ts: datetime) -> datetime:
    """datetime 을 UTC tz-aware 로 정규화. naive 는 UTC 로 간주."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def normalize_bars(raw_bars) -> list:
    """[{"ts": datetime, "close": float}, ...] → 오름차순 [(utc_ts, close), ...].

    · UTC 로 정규화(다른 tz 표현이라도 같은 순간이면 동일 봉으로 병합).
    · 동일 UTC 타임스탬프는 **마지막 값이 우선**(늦게 수정된 종가로 안전 교정).
    · ts/close 결측 항목은 스킵.
    """
    merged: dict = {}
    for b in (raw_bars or []):
        try:
            ts = b.get("ts")
            close = b.get("close")
        except AttributeError:
            continue
        if ts is None or close is None:
            continue
        try:
            u = to_utc(ts)
            c = float(close)
        except (TypeError, ValueError):
            continue
        merged[u] = c   # 마지막 값 우선(late correction)
    return sorted(merged.items(), key=lambda kv: kv[0])


def last_completed_1m(bars: list, now: datetime) -> tuple:
    """마지막 '마감 완료' 1분봉 (utc_iso, close). 없으면 (None, None). 진행중봉 제외."""
    nowu = to_utc(now)
    res = (None, None)
    for ts, close in bars:
        if ts + timedelta(minutes=1) <= nowu:
            res = (ts.isoformat(), close)
        # bars 오름차순 → 완료된 마지막 것이 최종 res
    return res


def _floor5(ts: datetime) -> datetime:
    return ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)


def last_completed_5m(bars: list, now: datetime) -> tuple:
    """마지막 '마감 완료' 5분봉 (utc_iso, close). 없으면 (None, None). 진행중 버킷 제외.

    5분봉 종가 = 해당 5분 버킷에서 가장 늦은 1분봉의 종가.
    """
    nowu = to_utc(now)
    groups: dict = {}   # bucket_start -> (latest_ts, close)
    for ts, close in bars:
        start = _floor5(ts)
        prev = groups.get(start)
        if prev is None or ts >= prev[0]:
            groups[start] = (ts, close)
    res = (None, None)
    for start in sorted(groups):
        if start + timedelta(minutes=5) <= nowu:
            res = (start.isoformat(), groups[start][1])
    return res


def completed_bar_context(raw_bars, now: datetime) -> dict:
    """호출부용 요약: 정규화 + 완료 1m/5m 추출.

    반환: {last_completed_1m_bar_at, last_completed_1m_close,
           last_completed_5m_bar_at, last_completed_5m_close, bar_count}
    """
    bars = normalize_bars(raw_bars)
    b1_ts, b1_close = last_completed_1m(bars, now)
    b5_ts, b5_close = last_completed_5m(bars, now)
    return {
        "last_completed_1m_bar_at": b1_ts,
        "last_completed_1m_close":  b1_close,
        "last_completed_5m_bar_at": b5_ts,
        "last_completed_5m_close":  b5_close,
        "bar_count": len(bars),
    }
