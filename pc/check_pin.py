# -*- coding: utf-8 -*-
"""只读比对：电脑证书指纹 vs 两个 App 里存的 tls_pin。

不打印任何令牌/密钥内容，只输出「一致 / 不一致」和指纹前 8 位。
"""
import hashlib
import os
import re
import ssl
import subprocess
import sys

PC = os.path.dirname(os.path.abspath(__file__))
APPS = [("voicekey", "/data/data/com.zuakeo.voicekey/shared_prefs/vk.xml"),
        ("inknote", "/data/data/com.zuakeo.inknote/shared_prefs/ink.xml")]


def adb_cat(path):
    r = subprocess.run(["adb", "shell", "su -c 'cat %s'" % path],
                       capture_output=True, text=True)
    return r.stdout or ""


def main():
    der = ssl.PEM_cert_to_DER_cert(open(PC + r"\cert.pem").read())
    mine = hashlib.sha256(der).hexdigest()
    print("电脑证书 sha256 前 8 位:", mine[:8])
    for name, path in APPS:
        xml = adb_cat(path)
        m = re.search(r'name="tls_pin">([0-9a-fA-F]+)<', xml)
        pin = (m.group(1).lower() if m else "")
        if not pin:
            print("  %-9s 没有存指纹（可能没跑过 set_pin）" % name)
            continue
        same = pin == mine
        print("  %-9s 存的前 8 位: %s  一致=%s" % (name, pin[:8], same))
        if not same:
            print("           !! 指纹不匹配，这个 App 的 HTTPS 会上不去（pin mismatch）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
