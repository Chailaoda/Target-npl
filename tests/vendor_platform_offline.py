"""Run with vendor/MediaCrawler/.venv Python. Fixtures only; no network/browser/model."""
import asyncio
import importlib
import json
import logging
from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'vendor/MediaCrawler'));os.chdir(ROOT/'vendor/MediaCrawler')
logging.disable(logging.CRITICAL)
from content_screening.platforms import PLATFORMS
from content_screening.platform_adapter import attach_store,search,detail,comments,detail_id
from content_screening.platform_projection import project
from content_screening.normalization import normalize
from scripts.platform_collect import RequestGate,collect
from model.m_baidu_tieba import TiebaNote,TiebaComment
from model.m_zhihu import ZhihuContent,ZhihuComment
from var import source_keyword_var


async def main():
    source_keyword_var.set('离线虚构测试')
    fixtures={
      'xhs':({'id':'p1','xsec_token':'never-persist-token'},{'note_id':'p1','desc':'原文找搭子','user':{'user_id':'fixture-person'},'time':1793352000000},
             {'id':'c1','content':'评论找同行','user_info':{'user_id':'fixture-person'},'create_time':1793352000000}),
      'kuaishou':({'photo':{'id':'p1','caption':'原文找搭子','timestamp':1793352000},'author':{'id':'fixture-person'}},None,
             {'commentId':'c1','content':'评论找同行','timestamp':1793352000000,'authorId':'fixture-person'}),
      'bilibili':({'aid':1},{'View':{'aid':1,'desc':'原文找搭子','pubdate':1793352000,'owner':{'mid':'fixture-person'},'stat':{}}},
             {'rpid':2,'parent':0,'ctime':1793352000,'content':{'message':'评论找同行'},'member':{'mid':'fixture-person'}}),
      'weibo':({'mblog':{'id':'p1','text':'<p>原文找搭子</p>','created_at':'Fri Oct 30 12:00:00 +0800 2026','user':{'id':'fixture-person'}}},None,
             {'id':'c1','text':'<p>评论找同行</p>','created_at':'Fri Oct 30 12:00:00 +0800 2026','user':{'id':'fixture-person'}}),
      'tieba':(TiebaNote(note_id='p1',title='测试',desc='原文找搭子',note_url='https://tieba.baidu.com/p/1',tieba_name='测试',tieba_link='',creator_hash='fixture-hash'),None,
             TiebaComment(comment_id='c1',content='评论找同行',note_id='p1',note_url='https://tieba.baidu.com/p/1',tieba_id='1',tieba_name='测试',tieba_link='',creator_hash='fixture-hash')),
      'zhihu':(ZhihuContent(content_id='p1',content_type='answer',content_text='<p>原文找搭子</p>',content_url='https://www.zhihu.com/question/1/answer/2',creator_hash='fixture-hash'),None,
             ZhihuComment(comment_id='c1',content='评论找同行',content_id='p1',content_type='answer',creator_hash='fixture-hash')),
    }
    checks=[]
    for platform,(summary,body,comment) in fixtures.items():
        spec=PLATFORMS[platform]
        crawler=getattr(importlib.import_module('media_platform.'+spec['module']+'.core'),spec['crawler'])()
        assert callable(getattr(crawler,spec['factory']))
        captured=[]
        class Sink:
            async def store_content(self,content_item):captured.append(('post',project(platform,'post',content_item)))
            async def store_comment(self,comment_item):captured.append(('comment',project(platform,'comment',comment_item)))
        save_post,save_comment=attach_store(platform,Sink())
        client=SimpleNamespace()
        if platform=='xhs':
            client.get_note_by_keyword=AsyncMock(return_value={'items':[summary]});client.get_note_by_id=AsyncMock(return_value=body);client.get_note_comments=AsyncMock(return_value={'comments':[comment]})
        elif platform=='kuaishou':
            client.search_info_by_keyword_v2=AsyncMock(return_value={'feeds':[summary]});client.get_video_comments=AsyncMock(return_value={'rootCommentsV2':[comment]})
        elif platform=='bilibili':
            client.search_video_by_keyword=AsyncMock(return_value={'result':[summary]});client.get_video_info=AsyncMock(return_value=body);client.get_video_comments=AsyncMock(return_value={'replies':[comment]})
        elif platform=='weibo':
            client.get_note_by_keyword=AsyncMock(return_value={'cards':[dict(summary,card_type=9)]});client.get_note_comments=AsyncMock(return_value={'data':[comment]})
        elif platform=='tieba':
            client.get_notes_by_keyword=AsyncMock(return_value=[summary]);client.get_note_by_id=AsyncMock(return_value=summary);client._get_pc_page_data=AsyncMock(return_value={});client._page_extractor=SimpleNamespace(extract_tieba_note_parent_comments_from_api=lambda *a,**kw:[comment])
        else:
            client.get_note_by_keyword=AsyncMock(return_value=[summary]);client.get_root_comments=AsyncMock(return_value={'data':[]});client._extractor=SimpleNamespace(extract_comments=lambda *a:[comment])
        found,_=await search(platform,client,'离线虚构测试',1,'fixture-search-id')
        record=await detail(platform,client,found[0]);await save_post(record)
        children=await comments(platform,client,record,1)
        for c in children:
            if platform=='zhihu':await save_comment(c)
            else:await save_comment(detail_id(platform,record),c)
        assert len(captured)==2,(platform,captured)
        normalized=[normalize({'platform':platform,'object_type':kind,'data':value}) for kind,value in captured]
        assert all(row['identity_mode']=='upstream_anonymized' for row in normalized)
        assert normalized[1]['root_id']==normalized[0]['content_id'],platform
        assert 'fixture-person' not in json.dumps(captured) and 'never-persist-token' not in json.dumps(captured)
        assert normalized[1]['text']=='评论找同行'
        checks.append(platform+': search/detail/comments + real upstream store conversion + normalized relations')
    import httpx
    state={'requests':0};settings={'platform':'xhs','max_requests':2,'request_interval_seconds':0}
    gate=RequestGate(settings,state,lambda:None)
    async with gate.factory(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'success':True,'data':{}}))) as client:
        assert client._trust_env is False
        await client.get('https://edith.xiaohongshu.com/test')
        await client.get('https://edith.xiaohongshu.com/test')
        try:await client.get('https://edith.xiaohongshu.com/test');raise AssertionError('budget not enforced')
        except ValueError as error:assert str(error)=='REQUEST_LIMIT'
    assert state['requests']==2
    try:gate.factory(proxy='http://invalid');raise AssertionError('proxy allowed')
    except ValueError:pass
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);job=root/'job';job.mkdir()
        with patch('scripts.platform_collect.ROOT',root):
            blocked=await collect({'platform':'xhs'},job)
        assert blocked['status']=='needs_user_login' and blocked['requests']==0
    proof={'offline_platform_checks':checks,'guard_checks':['direct trust_env=False','no proxy','request budget','missing login returns without launching browser'],
        'network_requests':0,'browser_launches':0,'model_calls':0,'live_acceptance':'pending manual login and real tests'}
    output=ROOT/'output/multiplatform';output.mkdir(parents=True,exist_ok=True)
    (output/'vendor-offline-verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(proof,ensure_ascii=False,indent=2))


if __name__=='__main__':asyncio.run(main())
