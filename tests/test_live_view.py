import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

from content_screening.control_server import RuleStore,make_server
from content_screening.task_service import TaskService,atomic_json
from content_screening.task_contract import default_task
from content_screening.live_view import live,latest
from content_screening.workflow import run
from content_screening.rules_v2 import local_analyze

ROOT=Path(__file__).resolve().parents[1]


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('config/collection.json','profiles/default.json'):
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/name).read_bytes())
        self.store=RuleStore(self.root);self.service=TaskService(self.store)
        self.run_id='a'*32;self.folder=self.service.runs/self.run_id;self.folder.mkdir()
        self.state={'run_id':self.run_id,'status':'running','phase':'collecting','created_at':'2026-10-30T00:00:00Z'}
        self.dataset='20261030T000000000000Z';self.source=self.root/'data/collections'/self.dataset;self.source.mkdir(parents=True)
        atomic_json(self.folder/'status.json',self.state)
        atomic_json(self.folder/'collection-progress.json',{'dataset_id':self.dataset})

    def test_capture_waits_for_complete_line_and_does_not_expose_author(self):
        atomic_json(self.source/'checkpoint.json',{'status':'running','posts':1,'requests':1})
        row={'object_type':'post','keyword':'示例活动','data':{'aweme_id':'1','desc':'原文','nickname':'不应返回的作者','creator_hash':'private'}}
        (self.source/'raw.jsonl').write_text(json.dumps(row)+'\n{"unfinished":',encoding='utf-8')
        result=live(self.service,self.run_id)
        self.assertEqual(result['captured_total'],1);self.assertEqual(result['captured'][0]['text'],'原文')
        self.assertNotIn('不应返回',json.dumps(result,ensure_ascii=False))
        self.assertEqual(latest(self.service)['run_id'],self.run_id)

    def test_committed_progress_and_final_candidates_use_same_scope_as_report(self):
        rows=[{'content_id':str(i),'text':place+'示例活动，我喜欢，找搭子一起去','author':'','published_at':'2026-10-30T00:00:00Z'} for i,place in enumerate(['示例城市甲','示例城市乙'],1)]
        raw=self.source/'raw.jsonl';raw.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
        policy=json.loads((self.root/'profiles/default.json').read_text(encoding='utf-8-sig'))
        task=default_task(self.service.get()['bundle']);task['scope']['locations']=['示例城市甲']
        observations=[]
        def started(job_id):
            self.state.update(phase='analyzing',dataset_id=self.dataset,analysis_job_id=job_id)
            atomic_json(self.folder/'status.json',self.state)
        def analyze(record,context):
            observations.append(live(self.service,self.run_id))
            return local_analyze(record,policy,context)
        result=run(raw,self.root/'profiles/default.json',self.root/'data/screening.sqlite3',self.root/'output/task-runs'/self.run_id,
            mode='rules',task=task,analyzer=analyze,on_start=started)
        self.assertEqual([r['processed'] for r in observations],[0,1])
        self.state.update(status=result['status'],phase='finished',result=result);atomic_json(self.folder/'status.json',self.state)
        view=live(self.service,self.run_id)
        self.assertEqual(view['processed'],2)
        self.assertEqual(view['candidate_total'],result['candidate_contents'])
        self.assertEqual(view['candidate_total'],1)
        self.assertIn('示例城市甲',view['candidates'][0]['text'])
        server=make_server(0,self.store);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));url=f'http://127.0.0.1:{server.server_port}'
        with opener.open(url+'/api/state') as response:token=json.load(response)['token']
        request=urllib.request.Request(url+'/api/tasks/live',data=json.dumps({'run_id':self.run_id}).encode(),headers={'Origin':url,'Content-Type':'application/json','X-Control-Token':token})
        with opener.open(request) as response:self.assertEqual(json.load(response)['candidate_total'],1)
        with opener.open(url+'/reports/'+self.run_id) as response:
            self.assertIn('sandbox',response.headers['Content-Security-Policy']);self.assertIn(b'html',response.read())
        self.state['result']['report']=str(self.root/'config/collection.json');atomic_json(self.folder/'status.json',self.state)
        with self.assertRaises(urllib.error.HTTPError) as caught:opener.open(url+'/reports/'+self.run_id)
        self.assertEqual(caught.exception.code,404)

    def _child_lane(self,platform,dataset,status,requests,rows):
        """A child platform-live pointer + dataset, including its own checkpoint."""
        pointer=self.folder/'platform-live'/platform
        pointer.mkdir(parents=True,exist_ok=True)
        atomic_json(pointer/'collection-progress.json',{'dataset_id':dataset})
        source=self.root/'data/collections'/dataset
        source.mkdir(parents=True,exist_ok=True)
        atomic_json(source/'checkpoint.json',{'status':status,'requests':requests,'posts':len(rows)})
        (source/'raw.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
        return source

    # --- Round 8: Web lane status must follow the parent's verified verdict --
    def test_live_platform_status_uses_parent_verdict_not_child_checkpoint(self):
        # xhs crashed (exit 1) but left a 'completed' receipt AND a
        # 'completed' child checkpoint. Only the parent knows it failed.
        ok_rows=[{'platform':'bilibili','object_type':'post','data':{'content_id':str(i),'desc':'公开原文'}} for i in range(2)]
        bad_rows=[{'platform':'xhs','object_type':'post','data':{'content_id':str(i),'text':'失败平台不应显示'}} for i in range(3)]
        atomic_json(self.source/'checkpoint.json',{
            'status':'partial','aggregate':True,'posts':2,'comments':0,'requests':5,
            'settings':{'platforms':['bilibili','xhs']},'incomplete_platforms':['xhs'],
            'platform_statuses':[
                {'platform':'bilibili','status':'completed','records':2,'requests':2,
                 'exit_code':0,'receipt_status':'completed'},
                {'platform':'xhs','status':'failed','records':0,'requests':3,
                 'exit_code':1,'receipt_status':'completed'}]})
        (self.source/'raw.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in ok_rows),encoding='utf-8')
        self._child_lane('bilibili','20261030T000000000001Z','completed',2,ok_rows)
        self._child_lane('xhs','20261030T000000000002Z','completed',3,bad_rows)
        view=live(self.service,self.run_id)
        lanes={p['platform']:p for p in view['collection']['platform_statuses']}
        self.assertEqual(lanes['xhs']['status'],'failed')
        self.assertEqual(lanes['xhs']['exit_code'],1)
        self.assertEqual(lanes['xhs']['receipt_status'],'completed')
        self.assertEqual(lanes['bilibili']['status'],'completed')
        self.assertEqual(view['collection']['requests'],5)
        # Failed child rows must not reach the Web feed either.
        self.assertEqual(view['captured_total'],2)
        self.assertTrue(all(c['platform']=='bilibili' for c in view['captured']))
        self.assertNotIn('失败平台不应显示',json.dumps(view,ensure_ascii=False))

    def test_live_does_not_fabricate_completed_lane_without_parent_verdict(self):
        atomic_json(self.source/'checkpoint.json',{
            'status':'failed','aggregate':True,'posts':0,'requests':0,
            'settings':{'platforms':['xhs']},'platform_statuses':[]})
        (self.source/'raw.jsonl').write_text('',encoding='utf-8')
        self._child_lane('xhs','20261030T000000000002Z','completed',1,
            [{'platform':'xhs','object_type':'post','data':{'content_id':'1','text':'不应显示'}}])
        view=live(self.service,self.run_id)
        self.assertFalse(any(p.get('status') in ('completed','completed_empty')
                             for p in view['collection']['platform_statuses']))
        self.assertEqual(view['captured_total'],0)

    def test_live_still_shows_in_flight_progress_while_collecting(self):
        ok_rows=[{'platform':'bilibili','object_type':'post','data':{'content_id':str(i),'desc':'公开原文'}} for i in range(2)]
        running_rows=[{'platform':'xhs','object_type':'post','data':{'content_id':'1','text':'采集中原文'}}]
        atomic_json(self.source/'checkpoint.json',{
            'status':'running','aggregate':True,'requests':3,
            'settings':{'platforms':['bilibili','xhs']},
            'platform_statuses':[{'platform':'bilibili','status':'completed','records':2,'requests':2}]})
        (self.source/'raw.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in ok_rows),encoding='utf-8')
        self._child_lane('bilibili','20261030T000000000001Z','completed',2,ok_rows)
        self._child_lane('xhs','20261030T000000000002Z','running',1,running_rows)
        view=live(self.service,self.run_id)
        lanes={p['platform']:p for p in view['collection']['platform_statuses']}
        self.assertEqual(lanes['bilibili']['status'],'completed')
        self.assertEqual(lanes['xhs']['status'],'running')
        self.assertEqual(view['captured_total'],3)


if __name__=='__main__':unittest.main()
