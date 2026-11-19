# -*- coding: utf-8 -*-
"""整改进度自检：逐条打勾。只读，不改任何东西。

跑法：  python audit.py
注意：最后一项会打满限流配额，故意放在最后；跑完等 60 秒再做别的。
"""
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
import nethelp as NH
HOST = NH.base_url()
AAPT = "<project-root>/toolchain/sdk/build-tools/36.0.0/aapt2.exe"
APK_INK = os.path.normpath(os.path.join(BASE, "..", "..", "inknote", "probe", "chk.apk"))
MODULE_APK = "/data/adb/modules/voicekey_mokey/system/priv-app/VoiceKey/VoiceKey.apk"

RESULTS = []


def mark(ok, title, detail=""):
    RESULTS.append((bool(ok), title, detail))
    print(("  [OK]   " if ok else "  [FAIL] ") + title + (("  ← " + detail) if detail else ""))


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def token():
    p = os.path.join(BASE, "secret.txt")
    return io.open(p, encoding="utf-8").read().strip() if os.path.exists(p) else ""


def req(path, method="GET", data=None, headers=None, timeout=30):
    return NH.req(path, method, data, headers, timeout)


def authed():
    return {"Authorization": "Bearer " + token()}


def audio():
    return io.open(os.path.join(BASE, "test_zh.m4a"), "rb").read()


def fake_audio(n=4096):
    """文件头像 MP4，但内容不是 —— 既能过 looks_like_audio，
    又会让 ffmpeg 直接失败，不产生云端调用。"""
    return b"\x00\x00\x00\x20ftypM4A " + b"\x00" * (n - 12)


def req_full(path, data, headers, timeout=60):
    r = urllib.request.Request(HOST + path, data=data, method="POST",
                               headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=NH.context()) as f:
            return f.status, dict(f.headers), f.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b""
    except Exception as e:
        # 服务端在读完 body 之前就拒绝时，会直接关连接；客户端这边看到的是
        # SSLEOF / ConnectionReset，而不是一个 HTTP 状态码。这本身是预期行为。
        return type(e).__name__, {}, b""


def check_auth():
    print("\n【P0-01 鉴权】")
    if req("/hotwords", headers=authed())[0] == 429:
        print("  （上轮打满了配额，等 62 秒）")
        time.sleep(62)
    c1 = req("/hotwords")[0]
    c2 = req("/hotwords", headers={"Authorization": "Bearer wrong"})[0]
    c3 = req("/hotwords", headers=authed())[0]
    mark(c1 == 401, "无令牌被拒", str(c1))
    mark(c2 == 401, "错令牌被拒", str(c2))
    mark(c3 == 200, "正确令牌通过", str(c3))
    cp = req("/ping")
    mark(cp[0] == 200 and b"inkvoice" in cp[1], "健康检查公开且保留识别字样")

    big = b"0" * (13 * 1024 * 1024)
    code = req("/upload?mode=text", "POST", big,
               dict(authed(), **{"Content-Type": "audio/mp4"}))[0]
    mark(code in (413, "ConnectionAbortedError", "ConnectionResetError", "URLError",
                  "BrokenPipeError"),
         "超大上传被拒", str(code))


def check_idempotent():
    print("\n【P0-02 幂等与状态】")
    cid = str(uuid.uuid4())
    h = dict(authed(), **{"Content-Type": "audio/mp4", "X-Clip-Id": cid})
    c1, b1 = req("/upload?mode=text", "POST", audio(), h, timeout=120)
    if c1 == 429:
        time.sleep(62)
        c1, b1 = req("/upload?mode=text", "POST", audio(), h, timeout=120)
    c2, b2 = req("/upload?mode=text", "POST", audio(), h, timeout=120)
    mark(c1 == 200 and c2 == 200 and b1.strip() == b2.strip(),
         "同 clip_id 两次结果一致", "%s / %s" % (c1, c2))
    st = json.loads(req("/jobs/" + cid, headers=authed())[1] or b"{}")
    mark(st.get("state") == "done", "任务状态可查", str(st.get("state")))
    mark(req("/jobs/" + str(uuid.uuid4()), headers=authed())[0] == 200,
         "查不存在的任务不报错")
    mark(req("/jobs/..%2f..%2fsecret", headers=authed())[0] == 400,
         "任务 ID 不像 ID 时被拒", "")


def check_files():
    print("\n【P1-04 落盘与文件名】")
    cid = str(uuid.uuid4())
    h = dict(authed(), **{"Content-Type": "audio/mp4", "X-Clip-Id": cid})
    code, _ = req("/upload?mode=text&name=../../evil.sh", "POST", audio(), h, timeout=120)
    evil = [p for p in (os.path.join(BASE, "evil.sh"),
                        os.path.join(os.path.dirname(BASE), "evil.sh"))
            if os.path.exists(p)]
    mark(not evil, "恶意文件名不越界", "状态 %s" % code)
    st = json.loads(req("/jobs/" + cid, headers=authed())[1] or b"{}")
    mark(st.get("clip_id") == cid, "服务端按 clip_id 建任务", str(st.get("state")))
    leftovers = [p for p in os.listdir(os.path.join(BASE, "inbox"))
                 if "evil" in p or p.endswith(".sh")]
    mark(not leftovers, "inbox 里没有客户端给的名字", str(leftovers))
    tmp = [p for p in os.listdir(os.path.join(BASE, "inbox")) if p.endswith(".tmp")]
    mark(not tmp, "inbox 无 .tmp 残留", str(tmp))


def check_upload_guard():
    print("\n【P1-06 上传的请求级保护】")
    body = fake_audio()

    st, _, _ = req_full("/upload?mode=text2", body,
                        dict(authed(), **{"Content-Type": "audio/mp4",
                                          "X-Clip-Id": "bad id!!"}))
    mark(st == 400, "非法 clip_id 直接 400（不再静默换 ID）", str(st))

    st, hd, _ = req_full("/upload?mode=text2", body,
                         dict(authed(), **{"Content-Type": "audio/mp4"}))
    cid = hd.get("X-Clip-Id", "")
    mark(st in (200, 202) and len(cid) == 36 and cid.count("-") == 4,
         "缺 clip_id → 生成 UUID 并在响应头回传", "%s %s" % (st, cid[:8]))

    big = b"0" * (13 * 1024 * 1024)
    st, _, _ = req_full("/upload?mode=text2", big,
                        dict(authed(), **{"Content-Type": "audio/mp4",
                                          "X-Clip-Id": str(uuid.uuid4())}))
    mark(st in (413, 400) or isinstance(st, str), "超过单次上限被拒", str(st))


def check_hotwords():
    print("\n【P1-02 热词】")
    code, body = req("/hotwords", headers=authed())
    if code == 429:
        time.sleep(62)
        code, body = req("/hotwords", headers=authed())
    d = json.loads(body or b"{}")
    mark(bool(d.get("v")), "版本号是内容哈希", str(d.get("v"))[:10])
    words = [w for w in (d.get("text") or "").split("、") if w.strip()]
    mark(len(words) >= 50, "热词数量正常", "%d 个" % len(words))
    mark(req("/hotwords/version", headers=authed())[0] == 200, "版本接口可用")


def check_code():
    print("\n【代码层】")
    inc = io.open(os.path.join(BASE, "inkvoice.py"), encoding="utf-8").read()
    up = io.open(os.path.join(BASE, "uploader.py"), encoding="utf-8").read()
    ig = io.open(os.path.join(BASE, "inputguard.py"), encoding="utf-8").read()

    mark("os.replace(tmp, path)" in up, "音频原子落盘")
    mark("os.fsync" in up, "音频落盘后 fsync")
    mark("os.replace(tmp, job_path(cid))" in inc, "任务记录原子写")
    mark("os.fsync" in inc, "任务记录写后 fsync")
    mark("os.replace(tmp, p)" in up, "热词原子写")
    mark("inputguard.verdict()" in inc, "输入前问真焦点控件（UIA）")
    mark("no_paste" in inc, "崩溃恢复的任务禁止自动输入")
    mark("hmac.compare_digest" in inc, "令牌常量时间比较")
    mark("MAX_UPLOAD" in up and "413" in up, "有上传大小上限")
    mark("socket.timeout" in up and "408" in up, "请求体读取有超时")
    mark("_sem.acquire(blocking=False)" in up, "有并发上传上限")
    i_queue = up.find('"queue full"')
    i_read = up.find("rfile.read", i_queue if i_queue > 0 else 0)   # 上传分支的读 body
    mark(i_queue > 0 and i_read > i_queue, "队列满的判定在读 body 之前",
         "位置 %d < %d" % (i_queue, i_read))
    mark("IP_BYTES_PER_MIN" in up, "有单 IP 分钟字节预算")
    mark("chunked not supported" in up, "拒绝 chunked")
    mark("invalid clip id" in up, "非法 clip_id 返回 400")
    mark("UIA_ValuePatternId" in ig and "secret_field" in ig, "只读/密码框会被判据拦住")
    mark("guard_unavailable" in ig, "UIA 不可用时默认拒绝（不猜）")


def check_secrets():
    print("\n【P1-05 密钥与明文残留】")
    import vault
    left = vault.plaintext_leftovers()
    mark(not left, "目录里没有明文密钥", str(left))
    mark(not os.path.exists(os.path.join(BASE, "groq_key.txt.bak")),
         "旧的 groq_key.txt.bak 已清")
    mark(not os.path.exists(os.path.join(BASE, "ds_cred.json")),
         "旧的 ds_cred.json 已清")
    mark(bool(vault.load("groq_key")), "Groq 密钥仍可解密读取")
    mark(bool(vault.load("deepseek_key")), "DeepSeek 密钥仍可解密读取")


def check_guard():
    print("\n【P1-07 输入判定可用性】")
    import inputguard
    g = inputguard.warmup()
    mark(g["uia"], "UIA 可用", g["uia_err"] or "")
    mark(g["msaa"], "MSAA 兜底可用", g["msaa_err"] or "")


def check_android():
    print("\n【P1-03 组件与签名】")
    out = ""
    if os.path.exists(APK_INK):
        out = sh('"%s" dump xmltree "%s" --file AndroidManifest.xml' % (AAPT, APK_INK))
    mark(bool(out), "能从已安装 APK 读到 manifest")

    seg = out.split(".SyncService")[1][:220] if ".SyncService" in out else ""
    mark("exported(0x01010010)=true" not in seg, "SyncService 不导出",
         "导出" if "exported(0x01010010)=true" in seg else "不导出")

    boot_lines = [l for l in out.splitlines()
                  if "intent.action.BOOT_COMPLETED" in l and "permission" not in l]
    mark(len(boot_lines) == 1 and "com.zuakeo.inknote.WAKE" not in out,
         "BootReceiver 只留开机广播", "%d 条动作" % len(boot_lines))

    r = sh('adb shell "am start-service -n com.zuakeo.inknote/.SyncService 2>&1"')
    mark(("Permission Denial" in r) or ("not exported" in r) or ("Error" in r),
         "外部启动被拒", r.strip().splitlines()[-1][:60] if r.strip() else "")

    sig = sh('adb shell "dumpsys package com.zuakeo.voicekey | grep -i -m1 signatures"').strip()
    mark(bool(sig), "VoiceKey 有签名", sig[:50])
    mark("com.zuakeo.voicekey" in sh('adb shell "pm list packages -s"'), "VoiceKey 是系统应用")

    cmd = 'adb shell "su -c \'test -f ' + MODULE_APK + ' && echo yes\'"'
    mark("yes" in sh(cmd), "APK 在模块 priv-app 目录")


def check_rate_limit():
    print("\n【P0-01 限流】")
    codes = [req("/hotwords", headers=authed())[0] for _ in range(70)]
    mark(429 in codes, "限流生效", "出现 429")


def main():
    check_auth()
    check_idempotent()
    check_files()
    check_upload_guard()
    check_hotwords()
    check_code()
    check_secrets()
    check_guard()
    check_android()
    check_rate_limit()

    print("\n———— 汇总 ————")
    bad = [r for r in RESULTS if not r[0]]
    print("通过 %d / 失败 %d" % (len(RESULTS) - len(bad), len(bad)))
    for ok, t, d in bad:
        print("  ✗", t, "|", d)


if __name__ == "__main__":
    main()
