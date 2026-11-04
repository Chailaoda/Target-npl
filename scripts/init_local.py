"""Create local configuration from neutral examples without overwriting edits."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    'examples/policy.example.json': 'profiles/default.json',
    'examples/legacy-profile.example.json': 'profiles/legacy.json',
    'examples/collection.example.json': 'config/collection.json',
    'examples/connection.example.json': 'config/connection.json',
}

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

if __name__ == '__main__':
    main()
