from .platforms import PLATFORMS
"""Shared, versioned screening-task contract for agents, Web UI and workers."""
from datetime import date,datetime,timedelta,timezone
import copy
import re

VERSION=1


def default_task(bundle):
    return {"schema_version":1,"description":bundle['policy']['name'],
            "scope":{"locations":[],"recent_days":None,"published_after":None,"published_before":None},
            "exclusions":{"promotions":False,"terms":[]},
            "execution":{"source":"existing","dataset_id":"latest","mode":"ark","max_api_calls":100},
            "assumptions":[],"unresolved":[],"human_overrides":[]}


def string_list(value,name,limit=100,length=500):
    if not isinstance(value,list) or len(value)>limit or any(not isinstance(s,str) or not s.strip() or len(s)>length for s in value):
        raise ValueError(f'{name} 必须为不超过 {limit} 项的非空字符串列表')


def validate_task(t):
    if not isinstance(t,dict) or set(t)!={'schema_version','description','scope','exclusions','execution','assumptions','unresolved','human_overrides'} or type(t['schema_version']) is not int or t['schema_version']!=1:
        raise ValueError('任务模板 schema_version 或字段无效')
    if not isinstance(t['description'],str) or not 1<=len(t['description'].strip())<=4000:raise ValueError('目标描述长度应为 1–4000 字符')
    s=t['scope']
    if not isinstance(s,dict) or set(s)!={'locations','recent_days','published_after','published_before'}:raise ValueError('任务范围字段无效')
    string_list(s['locations'],'活动地点',50,80)
    if s['recent_days'] is not None and (type(s['recent_days']) is not int or not 1<=s['recent_days']<=3650):raise ValueError('最近天数应为 1–3650 或 null')
    for name in ('published_after','published_before'):
        if s[name] is not None:
            if not isinstance(s[name],str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',s[name]):raise ValueError('日期必须为 YYYY-MM-DD 或 null')
            date.fromisoformat(s[name])
    if s['published_after'] and s['published_before'] and s['published_after']>s['published_before']:raise ValueError('起始日期不能晚于结束日期')
    e=t['exclusions']
    if not isinstance(e,dict) or set(e)!={'promotions','terms'} or type(e['promotions']) is not bool:raise ValueError('排除条件字段无效')
    string_list(e['terms'],'字面排除词')
    x=t['execution']
    if not isinstance(x,dict) or set(x)!={'source','dataset_id','mode','max_api_calls'} or x['source'] not in ('existing','collect') or x['mode'] not in ('rules','ark') or type(x['max_api_calls']) is not int or not 1<=x['max_api_calls']<=500:raise ValueError('任务执行设置无效')
    if not isinstance(x['dataset_id'],str) or not (x['dataset_id']=='latest' or re.fullmatch(r'\d{8}T\d{12}Z',x['dataset_id'])):raise ValueError('只能选择已有采集批次标识或 latest')
    for name in ('assumptions','unresolved'):string_list(t[name],name,100,500)
    string_list(t['human_overrides'],'人工锁定字段',100,200)
    if any(not p.startswith(('/policy','/collection','/task')) for p in t['human_overrides']):raise ValueError('人工锁定路径无效')
    return t


def validate_execution(bundle):
    t=validate_task(bundle['task'])
    if t['unresolved']:raise ValueError('目标存在未解决事项，不能应用执行：'+'；'.join(t['unresolved']))
    return True


def changed_paths(before,after,prefix=''):
    if isinstance(before,dict) and isinstance(after,dict):
        keys=set(before)|set(after);result=[]
        for key in sorted(keys):
            path=prefix+'/'+str(key).replace('~','~0').replace('/','~1')
            if key not in before or key not in after:result.append(path)
            else:result.extend(changed_paths(before[key],after[key],path))
        return result
    return [] if before==after else [prefix]


def apply_patch(base,operations):
    if not isinstance(operations,list) or not 1<=len(operations)<=200:raise ValueError('patch 必须包含 1–200 个操作')
    result=copy.deepcopy(base)
    for op in operations:
        if not isinstance(op,dict) or set(op)-{'op','path','value'} or op.get('op') not in ('add','replace','remove'):raise ValueError('仅支持 JSON Patch add/replace/remove')
        path=op.get('path','')
        if not isinstance(path,str) or not path.startswith(('/policy','/collection','/task')):raise ValueError('patch 路径必须指向任务中的具体字段')
        parts=[p.replace('~1','/').replace('~0','~') for p in path[1:].split('/')]
        node=result
        for key in parts[:-1]:
            try:node=node[int(key)] if isinstance(node,list) else node[key]
            except (KeyError,ValueError,IndexError,TypeError):raise ValueError('patch 父路径不存在') from None
        key=parts[-1]
        if op['op']!='remove' and 'value' not in op:raise ValueError('patch 缺少 value')
        if isinstance(node,list):
            try:index=len(node) if key=='-' and op['op']=='add' else int(key)
            except ValueError:raise ValueError('数组下标无效') from None
            if index<0 or index>len(node) or (op['op']!='add' and index==len(node)):raise ValueError('数组下标越界')
            if op['op']=='add':node.insert(index,op['value'])
            elif op['op']=='remove':node.pop(index)
            else:node[index]=op['value']
        elif isinstance(node,dict):
            if op['op']!='add' and key not in node:raise ValueError('patch 字段不存在')
            if op['op']=='remove':del node[key]
            else:node[key]=op['value']
        else:raise ValueError('patch 目标不是对象或数组')
    return result


def apply_scope(result,record,context,task,reference_time):
    if not task:return result
    out=copy.deepcopy(result);scope=task['scope'];reasons=[];unknown=[]
    texts=[record['text']]+[c['text'] for c in context]
    if scope['locations'] and not any(term.casefold() in text.casefold() for term in scope['locations'] for text in texts):reasons.append('活动地点未在正文或关联原帖中出现')
    if scope['recent_days'] or scope['published_after'] or scope['published_before']:
        try:
            timestamp=datetime.fromisoformat(record['published_at'])
            if timestamp.tzinfo is None:raise ValueError()
            timestamp=timestamp.astimezone(timezone.utc);clock=datetime.fromisoformat(reference_time)
            if scope['recent_days'] and not clock-timedelta(days=scope['recent_days'])<=timestamp<=clock:reasons.append('不在任务规定的最近时间范围')
            if scope['published_after'] and timestamp.date()<date.fromisoformat(scope['published_after']):reasons.append('早于起始日期（UTC）')
            if scope['published_before'] and timestamp.date()>date.fromisoformat(scope['published_before']):reasons.append('晚于结束日期（UTC）')
        except (ValueError,TypeError,KeyError):unknown.append('本条发布时间缺失或不可靠，保留待核对')
    if task['exclusions']['promotions']:
        if result['promotion']=='yes':reasons.append('售票或推广表达')
        elif result.get('engine')=='ark' and result['promotion']=='uncertain':unknown.append('推广状态存疑，保留待核对')
    if any(term.casefold() in record['text'].casefold() for term in task['exclusions']['terms']):reasons.append('命中字面排除词（仅当前作者正文）')
    out['scope_check']={'status':'excluded' if reasons else 'unknown' if unknown else 'matched','reasons':reasons+unknown,'location_basis':'正文或关联原帖的活动地点，不是作者位置','time_basis':'当前对象发布时间；绝对日期按 UTC'}
    if reasons or unknown:
        out.update(accepted=False,score=0,evidence_level='insufficient')
        out['reason']='任务范围检查：'+'；'.join(reasons+unknown)+'。'+out['reason']
    return out


def capabilities():
    return {'contract_version':1,'platforms':list(PLATFORMS),'multi_platform':{'execution':'sequential','limits':'shared_pool_budget','login':'manual_per_platform','live_acceptance':'ready'},'source_filters':['keywords','bounded_pages','bounded_counts'],
            'post_filters':{'locations':'活动地点文字匹配：本人正文或关联原帖','recent_days':'当前对象发布时间，缺失保留待核对','published_after_before':'UTC 日期范围','exclude_promotions':'模型已有推广维度','exclude_terms':'仅本人正文的字面排除词'},
            'native_location_filter':False,'native_time_filter':False,'proxy_mode':'direct','identity_mode':'upstream_anonymized',
            'manual_edits':'ETag + immutable snapshots','run_modes':['existing','collect'],'max_api_calls':500}
