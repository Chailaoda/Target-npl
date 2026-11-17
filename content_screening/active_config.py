"""Atomic pointer to immutable policy + collection snapshots."""
import json
from pathlib import Path
from .pipeline import fingerprint

ROOT = Path(__file__).resolve().parents[1]


def active_snapshot(root=None):
    if root is None:
        from .runtime_context import current_context
        root=current_context().state_root
    root = Path(root)
    pointer = root / "config/control-active.json"
    if not pointer.exists():
        policy,collection=root / "profiles/default.json",root / "config/collection.json"
        return {"revision":"baseline","policy_path":policy,"collection_path":collection,"bundle":{"policy":json.loads(policy.read_text(encoding='utf-8-sig')),"collection":json.loads(collection.read_text(encoding='utf-8-sig'))}}
    p = json.loads(pointer.read_text(encoding="utf-8"))
    revision = p.get("revision", "")
    if len(revision) != 32 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError("控制端版本指针无效")
    folder = root / "data/control/versions" / revision
    bundle = {"policy":json.loads((folder / "policy.json").read_text(encoding="utf-8")), "collection":json.loads((folder / "collection.json").read_text(encoding="utf-8"))}
    if (folder/'task.json').exists():bundle['task']=json.loads((folder/'task.json').read_text(encoding='utf-8'))
    if fingerprint(bundle) != p.get("bundle_hash"):
        raise ValueError("控制端配置快照校验失败")
    return {"revision":revision,"policy_path":folder/"policy.json","collection_path":folder/"collection.json","bundle":bundle}


def active_paths(root=None):
    snapshot=active_snapshot(root)
    return snapshot['policy_path'],snapshot['collection_path']
