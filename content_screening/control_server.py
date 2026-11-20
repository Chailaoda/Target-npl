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
from .http_api.preview_worker import PreviewWorker

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
    from .http_api.router import Router
    from .http_api import handlers_interest, handlers_task
    router=Router()
    handlers_interest.register(router)
    handlers_task.register(router)
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
            self.query=parse_qs(urlsplit(self.path).query)
            self.params={}
            self.body=None
            try:
                handler,params,_=self.router.resolve('GET',route)
                if handler:
                    self.params=params
                    return handler(self)
                if route.startswith('/api/'):
                    return self.send(404,{"error":"接口不存在"})
                return self.serve_static(route)
            except (ValueError,TypeError,KeyError) as error:self.send(400,{"error":str(error)})
            except OSError:self.send(404,{"error":"配置文件不存在或暂不可读"})
            except Exception:
                err_id=uuid.uuid4().hex[:8]
                logger.exception('Unhandled server error [err_id=%s method=%s path=%s]',err_id,self.command,route)
                self.send(500,{"error":f"服务处理失败，请稍后重试（错误编号：{err_id}）","error_id":err_id})

        def serve_static(self,route):
            relative='index.html' if route in ('/','/reference') else unquote(route).lstrip('/')
            file=(self.dist/relative).resolve()
            if not file.is_relative_to(self.dist.resolve()) or not file.is_file():
                return self.send(404,{"error":"页面不存在"})
            return self.send(200,file.read_bytes(),mimetypes.guess_type(file)[0] or 'application/octet-stream')

        def do_POST(self):
            set_context(self.server.runtime_context)
            if not self.allowed_host() or self.headers.get('Origin')!=f'http://127.0.0.1:{self.server.server_port}' or not secrets.compare_digest(self.headers.get('X-Control-Token',''),self.token):
                return self.send(403,{"error":"控制端来源或会话校验失败"})
            route=urlsplit(self.path).path
            self.query=parse_qs(urlsplit(self.path).query)
            self.params={}
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=262144 or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    raise ValueError("仅接受不超过 256KB 的 JSON")
                body=json.loads(self.rfile.read(size))
                handler,params,schema=self.router.resolve('POST',route)
                if not handler:
                    return self.send(404,{"error":"接口不存在"})
                self.params=params
                self.body=body
                from .http_api.router import validate_body
                validate_body(schema,body)
                return handler(self)
            except Conflict as error:self.send(409,{"error":str(error)})
            except subprocess.TimeoutExpired:self.send(400,{"error":"规则预览超时，请检查正则表达式"})
            except (ValueError,TypeError,KeyError) as error:self.send(400,{"error":str(error)})
            except Exception:
                err_id=uuid.uuid4().hex[:8]
                logger.exception('Unhandled server error [err_id=%s method=%s path=%s]',err_id,self.command,route)
                self.send(500,{"error":f"服务处理失败，原生效版本未改变（错误编号：{err_id}）","error_id":err_id})
    Handler.store=store
    Handler.tasks=tasks
    Handler.interests=interests
    Handler.context=context
    Handler.token=token
    Handler.preview_worker=preview_worker
    Handler.router=router
    Handler.dist=dist
    Handler.store=store
    Handler.tasks=tasks
    Handler.interests=interests
    Handler.context=context
    Handler.token=token
    Handler.preview_worker=preview_worker
    Handler.router=router
    Handler.dist=dist
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
