"""Minimal stdio MCP tools transport (JSON-RPC, newline-delimited UTF-8)."""
import json
import sys
from .task_service import TaskService
from .task_contract import capabilities
from .task_schema import tool_definitions


class AgentTools:
    def __init__(self,service=None):self.service=service or TaskService()
    def call(self,name,args):
        definition=next((d for d in tool_definitions() if d['name']==name),None)
        if not definition:raise ValueError('未知工具')
        schema=definition['inputSchema']
        if not isinstance(args,dict) or set(args)-set(schema['properties']) or not set(schema['required'])<=set(args):raise ValueError('工具参数缺失或包含不支持字段')
        from .platforms import catalog,open_login
        methods={'screening_platforms':lambda:catalog(self.service.root),'screening_platform_login':open_login,'screening_capabilities':capabilities,'screening_task_get':self.service.get,'screening_task_plan':self.service.plan,
                 'screening_task_submit':self.service.submit,
                 'screening_task_validate':self.service.validate,'screening_task_apply':self.service.apply,'screening_task_patch':self.service.patch,
                 'screening_datasets':self.service.datasets,'screening_task_run':self.service.run,'screening_task_status':self.service.status,'screening_task_results':self.service.results}
        return methods[name](**args)


def dispatch(message,tools):
    if not isinstance(message,dict) or message.get('jsonrpc')!='2.0':return {'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request'}}
    if 'id' not in message:return None
    rid=message['id'];method=message.get('method')
    if not isinstance(message.get('params',{}),dict):return {'jsonrpc':'2.0','id':rid,'error':{'code':-32602,'message':'Invalid params'}}
    if method=='initialize':
        requested=message.get('params',{}).get('protocolVersion')
        version=requested if requested in ('2024-11-05','2025-03-26','2025-06-18','2025-11-25') else '2025-11-25'
        result={'protocolVersion':version,'capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'target-npl-screening','version':'1.0.0'},
                'instructions':'先 capabilities/get，按最新 ETag 计划/校验/应用，明确版本和幂等 request_id 后执行。人工锁定不能覆盖，地域时间为后处理。'}
    elif method=='ping':result={}
    elif method=='tools/list':result={'tools':tool_definitions()}
    elif method=='tools/call':
        try:
            params=message.get('params',{});data=tools.call(params['name'],params.get('arguments',{}))
            result={'content':[{'type':'text','text':json.dumps(data,ensure_ascii=False)}],'structuredContent':data if isinstance(data,dict) else {'items':data},'isError':False}
        except (ValueError,KeyError,TypeError,OSError) as error:
            result={'content':[{'type':'text','text':str(error)}],'isError':True}
    else:return {'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'Method not found'}}
    return {'jsonrpc':'2.0','id':rid,'result':result}


def serve():
    tools=AgentTools()
    for line in sys.stdin:
        try:
            if len(line)>524288:raise ValueError('Request too large')
            response=dispatch(json.loads(line),tools)
        except (ValueError,TypeError):response={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Parse error'}}
        if response is not None:print(json.dumps(response,ensure_ascii=False),flush=True)


if __name__=='__main__':serve()
