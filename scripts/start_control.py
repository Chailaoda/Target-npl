"""Deployment-bound loopback launcher for integration and production."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from content_screening.runtime_context import RuntimeContext, preflight  # noqa: E402


def get_identity(port):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{port}/api/identity", timeout=1) as response:
        return json.load(response)


def assert_identity(actual, expected):
    fields = ("service", "protocol_version", "backend_package", "build_digest", "manifest_digest", "environment", "instance_id", "state_binding", "db_binding", "instance_binding", "schema_state")
    if any(actual.get(field) != expected.get(field) for field in fields):
        raise RuntimeError("实例身份不匹配，未接管或复用该端口")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", default=str(ROOT / "deployment.json"))
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    args = parser.parse_args()
    context = RuntimeContext.load(args.deployment)
    expected = preflight(context)
    port = context.deployment["port"]
    expected_port = 8767 if context.deployment["environment"] == "integration" else 8766
    if port != expected_port:
        raise SystemExit("端口与部署环境不一致")
    if args.foreground:
        os.environ["TARGET_NPL_DEPLOYMENT"] = str(context.deployment_path)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        if args.open:
            import webbrowser
            threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        from content_screening.control_server import main as server_main
        sys.argv = [sys.argv[0], "--port", str(port), "--deployment", str(context.deployment_path)]
        return server_main()
    try:
        assert_identity(get_identity(port), expected)
        print(f"http://127.0.0.1:{port}")
        if args.open:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}")
        return 0
    except (OSError, ValueError, RuntimeError):
        pass
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(f"{port} 被其他服务占用，未接管")
    if args.foreground:
        os.environ["TARGET_NPL_DEPLOYMENT"] = str(context.deployment_path)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        from content_screening.control_server import main as server_main
        sys.argv = [sys.argv[0], "--port", str(port), "--deployment", str(context.deployment_path)]
        return server_main()
    logs = context.state_path("output/control")
    logs.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TARGET_NPL_DEPLOYMENT=str(context.deployment_path), PYTHONDONTWRITEBYTECODE="1")
    with (logs / "server.log").open("ab") as log:
        server_executable = Path(sys.executable)
        if server_executable.name.lower() == "python.exe" and server_executable.with_name("pythonw.exe").is_file():
            server_executable = server_executable.with_name("pythonw.exe")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        child = subprocess.Popen([str(server_executable), "-X", "utf8", "-m", "content_screening.control_server", "--port", str(port), "--deployment", str(context.deployment_path)], cwd=context.app_root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags, close_fds=True)
    for _ in range(50):
        try:
            assert_identity(get_identity(port), expected)
            break
        except (OSError, ValueError):
            time.sleep(.2)
        if child.poll() is not None:
            raise SystemExit("控制端启动失败，请查看 output/control/server.log")
    else:
        raise SystemExit("控制端身份校验超时")
    (context.state_path("cache/control-server.json")).write_text(json.dumps({"pid": child.pid, "url": f"http://127.0.0.1:{port}", "instance_id": expected["instance_id"], "build_digest": expected["build_digest"]}), encoding="utf-8")
    print(f"http://127.0.0.1:{port}")
    if args.open:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
