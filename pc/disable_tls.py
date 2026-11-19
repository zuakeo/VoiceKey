# -*- coding: utf-8 -*-
"""回退到 HTTP：把证书挪走、清掉指纹、地址改回 http，重启并自测。"""
import io
import os
import re
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))


def host():
    out = subprocess.run(['adb', 'shell',
                          'su -c "cat /data/data/com.zuakeo.inknote/shared_prefs/ink.xml"'],
                         capture_output=True, text=True).stdout
    m = re.search(r'name="pc">https?://([^<]+)</string>', out)
    return m.group(1).split(":")[0] if m else None


def clear_pin():
    script = os.path.join(BASE, "_clear_pin.py")
    io.open(script, "w", encoding="utf-8").write(
        "import io,re,subprocess\n"
        "PKGS=[('com.zuakeo.inknote','ink.xml'),('com.zuakeo.voicekey','vk.xml')]\n"
        "def su(c):\n"
        "    return subprocess.run(['adb','shell','su -c '+repr(c)],capture_output=True,text=True).stdout\n"
        "for pkg,f in PKGS:\n"
        "    p='/data/data/%s/shared_prefs/%s'%(pkg,f)\n"
        "    x=su('cat '+p)\n"
        "    if '<map' not in x: continue\n"
        "    x=re.sub(r'\\s*<string name=\"tls_pin\">.*?</string>','',x,flags=re.S)\n"
        "    io.open('_nopin.xml','w',encoding='utf-8',newline='\\n').write(x)\n"
        "    subprocess.run(['adb','push','_nopin.xml','/sdcard/_nopin.xml'],capture_output=True)\n"
        "    uid=su('stat -c %%u /data/data/'+pkg).strip()\n"
        "    su(\"cp /sdcard/_nopin.xml %s; chown %s:%s %s; chmod 660 %s; restorecon %s; rm -f /sdcard/_nopin.xml\"%(p,uid,uid,p,p,p))\n"
        "print('指纹已清除')\n"
    )
    subprocess.run([sys.executable, script], check=False)
    for f in (script, os.path.join(BASE, "_nopin.xml")):
        try:
            os.remove(f)
        except OSError:
            pass


def main():
    h = sys.argv[1] if len(sys.argv) > 1 else host()
    if not h:
        print("拿不到 IP，用 python disable_tls.py <电脑IP>")
        return 1
    for p in ("cert.pem", "key.pem"):
        src = os.path.join(BASE, p)
        if os.path.exists(src):
            os.replace(src, src + ".off")
    subprocess.run([sys.executable, os.path.join(BASE, "set_pc_addr.py"),
                    "http://%s:8765" % h], check=False)
    clear_pin()

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
    try:
        with urllib.request.urlopen("http://%s:8765/ping" % h, timeout=8) as f:
            print("HTTP 恢复：", "OK" if f.status == 200 else "异常")
    except Exception as e:
        print("HTTP 也没通：", e)
        return 1
    print("已回退到 HTTP。两个 App 需要重启一次才读到新配置。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
