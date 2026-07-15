#!/bin/bash
# ============================================================
#  주식 자동매매 봇 — 원클릭 자동 설치 스크립트
#  사용법: bash install.sh
# ============================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

INSTALL_DIR="/root/stock_trader"
SERVICE_FILE="/etc/systemd/system/stockbot.service"
LOG_FILE="/root/install_log.txt"
DOWNLOAD_URL="https://www.genspark.ai/api/files/s/xHmx56WG"

log()  { echo -e "${GREEN}[✓]${NC} $1" | tee -a "$LOG_FILE"; }
warn() { echo -e "${YELLOW}[!]${NC} $1" | tee -a "$LOG_FILE"; }
err()  { echo -e "${RED}[✗]${NC} $1" | tee -a "$LOG_FILE"; }
info() { echo -e "${CYAN}[→]${NC} $1" | tee -a "$LOG_FILE"; }

banner() {
  echo -e "${BOLD}${BLUE}"
  echo "============================================================"
  echo "   📈 주식 자동매매 봇 — 자동 설치 스크립트"
  echo "============================================================"
  echo -e "${NC}"
}

# ──────────────────────────────────────────────────────────────
# 1. 시스템 업데이트 & 패키지 설치
# ──────────────────────────────────────────────────────────────
step1_system() {
  echo -e "\n${BOLD}[1/6] 시스템 패키지 설치 중...${NC}"
  apt-get update -qq
  apt-get install -y -qq python3 python3-pip python3-venv wget curl git \
    build-essential libssl-dev libffi-dev python3-dev 2>&1 | tail -3
  log "시스템 패키지 설치 완료"
}

# ──────────────────────────────────────────────────────────────
# 2. 봇 코드 다운로드
# ──────────────────────────────────────────────────────────────
step2_download() {
  echo -e "\n${BOLD}[2/6] 봇 코드 다운로드 중...${NC}"
  cd /root
  info "다운로드 URL: $DOWNLOAD_URL"
  wget -q --show-progress -O stock_trader_latest.tar.gz "$DOWNLOAD_URL"
  log "다운로드 완료 ($(du -sh stock_trader_latest.tar.gz | cut -f1))"

  # 기존 설치가 있으면 .env 백업
  if [ -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env" /root/.env_backup
    warn "기존 .env 백업 완료: /root/.env_backup"
  fi

  # 기존 코드 제거 후 새로 설치
  rm -rf "$INSTALL_DIR"
  tar -xzf stock_trader_latest.tar.gz -C /root/
  log "코드 압축 해제 완료: $INSTALL_DIR"

  # .env 복원
  if [ -f /root/.env_backup ]; then
    cp /root/.env_backup "$INSTALL_DIR/.env"
    log ".env 복원 완료"
  fi
}

# ──────────────────────────────────────────────────────────────
# 3. Python 가상환경 & 라이브러리 설치
# ──────────────────────────────────────────────────────────────
step3_python() {
  echo -e "\n${BOLD}[3/6] Python 환경 설치 중...${NC}"
  cd "$INSTALL_DIR"

  python3 -m venv venv
  source venv/bin/activate

  info "pip 업그레이드..."
  pip install --upgrade pip -q

  info "의존성 패키지 설치 중... (5~10분 소요)"
  # tensorflow는 무거우므로 선택적으로 설치
  grep -v "tensorflow" requirements.txt > /tmp/req_light.txt
  pip install -r /tmp/req_light.txt -q 2>&1 | tail -5

  # tensorflow는 별도 (AI 기능 필요 시)
  pip install tensorflow -q 2>&1 | tail -2 || warn "tensorflow 설치 실패 (AI 기능 비활성화)"

  deactivate
  log "Python 환경 설치 완료"
}

# ──────────────────────────────────────────────────────────────
# 4. .env 설정
# ──────────────────────────────────────────────────────────────
step4_env() {
  echo -e "\n${BOLD}[4/6] API 설정 입력${NC}"

  # .env가 이미 있으면 건너뜀
  if [ -f "$INSTALL_DIR/.env" ]; then
    log ".env 파일 이미 존재 — 기존 설정 유지"
    echo -e "${CYAN}현재 설정:${NC}"
    grep -v "SECRET\|KEY\|TOKEN" "$INSTALL_DIR/.env" || true
    echo ""
    read -p "설정을 다시 입력하시겠습니까? (y/N): " redo
    if [[ "$redo" != "y" && "$redo" != "Y" ]]; then
      return
    fi
  fi

  echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}한국투자증권 KIS API 설정${NC}"
  echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  read -p "KIS_APP_KEY       : " KIS_KEY
  read -p "KIS_APP_SECRET    : " KIS_SECRET
  read -p "KIS_ACCOUNT_NO    : " KIS_ACCOUNT
  echo ""
  echo -e "${BOLD}투자 모드 선택${NC}"
  echo "  1) 실전투자 (실제 돈)"
  echo "  2) 모의투자 (연습)"
  read -p "선택 (1/2): " mode_choice
  if [[ "$mode_choice" == "1" ]]; then
    KIS_IS_REAL="true"
    warn "⚠️  실전투자 모드 선택됨 — 실제 매매가 이루어집니다!"
  else
    KIS_IS_REAL="false"
    log "모의투자 모드 선택"
  fi

  echo ""
  echo -e "${BOLD}텔레그램 알림 설정 (선택사항, 엔터 건너뛰기)${NC}"
  read -p "TELEGRAM_BOT_TOKEN: " TG_TOKEN
  read -p "TELEGRAM_CHAT_ID  : " TG_CHAT

  echo ""
  echo -e "${BOLD}매매 기본 설정${NC}"
  read -p "종목당 최대 투자금 (기본 1000000원): " MAX_PER
  read -p "총 최대 투자금   (기본 5000000원): " MAX_TOTAL
  read -p "손절 기준 %      (기본 10): " STOP_LOSS
  read -p "익절 기준 %      (기본 30): " TAKE_PROFIT

  MAX_PER=${MAX_PER:-1000000}
  MAX_TOTAL=${MAX_TOTAL:-5000000}
  STOP_LOSS=${STOP_LOSS:-10}
  TAKE_PROFIT=${TAKE_PROFIT:-30}

  FLASK_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

  cat > "$INSTALL_DIR/.env" << EOF
KIS_APP_KEY=${KIS_KEY}
KIS_APP_SECRET=${KIS_SECRET}
KIS_ACCOUNT_NO=${KIS_ACCOUNT}
KIS_IS_REAL=${KIS_IS_REAL}
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT}
MAX_INVESTMENT_PER_STOCK=${MAX_PER}
MAX_TOTAL_INVESTMENT=${MAX_TOTAL}
STOP_LOSS_PERCENT=${STOP_LOSS}
TAKE_PROFIT_PERCENT=${TAKE_PROFIT}
FLASK_SECRET_KEY=${FLASK_KEY}
EOF

  log ".env 설정 완료"
}

# ──────────────────────────────────────────────────────────────
# 5. systemd 서비스 등록 (재부팅해도 자동 시작)
# ──────────────────────────────────────────────────────────────
step5_service() {
  echo -e "\n${BOLD}[5/6] 시스템 서비스 등록 중...${NC}"

  cat > "$SERVICE_FILE" << EOF
[Unit]
Description=주식 자동매매 봇
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python app.py
Restart=always
RestartSec=10
StandardOutput=append:${INSTALL_DIR}/logs/server.log
StandardError=append:${INSTALL_DIR}/logs/server.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

  mkdir -p "$INSTALL_DIR/logs"
  systemctl daemon-reload
  systemctl enable stockbot
  log "systemd 서비스 등록 완료 (재부팅 시 자동 시작)"
}

# ──────────────────────────────────────────────────────────────
# 6. 방화벽 설정 & 서비스 시작
# ──────────────────────────────────────────────────────────────
step6_start() {
  echo -e "\n${BOLD}[6/6] 서비스 시작 중...${NC}"

  # UFW 방화벽 포트 열기 (있는 경우)
  if command -v ufw &>/dev/null; then
    ufw allow 5000/tcp 2>/dev/null || true
    log "방화벽 포트 5000 오픈"
  fi

  # 기존 프로세스 종료
  pkill -f "python.*app.py" 2>/dev/null || true
  sleep 2

  # 서비스 시작
  systemctl start stockbot
  sleep 3

  if systemctl is-active --quiet stockbot; then
    log "봇 서비스 정상 시작!"
  else
    warn "systemd 시작 실패 — 직접 실행 시도"
    cd "$INSTALL_DIR"
    nohup "$INSTALL_DIR/venv/bin/python" app.py >> logs/server.log 2>&1 &
    sleep 3
    if pgrep -f "python.*app.py" > /dev/null; then
      log "봇 직접 실행 성공"
    else
      err "봇 시작 실패 — 로그 확인: tail -f $INSTALL_DIR/logs/server.log"
    fi
  fi
}

# ──────────────────────────────────────────────────────────────
# 완료 메시지
# ──────────────────────────────────────────────────────────────
finish() {
  # 공인 IP 확인
  PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "IP확인불가")

  echo ""
  echo -e "${BOLD}${GREEN}"
  echo "============================================================"
  echo "   ✅ 설치 완료!"
  echo "============================================================"
  echo -e "${NC}"
  echo -e "  🌐 대시보드:  ${BOLD}http://${PUBLIC_IP}:5000${NC}"
  echo -e "  📋 로그 보기: ${CYAN}tail -f $INSTALL_DIR/logs/server.log${NC}"
  echo -e "  🔄 봇 재시작: ${CYAN}systemctl restart stockbot${NC}"
  echo -e "  ⏹  봇 중지:   ${CYAN}systemctl stop stockbot${NC}"
  echo -e "  📊 상태 확인: ${CYAN}systemctl status stockbot${NC}"
  echo ""
  echo -e "  ${YELLOW}⚠️  Naver Cloud 보안 그룹에서 TCP 5000 포트를 열어야 접속됩니다!${NC}"
  echo ""

  # 서비스 상태 표시
  systemctl status stockbot --no-pager -l 2>/dev/null | head -10 || true
}

# ──────────────────────────────────────────────────────────────
# 메인 실행
# ──────────────────────────────────────────────────────────────
main() {
  banner
  echo "설치 로그: $LOG_FILE"
  echo ""

  # root 권한 확인
  if [ "$EUID" -ne 0 ]; then
    err "root 권한이 필요합니다. 'sudo bash install.sh' 로 실행하세요."
    exit 1
  fi

  step1_system
  step2_download
  step3_python
  step4_env
  step5_service
  step6_start
  finish
}

main "$@"
