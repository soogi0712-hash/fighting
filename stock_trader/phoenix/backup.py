"""백업 · corruption 감지 (Phase 1.5 §4).

- backup_online: SQLite Online Backup API 로 WAL 포함 일관 사본 생성(cp 금지).
- check_integrity: PRAGMA integrity_check. 손상 시 IntegrityResult(ok=False).
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass


@dataclass
class IntegrityResult:
    ok: bool
    detail: str


def backup_online(src_path: str, dst_path: str) -> str:
    """src DB 를 dst 로 온라인 백업(무중단). dst 경로 반환."""
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)) or ".", exist_ok=True)
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dst_path


def check_integrity(path: str) -> IntegrityResult:
    """DB 파일 무결성 검사. 손상(파일 아님/malformed 포함) 시 ok=False."""
    if not os.path.exists(path):
        return IntegrityResult(False, "file not found")
    conn = None
    try:
        conn = sqlite3.connect(path)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if row and row[0] == "ok":
            return IntegrityResult(True, "ok")
        return IntegrityResult(False, str(row[0]) if row else "unknown")
    except sqlite3.DatabaseError as e:
        return IntegrityResult(False, f"{type(e).__name__}: {e}")
    finally:
        if conn is not None:
            conn.close()
