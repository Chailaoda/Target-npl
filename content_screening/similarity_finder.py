"""通过高分种子账号关联的搜索关键词发现相似账号。"""
from contextlib import closing
import logging

from .db_access import connect_readonly
from .candidate_pool import CandidatePool
from .graph_explorer import seed_accounts

logger = logging.getLogger("similarity_finder")

_BATCH_SIZE = 200
_MIN_KEYWORD_PRODUCTIVITY = 2


def _batches(values):
    """将查询参数拆成每批最多 200 个。"""
    values = list(values)
    for offset in range(0, len(values), _BATCH_SIZE):
        batch = values[offset:offset + _BATCH_SIZE]
        yield batch, ", ".join("?" for _ in batch)


def _productive_keywords(db, seed_source_hashes, min_productivity=_MIN_KEYWORD_PRODUCTIVITY,
                         *, seed_accounts_by_source=None):
    """按不同种子内容计数，提供归属时按不同种子账号计数。"""
    keyword_seeds = {}
    for batch, placeholders in _batches(seed_source_hashes):
        for row in db.execute("""
            SELECT DISTINCT keyword, source_hash
            FROM v2_observations
            WHERE source_hash IN ({}) AND TRIM(keyword) <> ''
        """.format(placeholders), batch):
            source_hash = row["source_hash"]
            seeds = keyword_seeds.setdefault(row["keyword"], set())
            if seed_accounts_by_source is None:
                seeds.add(source_hash)
            else:
                seeds.update(seed_accounts_by_source[source_hash])
    return {keyword: len(seeds) for keyword, seeds in keyword_seeds.items()
            if len(seeds) >= min_productivity}


def _find_similar_accounts(db, productive_keywords, seed_account_keys):
    """归集公开账号命中的高产关键词，每个关键词只计一次。"""
    seeds = set(seed_account_keys)
    accounts = {}
    for batch, placeholders in _batches(productive_keywords):
        for row in db.execute("""
            SELECT DISTINCT a.platform, a.account_id, a.account_key, o.keyword
            FROM v2_observations o
            JOIN account_content_links l ON l.source_hash = o.source_hash
            JOIN accounts a ON a.account_key = l.account_key
            WHERE o.keyword IN ({}) AND a.identity_mode = 'public_source'
        """.format(placeholders), batch):
            account_key = row["account_key"]
            if account_key not in seeds:
                account = accounts.setdefault(account_key, {
                    "platform": row["platform"],
                    "account_id": row["account_id"],
                    "account_key": account_key,
                    "keywords": set(),
                })
                account["keywords"].add(row["keyword"])

    return [{**account, "keywords": sorted(account["keywords"]),
             "keyword_count": len(account["keywords"])}
            for _, account in sorted(accounts.items())]


def find_and_register(context, min_score=30, confidence_base=0.2, limit=200):
    """选取高分种子并登记相似账号，返回写入统计、种子数和待登记账号数。"""
    empty_result = {"added": 0, "updated": 0, "duplicates": 0}
    seeds = seed_accounts(context, min_score=min_score, limit=limit)
    if not seeds:
        return empty_result, 0, 0

    with closing(connect_readonly(context=context)) as db:
        seed_accounts_by_source = {}
        for batch, placeholders in _batches(seeds):
            for row in db.execute("""
                SELECT account_key, source_hash
                FROM account_content_links
                WHERE account_key IN ({}) AND source_hash <> ''
            """.format(placeholders), batch):
                seed_accounts_by_source.setdefault(row["source_hash"], set()).add(row["account_key"])
        keywords = _productive_keywords(
            db, seed_accounts_by_source, seed_accounts_by_source=seed_accounts_by_source,
        )
        similar = _find_similar_accounts(db, keywords, seeds)
    if not similar:
        return empty_result, len(seeds), 0

    pool = CandidatePool(context)
    try:
        reviewed = {tuple(row) for row in pool.db.execute("""
            SELECT platform, account_id
            FROM discovery_candidates
            WHERE status IN ('confirmed', 'rejected')
        """)}
        candidates = [{
            "platform": account["platform"],
            "account_id": account["account_id"],
            "discovery_path": "content_similar",
            "confidence": min(confidence_base + 0.05 * (account["keyword_count"] - 1), 0.6),
            "metadata": {"keywords": account["keywords"]},
        } for account in similar if (account["platform"], account["account_id"]) not in reviewed]
        result = pool.add_batch(candidates) if candidates else empty_result
    finally:
        pool.close()

    logger.info("内容相似发现：%d 个种子，%d 个待登记账号，新增 %d 个候选",
                len(seeds), len(candidates), result["added"])
    return result, len(seeds), len(candidates)
