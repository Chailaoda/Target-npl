"""Isolated collection entry for six new platforms; no automatic login or media download."""
import argparse
import asyncio
from datetime import datetime,timezone
import importlib
import inspect
import json
import logging
import os
import re
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from content_screening.platforms import PLATFORMS
from content_screening.platform_adapter import attach_store,disable_request_retries,search,post_id,detail,comments,detail_id
from content_screening.platform_projection import project
from scripts.mediacrawler_collect import load_settings,ensure_browser,publish_result,release_browser,NEEDS_USER_CODES,PINNED_COMMIT,ALLOWED_UPSTREAM_COMMITS,FORK_COMMIT,verify_upstream
from content_screening import browser_window
from content_screening.task_service import atomic_json


class RequestGate:
    def __init__(self,settings,state,save):self.settings=settings;self.state=state;self.save=save;self.last=0
    async def before(self,url,method):
        u=urlsplit(str(url));platform=self.settings['platform']
        if u.scheme!='https' or u.hostname not in PLATFORMS[platform]['hosts'] or u.port not in (None,443) or method.upper() not in ('GET','POST'):
            raise ValueError('REQUEST_REJECTED')
        if self.state['requests']>=self.settings['max_requests']:raise ValueError('REQUEST_LIMIT')
        await asyncio.sleep(max(0,self.settings['request_interval_seconds']-(time.monotonic()-self.last)))
        self.last=time.monotonic();self.state['requests']+=1;self.save()
    def factory(self,**kwargs):
        import httpx
        if kwargs.pop('proxy',None) or kwargs.pop('proxies',None):raise ValueError('PROXY_REJECTED')
        async def before(request):await self.before(request.url,request.method)
        async def after(response):
            if response.status_code!=200:raise ValueError('HTTP_'+str(response.status_code))
            await response.aread()
            if len(response.content)>5_000_000:raise ValueError('RESPONSE_TOO_LARGE')
            # Stop before upstream retry loops can retry rate-limit / CAPTCHA responses.
            if 'json' in response.headers.get('content-type',''):
                data=response.json();p=self.settings['platform']
                if isinstance(data,dict) and ((p=='kuaishou' and 'result' in data and data['result']!=1) or (p=='xhs' and data.get('success') is False) or (p=='weibo' and data.get('ok')==0) or (p=='bilibili' and data.get('code',0)!=0) or (p=='zhihu' and data.get('error'))):
                    raise ValueError('PLATFORM_RESTRICTION')
        kwargs.update(trust_env=False,verify=True,follow_redirects=False,event_hooks={'request':[before],'response':[after]})
        return httpx.AsyncClient(**kwargs)


async def collect(settings,job,progress=None):
    platform=settings['platform'];spec=PLATFORMS[platform]
    raw=job/'raw.jsonl';raw.touch(exist_ok=False)
    upstream_sha=os.environ.get('TARGET_NPL_UPSTREAM_SHA','')
    identity='public_source' if os.environ.get('TARGET_NPL_PUBLIC_ACCOUNTS')=='1' and upstream_sha==FORK_COMMIT else 'upstream_anonymized'
    state={'status':'running','platform':platform,'settings':settings,'adapter_version':'multi-1','upstream_commit':upstream_sha or PINNED_COMMIT,
        'records':0,'posts':0,'comments':0,'requests':0,'errors':[],'pages_completed':[],'raw_file':str(raw),'proxy_mode':'direct',
        'identity_mode':identity,'cookies_exported':False,'created_at_utc':datetime.now(timezone.utc).isoformat()}
    def save():atomic_json(job/'checkpoint.json',state)
    save()
    if progress:atomic_json(progress,{'dataset_id':job.name})
    session_path=ROOT/f'cache/{platform}-browser-session.json'
    if not session_path.exists():
        state.update(status='needs_user_login',errors=[{'stage':'login','code':'LOGIN_NOT_PREPARED'}]);save();return state
    gate=RequestGate(settings,state,save)
    # All upstream HTTP client factories in this child process use one direct, bounded gate.
    import tools.httpx_util as httpx_util
    httpx_util.make_async_client=gate.factory
    import config
    config.ENABLE_IP_PROXY=False;config.ENABLE_GET_SUB_COMMENTS=False;config.CRAWLER_MAX_SLEEP_SEC=settings['request_interval_seconds']
    config.ENABLE_GET_MEDIAS=False;config.SAVE_DATA_OPTION='jsonl'
    logging.disable(logging.CRITICAL)
    core=importlib.import_module('media_platform.'+spec['module']+'.core')
    client_module=importlib.import_module('media_platform.'+spec['module']+'.client')
    if hasattr(client_module,'make_async_client'):client_module.make_async_client=gate.factory
    from playwright.async_api import async_playwright
    from var import source_keyword_var
    seen=set();seen_posts=set()
    class Sink:
        def write(self,kind,item):
            clean=project(platform,kind,item);key=(kind,clean['content_id'])
            if key in seen:return
            seen.add(key)
            with raw.open('a',encoding='utf-8') as stream:stream.write(json.dumps({'platform':platform,'object_type':kind,'keyword':source_keyword_var.get(),'data':clean},ensure_ascii=False)+'\n')
            state['records']+=1;state['posts' if kind=='post' else 'comments']+=1;save()
        async def store_content(self,content_item):self.write('post',content_item)
        async def store_comment(self,comment_item):self.write('comment',comment_item)
    save_post,save_comment=attach_store(platform,Sink())
    session=json.loads(session_path.read_text(encoding='utf-8'));ensure_browser(session,platform)
    async with async_playwright() as pw:
        browser=await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{session['debug_port']}",timeout=10000)
        if len(browser.contexts)!=1:raise ValueError('AMBIGUOUS_CONTEXT')
        context=browser.contexts[0];page=await context.new_page();keep=False
        browser_window.hide(session['pid'])
        try:
            await page.goto(spec['home'],wait_until='domcontentloaded',timeout=25000)
            crawler=getattr(core,spec['crawler'])();crawler.browser_context=context;crawler.context_page=page
            client=await getattr(crawler,spec['factory'])(None);disable_request_retries(client)
            if platform=='tieba':
                original=client._fetch_json_by_browser
                async def bounded_browser(uri,method='GET',**kwargs):
                    if uri not in ('/mo/q/search/multsearch','/c/s/pc/sync','/c/f/pb/page_pc'):raise ValueError('REQUEST_REJECTED')
                    await gate.before(spec['home']+uri,method)
                    return await original(uri,method=method,**kwargs)
                client._fetch_json_by_browser=bounded_browser
            authenticated=await client.pong(context) if platform=='tieba' else await client.pong()
            if not authenticated:
                keep=True;state.update(status='needs_user_login',errors=[{'stage':'login','code':'LOGIN_REQUIRED'}]);return state
            for keyword in settings['keywords']:
                source_keyword_var.set(keyword);cursor='';per_keyword=0
                if platform=='xhs':
                    from media_platform.xhs.help import get_search_id
                    cursor=get_search_id()
                for page_number in range(1,settings['pages_per_keyword']+1):
                    if state['posts']>=settings['max_posts'] or per_keyword>=settings['posts_per_keyword']:break
                    items,cursor=await search(platform,client,keyword,page_number,cursor)
                    for item in items:
                        cid=post_id(platform,item)
                        if not cid or cid in seen_posts:continue
                        if state['posts']>=settings['max_posts'] or per_keyword>=settings['posts_per_keyword']:break
                        body=await detail(platform,client,item)
                        await save_post(body);seen_posts.add(cid);per_keyword+=1
                        children=await comments(platform,client,body,settings['comments_per_post'])
                        for comment in children:
                            if platform=='zhihu':await save_comment(comment)
                            else:await save_comment(detail_id(platform,body),comment)
                    state['pages_completed'].append({'keyword':keyword,'page_index':page_number-1});save()
                    if not items:break
            state['status']='completed' if state['posts'] else 'completed_empty'
            return state
        except Exception as error:
            keep=str(error) in NEEDS_USER_CODES
            raise
        finally:
            await release_browser(browser,page,session['pid'],keep)
            save()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--receipt',required=True);parser.add_argument('--progress-file');args=parser.parse_args()
    settings=load_settings(args.config)
    if settings['platform']=='douyin':raise ValueError('Use existing Douyin adapter')
    receipt=Path(args.receipt).resolve()
    if not receipt.is_relative_to(ROOT/'output') or receipt.exists():raise ValueError('Invalid receipt')
    progress=Path(args.progress_file).resolve() if args.progress_file else None
    if progress and (not progress.is_relative_to(ROOT/'data/task-runs') or progress.name!='collection-progress.json'):raise ValueError('Invalid progress path')
    import subprocess
    upstream=ROOT/'vendor/MediaCrawler'
    sha=verify_upstream(upstream)
    # 同 mediacrawler_collect.py：显式 0 强制关闭；默认 auto 时受控 fork 自动开启。
    override=os.environ.get('TARGET_NPL_PUBLIC_ACCOUNTS')
    os.environ['TARGET_NPL_PUBLIC_ACCOUNTS']='' if override=='0' else ('1' if sha==FORK_COMMIT else '')
    os.environ['TARGET_NPL_UPSTREAM_SHA']=sha
    job=ROOT/'data/collections'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');job.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(upstream));os.chdir(upstream)
    try:state=asyncio.run(asyncio.wait_for(collect(settings,job,progress),timeout=settings['timeout_seconds']))
    except Exception as error:
        checkpoint=job/'checkpoint.json';state=json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {'records':0,'posts':0,'comments':0,'requests':0,'raw_file':str(job/'raw.jsonl'),'settings':settings}
        state.update(status='partial' if state['records'] else 'failed',errors=[{'stage':'collection','code':str(error) if re.fullmatch(r'HTTP_\d{3}',str(error)) or str(error) in ('REQUEST_LIMIT','PLATFORM_RESTRICTION','SOURCE_SHAPE_CHANGED','EMPTY_DETAIL','REQUEST_REJECTED','PROXY_REJECTED','RESPONSE_TOO_LARGE') else type(error).__name__}])
    import hashlib
    raw=job/'raw.jsonl'
    if raw.exists():state['raw_sha256']=hashlib.sha256(raw.read_bytes()).hexdigest()
    state['finished_at_utc']=datetime.now(timezone.utc).isoformat();state['job_directory']=str(job)
    atomic_json(job/'checkpoint.json',state);publish_result(state,receipt)
    return 0 if state['status'] in ('completed','completed_empty') else 2


if __name__=='__main__':raise SystemExit(main())
