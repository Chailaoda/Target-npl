"""Interest, account and attribution-review routes."""
from urllib.parse import unquote


def recent_topics(h):
    return h.send(200, {'items': h.interests.recent_topics(h.query.get('limit', ['8'])[0])})


def accounts(h):
    return h.send(200, h.interests.accounts(h.query.get('keyword', [''])[0],
                                            h.query.get('limit', ['50'])[0],
                                            h.query.get('offset', ['0'])[0]))


def interest_search(h):
    if 'topic' not in h.query:
        raise ValueError('必须提供兴趣主题')
    allowed = {'topic', 'min_score', 'platform', 'window', 'min_evidence', 'stance', 'sort', 'limit', 'offset'}
    if set(h.query) - allowed:
        raise ValueError('兴趣筛选参数无效')
    options = {key: h.query[key][0] for key in allowed - {'topic'} if key in h.query}
    return h.send(200, h.interests.search(h.query['topic'][0], **options))


def analyse_status(h):
    return h.send(200, h.interests.topic_status(h.query.get('topic', [''])[0]))


def account_detail(h):
    key = unquote(h.params['key'])
    result = h.interests.account(key)
    if not result:
        return h.send(404, {'error': '账号不存在或没有可靠公开身份'})
    return h.send(200, result)


def account_interests(h):
    key = unquote(h.params['key'])
    result = h.interests.account(key)
    if not result:
        return h.send(404, {'error': '账号不存在'})
    return h.send(200, {'account_key': key, 'items': result['interests']})


def account_evidence(h):
    key = unquote(h.params['key'])
    if not h.interests.account(key):
        return h.send(404, {'error': '账号不存在'})
    return h.send(200, h.interests.evidence(key, unquote(h.params['topic']), h.query.get('window', ['all'])[0]))


def conflict_list(h):
    return h.send(200, h.interests.list_conflicts(
        status=h.query.get('status', ['pending'])[0],
        account_key=h.query.get('account_key', [''])[0] or None,
        limit=h.query.get('limit', ['50'])[0]))


def conflict_resolve(h):
    return h.send(200, h.interests.resolve_conflict(**h.body))


def analyse_topic(h):
    return h.send(200, h.interests.analyze(**h.body))


def evidence_feedback(h):
    return h.send(200, h.interests.feedback(**h.body))


def review_queue(h):
    from ..db_access import resolve_db
    from ..workflow import pending_reviews
    return h.send(200, pending_reviews(
        resolve_db(h.context),
        run_id=h.query.get('run_id', [''])[0] or None,
        limit=h.query.get('limit', ['50'])[0]))


def review_submit(h):
    from ..db_access import resolve_db
    from ..workflow import review_many
    return h.send(200, review_many(resolve_db(h.context), h.body['items']))


def platforms(h):
    from ..platforms import health
    return h.send(200, health(h.store.root))


def open_login(h):
    from ..platforms import open_login as launch
    return h.send(200, launch(h.body['platform']))


def register(router):
    router.get('/api/interests/topics', recent_topics)
    router.get('/api/interests/search', interest_search)
    router.get('/api/interests/analyze', analyse_status)
    router.get('/api/accounts', accounts)
    router.get('/api/accounts/conflicts', conflict_list)
    router.get('/api/accounts/<key>/interests/<topic>/evidence', account_evidence)
    router.get('/api/accounts/<key>/interests', account_interests)
    router.get('/api/accounts/<key>', account_detail)
    router.get('/api/reviews/pending', review_queue)
    router.get('/api/platforms', platforms)

    router.post('/api/interests/analyze', analyse_topic,
                schema={'required': {'topic'}, 'optional': {'aliases', 'exclude_terms', 'description', 'account_key'}})
    router.post('/api/interests/evidence-feedback', evidence_feedback,
                schema={'required': {'evidence_id', 'action'}, 'optional': {'note', 'profile_version'}})
    router.post('/api/accounts/conflicts/resolve', conflict_resolve,
                schema={'required': {'platform', 'object_type', 'content_id', 'chosen_key'}, 'optional': {'note'}})
    router.post('/api/reviews', review_submit, schema={'required': {'items'}})
    router.post('/api/platforms/open-login', open_login, schema={'required': {'platform'}})
