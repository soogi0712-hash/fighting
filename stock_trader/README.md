# 📈 KIS 자동매매 시스템

한국투자증권(KIS) Open API 기반 주식 자동매매 시스템  
Flask + SocketIO 실시간 대시보드 | 피라미딩 전략 | AI 스크리닝

---

## 🚀 빠른 시작 (Docker — 추천)

### 1단계 — 파일 업로드
서버에 `stock-trader.tar.gz` 업로드 후:
```bash
tar -xzf stock-trader.tar.gz
cd stock_trader
```

### 2단계 — KIS API 키 설정
```bash
cp .env.example .env
nano .env   # 또는 vi .env
```

**.env 필수 항목:**
```
KIS_APP_KEY=발급받은_앱키
KIS_APP_SECRET=발급받은_시크릿
KIS_ACCOUNT_NO=계좌번호-01        # 예: 73180640-01
KIS_MODE=paper                     # paper: 모의투자(기본), real: 실전투자
LIVE_ORDER_ENABLED=false           # true: 실주문 허용, false: dry-run(주문 없음)
MAX_INVESTMENT_PER_STOCK=1000000   # 종목당 최대 100만원
MAX_TOTAL_INVESTMENT=5000000       # 전체 최대 500만원
```

> **⚠️ 주의**: 처음 설정 시 `KIS_MODE=paper` + `LIVE_ORDER_ENABLED=false` 로 시작해  
> 모의투자 환경에서 동작을 확인한 뒤, 실전전환 시 `KIS_MODE=real` + `LIVE_ORDER_ENABLED=true` 로 변경하세요.

### 3단계 — 원클릭 배포
```bash
bash deploy.sh
```

배포 완료 후 브라우저에서:
```
http://서버IP:5000
```

---

## ☁️ 클라우드 서버 추천

| 서비스 | 비용 | 추천 사양 |
|--------|------|-----------|
| **Oracle Cloud 무료** | **완전 무료** | ARM 4코어 24GB |
| AWS Lightsail | 월 ~5달러 | 1GB RAM |
| Vultr | 월 ~6달러 | 1GB RAM |
| 네이버 클라우드 | 월 ~1만원 | 2GB RAM |

> **Oracle Cloud 무료 티어 추천** — 무기한 무료, 성능도 충분

---

## 🖥️ Oracle Cloud 무료 서버 세팅 (처음부터)

```bash
# 1. 서버 접속 후 Docker 설치
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
newgrp docker

# 2. 파일 전송 (로컬 → 서버)
scp stock-trader.tar.gz ubuntu@서버IP:~/

# 3. 서버에서 실행
ssh ubuntu@서버IP
tar -xzf stock-trader.tar.gz
cd stock_trader
nano .env           # API 키 입력
bash deploy.sh      # 자동 배포
```

**방화벽 포트 열기 (Oracle Cloud 콘솔):**
- VCN → 보안 목록 → 수신 규칙 추가 → 포트 5000 허용

---

## 📦 Docker 없이 직접 실행 (Python)

```bash
# Python 3.11 필요
pip install -r requirements.txt
cp .env.example .env
nano .env

# 백그라운드 실행
nohup python app.py > logs/server.log 2>&1 &

# 로그 확인
tail -f logs/server.log
```

---

## ⚙️ 주요 기능

| 기능 | 설명 |
|------|------|
| **AI 스크리닝** | 매일 16:05 전체 종목 자동 분석, BUY_CANDIDATE 선별 |
| **피라미딩 전략** | 1~4단계 추가매수, 실질수익률 기반 관리 |
| **6개 지표 검증** | MA, RSI, MACD, BB, ATR, OBV 복합 신호 |
| **실시간 대시보드** | 보유종목, 포지션, 신호, 로그 실시간 표시 |
| **포지션 자동복원** | 서버 재시작 시 실제 잔고 기반 자동 동기화 |
| **텔레그램 알림** | 매수/매도 발생 시 즉시 알림 (선택) |

---

## 📁 디렉토리 구조

```
stock_trader/
├── app.py                  # 메인 Flask 앱
├── config.py               # 환경변수 설정
├── api/
│   └── kis_api.py          # KIS Open API 연동
├── strategies/
│   ├── strategy_manager.py # 통합 전략 매니저
│   ├── pyramid_strategy.py # 피라미딩 전략
│   └── indicator_validator.py # 6개 지표 검증
├── screener/
│   ├── daily_screener.py   # 일일 AI 스크리닝
│   └── ai_scorer.py        # AI 종목 점수화
├── templates/
│   └── dashboard.html      # 실시간 대시보드
├── data/                   # 포지션/상태 저장 (영속)
├── logs/                   # 로그 파일
├── .env.example            # 환경변수 템플릿
├── Dockerfile
├── docker-compose.yml
└── deploy.sh               # 원클릭 배포
```

---

## 🔧 운영 명령어

```bash
# 상태 확인
docker compose ps

# 실시간 로그
docker compose logs -f

# 재시작
docker compose restart

# 중지
docker compose down

# 업데이트 후 재배포
docker compose up -d --build
```

---

## ⚠️ 주의사항

- **실전투자** 시 반드시 소액으로 먼저 테스트
- KIS API 토큰은 1분에 1회 발급 제한 있음
- 서버 시간이 **KST(한국시간)** 으로 설정되어야 정확한 장 시간 판단 가능
- `data/` 디렉토리는 삭제하지 마세요 (포지션/복리풀 데이터 보존)
