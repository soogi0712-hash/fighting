"""US 포지션 관리상태 원자 저장소 테스트 (§5/§9D 영속·동시성·손상안전)."""
import os
import sys
import json
import shutil
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from strategies.us_position_store import AtomicPositionStore, CorruptStoreError
import strategies.us_recovery as R


class PositionStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "us_positions.json")
        self.store = AtomicPositionStore(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_load_roundtrip(self):
        data = {"IONQ": R.default_state(recovered=True, highest_price=40.0)}
        self.store.save(data)
        loaded = self.store.load()
        self.assertEqual(loaded["IONQ"]["recovered"], True)
        self.assertEqual(loaded["IONQ"]["highest_price"], 40.0)

    def test_atomic_no_partial_file(self):
        # 저장 후 temp 잔여 없음 + 원자 교체
        self.store.save({"A": R.default_state()})
        leftovers = [f for f in os.listdir(self.tmp) if f.startswith(".uspos-")]
        self.assertEqual(leftovers, [])

    def test_missing_file_returns_empty(self):
        self.assertEqual(self.store.load(), {})

    def test_corrupt_main_falls_back_to_bak(self):
        self.store.save({"A": R.default_state(highest_price=10.0)})
        self.store.save({"A": R.default_state(highest_price=20.0)})  # .bak = 10.0 버전
        # 본 파일 손상
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{corrupt json ]]")
        loaded = self.store.load()   # .bak 폴백
        self.assertIn("A", loaded)

    def test_corrupt_both_raises_not_empty(self):
        # 본·백업 모두 손상 → CorruptStoreError (빈 dict 반환 금지 = 전체삭제 방지)
        self.store.save({"A": R.default_state()})
        self.store.save({"A": R.default_state()})
        with open(self.path, "w") as f: f.write("broken")
        with open(self.path + ".bak", "w") as f: f.write("broken")
        with self.assertRaises(CorruptStoreError):
            self.store.load()

    def test_legacy_json_merged_defaults(self):
        # 구버전 JSON(신규 관리필드 없음) → load_merged 안전 기본값
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"AAPL": {"code": "AAPL", "qty": 10, "avg_price": 50.0}}, f)
        merged = self.store.load_merged()
        self.assertEqual(merged["AAPL"]["management_mode"], R.MODE_NORMAL)
        self.assertFalse(merged["AAPL"]["recovered"])

    def test_restart_preserves_recovery_state(self):
        s = R.default_state()
        s["management_mode"] = R.MODE_RECOVERY
        s["recovery_started_at"] = "2026-08-14T22:40:00"
        s["recovery_high_price"] = 101.0
        self.store.save({"IONQ": s})
        # 새 인스턴스(=재시작)로 로드
        store2 = AtomicPositionStore(self.path)
        loaded = store2.load_merged()
        self.assertEqual(loaded["IONQ"]["management_mode"], R.MODE_RECOVERY)
        self.assertEqual(loaded["IONQ"]["recovery_started_at"], "2026-08-14T22:40:00")
        self.assertEqual(loaded["IONQ"]["recovery_high_price"], 101.0)

    def test_concurrent_saves_keep_integrity(self):
        # 동시 8회 저장 → 파일 항상 유효 JSON, 손상 없음
        errors = []
        def worker(i):
            try:
                for _ in range(20):
                    self.store.save({f"S{i}": R.default_state(highest_price=float(i))})
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        # 최종 파일은 항상 파싱 가능(부분저장/손상 없음)
        loaded = self.store.load()
        self.assertIsInstance(loaded, dict)

    def test_save_does_not_wipe_on_repeated_load_save(self):
        # 반복 로드→저장에도 데이터 유지(빈 dict 로 덮이지 않음)
        self.store.save({"A": R.default_state(highest_price=99.0)})
        for _ in range(5):
            data = self.store.load()
            self.store.save(data)
        self.assertEqual(self.store.load()["A"]["highest_price"], 99.0)


if __name__ == "__main__":
    unittest.main()
