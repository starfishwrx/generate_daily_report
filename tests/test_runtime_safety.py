from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from app_paths import AppPaths, migrate_legacy_runtime_files
from publish_state import PublishStateStore, content_hash
from run_lock import AlreadyRunningError, single_instance_lock


class RuntimeSafetyTests(unittest.TestCase):
    def test_migration_copies_without_deleting_legacy_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle = root / "release"
            data = root / "data"
            bundle.mkdir()
            (bundle / "config.yaml").write_text("base_url: test\n", encoding="utf-8")
            paths = AppPaths(bundle, data, data / "config.yaml", data / "extra_auth.json", data / ".env.scheduler", data / "output")
            migrated = migrate_legacy_runtime_files(paths)
            self.assertEqual(migrated, [data / "config.yaml"])
            self.assertTrue((bundle / "config.yaml").exists())
            self.assertEqual((data / "config.yaml").read_text(encoding="utf-8"), "base_url: test\n")

    def test_publish_state_skips_only_identical_completed_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir)
            report = output / "report.txt"
            report.write_text("v1", encoding="utf-8")
            first_hash = content_hash([report])
            store = PublishStateStore(output, date(2026, 7, 17))
            self.assertIsNone(store.completed_result("feishu_main", first_hash))
            store.mark_completed("feishu_main", first_hash, {"url": "https://example.test/doc"})
            self.assertEqual(store.completed_result("feishu_main", first_hash)["url"], "https://example.test/doc")
            report.write_text("v2", encoding="utf-8")
            self.assertIsNone(store.completed_result("feishu_main", content_hash([report])))

    def test_single_instance_lock_rejects_second_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "run.lock"
            with single_instance_lock(lock_path):
                with self.assertRaises(AlreadyRunningError):
                    with single_instance_lock(lock_path):
                        pass


if __name__ == "__main__":
    unittest.main()
