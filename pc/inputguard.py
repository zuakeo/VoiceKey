# -*- coding: utf-8 -*-
"""自动输入前的「真焦点控件」判定。

评审原话：**窗口类名不等于文本输入框**。
`Chrome_RenderWidgetHostHWND` 是浏览器整块渲染区，`CoreWindow` 是整个 UWP 窗口，
普通 `Edit` 也可能是密码框。只看类名，等于只看门牌号就断定屋里是客厅。

所以这里改成问 Windows 本身：焦点到底落在哪个控件上，它是不是可编辑文本框。

判定顺序（任何一步拒绝就停，且**失败方向朝安全那边倒**）：

  1. 前台进程在敏感名单（终端 / 远程桌面 / 密码管理器）→ 拒绝
  2. UI Automation：焦点元素必须是 Edit / Document 控件，非密码、可用、非只读，
     并且属于前台窗口 → 放行（source=uia）
  3. UIA 拿不到 → IAccessible(MSAA)：角色是文本、非只读、非受保护 → 放行（source=msaa）
  4. 两套都拿不到 → 拒绝（source=none），除非显式设了 `VK_GUARD_FALLBACK=class`
     才退回旧的类名白名单（用于排障，不建议常态化）

为什么不默认放行：打错窗口的代价是「私人文字进了别的程序」，
比「这次没自动输入、要手动复制一次」严重得多。

工程细节：
  - UIA 对象按线程缓存（`threading.local`），不跨线程传递；
    连接/事务超时压到 2 秒，避免被卡死的目标进程拖住整个转写队列。
  - 模式接口在独立函数里取、立即释放，不在调用栈上等解释器收尾，
    否则退出时 comtypes 会在 CoUninitialize 之后 GC 指针，刷一堆访问冲突。

返回结构统一为 dict：
    {"allow": bool, "code": str, "detail": str, "source": "uia|msaa|class|none",
     "hwnd": int, "exe": str}
"""
import atexit
import ctypes
import os
import threading
import time
from ctypes import wintypes

# 前台是这些程序时一律不自动输入（进程名小写）
SENSITIVE = ("cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
             "conhost.exe", "mstsc.exe", "ssh.exe", "putty.exe",
             "keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe",
             "openvpn.exe", "winscp.exe")

# 旧逻辑用的类名白名单，只在 VK_GUARD_FALLBACK=class 时启用
TEXT_INPUT_CLASSES = (
    "Edit", "RichEdit", "RICHEDIT", "_WwG", "Scintilla", "TEdit", "TMemo",
    "Chrome_RenderWidgetHostHWND", "MozillaWindowClass", "CoreWindow",
    "Windows.UI.Core.CoreWindow", "ApplicationFrameInputSinkWindow",
    "TfrmMain", "WpsTextBox", "Qt5152QWindowIcon",
)

# UIA 控件类型
CT_EDIT = 50004
CT_DOCUMENT = 50030
CT_TEXT = 50020

CT_NAMES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
}

# MSAA 角色 / 状态
ROLE_SYSTEM_TEXT = 0x2A
STATE_SYSTEM_UNAVAILABLE = 0x1
STATE_SYSTEM_FOCUSED = 0x4
STATE_SYSTEM_READONLY = 0x40
STATE_SYSTEM_FOCUSABLE = 0x100000
STATE_SYSTEM_PROTECTED = 0x40000000

_TIMEOUT_MS = 2000

_uia_lock = threading.RLock()      # 必须是可重入锁：warmup 持锁时会再调两个 _load_*
_uia_mod = None
_uia_err = ""
_msaa_mod = None
_msaa_err = ""
_local = threading.local()


def _u32():
    return ctypes.windll.user32


def _k32():
    return ctypes.windll.kernel32


def fg_hwnd():
    try:
        return int(_u32().GetForegroundWindow() or 0)
    except Exception:
        return 0


def pid_of(hwnd):
    try:
        pid = wintypes.DWORD()
        _u32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def exe_of_pid(pid):
    if not pid:
        return ""
    h = _k32().OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _k32().QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    except Exception:
        pass
    finally:
        _k32().CloseHandle(h)
    return ""


def focus_hwnd(hwnd):
    """GetGUIThreadInfo 拿真正有焦点的子窗口（UIA 拿不到时的兜底用）。"""
    try:
        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                        ("rcCaret", wintypes.RECT)]

        tid = _u32().GetWindowThreadProcessId(hwnd, None)
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if _u32().GetGUIThreadInfo(tid, ctypes.byref(info)):
            return int(info.hwndFocus or 0)
    except Exception:
        pass
    return 0


def class_of(hwnd):
    try:
        buf = ctypes.create_unicode_buffer(256)
        _u32().GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


def _coinit():
    """只能用 STA（单线程套间）。

    实测：同一台机器上 `CoInitializeEx(COINIT_MULTITHREADED)` 之后
    `CreateObject(CUIAutomation)` 会稳定段错误（6/6 崩），
    换成 STA 之后 6/6 正常。comtypes 在 import 时已经按 STA 初始化过，
    再来一次 MTA 会拿到 RPC_E_CHANGED_MODE，状态是坏的。

    代价是 UIA 对象只能在自己线程里用 —— 我们本来也是按线程缓存，
    不跨线程传递。
    """
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


def _release_at_exit():
    """退出前主动放掉 COM 指针。

    注意：只在「进程真的要退出」时调用。comtypes 自己也会在收尾时释放一遍，
    两边都放会变成双重 Release（访问冲突）。所以这里只清缓存引用，不深究顺序，
    真正的干净退出交给 __main__ 里的 os._exit。
    """
    try:
        _local.uia = None
    except Exception:
        pass


atexit.register(_release_at_exit)


# ───────────────────────────── UI Automation ─────────────────────────────
def _load_uia():
    """加载 UIAutomationCore 的类型库；失败返回 None，错误信息留在 _uia_err。"""
    global _uia_mod, _uia_err
    if _uia_mod is not None:
        return _uia_mod
    with _uia_lock:
        if _uia_mod is not None:
            return _uia_mod
        try:
            import comtypes          # noqa: F401
            import comtypes.client
            _uia_mod = comtypes.client.GetModule("UIAutomationCore.dll")
        except Exception as e:
            _uia_err = "%s: %s" % (type(e).__name__, e)
            _uia_mod = None
    return _uia_mod


def _automation():
    """每个线程一个 UIA 对象；顺手把连接超时压到 2 秒。

    注意顺序：**先把 oleacc 的类型库也加载完，再创建 UIA 对象**。
    实测反过来的话（已经建了 UIA 对象、再去加载 oleacc 的类型库），
    comtypes 的代码生成会让活着的接口指针 vtable 失效，进程直接段错误。
    这也是 warmup() 只预热类型库、不创建对象的原因。
    """
    uia = getattr(_local, "uia", None)
    if uia is not None:
        return uia
    import comtypes
    import comtypes.client

    _coinit()
    _load_msaa()
    mod = _load_uia()
    if mod is None:
        raise RuntimeError("uia-unavailable:" + _uia_err)
    uia = comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)
    try:
        u2 = uia.QueryInterface(mod.IUIAutomation2)
        u2.ConnectionTimeout = _TIMEOUT_MS
        u2.TransactionTimeout = _TIMEOUT_MS
    except Exception:
        pass
    _local.uia = uia
    return uia


def _legacy_state(el, mod):
    """LegacyIAccessible 的状态位。拿不到返回 None。

    放在独立函数里，指针一返回就释放，不留在调用栈上等解释器收尾。
    """
    import comtypes
    try:
        lp = comtypes.cast(el.GetCurrentPattern(mod.UIA_LegacyIAccessiblePatternId),
                           ctypes.POINTER(mod.IUIAutomationLegacyIAccessiblePattern))
        return int(lp.CurrentState)
    except Exception:
        return None


def _value_readonly(el, mod):
    """ValuePattern 的只读标志。拿不到返回 None。"""
    import comtypes
    try:
        vp = comtypes.cast(el.GetCurrentPattern(mod.UIA_ValuePatternId),
                           ctypes.POINTER(mod.IUIAutomationValuePattern))
        return bool(vp.CurrentIsReadOnly)
    except Exception:
        return None


def _has_text_pattern(el, mod):
    try:
        return bool(el.GetCurrentPropertyValue(mod.UIA_IsTextPatternAvailablePropertyId))
    except Exception:
        return False


def _uia_probe():
    """在当前线程查焦点元素。失败抛异常，由上层统一降级。

    只读 / 保密字段判断走两条路：
      - ValuePattern.IsReadOnly（最权威，Edit 类控件基本都有）
      - LegacyIAccessiblePattern.CurrentState 的状态位（Chromium / Electron /
        老 Win32 控件常常只给这一套）
    两条都拿不到才认为「不确定」，不确定就拒绝。
    """
    uia = _automation()
    mod = _load_uia()
    el = uia.GetFocusedElement()
    try:
        info = {
            "type": int(el.CurrentControlType),
            "is_secret": bool(el.CurrentIsPassword),
            "enabled": bool(el.CurrentIsEnabled),
            "focusable": bool(el.CurrentIsKeyboardFocusable),
            "win": int(el.CurrentNativeWindowHandle or 0),
            "readonly": None,
            "text_pattern": False,
        }
        state = _legacy_state(el, mod)
        if state is not None:
            if state & STATE_SYSTEM_PROTECTED:
                info["is_secret"] = True
            if state & STATE_SYSTEM_UNAVAILABLE:
                info["enabled"] = False
            if state & STATE_SYSTEM_READONLY:
                info["readonly"] = True
            elif state & (STATE_SYSTEM_FOCUSED | STATE_SYSTEM_FOCUSABLE):
                info["readonly"] = False
        ro = _value_readonly(el, mod)
        if ro is not None:
            info["readonly"] = ro
        info["text_pattern"] = _has_text_pattern(el, mod)
        return info
    finally:
        del el


# ───────────────────────────── IAccessible ─────────────────────────────
def _load_msaa():
    global _msaa_mod, _msaa_err
    if _msaa_mod is not None:
        return _msaa_mod
    with _uia_lock:
        if _msaa_mod is not None:
            return _msaa_mod
        try:
            import comtypes          # noqa: F401
            import comtypes.client
            _msaa_mod = comtypes.client.GetModule("oleacc.dll")
        except Exception as e:
            _msaa_err = "%s: %s" % (type(e).__name__, e)
            _msaa_mod = None
    return _msaa_mod


def _msaa_probe(hwnd):
    mod = _load_msaa()
    if mod is None:
        raise RuntimeError("msaa-unavailable:" + _msaa_err)
    _coinit()
    oleacc = ctypes.windll.oleacc
    p = ctypes.POINTER(mod.IAccessible)()
    OBJID_CLIENT = 0xFFFFFFFC
    iid = mod.IAccessible._iid_
    hr = oleacc.AccessibleObjectFromWindow(hwnd, OBJID_CLIENT,
                                           ctypes.byref(iid), ctypes.byref(p))
    if hr != 0 or not p:
        raise RuntimeError("msaa-null(0x%08x)" % (hr & 0xFFFFFFFF))
    try:
        return {"role": int(p.accRole(0)), "state": int(p.accState(0))}
    finally:
        del p


# ───────────────────────────── 判定 ─────────────────────────────
def _deny(code, detail, source, hwnd=0, exe=""):
    return {"allow": False, "code": code, "detail": detail,
            "source": source, "hwnd": hwnd, "exe": exe}


def _allow(code, detail, source, hwnd, exe):
    return {"allow": True, "code": code, "detail": detail,
            "source": source, "hwnd": hwnd, "exe": exe}


def _class_fallback(hwnd, exe):
    cls = class_of(focus_hwnd(hwnd) or hwnd)
    low = cls.lower()
    if any(low.startswith(c.lower()) for c in TEXT_INPUT_CLASSES):
        return _allow("class_allow", "class=" + cls, "class", hwnd, exe)
    return _deny("not_text_input", "class=" + (cls or "?"), "class", hwnd, exe)


def verdict(exe="", hwnd=0):
    """判定当前前台能不能自动输入。永不抛异常。"""
    try:
        hwnd = hwnd or fg_hwnd()
        exe = (exe or exe_of_pid(pid_of(hwnd))).lower()
        if not hwnd:
            return _deny("no_foreground", "no hwnd", "none", 0, exe)

        for bad in SENSITIVE:
            if exe == bad or exe.startswith(bad):
                return _deny("sensitive_window", exe, "none", hwnd, exe)

        info, err = None, None
        try:
            info = _uia_probe()
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:120])

        if info is not None:
            ct = info["type"]
            name = CT_NAMES.get(ct, str(ct))
            win = info["win"]
            if win and pid_of(win) and pid_of(win) != pid_of(hwnd):
                # 焦点跑到别的进程去了（工具提示、输入法候选窗等）
                return _deny("focus_elsewhere", name, "uia", hwnd, exe)
            if info["is_secret"]:
                return _deny("secret_field", name, "uia", hwnd, exe)
            if not info["enabled"]:
                return _deny("disabled", name, "uia", hwnd, exe)
            if ct == CT_EDIT:
                if info["readonly"] is None:
                    return _deny("readonly_unknown", name, "uia", hwnd, exe)
                if info["readonly"]:
                    return _deny("readonly", name, "uia", hwnd, exe)
                return _allow("text_edit", name, "uia", hwnd, exe)
            if ct == CT_DOCUMENT:
                if info["readonly"] is True:
                    return _deny("readonly", name, "uia", hwnd, exe)
                if info["readonly"] is None:
                    # 文档类控件常常没有 ValuePattern（Word / Chrome / WPS 都这样）。
                    # 这时只认「可键盘聚焦 + 有 TextPattern」的文档：
                    # PDF 阅读器、预览窗口通常不可聚焦，会被这一条挡住。
                    if not (info["focusable"] and info["text_pattern"]):
                        return _deny("document_not_editable", name, "uia", hwnd, exe)
                    return _allow("text_document", name + "+textpattern", "uia", hwnd, exe)
                return _allow("text_document", name, "uia", hwnd, exe)
            if ct == CT_TEXT:
                return _deny("static_text", name, "uia", hwnd, exe)
            return _deny("not_text_input", name, "uia", hwnd, exe)

        # UIA 不可用 → MSAA
        err2 = None
        got = None
        try:
            got = _msaa_probe(focus_hwnd(hwnd) or hwnd)
        except Exception as e:
            err2 = "%s: %s" % (type(e).__name__, str(e)[:120])
        if got is not None:
            if got["state"] & STATE_SYSTEM_PROTECTED:
                return _deny("secret_field", "msaa protected", "msaa", hwnd, exe)
            if got["state"] & STATE_SYSTEM_READONLY:
                return _deny("readonly", "msaa readonly", "msaa", hwnd, exe)
            if got["state"] & STATE_SYSTEM_UNAVAILABLE:
                return _deny("disabled", "msaa unavailable", "msaa", hwnd, exe)
            if got["role"] == ROLE_SYSTEM_TEXT:
                return _allow("text_edit", "msaa role=text", "msaa", hwnd, exe)
            return _deny("not_text_input", "msaa role=0x%x" % got["role"],
                         "msaa", hwnd, exe)

        if os.environ.get("VK_GUARD_FALLBACK", "").strip().lower() == "class":
            return _class_fallback(hwnd, exe)
        why = (err or "") + ((" / " + err2) if err2 else "")
        return _deny("guard_unavailable", why[:160], "none", hwnd, exe)
    except Exception as e:                  # 兜底：判定本身出错也当拒绝
        return {"allow": False, "code": "guard_error",
                "detail": "%s: %s" % (type(e).__name__, str(e)[:120]),
                "source": "none", "hwnd": 0, "exe": ""}


def warmup():
    """启动时预热：只把两个类型库的代码生成跑完，不创建 COM 对象。

    这样第一次真正输入时不会多花 1~2 秒，也不会出现
    「先建对象、后加载另一个类型库」导致的段错误。
    """
    with _uia_lock:
        m_msaa = _load_msaa()
        m_uia = _load_uia()
    return {"uia": m_uia is not None, "msaa": m_msaa is not None,
            "uia_err": _uia_err, "msaa_err": _msaa_err}


# 命令行入口放在 guard_probe.py。
# 不要在本文件里加 __main__ 分支：以 `python -m inputguard` 方式运行时，
# comtypes 的代码生成会走另一条路径，实测会段错误（同机器上用 guard_probe.py
# 调用同一批函数则 5/5 正常）。少一个入口，少一类坑。
