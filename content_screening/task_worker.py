import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .active_config import ROOT
from .task_service import TaskService,atomic_json
from .task_contract import validate_execution
from .pipeline import fingerprint


def execute(run_id,root=None):
    from .runtime_context import current_context,set_context
    context=current_context();set_context(context)
    root=Path(root) if root is not None else context.state_root
    from .control_server import RuleStore,validate_bundle
    service=TaskService(RuleStore(root));folder=service.runs/run_id
    request=json.loads((folder/'request.json').read_text(encoding='utf-8'));bundle=request['bundle']
    if fingerprint({'revision':request['revision'],'bundle':bundle})!=request['signature']:raise ValueError('任务快照哈希不一致，未执行')
    validate_bundle(bundle);validate_execution(bundle)
    state=json.loads((folder/'status.json').read_text(encoding='utf-8'))
    def update(**values):
        state.update(values,updated_at=datetime.now(timezone.utc).isoformat());atomic_json(folder/'status.json',state)
    update(status='running',phase='preparing',pid=os.getpid())
    collection_incomplete=False;incomplete_platforms=[];collection_status=None;collection_warnings=[]
    try:
        for name in ('policy','collection','task'):atomic_json(folder/f'{name}.json',bundle[name])
        execution=bundle['task']['execution']
        from .budget_controller import BudgetController
        budget=BudgetController(root)
        if execution['mode']=='ark' and budget.available_api_calls()<=0:
            update(status='not_started_budget',phase='stopped',
                   message='今日模型调用额度已用完，未启动采集；额度将在 UTC 次日重置。')
            return state
        if execution['source']=='existing':source=service.dataset(execution['dataset_id'])
        else:
            update(phase='collecting')
            from .multi_collection import collect_selected
            data=collect_selected(bundle['collection'],folder,root)
            collection_warnings=data.get('warnings') or []
            platform_statuses=data.get('platform_statuses',[])
            if any(p.get('status')=='needs_user_login' for p in platform_statuses):
                from .platforms import open_login
                for p in platform_statuses:
                    if p.get('status')=='needs_user_login':
                        try:open_login(p['platform'])
                        except Exception:pass
            raw_path=Path(data['raw_file']) if data.get('raw_file') else None
            has_raw_data=raw_path and raw_path.is_file() and raw_path.stat().st_size>0
            incomplete_platforms=data.get('incomplete_platforms') or [
                p.get('platform') for p in platform_statuses
                if p.get('status') not in ('completed','completed_empty')]
            collection_incomplete=bool(incomplete_platforms)
            collection_status=data['status']
            if data['status'] not in ('completed','completed_empty'):
                if has_raw_data:
                    source=raw_path
                    login_pending=any(p.get('status')=='needs_user_login' for p in platform_statuses)
                    message=('部分未登录平台已自动弹出登录窗并跳过，已优先完成其余平台数据分析' if login_pending
                             else '部分平台采集未完成（{}），已优先完成其余平台数据分析；结果不完整'.format(
                                 '、'.join(str(x) for x in incomplete_platforms)))
                    update(status='running',phase='analyzing',platform_statuses=platform_statuses,
                           collection_status=collection_status,incomplete_platforms=incomplete_platforms,
                           collection_incomplete=collection_incomplete,collection_warnings=collection_warnings,
                           collection_warning_count=len(collection_warnings),message=message)
                else:
                    update(status='needs_user_login' if data['status']=='needs_user_login' else 'failed',
                           phase='collection_stopped',collection_status=collection_status,
                           platform_statuses=platform_statuses,incomplete_platforms=incomplete_platforms,
                           collection_incomplete=collection_incomplete,collection_warnings=collection_warnings,
                           collection_warning_count=len(collection_warnings));return state
            else:
                source=Path(data['raw_file'])
        update(phase='analyzing',dataset_id=source.parent.name,input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
               collection_warnings=collection_warnings,collection_warning_count=len(collection_warnings))
        from .workflow import run
        from .db_access import resolve_db
        result=run(source,folder/'policy.json',resolve_db(),Path(root)/'output/task-runs'/run_id,
                   execution['mode'],Path(root)/'config/connection.json',execution['max_api_calls'],task=bundle['task'],config_revision=request['revision'],on_start=lambda job_id:update(analysis_job_id=job_id),budget=budget)
        analysis_status=result['status']
        analysis_ok=analysis_status in ('completed','completed_empty','completed_with_review')
        # Collection / analysis / overall are three distinct states. An
        # incomplete collection must never surface as a completed task even
        # when the analysis of the partial data succeeded; a failed analysis
        # keeps its own failure status.
        overall='partial' if collection_incomplete and analysis_ok else analysis_status
        finished={'status':overall,'phase':'finished','result':result,
                  'analysis_status':analysis_status}
        if collection_status is not None:
            finished.update(collection_status=collection_status,
                            collection_incomplete=collection_incomplete,
                            incomplete_platforms=incomplete_platforms)
        update(**finished)
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        update(status='failed',phase='stopped',error_code=type(error).__name__,message=str(error)[:300])
    return state


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run-id',required=True);args=parser.parse_args()
    import re
    if not re.fullmatch(r'[0-9a-f]{32}',args.run_id):raise SystemExit('invalid run ID')
    print(json.dumps(execute(args.run_id),ensure_ascii=False))
