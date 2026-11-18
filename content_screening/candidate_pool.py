"""管理跨平台发现的候选账号及其合并置信度。"""
from datetime import datetime, timezone
import json
import logging

from .db_access import connect

logger = logging.getLogger('candidate_pool')

_DISCOVERY_PATHS = {
    'keyword_search', 'comment_interaction', 'social_graph',
    'content_similar', 'deep_thread',
}
_STATUSES = {
    'new', 'queued', 'collection_pending', 'analyzed',
    'confirmed', 'rejected', 'duplicate',
}


class CandidatePool:
    """持久化候选账号并提供筛选、状态更新和统计。"""

    def __init__(self, context):
        """连接运行上下文指定的主数据库并初始化候选表。"""
        self.context = context
        self.db = connect(context=context)
        self.db.executescript("""
CREATE TABLE IF NOT EXISTS discovery_candidates(
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    account_key TEXT NOT NULL,
    discovery_path TEXT NOT NULL,
    source_account_key TEXT NOT NULL DEFAULT '',
    source_content_hash TEXT NOT NULL DEFAULT '',
    raw_confidence REAL NOT NULL DEFAULT 0.0,
    merged_confidence REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'new',
    priority INTEGER NOT NULL DEFAULT 0,
    discovery_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(platform, account_id)
);
CREATE INDEX IF NOT EXISTS idx_disc_status ON discovery_candidates(status, merged_confidence DESC);
CREATE INDEX IF NOT EXISTS idx_disc_path ON discovery_candidates(discovery_path);
""")

    def close(self):
        """关闭候选池的数据库连接。"""
        self.db.close()

    def add_candidate(self, platform, account_id, discovery_path, confidence,
                      source_account_key='', source_content_hash='', metadata=None):
        """新增候选账号或合并已有候选的发现置信度。"""
        with self.db:
            result = self._add_candidate(
                platform, account_id, discovery_path, confidence,
                source_account_key, source_content_hash, metadata,
            )
        logger.debug('候选账号已保存：%s，状态：%s', result['account_key'], result['status'])
        return result

    def _add_candidate(self, platform, account_id, discovery_path, confidence,
                       source_account_key='', source_content_hash='', metadata=None):
        """在调用方事务内写入候选账号并返回合并结果。"""
        if discovery_path not in _DISCOVERY_PATHS:
            raise ValueError(f'无效的发现路径：{discovery_path}')
        if not 0.0 <= confidence <= 1.0:
            raise ValueError('置信度必须介于 0 和 1 之间')
        account_key = f'{platform}:{account_id}'
        now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        known = self.db.execute(
            'SELECT 1 FROM accounts WHERE account_key=?', (account_key,),
        ).fetchone()
        existing = self.db.execute(
            'SELECT status, merged_confidence FROM discovery_candidates '
            'WHERE platform=? AND account_id=?', (platform, account_id),
        ).fetchone()
        if existing is not None:
            status = 'duplicate' if known is not None else existing['status']
            merged = round(1 - (1 - existing['merged_confidence']) * (1 - confidence), 6)
            self.db.execute(
                'UPDATE discovery_candidates SET discovery_count=discovery_count+1, '
                'merged_confidence=?, status=?, updated_at=? '
                'WHERE platform=? AND account_id=?',
                (merged, status, now, platform, account_id),
            )
        else:
            status = 'duplicate' if known is not None else 'new'
            merged = confidence
            self.db.execute(
                'INSERT INTO discovery_candidates '
                '(platform, account_id, account_key, discovery_path, source_account_key, '
                'source_content_hash, raw_confidence, merged_confidence, status, '
                'created_at, updated_at, metadata_json) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (platform, account_id, account_key, discovery_path, source_account_key,
                 source_content_hash, confidence, merged, status, now, now,
                 json.dumps(metadata if metadata is not None else {}, ensure_ascii=False)),
            )
        return {
            'account_key': account_key,
            'status': status,
            'merged_confidence': merged,
            'is_new': existing is None,
        }

    def add_batch(self, candidates):
        """单事务写入候选，统计新增、更新及其中的重复账号数。"""
        counts = {'added': 0, 'updated': 0, 'duplicates': 0}
        with self.db:
            for candidate in candidates:
                result = self._add_candidate(**candidate)
                counts['added' if result['is_new'] else 'updated'] += 1
                counts['duplicates'] += int(result['status'] == 'duplicate')
        logger.info('候选账号批量保存：新增 %d，更新 %d，重复 %d',
                    counts['added'], counts['updated'], counts['duplicates'])
        return counts

    def top_candidates(self, limit=50, min_confidence=0.0, status='new', discovery_path=None):
        """按合并置信度和发现次数返回符合条件的候选账号。"""
        query = 'SELECT * FROM discovery_candidates WHERE status=? AND merged_confidence>=?'
        params = [status, min_confidence]
        if discovery_path is not None:
            query += ' AND discovery_path=?'
            params.append(discovery_path)
        query += ' ORDER BY merged_confidence DESC, discovery_count DESC LIMIT ?'
        params.append(limit)
        return [dict(row) for row in self.db.execute(query, params)]

    def promote(self, account_key, new_status):
        """将指定候选账号更新为合法状态，返回是否匹配到记录。"""
        if new_status not in _STATUSES:
            raise ValueError(f'无效的候选状态：{new_status}')
        now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        with self.db:
            cursor = self.db.execute(
                'UPDATE discovery_candidates SET status=?, updated_at=? WHERE account_key=?',
                (new_status, now, account_key),
            )
            matched = cursor.rowcount > 0
        logger.debug('候选账号状态已更新：%s，状态：%s，匹配：%s', account_key, new_status, matched)
        return matched

    def stats(self):
        """返回候选总数、状态分布、发现路径分布和最高置信度。"""
        summary = self.db.execute(
            'SELECT COUNT(*) AS total, MAX(merged_confidence) AS top_confidence '
            'FROM discovery_candidates',
        ).fetchone()
        by_status = {
            row['status']: row['count'] for row in self.db.execute(
                'SELECT status, COUNT(*) AS count FROM discovery_candidates GROUP BY status',
            )
        }
        by_path = {
            row['discovery_path']: row['count'] for row in self.db.execute(
                'SELECT discovery_path, COUNT(*) AS count '
                'FROM discovery_candidates GROUP BY discovery_path',
            )
        }
        return {
            'total': summary['total'],
            'by_status': by_status,
            'by_path': by_path,
            'top_confidence': summary['top_confidence'] or 0.0,
        }
