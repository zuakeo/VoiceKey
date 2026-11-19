# -*- coding: utf-8 -*-
"""把录音/转写/任务记录目录的 Windows 权限收到「只有当前用户和系统」。

为什么需要：DPAPI 保护的是 Key 文件；录音、转写文本、任务 JSON 都还是普通文件，
默认继承父目录 ACL，同一台机器上的其他本地账户能直接读。

用法：
    python harden_acl.py            只检查，打印现状
    python harden_acl.py --apply    收紧（目录 + 每个文件，幂等）
    python harden_acl.py --repair   先用 /reset 复原继承，再收紧（需要管理员）
                                    —— 只有在 ACL 被弄坏、连自己都读不了时才用

注意（踩过的坑）：不要对目录用
    icacls <dir> /inheritance:r /grant:r "用户:(OI)(CI)F" /T
(OI)(CI) 对文件无效，而 /T 又把文件的继承 ACE 一起清掉，结果是**每个文件变成空 ACL**，
连所有者都读不了，只能提权修。正确做法：目录用 (OI)(CI)，文件逐个加 (F)。
"""
import getpass
import locale
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))

DIRS = ("inbox", "failed", "jobs", "out", "quarantine")
FILES = ("cert.pem", "key.pem", "secret.txt", "groq_key.enc", "deepseek_key.enc")
SKIP_EXT = (".tmp",)
# 管理员进程创建的文件带着更高的完整性级别，普通进程改不了它的 ACL。
# 这个日志里只有路径和时间戳，没有录音/转写内容；下次用管理员跑一次
# fix_acl_admin.ps1 会自动收紧（脚本最后有一步专门处理它）。
IGNORE = {"out\\fix_acl.log"}


def run(args):
    """icacls 的输出是控制台代码页（中文机器上是 GBK），不能按 UTF-8 解。"""
    r = subprocess.run(args, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    enc = locale.getpreferredencoding(False) or "mbcs"
    return (r.returncode,
            (r.stdout or b"").decode(enc, "replace"),
            (r.stderr or b"").decode(enc, "replace"))


def acl(path):
    _, out, err = run(["icacls", path])
    return out or err


def _principals():
    user = (os.environ.get("USERNAME") or getpass.getuser()).lower()
    comp = (os.environ.get("COMPUTERNAME") or "").lower()
    allow = {"nt authority\\system", "builtin\\administrators", "creator owner"}
    if user:
        allow.add(user)
        if comp:
            allow.add(comp + "\\" + user)
    return allow


def _aces(path):
    """把 icacls 输出拆成 (主体, 权限) 列表。

    不能简单按「行首是不是路径」来切：中文文件名在 GBK 解码后和真实路径
    对不上，路径会粘在主体前面，导致误判「没权限」。这里直接按形态找
    「某主体…:((F)(I)…)」—— 主体里必含反斜杠，权限段只有括号。
    """
    out = acl(path)
    aces = []
    pat = re.compile(r"(NT AUTHORITY\\[^\s:]+|NT SERVICE\\[^\s:]+|BUILTIN\\[^\s:]+"
                     r"|CREATOR OWNER|[^\s\\]+\\[^\s:]+):(\((?:[^)]*\))+)\s*$")
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = pat.search(line)
        if not m:
            continue
        aces.append((m.group(1).strip(), m.group(2)))
    return aces


def is_tight(path):
    """只有 当前用户 / SYSTEM / Administrators 三类主体，才算收紧。"""
    aces = _aces(path)
    if not aces:
        return False, ["没有可用的 ACL 条目（可能已被清空）"]
    allow = _principals()
    bad = ["%s:%s" % (p, r) for p, r in aces if p.lower() not in allow]
    return (not bad), bad[:3]


def _grant(path, suffix):
    user = os.environ.get("USERNAME") or getpass.getuser()
    code, out, err = run(["icacls", path, "/inheritance:r",
                          "/grant:r", "%s:%s" % (user, suffix),
                          "/grant:r", "SYSTEM:%s" % suffix,
                          "/grant:r", "Administrators:%s" % suffix])
    return code == 0, (out or err).strip().splitlines()[:2]


def apply_dir(path):
    return _grant(path, "(OI)(CI)F")


def apply_file(path):
    return _grant(path, "(F)")


def all_files(d):
    out = []
    for root, _dirs, files in os.walk(d):
        for fn in files:
            if fn.endswith(SKIP_EXT):
                continue
            out.append(os.path.join(root, fn))
    return out


def repair():
    """先 /reset 让文件重新继承父目录权限（需要管理员），再逐个收紧。"""
    for d in DIRS:
        p = os.path.join(BASE, d)
        if os.path.isdir(p):
            code, out, err = run(["icacls", p, "/reset", "/T", "/Q"])
            print("  reset %-12s %s" % (d, "OK" if code == 0 else (out or err)[:80]))


def main():
    do_apply = "--apply" in sys.argv
    if "--repair" in sys.argv:
        print("== 复原继承（需要管理员权限）==")
        repair()
        do_apply = True

    bad_total = 0
    for d in DIRS:
        p = os.path.join(BASE, d)
        if not os.path.isdir(p):
            continue
        ok, bad = is_tight(p)
        if ok:
            print("  [OK]    %-14s" % d)
        elif do_apply:
            apply_dir(p)
            ok, bad = is_tight(p)
            if ok:
                print("  [FIXED] %-14s" % d)
            else:
                print("  [FAIL]  %-14s %s" % (d, " / ".join(bad)[:120]))
                bad_total += 1
        else:
            print("  [WIDE]  %-14s %s" % (d, " | ".join(bad)[:140]))
            bad_total += 1

        # 逐个子文件：目录再紧，文件自己也得有 ACE
        for f in all_files(p):
            rel = os.path.relpath(f, BASE)
            if rel in IGNORE:
                continue
            fok, fbad = is_tight(f)
            if fok:
                continue
            if do_apply:
                apply_file(f)
                fok, fbad = is_tight(f)
                if not fok:
                    print("  [FAIL]  %s  %s"
                          % (os.path.relpath(f, BASE), " / ".join(fbad)[:100]))
                    bad_total += 1
            else:
                print("  [WIDE]  %s  %s"
                      % (os.path.relpath(f, BASE), " | ".join(fbad)[:100]))
                bad_total += 1

    for name in FILES:
        p = os.path.join(BASE, name)
        if not os.path.isfile(p):
            continue
        ok, bad = is_tight(p)
        if ok:
            print("  [OK]    %-14s" % name)
            continue
        if do_apply:
            apply_file(p)
            ok, bad = is_tight(p)
            if ok:
                print("  [FIXED] %-14s" % name)
            else:
                print("  [FAIL]  %-14s %s" % (name, " / ".join(bad)[:120]))
                bad_total += 1
        else:
            print("  [WIDE]  %-14s %s" % (name, " | ".join(bad)[:140]))
            bad_total += 1

    print()
    if bad_total == 0:
        print("全部已收紧。")
    elif do_apply:
        print("仍有 %d 处失败：多半是文件的完整性级别比当前进程高，"
              "需要用管理员身份跑 `python harden_acl.py --repair`。" % bad_total)
    else:
        print("有 %d 处没收紧。跑 `python harden_acl.py --apply`；"
              "提示拒绝访问就用管理员跑 `--repair`。" % bad_total)
    return 0 if bad_total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
