# -*- coding: utf-8 -*-
"""上传接口的请求级保护自检（起一个隔离实例，不动生产服务）。

测的是「评审点出的那一类漏洞」：单看文件大小上限不够，还要看
  - 请求体读取有没有超时
  - 同时接收的上传数有没有上限
  - 队列满时是不是**在读 body 之前**就拒绝
  - 单个 IP 的总上传字节有没有预算
  - chunked / 没有 Content-Length 是不是明确拒绝

跑法：python selftest_upload.py
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
PY = sys.executable
TOK = ""
RESULTS = []


def check(ok, name, detail=""):
    RESULTS.append(bool(ok))
    print(("  [OK]   " if ok else "  [FAIL] ") + name + ("  ← " + detail if detail else ""))


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


def start(port, data, **extra):
    env = os.environ.copy()
    env.update({
        "VK_PORT": str(port), "VK_DATA_DIR": data, "VK_AUTOPASTE": "0",
        "VK_RATE_LIMIT": "100000", "VK_MAX_UPLOAD": str(64 * 1024),
        "VK_READ_TIMEOUT": "2", "VK_IP_BYTES_PER_MIN": str(200 * 1024),
    })
    env.update({k: str(v) for k, v in extra.items()})
    p = subprocess.Popen([PY, os.path.join(BASE, "inkvoice.py")], cwd=BASE, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_port(port):
        p.terminate()
        raise SystemExit("隔离实例没起来（端口 %d）" % port)
    return p


def stop(p):
    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def raw(port, path, headers, body=b"", wait=10, read=True):
    """发一个请求；read=False 时返回 socket，留给调用方继续喂数据。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    head = "POST %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n" % path
    for k, v in headers.items():
        head += "%s: %s\r\n" % (k, v)
    s.sendall(head.encode() + b"\r\n")
    if body:
        s.sendall(body)
    if not read:
        return s
    data = b""
    try:
        while True:
            c = s.recv(4096)
            if not c:
                break
            data += c
            if len(data) > 65536:
                break
    except socket.timeout:
        pass
    s.close()
    return data


def status(data):
    try:
        return int(data.split(b" ")[1])
    except Exception:
        return -1


def header(data, name):
    m = re.search((name + r":\s*([^\r\n]+)").encode(), data, re.I)
    return m.group(1).decode().strip() if m else ""


def audio(n=2048):
    return b"\x00\x00\x00\x20ftypM4A " + b"\x00" * (n - 12)


def job_tmp_left(data_dir):
    inbox = os.path.join(data_dir, "inbox")
    return [f for f in os.listdir(inbox)] if os.path.isdir(inbox) else []


def main():
    global TOK
    import nethelp as NH
    TOK = NH.token()
    if not TOK:
        print("读不到访问令牌，先确认服务配置")
        return 2
    H = {"Authorization": "Bearer " + TOK, "Content-Type": "audio/mp4"}

    # ── 实例 A：常规请求级保护 ──
    data = tempfile.mkdtemp(prefix="vk_up_")
    port = 8791
    p = start(port, data)
    try:
        # 1. clip_id 格式不合法 → 400（不再静默换 ID）
        r = raw(port, "/upload?mode=text2",
                dict(H, **{"X-Clip-Id": "bad id!!", "Content-Length": "100"}), b"x" * 100)
        check(status(r) == 400, "非法 clip_id → 400", "状态 %s" % status(r))

        # 2. 没带 clip_id → 服务端生成 UUID 并回传
        body = audio()
        r = raw(port, "/upload?mode=text2",
                dict(H, **{"Content-Length": str(len(body))}), body * 1)
        cid = header(r, "X-Clip-Id")
        uuid_ok = bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                                cid))
        check(status(r) in (200, 202) and uuid_ok,
              "缺 clip_id → 生成 UUID 并回传", "状态 %s id=%s…" % (status(r), cid[:8]))

        # 3. chunked → 411
        r = raw(port, "/upload?mode=text2",
                dict(H, **{"Transfer-Encoding": "chunked"}), b"0\r\n\r\n")
        check(status(r) == 411, "chunked → 411", "状态 %s" % status(r))

        # 4. 没有 Content-Length → 411
        r = raw(port, "/upload?mode=text2", H)
        check(status(r) == 411, "缺 Content-Length → 411", "状态 %s" % status(r))

        # 5. 超过单次上限 → 413（在读 body 之前）
        r = raw(port, "/upload?mode=text2",
                dict(H, **{"Content-Length": "100000"}), b"")
        check(status(r) == 413, "超上限 → 413", "状态 %s" % status(r))

        # 6. 慢速上传：只给一半就不动了 → 408，且不留 .tmp
        s = raw(port, "/upload?mode=text2",
                dict(H, **{"Content-Length": "4000", "X-Clip-Id": str(uuid.uuid4())}),
                b"\x00" * 100, read=False)
        time.sleep(3.2)
        try:
            got = s.recv(4096)
        except socket.timeout:
            got = b""
        s.close()
        left = job_tmp_left(data)
        check(status(got) == 408 and not [x for x in left if x.endswith(".tmp")],
              "慢速上传 → 408 且无残留", "状态 %s inbox=%s" % (status(got), left))

        # 7. 单 IP 分钟字节预算（200KB/分钟）
        codes = []
        for _ in range(4):
            b = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * (60 * 1024 - 12)
            r = raw(port, "/upload?mode=text2",
                    dict(H, **{"Content-Length": str(len(b)),
                               "X-Clip-Id": str(uuid.uuid4())}), b, wait=20)
            codes.append(status(r))
        check(429 in codes, "单 IP 字节预算 → 429", str(codes))
    finally:
        stop(p)

    # ── 实例 B：队列满必须在读 body 之前拒绝 ──
    data2 = tempfile.mkdtemp(prefix="vk_up2_")
    port2 = 8792
    p2 = start(port2, data2, VK_MAX_QUEUE=1, VK_MAX_CONCURRENT=4,
               VK_IP_BYTES_PER_MIN=str(50 * 1024 * 1024))
    try:
        s1 = raw(port2, "/upload?mode=paste",
                 dict(H, **{"Content-Length": "8000", "X-Clip-Id": str(uuid.uuid4())}),
                 b"\x00" * 100, read=False)      # 占住一个在途上传
        time.sleep(0.4)
        t0 = time.time()
        r = raw(port2, "/upload?mode=paste",
                dict(H, **{"Content-Length": "8000", "X-Clip-Id": str(uuid.uuid4())}),
                b"", wait=6)
        dt = time.time() - t0
        s1.close()
        check(status(r) == 503 and dt < 1.5,
              "队列满 → 读 body 前就 503", "状态 %s 用时 %.2fs" % (status(r), dt))
    finally:
        stop(p2)

    # ── 实例 C：并发上限 ──
    data3 = tempfile.mkdtemp(prefix="vk_up3_")
    port3 = 8793
    p3 = start(port3, data3, VK_MAX_QUEUE=10, VK_MAX_CONCURRENT=1,
               VK_IP_BYTES_PER_MIN=str(50 * 1024 * 1024))
    try:
        s1 = raw(port3, "/upload?mode=paste",
                 dict(H, **{"Content-Length": "8000", "X-Clip-Id": str(uuid.uuid4())}),
                 b"\x00" * 100, read=False)
        time.sleep(0.4)
        t0 = time.time()
        r = raw(port3, "/upload?mode=paste",
                dict(H, **{"Content-Length": "8000", "X-Clip-Id": str(uuid.uuid4())}),
                b"", wait=6)
        dt = time.time() - t0
        s1.close()
        check(status(r) == 503 and dt < 1.5,
              "并发上限 → 503 busy", "状态 %s 用时 %.2fs" % (status(r), dt))
    finally:
        stop(p3)

    good = sum(1 for r in RESULTS if r)
    print("\n结果：%d / %d 通过" % (good, len(RESULTS)))
    return 0 if good == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
