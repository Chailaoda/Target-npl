"""Interest profile use cases: orchestration only, no SQL and no scoring math."""
from contextlib import closing
from datetime import datetime, timezone
import json

from ..db_access import connect
from ..interests import profiles, repository as repo
from ..interests.conflicts import ConflictReview
from ..interests.scoring import (
    ALGORITHM_VERSION, STANCES, WINDOWS, analyze_text, define_topic, fingerprint,
    metrics, normalize_topic, raw_evidence, representative_from, utcnow,
)


class InterestService:
    def __init__(self, context):
        self.context = context

    def _connect(self, readonly=False):
        return connect(context=self.context, readonly=readonly)

    def _set_status(self, definition, status, at, error="", stats=None):
        # Committed separately: a rolled-back analysis must not erase 'failed'.
        with closing(self._connect()) as db, db:
            repo.set_status(db, definition, status, at, error, stats)

    def _assert_single_account_definition(self, definition):
        """A per-account run may not silently supersede another live definition."""
        with closing(self._connect(readonly=True)) as db:
            other = repo.assert_single_account_definition(db, definition)
        if other:
            raise ValueError("同一主题已有另一版本的定义，请先统一后再按账号分析")

    # Kept for callers (profile refresh) written against the pre-WP8 class.
    def _save_profile(self, db, account_key, definition, evidence, now):
        repo.save_profile(db, account_key, definition, evidence, now)

    # ---- analysis ---------------------------------------------------------
    def analyze(self, topic, aliases=None, exclude_terms=None, description="", account_key=None):
        definition = define_topic(topic, aliases, exclude_terms, description)
        if account_key:
            self._assert_single_account_definition(definition)
        now = datetime.now(timezone.utc)
        observed_at = now.isoformat()
        cache_key = definition["profile_version"]
        self._set_status(definition, "running", observed_at)
        try:
            with closing(self._connect()) as db, db:
                if account_key and not repo.account_exists(db, account_key):
                    raise ValueError("账号不存在或没有可靠公开身份")
                keys = [account_key] if account_key else repo.public_account_keys(db)
                candidate_count = evidence_count = 0
                repo.add_profile_version(db, definition, observed_at)
                for key in keys:
                    rows = repo.candidate_rows(db, key, definition)
                    candidate_count += len(rows)
                    current = []
                    for link, record in rows:
                        source_hash = link["source_hash"]
                        cached = repo.cached_decision(db, key, definition["topic"], source_hash, cache_key)
                        if cached:
                            if cached["decision"] == "accepted":
                                stored = repo.stored_evidence(db, key, definition["topic"], source_hash, definition["profile_version"])
                                if stored:
                                    current.append(json.loads(stored["details_json"]))
                                else:
                                    analyzed = analyze_text(record, definition)
                                    if analyzed["decision"] == "accepted":
                                        current.append(raw_evidence(link, record, analyzed, definition))
                            continue
                        analyzed = analyze_text(record, definition)
                        analysis_key = fingerprint({"source": source_hash, "definition": cache_key})
                        repo.save_content_analysis(db, key, definition, source_hash, cache_key, analysis_key, analyzed, observed_at)
                        if analyzed["decision"] == "accepted":
                            current.append(raw_evidence(link, record, analyzed, definition))
                    self._save_profile(db, key, definition, current, now)
                    evidence_count += len(current)
                stats = {"accounts": len(keys), "candidate_contents": candidate_count, "evidence": evidence_count,
                         "empty_public_account_data": not keys}
                repo.finish_definition(db, definition, stats, utcnow())
        except Exception as error:
            self._set_status(definition, "failed", utcnow(), str(error)[:500])
            raise
        return self.search(definition["topic"], definition=definition)

    # ---- reads ------------------------------------------------------------
    def topic_status(self, topic):
        key = normalize_topic(topic)
        with closing(self._connect(readonly=True)) as db:
            row = repo.definition_row(db, key)
            if not row:
                legacy = repo.legacy_profile_exists(db, key)
                return {"topic": key, "status": "completed" if legacy else "not_analyzed",
                        "stats": {}, "error": "", "started_at": None, "finished_at": None}
            return {"topic": key, "definition": json.loads(row["definition_json"]), "status": row["status"],
                    "stats": json.loads(row["stats_json"] or "{}"), "error": row["error"],
                    "started_at": row["started_at"], "finished_at": row["finished_at"]}

    def search(self, topic, *, min_score=0, platform="", window="all", min_evidence=1, stance="", sort="score",
               limit=20, offset=0, definition=None):
        if window not in ("all", "30", "90", "180", 30, 90, 180):
            raise ValueError("时间窗口仅支持 30/90/180/all")
        window = str(window)
        limit, offset = int(limit), int(offset)
        if not 1 <= limit <= 100 or offset < 0 or not 0 <= float(min_score) <= 100 or int(min_evidence) < 0:
            raise ValueError("筛选或分页参数无效")
        if sort not in ("score", "recent", "evidence"):
            raise ValueError("排序仅支持 score/recent/evidence")
        if stance and stance not in STANCES:
            raise ValueError("态度筛选无效")
        key = normalize_topic(topic)
        with closing(self._connect(readonly=True)) as db:
            active = definition or repo.resolve_definition(db, key)
            if active is None:
                version = repo.legacy_profile_version(db, key)
                if not version:
                    return {"topic": key, "status": "not_analyzed", "items": [], "total": 0,
                            "limit": limit, "offset": offset, "message": "该主题尚未分析；不代表没有任何人感兴趣。"}
                active = define_topic(key)
                active["profile_version"] = version
            version = active["profile_version"]
            # Static filters stay in SQL; time-dependent metrics are recomputed
            # with the same clock/metric function used by the evidence detail.
            rows = repo.profile_rows(db, key, version, platform)
            now = datetime.now(timezone.utc)
            items = []
            for row in rows:
                evidence = repo.load_evidence(db, row["account_key"], key, version)
                current = metrics(evidence, now, window)
                if current["interest_score"] < float(min_score):
                    continue
                if current["evidence_count"] < int(min_evidence):
                    continue
                if stance and current["stance"] != stance:
                    continue
                details = json.loads(row["details_json"])
                item = {"account_key": row["account_key"], "platform": row["platform"],
                        "account_id": row["account_id"], "nickname": row["nickname"],
                        "profile_url": row["profile_url"], "topic": key, **current,
                        "profile_version": row["profile_version"], "updated_at": row["updated_at"],
                        "synthetic": details.get("synthetic", False)}
                item["representative_evidence"] = representative_from(evidence, window, now)
                items.append(item)
            if sort == "score":
                items.sort(key=lambda i: (i["interest_score"], i["evidence_count"], i["account_key"]), reverse=True)
            elif sort == "evidence":
                items.sort(key=lambda i: (i["evidence_count"], i["interest_score"], i["account_key"]), reverse=True)
            else:
                items.sort(key=lambda i: (i["last_activity"] or "", i["interest_score"], i["account_key"]), reverse=True)
            total = len(items)
            items = items[offset:offset + limit]
        status = self.topic_status(key)
        return {"topic": key, "definition": active, "status": status["status"],
                "analysis": status, "items": items, "total": total, "limit": limit, "offset": offset,
                "message": "已分析，当前条件下无匹配结果。" if not items else ""}

    def account(self, account_key):
        return profiles.account(self, account_key)

    def evidence(self, account_key, topic, window="all"):
        return profiles.evidence(self, account_key, topic, window)

    # ---- review feedback --------------------------------------------------
    def feedback(self, evidence_id, action, note="", profile_version=None):
        if action not in ("irrelevant", "stance_error", "attribution_question", "restore"):
            raise ValueError("证据反馈类型无效")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("反馈说明不能超过 1000 字符")
        # The evidence id survives a definition switch (business-key upsert),
        # so an old page must not be able to mutate the new profile with it.
        if not isinstance(profile_version, str) or not profile_version.strip():
            raise ValueError("反馈必须携带证据详情页展示的画像版本，请刷新证据详情后重试")
        state = "accepted" if action == "restore" else action
        with closing(self._connect()) as db, db:
            row = repo.feedback_row(db, evidence_id)
            if not row:
                raise ValueError("兴趣证据不存在")
            version = repo.active_version(db, row["topic"])
            if version is None or row["profile_version"] != version:
                raise ValueError("证据不属于当前画像版本，请重新分析该主题")
            if profile_version != version or profile_version != row["profile_version"]:
                raise ValueError("证据版本已变化，请刷新证据详情后重新提交反馈")
            definition = repo.resolve_definition(db, row["topic"]) or define_topic(row["topic"])
            definition = dict(definition, profile_version=version)
            item = json.loads(row["details_json"])
            item.update(review_state=state, review_note=note)
            repo.update_feedback(db, evidence_id, state, note, json.dumps(item, ensure_ascii=False))
            evidence = repo.load_evidence(db, row["account_key"], row["topic"], version)
            self._save_profile(db, row["account_key"], definition, evidence, datetime.now(timezone.utc))
        return self.account(row["account_key"])

    # ---- listings ---------------------------------------------------------
    def recent_topics(self, limit=8):
        limit = max(1, min(int(limit), 30))
        with closing(self._connect(readonly=True)) as db:
            result = []
            for row in repo.recent_topic_rows(db, limit):
                definition = json.loads(row["definition_json"])
                result.append({"topic": row["topic_key"],
                               "display_topic": definition.get("display_topic", row["topic_key"]),
                               "aliases": definition.get("aliases", []),
                               "description": definition.get("description", ""),
                               "status": row["status"],
                               "profile_count": repo.topic_profile_count(db, row["topic_key"], definition["profile_version"]),
                               "finished_at": row["finished_at"],
                               "stats": json.loads(row["stats_json"] or "{}")})
            return result

    def accounts(self, keyword="", limit=50, offset=0):
        from ..account_repository import search_accounts
        limit, offset = int(limit), int(offset)
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("分页参数无效")
        with closing(self._connect(readonly=True)) as db:
            items, total = search_accounts(db, keyword, limit, offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    # ---- attribution conflict review -------------------------------------
    # Kept as thin delegations: the workbench talks to InterestService, while
    # the attribution rules stay in their own module.
    def list_conflicts(self, status="pending", account_key=None, limit=50):
        return ConflictReview(self.context).list_conflicts(status, account_key, limit)

    def resolve_conflict(self, platform, object_type, content_id, chosen_key, note=""):
        return ConflictReview(self.context).resolve_conflict(platform, object_type, content_id, chosen_key, note)
