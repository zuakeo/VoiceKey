# -*- coding: utf-8 -*-
"""自动输入防护的真实控件测试（正向 + 负向，一次跑完）。

做法：用 ctypes 自己建一个 Win32 窗口，里面放四种控件，
逐个 SetFocus，然后问 inputguard「现在这个能不能打」：

    普通编辑框   → 必须放行（text_edit）
    密码框       → 必须拒绝（secret_field）
    只读编辑框   → 必须拒绝（readonly）
    静态文字     → 必须拒绝（static_text / not_text_input）

最后再做一次**真正的正向输入**：把一句中文打进普通编辑框，用 WM_GETTEXT 读回来比对。
这一步是上一轮评审说的「只验了该拒绝时能拒绝，没验该输入时能输入」。

跑法：python selftest_guard.py
需要交互式桌面（服务现在就跑在你的登录会话里，正常都有）。
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inputguard as G  # noqa: E402
import inkvoice as V    # noqa: E402  用生产代码里的逐字注入，不另写一套
# 放在建窗口之前导入：实测「先建窗口/初始化 COM、再 import inkvoice」会段错误，
# 反过来（先 import，再建窗口）稳定。和 comtypes 的初始化顺序是同一类问题。

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_BORDER = 0x00800000
WS_TABSTOP = 0x00010000
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
ES_PASSWORD = 0x0020
ES_READONLY = 0x0800
SW_SHOW = 5
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


u32.DefWindowProcW.restype = LRESULT
u32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                               ctypes.c_size_t, ctypes.c_ssize_t]
u32.CreateWindowExW.restype = wintypes.HWND
u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                            ctypes.c_uint, ctypes.c_uint]
k32.GetModuleHandleW.restype = wintypes.HMODULE


def wndproc(hwnd, msg, wparam, lparam):
    return u32.DefWindowProcW(hwnd, msg, wparam, lparam)


def pump(seconds=0.15):
    end = time.time() + seconds
    msg = wintypes.MSG()
    while time.time() < end:
        while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def read_text(hwnd):
    n = u32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
    buf = ctypes.create_unicode_buffer(int(n) + 1)
    u32.SendMessageW(hwnd, WM_GETTEXT, int(n) + 1, ctypes.byref(buf))
    return buf.value


def force_foreground(hwnd):
    """尽量把窗口顶到前台。

    目标程序占着前台时（全屏游戏、锁屏），SetForegroundWindow 会被系统拒绝，
    这里用 AttachThreadInput 绕过一次；还是不行就返回 False，让调用方跳过，
    而不是报一堆假失败。
    """
    fg = u32.GetForegroundWindow()
    tid_fg = u32.GetWindowThreadProcessId(fg, None)
    tid_me = k32.GetCurrentThreadId()
    try:
        u32.AttachThreadInput(tid_me, tid_fg, True)
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        u32.SetFocus(hwnd)
    except Exception:
        pass
    finally:
        try:
            u32.AttachThreadInput(tid_me, tid_fg, False)
        except Exception:
            pass
    pump(0.3)
    return u32.GetForegroundWindow() == hwnd


def main():
    inst = k32.GetModuleHandleW(None)
    cls = "VKGuardSelftest"
    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC(wndproc)
    wc.hInstance = inst
    wc.lpszClassName = cls
    if not u32.RegisterClassW(ctypes.byref(wc)):
        err = k32.GetLastError()
        if err not in (1410,):          # 1410 = 类已注册
            print("注册窗口类失败：", err)
            return 2

    W = 520
    form = u32.CreateWindowExW(0, cls, "voicekey guard selftest", WS_OVERLAPPEDWINDOW,
                               120, 120, W, 320, None, None, inst, None)
    if not form:
        print("建窗口失败：", k32.GetLastError())
        return 2

    normal = u32.CreateWindowExW(0, "Edit", "",
                                 WS_CHILD | WS_VISIBLE | WS_BORDER | ES_MULTILINE,
                                 16, 16, W - 48, 70, form, None, inst, None)
    secret = u32.CreateWindowExW(0, "Edit", "",
                                 WS_CHILD | WS_VISIBLE | WS_BORDER | ES_PASSWORD,
                                 16, 100, W - 48, 28, form, None, inst, None)
    readonly = u32.CreateWindowExW(0, "Edit", "只读内容",
                                   WS_CHILD | WS_VISIBLE | WS_BORDER | ES_READONLY,
                                   16, 140, W - 48, 28, form, None, inst, None)
    label = u32.CreateWindowExW(0, "Static", "静态文字，不是输入框",
                                WS_CHILD | WS_VISIBLE, 16, 180, W - 48, 24,
                                form, None, inst, None)

    u32.ShowWindow(form, SW_SHOW)
    u32.SetForegroundWindow(form)
    pump(0.4)

    if not force_foreground(form):
        fg = u32.GetForegroundWindow()
        who = G.exe_of_pid(G.pid_of(fg)) or "?"
        print("  [SKIP] 前台被 %s 占着，测试窗口抢不到焦点。" % who)
        print("         这不是代码问题：判定逻辑在 guard_probe.py 里可以单独看。")
        print("         把那个程序最小化（或退出全屏）后重跑本脚本即可。")
        u32.DestroyWindow(form)
        sys.stdout.flush()
        os._exit(0)

    cases = [
        ("普通编辑框", normal, True, ("text_edit", "text_document")),
        ("密码框", secret, False, ("secret_field",)),
        ("只读编辑框", readonly, False, ("readonly",)),
        ("静态文字", label, False, ("static_text", "not_text_input")),
    ]

    results = []
    for name, hwnd, want_allow, want_codes in cases:
        u32.SetFocus(hwnd)
        pump(0.2)
        v = G.verdict()
        ok = (v["allow"] is want_allow) and (v["code"] in want_codes)
        results.append(ok)
        print(("  [OK]   " if ok else "  [FAIL] ") + "%s → allow=%s code=%s (%s)"
              % (name, v["allow"], v["code"], v["detail"]))

    # ---- 真正的正向输入：打字 + 读回 ----
    text = "语音键输入自检：中文 123 ABC"
    force_foreground(form)          # 中间可能被别的程序抢回前台，这里再抢一次
    u32.SetFocus(normal)
    pump(0.2)
    v = G.verdict()
    typed = ""
    if not v["allow"]:
        print("  [FAIL] 正向输入前被判拒绝：", v["code"], v["detail"])
        results.append(False)
    else:
        print("  ..开始逐字注入", flush=True)
        sent, why = V.send_text_checked(text, u32.GetForegroundWindow(), "selftest")
        print("  ..注入返回 sent=%s why=%s" % (sent, why), flush=True)
        pump(0.2)
        typed = read_text(normal)
        ok = (why is None) and (typed.strip() == text)
        results.append(ok)
        print(("  [OK]   " if ok else "  [FAIL] ") +
              "正向输入 %d 字，读回 %d 字，一致=%s"
              % (sent, len(typed.strip()), typed.strip() == text))

    u32.DestroyWindow(form)
    pump(0.1)

    good = sum(1 for r in results if r)
    print("\n结果：%d / %d 通过" % (good, len(results)))
    sys.stdout.flush()
    # comtypes 收尾会重复释放 COM 指针，直接退，不给它机会
    os._exit(0 if good == len(results) else 1)


if __name__ == "__main__":
    sys.exit(main())
