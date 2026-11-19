# -*- coding: utf-8 -*-
"""上传接口：POST /upload、POST /hotwords。

第三轮评审的两条要求落在这里：

1. **请求级保护**（不只是「文件大小上限」）
   - 请求体读取有超时，慢速上传拖不住工作线程
   - 同时接收的上传数有上限
   - 队列满 / 磁盘紧 → **在读请求体之前**就拒绝，不白收 12MB
   - 实际读取字节数有硬上限，不信任 Content-Length
   - 单个 IP 每分钟总字节有预算（12MB × 60 次 = 720MB 这种量级要挡住）
   - 明确拒绝 chunked（BaseHTTPRequestHandler 本来也不解析，早拒早清楚）

2. **clip_id 不再静默替换**
   格式不合法 → 400。缺 ID → 服务端生成 UUID，并且**把 ID 回给手机**
   （响应头 `X-Clip-Id` + 响应体 `clip_id`）。手机必须存下来、重试沿用，
   否则「第一次上传超时、重试换新 ID」就会变成两条任务、两次转写、两次扣额度。
"""
import hashlib
import json
import os
import shutil
import socket
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

READ_TIMEOUT = float(os.environ.get("VK_READ_TIMEOUT", "30"))       # 秒
MAX_CONCURRENT = int(os.environ.get("VK_MAX_CONCURRENT", "4"))
IP_BYTES_PER_MIN = int(os.environ.get("VK_IP_BYTES_PER_MIN", str(60 * 1024 * 1024)))
MAX_INBOX_MB = int(os.environ.get("VK_MAX_INBOX_MB", "500"))

_sem = threading.BoundedSemaphore(MAX_CONCURRENT)
_inflight = 0
_budget = {}                 # ip -> [[时间戳, 字节], ...]
_lock = threading.Lock()


def _take_ip(ip, n):
    """按「最近 60 秒累计字节」限流。超了就返回 False。"""
    now = time.time()
    with _lock:
        arr = [x for x in _budget.get(ip, []) if now - x[0] < 60]
        used = sum(x[1] for x in arr)
        if used + n > IP_BYTES_PER_MIN:
            _budget[ip] = arr
            return False
        arr.append([now, n])
        _budget[ip] = arr
        return True


def _inbox_bytes(path):
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_file():
                        total += e.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def send(h, code, body, ctype="application/json", extra=None, close=False):
    if isinstance(body, str):
        body = body.encode("utf-8")
    try:
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            h.send_header(k, str(v))
        h.end_headers()
        h.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        h.close_connection = True
    if close:
        h.close_connection = True


def handle_post(h):
    import inkvoice as V

    global _inflight
    u = urlparse(h.path)
    qs = parse_qs(u.query)
    ip = h.client_address[0] if h.client_address else "?"

    # ── 传输层：先看请求形状，再看内容 ──
    if h.headers.get("Transfer-Encoding"):
        send(h, 411, "chunked not supported", "text/plain", close=True)
        V.log("拒绝 chunked 上传（%s）" % ip)
        return
    raw_len = h.headers.get("Content-Length")
    if raw_len is None:
        send(h, 411, "length required", "text/plain", close=True)
        return
    try:
        n = int(raw_len)
    except ValueError:
        send(h, 400, "bad length", "text/plain", close=True)
        return
    if n <= 0:
        send(h, 400, "bad length", "text/plain")
        return
    if n > V.MAX_UPLOAD:
        send(h, 413, "too large", "text/plain", close=True)
        return

    # ── clip_id：不合法直接拒，缺了才生成 ──
    clip = (h.headers.get("X-Clip-Id") or "").strip()
    if clip and not V.clip_ok(clip):
        send(h, 400, "invalid clip id", "text/plain", close=True)
        V.log("拒绝格式不合法的 clip_id（长度 %d，来自 %s）" % (len(clip), ip))
        return
    generated = not clip
    if generated:
        clip = str(uuid.uuid4())      # UUID：同一秒内多次录音也不会撞
        V.log("请求没带 clip_id，已生成 %s（手机应保存并在重试时沿用）" % clip)
    extra = {"X-Clip-Id": clip}

    if u.path == "/hotwords":
        if not _take_ip(ip, n):
            send(h, 429, "byte budget exceeded", "text/plain",
                 {"Retry-After": "60"}, close=True)
            return
        raw = h.rfile.read(n).decode("utf-8", "replace")
        words = [w.strip() for w in raw.replace("、", "\n").splitlines() if w.strip()]
        p = os.path.join(V.BASE, "hotwords_from_device.txt")
        tmp = p + ".tmp"          # 先写临时文件再原子改名，另一端不会读到半截词表
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("# 热词（手机和电脑双向同步，谁后改以谁为准）\n")
            f.write("\n".join(words) + "\n")
        os.replace(tmp, p)
        body = json.dumps({"mtime": os.path.getmtime(p),
                           "v": V.asr_cloud.hotwords_hash("、".join(words))})
        send(h, 200, body)
        V.log("热词已更新：%d 个" % len(words))
        return

    # ── 队列 / 磁盘：都在读 body 之前判断 ──
    if V.jobs.qsize() + _inflight >= V.MAX_QUEUE:
        send(h, 503, "queue full", "text/plain", {"Retry-After": "10"}, close=True)
        V.log("队列已满（%d），拒收 %s" % (V.jobs.qsize(), ip))
        return
    free = shutil.disk_usage(V.INBOX).free
    if free < 200 * 1024 * 1024:
        send(h, 507, "disk full", "text/plain", close=True)
        V.log("磁盘只剩 %dMB，拒收" % (free // (1024 * 1024)))
        return
    inbox_mb = _inbox_bytes(V.INBOX) / (1024 * 1024)
    if inbox_mb > MAX_INBOX_MB:
        send(h, 507, "inbox quota exceeded", "text/plain", close=True)
        V.log("收件箱已占 %.0fMB（上限 %dMB），拒收" % (inbox_mb, MAX_INBOX_MB))
        return
    if not _take_ip(ip, n):
        send(h, 429, "byte budget exceeded", "text/plain",
             {"Retry-After": "60"}, close=True)
        V.log("IP %s 一分钟内上传量超预算，拒收" % ip)
        return

    if not _sem.acquire(blocking=False):
        send(h, 503, "busy", "text/plain", {"Retry-After": "5"}, close=True)
        V.log("并发上传已满（%d），拒收 %s" % (MAX_CONCURRENT, ip))
        return

    tmp = None
    with _lock:            # 注意：_inflight 用同一把锁保护
        _inflight += 1
    try:
        ctype = (h.headers.get("Content-Type") or "").lower()
        ext = ".wav" if "wav" in ctype else ".m4a"
        path = os.path.join(V.INBOX, clip + ext)
        tmp = path + ".tmp"

        # ── 读请求体：超时 + 硬上限 ──
        digest = hashlib.sha256()
        got = 0
        hard_cap = min(n, V.MAX_UPLOAD)
        deadline = time.time() + READ_TIMEOUT * 3      # 总时长兜底，防「一点一点喂」
        try:
            h.connection.settimeout(READ_TIMEOUT)
            with open(tmp, "wb") as f:
                while got < hard_cap:
                    if time.time() > deadline:
                        raise socket.timeout("total deadline exceeded")
                    chunk = h.rfile.read(min(65536, hard_cap - got))
                    if not chunk:
                        break
                    got += len(chunk)
                    digest.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())      # 「音频已落盘」必须是真的落盘
        except (socket.timeout, TimeoutError):
            _cleanup(tmp)
            send(h, 408, "read timeout", "text/plain", extra, close=True)
            V.log("上传读取超时（%s，已收 %d/%d 字节），已清理" % (clip, got, n))
            return
        except Exception:
            _cleanup(tmp)
            V.log("上传中断，已清理临时文件（%s）" % clip)
            raise
        finally:
            try:
                h.connection.settimeout(None)
            except OSError:
                pass

        if got != n:
            _cleanup(tmp)
            send(h, 400, "incomplete", "text/plain", extra, close=True)
            V.log("收到不完整音频（%d/%d），已丢弃（%s）" % (got, n, clip))
            return

        sha = digest.hexdigest()
        prev = V.job_read(clip)
        if prev is not None:
            return _handle_duplicate(h, V, clip, prev, sha, tmp, qs, extra, generated)

        if not V.looks_like_audio(tmp):
            _cleanup(tmp)
            send(h, 400, "not audio", "text/plain", extra, close=True)
            V.log("不是有效音频文件，已丢弃（%s）" % clip)
            return

        os.replace(tmp, path)                 # 原子改名，worker 看不到半截文件
        tmp = None
        V.job_write(clip, state="stored", file=os.path.basename(path),
                    sha256=sha, t=time.time(), source_ip=ip,
                    id_generated=generated)
        V.log("收到 %s (%d 字节)" % (os.path.basename(path), n))
        _dispatch(h, V, clip, path, qs, extra)
    finally:
        with _lock:
            _inflight -= 1
        _sem.release()


def _cleanup(p):
    if not p:
        return
    try:
        os.remove(p)
    except OSError:
        pass


def _handle_duplicate(h, V, clip, prev, sha, tmp, qs, extra, generated):
    old = prev.get("sha256") or ""
    if old and old != sha:
        _cleanup(tmp)
        send(h, 409, "clip id reused with different audio", "text/plain",
             extra, close=True)
        V.log("同一 clip_id 收到不同录音，已拒绝（%s）" % clip)
        return
    _cleanup(tmp)
    mode = (qs.get("mode", [""])[0] or "")
    if prev.get("state") in ("done", "empty") and mode in ("text", "text2"):
        if mode == "text2":
            body = json.dumps({"clip_id": clip, "raw": prev.get("raw", ""),
                               "text": prev.get("text", "")}, ensure_ascii=False)
            ct = "application/json; charset=utf-8"
        else:
            body = prev.get("text") or ""
            ct = "text/plain; charset=utf-8"
        send(h, 200, body, ct, extra)
        V.log("重复投递 %s，直接回原结果" % clip)
        return
    body = json.dumps({"clip_id": clip, "state": prev.get("state"),
                       "paste_state": prev.get("paste_state", ""),
                       "id_generated": generated})
    send(h, 200, body, extra=extra)      # 同上：200 是为了兼容已装机的 APK
    V.log("重复投递 %s，状态 %s" % (clip, prev.get("state")))


def _dispatch(h, V, clip, path, qs, extra):
    mode = (qs.get("mode", [""])[0] or "")
    if mode in ("text", "text2"):
        # 便签 App 用：同步转写，只回文字，不碰剪贴板/不粘贴
        try:
            raw, text = V.transcribe_ex(path)
        except Exception as e:
            V.log("便签转写失败（%s）: %r" % (clip, e))
            raw, text = "", ""
        V.job_write(clip, state="done" if text else "empty",
                    raw=raw or "", text=text or "", t=time.time())
        if mode == "text2":
            body = json.dumps({"clip_id": clip, "raw": raw or "", "text": text or ""},
                              ensure_ascii=False)
            ct = "application/json; charset=utf-8"
        else:
            body = text or ""
            ct = "text/plain; charset=utf-8"
        send(h, 200, body, ct, extra)
        try:
            os.remove(path)
        except OSError:
            pass
        V.log("便签转写返回 %d 字（%s）" % (len(text or ""), clip))
        return

    V.job_write(clip, state="queued", t=time.time())
    V.jobs.put(path)
    # 这里必须回 200，不能回 202。
    # 已装机的 VoiceKey（KeyService.java: `return code == 200;`）把 202 当失败，
    # 会走「存进便签队列」的兜底分支：录音被挪到手机记事本，电脑这边收不到，
    # 表现为「按了侧键，电脑不出字」。语义上 202 更准确，但客户端先于服务端存在，
    # 只能先迁就它。等手机端改成接受 2xx 后，再考虑改回 202。
    send(h, 200, json.dumps({"clip_id": clip, "state": "queued"}), extra=extra)
