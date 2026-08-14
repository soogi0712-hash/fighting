"""US 포지션 관리상태 원자 저장소 (§5).

symbol → 관리상태 dict 를 JSON 으로 원자적으로 저장/복원한다.
매매 판단과 무관한 '상태 영속' 전용 유틸이며, 부분저장·손상으로 전체 포지션을
비우지 않는 것을 최우선으로 한다.

원칙:
  - 저장: temp 파일에 기록 → flush+fsync → os.replace(원자적 교체). 직전본은 .bak 보존.
  - 로드: 본 파일 파싱 실패 시 .bak 로 폴백. 둘 다 실패면 CorruptStoreError 를 던져
          호출부가 '빈 dict 로 덮어써 전체 삭제'하는 사고를 막는다(빈 dict 반환 금지).
  - 동시 저장 직렬화(instance Lock) + os.replace 원자성으로 파일 무결성 유지.
  - 병합 로드: us_recovery.merge_state 로 구버전/부분 필드에 안전 기본값 적용.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Optional

from strategies.us_recovery import merge_state


class CorruptStoreError(Exception):
    """본 파일·백업 모두 파싱 불가 — 호출부는 빈 저장으로 덮어쓰면 안 된다."""


class AtomicPositionStore:
    def __init__(self, path: str):
        self.path = path
        self.bak  = path + ".bak"
        self._lock = threading.Lock()

    # ── 저장(원자적·직렬화) ──────────────────────────────────
    def save(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("save 는 dict 만 허용")
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
            # 직전본 백업(원자 교체 전에 현재 파일을 .bak 로 복사)
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        prev = f.read()
                    with open(self.bak, "w", encoding="utf-8") as f:
                        f.write(prev)
                except OSError:
                    pass  # 백업 실패해도 저장은 진행(원자성은 유지)
            # temp 기록 → fsync → 원자 교체
            d = os.path.dirname(os.path.abspath(self.path)) or "."
            fd, tmp = tempfile.mkstemp(prefix=".uspos-", dir=d)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)   # 원자적
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    # ── 로드(손상 안전) ──────────────────────────────────────
    def _read(self, p: str) -> Optional[dict]:
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            raise ValueError("최상위가 dict 아님")
        return obj

    def load(self) -> dict:
        """본 파일 → 실패 시 .bak. 둘 다 실패면 CorruptStoreError.
        파일 자체가 없으면 {} (정상 초기 상태)."""
        if not os.path.exists(self.path) and not os.path.exists(self.bak):
            return {}
        try:
            obj = self._read(self.path)
            if obj is not None:
                return obj
        except (ValueError, json.JSONDecodeError, OSError):
            pass
        # 본 파일 손상 → 백업 폴백
        try:
            obj = self._read(self.bak)
            if obj is not None:
                return obj
        except (ValueError, json.JSONDecodeError, OSError):
            pass
        raise CorruptStoreError(f"본/백업 모두 파싱 불가: {self.path}")

    def load_merged(self) -> dict:
        """로드 + 각 항목을 us_recovery.merge_state 로 안전 기본값 병합."""
        raw = self.load()
        return {sym: merge_state(st) for sym, st in raw.items()}
