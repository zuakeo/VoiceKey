# -*- coding: utf-8 -*-
"""本机凭据保管：用 Windows DPAPI 加密，只有当前用户能解开。

第三轮评审点出的一件事：迁移时**不能只改后缀**。
旧明文 `groq_key.txt` 改成 `groq_key.txt.bak` 之后，Key 还在里面，
"`*_key.txt` 为 0 个" 只是统计口径自欺欺人。

所以现在的顺序固定为：
    写 .enc → 回读解密校验 → 覆写粉碎明文 → 删除明文

仍然要说清边界：DPAPI 挡的是「文件被单独拷走 / 换台机器打开」，
**同一 Windows 用户下运行的程序照样能调 CryptUnprotectData 解开**。
它抬高了门槛，不是保险箱。
"""
import ctypes
import os
import re
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))

# 允许存成明文的唯一一个凭据：本机访问令牌。它是服务自己读的随机串，
# 加密它会让 push_token / rotate_token / 自检脚本全部要过一遍 DPAPI，
# 收益有限（同用户下同样能解），所以保留，靠 ACL 收权限（见 harden_acl.py）。
PLAIN_BY_DESIGN = ("secret.txt",)

# 旧版本遗留的明文命名
LEGACY_NAMES = ("groq_key", "deepseek_key")
LEGACY_SUFFIXES = (".txt", ".txt.bak", ".bak", ".txt.tmp", ".tmp", ".txt.old")

# 看起来像 Key 的内容（Groq: gsk_xxx，DeepSeek/OpenAI: sk-xxx）
KEY_RE = re.compile(rb"(?:gsk_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})")

# 自动扫「像 Key 的散落文件」时跳过的目录
SKIP_DIRS = {".venv", ".venv312", "__pycache__", "inbox", "failed", "jobs",
             "out", "quarantine", "_batch", "_bench", "_probe"}


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _protect(data):
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    inb, _keep = _blob(data)
    out = _BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(inb), None, None, None, None, 0,
                                    ctypes.byref(out)):
        raise OSError("CryptProtectData 失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _unprotect(data):
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    inb, _keep = _blob(data)
    out = _BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(inb), None, None, None, None, 0,
                                      ctypes.byref(out)):
        raise OSError("CryptUnprotectData 失败（换了用户或换了机器？）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _enc_path(name):
    return os.path.join(HERE, name + ".enc")


def _plain_path(name):
    return os.path.join(HERE, name + ".txt")


def load(name, env=None):
    """按「环境变量 → 密文 → 明文」的顺序取。明文会被加密并粉碎。"""
    if env and os.environ.get(env, "").strip():
        return os.environ[env].strip()

    ep = _enc_path(name)
    if os.path.exists(ep):
        try:
            return _unprotect(open(ep, "rb").read()).decode("utf-8").strip()
        except OSError:
            return ""

    pp = _plain_path(name)
    if os.path.exists(pp):
        try:
            val = open(pp, encoding="utf-8").read().strip()
        except OSError:
            return ""
        if val:
            try:
                save(name, val)          # 写 .enc 并回读校验
                _scrub_file(pp)          # 明文粉碎，不留 .bak
            except OSError:
                pass
        return val
    return ""


def save(name, value):
    """先写临时文件再原子改名，最后回读校验 —— 校验不过就当失败，不骗自己。"""
    data = _protect(value.encode("utf-8"))
    tmp = _enc_path(name) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _enc_path(name))
    if _unprotect(open(_enc_path(name), "rb").read()).decode("utf-8") != value:
        raise OSError("加密回读校验失败：%s" % name)


def is_encrypted(name):
    return os.path.exists(_enc_path(name))


def _read_text(path, limit=65536):
    try:
        if os.path.getsize(path) > limit:
            return ""
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return ""


def _scrub_file(path, passes=2):
    """覆写 + 截断 + 删除。

    对 SSD 的磨损均衡不构成物理保证，但能保证「文件系统里不再有那份内容」，
    也挡住所有按路径读文件的工具。要做物理级保证只能全盘加密（BitLocker）。
    """
    try:
        size = max(1, os.path.getsize(path))
        for _ in range(passes):
            with open(path, "r+b") as f:
                f.seek(0)
                f.write(os.urandom(size))
                f.flush()
                os.fsync(f.fileno())
        with open(path, "r+b") as f:
            f.truncate(0)
            f.flush()
            os.fsync(f.fileno())
        os.remove(path)
        return True
    except OSError:
        return False


def scrub_legacy():
    """启动时跑：把遗留明文凭据全部收干净。

    返回 [(文件名, 动作)]，只用于日志，不含任何密钥内容。
    """
    acts = []
    for name in LEGACY_NAMES:
        current = load(name) if is_encrypted(name) else ""
        for suffix in LEGACY_SUFFIXES:
            p = os.path.join(HERE, name + suffix)
            if not os.path.isfile(p):
                continue
            val = _read_text(p)
            if not val:
                _scrub_file(p)
                acts.append((name + suffix, "empty-removed"))
                continue
            # 只有 .enc 缺失或解不开时才拿明文兜底。
            # 已有的 .enc 优先 —— 明文可能是换 Key 之前留下的旧值，
            # 拿它回写等于把新 Key 覆盖掉。
            if not current:
                try:
                    save(name, val)
                    current = val
                    acts.append((name + suffix, "encrypted"))
                except OSError:
                    acts.append((name + suffix, "encrypt-failed-kept"))
                    continue
            elif current != val:
                acts.append((name + suffix, "stale-removed"))
            _scrub_file(p)
            acts.append((name + suffix, "scrubbed"))
    return acts


def plaintext_leftovers():
    """扫一遍目录，报告还有哪些文件里躺着像 Key 的东西（只报路径）。"""
    hits = []
    here = HERE
    for root, dirs, files in os.walk(here):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if fn.endswith((".enc", ".pyc")) or fn in PLAIN_BY_DESIGN:
                continue
            p = os.path.join(root, fn)
            try:
                if os.path.getsize(p) > 256 * 1024:
                    continue
                with open(p, "rb") as f:
                    if KEY_RE.search(f.read()):
                        hits.append(os.path.relpath(p, here))
            except OSError:
                continue
    return hits
