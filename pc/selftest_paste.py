# -*- coding: utf-8 -*-
"""单测：自动输入状态机（假判定 + 假注入，不真的往桌面打字）。

覆盖：
  1. 判据放行 + 逐字成功         → PASTED
  2. 打到一半换了窗口            → NEEDS_REVIEW / window_changed
  3. SendInput 失败              → NEEDS_REVIEW / sendinput_failed
  4. 判据拒绝（密码框）          → NEEDS_REVIEW / secret_field，**不碰剪贴板**
  5. 判据拒绝 + 显式开剪贴板兜底 → 剪贴板写入，仍标 NEEDS_REVIEW

跑法：python selftest_paste.py
数据写到临时目录，不污染真实 jobs/。
"""
import io
import json
import os
import sys
import tempfile
import uuid

os.environ["VK_DATA_DIR"] = tempfile.mkdtemp(prefix="vk_paste_")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inkvoice as V  # noqa: E402


def job(cid):
    p = os.path.join(V.JOB_DIR, cid + ".json")
    if not os.path.exists(p):
        return {}
    return json.load(io.open(p, encoding="utf-8"))


def canned(allow=True, code="text_edit", source="uia",
           detail="Edit", hwnd=424242, exe="notepad.exe"):
    return {"allow": allow, "code": code, "detail": detail,
            "source": source, "hwnd": hwnd, "exe": exe}


def run(name, verdict, sent, expect_state, expect_reason=None, clip_on_refuse=False):
    cid = str(uuid.uuid4())
    V.inputguard.verdict = lambda *a, **k: verdict
    V.send_text_checked = sent
    V.CLIP_ON_REFUSE = clip_on_refuse
    box = {"clip": False}
    V.set_clipboard = lambda t: box.__setitem__("clip", True) or True

    V.do_paste(cid, "这是一段测试文字")
    st = job(cid)
    ok = st.get("paste_state") == expect_state
    if expect_reason is not None:
        ok = ok and st.get("reason") == expect_reason
    if clip_on_refuse:
        ok = ok and box["clip"] is True
    else:
        ok = ok and box["clip"] is False       # 默认绝不碰剪贴板
    print(("  [OK]   " if ok else "  [FAIL] ") + "%-22s → %s / %s / chars=%s / 剪贴板=%s"
          % (name, st.get("paste_state"), st.get("reason"),
             st.get("chars"), box["clip"]))
    return ok


def main():
    print("【输入状态机】")
    results = [
        run("放行且全部成功", canned(),
            lambda t, h, c: (len(t), None), "PASTED"),
        run("中途换窗口", canned(),
            lambda t, h, c: (5, "window_changed"),
            "NEEDS_REVIEW", "window_changed"),
        run("注入失败只打一半", canned(),
            lambda t, h, c: (3, "sendinput_failed"),
            "NEEDS_REVIEW", "sendinput_failed"),
        run("密码框拒绝", canned(False, "secret_field", "uia", "Edit"),
            lambda t, h, c: (0, None), "NEEDS_REVIEW", "secret_field"),
        run("只读框拒绝", canned(False, "readonly", "uia", "Edit"),
            lambda t, h, c: (0, None), "NEEDS_REVIEW", "readonly"),
        run("拒绝时开剪贴板兜底", canned(False, "not_text_input", "uia", "Document"),
            lambda t, h, c: (0, None), "NEEDS_REVIEW", "not_text_input",
            clip_on_refuse=True),
    ]
    good = sum(1 for r in results if r)
    print("\n结果：%d / %d 通过" % (good, len(results)))
    return 0 if good == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
