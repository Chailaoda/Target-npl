"""Shared isolated WP4 fixture helpers (not collected as tests)."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from content_screening.account_repository import migrate
from content_screening.interest_service import InterestService
from content_screening.runtime_context import RuntimeContext, set_context, clear_context
from content_screening.workflow import run


def public_row(cid, account_id, text, published="2026-11-01T00:00:00+00:00", platform="bilibili", **extra):
    data = {"content_id": cid, "text": text, "author_id": account_id, "author": "公开账号",
            "author_profile_url": f"https://space.bilibili.com/{account_id}", "anonymized": False,
            "published_at": published, "url": f"https://www.bilibili.com/video/{cid}", "synthetic": True, **extra}
    return {"platform": platform, "object_type": "post", "data": data}


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        self.db_path = self.root / "data/festival.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(self.db_path).close()
        from content_screening.db_access import V2_SCHEMA, LEGACY_SCHEMA
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(V2_SCHEMA + LEGACY_SCHEMA)
            db.execute("ALTER TABLE v2_jobs ADD COLUMN task_json TEXT")
            db.execute("ALTER TABLE v2_jobs ADD COLUMN config_revision TEXT")
            migrate(db)
        self.context = RuntimeContext(self.root / "deployment.json", ROOT, self.root,
                                      {"test_isolated": True, "database": {"relative_path": "data/festival.sqlite3"}})
        set_context(self.context)
        self.service = InterestService(self.context)

    def import_rows(self, rows):
        source = self.root / f"source-{len(list(self.root.glob('*.jsonl')))}.jsonl"
        source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return run(source, ROOT / "profiles/default.json", self.db_path, self.root / "out", mode="rules")

    def close(self):
        clear_context()


def make_fixture():
    temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp", ignore_cleanup_errors=True)
    temporary.cleanup = lambda: None
    temporary._finalizer.detach()
    fixture = Fixture(temporary.name)
    return temporary, fixture
