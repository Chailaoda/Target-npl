"""Concurrency invariants for the single-file SQLite store.

The service runs a ThreadingHTTPServer, an analysis worker and a collection
orchestrator against one database file. These tests pin the pragma profile and
prove that interleaved writers plus concurrent readers never surface
"database is locked".
"""
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from content_screening.db_access import connect


class SqliteConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "concurrency.sqlite3"

    def seed(self):
        with closing(connect(self.db, provision=True)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER PRIMARY KEY, value TEXT)")
            db.commit()
        return self.db

    def test_wal_pragma_profile_is_applied(self):
        self.seed()
        with closing(connect(self.db)) as db:
            self.assertEqual(str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "wal")
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 1)
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 10000)

    def test_readonly_connection_refuses_writes(self):
        self.seed()
        with closing(connect(self.db, readonly=True)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO probe(value) VALUES ('blocked')")

    def test_wal_concurrent_read_write(self):
        self.seed()
        errors = []

        def writer(index):
            try:
                with closing(connect(self.db)) as db:
                    for row in range(50):
                        db.execute("INSERT INTO probe(value) VALUES (?)", (f"{index}-{row}",))
                        db.commit()
            except Exception as error:  # noqa: BLE001 - reported to the assertion below
                errors.append(error)

        def reader():
            try:
                with closing(connect(self.db, readonly=True)) as db:
                    for _ in range(100):
                        db.execute("SELECT count(*) FROM probe").fetchone()
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        self.assertEqual(errors, [])
        with closing(connect(self.db, readonly=True)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM probe").fetchone()[0], 200)


if __name__ == "__main__":
    unittest.main()
