"""Create local configuration from neutral examples without overwriting edits.

The deployment manifest is portable: app_root/state_root are relative to the
file, so a checkout can be copied to another drive or machine and still pass
preflight after the release manifest is rebuilt.
"""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    'examples/policy.example.json': 'profiles/default.json',
    'examples/legacy-profile.example.json': 'profiles/legacy.json',
    'examples/collection.example.json': 'config/collection.json',
    'examples/connection.example.json': 'config/connection.json',
}


def _base_commit():
    """Keep a fresh manifest consistent with the recorded release base."""
    manifest = ROOT / 'integration_manifest.json'
    if manifest.is_file():
        try:
            recorded = json.loads(manifest.read_text(encoding='utf-8')).get('base_commit')
            if recorded:
                return recorded
        except (OSError, ValueError):
            pass
    try:
        return subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return ''


def _ensure_database(root=ROOT):
    """Create the authority schema once, explicitly, from the installer.

    Runtime preflight stays fail-closed: it never invents a missing database.
    Only this user-invoked bootstrap creates an empty migrated store.
    """
    root = Path(root).resolve()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from content_screening.db_access import provision_legacy, resolve_db
    from content_screening.runtime_context import RuntimeContext

    context = RuntimeContext.load(root / 'deployment.json')
    path = resolve_db(context)
    if path.is_file() and path.stat().st_size:
        print(f'Kept existing {path.relative_to(context.state_root).as_posix()}')
        return path
    provision_legacy(path)
    print(f'Created {path.relative_to(context.state_root).as_posix()} (migrated empty database)')
    return path


def main():
    for source, target in FILES.items():
        path = ROOT / target
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            print(f'Kept existing {target}')
            continue
        with path.open('xb') as stream:
            stream.write((ROOT / source).read_bytes())
        print(f'Created {target}')

    deployment = ROOT / 'deployment.json'
    if deployment.exists():
        print('Kept existing deployment.json')
    else:
        data = json.loads((ROOT / 'examples/deployment.example.json').read_text(encoding='utf-8'))
        data['base_commit'] = _base_commit()
        deployment.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('Created deployment.json (portable: app_root/state_root = ".")')
    _ensure_database()


if __name__ == '__main__':
    main()
