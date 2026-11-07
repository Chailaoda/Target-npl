"""Single source of truth for the MediaCrawler runtime dependency.

Every entry point that can start a collection job resolves its environment
HERE, so the three things that must never drift apart always come from one
directory:

    1. the Python interpreter chosen by the scheduler
       (content_screening/multi_collection.py)
    2. the MediaCrawler source tree that is imported / chdir'd into
       (scripts/mediacrawler_collect.py, scripts/platform_collect.py)
    3. the git worktree used by the version integrity check

The paths and commit pins are declared ONLY in third_party/mediacrawler.json.
The upstream baseline is kept intact in its own directory; the controlled
fork must be installed into a SEPARATE directory (the registry refuses an
identical path), so a clean machine can reproduce both without overwriting
the pinned upstream checkout.

Detecting the controlled fork proves only that this runtime HAS THE CAPABILITY
to pass through public author fields. It does NOT grant, verify or replace
confirmation of platform access permission, the operator's authorisation or
a valid login state. That remains a manual pre-online-acceptance step.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ENV_PUBLIC = "TARGET_NPL_PUBLIC_ACCOUNTS"
ENV_VENDOR_DIR = "TARGET_NPL_VENDOR_DIR"      # scheduler -> child process lock
ENV_SHA = "TARGET_NPL_UPSTREAM_SHA"
ENV_KIND = "TARGET_NPL_VENDOR_KIND"

_MANUAL_CONFIRMATION = (
    "Detecting the fork only proves passthrough capability. Platform access "
    "permission, the operator's authorisation and login state still require "
    "manual confirmation before any online collection."
)


class VendorEnvironmentError(RuntimeError):
    """Raised BEFORE collection starts; never silently degrade to upstream."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def load_registry(root=None, decl=None):
    """Read and validate the dependency declaration (single source of truth).

    Validation also applies to an injected declaration object, so callers
    cannot bypass the "fork must not overwrite the baseline" guard.
    """
    root = Path(root or PROJECT_ROOT).resolve()
    if decl is None:
        path = root / "third_party" / "mediacrawler.json"
        if not path.is_file():
            raise VendorEnvironmentError("REGISTRY_MISSING", f"dependency declaration not found: {path}")
        decl = json.loads(path.read_text(encoding="utf-8"))
    fork = decl.get("controlled_fork", {})
    for key in ("repository", "commit", "local_path"):
        if not decl.get(key) or not fork.get(key):
            raise VendorEnvironmentError("REGISTRY_INCOMPLETE", f"missing '{key}' in declaration")
    # Compare fully resolved paths: ".." aliases, junctions and symlinks that
    # point at the same physical directory must not slip through as "separate".
    if _resolve_dir(root, decl["local_path"]) == _resolve_dir(root, fork["local_path"]):
        raise VendorEnvironmentError(
            "REGISTRY_PATH_COLLISION",
            "controlled fork must use a separate directory; overwriting the pinned upstream baseline is forbidden")
    allowed = tuple(decl.get("compatibility", {}).get("allowed_commits", ()))
    if decl["commit"] not in allowed or fork["commit"] not in allowed:
        raise VendorEnvironmentError("REGISTRY_INCOMPLETE", "allowed_commits must contain both pinned commits")
    return decl


def is_pycache_entry(status_line):
    """True only for a real Python bytecode cache FILE inside a __pycache__ dir.

    Matching by path COMPONENTS + strict parent directory + filename suffix
    prevents disguised names ("__pycache__-notes.md", "evil__pycache__.py")
    and arbitrary files planted inside a cache directory from being tolerated.
    Only *.pyc / *.pyo directly under a __pycache__ component are allowed.
    """
    raw = status_line[3:].strip()
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    raw = raw.encode().decode("unicode_escape") if "\\u" in raw or "\\x" in raw else raw
    parts = PurePosixPath(raw.replace("\\\\", "\\")).parts
    return len(parts) >= 2 and parts[-2] == "__pycache__" \
        and parts[-1].endswith((".pyc", ".pyo"))


def _git(source, args, run):
    return run(["git", "-C", str(source), *args], text=True).strip()


def verify_worktree(source, *, expected=None, allowed=None, run=None):
    """Check HEAD whitelist + fully clean tree (staged/unstaged/untracked).

    `run` is injectable so the whole version-selection logic can be tested
    offline with zero network requests and without a real git checkout.
    """
    run = run or subprocess.check_output
    sha = _git(source, ["rev-parse", "HEAD"], run)
    permitted = (expected,) if expected is not None else tuple(allowed or ())
    if permitted and sha not in permitted:
        raise ValueError("Pinned upstream changed; review adapter compatibility first")
    status = _git(source, ["status", "--porcelain", "--untracked-files=all"], run)
    dirty = [line for line in status.splitlines() if line and not is_pycache_entry(line)]
    if dirty:
        raise ValueError("Pinned upstream working tree is dirty; review adapter compatibility first")
    return sha


def _resolve_dir(root, value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _python_relpath(decl):
    runtime = decl.get("runtime", {})
    if runtime.get("python_relpath"):
        return runtime["python_relpath"]
    windows = sys.platform == "win32"
    key = "python_relpath_windows" if windows else "python_relpath_posix"
    return runtime.get(key, ".venv/Scripts/python.exe" if windows else ".venv/bin/python")


def _install_hint(decl, want_public):
    if want_public:
        fork = decl["controlled_fork"]
        return (f"git clone -b {fork['branch']} {fork['repository']} "
                f"{fork['local_path']} ; then create .venv and install its requirements (README)")
    return (f"git clone {decl['repository']} {decl['local_path']} ; "
            f"git -C {decl['local_path']} checkout {decl['commit']} ; "
            f"create .venv and install its requirements (README)")


@dataclass(frozen=True)
class VendorEnvironment:
    """One resolved dependency: interpreter, source tree and git worktree."""

    root: Path
    source_dir: Path
    python: Path
    sha: str
    kind: str                 # "upstream" | "controlled_fork"
    public_accounts: bool
    flag: str                 # "1" when passthrough is enabled, else ""
    declaration: dict

    @property
    def expected_commit(self):
        decl = self.declaration
        return decl["controlled_fork"]["commit"] if self.kind == "controlled_fork" else decl["commit"]

    def overrides(self):
        """Only the four values the scheduler must hand to its child process.

        TARGET_NPL_VENDOR_DIR locks the child to the SAME directory, so the
        child cannot independently pick a different dependency.
        """
        return {
            ENV_PUBLIC: self.flag,
            ENV_VENDOR_DIR: str(self.source_dir),
            ENV_SHA: self.sha,
            ENV_KIND: self.kind,
        }

    def launch_env(self, base=None):
        merged = dict(os.environ if base is None else base)
        merged.update(self.overrides())
        return merged

    def describe(self):
        return {
            "source_dir": str(self.source_dir),
            "python": str(self.python),
            "sha": self.sha,
            "kind": self.kind,
            "expected_commit": self.expected_commit,
            "public_accounts_enabled": self.public_accounts,
            "flag": self.flag,
            "interpreter_matches_source_tree": self.python.resolve().is_relative_to(self.source_dir),
            "manual_confirmation_required": _MANUAL_CONFIRMATION,
        }


def resolve_vendor_env(root=None, env=None, *, decl=None, run=None,
                       require_public_accounts=None):
    """Resolve ONE vendor environment for all three collection entry points.

    Modes (fail closed, never silently downgrade):
      1. upstream + anonymous            -> allowed, uses the pinned baseline
      2. upstream + public requested    -> error BEFORE collection
      3. controlled fork + public       -> passthrough enabled for the declared fields
      4. controlled fork + explicit 0   -> anonymous behaviour kept
      5. bad commit / dirty tree / missing python / missing install -> error

    TARGET_NPL_PUBLIC_ACCOUNTS: "1" = require public, "0" = force anonymous,
    unset/"auto" = use the controlled fork when installed, else the baseline.
    """
    root = Path(root or PROJECT_ROOT).resolve()
    decl = load_registry(root, decl)
    env = dict(os.environ if env is None else env)
    run = run or subprocess.check_output

    upstream_commit = decl["commit"]
    fork_commit = decl["controlled_fork"]["commit"]
    allowed = tuple(decl["compatibility"]["allowed_commits"])
    baseline_dir = _resolve_dir(root, decl["local_path"])
    controlled_dir = _resolve_dir(root, decl["controlled_fork"]["local_path"])

    flag = (env.get(ENV_PUBLIC) or "").strip()
    if require_public_accounts is not None:
        want_public = bool(require_public_accounts)
    elif flag == "1":
        want_public = True
    elif flag == "0":
        want_public = False
    elif flag in ("", "auto"):
        # "auto": a properly installed controlled fork opts in; a baseline-only
        # install stays anonymous. A fork dir that exists but is broken is NOT
        # skipped here - the strict checks below fail loudly instead.
        want_public = controlled_dir.is_dir() and (controlled_dir / ".git").exists()
    else:
        raise VendorEnvironmentError(
            "PUBLIC_ACCOUNTS_FLAG_INVALID",
            f"{ENV_PUBLIC} must be 0, 1, auto or unset; got {flag!r}")

    pinned = env.get(ENV_VENDOR_DIR)
    pinned_sha = (env.get(ENV_SHA) or "").strip()
    pinned_kind = (env.get(ENV_KIND) or "").strip()
    if pinned:
        # The scheduler locked this directory for its child process. A pinned
        # path may only be one of the two DECLARED install locations; a random
        # directory holding a whitelisted commit must never be accepted.
        source_dir = Path(pinned).resolve()
        if source_dir not in (baseline_dir, controlled_dir):
            raise VendorEnvironmentError(
                "VENDOR_DIR_NOT_DECLARED",
                f"{ENV_VENDOR_DIR}={source_dir} is not a declared dependency directory; "
                f"expected {baseline_dir} or {controlled_dir}")
    elif want_public:
        source_dir = controlled_dir
    elif baseline_dir.is_dir() and (baseline_dir / ".git").exists():
        source_dir = baseline_dir
    elif controlled_dir.is_dir() and (controlled_dir / ".git").exists():
        # Explicit opt-out while ONLY the controlled fork is installed: keep
        # anonymous behaviour using that binary, but never enable passthrough.
        source_dir = controlled_dir
    else:
        source_dir = baseline_dir  # fails below with an explicit install hint

    if not source_dir.is_dir() or not (source_dir / ".git").exists():
        raise VendorEnvironmentError(
            "PUBLIC_ACCOUNTS_REQUIRE_CONTROLLED_FORK" if want_public else "VENDOR_NOT_INSTALLED",
            f"vendor source tree is not installed at {source_dir}. Install: {_install_hint(decl, want_public)}")

    try:
        sha = _git(source_dir, ["rev-parse", "HEAD"], run)
    except (OSError, subprocess.SubprocessError) as error:
        raise VendorEnvironmentError("VENDOR_GIT_UNAVAILABLE", f"cannot read git HEAD of {source_dir}: {error}") from error
    if sha not in allowed:
        raise VendorEnvironmentError(
            "VENDOR_COMMIT_NOT_ALLOWED",
            f"HEAD {sha} is not in the declared whitelist {list(allowed)}; do not edit the whitelist to hide changes")
    # The selected commit must match the declaration exactly (not merely be allowed).
    kind = "controlled_fork" if sha == fork_commit else "upstream" if sha == upstream_commit else ""
    if not kind:
        raise VendorEnvironmentError("VENDOR_COMMIT_MISMATCH", f"HEAD {sha} matches no declared dependency")
    if want_public and kind != "controlled_fork":
        raise VendorEnvironmentError(
            "PUBLIC_ACCOUNTS_REQUIRE_CONTROLLED_FORK",
            f"public-account collection requested but the selected vendor ({source_dir}) is the upstream "
            f"baseline {sha[:7]}; refusing to fall back to anonymous collection")

    # Directory and commit must agree: an upstream tree may only live in the
    # declared baseline directory, a fork tree only in the declared fork dir.
    expected_dir = controlled_dir if kind == "controlled_fork" else baseline_dir
    if source_dir != expected_dir:
        raise VendorEnvironmentError(
            "VENDOR_DIR_KIND_MISMATCH",
            f"{kind} must be installed in {expected_dir}, but {source_dir} was selected")
    # Child processes inherit the scheduler's pins; any drift is a hard stop.
    if pinned_sha and pinned_sha != sha:
        raise VendorEnvironmentError(
            "VENDOR_SHA_MISMATCH",
            f"scheduler pinned {ENV_SHA}={pinned_sha} but the selected tree HEAD is {sha}")
    if pinned_kind and pinned_kind != kind:
        raise VendorEnvironmentError(
            "VENDOR_KIND_MISMATCH",
            f"scheduler pinned {ENV_KIND}={pinned_kind} but the selected tree is {kind}")

    try:
        status = _git(source_dir, ["status", "--porcelain", "--untracked-files=all"], run)
    except (OSError, subprocess.SubprocessError) as error:
        raise VendorEnvironmentError(
            "VENDOR_GIT_UNAVAILABLE", f"cannot read git status of {source_dir}: {error}") from error
    dirty = [line for line in status.splitlines() if line and not is_pycache_entry(line)]
    if dirty:
        raise VendorEnvironmentError(
            "VENDOR_WORKTREE_DIRTY",
            f"{source_dir} has {len(dirty)} unrecorded change(s): {dirty[:3]}")

    python = (source_dir / _python_relpath(decl)).resolve()
    if not python.is_file():
        raise VendorEnvironmentError(
            "VENDOR_PYTHON_MISSING",
            f"interpreter {python} is missing; create the vendor .venv and install its requirements (README)")
    if not python.is_relative_to(source_dir):
        raise VendorEnvironmentError(
            "VENDOR_PATH_MISMATCH",
            f"interpreter {python} is outside the selected source tree {source_dir}")

    public_accounts = want_public and kind == "controlled_fork"
    return VendorEnvironment(root=root, source_dir=source_dir, python=python, sha=sha,
                             kind=kind, public_accounts=public_accounts,
                             flag="1" if public_accounts else "", declaration=decl)


def preflight(root=None, env=None, *, require_public_accounts=None):
    """Dry-run check: read-only, no login, no collection, no network requests."""
    try:
        resolved = resolve_vendor_env(root=root, env=env,
                                     require_public_accounts=require_public_accounts)
    except VendorEnvironmentError as error:
        return False, {"status": "BLOCKED", "code": error.code, "error": str(error),
                       "manual_confirmation_required": _MANUAL_CONFIRMATION}
    return True, {"status": "PASS", **resolved.describe()}


def main():
    parser = argparse.ArgumentParser(description="Offline vendor dependency preflight (no network, no login)")
    parser.add_argument("--preflight", action="store_true", help="run the offline preflight (default action)")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="project root that owns third_party/ and vendor/")
    parser.add_argument("--public-accounts", action="store_true", help="simulate a public-account task")
    args = parser.parse_args()
    ok, report = preflight(root=args.root,
                           require_public_accounts=True if args.public_accounts else None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
