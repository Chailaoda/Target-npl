from .platforms import PLATFORMS
"""Published JSON Schema and MCP tool definitions; no executable expressions."""
def task_schema():
    strings={'type':'array','items':{'type':'string','minLength':1,'maxLength':500},'maxItems':100}
    nullable_date={'anyOf':[{'type':'null'},{'type':'string','format':'date'}]}
    def obj(properties,required=None):return {'type':'object','properties':properties,'required':required or list(properties),'additionalProperties':False}
    scope=obj({'locations':{'type':'array','maxItems':20,'items':{'type':'string','minLength':1,'maxLength':80}},'recent_days':{'anyOf':[{'type':'null'},{'type':'integer','minimum':1,'maximum':3650}]},'published_after':nullable_date,'published_before':nullable_date})
    task=obj({'schema_version':{'const':1},'description':{'type':'string','minLength':1,'maxLength':4000},'scope':scope,
        'exclusions':obj({'promotions':{'type':'boolean'},'terms':strings}),
        'execution':obj({'source':{'enum':['existing','collect']},'dataset_id':{'type':'string','pattern':r'^(latest|\d{8}T\d{12}Z)$'},'mode':{'enum':['rules','ark']},'max_api_calls':{'type':'integer','minimum':1,'maximum':500}}),
        'assumptions':dict(strings,maxItems=100),'unresolved':dict(strings,maxItems=100),'human_overrides':{'type':'array','maxItems':100,'items':{'type':'string','maxLength':200,'pattern':r'^/(policy/|collection/|task/(scope|execution|exclusions)/)'}}})
    dims={'topic':['relevant','unrelated','uncertain'],'stance':['positive','negative','neutral','unclear'],'intent':['seeking_company','planning','attended','none','unclear'],'promotion':['yes','no','uncertain'],'author_role':['individual','organizer','unknown']}
    patterns={'type':'object','additionalProperties':strings}
    policy=obj({'schema_version':{'const':2},'id':{'type':'string','pattern':'^[a-zA-Z0-9_-]{1,64}$'},'name':{'type':'string','minLength':1,'maxLength':120},'topic_patterns':dict(strings,minItems=1),'ambiguous_reply_patterns':strings,
        'dimensions':obj({d:{'type':'array','items':{'enum':values},'uniqueItems':True,'minItems':len(values),'maxItems':len(values)} for d,values in dims.items()}),'rules':patterns,'decision_gates':patterns,
        'acceptance':obj({'stances':strings,'intents':strings}),'display':obj({'sort_weights':{'type':'object','additionalProperties':{'type':'integer','minimum':0,'maximum':100}},'include_related':{'type':'boolean'}})},
        ['schema_version','id','name','topic_patterns','ambiguous_reply_patterns','dimensions','rules','acceptance','display'])
    bounds={'pages_per_keyword':(1,20),'posts_per_keyword':(1,100),'max_posts':(1,500),'comments_per_post':(0,100),'request_interval_seconds':(1,30),'max_requests':(1,500),'timeout_seconds':(20,1800)}
    from .platforms import PLATFORMS
    collection=obj({'schema_version':{'const':1},'platform':{'enum':list(PLATFORMS)},'proxy_mode':{'const':'direct'},'keywords':{'type':'array','minItems':1,'maxItems':20,'items':{'type':'string','minLength':1,'maxLength':80}},**{k:{'type':'integer','minimum':a,'maximum':b} for k,(a,b) in bounds.items()}})
    collection['properties']['platforms']={'type':'array','items':{'enum':list(PLATFORMS)},'minItems':1,'maxItems':7,'uniqueItems':True}
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'urn:target-npl:screening-task:1','title':'统一筛选任务包 v1',**obj({'task':task,'policy':policy,'collection':collection})}


def tool_definitions():
    def tool(name,description,properties=None,required=None,read_only=False):
        return {'name':name,'description':description,'inputSchema':{'type':'object','properties':properties or {},'required':required or [],'additionalProperties':False},
                'annotations':{'readOnlyHint':read_only,'destructiveHint':False,'openWorldHint':name in ('screening_task_plan','screening_task_run','screening_task_submit','screening_platform_login')}}
    bundle=task_schema();text={'type':'string'}
    patch={'type':'array','minItems':1,'maxItems':200,'items':{'type':'object','properties':{'op':{'enum':['add','replace','remove']},'path':text,'value':{}},'required':['op','path'],'additionalProperties':False}}
    return [tool('screening_platforms','只读平台接入目录；配置浏览器不代表已登录或真实采集验收通过',read_only=True),
        tool('screening_platform_login','仅在用户准备本人登录时打开指定平台的专用直连窗口；不自动登录或启动采集',{'platform':{'enum':list(PLATFORMS)}},['platform']),
        tool('screening_capabilities','读取真实采集和后处理能力，地域/时间不是平台原生过滤',read_only=True),
        tool('screening_task_get','读取人工最新修改后的统一任务、ETag 和生效版本；调整前必须先读取',read_only=True),
        tool('screening_task_submit','在已授权额度内，一次自然语言目标自动制定、校验、应用并启动；同 request_id 不重复规划/启动，关键问题返回 needs_attention',{'goal':{'type':'string','minLength':1,'maxLength':4000},'base_etag':text,'request_id':{'type':'string','pattern':'^[A-Za-z0-9_-]{1,64}$'}},['goal','base_etag','request_id']),
        tool('screening_task_plan','一次模型调用把自然语言目标编译为当前模板的字段变更；只返回草稿，不执行或应用',{'goal':{'type':'string','minLength':1,'maxLength':4000},'base_etag':text},['goal','base_etag']),
        tool('screening_task_validate','校验完整任务、真实能力和执行额度，不调用模型',{'bundle':bundle},['bundle'],True),
        tool('screening_task_apply','应用统一任务新版本，ETag 冲突或人工锁定则拒绝；不会立即执行',{'bundle':bundle,'base_etag':text},['bundle','base_etag']),
        tool('screening_task_patch','用 JSON Patch 微调具体字段，保留人工锁定和版本冲突保护',{'operations':patch,'base_etag':text},['operations','base_etag']),
        tool('screening_datasets','列出可以复用的已完成真实采集批次',read_only=True),
        tool('screening_task_run','按明确版本执行已校验任务；遵守额度/直连/数据来源设置；同 request_id 幂等，不重复计费启动',{'revision':text,'request_id':{'type':'string','pattern':'^[A-Za-z0-9_-]{1,64}$'}},['revision','request_id']),
        tool('screening_task_status','查询后台任务真实阶段与结果摘要，不触发重跑',{'run_id':text},['run_id'],True),
        tool('screening_task_results','分页读取实际结果及原文证据，不把匿名作者分组称为确认人数',{'run_id':text,'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':100}},['run_id'],True)]
