"""Local platform registry. Catalog readiness is not proof of live authentication."""
from pathlib import Path
import json
import socket

PLATFORMS={
    'douyin':{'name':'抖音','upstream':'dy','home':'https://www.douyin.com/jingxuan','module':'douyin','crawler':'DouYinCrawler','factory':'create_douyin_client','hosts':['www.douyin.com']},
    'xhs':{'name':'小红书','upstream':'xhs','home':'https://www.xiaohongshu.com','module':'xhs','crawler':'XiaoHongShuCrawler','factory':'create_xhs_client','hosts':['www.xiaohongshu.com','edith.xiaohongshu.com']},
    'kuaishou':{'name':'快手','upstream':'ks','home':'https://www.kuaishou.com','module':'kuaishou','crawler':'KuaishouCrawler','factory':'create_ks_client','hosts':['www.kuaishou.com']},
    'bilibili':{'name':'B站','upstream':'bili','home':'https://www.bilibili.com','module':'bilibili','crawler':'BilibiliCrawler','factory':'create_bilibili_client','hosts':['www.bilibili.com','api.bilibili.com']},
    'weibo':{'name':'微博','upstream':'wb','home':'https://m.weibo.cn','module':'weibo','crawler':'WeiboCrawler','factory':'create_weibo_client','hosts':['m.weibo.cn','weibo.com','www.weibo.com']},
    'tieba':{'name':'百度贴吧','upstream':'tieba','home':'https://tieba.baidu.com','module':'tieba','crawler':'TieBaCrawler','factory':'create_tieba_client','hosts':['tieba.baidu.com']},
    'zhihu':{'name':'知乎','upstream':'zhihu','home':'https://www.zhihu.com','module':'zhihu','crawler':'ZhihuCrawler','factory':'create_zhihu_client','hosts':['www.zhihu.com']},
}


def selected(collection):
    return collection.get('platforms',[collection['platform']])


def catalog(root=None):
    from .active_config import ROOT
    root=Path(root or ROOT)
    return [{'id':key,'name':item['name'],'upstream':item['upstream'],'adapter':'available',
        'browser_configured':(root/f'cache/{key}-browser-session.json').exists(),
        'login_status':'not_verified','live_acceptance':'pending_user_login_and_live_test'} for key,item in PLATFORMS.items()]


def open_login(platform):
    from .active_config import ROOT
    from scripts.mediacrawler_collect import ensure_browser
    if platform not in PLATFORMS:raise ValueError('不支持的平台')
    path=ROOT/f'cache/{platform}-browser-session.json';path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():session=json.loads(path.read_text(encoding='utf-8'))
    else:
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        session={'profile_path':str(ROOT/f'cache/{platform}-browser'),'debug_host':'127.0.0.1','debug_port':port,'proxy_mode':'direct','pid':0}
    ensure_browser(session,platform,visible=True)
    return {'platform':platform,'status':'awaiting_manual_login','message':f"{PLATFORMS[platform]['name']}专用窗口已显示在桌面；请本人登录或处理验证码，完成后下次采集会自动转入后台。"}
