"""Portable deployment bindings: relative roots and database-path validation."""
import json
import tempfile
import unittest
from pathlib import Path

from content_screening.db_access import resolve_db
from content_screening.runtime_context import RuntimeContext, RuntimeErrorBase

POLICY = {
    "allow_external_collection": False,
    "allow_external_model": False,
    "allow_external_login": False,
    "allow_external_network": False,
}


def deployment(relative_path, **overrides):
    data = {
        "schema_version": 1,
        "service": "target-npl-rule-control",
        "protocol_version": 2,
        "backend_package": "content_screening",
        "environment": "production",
        "instance_id": "target-npl-production-8766",
        "host": "127.0.0.1",
        "port": 8766,
        "app_root": ".",
        "state_root": ".",
        "database": {"relative_path": relative_path},
        "policy": POLICY,
        "base_commit": "",
    }
    data.update(overrides)
    return data


class DeploymentPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def context(self, relative_path, **overrides):
        data = deployment(relative_path, **overrides)
        return RuntimeContext(self.root / "deployment.json", self.root, self.root, data)

    def test_relative_roots_resolve_against_manifest_directory(self):
        manifest = self.root / "deployment.json"
        manifest.write_text(json.dumps(deployment("data/festival.sqlite3")), encoding="utf-8")
        context = RuntimeContext.load(manifest)
        self.assertEqual(context.app_root, self.root.resolve())
        self.assertEqual(context.state_root, self.root.resolve())
        self.assertEqual(context.state_path("data/festival.sqlite3"), (self.root / "data/festival.sqlite3").resolve())

    def test_absolute_roots_remain_supported(self):
        manifest = self.root / "deployment.json"
        data = deployment("data/festival.sqlite3", app_root=str(self.root), state_root=str(self.root))
        manifest.write_text(json.dumps(data), encoding="utf-8")
        context = RuntimeContext.load(manifest)
        self.assertEqual(context.app_root, self.root.resolve())

    def test_unified_database_name_is_no_longer_hardcoded(self):
        context = self.context("data/screening.sqlite3")
        self.assertEqual(resolve_db(context), (self.root / "data/screening.sqlite3").resolve())

    def test_parent_traversal_is_rejected(self):
        with self.assertRaises(RuntimeErrorBase) as caught:
            resolve_db(self.context("../outside.sqlite3"))
        self.assertEqual(caught.exception.code, "DB_BINDING_MISMATCH")

    def test_absolute_database_path_is_rejected(self):
        absolute = (self.root / "data/festival.sqlite3").as_posix()
        with self.assertRaises(RuntimeErrorBase) as caught:
            resolve_db(self.context(absolute))
        self.assertEqual(caught.exception.code, "DB_BINDING_MISMATCH")

    def test_database_outside_data_directory_is_rejected(self):
        with self.assertRaises(RuntimeErrorBase) as caught:
            resolve_db(self.context("festival.sqlite3"))
        self.assertEqual(caught.exception.code, "DB_BINDING_MISMATCH")

    def test_non_sqlite_suffix_is_rejected(self):
        with self.assertRaises(RuntimeErrorBase) as caught:
            resolve_db(self.context("data/festival.txt"))
        self.assertEqual(caught.exception.code, "DB_BINDING_MISMATCH")


if __name__ == "__main__":
    unittest.main()
