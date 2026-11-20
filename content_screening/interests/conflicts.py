"""Attribution conflict review: list pending conflicts and confirm an owner.

This is not interest scoring. A conflict is about *who said it*, so the read
model joins the live content link (current authority) with the recorded
candidate, and the write path delegates to account_repository so the
raw_import < platform_recheck < manual_review order stays the single source of
truth.
"""
from contextlib import closing
import sqlite3

from ..interests import repository as repo
from ..interests.scoring import utcnow


class ConflictReview:
    def __init__(self, context):
        self.context = context

    def _connect(self, readonly=False):
        from ..db_access import connect
        return connect(context=self.context, readonly=readonly)

    def list_conflicts(self, status="pending", account_key=None, limit=50):
        """Read-only view for the review workbench."""
        from ..account_repository import attribution_conflicts
        if status not in ("pending", "resolved", "all"):
            raise ValueError("冲突状态筛选无效")
        if isinstance(limit, str) and limit.isdigit():
            limit = int(limit)
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("冲突数量上限应为 1–200 的整数")
        if account_key is not None and (not isinstance(account_key, str) or len(account_key) > 200):
            raise ValueError("账号标识无效")
        with closing(self._connect(readonly=True)) as db:
            rows = []
            for state in (("pending", "resolved") if status == "all" else (status,)):
                rows += [dict(r) for r in attribution_conflicts(db, status=state, account_key=account_key)]
            rows.sort(key=lambda r: r["id"], reverse=True)
            pending_total = sum(1 for r in rows if r["status"] == "pending")
            items = [self._view(db, row) for row in rows[:limit]]
        return {"items": items, "total_pending": pending_total}

    def _view(self, db, row):
        link = repo.conflict_link(db, row["platform"], row["object_type"], row["content_id"], row["current_account_key"])
        # Older conflict rows were written before source hashes were recorded;
        # the live link still points at the text the operator has to judge.
        snippet = repo.conflict_snippet(
            db, row["candidate_source_hash"], row["current_source_hash"],
            link["source_hash"] if link else "")
        return {
            "conflict_id": row["id"],
            "platform": row["platform"],
            "object_type": row["object_type"],
            "content_id": row["content_id"],
            "current_key": row["current_account_key"],
            "current_authority": link["attribution_authority"] if link else "",
            "candidate_key": row["candidate_account_key"],
            "candidate_authority": row["candidate_authority"],
            "snippet": snippet,
            "detected_at": row["first_seen_at"],
            "status": row["status"],
            "resolution": row["resolution"],
        }

    def resolve_conflict(self, platform, object_type, content_id, chosen_key, note=""):
        """Confirm one attribution by hand; authority is always manual_review."""
        from ..account_repository import resolve_attribution_conflict
        for name, value in (("platform", platform), ("object_type", object_type),
                            ("content_id", content_id), ("chosen_key", chosen_key)):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError(f"冲突裁决参数无效：{name}")
        if not isinstance(note, str) or len(note) > 500:
            raise ValueError("裁决说明不能超过 500 字符")
        with closing(self._connect()) as db:
            pending = repo.pending_conflict(db, platform, object_type, content_id)
            try:
                active = resolve_attribution_conflict(
                    db, platform, object_type, content_id, chosen_key,
                    authority="manual_review", note=note)
            except sqlite3.IntegrityError as error:
                # Moving a link to an account that has no account row would
                # break referential integrity; report it as a reviewable input
                # problem instead of dropping the connection.
                raise ValueError("裁决目标账号没有账号记录，无法完成归属迁移；请先重新采集该账号") from error
            return {"resolved": True,
                    "conflict_id": pending["id"] if pending else None,
                    "active_account_key": active,
                    "authority": "manual_review",
                    "updated_at": utcnow()}
