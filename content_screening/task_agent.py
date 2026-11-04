"""Command-line bridge for agents that do not speak MCP."""
import argparse
import json
import time
import uuid
from .task_service import TaskService
from .agent_mcp import AgentTools


if __name__=='__main__':
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
    call=sub.add_parser('call');call.add_argument('--tool',required=True);call.add_argument('--arguments-file')
    active=sub.add_parser('run-active');active.add_argument('--wait',action='store_true')
    sub.add_parser('platforms')
    login=sub.add_parser('open-login');login.add_argument('--platform',required=True)
    args=parser.parse_args()
    try:
        if args.command=='call':
            from pathlib import Path
            parameters=json.loads(Path(args.arguments_file).read_text(encoding='utf-8-sig')) if args.arguments_file else {}
            result=AgentTools().call(args.tool,parameters)
        elif args.command in ('platforms','open-login'):
            from .platforms import catalog,open_login
            result=catalog() if args.command=='platforms' else open_login(args.platform)
        else:
            service=TaskService();state=service.get();result=service.run(state['revision'],uuid.uuid4().hex)
            while args.wait and result['status'] in ('queued','running'):
                time.sleep(3);result=service.status(result['run_id'])
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,OSError,KeyError) as error:raise SystemExit(str(error))
