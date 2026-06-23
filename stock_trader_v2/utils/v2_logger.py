"""utils/v2_logger.py — V2 전용 로거 (기존 logger.py 와 독립)

★ 타임스탬프 정책
  - 서버 OS timezone : UTC (Etc/UTC)
  - 로그 출력 timezone: KST (Asia/Seoul, UTC+9)
  - 변환 방법: KSTFormatter.formatTime() 오버라이드
  - PM2 log_date_format 앞 타임스탬프도 UTC이므로
    로그 본문 [YYYY-MM-DD HH:MM:SS KST] 로 명확히 구분
"""
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

_LOG_DIR  = os.path.join(os.path.dirname(__file__), "..", "logs")
_handlers: dict = {}

# KST = UTC+9
_KST = timezone(timedelta(hours=9))


class _KSTFormatter(logging.Formatter):
    """모든 로그 레코드의 타임스탬프를 KST(UTC+9)로 출력하는 Formatter."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # type: ignore[override]
        # record.created 는 UTC epoch(float)
        dt_kst = datetime.fromtimestamp(record.created, tz=_KST)
        if datefmt:
            return dt_kst.strftime(datefmt)
        return dt_kst.strftime("%Y-%m-%d %H:%M:%S")


def get_logger(name: str) -> logging.Logger:
    if name in _handlers:
        return logging.getLogger(f"v2.{name}")

    os.makedirs(_LOG_DIR, exist_ok=True)
    logger = logging.getLogger(f"v2.{name}")
    logger.setLevel(logging.DEBUG)

    # ★ KSTFormatter — asctime이 KST로 출력됨
    fmt = _KSTFormatter(
        "[%(asctime)s KST] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 콘솔
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 파일
    log_path = os.path.join(_LOG_DIR, "v2_server.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.propagate = False
    _handlers[name]  = True
    return logger
