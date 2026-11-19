# -*- coding: utf-8 -*-
"""触发一次「异步转写 + 自动输入」（mode=paste），用于真实窗口验证。"""
import io
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import nethelp as NH  # noqa: E402


def main():
    data = io.open(os.path.join(BASE, "test_zh.m4a"), "rb").read()
    cid = time.strftime("c_%Y%m%d_%H%M%S_") + ("%06x" % (int(time.time()*1000) % 0xFFFFFF))
    code, body = NH.req("/upload?mode=paste", "POST", data,
                        dict(NH.authed(), **{"Content-Type": "audio/mp4",
                                             "X-Clip-Id": cid}), timeout=30)
    print("提交：", code, body[:90].decode("utf-8", "replace"))
    import json as _j
    try:
        cid = _j.loads(body.decode("utf-8")).get("clip_id", cid)
    except Exception:
        pass
    for _ in range(30):
        time.sleep(1)
        st = NH.req("/jobs/" + cid, headers=NH.authed())[1]
        if not st:
            continue
        text = st.decode("utf-8", "replace")
        if '"state": "done"' in text.replace(" ", " "):
            print("任务：", text[:160])
            return 0
    print("超时没等到结果")
    return 1


if __name__ == "__main__":
    sys.exit(main())
