"""Generate canonical release identity, then stop; never edits source files."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from content_screening.runtime_context import RuntimeContext, build_digest, canonical_manifest_bytes  # noqa: E402


def main():
    context = RuntimeContext.load(ROOT / "deployment.json")
    digest, count = build_digest(context)
    import subprocess
    try:
        head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, OSError):
        previous = json.loads((ROOT / "integration_manifest.json").read_text(encoding="utf-8"))
        head = previous["integration_head"]
    from datetime import datetime, timezone
    manifest = {
        "schema_version": 1,
        "base_commit": context.deployment["base_commit"],
        "integration_head": head,
        "service": context.deployment["service"],
        "protocol_version": context.deployment["protocol_version"],
        "backend_package": context.deployment["backend_package"],
        "instance_binding": context.identity()["instance_binding"],
        "build_digest": digest,
        "file_count": count,
        "frozen_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "path_normalization": "relative POSIX, UTF-8 byte order, raw SHA256 lowercase",
        "excluded": [".git", "node_modules", "__pycache__", ".venv", "integration_manifest.json", "runtime outputs"],
        "release_identity": {"build_digest": digest},
    }
    manifest["manifest_digest"] = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
    (ROOT / "integration_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "build_digest": digest, "manifest_digest": manifest["manifest_digest"], "file_count": count, "integration_head": head}, indent=2))


if __name__ == "__main__":
    main()
