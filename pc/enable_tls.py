# -*- coding: utf-8 -*-
"""开启 HTTPS：自签一张证书，把指纹写进两个 App 的配置，重启服务并自测。

用法： python enable_tls.py [电脑IP]
回退： python disable_tls.py
"""
import base64
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import ssl

BASE = os.path.dirname(os.path.abspath(__file__))
CERT = os.path.join(BASE, "cert.pem")
KEY = os.path.join(BASE, "key.pem")


def openssl():
    for c in (shutil.which("openssl"),
              r"C:\Program Files\Git\usr\bin\openssl.exe",
              r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe"):
        if c and os.path.exists(c):
            return c
    return None


def current_host():
    for f in ("ink.xml", "vk.xml"):
        pass
    out = subprocess.run(['adb', 'shell',
                          'su -c "cat /data/data/com.zuakeo.inknote/shared_prefs/ink.xml"'],
                         capture_output=True, text=True).stdout
    m = re.search(r'name="pc">https?://([^<]+)</string>', out)
    if m:
        return m.group(1).split(":")[0]
    return None


def make_cert(host, exe):
    subj = "/CN=inkvoice"
    cmd = [exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
           "-keyout", KEY, "-out", CERT, "-days", "3650",
           "-subj", subj, "-addext", "subjectAltName=IP:%s,DNS:localhost" % host]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("生成证书失败：", r.stderr[-400:])
        return False
    return True


def cert_pin():
    """从 PEM 里取出 DER 算 SHA-256，纯 Python 实现，不依赖 openssl 命令。"""
    txt = io.open(CERT, encoding="utf-8").read()
    body = re.search(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", txt, re.S)
    if not body:
        return None
    der = base64.b64decode("".join(body.group(1).split()))
    return hashlib.sha256(der).hexdigest()


def push_prefs(host, pin):
    """把 https 地址和指纹写进两个 App（复用现成脚本，避免重复实现）。"""
    subprocess.run([sys.executable, os.path.join(BASE, "set_pc_addr.py"),
                    "https://%s:8765" % host], check=False)
    # 指纹由 set_pin.py 从证书算，避免嵌代码
    subprocess.run([sys.executable, os.path.join(BASE, "set_pin.py")], check=False)


def restart_service():
    ps = ("$p = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue "
          "| Select-Object -First 1 -ExpandProperty OwningProcess; "
          "if ($p) { Stop-Process -Id $p -Force }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    time.sleep(2)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    'Start-Process -FilePath "%s" -ArgumentList "%s" -WorkingDirectory "%s" '
                    '-WindowStyle Hidden' % (
                        os.path.join(BASE, ".venv312", "Scripts", "pythonw.exe"),
                        os.path.join(BASE, "_supervisor.py"), BASE)],
                   capture_output=True)
    time.sleep(6)


def verify(host, pin):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen("https://%s:8765/ping" % host, context=ctx, timeout=8) as f:
            ok_conn = f.status == 200
    except Exception as e:
        print("HTTPS 连不上：", e)
        return False
    # 取服务器证书比对指纹
    try:
        with __import__("socket").create_connection((host, 8765), 5) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
    except Exception as e:
        print("取证书失败：", e)
        return False
    got = hashlib.sha256(der).hexdigest()
    print("连接:", "OK" if ok_conn else "FAIL")
    print("指纹:", "一致" if got == pin else "不一致（配置里的和实际的不一样）")
    return ok_conn and got == pin


def main():
    exe = openssl()
    if not exe:
        print("找不到 openssl，先装一个（或把路径写进脚本）")
        return 1
    host = sys.argv[1] if len(sys.argv) > 1 else current_host()
    if not host:
        print("拿不到电脑 IP：请用 python enable_tls.py <电脑IP>")
        return 1
    print("电脑 IP:", host)
    if not make_cert(host, exe):
        return 1
    pin = cert_pin()
    print("证书指纹:", pin[:16], "…")
    push_prefs(host, pin)
    restart_service()
    ok = verify(host, pin)
    print("\n结果：", "HTTPS 已生效" if ok else "没成功，可以跑 disable_tls.py 回退")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
