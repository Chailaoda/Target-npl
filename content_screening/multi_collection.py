"""Sequential platform scheduler with independent platform budgets and fault isolation."""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from .platforms import selected
from .task_service import atomic_json
from .vendor_env import resolve_vendor_env,VendorEnvironmentError


def collection_command(root,python,script,config,receipt,progress):
    """Build one child launch command.

    The interpreter ALWAYS comes from the resolved vendor environment, never
    from a hardcoded path: the Python running the adapter is the interpreter
    installed inside the very same directory whose MediaCrawler source is
    imported and whose git worktree is integrity-checked.
    """
    return [str(python),'-X','utf8',str(Path(root)/'scripts'/script),
        '--config',str(config),'--receipt',str(receipt),'--progress-file',str(progress)]


def collect_selected(settings,folder,root):
    root=Path(root);folder=Path(folder);platforms=selected(settings)
    dataset=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    job=root/'data/collections'/dataset;job.mkdir(parents=True,exist_ok=False)
    raw=job/'raw.jsonl';raw.touch()
    status={'status':'running','settings':settings,'records':0,'posts':0,'comments':0,'requests':0,'errors':[],
        'raw_file':str(raw),'proxy_mode':'direct','identity_mode':'upstream_anonymized',
        'created_at_utc':datetime.now(timezone.utc).isoformat(),'platform_statuses':[],'aggregate':True}
    atomic_json(folder/'collection-progress.json',{'dataset_id':dataset})
    def save():atomic_json(job/'checkpoint.json',status)
    save();started=time.monotonic()
    # 采集前统一解析受控依赖：调度器解释器、子进程导入的源码树、git 完整性
    # 校验工作树三者必为同一目录。依赖缺失/提交不在白名单/工作树异常/
    # 解释器缺失时在此停止并给出明确错误，绝不静默降级到上游版本。
    # 检测到 fork 只证明具备透传能力，不替代平台访问权限与授权登录态确认。
    try:
        vendor=resolve_vendor_env(root,env=os.environ)
    except VendorEnvironmentError as error:
        status['status']='blocked';status['errors'].append({'platform':'vendor','code':error.code})
        save()
        raise
    child_env=vendor.launch_env()

    for index,platform in enumerate(platforms):
        remaining=len(platforms)-index
        seconds=settings['timeout_seconds']-int(time.monotonic()-started)
        budget_posts=(settings['max_posts']-status['posts'])//remaining
        budget_requests=(settings['max_requests']-status['requests'])//remaining
        if seconds<20 or budget_posts<1 or budget_requests<1:
            status['platform_statuses'].append({'platform':platform,'status':'not_started_budget','records':0});save();continue
        child=dict(settings,platform=platform,max_posts=budget_posts,max_requests=budget_requests,timeout_seconds=min(seconds, settings.get('timeout_seconds', 180)),proxy_mode='direct')
        child.pop('platforms',None)
        child_dir=folder/'platform-live'/platform;child_dir.mkdir(parents=True,exist_ok=True)
        config=child_dir/'collection.json';atomic_json(config,child)
        receipt=root/'output/collections'/f'{folder.name}-{platform}.json'
        if receipt.exists():raise ValueError('平台采集回执已存在，未重复采集')
        script='mediacrawler_collect.py' if platform=='douyin' else 'platform_collect.py'
        command=collection_command(root,vendor.python,script,config,receipt,child_dir/'collection-progress.json')
        status['active_platform']=platform;save()
        try:
            result=subprocess.run(command,cwd=root,timeout=child['timeout_seconds']+10,env=child_env)
            data=json.loads(receipt.read_text(encoding='utf-8'))
            child_job=Path(data['raw_file']).parent.resolve()
            if not child_job.is_relative_to(Path(root/'data/collections').resolve()):raise ValueError('Invalid child source path')
            checkpoint=child_job/'checkpoint.json'
            if checkpoint.exists():
                child_state=json.loads(checkpoint.read_text(encoding='utf-8'));child_state['parent_dataset_id']=dataset;atomic_json(checkpoint,child_state)
            if result.returncode==0 and data['status'] in ('completed','completed_empty'):
                with raw.open('a',encoding='utf-8') as target:
                    for line in Path(data['raw_file']).read_text(encoding='utf-8').splitlines():
                        if line.strip():
                            row=json.loads(line);row.setdefault('platform',platform);target.write(json.dumps(row,ensure_ascii=False)+'\n')
                status['records']+=data.get('records',0)
            for key in ('posts','comments'):status[key]+=data.get(key,0)
            status['requests']+=data.get('requests',0)
            status['platform_statuses'].append({'platform':platform,'status':data['status'],'records':data.get('records',0),'requests':data.get('requests',0),'dataset_id':Path(data['raw_file']).parent.name})
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
            status['platform_statuses'].append({'platform':platform,'status':'failed','error_code':type(error).__name__})
            status['errors'].append({'platform':platform,'code':type(error).__name__})
        save()
    states=[s['status'] for s in status['platform_statuses']]
    complete=len(states)==len(platforms) and all(s in ('completed','completed_empty') for s in states)
    status['status']=('completed' if status['records'] else 'completed_empty') if complete else 'completed' if status['records'] else 'needs_user_login' if 'needs_user_login' in states else 'failed'
    status.update(raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),finished_at_utc=datetime.now(timezone.utc).isoformat())
    status.pop('active_platform',None);save()
    return status
