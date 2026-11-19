# -*- coding: utf-8 -*-
"""恢复/设置两个 App 里的电脑地址（读写都走 su，保留其它配置不动）。

用法：  python set_pc_addr.py http://<PC-IP>:8765
"""
import io
import os
import re
import subprocess
import sys

PKGS = [("com.zuakeo.inknote", "ink.xml", "pc"),
        ("com.zuakeo.voicekey", "vk.xml", "server")]
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pcaddr.xml")


def adb_su(inner):
    r = subprocess.run(['adb', 'shell', "su -c '%s'" % inner],
                       capture_output=True, text=True)
    return r.stdout.strip()


def set_one(pkg, fname, key, url):
    remote = "/data/data/%s/shared_prefs/%s" % (pkg, fname)
    got = adb_su("cat %s 2>/dev/null" % remote)
    if got and "<map" in got:
        xml = got if got.startswith("<?xml") else (
            "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n" + got)
    else:
        xml = "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n<map>\n</map>\n"

    tag = '<string name="%s">%s</string>' % (key, url)
    if ('<string name="%s">' % key) in xml:
        xml = re.sub(r'<string name="%s">.*?</string>' % key, tag, xml, flags=re.S)
    else:
        xml = xml.replace("</map>", "    " + tag + "\n</map>")

    with io.open(TMP, "w", encoding="utf-8", newline="\n") as f:
        f.write(xml)
    uid = adb_su("stat -c %%u /data/data/%s" % pkg).strip()
    subprocess.run(['adb', 'push', TMP, '/sdcard/_pcaddr.xml'],
                   capture_output=True, text=True)
    adb_su("mkdir -p /data/data/%s/shared_prefs; cp /sdcard/_pcaddr.xml %s; "
           "chown %s:%s %s; chmod 660 %s; restorecon %s; "
           "chown %s:%s /data/data/%s/shared_prefs; chmod 771 /data/data/%s/shared_prefs; "
           "rm -f /sdcard/_pcaddr.xml"
           % (pkg, remote, uid, uid, remote, remote, remote,
              uid, uid, pkg, pkg))
    print("  %s.%s 已设为 %s" % (pkg, key, url))


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url.startswith("http"):
        print("用法: python set_pc_addr.py http://<电脑IP>:8765")
        return 1
    for pkg, f, k in PKGS:
        set_one(pkg, f, k, url)
    if os.path.exists(TMP):
        os.remove(TMP)
    print("完成。两个 App 都要重启才会生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
