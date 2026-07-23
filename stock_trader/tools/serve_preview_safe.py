"""
serve_preview_safe.py — 기존 stock_trader 앱을 '안전하게' 미리보기로 실행하는 런처

주의: 이 파일은 새 앱/새 UI 가 아니다. 기존 app.py 의 Flask/SocketIO 객체를
      그대로 import 해서 서버만 띄운다(코드·UI·라우트 변경 없음).

안전장치:
  - LIVE_ORDER_ENABLED=false 강제 (실주문 게이트 OFF)
  - KIS 키를 프로세스 내에서 공란 처리 → 실 KIS API 호출을 원천 차단
    (기존 노출 키로 실계좌를 건드리지 않기 위함)
  - app.py 의 __main__ 블록을 실행하지 않으므로 _init_api()/_auto_start_bot() 미호출
    → 자동매매 봇이 켜지지 않는다.

용도:
  - UI/화면이 정상 표시되는지만 확인할 때. 실데이터(잔고/시세)는 키 공란이라 비어 있음.
  - 실데이터가 필요하면 로컬 PC 에서 정식으로 `python3 app.py`
    (LIVE_ORDER_ENABLED=false 유지)를 사용하고, 봇 시작 버튼은 누르지 말 것.

실행:
    PREVIEW_PORT=5000 python3 tools/serve_preview_safe.py
    → http://localhost:5000/demo  (키 없이 대시보드 UI 확인용 기존 라우트)
"""
import os

os.environ["LIVE_ORDER_ENABLED"] = "false"
os.environ["KIS_APP_KEY"] = ""
os.environ["KIS_APP_SECRET"] = ""
os.environ["KIS_ACCOUNT_NO"] = ""

import app  # 기존 앱 객체(app.app, app.socketio) — __main__ 미실행

port = int(os.environ.get("PREVIEW_PORT", "5000"))
print(f"[preview] 기존 app 서빙: 0.0.0.0:{port} "
      f"(bot OFF, keys blanked, LIVE_ORDER_ENABLED=false) → /demo 로 UI 확인")
app.socketio.run(app.app, host="0.0.0.0", port=port,
                 debug=False, allow_unsafe_werkzeug=True)
