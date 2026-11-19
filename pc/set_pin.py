# -*- coding: utf-8 -*-
"""从 cert.pem 算出指纹，写进两个 App 的配置（不打印指纹）。"""
import base64
import hashlib
import io
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CERT = os.path.join(BASE, "cert.pem")
PKGS = [("com.zuakeo.inknote", "ink.xml"), ("com.zuakeo.voicekey", "vk.xml")]
TMP = os.path.join(BASE, "_pin.xml")


def pin():
    txt = io.open(CERT, encoding="utf-8").read()
    m = re.search(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", txt, re.S)
    if not m:
        return None
    der = base64.b64decode("".join(m.group(1).split()))
    return hashlib.sha256(der).hexdigest()


def su(cmd):
    return subprocess.run(["adb", "shell", "su -c '%s'" % cmd],
                          capture_output=True, text=True).stdout


def main():
    if not os.path.exists(CERT):
        print("没有 cert.pem，先跑 enable_tls.py")
        return 1
    p = pin()
    print("指纹已算出，长度", len(p))
    for pkg, f in PKGS:
        remote = "/data/data/%s/shared_prefs/%s" % (pkg, f)
        xml = su("cat %s 2>/dev/null" % remote)
        if "<map" not in xml:
            print("  %s：没有配置文件，跳过" % pkg)
            continue
        tag = '<string name="tls_pin">%s</string>' % p
        if 'name="tls_pin"' in xml:
            xml = re.sub(r'<string name="tls_pin">.*?</string>', tag, xml, flags=re.S)
        else:
            xml = xml.replace("</map>", "    " + tag + "\n</map>")
        io.open(TMP, "w", encoding="utf-8", newline="\n").write(xml)
        subprocess.run(["adb", "push", TMP, "/sdcard/_pin.xml"], capture_output=True)
        uid = su("stat -c %%u /data/data/%s" % pkg).strip()
        su("cp /sdcard/_pin.xml %s; chown %s:%s %s; chmod 660 %s; restorecon %s; "
           "rm -f /sdcard/_pin.xml" % (remote, uid, uid, remote, remote, remote))
        print("  %s：指纹已写入" % pkg)
    if os.path.exists(TMP):
        os.remove(TMP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
