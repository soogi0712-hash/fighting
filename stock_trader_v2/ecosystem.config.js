/**
 * PM2 ecosystem 설정 — KIS 단타봇 V2
 * =====================================
 * 실행: cd stock_trader_v2 && pm2 start ecosystem.config.js
 * 중지: pm2 stop v2-live
 * 재시작: pm2 restart v2-live
 * 로그: pm2 logs v2-live
 * 상태: pm2 status
 */

const path = require("path");

module.exports = {
  apps: [
    {
      // ── V2 LIVE 메인 봇 ───────────────────────────────────
      name: "v2-live",
      script: path.join(__dirname, "main.py"),
      interpreter: "python3",
      cwd: __dirname,

      // 자동 재시작 비활성화 — 수동 제어
      autorestart: false,
      watch: false,
      max_restarts: 0,

      // 환경변수: .env 파일을 dotenv로 로드하므로 추가 설정 최소화
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: __dirname,
      },

      // 로그 설정
      // ★ PM2 앞 타임스탬프(log_date_format)는 서버 OS 기준 UTC로 찍힘
      //   로그 본문 [asctime KST]는 v2_logger.py의 _KSTFormatter가 KST로 변환
      //   → "2026-06-18 00:32:05:" (PM2, UTC) + "[2026-06-18 00:32:05 KST]" (Python, KST)
      //   → KST 자정대에는 PM2 날짜(UTC 17일)와 Python 날짜(KST 18일)가 다를 수 있음
      output: path.join(__dirname, "logs", "v2_live_out.log"),
      error:  path.join(__dirname, "logs", "v2_live_err.log"),
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss [UTC]",

      // 단일 인스턴스 강제 (클러스터 모드 사용 안 함)
      instances: 1,
      exec_mode: "fork",

      // 종료 대기 (포지션 정리 + lock 삭제 시간)
      kill_timeout: 15000,

      // 시작 시 최소 실행 시간 (1초 미만 즉시 종료는 오류로 간주)
      min_uptime: "1s",
    },
  ],
};
