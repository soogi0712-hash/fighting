#!/bin/bash
# pm2_watchdog.sh — PM2 v2-live 프로세스 생존 감시 + 자동 재시작
# 크론탭 등록: */5 * * * * /home/user/webapp/stock_trader_v2/scripts/pm2_watchdog.sh >> /home/user/webapp/stock_trader_v2/logs/watchdog.log 2>&1
#
# 목적:
#   - 서버 재부팅/OOM 등으로 PM2가 내려갔을 때 자동 복구
#   - KR 장 시간(UTC 00:00~06:30) 다운 방지 → 06-27 거래 0건 재발 방지
#
# [버그] 06-27: PM2가 UTC 23:10(06-26) ~ 10:34(06-27) 동안 다운
#          → KR 장(UTC 00:00~06:30) 전체 누락

LOGFILE="/home/user/webapp/stock_trader_v2/logs/watchdog.log"
TS=$(date '+%Y-%m-%d %H:%M:%S KST')

# PM2가 실행중인지 확인
if ! command -v pm2 &>/dev/null; then
    echo "[$TS] [WATCHDOG] pm2 명령 없음 — 건너뜀" >> "$LOGFILE"
    exit 0
fi

STATUS=$(pm2 jlist 2>/dev/null | python3 -c "
import json, sys
try:
    procs = json.load(sys.stdin)
    for p in procs:
        if p.get('name') == 'v2-live':
            print(p.get('pm2_env', {}).get('status', 'unknown'))
            sys.exit(0)
    print('not_found')
except:
    print('error')
" 2>/dev/null)

echo "[$TS] [WATCHDOG] v2-live 상태=$STATUS" >> "$LOGFILE"

if [[ "$STATUS" != "online" ]]; then
    echo "[$TS] [WATCHDOG] ⚠️  v2-live DOWN($STATUS) → pm2 restart 시도" >> "$LOGFILE"
    pm2 restart v2-live >> "$LOGFILE" 2>&1
    sleep 5
    NEW_STATUS=$(pm2 jlist 2>/dev/null | python3 -c "
import json, sys
try:
    procs = json.load(sys.stdin)
    for p in procs:
        if p.get('name') == 'v2-live':
            print(p.get('pm2_env', {}).get('status', 'unknown'))
            sys.exit(0)
    print('not_found')
except:
    print('error')
" 2>/dev/null)
    echo "[$TS] [WATCHDOG] 재시작 후 상태=$NEW_STATUS" >> "$LOGFILE"
else
    echo "[$TS] [WATCHDOG] ✅ v2-live 정상 운영 중" >> "$LOGFILE"
fi
