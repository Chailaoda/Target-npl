from pathlib import Path
import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from festival_match.platforms import PLATFORMS,catalog,selected
from festival_match.platform_projection import project,timestamp
from festival_match.normalization import normalize,public_url
from festival_match.multi_collection import collect_selected
from festival_match.task_service import atomic_json,TaskService
from festival_match.control_server import RuleStore
from scripts.mediacrawler_collect import validate_settings,validate_session

ROOT=Path(__file__).resolve().parents[1]


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.settings=json.loads((ROOT/'config/collection.json').read_text(encoding='utf-8-sig'))
        self.folder=self.root/'data/task-runs'/('c'*32);self.folder.mkdir(parents=True)

    def test_selection_is_backward_compatible_and_rejects_invalid_pairs(self):
        self.assertEqual(selected(validate_settings(self.settings)),['douyin'])
        value=dict(self.settings,platforms=list(PLATFORMS));self.assertEqual(len(selected(validate_settings(value))),7)
        for ids in ([],['xhs'],['douyin','douyin'],['unknown']):
            with self.assertRaises(ValueError):validate_settings(dict(self.settings,platforms=ids))

    def test_projection_preserves_platform_relations_and_discards_private_fields(self):
        for platform in PLATFORMS:
            if platform=='douyin':continue
            raw={'comment_id':'2','note_id':'1','content':'本人找搭子','creator_hash':'hashed','create_time':1793352000000,
                'nickname':'private-name','user_id':'private-id','xsec_token':'private-token','parent_comment_id':'3'}
            clean=project(platform,'comment',raw)
            row=normalize({'platform':platform,'object_type':'comment','data':clean})
            self.assertEqual((row['platform'],row['root_id'],row['parent_id']),(platform,'1','3'))
            self.assertEqual(row['identity_mode'],'upstream_anonymized');self.assertTrue(row['published_at'].endswith('+00:00'))
            self.assertNotIn('private',json.dumps(row));self.assertEqual(row['text'],'本人找搭子')
        self.assertEqual(timestamp('昨天'),'');self.assertEqual(timestamp('2026-10-30 10:00'),'')
        self.assertEqual(public_url('https://www.douyin.com/video/1','xhs'),'')
        self.assertEqual(public_url('https://www.xiaohongshu.com/explore/1?xsec_token=secret','xhs'),'https://www.xiaohongshu.com/explore/1')

    def test_browser_profiles_cannot_cross_platform_or_use_proxy(self):
        session={'profile_path':str(ROOT/'cache/xhs-browser'),'debug_host':'127.0.0.1','debug_port':12345,'proxy_mode':'direct'}
        validate_session(session,'xhs')
        with self.assertRaises(ValueError):validate_session(session,'weibo')
        with self.assertRaises(ValueError):validate_session(dict(session,proxy_mode='system'),'xhs')
        before=list(self.root.rglob('*'));self.assertEqual(len(catalog(self.root)),7);self.assertEqual(list(self.root.rglob('*')),before)
        self.assertTrue(all(p['login_status']=='not_verified' for p in catalog(self.root)))

    def test_scheduler_shares_limits_merges_platforms_and_excludes_child_datasets(self):
        settings=dict(self.settings,platforms=['xhs','weibo'],platform='xhs',max_posts=3,max_requests=10,timeout_seconds=60)
        limits=[]
        def child(command,**kwargs):
            config=Path(command[command.index('--config')+1]);value=json.loads(config.read_text(encoding='utf-8'));limits.append(value)
            platform=value['platform'];cid='20261030T000000000001Z' if platform=='xhs' else '20261030T000000000002Z'
            folder=self.root/'data/collections'/cid;folder.mkdir(parents=True)
            raw=folder/'raw.jsonl';rows=[{'platform':platform,'object_type':'post','data':{'content_id':str(i),'text':'公开原文'}} for i in range(value['max_posts'])]
            raw.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
            state={'status':'completed','records':len(rows),'posts':len(rows),'comments':0,'requests':2,'raw_file':str(raw),'settings':value}
            atomic_json(folder/'checkpoint.json',state);atomic_json(Path(command[command.index('--receipt')+1]),state)
            return subprocess.CompletedProcess(command,0)
        with patch('festival_match.multi_collection.subprocess.run',side_effect=child):result=collect_selected(settings,self.folder,self.root)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['posts'],3)
        self.assertLessEqual(sum(v['max_posts'] for v in limits),3)
        self.assertTrue(all(v['proxy_mode']=='direct' for v in limits));self.assertEqual(result['requests'],4)
        rows=[json.loads(line) for line in Path(result['raw_file']).read_text(encoding='utf-8').splitlines()]
        self.assertEqual({r['platform'] for r in rows},{'xhs','weibo'})
        for name in ('config/collection.json','profiles/music_festival_v2.json'):
            path=self.root/name;path.parent.mkdir(exist_ok=True);path.write_bytes((ROOT/name).read_bytes())
        datasets=TaskService(RuleStore(self.root)).datasets();self.assertEqual(len(datasets),1)
        self.assertEqual(datasets[0]['platforms'],['xhs','weibo'])

    def test_login_requirement_does_not_launch_model_or_claim_complete(self):
        settings=dict(self.settings,platform='xhs',platforms=['xhs','weibo'],max_requests=10)
        def child(command,**kwargs):
            platform=json.loads(Path(command[command.index('--config')+1]).read_text(encoding='utf-8'))['platform']
            folder=self.root/'data/collections'/('20261030T000000000001Z' if platform=='xhs' else '20261030T000000000002Z');folder.mkdir(parents=True)
            raw=folder/'raw.jsonl';raw.touch();state={'status':'needs_user_login','records':0,'posts':0,'comments':0,'requests':0,'raw_file':str(raw)}
            atomic_json(folder/'checkpoint.json',state);atomic_json(Path(command[command.index('--receipt')+1]),state)
            return subprocess.CompletedProcess(command,2)
        with patch('festival_match.multi_collection.subprocess.run',side_effect=child):result=collect_selected(settings,self.folder,self.root)
        self.assertEqual(result['status'],'needs_user_login');self.assertEqual(result['requests'],0)
        self.assertEqual(len(result['platform_statuses']),2)

    def test_all_platforms_keep_distinct_content_and_anonymous_groups(self):
        from festival_match.workflow import run
        from festival_match.task_contract import default_task
        for name in ('config/collection.json','profiles/music_festival_v2.json'):
            path=self.root/name;path.parent.mkdir(exist_ok=True);path.write_bytes((ROOT/name).read_bytes())
        bundle=TaskService(RuleStore(self.root)).get()['bundle'];task=default_task(bundle)
        rows=[{'platform':platform,'object_type':'post','data':{'content_id':'same-id','text':'成都音乐节，我喜欢，找搭子一起去','creator_hash':'same-hash','anonymized':True}} for platform in PLATFORMS]
        source=self.root/'mixed.jsonl';source.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
        result=run(source,self.root/'profiles/music_festival_v2.json',self.root/'data/festival.sqlite3',self.root/'output',mode='rules',task=task)
        self.assertEqual(result['unique_contents'],7);self.assertEqual(result['candidate_anonymous_groups'],7)
        self.assertEqual(result['candidate_public_accounts'],0);self.assertEqual(result['api_calls'],0)
        report=Path(result['report']).read_text(encoding='utf-8')
        for platform in PLATFORMS:self.assertIn('平台 '+platform,report)

    def test_platform_http_catalog_and_manual_login_route_are_scoped(self):
        import threading
        import urllib.request
        from festival_match.control_server import make_server
        from festival_match.agent_mcp import AgentTools
        for name in ('config/collection.json','profiles/music_festival_v2.json'):
            path=self.root/name;path.parent.mkdir(exist_ok=True);path.write_bytes((ROOT/name).read_bytes())
        store=RuleStore(self.root);service=TaskService(store)
        tools=AgentTools(service);self.assertEqual(len(tools.call('screening_platforms',{})),7)
        with patch('festival_match.platforms.open_login',return_value={'status':'awaiting_manual_login'}) as open_window:
            result=tools.call('screening_platform_login',{'platform':'xhs'});self.assertEqual(result['status'],'awaiting_manual_login')
            open_window.assert_called_once_with(platform='xhs')
        server=make_server(0,store);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}';opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base+'/api/platforms') as response:self.assertEqual(len(json.load(response)),7)
        with opener.open(base+'/api/state') as response:token=json.load(response)['token']
        request=urllib.request.Request(base+'/api/platforms/open-login',data=b'{"platform":"xhs"}',headers={'Origin':base,'Content-Type':'application/json','X-Control-Token':token})
        with patch('festival_match.platforms.open_login',return_value={'status':'awaiting_manual_login'}) as open_window:
            with opener.open(request) as response:self.assertEqual(json.load(response)['status'],'awaiting_manual_login')
            open_window.assert_called_once_with('xhs')


if __name__=='__main__':unittest.main()
