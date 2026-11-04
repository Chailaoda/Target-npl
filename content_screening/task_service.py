"""Single application API shared by Web, stdio MCP and command-line agents."""
from datetime import datetime,timezone
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from .active_config import ROOT
from .pipeline import fingerprint
from .task_contract import default_task,validate_task,validate_execution,changed_paths,apply_patch,capabilities


def atomic_json(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)


def process_alive(pid):
    if type(pid) is not int:return False
    import os
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.WaitForSingleObject.restype=wintypes.DWORD
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x100000,False,pid)
        if not handle:return False
        try:return kernel.WaitForSingleObject(handle,0)==258
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0);return True
    except OSError:return False


class TaskService:
    def __init__(self,store=None):
        from .control_server import RuleStore
        self.store=store or RuleStore();self.root=self.store.root
        self.runs=self.root/'data/task-runs';self.runs.mkdir(parents=True,exist_ok=True)

    def get(self):
        state=self.store.state();state=copy.deepcopy(state)
        state['bundle'].setdefault('task',default_task(state['bundle']))
        state['capabilities']=capabilities()
        return state

    def validate(self,bundle):
        from .control_server import validate_bundle
        validate_bundle(bundle)
        if 'task' not in bundle:raise ValueError('统一任务包缺少 task')
        validate_execution(bundle)
        return {'valid':True,'capabilities':capabilities(),'warnings':list(bundle['task']['assumptions']),
                'filter_placement':{'locations':'postprocess','time':'postprocess','keywords':'source_search'},'estimated_max_contents':bundle['collection']['max_posts']*(1+bundle['collection']['comments_per_post'])}

    def check_budget(self,bundle,current):
        if bundle.get('collection', {}).get('proxy_mode') != 'direct':
            raise ValueError('所有平台必须保持直连 direct，不允许使用代理')
        max_calls = bundle.get('task', {}).get('execution', {}).get('max_api_calls', 40)
        if max_calls > 500:
            raise ValueError('超出系统允许的最大模型调用上限（500次）')
        for field in ('pages_per_keyword','posts_per_keyword','max_posts','comments_per_post','max_requests'):
            if bundle['collection'][field]>current['collection'][field]:
                raise ValueError('超出当前预设采集额度：'+field)
        if bundle['collection']['request_interval_seconds']<current['collection']['request_interval_seconds']:
            raise ValueError('不能自动提高采集频率')

    def apply(self,bundle,base_etag):
        from .control_server import Conflict
        self.validate(bundle);current=self.get()
        if base_etag!=current['etag']:raise Conflict('基础版本已变化，请重新读取人工最新修改后制定配置')
        self.check_budget(bundle,current['bundle'])
        locks=current['bundle']['task']['human_overrides']
        if bundle['task']['human_overrides']!=locks:raise ValueError('智能体不能删除人工锁定标记，请由人工在 Web 端解除')
        changed=changed_paths(current['bundle'],bundle)
        blocked=[p for p in changed for locked in locks if p==locked or p.startswith(locked+'/') or locked.startswith(p+'/')]
        if blocked:raise ValueError('这些字段由人工锁定，未应用：'+'、'.join(sorted(set(blocked))))
        return self.store.save(bundle,base_etag,actor='agent')

    def patch(self,operations,base_etag):
        current=self.get()
        if current['etag']!=base_etag:
            from .control_server import Conflict
            raise Conflict('基础版本已变化，请重新读取')
        return self.apply(apply_patch(current['bundle'],operations),base_etag)

    def submit(self,goal,base_etag,request_id):
        """One authorized goal -> plan -> validate -> apply -> queue execution.

        A durable idempotency journal also prevents duplicate planner charges.
        """
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id):raise ValueError('request_id 无效')
        if not isinstance(goal,str) or not 1<=len(goal.strip())<=4000:raise ValueError('目标描述长度无效')
        from .control_server import write_lock
        journal=self.root/'data/task-submissions';journal.mkdir(parents=True,exist_ok=True)
        path=journal/(hashlib.sha256(request_id.encode()).hexdigest()+'.json');signature=fingerprint({'goal':goal,'base_etag':base_etag})
        with write_lock(journal/'submit.lock'):
            if path.exists():
                previous=json.loads(path.read_text(encoding='utf-8'))
                if previous['signature']!=signature:raise ValueError('同一 request_id 不能提交不同目标或基础版本')
                return previous
            state={'request_id':request_id,'signature':signature,'status':'planning','created_at':datetime.now(timezone.utc).isoformat()}
            atomic_json(path,state)
            try:
                plan=self.plan(goal,base_etag);state['plan_id']=plan['plan_id']
                if not plan['can_apply']:
                    state.update(status='needs_attention',unresolved=plan['bundle']['task']['unresolved'],bundle=plan['bundle'])
                else:
                    applied=self.apply(plan['bundle'],base_etag)
                    state.update(status='applied',state=applied);atomic_json(path,state)
                    queued=self.run(applied['revision'],request_id)
                    state.update(status='submitted',run=queued)
            except (ValueError,OSError) as error:
                state.update(status='needs_attention',message=str(error)[:500])
            finally:atomic_json(path,state)
            return state

    def plan(self,goal,base_etag,compiler=None):
        current=self.get()
        if base_etag!=current['etag']:
            from .control_server import Conflict
            raise Conflict('制定目标前基础版本已变化，未调用模型')
        if not isinstance(goal,str) or not 1<=len(goal.strip())<=4000:raise ValueError('目标应为 1–4000 字符')
        if compiler is None:
            from .task_planner import compile_goal
            compiler=compile_goal
        proposal,usage=compiler(goal,current['bundle'],capabilities())
        proposal=copy.deepcopy(proposal)
        proposal['task']['description']=goal
        proposal['task']['human_overrides']=current['bundle']['task']['human_overrides']

        exec_cfg=proposal.get('task',{}).get('execution',{})
        raw_calls=exec_cfg.get('max_api_calls')
        clamped_assumption=None
        if isinstance(raw_calls,(int,float)):
            raw_int=int(raw_calls)
            if raw_int>500:
                exec_cfg['max_api_calls']=500
                clamped_assumption=f'模型调用额度提议（{raw_int}次）超出系统安全上限，已自动收敛至 500 次'
            elif raw_int<1:
                exec_cfg['max_api_calls']=1
            else:
                exec_cfg['max_api_calls']=raw_int

        from .control_server import validate_bundle
        validate_bundle(proposal)
        if clamped_assumption:
            proposal['task']['assumptions'].append(clamped_assumption)
        locks=current['bundle']['task']['human_overrides']
        changed=changed_paths(current['bundle'],proposal)
        protected=[p for p in changed for locked in locks if p==locked or p.startswith(locked+'/') or locked.startswith(p+'/')]
        if protected:
            for p in protected:
                parts=[x.replace('~1','/').replace('~0','~') for x in p[1:].split('/')]
                src=current['bundle'];dst=proposal
                for k in parts[:-1]:
                    src=src[int(k)] if isinstance(src,list) else src[k]
                    dst=dst[int(k)] if isinstance(dst,list) else dst[k]
                last_key=int(parts[-1]) if isinstance(dst,list) else parts[-1]
                dst[last_key]=copy.deepcopy(src[last_key])
            proposal['task']['assumptions'].append('目标调整包含此前锁定的配置，已保持原人工设定：'+','.join(sorted(set(protected))))

        # 自动收敛超出人工预设上限的采集额度配置
        cur_col = current['bundle'].get('collection', {})
        prop_col = proposal.get('collection', {})
        for field in ('pages_per_keyword', 'posts_per_keyword', 'max_posts', 'comments_per_post', 'max_requests'):
            if field in prop_col and field in cur_col and prop_col[field] > cur_col[field]:
                proposal['task']['assumptions'].append(
                    f'采集参数 {field} 提议（{prop_col[field]}）超出当前人工预设，已自动收敛至预设上限 {cur_col[field]}'
                )
                prop_col[field] = cur_col[field]
        if prop_col.get('request_interval_seconds', 0) < cur_col.get('request_interval_seconds', 0):
            prop_col['request_interval_seconds'] = cur_col['request_interval_seconds']
        if prop_col.get('proxy_mode') != 'direct':
            prop_col['proxy_mode'] = 'direct'

        # 过滤 AI 规划模型误报的额度类 unresolved（额度已由系统统一审核与收敛）
        budget_keywords = ('额度', '上限', '预算', 'max_api_calls', '授权', '模型额度')
        demoted = [u for u in proposal['task']['unresolved'] if any(k in u for k in budget_keywords)]
        proposal['task']['unresolved'] = [u for u in proposal['task']['unresolved'] if u not in demoted]
        if demoted:
            proposal['task']['assumptions'].extend(f'预算说明（已自动收敛处理）：{u}' for u in demoted)

        cur_exec=current['bundle']['task']['execution']
        prop_exec=proposal['task']['execution']
        cur_calls=cur_exec.get('max_api_calls',40)
        prop_calls=prop_exec.get('max_api_calls',cur_calls)
        col=proposal.get('collection',{})

        if prop_exec.get('source')=='collect' and prop_exec.get('mode')=='ark':
            needed=col.get('max_posts',30)*(1+col.get('comments_per_post',10))
            if needed>prop_calls and needed<=500 and '/task/execution/max_api_calls' not in locks:
                prop_calls=needed
                prop_exec['max_api_calls']=needed

        if '/task/execution/max_api_calls' not in locks:
            if prop_calls>500:
                prop_exec['max_api_calls']=500
                proposal['task']['assumptions'].append(f'模型调用额度提议（{prop_calls}次）超出系统安全上限，已自动收敛至 500 次')
            elif prop_calls!=cur_calls:
                proposal['task']['assumptions'].append(f'模型调用额度由 {cur_calls} 次调整为 {prop_exec["max_api_calls"]} 次（已在安全授权额度内自动批准）')

        changed=changed_paths(current['bundle'],proposal)

        if not proposal['task']['unresolved']:
            try:
                validate_execution(proposal)
                self.check_budget(proposal,current['bundle'])
            except ValueError as error:
                err_msg=str(error)
                if any(k in err_msg for k in ('额度','上限','预算','max_api_calls')):
                    proposal['task']['assumptions'].append(f'预算说明：{err_msg}')
                else:
                    proposal['task']['unresolved'].append(err_msg)
        entry={'goal':goal,'base_revision':current['revision'],'base_etag':base_etag,'bundle':proposal,'usage':usage,'created_at':datetime.now(timezone.utc).isoformat()}
        import uuid
        plan_id=uuid.uuid4().hex;atomic_json(self.root/'data/task-plans'/f'{plan_id}.json',entry)
        return dict(entry,plan_id=plan_id,changed_paths=changed,can_apply=not proposal['task']['unresolved'],filter_placement={'locations':'postprocess','time':'postprocess','keywords':'source_search'})

    def datasets(self):
        result=[]
        for folder in sorted((self.root/'data/collections').glob('*'),reverse=True):
            if not re.fullmatch(r'\d{8}T\d{12}Z',folder.name):continue
            checkpoint=folder/'checkpoint.json'
            if checkpoint.exists() and (folder/'raw.jsonl').exists():
                state=json.loads(checkpoint.read_text(encoding='utf-8'))
                if state.get('status')=='completed' and not state.get('parent_dataset_id'):
                    settings=state.get('settings',{})
                    result.append({'dataset_id':folder.name,'records':state.get('records'),'created_at_utc':state.get('created_at_utc'),'platforms':settings.get('platforms',[settings.get('platform','douyin')])})
        return result

    def dataset(self,name):
        datasets=self.datasets()
        if name=='latest' and datasets:name=datasets[0]['dataset_id']
        if name not in {d['dataset_id'] for d in datasets}:raise ValueError('没有可用的已完成采集批次')
        path=self.root/'data/collections'/name/'raw.jsonl'
        if not path.resolve().is_relative_to((self.root/'data/collections').resolve()):raise ValueError('采集批次路径无效')
        checkpoint=json.loads((path.parent/'checkpoint.json').read_text(encoding='utf-8'))
        if checkpoint.get('raw_sha256') and hashlib.sha256(path.read_bytes()).hexdigest()!=checkpoint['raw_sha256']:raise ValueError('采集原文哈希不一致，未执行')
        return path

    def snapshot(self,revision):
        if revision=='baseline':
            current=self.get()
            if current['revision']!='baseline':raise ValueError('当前已不是初始配置，请指定实际版本')
            return current['bundle']
        bundle=self.store.version(revision);bundle.setdefault('task',default_task(bundle));return bundle

    def run(self,revision,request_id,spawn=True):
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id):raise ValueError('request_id 应为 1–64 位字母数字、横线或下划线')
        bundle=self.snapshot(revision);self.validate(bundle)
        self.check_budget(bundle,self.get()['bundle'])
        if bundle['task']['execution']['source']=='existing':self.dataset(bundle['task']['execution']['dataset_id'])
        run_id=hashlib.sha256(request_id.encode()).hexdigest()[:32];folder=self.runs/run_id
        signature=fingerprint({'revision':revision,'bundle':bundle})
        from .control_server import write_lock
        with write_lock(self.runs/'start.lock'):
            if folder.exists():
                if json.loads((folder/'request.json').read_text(encoding='utf-8'))['signature']!=signature:raise ValueError('同一 request_id 不能用于不同任务版本')
                return self.status(run_id)
            for state_file in self.runs.glob('*/status.json'):
                state=json.loads(state_file.read_text(encoding='utf-8'))
                if state['status'] in ('queued','running'):
                    launch=state_file.parent/'launch.json'
                    pid=state.get('pid') or (json.loads(launch.read_text(encoding='utf-8')).get('pid') if launch.exists() else None)
                    if process_alive(pid):raise ValueError('已有任务运行，未启动新的采集或模型调用')
            folder.mkdir()
            atomic_json(folder/'request.json',{'revision':revision,'signature':signature,'request_id':request_id,'bundle':bundle})
            state={'run_id':run_id,'status':'queued','revision':revision,'created_at':datetime.now(timezone.utc).isoformat()}
            atomic_json(folder/'status.json',state)
            if spawn:
                import os
                with (folder/'worker.log').open('ab') as log:
                    child=subprocess.Popen([sys.executable,'-X','utf8','-m','content_screening.task_worker','--run-id',run_id],cwd=self.root,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                atomic_json(folder/'launch.json',{'pid':child.pid})
            return state

    def status(self,run_id):
        if not isinstance(run_id,str) or not re.fullmatch(r'[0-9a-f]{32}',run_id):raise ValueError('run_id 无效')
        folder=self.runs/run_id
        state=json.loads((folder/'status.json').read_text(encoding='utf-8'))
        if state['status'] in ('queued','running'):
            launch=folder/'launch.json';pid=state.get('pid') or (json.loads(launch.read_text(encoding='utf-8')).get('pid') if launch.exists() else None)
            if pid and not process_alive(pid):state=dict(state,status='interrupted',message='工作进程已退出；未自动重试，成功分析仍可由缓存复用')
        return state

    def results(self,run_id,offset=0,limit=20):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:raise ValueError('分页参数无效')
        state=self.status(run_id)
        if 'result' not in state:return {'status':state['status'],'items':[],'total':0}
        path=Path(state['result']['export'])
        if not path.resolve().is_relative_to((self.root/'output/task-runs').resolve()):raise ValueError('结果路径不在任务输出目录')
        rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        return {'status':state['status'],'total':len(rows),'offset':offset,'items':rows[offset:offset+limit],'summary':state['result']}
