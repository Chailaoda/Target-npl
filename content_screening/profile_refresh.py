"""分析完成后增量刷新受影响账号的兴趣画像。

在分析工作流完成一批记录后，找出受影响的账号和活跃主题定义，
使用本地规则重新计算画像分数。不调用模型 API，不修改主题状态。
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import logging

from .db_access import connect
from .interest_service import (
    InterestService, _candidate_rows, _raw_evidence, analyze_text,
)
from .pipeline import fingerprint

logger = logging.getLogger("profile_refresh")


def _active_definitions(db):
    """读取所有已完成分析的活跃主题定义，每个主题最多一个。"""
    rows = db.execute("""
        SELECT d.definition_json FROM interest_topic_definitions d
        WHERE COALESCE(d.finished_at, d.started_at, d.created_at) = (
            SELECT MAX(COALESCE(x.finished_at, x.started_at, x.created_at))
            FROM interest_topic_definitions x WHERE x.topic_key = d.topic_key)
        AND d.status = 'completed'
        ORDER BY d.topic_key, d.created_at DESC, d.definition_hash DESC
    """).fetchall()
    seen = set()
    result = []
    for r in rows:
        defn = json.loads(r[0])
        if defn["topic"] not in seen:
            seen.add(defn["topic"])
            result.append(defn)
    return result


def _affected_accounts(db, job_id):
    """找出分析任务影响的公开身份账号。"""
    return [r[0] for r in db.execute("""
        SELECT DISTINCT l.account_key
        FROM v2_tasks t
        JOIN account_content_links l ON l.source_hash = t.source_hash
        JOIN accounts a ON a.account_key = l.account_key
        WHERE t.job_id = ?
        AND t.status IN ('completed', 'needs_review')
        AND a.identity_mode = 'public_source'
    """, (job_id,)).fetchall()]


def _refresh_one(db, service, account_key, definition, now):
    """为单个账号刷新单个主题的画像（使用分析缓存，不修改主题状态）。"""
    cache_key = definition["profile_version"]
    observed_at = now.isoformat()
    rows = _candidate_rows(db, account_key, definition)
    current = []
    for link, record in rows:
        source_hash = link["source_hash"]
        cached = db.execute(
            """SELECT decision FROM interest_content_analysis
               WHERE account_key=? AND topic_key=? AND source_hash=?
               AND definition_hash=?""",
            (account_key, definition["topic"], source_hash, cache_key),
        ).fetchone()
        if cached:
            if cached["decision"] == "accepted":
                stored = db.execute(
                    """SELECT details_json FROM interest_evidence
                       WHERE account_key=? AND topic=? AND source_hash=?
                       AND profile_version=?""",
                    (account_key, definition["topic"], source_hash,
                     definition["profile_version"]),
                ).fetchone()
                if stored:
                    current.append(json.loads(stored["details_json"]))
                else:
                    analyzed = analyze_text(record, definition)
                    if analyzed["decision"] == "accepted":
                        current.append(
                            _raw_evidence(link, record, analyzed, definition))
            continue
        analyzed = analyze_text(record, definition)
        analysis_key = fingerprint(
            {"source": source_hash, "definition": cache_key})
        db.execute(
            """INSERT OR REPLACE INTO interest_content_analysis(
               account_key, topic_key, source_hash, definition_hash,
               analysis_key, decision, details_json, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (account_key, definition["topic"], source_hash, cache_key,
             analysis_key, analyzed["decision"],
             json.dumps(analyzed, ensure_ascii=False), observed_at),
        )
        if analyzed["decision"] == "accepted":
            current.append(_raw_evidence(link, record, analyzed, definition))
    service._save_profile(db, account_key, definition, current, now)
    return len(current)


def refresh_for_job(context, job_id):
    """刷新受分析任务影响的账号画像，返回 (账号数, 主题数) 刷新计数。"""
    service = InterestService(context)
    with closing(connect(context=context, readonly=True)) as ro:
        definitions = _active_definitions(ro)
        accounts = _affected_accounts(ro, job_id)
    if not definitions or not accounts:
        return 0, 0
    now = datetime.now(timezone.utc)
    refreshed = 0
    topics_seen = set()
    with closing(connect(context=context)) as db:
        for defn in definitions:
            for key in accounts:
                try:
                    with db:
                        _refresh_one(db, service, key, defn, now)
                    refreshed += 1
                    topics_seen.add(defn["topic"])
                except Exception:
                    logger.debug("刷新 %s / %s 画像失败", key, defn["topic"],
                                 exc_info=True)
    if refreshed:
        job_short = str(job_id)[:8]
        logger.info("增量刷新 %d 个账号画像，涉及 %d 个主题（job %s）",
                     refreshed, len(topics_seen), job_short)
    return refreshed, len(topics_seen)
