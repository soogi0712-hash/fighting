"""
utils/log_archiver.py — 로그 자동 아카이브 / 정리 (V2)
======================================================
★ 역할:
  - 오늘 로그 : 실시간 표시 (유지)
  - 전일 로그 : logs/archive/YYYYMMDD/ 폴더로 이동
  - 24시간 초과 로그 : 메인 logs/ 에서 제거 (archive에 보존)
  - trade_history.db / v2_trade_history.json : 절대 삭제 안 함

★ 호출 방법:
  archiver = LogArchiver()
  archiver.run()     # 메인 루프에서 하루 1회 (00:01~00:05 사이)
"""

import os
import shutil
import gzip
from datetime import datetime, date, timedelta

from utils.v2_logger import get_logger

logger = get_logger("LogArchiver")

_LOG_DIR     = os.path.join(os.path.dirname(__file__), "..", "logs")
_ARCHIVE_DIR = os.path.join(_LOG_DIR, "archive")

# 아카이브 대상 확장자 (학습 DB는 제외)
_ARCHIVABLE_EXTS = {".log", ".out", ".err"}

# 절대 삭제/아카이브 하지 않는 파일 (패턴)
_NEVER_DELETE = {
    "trade_history.db",
    "v2_trade_history.json",
    "v2_trade_log.json",
    "v2_us_positions.json",
    "v2_kr_positions.json",
    "v2_account_state.json",
    "v2_live.lock",
}


class LogArchiver:
    """
    로그 자동 아카이브 / 정리 유틸리티.

    사용법:
        archiver = LogArchiver()
        archiver.run()     # 00:01~00:05에 1회 호출
    """

    def __init__(self,
                 log_dir:     str = _LOG_DIR,
                 archive_dir: str = _ARCHIVE_DIR):
        self.log_dir     = log_dir
        self.archive_dir = archive_dir
        os.makedirs(self.archive_dir, exist_ok=True)

    def run(self) -> dict:
        """
        아카이브 실행.
        Returns: {"archived": n, "cleaned": n}
        """
        today = date.today()
        result = {"archived": 0, "cleaned": 0}

        try:
            for fname in os.listdir(self.log_dir):
                fpath = os.path.join(self.log_dir, fname)

                # 디렉토리는 건너뜀
                if os.path.isdir(fpath):
                    continue

                # 절대 보존 파일
                if fname in _NEVER_DELETE:
                    continue

                # 확장자 체크
                _, ext = os.path.splitext(fname)
                if ext not in _ARCHIVABLE_EXTS:
                    continue

                # 파일 수정 시각 확인
                try:
                    mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                    file_date = mtime.date()
                except OSError:
                    continue

                age_days = (today - file_date).days

                if age_days == 0:
                    # 오늘 파일 → 유지
                    continue
                elif age_days == 1:
                    # 전일 파일 → 아카이브 (gzip 압축)
                    arch_subdir = os.path.join(self.archive_dir,
                                               file_date.strftime("%Y%m%d"))
                    os.makedirs(arch_subdir, exist_ok=True)
                    dest = os.path.join(arch_subdir, fname + ".gz")
                    if not os.path.exists(dest):
                        try:
                            with open(fpath, "rb") as fin, gzip.open(dest, "wb") as fout:
                                shutil.copyfileobj(fin, fout)
                            os.remove(fpath)
                            result["archived"] += 1
                            logger.info(
                                f"[LogArchiver] 아카이브: {fname} → "
                                f"archive/{file_date.strftime('%Y%m%d')}/{fname}.gz"
                            )
                        except Exception as e:
                            logger.warning(f"[LogArchiver] 아카이브 실패 {fname}: {e}")
                elif age_days >= 2:
                    # 2일 이상 → 메인 logs에서 삭제 (archive에 이미 있음)
                    # archive에 없으면 아카이브 후 삭제
                    arch_subdir = os.path.join(self.archive_dir,
                                               file_date.strftime("%Y%m%d"))
                    dest = os.path.join(arch_subdir, fname + ".gz")
                    if not os.path.exists(dest):
                        os.makedirs(arch_subdir, exist_ok=True)
                        try:
                            with open(fpath, "rb") as fin, gzip.open(dest, "wb") as fout:
                                shutil.copyfileobj(fin, fout)
                        except Exception:
                            pass
                    try:
                        os.remove(fpath)
                        result["cleaned"] += 1
                    except Exception as e:
                        logger.warning(f"[LogArchiver] 삭제 실패 {fname}: {e}")

        except Exception as e:
            logger.warning(f"[LogArchiver] 실행 실패: {e}")

        if result["archived"] or result["cleaned"]:
            logger.info(
                f"[LogArchiver] 완료 — 아카이브={result['archived']}개 / "
                f"정리={result['cleaned']}개"
            )
        return result

    def rotate_live_log(self, log_path: str, max_mb: float = 50.0):
        """
        실시간 로그 파일이 max_mb 초과 시 로테이션 (새 파일 시작).
        PM2 로그 크기 무제한 방지용.
        """
        try:
            if not os.path.exists(log_path):
                return
            size_mb = os.path.getsize(log_path) / (1024 * 1024)
            if size_mb < max_mb:
                return

            today = date.today()
            ts    = datetime.now().strftime("%H%M%S")
            arch_subdir = os.path.join(self.archive_dir, today.strftime("%Y%m%d"))
            os.makedirs(arch_subdir, exist_ok=True)

            fname   = os.path.basename(log_path)
            dest    = os.path.join(arch_subdir, f"{fname}.{ts}.gz")
            with open(log_path, "rb") as fin, gzip.open(dest, "wb") as fout:
                shutil.copyfileobj(fin, fout)

            # 원본 파일 비우기 (PM2 로그 파일 유지하면서)
            open(log_path, "w").close()
            logger.info(
                f"[LogArchiver] 로그 로테이션: {fname} "
                f"({size_mb:.1f}MB) → {dest}"
            )
        except Exception as e:
            logger.warning(f"[LogArchiver] 로테이션 실패: {e}")
