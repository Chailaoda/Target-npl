"""Start only this project's loopback control server, keeping helpers hidden."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.error
import webbrowser

ROOT=Path(__file__).resolve().parents[1]
URL='http://127.0.0.1:8766'
def identity():
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(URL+'/api/identity',timeout=1) as response:
            return json.load(response).get('service')=='target-npl-rule-control'
    except (OSError,ValueError):return False

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--open',action='store_true');args=parser.parse_args()
    if not identity():
        import socket
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1',8766))==0:raise SystemExit('8766 端口被其他服务占用，未改动该服务。')
        (ROOT/'output/control').mkdir(parents=True,exist_ok=True)
        with (ROOT/'output/control/server.log').open('ab') as log:
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)
            child=subprocess.Popen([sys.executable,'-X','utf8','-m','content_screening.control_server','--port','8766'],cwd=ROOT,stdout=log,stderr=log,creationflags=flags)
        for _ in range(30):
            if identity():break
            if child.poll() is not None:raise SystemExit('控制端启动失败，请查看 output/control/server.log')
            time.sleep(.2)
        else:raise SystemExit('控制端启动检查超时，请查看 output/control/server.log')
        (ROOT/'cache').mkdir(exist_ok=True)
        (ROOT/'cache/control-server.json').write_text(json.dumps({'pid':child.pid,'url':URL,'service':'target-npl-rule-control'}),encoding='utf-8')
    if args.open:webbrowser.open(URL)
    print(URL)
