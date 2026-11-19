# -*- coding: utf-8 -*-
"""崩溃恢复自检：把「任务记录 × 音频」的各种组合造出来，跑对账，看处理对不对。

只动临时目录，不碰真实 jobs / inbox。

跑法：python selftest_recovery.py
"""
import io
import json
import os
import queue
import shutil
import sys
import tempfile
import time
import uuid

TMP = tempfile.mkdtemp(prefix="vk_rec_")
os.environ["VK_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inkvoice as V  # noqa: E402

RESULTS = []


def check(ok, name, detail=""):
    RESULTS.append(bool(ok))
    print(("  [OK]   " if ok else "  [FAIL] ") + name + ("  ← " + detail if detail else ""))


def reset():
    for d in (V.INBOX, V.JOB_DIR, V.QUARANTINE):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    while True:
        try:
            V.jobs.get_nowait()
        except queue.Empty:
            break


def audio(name="a.m4a", size=4096):
    p = os.path.join(V.INBOX, name)
    with open(p, "wb") as f:
        f.write(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * (size - 12))
    return p


def job(cid, **kw):
    V.job_write(cid, **kw)


def read(cid):
    return V.job_read(cid) or {}


def queued():
    out = []
    while True:
        try:
            out.append(V.jobs.get_nowait())
        except queue.Empty:
            return out


def main():
    cid = lambda: str(uuid.uuid4())  # noqa: E731

    # 1. 有记录、有音频、状态未完成 → 重新转写，且不许自动输入
    reset()
    c = cid()
    audio(c + ".m4a")
    job(c, state="queued", file=c + ".m4a", t=time.time())
    V.reconcile_jobs()
    st = read(c)
    q = queued()
    check(st.get("state") == "queued" and st.get("no_paste") is True and len(q) == 1,
          "未完成 + 音频在 → 重新转写（禁自动输入）",
          "state=%s no_paste=%s 队列=%d" % (st.get("state"), st.get("no_paste"), len(q)))

    # 2. 有记录、音频丢了
    reset()
    c = cid()
    job(c, state="stored", file=c + ".m4a", t=time.time())
    V.reconcile_jobs()
    st = read(c)
    check(st.get("state") == "needs_review" and st.get("reason") == "crash_before_transcribe",
          "有记录 + 音频丢 → 待核查", str(st.get("reason")))

    # 3. 没记录、有音频 → 孤儿认领
    reset()
    c = cid()
    audio(c + ".m4a")
    V.reconcile_jobs()
    st = read(c)
    q = queued()
    check(st.get("state") == "queued" and st.get("sha256") and len(q) == 1,
          "没记录 + 音频在 → 认领并重转", "sha=%s 队列=%d" % (bool(st.get("sha256")), len(q)))

    # 4. 没记录、音频是垃圾 → 隔离，不删
    reset()
    p = os.path.join(V.INBOX, "junk_%s.m4a" % uuid.uuid4().hex[:6])
    with open(p, "wb") as f:
        f.write(b"not audio at all" * 10)
    V.reconcile_jobs()
    moved = os.listdir(V.QUARANTINE)
    check(not os.path.exists(p) and any(x.endswith(".m4a") or ".m4a." in x for x in moved),
          "垃圾音频 → 隔离留档", "quarantine=%d" % len(moved))

    # 5. 转写完成但没进输入 → 待核查，绝不重打
    reset()
    c = cid()
    job(c, state="transcribed", raw="原文", text="整理后", t=time.time())
    V.reconcile_jobs()
    st = read(c)
    check(st.get("paste_state") == "NEEDS_REVIEW" and st.get("reason") == "crash_after_transcribe",
          "转写完就崩 → 待核查不重打", "%s/%s" % (st.get("state"), st.get("reason")))

    # 6. 正在输入时崩 → 待核查
    reset()
    c = cid()
    job(c, state="pasting", paste_state="PASTING", chars=5, t=time.time())
    V.reconcile_jobs()
    st = read(c)
    check(st.get("paste_state") == "NEEDS_REVIEW" and st.get("reason") == "crash_during_paste",
          "PASTING 中断 → 待核查", str(st.get("reason")))

    # 7. 任务记录损坏 + 音频在 → 记录留档、音频认领
    reset()
    c = cid()
    audio(c + ".m4a")
    with open(os.path.join(V.JOB_DIR, c + ".json"), "w", encoding="utf-8") as f:
        f.write("{broken json")
    V.reconcile_jobs()
    st = read(c)
    q = queued()
    quarantined = [x for x in os.listdir(V.QUARANTINE) if x.endswith(".corrupt")]
    check(st.get("state") == "queued" and quarantined and len(q) == 1,
          "记录损坏 → 留档 + 音频认领",
          "corrupt=%d 队列=%d" % (len(quarantined), len(q)))

    # 8. 中间文件 / 临时文件 → 直接清
    reset()
    audio("x.m4a")
    for n in ("x.m4a.16k.wav", "x.m4a.trim.wav", "x.m4a.part0.wav", "y.m4a.tmp"):
        with open(os.path.join(V.INBOX, n), "wb") as f:
            f.write(b"\x00" * 64)
    V.reconcile_jobs()
    left = sorted(os.listdir(V.INBOX))
    check(left == ["x.m4a"], "中间/临时文件被清掉", str(left))

    # 9. 已完成 + 音频没删 → 补删音频
    reset()
    c = cid()
    p = audio(c + ".m4a")
    job(c, state="done", file=c + ".m4a", text="已经转好了", t=time.time())
    V.reconcile_jobs()
    st = read(c)
    check(not os.path.exists(p) and st.get("state") == "done",
          "已完成 + 音频残留 → 清音频", "存在=%s" % os.path.exists(p))

    good = sum(1 for r in RESULTS if r)
    print("\n结果：%d / %d 通过" % (good, len(RESULTS)))
    return 0 if good == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
