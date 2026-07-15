"""
_test_kr_order_time.py — KR 주문가능 시간 판정 테스트 스크립트
==============================================================
실행: cd stock_trader_v2 && python3 _test_kr_order_time.py

확인 항목:
  1. 서버 OS timezone (UTC 여부)
  2. datetime.now(KST) 정확도
  3. KR 주문가능 시간 판정 (09:00~14:30 KST)
  4. 각 시간대별 예상 판정 결과 (시뮬레이션)
  5. 로그 타임스탬프가 KST로 찍히는지 확인
"""

import sys
import os
import time
from datetime import datetime, timezone, timedelta, time as dtime

import pytz

# ── 경로 설정 ──
sys.path.insert(0, os.path.dirname(__file__))

KST     = pytz.timezone("Asia/Seoul")
_KST_TZ = timezone(timedelta(hours=9))

BUY_OPEN_TIME  = dtime(9,  0)
BUY_CLOSE_TIME = dtime(14, 30)
BUY_HARD_STOP  = dtime(15, 20)

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def check_order_window(kst_time: dtime, weekday: int) -> str:
    """KR 주문가능 시간 판정 — kr_broker.py _validate_order() V4와 동일 로직"""
    if weekday >= 5:
        return "❌ 주말 — 매수 불가"
    if BUY_OPEN_TIME <= kst_time <= BUY_CLOSE_TIME:
        return "✅ 매수 허용 (09:00~14:30)"
    return f"❌ 매수 불가 시간 ({kst_time.strftime('%H:%M')}) — 허용=09:00~14:30"


def print_section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ────────────────────────────────────────────────────────────
# 1. 서버 timezone 확인
# ────────────────────────────────────────────────────────────
print_section("1. 서버 OS Timezone 확인")

try:
    with open("/etc/timezone") as f:
        os_tz = f.read().strip()
except Exception:
    os_tz = "읽기 실패"

utc_offset_sec = -time.timezone  # 서머타임 미적용
utc_offset_h   = utc_offset_sec // 3600

print(f"  /etc/timezone   : {os_tz}")
print(f"  time.timezone   : UTC{utc_offset_h:+d}h  (음수=서쪽)")
print(f"  → 서버 OS는 {'UTC' if os_tz in ('Etc/UTC', 'UTC') else os_tz} 기준")


# ────────────────────────────────────────────────────────────
# 2. 현재 시각 — UTC vs KST 비교
# ────────────────────────────────────────────────────────────
print_section("2. 현재 시각 (UTC vs KST)")

now_kst_pytz   = datetime.now(KST)               # pytz KST
now_kst_stdlib = datetime.now(_KST_TZ)           # stdlib KST (Python 3.9+)
now_naive      = datetime.now()                   # naive (= OS local = UTC)

print(f"  datetime.now()           = {now_naive.strftime('%Y-%m-%d %H:%M:%S')}  ← naive=OS local(UTC)")
print(f"  datetime.now(pytz KST)   = {now_kst_pytz.strftime('%Y-%m-%d %H:%M:%S %Z')}")
print(f"  datetime.now(stdlib KST) = {now_kst_stdlib.strftime('%Y-%m-%d %H:%M:%S %Z')}")
print()
print(f"  .time() naive  = {now_naive.time().strftime('%H:%M:%S')}  ← UTC")
print(f"  .time() KST    = {now_kst_pytz.time().strftime('%H:%M:%S')}  ← KST (+9h)")
print()
print(f"  ★ kr_broker.py는 datetime.now(KST).time() 사용 → KST 기준 올바름")

# pytz와 stdlib 결과 일치 확인
delta_sec = abs((now_kst_pytz - now_kst_stdlib).total_seconds())
if delta_sec < 1.0:
    print(f"  ✅ pytz KST = stdlib KST (차이 {delta_sec:.3f}초 — 정상)")
else:
    print(f"  ⚠️  pytz KST vs stdlib KST 차이 {delta_sec:.3f}초 — 확인 필요")


# ────────────────────────────────────────────────────────────
# 3. 현재 KR 주문가능 시간 판정
# ────────────────────────────────────────────────────────────
print_section("3. 현재 KR 주문가능 시간 판정")

t_kst    = now_kst_pytz.time()
weekday  = now_kst_pytz.weekday()

print(f"  현재 시각(KST)  = {now_kst_pytz.strftime('%Y-%m-%d %H:%M:%S')} ({WEEKDAYS[weekday]})")
print(f"  판정 시각(t)    = {t_kst.strftime('%H:%M:%S')}")
print(f"  요일(weekday)   = {weekday} ({WEEKDAYS[weekday]}) → {'주말' if weekday >= 5 else '평일'}")
print()
result = check_order_window(t_kst, weekday)
print(f"  → 판정 결과: {result}")


# ────────────────────────────────────────────────────────────
# 4. 시간대별 시뮬레이션
# ────────────────────────────────────────────────────────────
print_section("4. 시간대별 판정 시뮬레이션 (평일 기준)")

sim_cases = [
    (dtime(8, 59),  "장 시작 1분 전"),
    (dtime(9,  0),  "장 시작 (OPEN)"),
    (dtime(9,  1),  "장 시작 1분 후"),
    (dtime(12, 0),  "점심 (허용)"),
    (dtime(14, 29), "마감 1분 전 (허용)"),
    (dtime(14, 30), "마감 정각 (허용)"),
    (dtime(14, 31), "마감 1분 후 (차단)"),
    (dtime(15, 20), "BUY_HARD_STOP"),
    (dtime(0, 32),  "현재 KST 시각대 (자정)"),
    (dtime(23, 59), "KST 23:59"),
]

for t_sim, label in sim_cases:
    r = check_order_window(t_sim, 3)   # 3=목요일(평일)
    print(f"  {t_sim.strftime('%H:%M')}  {label:25s}  →  {r}")


# ────────────────────────────────────────────────────────────
# 5. 로그 타임스탬프 KST 출력 확인
# ────────────────────────────────────────────────────────────
print_section("5. v2_logger 타임스탬프 KST 출력 확인")

try:
    from utils.v2_logger import get_logger
    test_logger = get_logger("TimeTest")

    test_logger.info(
        f"[TIME_CHECK] 이 로그의 타임스탬프가 KST인지 확인 | "
        f"UTC={now_naive.strftime('%H:%M:%S')} | KST={now_kst_pytz.strftime('%H:%M:%S')} | "
        f"기대값: [{now_kst_pytz.strftime('%Y-%m-%d %H:%M:%S')} KST]"
    )
    print(f"  ✅ 위 로그의 [asctime KST] 부분이 KST {now_kst_pytz.strftime('%H:%M:%S')}와 일치하면 정상")
    print(f"  ★ 로그 파일: logs/v2_server.log 마지막 줄 확인")
except Exception as e:
    print(f"  ⚠️  v2_logger import 실패: {e}")


# ────────────────────────────────────────────────────────────
# 6. 내일 장 시작 예상 판정
# ────────────────────────────────────────────────────────────
print_section("6. 내일 장 시작(09:00 KST) 예상 판정")

from datetime import date, timedelta as td
tomorrow_kst  = now_kst_pytz.date() + td(days=1)
tomorrow_open = datetime.combine(tomorrow_kst, dtime(9, 0)).replace(tzinfo=_KST_TZ)
tomorrow_wd   = tomorrow_open.weekday()

print(f"  내일(KST) 09:00 = {tomorrow_open.strftime('%Y-%m-%d %H:%M')} ({WEEKDAYS[tomorrow_wd]})")
r_open = check_order_window(dtime(9, 0), tomorrow_wd)
r_close = check_order_window(dtime(14, 30), tomorrow_wd)
print(f"  09:00 판정       → {r_open}")
print(f"  14:30 판정       → {r_close}")
print(f"  14:31 판정       → {check_order_window(dtime(14, 31), tomorrow_wd)}")

# UTC 기준으로 환산 (서버 로그와 비교용)
tomorrow_open_utc = tomorrow_open.astimezone(timezone.utc)
print()
print(f"  서버(UTC) 09:00 KST = {tomorrow_open_utc.strftime('%Y-%m-%d %H:%M UTC')}")
print(f"  → PM2 로그에 '{tomorrow_open_utc.strftime('%H:%M')} UTC'로 찍히면 정상")

print()
print("="*60)
print("  테스트 완료")
print("="*60)
