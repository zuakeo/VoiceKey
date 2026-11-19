# -*- coding: utf-8 -*-
"""端到端：真实音频 → 上传 → 队列 → 转写 → 自动输入 → 读回比对。

这是评审要的那条「正向链路」：不是假注入，也不是只验拒绝，
而是让生产代码真的把字打进一个窗口，再把窗口里的内容读出来比。

注意：判定（UIA）放在**子进程**里做（调用 guard_probe.py）。
本进程既要跑窗口消息循环、又要建 COM 对象，实测会在消息泵里段错误；
生产服务里 UIA 跑在独立的 worker 线程且没有窗口消息循环，不存在这个问题。

跑法：python selftest_e2e.py
用的音频是 test_zh.m4a（8 秒中文），会消耗一次云端转写额度。
"""
import ctypes
import io
import json
import os
import subprocess
import sys
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import nethelp as NH            # noqa: E402
import selftest_guard as SG     # noqa: E402

u32 = SG.u32
k32 = SG.k32


def guard():
    """子进程做判定；等待期间必须继续泵消息，否则它查我们的窗口会卡死。"""
    p = subprocess.Popen([sys.executable, os.path.join(BASE, "guard_probe.py")],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < 20:
        SG.pump(0.1)
    if p.poll() is None:
        p.kill()
        return {}
    try:
        return json.loads(p.stdout.read() or "{}")
    except ValueError:
        return {}


def main():
    inst = k32.GetModuleHandleW(None)
    cls = "VKE2E"
    wc = SG.WNDCLASSW()
    wc.lpfnWndProc = SG.WNDPROC(SG.wndproc)
    wc.hInstance = inst
    wc.lpszClassName = cls
    u32.RegisterClassW(ctypes.byref(wc))

    W = 620
    form = u32.CreateWindowExW(0, cls, "voicekey e2e target", SG.WS_OVERLAPPEDWINDOW,
                               140, 140, W, 300, None, None, inst, None)
    edit = u32.CreateWindowExW(0, "Edit", "",
                               SG.WS_CHILD | SG.WS_VISIBLE | SG.WS_BORDER | SG.ES_MULTILINE,
                               16, 16, W - 48, 200, form, None, inst, None)
    u32.ShowWindow(form, SG.SW_SHOW)
    if not SG.force_foreground(form):
        print("  [SKIP] 前台被 %s 占着，抢不到焦点" % (guard().get("exe") or "?"))
        return 0
    u32.SetFocus(edit)
    SG.pump(0.3)

    g = guard()
    print("  判定：allow=%s code=%s (%s)" % (g.get("allow"), g.get("code"), g.get("detail")))
    if not g.get("allow"):
        print("  [FAIL] 自建编辑框被判拒绝，后面的链路无法验证")
        return 1

    cid = str(uuid.uuid4())
    data = io.open(os.path.join(BASE, "test_zh.m4a"), "rb").read()
    import threading
    box = {}

    def submit():
        code, body = NH.req("/upload?mode=paste", "POST", data,
                            dict(NH.authed(), **{"Content-Type": "audio/mp4",
                                                 "X-Clip-Id": cid}), timeout=30)
        box["code"] = code
        box["body"] = body[:50].decode("utf-8", "replace")
        for _ in range(40):
            st = json.loads(NH.req("/jobs/" + cid, headers=NH.authed())[1] or b"{}")
            box["state"] = st
            if st.get("paste_state") in ("PASTED", "NEEDS_REVIEW"):
                return
            time.sleep(0.5)

    th = threading.Thread(target=submit, daemon=True)
    th.start()
    t0 = time.time()
    while th.is_alive() and time.time() - t0 < 45:
        SG.pump(0.1)
    print("  上传：%s %s" % (box.get("code"), box.get("body", "")))
    if box.get("code") not in (200, 202):
        return 1
    st = box.get("state") or {}

    got = SG.read_text(edit).strip()
    want = (st.get("text") or "").strip()
    print("  任务：state=%s paste_state=%s reason=%s chars=%s"
          % (st.get("state"), st.get("paste_state"), st.get("reason"), st.get("chars")))
    print("  窗口 %d 字 / 任务记录 %d 字 / 一致=%s" % (len(got), len(want), got == want))
    if got:
        print("  内容：" + got[:60])

    ok = st.get("paste_state") == "PASTED" and got == want and bool(want)
    print(("  [OK]   " if ok else "  [FAIL] ") + "端到端链路")
    u32.DestroyWindow(form)
    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
