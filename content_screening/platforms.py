"""Local platform registry. Catalog readiness is not proof of live authentication."""
from pathlib import Path
import json
import socket

PLATFORMS={
    'douyin':{'name':'抖音','upstream':'dy','home':'https://www.douyin.com/jingxuan','module':'douyin','crawler':'DouYinCrawler','factory':'create_douyin_client','hosts':['www.douyin.com']},
    'xhs':{'name':'小红书','upstream':'xhs','home':'https://www.xiaohongshu.com','module':'xhs','crawler':'XiaoHongShuCrawler','factory':'create_xhs_client','hosts':['www.xiaohongshu.com','edith.xiaohongshu.com']},
    'kuaishou':{'name':'快手','upstream':'ks','home':'https://www.kuaishou.com','module':'kuaishou','crawler':'KuaishouCrawler','factory':'create_ks_client','hosts':['www.kuaishou.com']},
    'bilibili':{'name':'B站','upstream':'bili','home':'https://www.bilibili.com','module':'bilibili','crawler':'BilibiliCrawler','factory':'create_bilibili_client','hosts':['www.bilibili.com','api.bilibili.com','space.bilibili.com']},
    'weibo':{'name':'微博','upstream':'wb','home':'https://m.weibo.cn','module':'weibo','crawler':'WeiboCrawler','factory':'create_weibo_client','hosts':['m.weibo.cn','weibo.com','www.weibo.com','weibo.cn','m.weibo.cn']},
    'tieba':{'name':'百度贴吧','upstream':'tieba','home':'https://tieba.baidu.com','module':'tieba','crawler':'TieBaCrawler','factory':'create_tieba_client','hosts':['tieba.baidu.com']},
    'zhihu':{'name':'知乎','upstream':'zhihu','home':'https://www.zhihu.com','module':'zhihu','crawler':'ZhihuCrawler','factory':'create_zhihu_client','hosts':['www.zhihu.com','zhihu.com']},
}


def selected(collection):
    return collection.get('platforms',[collection['platform']])


def catalog(root=None):
    from .runtime_context import current_context
    root=Path(root or current_context().state_root)
    health=_recent_platform_states(root)
    items=[]
    for key,item in PLATFORMS.items():
        recent=health.get(key,{})
        items.append({'id':key,'name':item['name'],'upstream':item['upstream'],'adapter':'available',
            'browser_configured':(root/f'cache/{key}-browser-session.json').exists(),
            'login_status':'not_verified','live_acceptance':'pending_user_login_and_live_test',
            'last_status':recent.get('status',''),'last_run_at':recent.get('at',''),
            'needs_login':recent.get('status')=='needs_user_login',
            'warning_count':recent.get('warning_count',0)})
    return items


def _recent_platform_states(root, runs_to_scan=8):
    """Last known lane state per platform, read from local run artifacts only.

    Opening the workbench must never probe the seven platforms, so readiness is
    derived from what the last few local runs already recorded. Newer runs win;
    a lane that never appeared simply has no status.
    """
    result={}
    runs=sorted((Path(root)/'data/task-runs').glob('*/status.json'),
                key=lambda p:p.stat().st_mtime, reverse=True)[:runs_to_scan]
    for path in runs:
        try:
            state=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            continue
        at=state.get('created_at','')
        dataset=state.get('dataset_id')
        if not dataset:
            pointer=path.parent/'collection-progress.json'
            if pointer.exists():
                try:dataset=json.loads(pointer.read_text(encoding='utf-8')).get('dataset_id')
                except (OSError,ValueError):dataset=None
        statuses=[{'platform':state.get('active_platform'),'status':state.get('status','')}]
        if dataset:
            checkpoint=Path(root)/'data/collections'/dataset/'checkpoint.json'
            if checkpoint.exists():
                try:
                    data=json.loads(checkpoint.read_text(encoding='utf-8'))
                except (OSError,ValueError):
                    data={}
                statuses=data.get('platform_statuses') or statuses
        for entry in statuses:
            platform=entry.get('platform') if isinstance(entry,dict) else None
            if not platform or platform in result:
                continue
            result[platform]={'status':entry.get('status',''),'at':at,
                              'warning_count':entry.get('warning_count',0)}
    return result


def budget_status(root):
    """Today's budget usage; read-only and offline."""
    from .budget_controller import BudgetController
    try:
        status=BudgetController(root).status()
    except OSError:
        return {'posts_used':0,'posts_limit':0,'api_calls_used':0,'api_calls_limit':0}
    return {
        'posts_used':status['collection_posts']['used'],
        'posts_limit':status['collection_posts']['limit'],
        'api_calls_used':status['api_calls']['used'],
        'api_calls_limit':status['api_calls']['limit'],
    }


def vendor_status(root):
    """Offline vendor preflight summary; never logs in or calls out."""
    from .vendor_env import preflight
    try:
        ok,payload=preflight(root=root)
    except (OSError,ValueError) as error:
        return {'ok':False,'mode':'unknown','code':type(error).__name__}
    summary={'ok':ok,'mode':payload.get('identity_mode') or payload.get('mode') or 'unknown'}
    if payload.get('code'):
        summary['code']=payload['code']
    return summary


def health(root=None):
    from .runtime_context import current_context
    root=Path(root or current_context().state_root)
    return {'platforms':catalog(root),'budget':budget_status(root),'vendor_preflight':vendor_status(root)}


def open_login(platform):
    from .runtime_context import current_context
    context=current_context()
    if not context.deployment.get("test_isolated"):
        context.require_external('login')
    ROOT=context.state_root
    from scripts.mediacrawler_collect import ensure_browser
    if platform not in PLATFORMS:raise ValueError('不支持的平台')
    path=ROOT/f'cache/{platform}-browser-session.json';path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():session=json.loads(path.read_text(encoding='utf-8'))
    else:
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        session={'profile_path':str(ROOT/f'cache/{platform}-browser'),'debug_host':'127.0.0.1','debug_port':port,'proxy_mode':'direct','pid':0}
    ensure_browser(session,platform,visible=True)
    return {'platform':platform,'status':'awaiting_manual_login','message':f"{PLATFORMS[platform]['name']}专用窗口已显示在桌面；请本人登录或处理验证码，完成后下次采集会自动转入后台。"}
