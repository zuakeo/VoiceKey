# -*- coding: utf-8 -*-
"""把电脑端的访问令牌同步进两个手机的 prefs（不打印令牌）。

用法：  python push_token.py

做三件事：
  1. 读 pc/secret.txt（服务启动时自动生成）
  2. 分别写进 com.zuakeo.inknote / com.zuakeo.voicekey 的 shared_prefs
  3. 修好属主、权限、SELinux 标签 —— 否则应用能读不能写（踩过这个坑）
"""
import io
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE, "secret.txt")
PKGS = ["com.zuakeo.inknote", "com.zuakeo.voicekey"]
TMP_PC = os.path.join(BASE, "_token_push.xml")


def sh(cmd, check=False):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        print("命令失败:", cmd, r.stderr.strip())
    return r.stdout.strip()


def adb_su(inner):
    return sh('adb shell "su -c \'%s\'"' % inner)


def read_token():
    if not os.path.exists(TOKEN_FILE):
        print("还没有 secret.txt —— 先启动一次电脑端服务，它会自动生成")
        sys.exit(1)
    t = io.open(TOKEN_FILE, encoding="utf-8").read().strip()
    if len(t) < 32:
        print("secret.txt 内容太短，可能损坏")
        sys.exit(1)
    return t


def inject(xml_text, token):
    tag = '<string name="token">%s</string>' % token
    if '<string name="token">' in xml_text:
        return re.sub(r'<string name="token">.*?</string>', tag, xml_text, flags=re.S)
    return xml_text.replace("</map>", "    " + tag + "\n</map>")


def push_one(pkg, token):
    remote = "/data/data/%s/shared_prefs/ink.xml" % pkg
    if pkg == "com.zuakeo.voicekey":
        remote = "/data/data/%s/shared_prefs/vk.xml" % pkg
    # 已有配置必须先读到，读不到就中止 —— 绝不能从空配置重写，否则会冲掉电脑地址/Key/热词
    # 注意：必须用 su cat 读，adb pull 以 shell 身份拿不到 /data/data
    got = adb_su("cat %s 2>/dev/null" % remote)
    if got and "<map" in got:
        xml = got if got.startswith("<?xml") else (
            "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n" + got)
        if not xml.rstrip().endswith("</map>"):
            print("  %s：配置读出来不完整，已中止" % pkg)
            return False
    elif got.strip():
        print("  %s：配置文件存在但格式异常，已中止" % pkg)
        return False
    else:
        xml = '<?xml version=\'1.0\' encoding=\'utf-8\' standalone=\'yes\' ?>\n<map>\n</map>\n'
        print("  %s：还没有配置文件，将新建一个" % pkg)

    xml = inject(xml, token)
    io.open(TMP_PC, "w", encoding="utf-8", newline="\n").write(xml)

    uid = adb_su("stat -c %%u /data/data/%s" % pkg).strip()
    if not uid.isdigit():
        print("  %s：拿不到 uid，跳过" % pkg)
        return False

    sh("adb shell \"su -c 'cp %s /data/local/tmp/_prefs_bak.xml'\"" % remote)
    sh("adb push %s /sdcard/_tok.xml" % TMP_PC)
    adb_su("mkdir -p /data/data/%s/shared_prefs; cp /sdcard/_tok.xml %s; "
           "chown %s:%s %s; chmod 660 %s; restorecon %s; rm -f /sdcard/_tok.xml"
           % (pkg, remote, uid, uid, remote, remote, remote))
    # 目录本身也要修，否则应用写不进去
    adb_su("chown %s:%s /data/data/%s/shared_prefs; chmod 771 /data/data/%s/shared_prefs"
           % (uid, uid, pkg, pkg))
    print("  %s：已写入" % pkg)
    return True


def main():
    token = read_token()
    print("令牌已读取（不显示内容），长度 %d" % len(token))
    ok = 0
    for pkg in PKGS:
        if push_one(pkg, token):
            ok += 1
    if os.path.exists(TMP_PC):
        os.remove(TMP_PC)
    print("完成 %d/%d" % (ok, len(PKGS)))
    if ok < len(PKGS):
        print("提示：某个 App 可能还没装过，装完再跑一次本脚本即可")


if __name__ == "__main__":
    main()
