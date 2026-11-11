from contextlib import redirect_stdout
import copy
from datetime import datetime,timezone,timedelta
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import threading
import urllib.request
import urllib.error

from festival_match.control_server import RuleStore,Conflict
from festival_match.task_service import TaskService
from festival_match.task_contract import default_task,apply_scope,apply_patch
from festival_match.agent_mcp import AgentTools,dispatch
from festival_match.task_worker import execute
from festival_match.task_schema import task_schema

ROOT=Path(__file__).resolve().parents[1]


class TaskTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'tmp/tests').mkdir(parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=ROOT/'tmp/tests');self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        for folder in ('profiles','config'):(self.root/folder).mkdir()
        for file in ('profiles/music_festival_v2.json','config/collection.json'):(self.root/file).write_bytes((ROOT/file).read_bytes())
        self.store=RuleStore(self.root);self.service=TaskService(self.store)

    def test_agent_apply_human_tuning_and_protection(self):
        initial=self.service.get();bundle=initial['bundle'];bundle['task']['description']='测试统一目标'
        saved=self.service.apply(bundle,initial['etag'])
        manual=copy.deepcopy(saved['bundle']);manual['policy']['display']['sort_weights']['positive']=8
        newer=self.store.save(manual,saved['etag'])
        self.assertIn('/policy/display/sort_weights/positive',newer['bundle']['task']['human_overrides'])
        with self.assertRaises(Conflict):self.service.apply(bundle,initial['etag'])
        agent=copy.deepcopy(newer['bundle']);agent['policy']['display']['sort_weights']['positive']=80
        with self.assertRaisesRegex(ValueError,'人工锁定'):self.service.apply(agent,newer['etag'])
        agent=copy.deepcopy(newer['bundle']);agent['task']['human_overrides']=[]
        with self.assertRaisesRegex(ValueError,'不能删除'):self.service.apply(agent,newer['etag'])

    def test_first_manual_task_save_locks_scope_even_from_legacy_bundle(self):
        initial=self.service.get();bundle=initial['bundle'];bundle['task']['scope']['locations']=['成都']
        saved=self.store.save(bundle,initial['etag'])
        self.assertIn('/task/scope/locations',saved['bundle']['task']['human_overrides'])
        modified=copy.deepcopy(saved['bundle']);modified['task']['scope']['locations']=['北京']
        with self.assertRaisesRegex(ValueError,'人工锁定'):self.service.apply(modified,saved['etag'])

    def test_goal_compiler_uses_latest_state_and_does_not_auto_apply(self):
        initial=self.service.get();before=copy.deepcopy(initial)
        def compiler(goal,bundle,caps):
            self.assertFalse(caps['native_location_filter'])
            candidate=copy.deepcopy(bundle);candidate['task']['scope']['locations']=['成都'];candidate['task']['scope']['recent_days']=30
            candidate['task']['assumptions']=['最近按近30天，地域与时间均在后处理检查']
            return candidate,{'total_tokens':100}
        plan=self.service.plan('找成都音乐节同行',initial['etag'],compiler)
        self.assertTrue(plan['can_apply']);self.assertEqual(plan['bundle']['task']['scope']['locations'],['成都'])
        self.assertEqual(self.service.get(),before)
        self.service.apply(plan['bundle'],plan['base_etag'])
        self.assertEqual(self.service.get()['bundle']['task']['description'],'找成都音乐节同行')

    def test_patch_validation_and_human_locked_fields(self):
        initial=self.service.get()
        updated=self.service.patch([{'op':'replace','path':'/collection/keywords','value':['新关键词']}],initial['etag'])
        self.assertEqual(updated['bundle']['collection']['keywords'],['新关键词'])
        with self.assertRaises(ValueError):self.service.patch([{'op':'replace','path':'/collection/proxy_mode','value':'system'}],updated['etag'])
        with self.assertRaises(ValueError):apply_patch(updated['bundle'],[{'op':'remove','path':'/etc/passwd'}])

    def test_one_goal_submit_plans_applies_and_runs_only_once(self):
        initial=self.service.get();proposal=copy.deepcopy(initial['bundle'])
        proposal['task']['scope']['locations']=['成都']
        queued={'run_id':'a'*32,'status':'queued'}
        with patch('festival_match.task_planner.compile_goal',return_value=(proposal,{'total_tokens':1})) as compiler,patch.object(self.service,'run',return_value=queued) as start:
            first=self.service.submit('找成都音乐节搭子',initial['etag'],'same-request')
            second=self.service.submit('找成都音乐节搭子',initial['etag'],'same-request')
        self.assertEqual(first['status'],'submitted');self.assertEqual(second,first)
        self.assertEqual(compiler.call_count,1);self.assertEqual(start.call_count,1)
        self.assertEqual(self.service.get()['bundle']['task']['scope']['locations'],['成都'])
        self.assertEqual(self.service.get()['bundle']['task']['human_overrides'],[])
        with self.assertRaises(ValueError):self.service.submit('不同目标',initial['etag'],'same-request')

    def test_source_filters_not_faked_and_own_timestamp_not_parent_timestamp(self):
        task=default_task(self.service.get()['bundle']);task['scope'].update(locations=['成都'],recent_days=30)
        now=datetime.now(timezone.utc)
        base={'accepted':True,'score':90,'evidence_level':'explicit','reason':'自述','promotion':'no'}
        record={'text':'找搭子','published_at':now.isoformat()}
        out=apply_scope(base,record,[{'text':'成都音乐节'}],task,now.isoformat())
        self.assertTrue(out['accepted']);self.assertEqual(out['scope_check']['status'],'matched')
        record['published_at']='';out=apply_scope(base,record,[{'text':'成都音乐节'}],task,now.isoformat())
        self.assertFalse(out['accepted']);self.assertEqual(out['scope_check']['status'],'unknown')
        record['published_at']=(now-timedelta(days=60)).isoformat();out=apply_scope(base,record,[{'text':'成都音乐节'}],task,now.isoformat())
        self.assertEqual(out['scope_check']['status'],'excluded')
        self.assertTrue(base['accepted'])

    def test_model_budget_unresolved_and_fake_dataset_refused(self):
        initial=self.service.get();bundle=initial['bundle']
        bundle['task']['execution']['source']='existing';bundle['task']['unresolved']=['不支持精确位置筛选']
        with self.assertRaisesRegex(ValueError,'未解决'):self.service.apply(bundle,initial['etag'])
        with self.assertRaises(ValueError):self.service.dataset('../../secret')
        proposed=self.service.get()['bundle'];proposed['task']['execution']['max_api_calls']=50
        res=self.service.apply(proposed,self.service.get()['etag'])
        self.assertEqual(res['bundle']['task']['execution']['max_api_calls'],50)

    def test_planner_uses_typed_patch_values_and_accepts_description_metadata(self):
        from festival_match.task_planner import compile_goal
        config=json.loads((ROOT/'config/connection.json').read_text(encoding='utf-8-sig'))
        changes=[{'op':'replace','path':'/task/description','value':'新目标'},
                 {'op':'replace','path':'/task/scope/locations','value':['成都']},
                 {'op':'replace','path':'/policy/topic_patterns','value':[r'音乐节\s*现场']}]
        envelope={'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'function':{'name':'draft_screening_task','arguments':json.dumps({'changes':changes,'assumptions':[],'unresolved':[]},ensure_ascii=False)}}]}}],'usage':{'total_tokens':10}}
        with patch('festival_match.task_planner.ROOT',self.root),patch('festival_match.task_planner.load_connection',return_value=config),patch('festival_match.task_planner.read_key',return_value='not-a-real-key'),patch('festival_match.task_planner.request',return_value=(json.dumps(envelope).encode(),{})):
            bundle,usage=compile_goal('测试目标',self.service.get()['bundle'],{})
        self.assertEqual(bundle['task']['scope']['locations'],['成都'])
        self.assertEqual(bundle['policy']['topic_patterns'],[r'音乐节\s*现场'])
        self.assertEqual(usage['total_tokens'],10)

    def test_mcp_tools_and_parameter_boundary(self):
        tools=AgentTools(self.service)
        response=dispatch({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},tools)
        self.assertEqual(response['result']['protocolVersion'],'2025-11-25')
        listing=dispatch({'jsonrpc':'2.0','id':2,'method':'tools/list'},tools)
        self.assertEqual(len(listing['result']['tools']),13)
        response=dispatch({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'screening_task_get'}},tools)
        self.assertFalse(response['result']['isError']);self.assertIn('task',response['result']['structuredContent']['bundle'])
        with self.assertRaises(ValueError):tools.call('screening_task_run',{'revision':'baseline','request_id':'test','spawn':False})

    def test_http_plan_draft_tracks_only_actual_human_tuning(self):
        from festival_match.control_server import make_server
        initial=self.service.get()
        def compiler(goal,bundle,caps):
            bundle=copy.deepcopy(bundle);bundle['task']['scope']['locations']=['成都'];return bundle,{}
        plan=self.service.plan('测试成都范围',initial['etag'],compiler)
        server=make_server(0,self.store);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f'http://127.0.0.1:{server.server_port}';opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url+'/api/tasks/state') as response:state=json.load(response)
        with opener.open(url+'/api/tasks/schema') as response:self.assertEqual(json.load(response)['title'],'统一筛选任务包 v1')
        draft=copy.deepcopy(plan['bundle']);draft['task']['scope']['recent_days']=7
        def post(endpoint,body):
            req=urllib.request.Request(url+endpoint,data=json.dumps(body).encode(),headers={'Origin':url,'Content-Type':'application/json','X-Control-Token':state['token']})
            return opener.open(req)
        with post('/api/tasks/save-draft',{'plan_id':plan['plan_id'],'etag':state['etag'],'bundle':draft}) as response:saved=json.load(response)
        self.assertIn('/task/scope/recent_days',saved['bundle']['task']['human_overrides'])
        self.assertNotIn('/task/scope/locations',saved['bundle']['task']['human_overrides'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            post('/api/tasks/patch',{'operations':[{'op':'replace','path':'/task/scope/recent_days','value':30}],'base_etag':saved['etag']})
        self.assertEqual(error.exception.code,400)

    def test_end_to_end_snapshot_execution_idempotency_and_results(self):
        initial=self.service.get();bundle=initial['bundle'];bundle['task']['execution']['mode']='rules';bundle['task']['scope']['locations']=['成都']
        saved=self.service.apply(bundle,initial['etag'])
        dataset_id='20261029T200000000000Z';dataset=self.root/'data/collections'/dataset_id;dataset.mkdir(parents=True)
        rows=[{'platform':'douyin','content_id':'1','text':'我喜欢成都音乐节，找搭子','published_at':datetime.now(timezone.utc).isoformat(),'synthetic':True},
              {'platform':'douyin','content_id':'2','text':'我喜欢北京音乐节，找搭子','published_at':datetime.now(timezone.utc).isoformat(),'synthetic':True}]
        (dataset/'raw.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
        (dataset/'checkpoint.json').write_text(json.dumps({'status':'completed','records':2}),encoding='utf-8')
        launched=self.service.run(saved['revision'],'repeat-safe',spawn=False)
        result=execute(launched['run_id'],self.root)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['result']['candidate_contents'],1)
        self.assertEqual(result['result']['scope_excluded_contents'],1)
        self.assertEqual(result['result']['config_revision'],saved['revision'])
        again=self.service.run(saved['revision'],'repeat-safe',spawn=False)
        self.assertEqual(again['run_id'],launched['run_id']);self.assertEqual(again['status'],'completed')
        results=self.service.results(launched['run_id'],0,1)
        self.assertEqual(results['total'],2);self.assertEqual(len(results['items']),1)

    def test_plan_smooth_budget_approval_and_human_override_lock(self):
        # 1. 模拟 AI 提议调大 max_api_calls（例如从 40 调大到 80，在 500 次安全上限内）
        def compiler_increase(goal, bundle, caps):
            p = copy.deepcopy(bundle)
            p['task']['execution']['max_api_calls'] = 80
            return p, {'total_tokens': 15}

        current = self.service.get()
        plan_res = self.service.plan('测试安全边界内平滑批准', current['etag'], compiler=compiler_increase)
        self.assertTrue(plan_res['can_apply'])
        self.assertEqual(plan_res['bundle']['task']['unresolved'], [])
        self.assertEqual(plan_res['bundle']['task']['execution']['max_api_calls'], 80)
        self.assertTrue(any('自动批准' in a for a in plan_res['bundle']['task']['assumptions']))

        # 2. 模拟 AI 提议超出 500 次安全上限（例如提议 800 次）
        def compiler_over(goal, bundle, caps):
            p = copy.deepcopy(bundle)
            p['task']['execution']['max_api_calls'] = 800
            return p, {'total_tokens': 15}

        plan_over = self.service.plan('测试超出上限自动收敛', current['etag'], compiler=compiler_over)
        self.assertTrue(plan_over['can_apply'])
        self.assertEqual(plan_over['bundle']['task']['unresolved'], [])
        self.assertEqual(plan_over['bundle']['task']['execution']['max_api_calls'], 500)
        self.assertTrue(any('收敛至 500 次' in a for a in plan_over['bundle']['task']['assumptions']))

        # 3. 测试人工在 Web 端锁定 /task/execution/max_api_calls
        locked_bundle = copy.deepcopy(current['bundle'])
        locked_bundle['task']['execution']['max_api_calls'] = 60
        locked_state = self.service.store.save(locked_bundle, current['etag'], actor='human')
        self.assertIn('/task/execution/max_api_calls', locked_state['bundle']['task']['human_overrides'])

        # AI 再次尝试修改已被人工锁定的额度
        plan_locked = self.service.plan('测试人工锁定保护', locked_state['etag'], compiler=compiler_increase)
        self.assertTrue(plan_locked['can_apply'])
        self.assertEqual(plan_locked['bundle']['task']['unresolved'], [])
        # 额度保持人工锁定的 60，未被 AI 篡改
        self.assertEqual(plan_locked['bundle']['task']['execution']['max_api_calls'], 60)
        self.assertTrue(any('保持原人工设定' in a for a in plan_locked['bundle']['task']['assumptions']))

    def test_real_stdio_handshake_is_only_json_and_no_model_calls(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'screening_capabilities','arguments':{}}}]
        process=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/task_mcp.py')],input=''.join(json.dumps(m)+'\n' for m in messages).encode(),capture_output=True,timeout=10,cwd=ROOT)
        self.assertEqual(process.returncode,0)
        responses=[json.loads(line) for line in process.stdout.decode('utf-8').splitlines()]
        self.assertEqual(len(responses),3);self.assertEqual(len(responses[1]['result']['tools']),13)
        self.assertFalse(responses[2]['result']['structuredContent']['native_time_filter'])


if __name__=='__main__':unittest.main()
