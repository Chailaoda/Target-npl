"""Offline acceptance tests for the shared vendor dependency bootstrap.

All tests are fully local: git output is injected, child collection processes
are mocked, and no login / HTTP / model call happens. They verify the actual
launch command, the selected dependency directory and the five run modes.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from content_screening import vendor_env as ve
from content_screening.multi_collection import collect_selected, collection_command
from content_screening.task_service import atomic_json
from scripts.mediacrawler_collect import validate_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = "68d9ef1c1da22eab75940dfd7cec259907006538"
FORK = "c15e46b15f32e5257ae139f59390fa4efa42ecba"


class VendorEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.decl = copy.deepcopy(ve.load_registry(REPO_ROOT))
        (self.root / "third_party").mkdir(parents=True)
        (self.root / "third_party" / "mediacrawler.json").write_text(
            json.dumps(self.decl, ensure_ascii=False), encoding="utf-8")
        self.upstream = (self.root / self.decl["local_path"]).resolve()
        self.fork = (self.root / self.decl["controlled_fork"]["local_path"]).resolve()
        self.make_tree(self.upstream)
        self.make_tree(self.fork)
        self.shas = {str(self.upstream): UPSTREAM, str(self.fork): FORK}
        self.status = {}
        self.git_calls = []

    def make_tree(self, path, python=True):
        (path / ".git").mkdir(parents=True, exist_ok=True)
        if python:
            py = path / ve._python_relpath(self.decl)
            py.parent.mkdir(parents=True, exist_ok=True)
            py.touch()

    def fake_git(self, cmd, text=True):
        self.git_calls.append(cmd)
        self.assertTrue(cmd[:2] == ["git", "-C"], "no non-git command may be issued")
        source = str(Path(cmd[2]).resolve())
        if "rev-parse" in cmd:
            return self.shas[source] + "\n"
        if "status" in cmd:
            return self.status.get(source, "")
        raise AssertionError(f"unexpected git command: {cmd}")

    def resolve(self, env=None, **kw):
        return ve.resolve_vendor_env(self.root, env={} if env is None else env,
                                    run=self.fake_git, **kw)

    # --- declaration / single source of truth -----------------------------
    def test_declaration_has_separate_controlled_dir_and_required_flag(self):
        d = self.decl
        self.assertNotEqual(d["local_path"], d["controlled_fork"]["local_path"])
        self.assertTrue(d["controlled_fork"]["required_for_public_account_fields"])
        self.assertEqual(d["commit"], UPSTREAM)
        self.assertEqual(d["controlled_fork"]["commit"], FORK)
        self.assertIn(UPSTREAM, d["compatibility"]["allowed_commits"])
        self.assertIn(FORK, d["compatibility"]["allowed_commits"])
        self.assertEqual(d["runtime"]["resolved_by"], "content_screening/vendor_env.py")

    def test_registry_rejects_fork_path_collision_with_baseline(self):
        bad = copy.deepcopy(self.decl)
        bad["controlled_fork"]["local_path"] = bad["local_path"]
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            ve.load_registry(self.root, bad)
        self.assertEqual(ctx.exception.code, "REGISTRY_PATH_COLLISION")

    def test_three_entry_points_all_use_the_shared_resolver(self):
        sources = {
            "multi_collection.py": (REPO_ROOT / "content_screening" / "multi_collection.py").read_text(encoding="utf-8"),
            "mediacrawler_collect.py": (REPO_ROOT / "scripts" / "mediacrawler_collect.py").read_text(encoding="utf-8"),
            "platform_collect.py": (REPO_ROOT / "scripts" / "platform_collect.py").read_text(encoding="utf-8"),
        }
        for name, text in sources.items():
            self.assertIn("resolve_vendor_env", text, name)
            self.assertNotIn("vendor/MediaCrawler/.venv/Scripts/python.exe", text, name)
            self.assertNotIn("'vendor/MediaCrawler'", text, name)
            self.assertNotIn('"vendor/MediaCrawler"', text, name)

    # --- five run modes ---------------------------------------------------
    def test_mode1_upstream_anonymous_is_allowed(self):
        shutil.rmtree(self.fork)
        e = self.resolve()
        self.assertEqual((e.kind, e.public_accounts, e.flag), ("upstream", False, ""))
        self.assertEqual(e.source_dir, self.upstream)
        self.assertEqual(e.sha, UPSTREAM)

    def test_mode2_upstream_with_public_request_fails_before_collection(self):
        shutil.rmtree(self.fork)
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "1"})
        self.assertEqual(ctx.exception.code, "PUBLIC_ACCOUNTS_REQUIRE_CONTROLLED_FORK")
        # Even when the scheduler explicitly pinned the upstream directory,
        # a public-account task must NOT quietly fall back to anonymous.
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "1", ve.ENV_VENDOR_DIR: str(self.upstream)})
        self.assertEqual(ctx.exception.code, "PUBLIC_ACCOUNTS_REQUIRE_CONTROLLED_FORK")
        self.assertIn("upstream", str(ctx.exception))

    def test_mode3_controlled_fork_enables_passthrough(self):
        for env in ({}, {ve.ENV_PUBLIC: "auto"}, {ve.ENV_PUBLIC: "1"}):
            e = self.resolve(env)
            self.assertEqual((e.kind, e.sha), ("controlled_fork", FORK))
            self.assertTrue(e.public_accounts)
            self.assertEqual(e.flag, "1")
            self.assertEqual(e.overrides()[ve.ENV_VENDOR_DIR], str(self.fork))
            self.assertEqual(e.overrides()[ve.ENV_SHA], FORK)

    def test_mode4_explicit_zero_keeps_anonymous_behaviour(self):
        # Fork installed + explicit off -> anonymous even if only the fork binary exists.
        shutil.rmtree(self.upstream)
        e = self.resolve({ve.ENV_PUBLIC: "0"})
        self.assertEqual(e.source_dir, self.fork)
        self.assertFalse(e.public_accounts)
        self.assertEqual(e.flag, "")
        # Both installed -> unrelated/anonymous mode may still use the upstream baseline.
        self.make_tree(self.upstream)
        e2 = self.resolve({ve.ENV_PUBLIC: "0"})
        self.assertEqual(e2.source_dir, self.upstream)
        self.assertEqual(e2.flag, "")

    def test_mode5_bad_commit_dirty_tree_missing_python_and_install(self):
        self.shas[str(self.fork)] = "0" * 40
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "1"})
        self.assertEqual(ctx.exception.code, "VENDOR_COMMIT_NOT_ALLOWED")

        self.shas[str(self.fork)] = FORK
        self.status[str(self.fork)] = " M store/douyin/__init__.py\n"
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "1"})
        self.assertEqual(ctx.exception.code, "VENDOR_WORKTREE_DIRTY")

        self.status[str(self.fork)] = ""
        (self.fork / ve._python_relpath(self.decl)).unlink()
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "1"})
        self.assertEqual(ctx.exception.code, "VENDOR_PYTHON_MISSING")

        shutil.rmtree(self.upstream)
        shutil.rmtree(self.fork)
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.code, "VENDOR_NOT_INSTALLED")

    def test_commit_that_matches_declaration_but_is_not_fork_is_refused_for_public(self):
        self.shas[str(self.fork)] = UPSTREAM
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "1"})
        self.assertEqual(ctx.exception.code, "PUBLIC_ACCOUNTS_REQUIRE_CONTROLLED_FORK")

    def test_invalid_flag_and_shared_pycache_rule(self):
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "maybe"})
        self.assertEqual(ctx.exception.code, "PUBLIC_ACCOUNTS_FLAG_INVALID")
        self.status[str(self.fork)] = "?? tools/__pycache__/x.cpython-313.pyc\n"
        self.assertTrue(self.resolve({ve.ENV_PUBLIC: "1"}).public_accounts)
        self.status[str(self.fork)] = "?? tools/__pycache__/planted.txt\n"
        with self.assertRaises(ve.VendorEnvironmentError):
            self.resolve({ve.ENV_PUBLIC: "1"})

    def test_selected_commit_matches_declaration_and_interpreter_is_in_source_tree(self):
        fork_env = self.resolve({ve.ENV_PUBLIC: "1"})
        self.assertEqual(fork_env.sha, FORK)
        self.assertEqual(fork_env.expected_commit, self.decl["controlled_fork"]["commit"])
        self.assertTrue(fork_env.python.is_relative_to(fork_env.source_dir))
        self.assertTrue(fork_env.describe()["interpreter_matches_source_tree"])
        self.assertIn("manual confirmation", fork_env.describe()["manual_confirmation_required"])
        base_env = self.resolve({ve.ENV_PUBLIC: "0", ve.ENV_VENDOR_DIR: str(self.upstream)})
        self.assertEqual((base_env.kind, base_env.sha, base_env.expected_commit),
                         ("upstream", UPSTREAM, self.decl["commit"]))
        self.assertFalse(base_env.public_accounts)

    def test_scheduler_command_uses_resolved_interpreter(self):
        e = self.resolve({ve.ENV_PUBLIC: "1"})
        cmd = collection_command(self.root, e.python, "platform_collect.py",
                                 self.root / "c.json", self.root / "r.json", self.root / "p.json")
        self.assertEqual(cmd[0], str(e.python))
        self.assertEqual(cmd[1:3], ["-X", "utf8"])
        self.assertEqual(Path(cmd[3]), self.root / "scripts" / "platform_collect.py")

    # --- real launch command, end-to-end offline --------------------------
    def test_real_task_launch_command_and_child_env(self):
        settings = json.loads((REPO_ROOT / "config" / "collection.json").read_text(encoding="utf-8-sig"))
        settings = dict(validate_settings(settings), platforms=["bilibili"], platform="bilibili",
                        max_posts=2, max_requests=10, timeout_seconds=60)
        folder = self.root / "data" / "task-runs" / ("c" * 32)
        folder.mkdir(parents=True)
        captured = {}

        def child(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs.get("env")
            cfg = Path(command[command.index("--config") + 1])
            value = json.loads(cfg.read_text(encoding="utf-8"))
            job = self.root / "data" / "collections" / "20261105T000000000001Z"
            job.mkdir(parents=True, exist_ok=True)
            raw = job / "raw.jsonl"
            raw.write_text(json.dumps({"platform": "bilibili", "object_type": "post",
                                       "data": {"content_id": "1", "text": "离线样例"}}) + "\n",
                           encoding="utf-8")
            state = {"status": "completed", "records": 1, "posts": 1, "comments": 0,
                     "requests": 2, "raw_file": str(raw), "settings": value}
            atomic_json(job / "checkpoint.json", state)
            atomic_json(Path(command[command.index("--receipt") + 1]), state)
            return subprocess.CompletedProcess(command, 0)

        clean_env = {ve.ENV_PUBLIC: "1", ve.ENV_VENDOR_DIR: "", ve.ENV_SHA: "", ve.ENV_KIND: ""}
        with patch.dict(os.environ, clean_env), \
             patch("content_screening.vendor_env.subprocess.check_output", side_effect=self.fake_git), \
             patch("content_screening.multi_collection.subprocess.run", side_effect=child):
            result = collect_selected(settings, folder, self.root)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(captured["command"][0], str((self.fork / ve._python_relpath(self.decl)).resolve()))
        launch = captured["env"]
        self.assertEqual(launch[ve.ENV_VENDOR_DIR], str(self.fork))
        self.assertEqual(launch[ve.ENV_PUBLIC], "1")
        self.assertEqual(launch[ve.ENV_SHA], FORK)
        self.assertEqual(launch[ve.ENV_KIND], "controlled_fork")
        self.assertTrue(all(c[:2] == ["git", "-C"] for c in self.git_calls))

    def test_explicit_zero_reaches_child_env(self):
        e = self.resolve({ve.ENV_PUBLIC: "0"})
        self.assertEqual(e.launch_env({})[ve.ENV_PUBLIC], "")
        # TARGET_NPL_VENDOR_DIR pins the child to the SAME directory the parent resolved.
        locked = self.resolve({ve.ENV_PUBLIC: "0", ve.ENV_VENDOR_DIR: str(e.source_dir)})
        self.assertEqual(locked.source_dir, e.source_dir)

    # --- post-merge P2: resolved paths, declared dirs, child pins ---------
    def test_registry_rejects_aliased_path_collision(self):
        bad = copy.deepcopy(self.decl)
        # ".." alias resolves to the SAME physical directory as the baseline.
        bad["controlled_fork"]["local_path"] = "vendor/MediaCrawler/../MediaCrawler"
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            ve.load_registry(self.root, bad)
        self.assertEqual(ctx.exception.code, "REGISTRY_PATH_COLLISION")

    def test_pinned_dir_must_be_one_of_the_declared_locations(self):
        alien = self.root / "vendor" / "undeclared-clone"
        self.make_tree(alien)
        self.shas[str(alien.resolve())] = FORK
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "1", ve.ENV_VENDOR_DIR: str(alien)})
        self.assertEqual(ctx.exception.code, "VENDOR_DIR_NOT_DECLARED")

    def test_pinned_dir_alias_resolving_to_declared_dir_is_accepted(self):
        alias = self.fork / ".." / self.fork.name
        e = self.resolve({ve.ENV_PUBLIC: "1", ve.ENV_VENDOR_DIR: str(alias)})
        self.assertEqual(e.source_dir, self.fork)
        self.assertEqual(e.kind, "controlled_fork")

    def test_selected_dir_must_match_selected_commit_kind(self):
        # Fork directory holding the upstream commit: anonymous mode must not
        # silently accept a mismatched directory / dependency kind.
        self.shas[str(self.fork)] = UPSTREAM
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "0", ve.ENV_VENDOR_DIR: str(self.fork)})
        self.assertEqual(ctx.exception.code, "VENDOR_DIR_KIND_MISMATCH")

    def test_child_sha_and_kind_must_match_scheduler_pins(self):
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "1", ve.ENV_SHA: UPSTREAM})
        self.assertEqual(ctx.exception.code, "VENDOR_SHA_MISMATCH")
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            self.resolve({ve.ENV_PUBLIC: "1", ve.ENV_KIND: "upstream"})
        self.assertEqual(ctx.exception.code, "VENDOR_KIND_MISMATCH")
        # Positive path: the exact env handed to a child resolves identically.
        parent = self.resolve({ve.ENV_PUBLIC: "1"})
        child = self.resolve(parent.launch_env({}))
        self.assertEqual((child.kind, child.sha, child.source_dir),
                         (parent.kind, parent.sha, parent.source_dir))

    def test_git_status_failure_is_structured_preflight_error(self):
        def broken(cmd, text=True):
            if "status" in cmd:
                raise subprocess.CalledProcessError(128, cmd)
            return self.fake_git(cmd, text=text)
        with self.assertRaises(ve.VendorEnvironmentError) as ctx:
            ve.resolve_vendor_env(self.root, env={ve.ENV_PUBLIC: "1"}, run=broken)
        self.assertEqual(ctx.exception.code, "VENDOR_GIT_UNAVAILABLE")
        with patch("content_screening.vendor_env.subprocess.check_output", side_effect=broken):
            ok, report = ve.preflight(root=self.root, env={ve.ENV_PUBLIC: "1"})
        self.assertFalse(ok)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["code"], "VENDOR_GIT_UNAVAILABLE")
        self.assertNotIn("Traceback", str(report))

    # --- offline preflight ------------------------------------------------
    def test_preflight_blocks_when_install_dir_missing(self):
        shutil.rmtree(self.upstream)
        shutil.rmtree(self.fork)
        ok, report = ve.preflight(root=self.root, env={})
        self.assertFalse(ok)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["code"], "VENDOR_NOT_INSTALLED")
        ok2, report2 = ve.preflight(root=self.root, env={}, require_public_accounts=True)
        self.assertFalse(ok2)
        self.assertEqual(report2["code"], "PUBLIC_ACCOUNTS_REQUIRE_CONTROLLED_FORK")
        # CLI is dry-run only: no network, explicit exit code 2.
        proc = subprocess.run([sys.executable, "-X", "utf8", "-m",
                               "content_screening.vendor_env", "--preflight",
                               "--root", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BLOCKED", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
