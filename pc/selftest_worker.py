# -*- coding: utf-8 -*-
"""回归测试：上传进来的 paste 任务，必须真的被 worker 消费掉。

这个测试是为一个真实事故写的：
`uploader.py` 里 `import inkvoice as V`，而服务是以 `pythonw inkvoice.py`
启动的（模块名 __main__）。那句 import 会把文件再执行一遍，生成**第二份
jobs 队列**；上传的任务进了没人消费的队列，任务永远停在 queued，
表现为「按了语音键，电脑不出字，但日志说收到了」。

判据：上传后 15 秒内 state 必须离开 queued。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
PY = sys.executable
PORT = 8794


def wait_port(port, seconds=25):
    end = time.time() + seconds
    while time.time() < end:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    import nethelp as NH
    data = tempfile.mkdtemp(prefix="vk_worker_")
    env = os.environ.copy()
    env.update({"VK_PORT": str(PORT), "VK_DATA_DIR": data, "VK_AUTOPASTE": "0",
                "VK_RATE_LIMIT": "10000", "VK_IP_BYTES_PER_MIN": str(50 * 1024 * 1024)})
    p = subprocess.Popen([PY, os.path.join(BASE, "inkvoice.py")], cwd=BASE, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PORT):
            print("  [FAIL] 隔离实例没起来")
            return 1
        base = "http://127.0.0.1:%d" % PORT
        import urllib.request

        def req(path, method="GET", body=None, headers=None):
            r = urllib.request.Request(base + path, data=body, method=method,
                                       headers=dict(headers or {}))
            try:
                with urllib.request.urlopen(r, timeout=20) as f:
                    return f.status, f.read()
            except Exception as e:
                return getattr(e, "code", type(e).__name__), b""

        cid = str(uuid.uuid4())
        audio = open(os.path.join(BASE, "_silent.m4a"), "rb").read()
        h = {"Authorization": "Bearer " + NH.token(), "Content-Type": "audio/mp4",
             "X-Clip-Id": cid}
        code, _ = req("/upload?mode=paste", "POST", audio, h)
        if code not in (200, 202):
            print("  [FAIL] 上传没被接受：", code)
            return 1

        state = "queued"
        for _ in range(15):
            time.sleep(1)
            c, b = req("/jobs/" + cid, headers={"Authorization": "Bearer " + NH.token()})
            state = json.loads(b or b"{}").get("state")
            if state != "queued":
                break
        print(("  [OK]   " if state != "queued" else "  [FAIL] ") +
              "上传的任务被消费：state=%s" % state)
        return 0 if state != "queued" else 1
    finally:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            p.kill()


if __name__ == "__main__":
    sys.exit(main())
