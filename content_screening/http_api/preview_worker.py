"""Long-lived sandbox process for untrusted rule previews.

Kept next to the HTTP layer because it exists only to serve /api/preview.
"""
import json
import os
import subprocess
import sys
import threading


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
