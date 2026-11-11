from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
import io
import sys

from festival_match.active_config import active_paths
from festival_match.control_server import RuleStore,make_server,Conflict

ROOT=Path(__file__).resolve().parents[1]


class ControlTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'tmp/tests').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tmp/tests');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for folder in ('profiles','config'):(self.root/folder).mkdir()
        for relative in ('profiles/music_festival_v2.json','config/collection.json'):
            (self.root/relative).write_bytes((ROOT/relative).read_bytes())
        self.store=RuleStore(self.root)

    def test_atomic_bundle_selection_and_history(self):
        original=self.store.state();bundle=copy.deepcopy(original['bundle'])
        bundle['policy']['name']='测试整套规则';bundle['collection']['keywords']=['测试关键词']
        saved=self.store.save(bundle,original['etag'])
        policy,collection=active_paths(self.root)
        self.assertEqual(json.loads(policy.read_text(encoding='utf-8'))['name'],'测试整套规则')
        self.assertEqual(json.loads(collection.read_text(encoding='utf-8'))['keywords'],['测试关键词'])
        self.assertEqual(self.store.version(saved['revision']),bundle)
        self.assertEqual(len(self.store.history()),1)
        restored=self.store.save(original['bundle'],saved['etag'])
        self.assertNotEqual(restored['etag'],original['etag']) # ABA protection.
        with self.assertRaises(Conflict):self.store.save(bundle,original['etag'])

    def test_invalid_regex_proxy_or_snapshot_cannot_be_activated(self):
        original=self.store.state()
        for change in ('regex','proxy','unknown'):
            bundle=copy.deepcopy(original['bundle'])
            if change=='regex':bundle['policy']['topic_patterns']=['[']
            elif change=='proxy':bundle['collection']['proxy_mode']='system'
            else:bundle['policy']['unknown_key']='bad'
            with self.assertRaises(ValueError):self.store.save(bundle,original['etag'])
        self.assertEqual(self.store.state(),original)
        self.assertEqual(self.store.history(),[])
        changed=copy.deepcopy(original['bundle']);changed['policy']['name']='新版本'
        saved=self.store.save(changed,original['etag'])
        policy,_=active_paths(self.root);policy.write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):self.store.state()

    def test_parallel_writers_one_conflict_even_between_stores(self):
        original=self.store.state()
        def save(i):
            bundle=copy.deepcopy(original['bundle']);bundle['policy']['name']=f'并发 {i}'
            try:RuleStore(self.root).save(bundle,original['etag']);return 'saved'
            except Conflict:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(save,[1,2]))
        self.assertEqual(sorted(results),['conflict','saved'])

    def test_workflow_default_reads_saved_snapshot_instead_of_baseline(self):
        original=self.store.state();bundle=copy.deepcopy(original['bundle'])
        bundle['policy']['display']['sort_weights']['positive']=7
        self.store.save(bundle,original['etag'])
        selected=active_paths(self.root)
        source=self.root/'source.jsonl'
        source.write_text(json.dumps({'platform':'douyin','content_id':'1','text':'我喜欢音乐节'},ensure_ascii=False),encoding='utf-8')
        from festival_match.workflow import main
        output=io.StringIO()
        args=['workflow','run','--input',str(source),'--db',str(self.root/'data.sqlite'),'--output',str(self.root/'out')]
        with patch('festival_match.active_config.active_paths',return_value=selected),patch.object(sys,'argv',args),patch('sys.stdout',output):
            self.assertEqual(main(),0)
        manifest=json.loads(output.getvalue())
        record=json.loads(Path(manifest['export']).read_text(encoding='utf-8'))
        self.assertEqual(record['result']['score'],7)
        self.assertEqual(json.loads((self.root/'profiles/music_festival_v2.json').read_text(encoding='utf-8-sig'))['display']['sort_weights']['positive'],70)

    def test_local_api_validation_preview_and_origin_controls(self):
        server=make_server(0,self.store);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f'http://127.0.0.1:{server.server_port}'
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url+'/api/state') as response:state=json.load(response)
        def request(route,body,origin=url,token=state['token']):
            req=urllib.request.Request(url+route,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':origin,'X-Control-Token':token})
            return opener.open(req)
        with request('/api/validate',{'bundle':state['bundle']}) as response:self.assertTrue(json.load(response)['valid'])
        with request('/api/preview',{'policy':state['bundle']['policy'],'text':'我喜欢音乐节'}) as response:self.assertTrue(json.load(response)['accepted'])
        for origin,token in (('https://evil.example',state['token']),(url,'bad-token')):
            with self.assertRaises(urllib.error.HTTPError) as error:request('/api/save',{'bundle':state['bundle'],'etag':state['etag']},origin,token)
            self.assertEqual(error.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as error:opener.open(url+'/../config/connection.json')
        self.assertEqual(error.exception.code,404)


if __name__=='__main__':unittest.main()
