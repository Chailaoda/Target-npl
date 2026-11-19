"""Bounded upstream client operations. Imported only by the isolated vendor process."""
import importlib
import inspect
import types

STORE_NAMES={'xhs':('XhsStoreFactory','update_xhs_note','update_xhs_note_comment'),
    'kuaishou':('KuaishouStoreFactory','update_kuaishou_video','update_ks_video_comment'),
    'bilibili':('BiliStoreFactory','update_bilibili_video','update_bilibili_video_comment'),
    'weibo':('WeibostoreFactory','update_weibo_note','update_weibo_note_comment'),
    'tieba':('TieBaStoreFactory','update_tieba_note','update_tieba_note_comment'),
    'zhihu':('ZhihuStoreFactory','update_zhihu_content','update_zhihu_content_comment')}


def attach_store(platform,sink):
    module=importlib.import_module('store.'+platform)
    factory,post,comment=STORE_NAMES[platform]
    getattr(module,factory).create_store=staticmethod(lambda:sink)
    return getattr(module,post),getattr(module,comment)


def disable_request_retries(client):
    method=type(client).request
    client.request=types.MethodType(inspect.unwrap(method),client)


def require_list(value):
    if not isinstance(value,list):raise ValueError('SOURCE_SHAPE_CHANGED')
    return value


async def search(platform,client,keyword,page,cursor):
    if platform=='xhs':
        data=await client.get_note_by_keyword(keyword=keyword,page=page,search_id=cursor)
        return [item for item in require_list(data.get('items',[])) if item.get('model_type') not in ('rec_query','hot_query')],cursor
    if platform=='kuaishou':
        data=await client.search_info_by_keyword_v2(keyword=keyword,pcursor=str(page),search_session_id=cursor)
        return require_list(data.get('feeds',[])),data.get('searchSessionId','')
    if platform=='bilibili':
        data=await client.search_video_by_keyword(keyword=keyword,page=page,page_size=20)
        return require_list(data.get('result',[])),cursor
    if platform=='weibo':
        from media_platform.weibo.help import filter_search_result_card
        data=await client.get_note_by_keyword(keyword=keyword,page=page)
        return filter_search_result_card(require_list(data.get('cards',[]))),cursor
    if platform=='tieba':return require_list(await client.get_notes_by_keyword(keyword=keyword,page=page)),cursor
    return require_list(await client.get_note_by_keyword(keyword=keyword,page=page)),cursor


def post_id(platform,item):
    if platform=='xhs':return str(item.get('id') or item.get('note_id') or '')
    if platform=='kuaishou':return str(item.get('photo',{}).get('id') or '')
    if platform=='bilibili':return str(item.get('aid') or '')
    if platform=='weibo':return str(item.get('mblog',{}).get('id') or '')
    return str(getattr(item,'note_id' if platform=='tieba' else 'content_id',''))


async def detail(platform,client,item):
    cid=post_id(platform,item)
    if platform=='xhs':
        data=await client.get_note_by_id(cid,item.get('xsec_source','pc_search'),item.get('xsec_token',''))
        if not data:raise ValueError('EMPTY_DETAIL')
        # Required for reading comments; never persisted by project projection.
        data['xsec_token']=item.get('xsec_token','')
        return data
    if platform=='bilibili':return await client.get_video_info(aid=int(cid),bvid=None)
    if platform=='tieba':return await client.get_note_by_id(cid)
    return item


async def comments(platform,client,item,limit):
    if not limit:return []
    if platform=='xhs':
        data=await client.get_note_comments(item['note_id'],item.get('xsec_token',''))
        return require_list(data.get('comments',[]))[:limit]
    if platform=='kuaishou':
        data=await client.get_video_comments(str(item['photo']['id']))
        return require_list(data.get('rootCommentsV2',[]))[:limit]
    if platform=='bilibili':
        data=await client.get_video_comments(str(item['View']['aid']))
        return require_list(data.get('replies') or [])[:limit]
    if platform=='weibo':
        data=await client.get_note_comments(str(item['mblog']['id']),max_id=0)
        return require_list(data.get('data',[]))[:limit]
    if platform=='tieba':
        data=await client._get_pc_page_data(item.note_id,1)
        return client._page_extractor.extract_tieba_note_parent_comments_from_api(data,note_detail=item)[:limit]
    data=await client.get_root_comments(item.content_id,item.content_type,limit=limit)
    return client._extractor.extract_comments(item,data.get('data',[]))[:limit]


def detail_id(platform,item):
    if platform=='xhs':return str(item['note_id'])
    if platform=='bilibili':return str(item['View']['aid'])
    return post_id(platform,item)
