"""Create local configuration from neutral examples without overwriting edits.

The deployment manifest is portable: app_root/state_root are relative to the
file, so a checkout can be copied to another drive or machine and still pass
preflight after the release manifest is rebuilt.
"""
import json
from pathlib import Path
import subprocess

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


if __name__ == '__main__':
    main()
