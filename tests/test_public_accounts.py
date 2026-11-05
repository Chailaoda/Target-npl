"""Regression tests for WP0-WP3 of the public-account-profiles plan.

Covers the plan's section 6 scenarios relevant to this round:
- same platform + same account id, different nicknames -> one account
- same account id on different platforms -> never merged
- same nickname, different ids -> never merged
- nickname/profile updates -> no duplicate semantic analysis
- missing account id -> unknown, never invented
- legacy anonymized data imports keep working
- duplicate collection of the same comment -> no double interest accrual
- multi-platform source links validated per platform (A07)
"""
import json
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from content_screening.normalization import normalize, normalize_profile, public_url, read_export
from content_screening.pipeline import fingerprint
from content_screening.workflow import run, connect
from content_screening.account_repository import (account_key, search_accounts, account_overview,
                                                  attribution_conflicts, resolve_attribution_conflict,
                                                  analyzed_content_links)

POLICY = ROOT / "profiles/default.json"


def pub_row(platform="bilibili", cid="p1", aid="42", nick="UP主", text="开源Agent讨论，找搭子",
            published="2026-10-30T00:00:00+00:00"):
    return {"platform": platform, "object_type": "post",
            "data": {"content_id": cid, "text": text, "author_id": aid, "author": nick,
                     "author_profile_url": f"https://space.bilibili.com/{aid}" if platform == "bilibili" else "",
                     "anonymized": False, "published_at": published, "published_time_text": "", "url": ""}}


def anon_row(cid="c1", chash="abc", text="匿名内容示例活动"):
    return {"object_type": "comment",
            "data": {"comment_id": cid, "aweme_id": "1", "content": text, "parent_comment_id": "",
                     "creator_hash": chash, "nickname": "示***例", "create_time": 1793352000}}


def douyin_post_fork(aid="dy-123", nick="抖音作者", cid="7000000000000000001", text="开源Agent实战分享"):
    """Raw Douyin aweme record as emitted by the controlled fork (author fields present)."""
    return {"object_type": "post", "keyword": "Agent",
            "data": {"aweme_id": cid, "desc": text, "create_time": 1793352000,
                     "creator_hash": "legacy-hash", "nickname": "匿***称",
                     "author_id": aid, "author_nickname": nick,
                     "author_profile_url": f"https://www.douyin.com/user/{aid}",
                     "statistics": {"digg_count": 5}}}


def douyin_comment_fork(aid="dy-123", nick="抖音作者", cid="7000000000000000002", text="求教程，想搭子一起学"):
    """Raw Douyin comment record as emitted by the controlled fork."""
    return {"object_type": "comment", "keyword": "Agent",
            "data": {"comment_id": cid, "aweme_id": "7000000000000000001", "content": text,
                     "parent_comment_id": "0", "create_time": 1793352100,
                     "creator_hash": "legacy-hash", "nickname": "匿***称",
                     "author_id": aid, "author_nickname": nick}}


class DouyinForkPassthroughTests(unittest.TestCase):
    """P0: raw Douyin (aweme_id) records from the controlled fork must reach
    the new accounts pipeline — while legacy anonymized rows stay identical."""

    def test_fork_douyin_post_takes_public_path(self):
        record = normalize(douyin_post_fork())
        self.assertEqual(record["author_key"], "douyin:dy-123")
        self.assertEqual(record["identity_mode"], "public_source")
        self.assertEqual(record["content_id"], "7000000000000000001")
        # Display fields never join the semantic record (cache stability).
        self.assertEqual(record["author"], "")
        self.assertEqual(record["author_url"], "")
        profile = normalize_profile(douyin_post_fork())
        self.assertEqual(profile["account_id"], "dy-123")
        self.assertEqual(profile["nickname"], "抖音作者")
        self.assertEqual(profile["profile_url"], "https://www.douyin.com/user/dy-123")

    def test_fork_douyin_comment_takes_public_path(self):
        record = normalize(douyin_comment_fork())
        self.assertEqual(record["author_key"], "douyin:dy-123")
        self.assertEqual(record["identity_mode"], "public_source")
        self.assertEqual(record["root_id"], "7000000000000000001")
        self.assertEqual(record["parent_id"], "")  # "0" normalized to ""
        profile = normalize_profile(douyin_comment_fork())
        self.assertEqual(profile["account_id"], "dy-123")

    def test_douyin_post_and_comment_same_author_aggregate(self):
        records = [normalize(douyin_post_fork()), normalize(douyin_comment_fork())]
        self.assertEqual(len({r["author_key"] for r in records}), 1)

    def test_legacy_douyin_without_fork_fields_stays_anonymized(self):
        # Upstream-original records (no author_id) must be byte-identical to
        # the pre-WP2 behavior: anonymous key, masked nickname in record.
        record = normalize(anon_row())
        self.assertEqual(record["author_key"], "anonymous:abc")
        self.assertEqual(record["identity_mode"], "upstream_anonymized")
        self.assertEqual(record["author"], "示***例")
        self.assertIsNone(normalize_profile(anon_row()))

    def test_douyin_end_to_end_accounts_write(self):
        # Full chain: raw fork rows -> run() -> accounts + content links.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source = root / "in.jsonl"
            rows = [douyin_post_fork(), douyin_comment_fork(), anon_row()]
            source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            db_path = root / "db.sqlite"
            run(source, POLICY, db_path, root / "out", mode="rules")
            db = connect(db_path)
            accounts = db.execute("SELECT account_key,nickname,profile_url FROM accounts").fetchall()
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["account_key"], "douyin:dy-123")
            self.assertEqual(accounts[0]["nickname"], "抖音作者")
            self.assertEqual(accounts[0]["profile_url"], "https://www.douyin.com/user/dy-123")
            links = sorted(l["content_id"] for l in db.execute("SELECT content_id FROM account_content_links"))
            self.assertEqual(links, ["7000000000000000001", "7000000000000000002"])
            db.close()


class PublicAccountNormalizationTests(unittest.TestCase):
    def test_cross_platform_anonymous_hashes_never_share_group_key(self):
        # P1 review item: anonymous:hash keys are NOT globally unique. The
        # same hash on two platforms must produce two distinct display keys.
        bili = normalize(anon_row(chash="abc"))
        # A douyin comment already uses platform "douyin"; simulate the same
        # hash reaching the view layer from a different platform context.
        from content_screening.live_view import _person_key
        self.assertEqual(_person_key({"platform": "douyin", "author_key": bili["author_key"], "id": "1"}),
                         "douyin:anonymous:abc")
        self.assertEqual(_person_key({"platform": "xhs", "author_key": bili["author_key"], "id": "2"}),
                         "xhs:anonymous:abc")
        self.assertNotEqual(_person_key({"platform": "douyin", "author_key": "anonymous:abc", "id": "1"}),
                             _person_key({"platform": "xhs", "author_key": "anonymous:abc", "id": "2"}))

    def test_same_id_different_platforms_never_merge(self):
        bili = normalize(pub_row(platform="bilibili"))
        xhs = normalize(pub_row(platform="xhs", aid="42", nick="同名不同人"))
        self.assertEqual(bili["author_key"], "bilibili:42")
        self.assertEqual(xhs["author_key"], "xhs:42")
        self.assertNotEqual(bili["author_key"], xhs["author_key"])

    def test_same_nickname_different_ids_never_merge(self):
        a = normalize(pub_row(aid="1", nick="同一个名字"))
        b = normalize(pub_row(cid="p2", aid="2", nick="同一个名字"))
        self.assertEqual(a["author_key"], "bilibili:1")
        self.assertEqual(b["author_key"], "bilibili:2")
        self.assertNotEqual(a["author_key"], b["author_key"])

    def test_missing_id_stays_unknown_and_never_invented(self):
        row = pub_row(aid="", nick="随便起的名字")
        row["data"]["author_id"] = ""
        result = normalize(row)
        self.assertEqual(result["author_key"], "")
        self.assertEqual(result["identity_mode"], "unknown")
        self.assertIsNone(normalize_profile(row))

    def test_nickname_update_keeps_semantic_fingerprint(self):
        a = normalize(pub_row(nick="旧名字"))
        b = normalize(pub_row(nick="新名字"))
        self.assertEqual(fingerprint(a), fingerprint(b))
        pa, pb = normalize_profile(pub_row(nick="旧名字")), normalize_profile(pub_row(nick="新名字"))
        self.assertEqual(pa["nickname"], "旧名字")
        self.assertEqual(pb["nickname"], "新名字")

    def test_profile_url_update_keeps_semantic_fingerprint(self):
        row_b = pub_row(); row_b["data"]["author_profile_url"] = "https://space.bilibili.com/other"
        self.assertEqual(fingerprint(normalize(pub_row())), fingerprint(normalize(row_b)))

    def test_legacy_anonymized_record_normalizes_byte_identically(self):
        result = normalize(anon_row())
        expected = {"platform": "douyin", "object_type": "comment", "content_id": "c1", "text": "匿名内容示例活动",
                    "author": "示***例", "author_key": "anonymous:abc", "author_url": "",
                    "identity_mode": "upstream_anonymized", "url": "https://www.douyin.com/video/1",
                    "root_id": "1", "parent_id": "", "published_at": "2026-10-30T09:20:00+00:00",
                    "published_time_text": "1793352000", "normalization_version": "1",
                    "text_status": "available", "synthetic": False}
        self.assertEqual(result, expected)

    def test_public_url_per_platform_validation(self):
        # A07: cross-platform links validated against their own platform hosts
        self.assertEqual(public_url("https://space.bilibili.com/42", "bilibili"), "https://space.bilibili.com/42")
        self.assertEqual(public_url("https://space.bilibili.com/42", "xhs"), "")
        self.assertEqual(public_url("https://www.zhihu.com/people/tok", "zhihu"), "https://www.zhihu.com/people/tok")
        self.assertEqual(public_url("javascript:alert(1)", "bilibili"), "")

    def test_tokens_never_reach_normalized_output(self):
        row = pub_row()
        row["data"]["xsec_token"] = "never-persist-token"
        result = normalize(row)
        self.assertNotIn("never-persist-token", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("xsec", json.dumps(result))


class AccountRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "db.sqlite"
        self._connections = []
        self.addCleanup(self._close_connections)

    def _close_connections(self):
        for db in self._connections:
            try:
                db.close()
            except sqlite3.Error:
                pass

    def _db(self):
        db = connect(self.db_path)
        self._connections.append(db)
        return db

    def _import(self, rows, authority="raw_import"):
        source = self.root / f"in{len(list(self.root.glob('in*.jsonl')))}.jsonl"
        source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return run(source, POLICY, self.db_path, self.root / "out", mode="rules",
                   attribution_authority=authority)

    def _owner(self, cid="p1"):
        return self._db().execute(
            "SELECT account_key,attribution_authority FROM account_content_links WHERE content_id=?",
            (cid,)).fetchone()

    def test_same_account_multiple_contents_aggregate(self):
        self._import([pub_row(cid="p1", aid="42", nick="UP主"), pub_row(cid="p2", aid="42", nick="UP主")])
        db = self._db()
        accounts = db.execute("SELECT account_key FROM accounts").fetchall()
        self.assertEqual(len(accounts), 1)
        links = db.execute("SELECT content_id FROM account_content_links").fetchall()
        self.assertEqual(sorted(l["content_id"] for l in links), ["p1", "p2"])

    def test_text_change_triggers_reanalysis(self):
        # Inverse of the rename test: when the actual content text changes the
        # semantic fingerprint must change, so a NEW analysis row is produced.
        # Otherwise the cache optimization would serve stale results.
        self._import([pub_row(cid="p1", aid="42", text="开源Agent讨论，找搭子")])
        self._import([pub_row(cid="p1", aid="42", text="完全不同的正文，讲的是别的话题")])
        db = self._db()
        sources = db.execute("SELECT count(*) FROM v2_sources").fetchone()[0]
        self.assertEqual(sources, 2)  # two distinct semantic records
        analyses = db.execute("SELECT count(*) FROM v2_analyses").fetchone()[0]
        self.assertEqual(analyses, 2)  # both were analyzed, no stale reuse
        accounts = db.execute("SELECT account_key FROM accounts").fetchall()
        self.assertEqual(len(accounts), 1)  # same account either way

    def test_rename_updates_account_without_reanalysis(self):
        self._import([pub_row(cid="p1", aid="42", nick="旧名字")])
        db = self._db()
        before = (db.execute("SELECT count(*) FROM v2_sources").fetchone()[0],
                  db.execute("SELECT count(*) FROM v2_analyses").fetchone()[0],                  sorted(r[0] for r in db.execute("SELECT source_hash FROM v2_sources")))
        self._import([pub_row(cid="p1", aid="42", nick="新名字")])
        after = (db.execute("SELECT count(*) FROM v2_sources").fetchone()[0],
                 db.execute("SELECT count(*) FROM v2_analyses").fetchone()[0],
                 sorted(r[0] for r in db.execute("SELECT source_hash FROM v2_sources")))
        self.assertEqual(before, after)  # zero re-analysis: cache intact
        account = db.execute("SELECT nickname FROM accounts WHERE account_key='bilibili:42'").fetchone()
        self.assertEqual(account["nickname"], "新名字")
        history = db.execute("SELECT nickname FROM account_profile_history WHERE account_key='bilibili:42' ORDER BY id").fetchall()
        self.assertEqual([h["nickname"] for h in history], ["旧名字", "新名字"])

    def test_duplicate_collection_does_not_double_link(self):
        # Same comment re-collected in a later batch: links stay unique.
        self._import([pub_row(cid="p1", aid="42")])
        self._import([pub_row(cid="p1", aid="42")])
        db = self._db()
        links = db.execute("SELECT count(*) FROM account_content_links").fetchone()[0]
        self.assertEqual(links, 1)

    def test_anonymous_rows_never_create_accounts(self):
        self._import([anon_row(cid="c1", chash="abc"), anon_row(cid="c2", chash="abc")])
        db = self._db()
        self.assertEqual(db.execute("SELECT count(*) FROM accounts").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM account_content_links").fetchone()[0], 0)

    def test_text_change_repoints_content_link(self):
        # P1: after a content edit produces a new source_hash, the account's
        # content link must point at the LATEST version - account overviews
        # and interest profiling always read current content.
        self._import([pub_row(cid="p1", aid="42", text="开源Agent讨论，找搭子")])
        db = self._db()
        old_hash = db.execute(
            "SELECT source_hash FROM account_content_links WHERE content_id='p1'").fetchone()["source_hash"]
        self._import([pub_row(cid="p1", aid="42", text="完全不同的新正文，讲别的")])
        rows = db.execute(
            "SELECT source_hash FROM account_content_links WHERE content_id='p1'").fetchall()
        self.assertEqual(len(rows), 1)  # one link, not duplicated
        self.assertNotEqual(rows[0]["source_hash"], old_hash)  # repointed
        latest = db.execute("SELECT source_hash FROM v2_sources WHERE source_json LIKE ?",
                            ('%完全不同的新正文%',)).fetchone()
        self.assertEqual(rows[0]["source_hash"], latest["source_hash"])  # points at current text
        # The old v2_sources row survives for audit history.
        self.assertIsNotNone(db.execute(
            "SELECT source_hash FROM v2_sources WHERE source_hash=?", (old_hash,)).fetchone())

    def test_account_content_link_and_profile_kept_on_partial_update(self):
        # P2: a later batch missing the nickname/profile_url must never erase
        # known values (missing != explicitly cleared).
        self._import([pub_row(cid="p1", aid="42", nick="完整昵称")])
        partial = pub_row(cid="p2", aid="42", nick="")
        partial["data"]["author_profile_url"] = ""
        self._import([partial])
        db = self._db()
        account = db.execute("SELECT nickname,profile_url FROM accounts WHERE account_key='bilibili:42'").fetchone()
        self.assertEqual(account["nickname"], "完整昵称")  # kept, not overwritten with ""
        self.assertEqual(account["profile_url"], "https://space.bilibili.com/42")

    def test_conflicting_author_is_recorded_not_overwritten(self):
        # P1 (review round 4): a plain import must NOT silently take ownership
        # away from a different account. The conflict is left for confirmation.
        self._import([pub_row(cid="p1", aid="42", nick="原作者")])
        self.assertEqual(self._owner()["account_key"], "bilibili:42")
        self._import([pub_row(cid="p1", aid="99", nick="修正后的作者")])
        self.assertEqual(self._owner()["account_key"], "bilibili:42")  # unchanged
        db = self._db()
        conflicts = attribution_conflicts(db)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["candidate_account_key"], "bilibili:99")
        self.assertEqual(conflicts[0]["status"], "pending")
        self.assertEqual(conflicts[0]["candidate_authority"], "raw_import")
        self.assertEqual(db.execute("SELECT count(*) FROM account_content_links").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM accounts").fetchone()[0], 2)

    def test_higher_authority_reattribution_moves_link_and_audits(self):
        # Automatic reattribution is allowed ONLY with an explicitly declared
        # higher source priority, and must leave a resolved audit record.
        self._import([pub_row(cid="p1", aid="42")])
        self._import([pub_row(cid="p1", aid="99")], authority="platform_recheck")
        owner = self._owner()
        self.assertEqual(owner["account_key"], "bilibili:99")
        self.assertEqual(owner["attribution_authority"], "platform_recheck")
        db = self._db()
        self.assertEqual(db.execute("SELECT count(*) FROM account_content_links").fetchone()[0], 1)
        resolved = attribution_conflicts(db, status="resolved")
        self.assertTrue(any(r["candidate_account_key"] == "bilibili:99" for r in resolved))

    def test_replay_of_old_batch_cannot_revert_confirmed_attribution(self):
        # Review round 4 blocking case: A -> B (corrected) -> replay of A.
        self._import([pub_row(cid="p1", aid="42", text="原文内容")])
        self._import([pub_row(cid="p1", aid="99", text="原文内容")], authority="platform_recheck")
        self.assertEqual(self._owner()["account_key"], "bilibili:99")
        self._import([pub_row(cid="p1", aid="42", text="原文内容")])  # stale batch, raw_import
        owner = self._owner()
        self.assertEqual(owner["account_key"], "bilibili:99")  # NOT reverted
        self.assertEqual(owner["attribution_authority"], "platform_recheck")
        db = self._db()
        self.assertTrue(any(r["candidate_account_key"] == "bilibili:42" for r in attribution_conflicts(db)))
        self.assertEqual(db.execute("SELECT count(*) FROM account_content_links").fetchone()[0], 1)
        # Repeated replays must not pile up duplicate conflict rows.
        self._import([pub_row(cid="p1", aid="42", text="原文内容")])
        self.assertEqual(len(attribution_conflicts(db)), 1)

    def test_manual_resolution_and_low_authority_cannot_downgrade(self):
        self._import([pub_row(cid="p1", aid="42")])
        self._import([pub_row(cid="p1", aid="99")])
        db = self._db()
        resolve_attribution_conflict(db, "bilibili", "post", "p1", "bilibili:99",
                                     authority="manual_review", note="页面复核确认作者")
        self.assertEqual(self._owner()["account_key"], "bilibili:99")
        self.assertEqual(self._owner()["attribution_authority"], "manual_review")
        self.assertEqual(attribution_conflicts(db), [])  # all resolved
        # A raw replay creates a pending conflict but cannot revert the owner.
        self._import([pub_row(cid="p1", aid="42")])
        self.assertEqual(self._owner()["account_key"], "bilibili:99")
        with self.assertRaises(ValueError):  # cannot downgrade manual_review
            resolve_attribution_conflict(db, "bilibili", "post", "p1", "bilibili:42",
                                         authority="raw_import")
        with self.assertRaises(ValueError):  # unknown account is not a valid choice
            resolve_attribution_conflict(db, "bilibili", "post", "p1", "bilibili:nope")

    def test_reopened_conflict_after_resolution_is_recorded_again(self):
        # A once-resolved conflict must be re-raised when the stale candidate
        # is replayed, without hitting a UNIQUE violation or silently vanishing.
        self._import([pub_row(cid="p1", aid="42")])
        self._import([pub_row(cid="p1", aid="99")])
        db = self._db()
        resolve_attribution_conflict(db, "bilibili", "post", "p1", "bilibili:42",
                                     authority="manual_review", note="复核确认仍是原作者")
        self.assertEqual(attribution_conflicts(db), [])
        self._import([pub_row(cid="p1", aid="99")])  # stale candidate replayed
        pending = attribution_conflicts(db)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["candidate_account_key"], "bilibili:99")
        self.assertEqual(pending[0]["status"], "pending")  # re-opened
        self.assertEqual(self._owner()["account_key"], "bilibili:42")

    def test_unknown_authority_is_rejected(self):
        # Trust level must be explicit and from the allowlist - never invented.
        with self.assertRaises(ValueError):
            self._import([pub_row()], authority="superuser")

    def test_analyzed_content_links_exclude_unanalyzed_content(self):
        # P2 (WP4 contract): workflow.run() writes links BEFORE model analysis.
        # A failed model call leaves a link with no v2_analyses row; WP4 must
        # never treat that link as validated interest evidence.
        self._import([pub_row(cid="p1", aid="42")])
        db = self._db()
        link = db.execute("SELECT source_hash FROM account_content_links WHERE content_id='p1'").fetchone()
        db.execute("DELETE FROM v2_analyses WHERE source_hash=?", (link["source_hash"],))
        db.commit()
        self.assertEqual(analyzed_content_links(db, "bilibili:42"), [])
        self.assertEqual(account_overview(db, "bilibili:42")["content_count"], 1)  # link still there
        db.execute("INSERT INTO v2_analyses VALUES (?,?,?,?,?,?)",
                   ("analysis-key-1", link["source_hash"], "policy", "{}",
                    '{"ok":true}', "2026-11-05T00:00:00+00:00"))
        db.commit()
        rows = analyzed_content_links(db, "bilibili:42")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content_id"], "p1")

    def test_failed_account_ingest_rolls_back_whole_batch(self):
        # P1: repository writes now run INSIDE workflow.run()'s transaction.
        # If the second account fails mid-import, the entire batch (sources,
        # tasks, observations, accounts, links) must roll back together -
        # no partial import state.
        import unittest.mock as mock
        from content_screening.account_repository import _upsert_account_inner
        real = _upsert_account_inner
        seen = []

        def flaky(db, key, profile, nickname, profile_url, row, seen_at):
            if seen:
                raise sqlite3.OperationalError("injected failure on second account")
            seen.append(key)
            return real(db, key, profile, nickname, profile_url, row, seen_at)

        db = self._db()
        with mock.patch("content_screening.account_repository._upsert_account_inner", side_effect=flaky):
            try:
                self._import([pub_row(cid="p1", aid="42", nick="第一个账号"),
                              pub_row(cid="p2", aid="77", nick="第二个账号")])
            except sqlite3.OperationalError:
                pass
            else:
                self.fail("injected failure should propagate")
        # Whole batch rolled back: no sources, no analyses, no accounts, no links.
        db2 = self._db()
        for table in ("v2_sources", "v2_analyses", "accounts", "account_content_links"):
            count = db2.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            self.assertEqual(count, 0, f"{table} leaked rows after failed batch")

    def test_active_days_are_calendar_dates(self):
        # P2: same UTC date with different timestamps counts as ONE active day.
        self._import([pub_row(cid="p1", aid="42", published="2026-10-30T01:00:00+00:00"),
                      pub_row(cid="p2", aid="42", published="2026-10-30T15:00:00+00:00"),
                      pub_row(cid="p3", aid="42", published="2026-10-31T09:00:00+00:00")])
        db = self._db()
        overview = account_overview(db, "bilibili:42")
        self.assertEqual(overview["content_count"], 3)
        self.assertEqual(overview["active_days"], 2)  # Oct 30 + Oct 31, not 3

    def test_active_days_normalize_non_utc_offsets(self):
        # P2: +08:00 timestamps must be converted to UTC dates, not string-sliced.
        # 2026-10-30 22:00 +08:00 == 2026-10-30 14:00 UTC (same UTC date)
        # 2026-10-31 02:00 +08:00 == 2026-10-30 18:00 UTC (PREVIOUS UTC date!)
        self._import([pub_row(cid="p1", aid="42", published="2026-10-30T22:00:00+08:00"),
                      pub_row(cid="p2", aid="42", published="2026-10-31T02:00:00+08:00"),
                      pub_row(cid="p3", aid="42", published="2026-10-30T14:00:00+00:00")])
        db = self._db()
        overview = account_overview(db, "bilibili:42")
        self.assertEqual(overview["active_days"], 1)  # all three are ONE UTC date

    def test_overview_and_search(self):
        self._import([pub_row(cid="p1", aid="42"), pub_row(cid="p2", aid="42")])
        db = self._db()
        overview = account_overview(db, "bilibili:42")
        self.assertEqual(overview["account"]["nickname"], "UP主")
        self.assertEqual(overview["content_count"], 2)
        self.assertEqual(overview["active_days"], 1)
        found, total = search_accounts(db, "UP主")
        self.assertEqual((total, found[0]["account_key"]), (1, "bilibili:42"))


class UpstreamIntegrityTests(unittest.TestCase):
    """P2: the __pycache__ exception must match path COMPONENTS, not substrings."""

    def setUp(self):
        from scripts.mediacrawler_collect import _is_pycache_entry
        self.is_pycache = _is_pycache_entry

    def test_real_pycache_directories_are_tolerated(self):
        self.assertTrue(self.is_pycache("?? tools/__pycache__/x.cpython-313.pyc"))
        self.assertTrue(self.is_pycache(" M store/douyin/__pycache__/y.pyc"))
        self.assertTrue(self.is_pycache("?? __pycache__/z.pyc"))

    def test_disguised_filenames_are_rejected(self):
        # A file NAMED like the cache dir but not inside one must be rejected.
        self.assertFalse(self.is_pycache("?? __pycache__-notes.md"))
        self.assertFalse(self.is_pycache("?? evil__pycache__.py"))
        self.assertFalse(self.is_pycache(" M docs/__pycache__readme.md"))
        self.assertFalse(self.is_pycache("?? real_source.py"))

    def test_non_bytecode_files_in_cache_dir_are_rejected(self):
        # P2: the exception must be scoped to real bytecode cache FILES, not
        # any arbitrary file planted inside a __pycache__ directory.
        self.assertFalse(self.is_pycache("?? tools/__pycache__/evil.txt"))
        self.assertFalse(self.is_pycache("?? tools/__pycache__/README.md"))
        self.assertFalse(self.is_pycache("?? tools/__pycache__/injected.py"))
        self.assertFalse(self.is_pycache("?? tools/__pycache__/sub/deeper/evil.pyc"))
        self.assertFalse(self.is_pycache("?? __pycache__/"))

    def test_verify_upstream_filters_only_real_bytecode_files(self):
        # .gitignore usually hides __pycache__, but cache paths can still be
        # reported (force-added files, missing ignore rules). Feed synthetic
        # status output so the filter is verified independently of local
        # .gitignore behaviour.
        import unittest.mock as mock
        from scripts import mediacrawler_collect as mc
        sha = mc.ALLOWED_UPSTREAM_COMMITS[0]

        def verify_with(status):
            def fake(cmd, *args, **kwargs):
                return status if "status" in cmd else sha + "\n"  # rev-parse
            with mock.patch.object(mc.subprocess, "check_output", side_effect=fake):
                return mc.verify_upstream(Path("unused"), expected=sha)

        self.assertEqual(verify_with("?? tools/__pycache__/x.cpython-313.pyc\n"), sha)
        with self.assertRaises(ValueError):
            verify_with("?? tools/__pycache__/evil.txt\n")
        with self.assertRaises(ValueError):
            verify_with("?? tools/__pycache__/injected.py\n")


class MigrationTests(unittest.TestCase):
    def test_migrate_rollback_and_reapply(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite"
            sqlite3.connect(db_path).close()
            for expect in ("迁移完成", "回滚完成", "迁移完成"):
                args = [sys.executable, "scripts/db_migrate.py", "--db", str(db_path)]
                if expect == "回滚完成":
                    args.append("--rollback")
                result = subprocess.run(args, capture_output=True, text=True)
                self.assertIn(expect, result.stdout, result.stderr)
            with closing(sqlite3.connect(db_path)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("accounts", tables)
                self.assertIn("interest_profiles", tables)

    def test_legacy_links_table_upgraded_in_place(self):
        # WP3 databases predate attribution_authority + the conflicts table.
        # migrate() must ALTER in place and keep existing link rows intact.
        from content_screening.account_repository import migrate
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "legacy.sqlite"
            with closing(sqlite3.connect(db_path)) as db:
                db.executescript("""
                    CREATE TABLE accounts(account_key TEXT PRIMARY KEY, platform TEXT,
                        account_id TEXT, nickname TEXT, profile_url TEXT,
                        identity_source TEXT, identity_mode TEXT,
                        first_seen_at TEXT, last_seen_at TEXT);
                    CREATE TABLE account_content_links(account_key TEXT, platform TEXT,
                        object_type TEXT, content_id TEXT, source_hash TEXT,
                        first_seen_at TEXT, last_seen_at TEXT,
                        PRIMARY KEY(account_key,platform,object_type,content_id));
                """)
                db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?)",
                           ("bilibili:42", "bilibili", "42", "UP主", "", "", "public_source", "t0", "t0"))
                db.execute("INSERT INTO account_content_links VALUES (?,?,?,?,?,?,?)",
                           ("bilibili:42", "bilibili", "post", "p1", "hash-old", "t0", "t0"))
                db.commit()
            with closing(sqlite3.connect(db_path)) as db:
                migrate(db)
                cols = {r[1] for r in db.execute("PRAGMA table_info(account_content_links)")}
                self.assertIn("attribution_authority", cols)
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("account_attribution_conflicts", tables)
                row = db.execute("SELECT * FROM account_content_links").fetchone()
                self.assertEqual(row[0], "bilibili:42")
                self.assertEqual(row[4], "hash-old")  # old data preserved
                self.assertEqual(row[7], "raw_import")  # safe default for legacy rows

    def test_interrupted_migration_recovers_and_keeps_data(self):
        # P1: a half-created schema (migration interrupted midway) must be
        # repairable by re-running the migration, and existing v2_* rows must
        # survive untouched.
        import subprocess
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "db.sqlite"
            # Build a partially migrated database: full migration first, then
            # rollback, then simulate a crash by dropping only one table.
            sqlite3.connect(db_path).close()
            def migrate(*extra):
                return subprocess.run([sys.executable, "scripts/db_migrate.py",
                                       "--db", str(db_path), *extra],
                                      capture_output=True, text=True)
            self.assertIn("迁移完成", migrate().stdout)
            # Seed a row in a migrated table, then simulate an interrupted
            # migration where a later table is missing.
            with closing(sqlite3.connect(db_path)) as db:
                db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?)",
                           ("bilibili:42", "bilibili", "42", "UP主",
                            "https://space.bilibili.com/42", "platform_public_content_api",
                            "public_source", "2026-11-05T00:00:00+00:00",
                            "2026-11-05T00:00:00+00:00"))
                db.commit()
            with closing(sqlite3.connect(db_path)) as db:
                db.execute("DROP TABLE interest_evidence")
                db.commit()
            # Re-running the migration repairs the schema without harming data.
            self.assertIn("迁移完成", migrate().stdout)
            with closing(sqlite3.connect(db_path)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("interest_evidence", tables)  # repaired
                rows = db.execute("SELECT nickname FROM accounts").fetchall()
                self.assertEqual([r[0] for r in rows], ["UP主"])  # untouched


if __name__ == "__main__":
    unittest.main()
