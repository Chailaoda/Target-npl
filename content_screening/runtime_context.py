"""Fail-closed APP_ROOT / STATE_ROOT and release identity resolution.

Loading this module performs no writes. Integration uses 8767 and production
uses 8766; both are selected explicitly by the deployment manifest.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re


class RuntimeErrorBase(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def load_deployment(path=None):
    candidate = Path(path or os.environ.get("TARGET_NPL_DEPLOYMENT", Path(__file__).resolve().parents[1] / "deployment.json"))
    if not candidate.is_file():
        raise RuntimeErrorBase("DEPLOYMENT_MISSING", str(candidate))
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeErrorBase("DEPLOYMENT_MISSING", "deployment is unreadable") from error
    required = {"service", "protocol_version", "backend_package", "environment", "instance_id", "host", "port", "app_root", "state_root", "database", "policy", "base_commit"}
    if not required <= set(data):
        raise RuntimeErrorBase("DEPLOYMENT_MISSING", "deployment required keys are incomplete")
    return data


@dataclass(frozen=True)
class RuntimeContext:
    deployment_path: Path
    app_root: Path
    state_root: Path
    deployment: dict

    @classmethod
    def load(cls, path=None):
        deployment_path = Path(path or os.environ.get("TARGET_NPL_DEPLOYMENT", Path(__file__).resolve().parents[1] / "deployment.json")).resolve()
        data = load_deployment(deployment_path)
        app_root = Path(data["app_root"]).resolve()
        state_root = Path(data["state_root"]).resolve()
        if data.get("test_isolated"):
            return cls(deployment_path, app_root, state_root, data)
        if app_root != deployment_path.parent and not data.get("test_isolated"):
            raise RuntimeErrorBase("DB_PATH_OUTSIDE_STATE", "APP_ROOT/STATE_ROOT binding is invalid")
        if data["environment"] == "integration" and (app_root.name != "app" or state_root.name != "state" or app_root.parent != state_root.parent):
            raise RuntimeErrorBase("DB_PATH_OUTSIDE_STATE", "integration roots must be sibling app/state directories")
        return cls(deployment_path, app_root, state_root, data)

    def state_path(self, relative):
        path = (self.state_root / relative).resolve()
        if not path.is_relative_to(self.state_root):
            raise RuntimeErrorBase("DB_PATH_OUTSIDE_STATE", str(relative))
        return path

    def app_path(self, relative):
        path = (self.app_root / relative).resolve()
        if not path.is_relative_to(self.app_root):
            raise RuntimeErrorBase("DB_PATH_OUTSIDE_STATE", str(relative))
        return path

    @property
    def policy(self):
        return self.deployment["policy"]

    def require_external(self, kind):
        key = {"collection": "allow_external_collection", "model": "allow_external_model", "login": "allow_external_login", "network": "allow_external_network"}[kind]
        if self.deployment["environment"] == "integration" or not self.policy.get(key, False):
            raise RuntimeErrorBase("POLICY_EXTERNAL_BLOCKED", f"external {kind} is disabled")

    def identity(self, release=None):
        release = release or {}
        state_binding = hashlib.sha256(str(self.state_root).replace("\\", "/").lower().encode()).hexdigest()
        db_binding = hashlib.sha256(str(self.state_path(self.deployment["database"]["relative_path"])).replace("\\", "/").lower().encode()).hexdigest()
        instance = {key: self.deployment[key] for key in ("service", "protocol_version", "backend_package", "environment", "instance_id", "host", "port", "base_commit")}
        instance.update(policy=self.policy, features=self.deployment.get("features", {}), state_binding=state_binding, db_binding=db_binding)
        project_file = self.app_root / "pyproject.toml"
        instance["pyproject_sha256"] = hashlib.sha256(project_file.read_bytes()).hexdigest() if project_file.is_file() else None
        instance_binding = hashlib.sha256(json.dumps(instance, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {
            "service": self.deployment["service"],
            "protocol_version": self.deployment["protocol_version"],
            "backend_package": self.deployment["backend_package"],
            "environment": self.deployment["environment"],
            "instance_id": self.deployment["instance_id"],
            "state_binding": state_binding,
            "db_binding": db_binding,
            "instance_binding": instance_binding,
            "schema_state": "migrated" if self.state_path(self.deployment["database"]["relative_path"]).is_file() else "missing",
            "base_commit": self.deployment["base_commit"],
            **release,
        }


def _is_relative_to(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_default_config(context):
    """Validate default JSON structures independently of the active pointer."""
    try:
        collection = json.loads(context.state_path("config/collection.json").read_text(encoding="utf-8-sig"))
        policy = json.loads(context.state_path("profiles/default.json").read_text(encoding="utf-8-sig"))
        from scripts.mediacrawler_collect import validate_settings
        from .rules_v2 import validate_policy
        validate_settings(collection)
        validate_policy(policy)
        for rel in ("schemas/screening-task-v1.schema.json", "schemas/agent-tools-v1.json"):
            json.loads(context.app_path(rel).read_text(encoding="utf-8-sig"))
        connection = json.loads(context.state_path("config/connection.json").read_text(encoding="utf-8-sig"))
        if context.deployment["environment"] == "integration":
            if set(connection) != {"source", "ark", "integration_placeholder"} or connection.get("integration_placeholder") is not True or connection["source"] or connection["ark"]:
                raise ValueError("integration connection must contain only a marked empty placeholder")
        else:
            from .connectors import load_connection
            load_connection(context.state_path("config/connection.json"))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RuntimeErrorBase("CONFIG_DEFAULT_INVALID", str(error)[:240]) from error
    return True


def validate_active_snapshot(context):
    """Validate the active pointer and its snapshot hash without conflating defaults."""
    from .active_config import active_snapshot
    try:
        return active_snapshot(context.state_root)
    except FileNotFoundError as error:
        raise RuntimeErrorBase("ACTIVE_SNAPSHOT_MISSING", str(error)) from error
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RuntimeErrorBase("ACTIVE_SNAPSHOT_HASH_MISMATCH", str(error)) from error


def preflight(context=None, *, validate_release=True):
    context = context or RuntimeContext.load()
    data = context.deployment
    if data.get("test_isolated"):
        return context.identity({"schema_state": "migrated"})
    if data["environment"] not in {"integration", "production"} or data["host"] != "127.0.0.1":
        raise RuntimeErrorBase("DEPLOYMENT_MISSING", "unsupported runtime environment or non-loopback host")
    expected_port = 8767 if data["environment"] == "integration" else 8766
    if data["port"] != expected_port:
        raise RuntimeErrorBase("DEPLOYMENT_MISSING", "port does not match the deployment environment")
    external_switches = ("allow_external_collection", "allow_external_model", "allow_external_login", "allow_external_network")
    if data["environment"] == "integration" and any(data["policy"].get(key, False) for key in external_switches):
        raise RuntimeErrorBase("POLICY_EXTERNAL_BLOCKED", "integration external policy switches must remain false")
    features = data.get("features", {})
    if data["environment"] == "integration" and (features.get("formal_collection") or features.get("os_network_rules")):
        raise RuntimeErrorBase("POLICY_EXTERNAL_BLOCKED", "isolated-environment features cannot be enabled")
    validate_default_config(context)
    snapshot = validate_active_snapshot(context)
    from .db_access import preflight_db
    db_state = preflight_db(context)
    release = verify_release(context) if validate_release else {}
    return context.identity({**release, "schema_state": db_state, "revision": snapshot["revision"]})


def _release_source(root):
    """Keep paths relative to APP_ROOT in both portable layouts.

    Integration retains its frontend build inputs under build/control-web;
    production receives the same built assets under control-web. Only dist
    output is treated specially; legacy docs, examples, tests and the vendor
    development tree are never declared release payload.
    """
    return root, (root / "build/control-web").is_dir()


def _iter_release_files(root):
    source, exclude_web = _release_source(root)
    declared_roots = (
        "content_screening", "scripts", "schemas", "third_party",
    )
    declared_files = (
        "THIRD_PARTY_NOTICES.md",
        "launch.vbs", "启动工作台.bat", "打开规则控制端.cmd", "配置方舟密钥.cmd",
        "运行音乐节筛选.cmd",
    )
    def walk(folder):
        import os
        for parent, directories, filenames in os.walk(folder, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in {".git", "node_modules", "__pycache__", ".venv"})
            for name in sorted(filenames):
                path = Path(parent) / name
                if path.is_symlink():
                    continue
                relative = path.relative_to(source).as_posix()
                if relative == "control-web/dist" or relative.startswith("control-web/dist/") or relative == "build/control-web/dist" or relative.startswith("build/control-web/dist/"):
                    if relative.startswith("build/") != exclude_web:
                        continue
                    index = path.parent.parent / "index.html"
                    if not index.is_file():
                        raise RuntimeErrorBase("RELEASE_DIGEST_MISMATCH", "frontend entrypoint is missing")
                    if relative != "control-web/dist/index.html" and relative != "build/control-web/dist/index.html":
                        if path.name not in index.read_text(encoding="utf-8"):
                            continue
                    yield path
                    continue
                if relative.startswith("scripts/") and relative not in {"scripts/start_control.py", "scripts/task_mcp.py", "scripts/db_migrate.py", "scripts/mediacrawler_collect.py", "scripts/platform_collect.py", "scripts/vendor_preflight.py", "scripts/build_release.py"}:
                    continue
                if path.suffix in {".pyc", ".log", ".sqlite3", ".tsbuildinfo"}:
                    continue
                if path.name.endswith(".lock") and path.name != "package-lock.json":
                    continue
                yield path

    for name in declared_files:
        path = source / name
        if path.is_file() and not path.is_symlink():
            yield path
    for name in declared_roots:
        folder = source / name
        if folder.is_dir():
            yield from walk(folder)
    web_root = source / ("build/control-web/dist" if exclude_web else "control-web/dist")
    if web_root.is_dir():
        yield from walk(web_root)


def release_payload(context):
    """Hash exact declared files; unrelated retained files cannot alter identity."""
    source, exclude_web = _release_source(context.app_root)
    files = {}
    for path in _iter_release_files(context.app_root):
        relative = path.relative_to(source).as_posix()
        if exclude_web and relative.startswith("build/"):
            relative = relative.removeprefix("build/")
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files or "content_screening/runtime_context.py" not in files or not any(name.endswith("/dist/index.html") for name in files):
        raise RuntimeErrorBase("RELEASE_PAYLOAD_EMPTY", "release payload is missing required application or frontend files")
    return files


def build_digest(context):
    payload = release_payload(context)
    digest = hashlib.sha256()
    for relative, file_digest in sorted(payload.items(), key=lambda item: item[0].encode("utf-8")):
        digest.update(relative.encode("utf-8") + b"\0" + file_digest.encode("ascii") + b"\0")
    return digest.hexdigest(), len(payload)


def canonical_manifest_bytes(manifest):
    projection = dict(manifest)
    for key in ("manifest_digest", "self_digest", "frozen_at"):
        projection.pop(key, None)
    projection.get("release_identity", {}).pop("manifest_digest", None)
    return json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_release(context):
    path = context.app_path("integration_manifest.json")
    if not path.is_file():
        raise RuntimeErrorBase("RELEASE_DIGEST_MISMATCH", "release manifest is absent")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    declared = manifest.get("manifest_digest")
    actual_manifest = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
    try:
        actual_build, count = build_digest(context)
    except RuntimeErrorBase as error:
        raise RuntimeErrorBase("RELEASE_DIGEST_MISMATCH", "release payload is incomplete") from error
    deployment = context.deployment
    deployment_matches = all(manifest.get(key) == deployment[key] for key in ("service", "protocol_version", "backend_package", "base_commit"))
    instance_matches = manifest.get("instance_binding") == context.identity()["instance_binding"]
    if declared != actual_manifest or manifest.get("build_digest") != actual_build or manifest.get("file_count") != count or not deployment_matches or not instance_matches:
        raise RuntimeErrorBase("RELEASE_DIGEST_MISMATCH", "release identity, deployment binding or file set changed")
    return {"build_digest": actual_build, "manifest_digest": actual_manifest, "file_count": count}


_CURRENT = ContextVar("target_npl_runtime_context", default=None)


def current_context():
    context = _CURRENT.get()
    if context is None:
        context = RuntimeContext.load()
        _CURRENT.set(context)
    return context


def set_context(context):
    _CURRENT.set(context)


def clear_context():
    _CURRENT.set(None)
