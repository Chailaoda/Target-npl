"""Task, version and rule-preview routes."""
import json
from pathlib import Path
import uuid

from ..active_config import active_snapshot
from ..pipeline import fingerprint
from ..rules_v2 import analysis_policy, validate_policy
from ..runtime_context import preflight
from ..task_contract import changed_paths, default_task


TASK_METHODS = ('plan', 'submit', 'validate', 'apply', 'patch', 'run', 'status', 'results', 'live')
TASK_SCHEMAS = {
    'plan': {'goal', 'base_etag'},
    'submit': {'goal', 'base_etag', 'request_id'},
    'validate': {'bundle'},
    'apply': {'bundle', 'base_etag'},
    'patch': {'operations', 'base_etag'},
    'run': {'revision', 'request_id'},
    'status': {'run_id'},
    'results': {'run_id', 'offset', 'limit'},
    'live': {'run_id', 'since_seq'},
}


def state(h):
    return h.send(200, dict(h.tasks.get(), token=h.token))


def latest(h):
    from ..live_view import latest as newest
    return h.send(200, newest(h.tasks))


def schema(h):
    from ..task_schema import task_schema
    return h.send(200, task_schema())


def capabilities(h):
    from ..task_contract import capabilities as caps
    return h.send(200, caps())


def datasets(h):
    return h.send(200, h.tasks.datasets())


def versions(h):
    return h.send(200, h.store.history())


def version(h):
    return h.send(200, h.store.version(h.params['revision']))


def identity(h):
    from ..runtime_context import RuntimeErrorBase
    try:
        return h.send(200, preflight(h.context))
    except RuntimeErrorBase as error:
        return h.send(503, {"error": str(error), "code": error.code})


def report(h):
    run_id = h.params['run_id']
    state_ = h.tasks.status(run_id)
    path = Path(state_.get('result', {}).get('report', '')).resolve()
    expected = (h.store.root / 'output/task-runs' / run_id).resolve()
    if not path.is_relative_to(expected) or not path.is_file():
        return h.send(404, {'error': '报告尚未生成'})
    return h.send(200, path.read_bytes(), 'text/html; charset=utf-8', report=True)


def save(h):
    return h.send(200, h.store.save(h.body['bundle'], h.body['etag']))


def validate_bundle_endpoint(h):
    from ..control_server import validate_bundle
    bundle = validate_bundle(h.body['bundle'])
    current = h.store.state()['bundle']
    changed = [key for key in bundle['policy'] if bundle['policy'].get(key) != current['policy'].get(key)]
    return h.send(200, {"valid": True, "changed": changed,
                        "collection_changed": bundle['collection'] != current['collection'],
                        "requires_new_analysis": analysis_policy(bundle['policy']) != analysis_policy(current['policy']),
                        "proxy_mode": "direct"})


def preview(h):
    validate_policy(h.body['policy'])
    text = h.body.get('text')
    if not isinstance(text, str) or not 1 <= len(text) <= 4000:
        raise ValueError("测试文本长度应为 1–4000 字符")
    payload = h.preview_worker.evaluate({
        'policy': h.body['policy'], 'text': text,
        'context_text': h.body.get('context_text'), 'task': h.body.get('task')})
    return h.send(400 if 'error' in payload else 200, payload)


def save_draft(h):
    from ..control_server import validate_bundle
    plan_id = h.body['plan_id']
    if not isinstance(plan_id, str) or len(plan_id) != 32 or any(c not in '0123456789abcdef' for c in plan_id):
        raise ValueError('plan_id 无效')
    plan = json.loads((h.store.root / 'data/task-plans' / f'{plan_id}.json').read_text(encoding='utf-8'))
    if plan['base_etag'] != h.body['etag']:
        from ..control_server import Conflict
        raise Conflict('计划基础版本不一致，请重新读取')
    bundle = validate_bundle(h.body['bundle'])
    h.tasks.validate(bundle)
    manual = [p for p in changed_paths(plan['bundle'], bundle)
              if p.startswith(('/policy/', '/collection/', '/task/scope/', '/task/exclusions/', '/task/execution/'))]
    bundle['task']['human_overrides'] = sorted(set(bundle['task']['human_overrides']) | set(manual))
    return h.send(200, h.store.save(bundle, h.body['etag'], actor='agent+human'))


def task_action(h):
    name = h.params['name']
    if name == 'save-draft':
        return save_draft(h)
    if name not in TASK_METHODS:
        return h.send(404, {'error': '任务接口不存在'})
    if set(h.body) - TASK_SCHEMAS[name]:
        raise ValueError('任务接口参数无效')
    from ..live_view import live as live_view
    methods = {'plan': h.tasks.plan, 'submit': h.tasks.submit, 'validate': h.tasks.validate,
               'apply': h.tasks.apply, 'patch': h.tasks.patch, 'run': h.tasks.run,
               'status': h.tasks.status, 'results': h.tasks.results,
               'live': lambda run_id, since_seq=None: live_view(h.tasks, run_id, since_seq)}
    return h.send(200, methods[name](**h.body))


def register(router):
    router.get('/api/state', state)
    router.get('/api/tasks/state', state)
    router.get('/api/tasks/latest', latest)
    router.get('/api/tasks/schema', schema)
    router.get('/api/tasks/capabilities', capabilities)
    router.get('/api/tasks/datasets', datasets)
    router.get('/api/versions', versions)
    router.get('/api/versions/<revision>', version)
    router.get('/api/identity', identity)
    router.get('/reports/<run_id>', report)

    router.post('/api/save', save, schema={'required': {'bundle', 'etag'}})
    router.post('/api/validate', validate_bundle_endpoint, schema={'required': {'bundle'}})
    router.post('/api/preview', preview, schema={'required': {'policy', 'text'},
                                                 'optional': {'context_text', 'task'}})
    router.post('/api/tasks/save-draft', save_draft,
                schema={'required': {'bundle', 'etag', 'plan_id'}})
    router.post('/api/tasks/<name>', task_action, schema=None)
