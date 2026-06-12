FROM python:3.11-slim

WORKDIR /app

# 시스템 패키지
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl tzdata \
    && ln -sf /usr/share/zoneinfo/Asia/Seoul /etc/localtime \
    && echo "Asia/Seoul" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# requirements 먼저 복사 (레이어 캐시 활용)
COPY requirements.txt .

# tensorflow 제외 버전으로 설치 (서버 경량화 — 백테스트 불필요 시)
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY . .

# 데이터 디렉토리 생성
RUN mkdir -p data logs

# 포트
EXPOSE 5000

# 실행
CMD ["python", "app.py"]
