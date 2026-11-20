"""Loopback-only configuration UI. No credentials, collection or model API calls."""
import argparse
from contextlib import contextmanager
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from urllib.parse import urlsplit,unquote,parse_qs
import uuid

from .active_config import ROOT,active_snapshot
from .pipeline import fingerprint
from .rules_v2 import validate_policy,analysis_policy
from .task_contract import validate_task,changed_paths,default_task
from .runtime_context import RuntimeContext,current_context,set_context,preflight

logger=logging.getLogger('content_screening.control_server')


def configure_logging(context):
    """Rotating, local-only server log with no request headers or query strings."""
    folder=Path(context.state_root)/'output/control'
    folder.mkdir(parents=True,exist_ok=True)
    target=str((folder/'server.log').resolve())
    for handler in list(logger.handlers):
        if getattr(handler,'baseFilename',None)==target:
            return logger
        # A different state root (tests, integration instance) needs its own file.
        logger.removeHandler(handler)
        handler.close()
    handler=RotatingFileHandler(target,maxBytes=2_000_000,backupCount=3,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate=False
    return logger


class Conflict(ValueError):
    pass


class PreviewWorker:
    """Long-lived sandbox process for rule previews.

    The interpreter start (~300-600ms) was paid on every keystroke preview.
    The process is kept warm, requests are serialized by a lock, and a request
    that exceeds its deadline gets the whole process killed: a runaway regex
    must never be able to block the next preview or eat the CPU.
    """

    def __init__(self, context, timeout=4.0):
        self.context = context
        self.timeout = timeout
        self._lock = threading.Lock()
        self._proc = None
        self._queue = None

    def _spawn(self):
        import queue
        self._proc = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'content_screening.rule_preview', '--daemon'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(self.context.app_root),
            env=dict(os.environ, TARGET_NPL_DEPLOYMENT=str(self.context.deployment_path), PYTHONDONTWRITEBYTECODE='1'))
        self._queue = queue.Queue()
        stream = self._proc.stdout
        def pump():
            try:
                for line in stream:
                    self._queue.put(line)
            except (OSError, ValueError):
                pass
        threading.Thread(target=pump, name='preview-reader', daemon=True).start()

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _kill(self):
        proc, self._proc = self._proc, None
        self._queue = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def evaluate(self, payload, timeout=None):
        import queue
        timeout = self.timeout if timeout is None else timeout
        with self._lock:
            if not self._alive():
                self._spawn()
            try:
                self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
                self._proc.stdin.flush()
                line = self._queue.get(timeout=timeout)
            except queue.Empty:
                self._kill()
                raise subprocess.TimeoutExpired(cmd='rule_preview', timeout=timeout)
            except (BrokenPipeError, OSError) as error:
                self._kill()
                raise subprocess.SubprocessError('规则预览进程已退出') from error
            return json.loads(line)

    def close(self):
        with self._lock:
            self._kill()


@contextmanager
def write_lock(path):
    import time
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline=time.monotonic()+10
    while True:
        stream=None
        try:
            stream=path.open('a+b')
            if stream.tell()==0:stream.write(b'0');stream.flush()
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            break
        except (PermissionError,OSError):
            if stream is not None:stream.close()
            if time.monotonic()>=deadline:raise
            time.sleep(.05)
    with stream:
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def validate_bundle(bundle):
    if not isinstance(bundle,dict) or not {'policy','collection'}<=set(bundle) or set(bundle)-{'policy','collection','task'}:
        raise ValueError("导入内容应包含 policy 与 collection")
    validate_policy(bundle["policy"])
    from scripts.mediacrawler_collect import validate_settings
    validate_settings(bundle["collection"])
    if 'task' in bundle:validate_task(bundle['task'])
    return bundle


class RuleStore:
    def __init__(self,root=None):
        self.root=Path(root) if root is not None else current_context().state_root
        self.lock=threading.RLock()
        self.versions=self.root/'data/control/versions'
        # The store owns these directories; creating them here keeps the first
        # save from failing on a fresh state root or a test-owned root.
        self.versions.mkdir(parents=True,exist_ok=True)
        (self.root/'config').mkdir(parents=True,exist_ok=True)
        self._history=None

    def state(self):
        with self.lock:
            snapshot=active_snapshot(self.root)
            bundle=validate_bundle(snapshot['bundle'])
            revision=snapshot['revision']
            return {"revision":revision,"etag":fingerprint({"revision":revision,"bundle":bundle}),"bundle":bundle}

    def save(self,bundle,etag,actor='human'):
        validate_bundle(bundle)
        with self.lock,write_lock(self.root/'data/control/write.lock'):
            current=self.state()
            if etag!=current['etag']:
                raise Conflict("配置已被其他窗口更新，草稿已保留，请重新读取后合并")
            if bundle==current['bundle']:
                return dict(current,unchanged=True)
            import copy
            bundle=copy.deepcopy(bundle)
            if 'task' not in bundle and 'task' in current['bundle']:bundle['task']=copy.deepcopy(current['bundle']['task'])
            if actor=='human' and 'task' in bundle:
                before=copy.deepcopy(current['bundle']);before.setdefault('task',default_task(before))
                edits=[p for p in changed_paths(before,bundle) if p.startswith(('/policy/','/collection/','/task/scope/','/task/exclusions/','/task/execution/'))]
                bundle['task']['human_overrides']=sorted(set(bundle['task']['human_overrides'])|set(edits))
            revision=uuid.uuid4().hex
            folder=self.versions/revision
            folder.mkdir()
            metadata={"revision":revision,"parent":current['revision'],"created_at":datetime.now(timezone.utc).isoformat(),"name":bundle['policy']['name'],"bundle_hash":fingerprint(bundle),'actor':actor}
            files=[('policy.json',bundle['policy']),('collection.json',bundle['collection'])]
            if 'task' in bundle:files.append(('task.json',bundle['task']))
            for filename,obj in files+[('metadata.json',metadata)]:
                with (folder/filename).open('x',encoding='utf-8') as f:
                    f.write(json.dumps(obj,ensure_ascii=False,indent=2));f.flush();os.fsync(f.fileno())
            pointer=self.root/'config/control-active.json'
            temporary=pointer.with_suffix('.tmp')
            with temporary.open('w',encoding='utf-8') as f:
                f.write(json.dumps({"revision":revision,"bundle_hash":fingerprint(bundle)},indent=2));f.flush();os.fsync(f.fileno())
            temporary.replace(pointer)
            self._history=None
            return self.state()

    def history(self):
        """Newest-first snapshot list, indexed once and invalidated on write.

        Scanning every metadata.json on each /api/versions poll was O(versions)
        disk I/O for an immutable list. save() is the only writer, so it is the
        only place that has to drop the cache.
        """
        with self.lock:
            if self._history is None:
                entries=[]
                for p in self.versions.glob('*/metadata.json'):
                    entries.append(json.loads(p.read_text(encoding='utf-8')))
                self._history=sorted(entries,key=lambda x:x['created_at'],reverse=True)[:100]
            return [dict(entry) for entry in self._history]

    def version(self,revision):
        if len(revision)!=32 or any(c not in '0123456789abcdef' for c in revision):
            raise ValueError("版本标识无效")
        folder=self.versions/revision
        bundle=validate_bundle({"policy":json.loads((folder/'policy.json').read_text(encoding='utf-8')),"collection":json.loads((folder/'collection.json').read_text(encoding='utf-8'))})
        if (folder/'task.json').exists():bundle['task']=validate_task(json.loads((folder/'task.json').read_text(encoding='utf-8')))
        metadata=json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
        if metadata['bundle_hash']!=fingerprint(bundle):
            raise ValueError("历史快照校验失败")
        return bundle


def make_server(port=8766,store=None,context=None):
    context=context or current_context()
    configure_logging(context)
    store=store or RuleStore(context.state_root)
    from .task_service import TaskService
    from .task_schema import task_schema
    from .task_contract import capabilities
    tasks=TaskService(store)
    from .interest_service import InterestService
    interests=InterestService(context)
    preview_worker=PreviewWorker(context)
    token=secrets.token_urlsafe(32)
    dist=context.app_root/'control-web/dist'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            # BaseHTTPRequestHandler logs every request line, including query
            # strings and the access token when it travels in a header dump.
            # Only failures and writes are worth persisting.
            if not logger.handlers:
                return
            # args = (format, requestline, code, size) when called by log_request
            # and (format, message) when called by log_error.
            try:
                code=int(args[2]) if len(args)>=4 else 0
            except (IndexError,TypeError,ValueError):
                code=0
            if self.command=='GET' and code and code<400:
                return
            logger.info('%s %s %s',self.command,urlsplit(self.path).path,code or '-')

        def send(self,status,body,content_type='application/json; charset=utf-8',report=False):
            if isinstance(body,(dict,list)):
                body=json.dumps(body,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy',"sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'" if report else "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers();self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            set_context(self.server.runtime_context)
            if not self.allowed_host():
                return self.send(403,{"error":"仅允许本机控制端访问"})
            route=urlsplit(self.path).path
            query=parse_qs(urlsplit(self.path).query)
            try:
                if route=='/api/interests/topics':
                    return self.send(200,{'items':interests.recent_topics(query.get('limit',[8])[0])})
                if route=='/api/accounts':
                    return self.send(200,interests.accounts(query.get('keyword',[''])[0],query.get('limit',[50])[0],query.get('offset',[0])[0]))
                if route=='/api/interests/search':
                    if 'topic' not in query:raise ValueError('必须提供兴趣主题')
                    allowed={'topic','min_score','platform','window','min_evidence','stance','sort','limit','offset'}
                    if set(query)-allowed:raise ValueError('兴趣筛选参数无效')
                    options={key:query[key][0] for key in allowed-{'topic'} if key in query}
                    return self.send(200,interests.search(query['topic'][0],**options))
                if route=='/api/interests/analyze':
                    return self.send(200,interests.topic_status(query.get('topic',[''])[0]))
                if route=='/api/reviews/pending':
                    from .db_access import resolve_db
                    from .workflow import pending_reviews
                    return self.send(200,pending_reviews(
                        resolve_db(context),
                        run_id=query.get('run_id',[''])[0] or None,
                        limit=query.get('limit',['50'])[0]))
                if route=='/api/accounts/conflicts':
                    return self.send(200,interests.list_conflicts(
                        status=query.get('status',['pending'])[0],
                        account_key=query.get('account_key',[''])[0] or None,
                        limit=query.get('limit',['50'])[0]))
                if route.startswith('/api/accounts/'):
                    parts=route.split('/')
                    if len(parts)<4 or not parts[3]:return self.send(404,{'error':'账号不存在'})
                    key=unquote(parts[3])
                    if len(parts)==4:
                        result=interests.account(key)
                        return self.send(200,result) if result else self.send(404,{'error':'账号不存在或没有可靠公开身份'})
                    if len(parts)==5 and parts[4]=='interests':
                        result=interests.account(key)
                        return self.send(200,{'account_key':key,'items':result['interests']}) if result else self.send(404,{'error':'账号不存在'})
                    if len(parts)==7 and parts[4]=='interests' and parts[6]=='evidence':
                        if not interests.account(key):return self.send(404,{'error':'账号不存在'})
                        return self.send(200,interests.evidence(key,unquote(parts[5]),query.get('window',['all'])[0]))
                    return self.send(404,{'error':'人物接口不存在'})
                if route=='/api/platforms':
                    from .platforms import health
                    return self.send(200,health(store.root))
                if route=='/api/tasks/latest':
                    from .live_view import latest
                    return self.send(200,latest(tasks))
                if route.startswith('/reports/'):
                    run_id=route.rsplit('/',1)[1]
                    state=tasks.status(run_id)
                    report=Path(state.get('result',{}).get('report','')).resolve()
                    expected=(store.root/'output/task-runs'/run_id).resolve()
                    if not report.is_relative_to(expected) or not report.is_file():return self.send(404,{'error':'报告尚未生成'})
                    return self.send(200,report.read_bytes(),'text/html; charset=utf-8',report=True)
                if route=='/api/identity':
                    from .runtime_context import RuntimeErrorBase
                    try:
                        return self.send(200,preflight(context))
                    except RuntimeErrorBase as error:
                        return self.send(503,{"error":str(error),"code":error.code})
                if route in ('/api/state','/api/tasks/state'):return self.send(200,dict(tasks.get(),token=token))
                if route=='/api/tasks/schema':return self.send(200,task_schema())
                if route=='/api/tasks/capabilities':return self.send(200,capabilities())
                if route=='/api/tasks/datasets':return self.send(200,tasks.datasets())
                if route=='/api/versions':return self.send(200,store.history())
                if route.startswith('/api/versions/'):return self.send(200,store.version(route.rsplit('/',1)[1]))
                if route.startswith('/api/'):return self.send(404,{"error":"接口不存在"})
                relative='index.html' if route in ('/','/reference') else unquote(route).lstrip('/')
                file=(dist/relative).resolve()
                if not file.is_relative_to(dist.resolve()) or not file.is_file():return self.send(404,{"error":"页面不存在"})
                self.send(200,file.read_bytes(),mimetypes.guess_type(file)[0] or 'application/octet-stream')
            except (ValueError,TypeError,KeyError) as error:self.send(400,{"error":str(error)})
            except OSError:self.send(404,{"error":"配置文件不存在或暂不可读"})
            except Exception:
                err_id=uuid.uuid4().hex[:8]
                logger.exception('Unhandled server error [err_id=%s method=%s path=%s]',err_id,self.command,urlsplit(self.path).path)
                self.send(500,{"error":f"服务处理失败，请稍后重试（错误编号：{err_id}）","error_id":err_id})

        def do_POST(self):
            set_context(self.server.runtime_context)
            if not self.allowed_host() or self.headers.get('Origin')!=f'http://127.0.0.1:{self.server.server_port}' or not secrets.compare_digest(self.headers.get('X-Control-Token',''),token):
                return self.send(403,{"error":"控制端来源或会话校验失败"})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=262144 or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    raise ValueError("仅接受不超过 256KB 的 JSON")
                obj=json.loads(self.rfile.read(size))
                route=urlsplit(self.path).path
                if route=='/api/interests/analyze':
                    if not isinstance(obj,dict) or set(obj)-{'topic','aliases','exclude_terms','description','account_key'}:
                        raise ValueError('兴趣分析参数无效')
                    return self.send(200,interests.analyze(**obj))
                if route=='/api/interests/evidence-feedback':
                    allowed={'evidence_id','action','note','profile_version'}
                    if not isinstance(obj,dict) or not {'evidence_id','action'} <= set(obj) or set(obj)-allowed:
                        raise ValueError('证据反馈参数无效')
                    return self.send(200,interests.feedback(**obj))
                if route=='/api/accounts/conflicts/resolve':
                    allowed={'platform','object_type','content_id','chosen_key','note'}
                    if not isinstance(obj,dict) or not {'platform','object_type','content_id','chosen_key'}<=set(obj) or set(obj)-allowed:
                        raise ValueError('冲突裁决参数无效')
                    return self.send(200,interests.resolve_conflict(**obj))
                if route=='/api/reviews':
                    if not isinstance(obj,dict) or set(obj)!={'items'}:
                        raise ValueError('人工复核参数无效')
                    from .db_access import resolve_db
                    from .workflow import review_many
                    return self.send(200,review_many(resolve_db(context),obj['items']))
                if route=='/api/platforms/open-login':
                    from .platforms import open_login
                    if not isinstance(obj,dict) or set(obj)!={'platform'}:raise ValueError('平台参数无效')
                    return self.send(200,open_login(obj['platform']))
                if route.startswith('/api/tasks/'):
                    name=route.rsplit('/',1)[1]
                    from .live_view import live
                    methods={'plan':tasks.plan,'submit':tasks.submit,'validate':tasks.validate,'apply':tasks.apply,'patch':tasks.patch,'run':tasks.run,'status':tasks.status,'results':tasks.results,'live':lambda run_id,since_seq=None:live(tasks,run_id,since_seq)}
                    if name=='save-draft':
                        plan_id=obj['plan_id']
                        if not isinstance(plan_id,str) or len(plan_id)!=32 or any(c not in '0123456789abcdef' for c in plan_id):raise ValueError('plan_id 无效')
                        plan=json.loads((store.root/'data/task-plans'/f'{plan_id}.json').read_text(encoding='utf-8'))
                        if plan['base_etag']!=obj['etag']:raise Conflict('计划基础版本不一致，请重新读取')
                        bundle=validate_bundle(obj['bundle']);tasks.validate(bundle)
                        manual=[p for p in changed_paths(plan['bundle'],bundle) if p.startswith(('/policy/','/collection/','/task/scope/','/task/exclusions/','/task/execution/'))]
                        bundle['task']['human_overrides']=sorted(set(bundle['task']['human_overrides'])|set(manual))
                        return self.send(200,store.save(bundle,obj['etag'],actor='agent+human'))
                    if name not in methods:return self.send(404,{'error':'任务接口不存在'})
                    # Do not expose internal spawn/test parameters to a client.
                    allowed={'plan':{'goal','base_etag'},'submit':{'goal','base_etag','request_id'},'validate':{'bundle'},'apply':{'bundle','base_etag'},'patch':{'operations','base_etag'},'run':{'revision','request_id'},'status':{'run_id'},'results':{'run_id','offset','limit'},'live':{'run_id','since_seq'}}
                    if not isinstance(obj,dict) or set(obj)-allowed[name]:raise ValueError('任务接口参数无效')
                    return self.send(200,methods[name](**obj))
                if route=='/api/validate':
                    bundle=validate_bundle(obj['bundle']);current=store.state()['bundle']
                    changed=[k for k in bundle['policy'] if bundle['policy'].get(k)!=current['policy'].get(k)]
                    return self.send(200,{"valid":True,"changed":changed,"collection_changed":bundle['collection']!=current['collection'],"requires_new_analysis":analysis_policy(bundle['policy'])!=analysis_policy(current['policy']),"proxy_mode":"direct"})
                if route=='/api/save':return self.send(200,store.save(obj['bundle'],obj['etag']))
                if route=='/api/preview':
                    validate_policy(obj['policy'])
                    if not isinstance(obj.get('text'),str) or not 1<=len(obj['text'])<=4000:raise ValueError("测试文本长度应为 1–4000 字符")
                    preview_input={'policy':obj['policy'],'text':obj['text'],'context_text':obj.get('context_text'),'task':obj.get('task')}
                    payload=preview_worker.evaluate(preview_input)
                    return self.send(400 if 'error' in payload else 200,payload)
                return self.send(404,{"error":"接口不存在"})
            except Conflict as error:self.send(409,{"error":str(error)})
            except subprocess.TimeoutExpired:self.send(400,{"error":"规则预览超时，请检查正则表达式"})
            except (ValueError,TypeError,KeyError) as error:self.send(400,{"error":str(error)})
            except Exception:
                err_id=uuid.uuid4().hex[:8]
                logger.exception('Unhandled server error [err_id=%s method=%s path=%s]',err_id,self.command,urlsplit(self.path).path)
                self.send(500,{"error":f"服务处理失败，原生效版本未改变（错误编号：{err_id}）","error_id":err_id})
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.runtime_context=context
    server.daemon_threads=True
    server.preview_worker=preview_worker
    close_server=server.server_close
    def close_with_worker(*args,**kwargs):
        try:preview_worker.close()
        finally:close_server(*args,**kwargs)
    server.server_close=close_with_worker
    return server


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--deployment')
    parser.add_argument('--preflight',action='store_true')
    args=parser.parse_args()
    context=RuntimeContext.load(args.deployment)
    if args.preflight:
        print(json.dumps(preflight(context),ensure_ascii=False,indent=2));return
    identity=preflight(context);set_context(context)
    if args.port!=context.deployment['port']:
        raise SystemExit('端口必须与隔离部署清单一致')
    server=make_server(args.port,context=context)
    print(f'规则控制端：http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
