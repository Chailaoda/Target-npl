"""Explicit migration / read-only preflight for the isolated unified DB."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from content_screening.runtime_context import RuntimeContext, RuntimeErrorBase  # noqa: E402
from content_screening.db_access import migrate, preflight_db, resolve_db  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes-really-migrate", action="store_true")
    args = parser.parse_args()
    try:
        context = RuntimeContext.load(args.deployment)
        path = resolve_db(context)
        if args.preflight:
            print(json.dumps({"status": "PASS", "schema_state": preflight_db(context), "db": str(path)}, ensure_ascii=False))
            return 0
        if not args.apply or not args.yes_really_migrate:
            raise RuntimeErrorBase("DB_MIGRATION_NOT_AUTHORISED", "migration requires --apply --yes-really-migrate")
        state = migrate(context)
        print(json.dumps({"status": "PASS", "schema_state": state, "db": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}, ensure_ascii=False))
        return 0
    except RuntimeErrorBase as error:
        print(json.dumps({"status": "BLOCKED", "code": error.code, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
