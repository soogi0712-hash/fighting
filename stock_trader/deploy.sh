#!/bin/bash
# ============================================================
#  KIS 자동매매 시스템 — 원클릭 배포 스크립트
#  사용법: bash deploy.sh
# ============================================================
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}"
echo "=================================================="
echo "   KIS 자동매매 시스템 — 배포 시작"
echo "=================================================="
echo -e "${NC}"

# ── 1. Docker 설치 확인 ───────────────────────────────────
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}Docker가 없습니다. 자동 설치합니다...${NC}"
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo -e "${GREEN}Docker 설치 완료${NC}"
fi

if ! command -v docker compose &> /dev/null && ! docker compose version &> /dev/null 2>&1; then
    echo -e "${YELLOW}Docker Compose 설치 중...${NC}"
    sudo apt-get install -y docker-compose-plugin 2>/dev/null || \
    sudo pip install docker-compose
fi

# ── 2. .env 파일 확인 ─────────────────────────────────────
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}.env 파일이 없습니다. .env.example을 복사합니다.${NC}"
    cp .env.example .env
    echo -e "${RED}"
    echo "======================================================"
    echo "  ⚠️  .env 파일을 반드시 수정하세요!"
    echo "  vi .env  또는  nano .env"
    echo ""
    echo "  필수 항목:"
    echo "    KIS_APP_KEY=발급받은_앱키"
    echo "    KIS_APP_SECRET=발급받은_시크릿"
    echo "    KIS_ACCOUNT_NO=계좌번호-01"
    echo "    KIS_MODE=paper          # paper: 모의투자(기본), real: 실전투자"
    echo "    LIVE_ORDER_ENABLED=false # true: 실주문 허용, false: dry-run"
    echo "======================================================"
    echo -e "${NC}"
    echo "수정 후 다시 deploy.sh를 실행하세요."
    exit 1
fi

# .env에 필수값 채워져있는지 확인
if grep -q "your_app_key_here" .env; then
    echo -e "${RED}❌ .env 파일의 KIS_APP_KEY를 실제 값으로 수정하세요!${NC}"
    exit 1
fi

# ── 3. 데이터/로그 디렉토리 생성 ──────────────────────────
mkdir -p data logs
echo -e "${GREEN}✅ 디렉토리 생성 완료${NC}"

# ── 4. 기존 컨테이너 중지 ─────────────────────────────────
echo -e "${YELLOW}기존 컨테이너 중지 중...${NC}"
docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true

# ── 5. 이미지 빌드 & 컨테이너 시작 ───────────────────────
echo -e "${YELLOW}이미지 빌드 중... (최초 실행 시 5~10분 소요)${NC}"
docker compose up -d --build 2>/dev/null || docker-compose up -d --build

# ── 6. 헬스체크 ───────────────────────────────────────────
echo -e "${YELLOW}서버 시작 대기 중...${NC}"
for i in {1..12}; do
    sleep 5
    if curl -sf http://localhost:5000/api/status > /dev/null 2>&1; then
        echo -e "${GREEN}"
        echo "=================================================="
        echo "  ✅ 배포 완료!"
        echo ""
        echo "  대시보드: http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_SERVER_IP'):5000"
        echo "  로그 확인: docker compose logs -f"
        echo "  중지:      docker compose down"
        echo "  재시작:    docker compose restart"
        echo "=================================================="
        echo -e "${NC}"
        exit 0
    fi
    echo "  대기 중... ($((i*5))초)"
done

echo -e "${RED}❌ 서버 시작 실패. 로그를 확인하세요:${NC}"
docker compose logs --tail=50
exit 1
