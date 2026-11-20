"""SQLite access for interest profiles.

Every statement that touches storage lives here; the service only decides what
to ask for. Functions take an open connection so the caller owns the
transaction boundary (analysis and feedback must commit or roll back together).
"""
import json
import re

from ..account_repository import analyzed_content_links
from ..interests.scoring import ALGORITHM_VERSION, HALF_LIFE_DAYS, WINDOWS, metrics

ACTIVE_DEFINITION_SQL = """SELECT * FROM interest_topic_definitions WHERE topic_key=?
    ORDER BY COALESCE(finished_at,started_at,created_at) DESC,
             created_at DESC, definition_hash DESC LIMIT 1"""


def definition_row(db, topic):
    return db.execute(ACTIVE_DEFINITION_SQL, (topic,)).fetchone()


def resolve_definition(db, topic):
    row = definition_row(db, topic)
    return json.loads(row["definition_json"]) if row else None


def active_version(db, topic):
    definition = resolve_definition(db, topic)
    if definition:
        return definition["profile_version"]
    row = db.execute("SELECT profile_version FROM interest_profiles WHERE topic=? ORDER BY updated_at DESC LIMIT 1",
                     (topic,)).fetchone()
    return row[0] if row else None


def set_status(db, definition, status, at, error="", stats=None):
    db.execute("""INSERT INTO interest_topic_definitions(
        topic_key,definition_json,definition_hash,status,created_at,started_at,finished_at,error,stats_json)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(topic_key,definition_hash) DO UPDATE SET status=excluded.status,
        started_at=CASE WHEN excluded.status='running' THEN excluded.started_at
                        ELSE interest_topic_definitions.started_at END,
        finished_at=excluded.finished_at,error=excluded.error,stats_json=excluded.stats_json""",
        (definition["topic"], json.dumps(definition, ensure_ascii=False), definition["definition_hash"],
         status, at, at, None if status == "running" else at, error,
         json.dumps(stats or {}, ensure_ascii=False)))


def assert_single_account_definition(db, definition):
    return db.execute("""SELECT DISTINCT topic_key,definition_hash FROM interest_topic_definitions
        WHERE topic_key=? AND definition_hash<>? AND status IN ('running','completed')""",
        (definition["topic"], definition["definition_hash"])).fetchone()


def account_exists(db, account_key):
    return db.execute("SELECT 1 FROM accounts WHERE account_key=? AND identity_mode='public_source'",
                      (account_key,)).fetchone()


def public_account_keys(db):
    return [row[0] for row in db.execute(
        "SELECT account_key FROM accounts WHERE identity_mode='public_source' ORDER BY account_key")]


def add_profile_version(db, definition, observed_at):
    db.execute("INSERT OR IGNORE INTO interest_profile_versions VALUES (?,?,?)",
               (definition["profile_version"],
                "Verbatim literal rules; clause-scoped stance; independent coverage; capped threads; deterministic saturation",
                observed_at))


def candidate_rows(db, account_key, definition):
    rows = []
    seen = set()
    for link in analyzed_content_links(db, account_key):
        if link["source_hash"] in seen:
            continue
        seen.add(link["source_hash"])
        conflict = db.execute("""SELECT 1 FROM account_attribution_conflicts
            WHERE platform=? AND object_type=? AND content_id=? AND status='pending'
              AND (current_account_key=? OR candidate_account_key=?) LIMIT 1""",
            (link["platform"], link["object_type"], link["content_id"], account_key, account_key)).fetchone()
        if conflict:
            continue
        source = db.execute("SELECT source_json FROM v2_sources WHERE source_hash=?", (link["source_hash"],)).fetchone()
        if not source:
            continue
        record = json.loads(source["source_json"])
        if record.get("identity_mode") != "public_source" or record.get("author_key") != account_key:
            continue
        analyses = db.execute("""SELECT a.analysis_key,a.result_json,r.decision FROM v2_analyses a
            LEFT JOIN v2_reviews r ON r.analysis_key=a.analysis_key
            WHERE a.source_hash=? AND a.result_json IS NOT NULL AND a.result_json<>''""", (link["source_hash"],)).fetchall()
        eligible = False
        for analysis in analyses:
            if analysis["decision"] == "reject":
                continue
            try:
                result = json.loads(analysis["result_json"])
            except (ValueError, TypeError):
                continue
            if result.get("engine") != "quarantined_model_output":
                eligible = True
                break
        if eligible:
            rows.append((link, record))
    return rows


def cached_decision(db, account_key, topic, source_hash, cache_key):
    return db.execute("""SELECT decision FROM interest_content_analysis
        WHERE account_key=? AND topic_key=? AND source_hash=? AND definition_hash=?""",
        (account_key, topic, source_hash, cache_key)).fetchone()


def stored_evidence(db, account_key, topic, source_hash, version):
    return db.execute("""SELECT details_json FROM interest_evidence
        WHERE account_key=? AND topic=? AND source_hash=? AND profile_version=?""",
        (account_key, topic, source_hash, version)).fetchone()


def save_content_analysis(db, account_key, definition, source_hash, cache_key, analysis_key, analyzed, observed_at):
    db.execute("""INSERT OR REPLACE INTO interest_content_analysis(
        account_key,topic_key,source_hash,definition_hash,analysis_key,decision,details_json,updated_at)
        VALUES (?,?,?,?,?,?,?,?)""", (account_key, definition["topic"], source_hash, cache_key, analysis_key,
        analyzed["decision"], json.dumps(analyzed, ensure_ascii=False), observed_at))


def load_evidence(db, account_key, topic, version):
    rows = db.execute("""SELECT id,details_json FROM interest_evidence
        WHERE account_key=? AND topic=? AND profile_version=? ORDER BY source_hash""",
                      (account_key, topic, version)).fetchall()
    return [dict(json.loads(r["details_json"]), id=r["id"]) for r in rows]


def save_profile(db, account_key, definition, evidence, now):
    """Current version wins: one evidence row per (account, topic, source).

    id is always assigned by SQLite. The business key
    (account_key, topic, analysis_key) is used for upserts, so the same
    source_hash can be evidence for several topics without a PK collision.
    """
    version = definition["profile_version"]
    keep = {item["source_hash"] for item in evidence}
    for old in db.execute("SELECT id,source_hash FROM interest_evidence WHERE account_key=? AND topic=?",
                          (account_key, definition["topic"])).fetchall():
        if old["source_hash"] not in keep:
            db.execute("DELETE FROM interest_evidence WHERE id=?", (old["id"],))
    for item in evidence:
        old = db.execute("""SELECT review_state,review_note FROM interest_evidence
            WHERE account_key=? AND topic=? AND source_hash=?""",
            (account_key, definition["topic"], item["source_hash"])).fetchone()
        if old:
            item.update(review_state=old["review_state"], review_note=old["review_note"])
        db.execute("""INSERT INTO interest_evidence(account_key,topic,analysis_key,source_hash,stance,quote,observed_at,
            object_type,published_at,relevance,depth,quality,source_url,quality_flags,details_json,profile_version,review_state,review_note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_key,topic,analysis_key) DO UPDATE SET stance=excluded.stance,quote=excluded.quote,
            observed_at=excluded.observed_at,object_type=excluded.object_type,published_at=excluded.published_at,
            relevance=excluded.relevance,depth=excluded.depth,quality=excluded.quality,source_url=excluded.source_url,
            quality_flags=excluded.quality_flags,details_json=excluded.details_json,profile_version=excluded.profile_version""",
            (account_key, definition["topic"], item["source_hash"], item["source_hash"], item["stance"], item["quote"], now.isoformat(),
             item["object_type"], item["published_at"], item["relevance"], item["depth"], item["quality"], item["source_url"],
             json.dumps(item["quality_flags"], ensure_ascii=False), json.dumps(item, ensure_ascii=False), version,
             item.get("review_state", "accepted"), item.get("review_note", "")))
        stored = db.execute("""SELECT id FROM interest_evidence
            WHERE account_key=? AND topic=? AND source_hash=? AND profile_version=?""",
            (account_key, definition["topic"], item["source_hash"], version)).fetchone()
        if stored:
            item["id"] = stored["id"]
            db.execute("UPDATE interest_evidence SET details_json=? WHERE id=?",
                       (json.dumps(item, ensure_ascii=False), stored["id"]))
    valid = [e for e in evidence if e.get("review_state", "accepted") == "accepted"]
    current = metrics(valid, now)
    windows = {str(w): metrics(valid, now, w) for w in WINDOWS}
    windows["all"] = current
    details = {"windows": windows, "algorithm_version": ALGORITHM_VERSION,
               "definition_hash": definition["definition_hash"], "half_life_days": HALF_LIFE_DAYS,
               "evidence_refs": [e["analysis_key"] for e in valid], "synthetic": bool(valid) and all(
                   json.loads(r[0]).get("synthetic") is True for r in db.execute(
                       "SELECT source_json FROM v2_sources WHERE source_hash IN (SELECT source_hash FROM account_content_links WHERE account_key=?)",
                       (account_key,)))}
    db.execute("""INSERT INTO interest_profiles(account_key,topic,score,content_count,active_days,stance_summary,trend,
        evidence_notes,profile_version,updated_at,evidence_count,independent_content_count,coverage,last_activity,details_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_key,topic,profile_version) DO UPDATE SET score=excluded.score,content_count=excluded.content_count,
        active_days=excluded.active_days,stance_summary=excluded.stance_summary,trend=excluded.trend,
        evidence_notes=excluded.evidence_notes,updated_at=excluded.updated_at,evidence_count=excluded.evidence_count,
        independent_content_count=excluded.independent_content_count,coverage=excluded.coverage,
        last_activity=excluded.last_activity,details_json=excluded.details_json""",
        (account_key, definition["topic"], current["interest_score"], current["independent_content_count"], current["active_days"],
         current["stance"], current["trend"],
         "兴趣参与强度，不是概率或人格判断；半衰期用于趋势对比，窗口决定计入证据。",
         version, now.isoformat(), current["evidence_count"], current["independent_content_count"],
         current["coverage"], current["last_activity"], json.dumps(details, ensure_ascii=False)))


def finish_definition(db, definition, stats, finished_at):
    db.execute("""UPDATE interest_topic_definitions SET status='completed',finished_at=?,stats_json=?
        WHERE topic_key=? AND definition_hash=?""",
        (finished_at, json.dumps(stats, ensure_ascii=False), definition["topic"], definition["definition_hash"]))


def legacy_profile_exists(db, topic):
    return db.execute("SELECT 1 FROM interest_profiles WHERE topic=? LIMIT 1", (topic,)).fetchone()


def legacy_profile_version(db, topic):
    row = db.execute("SELECT profile_version FROM interest_profiles WHERE topic=? ORDER BY updated_at DESC LIMIT 1",
                     (topic,)).fetchone()
    return row[0] if row else None


def profile_rows(db, topic, version, platform=""):
    where = ["p.topic=?", "p.profile_version=?", "a.identity_mode='public_source'"]
    params = [topic, version]
    if platform:
        where.append("a.platform=?")
        params.append(platform)
    return db.execute(f"""SELECT a.account_key,a.platform,a.account_id,a.nickname,a.profile_url,
        p.topic,p.profile_version,p.updated_at,p.details_json
        FROM interest_profiles p JOIN accounts a ON a.account_key=p.account_key
        WHERE {" AND ".join(where)} ORDER BY p.account_key""", params).fetchall()


def account_row(db, account_key):
    return db.execute("SELECT * FROM accounts WHERE account_key=? AND identity_mode='public_source'",
                      (account_key,)).fetchone()


def account_profile_rows(db, account_key):
    return db.execute("SELECT * FROM interest_profiles WHERE account_key=? ORDER BY updated_at DESC,profile_version DESC",
                      (account_key,)).fetchall()


def feedback_row(db, evidence_id):
    return db.execute("SELECT * FROM interest_evidence WHERE id=?", (int(evidence_id),)).fetchone()


def update_feedback(db, evidence_id, state, note, details_json):
    db.execute("UPDATE interest_evidence SET review_state=?,review_note=?,details_json=? WHERE id=?",
               (state, note, details_json, int(evidence_id)))


def recent_topic_rows(db, limit=8):
    return db.execute("""SELECT * FROM interest_topic_definitions d
        WHERE COALESCE(finished_at,started_at,created_at)=(
            SELECT MAX(COALESCE(finished_at,started_at,created_at))
            FROM interest_topic_definitions x WHERE x.topic_key=d.topic_key)
        ORDER BY COALESCE(finished_at,started_at,created_at) DESC,topic_key LIMIT ?""", (limit,)).fetchall()


def topic_profile_count(db, topic, version):
    return db.execute("""SELECT count(*) FROM interest_profiles p
        JOIN accounts a ON a.account_key=p.account_key
        WHERE p.topic=? AND p.profile_version=? AND a.identity_mode='public_source'""",
                      (topic, version)).fetchone()[0]


def conflict_link(db, platform, object_type, content_id, account_key):
    return db.execute("""SELECT attribution_authority,source_hash FROM account_content_links
        WHERE platform=? AND object_type=? AND content_id=? AND account_key=?""",
        (platform, object_type, content_id, account_key)).fetchone()


def conflict_snippet(db, *source_hashes, limit=280):
    for source_hash in source_hashes:
        if not source_hash:
            continue
        stored = db.execute("SELECT source_json FROM v2_sources WHERE source_hash=?", (source_hash,)).fetchone()
        if not stored:
            continue
        try:
            record = json.loads(stored["source_json"])
        except ValueError:
            continue
        text = re.sub(r"\s+", " ", str(record.get("text") or "")).strip()
        if text:
            return text[:limit]
    return ""


def pending_conflict(db, platform, object_type, content_id):
    return db.execute("""SELECT id FROM account_attribution_conflicts
        WHERE platform=? AND object_type=? AND content_id=? AND status='pending'
        ORDER BY id DESC LIMIT 1""", (platform, object_type, content_id)).fetchone()


def topic_is_analysed(db, topic):
    return legacy_profile_exists(db, topic) or definition_row(db, topic) is not None
