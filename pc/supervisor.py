"""接收服务的守护：每 20 秒探一次本机 8765，不通就把 inkvoice.py 拉起来。

为什么需要它：pythonw 进程被系统/杀软清掉后没人管，用户按语音键就「没反应」，
而且从表面完全看不出是电脑端退出了。这个守护是另一个 python 进程，只管探活 + 拉起。

日志：out/supervisor.log
"""
import datetime
import os
import socket
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
# 优先用同目录 .venv 里的 pythonw，没有就用当前的解释器
_venv_py = os.path.join(BASE, ".venv", "Scripts", "pythonw.exe")
PY = _venv_py if os.path.exists(_venv_py) else sys.executable
SCRIPT = os.path.join(BASE, "inkvoice.py")
LOG = os.path.join(BASE, "out", "supervisor.log")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PORT = 8765


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = "%s %s" % (datetime.datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 512 * 1024:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def alive():
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


log("supervisor start")
fails = 0
while True:
    if not alive():
        fails += 1
        try:
            subprocess.Popen([PY, SCRIPT], cwd=BASE,
                             creationflags=CREATE_NO_WINDOW,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("8765 不通（第 %d 次），已拉起接收服务" % fails)
        except Exception as e:
            log("拉起失败: %r" % (e,))
        time.sleep(8)
    else:
        fails = 0
    time.sleep(20)
