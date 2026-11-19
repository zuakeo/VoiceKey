"""
墨水屏语音输入 — 电脑端（纯云端，不含本地模型）

设备推来 m4a → ffmpeg 转 16k wav → 云端 ASR（Groq whisper-large-v3-turbo，
与 SayIt 同模型）→ AI 整理 → 写剪贴板并粘贴到当前焦点窗口。

识别后端：
  1. Groq     —— pc/groq_key.txt（一行 key），支持热词 prompt（pc/hotwords.txt）
  2. DeepSeek —— pc/deepseek_key.txt 或 ds_cred.json（无热词能力）
  都没有就直接报错，不做任何本地推理、不下载模型。

环境变量：
  VK_ASR=groq|deepseek   强制指定后端
  VK_POLISH=0            关闭 AI 整理
  VK_AUTOPASTE=0         只进剪贴板不粘贴
"""
import binascii
import hmac
import re
import shutil
import uuid
import hashlib
import ssl
import json
import os
import subprocess
import sys
import time
import ctypes
import queue
import threading
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# GUI 进程（pythonw）调控制台程序时必须带这个标志，否则会闪一个黑窗口
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

BASE = os.path.dirname(os.path.abspath(__file__))
# 数据目录可以单独指走：自检脚本用临时目录起一个隔离实例，不碰真实收件箱
DATA = os.environ.get("VK_DATA_DIR") or BASE
INBOX = os.path.join(DATA, "inbox")
FAILED = os.path.join(DATA, "failed")
LOGDIR = os.path.join(DATA, "out")
QUARANTINE = os.path.join(DATA, "quarantine")
for _d in (INBOX, FAILED, LOGDIR, QUARANTINE):
    os.makedirs(_d, exist_ok=True)

PORT = int(os.environ.get("VK_PORT", "8765"))
AUTO_PASTE = os.environ.get("VK_AUTOPASTE", "1") == "1"
POLISH = os.environ.get("VK_POLISH", "1") == "1"
# 拒绝自动输入时默认**不碰剪贴板**：剪贴板会触发电脑管家的跨屏弹窗，
# 而且 Windows 剪贴板历史/云剪贴板会长期留着私人转写内容。
# 需要旧行为（拒绝时也放剪贴板）就设 VK_CLIP_ON_REFUSE=1。
CLIP_ON_REFUSE = os.environ.get("VK_CLIP_ON_REFUSE", "0") == "1"

import asr
import terms
import asr_cloud
import inputguard
import vault
import punct

# 以 `pythonw inkvoice.py` 启动时本模块叫 __main__；uploader 里那句
# `import inkvoice as V` 会**把整个文件再执行一遍**，于是出现第二份 jobs 队列
# 和第二份模块状态 —— 上传进来的任务被放进没人消费的队列，永远卡在 queued。
# 这里把自己登记成 inkvoice，保证全进程只有一份状态。
if __name__ == "__main__":
    sys.modules.setdefault("inkvoice", sys.modules[__name__])

jobs = queue.Queue()


def _crash(exc_type, exc, tb):
    import traceback
    try:
        with open(os.path.join(LOGDIR, "crash.log"), "a", encoding="utf-8") as f:
            f.write("\n==== %s ====\n" % datetime.datetime.now().isoformat())
            traceback.print_exception(exc_type, exc, tb, file=f)
    except OSError:
        pass
    log("崩溃：%s: %s" % (exc_type.__name__, exc))


sys.excepthook = _crash
try:
    threading.excepthook = lambda a: _crash(a.exc_type, a.exc_value, a.exc_traceback)
except Exception:
    pass


# ───────────────────────── 访问控制（第一批加固） ─────────────────────────
SECRET_FILE = os.path.join(BASE, "secret.txt")
MAX_UPLOAD = int(os.environ.get("VK_MAX_UPLOAD", str(12 * 1024 * 1024)))
RATE_LIMIT = int(os.environ.get("VK_RATE_LIMIT", "60"))     # 每 IP 每分钟请求数
LOG_FULL = os.environ.get("VK_LOG_FULL", "0") == "1"
_rl = {}          # ip -> [时间戳...]
_rl_lock = threading.Lock()


def load_token():
    """读取或生成访问令牌。只存在本地文件里，不进 URL、不进日志。"""
    try:
        if os.path.exists(SECRET_FILE):
            with open(SECRET_FILE, encoding="utf-8") as f:
                t = f.read().strip()
                if len(t) >= 32:
                    return t
    except OSError:
        pass
    t = binascii.hexlify(os.urandom(32)).decode()
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(t + "\n")
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return t


TOKEN = load_token()


def rate_ok(ip):
    now = time.time()
    with _rl_lock:
        arr = [t for t in _rl.get(ip, []) if now - t < 60]
        if len(arr) >= RATE_LIMIT:
            _rl[ip] = arr
            return False
        arr.append(now)
        _rl[ip] = arr
        return True


# 敏感进程名单与「是不是输入框」的判断都搬到 inputguard.py 了。
# 这里不再保留一份 —— 两份名单只会让人误以为旧的还在生效。

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    try:
        print(line, flush=True)     # pythonw 下 stdout 可能是断的，不能让它把进程带崩
    except Exception:
        pass
    # 挨个试：当前日志文件写不进去（权限坏了 / 被占用）就换一个名字，
    # 不能让「日志」这种排障手段先失效。
    for name in ("receiver.log", "receiver-b.log", "receiver-c.log"):
        p = os.path.join(LOGDIR, name)
        try:
            if os.path.exists(p) and os.path.getsize(p) > 1024 * 1024:
                try:
                    os.replace(p, p + ".1")
                except OSError:
                    pass
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return
        except OSError:
            continue


# ---------- 剪贴板与粘贴 ----------
def set_clipboard(text):
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)

    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalFree.argtypes = [ctypes.c_void_p]
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    data = text.encode("utf-16-le") + b"\x00\x00"
    h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = k32.GlobalLock(h)
    if not p:
        k32.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    k32.GlobalUnlock(h)

    if not u32.OpenClipboard(None):
        k32.GlobalFree(h)
        return False
    try:
        u32.EmptyClipboard()
        if not u32.SetClipboardData(CF_UNICODETEXT, h):
            k32.GlobalFree(h)
            return False
    finally:
        u32.CloseClipboard()
    return True


def send_ctrl_v():
    u32 = ctypes.windll.user32
    KEYUP = 2
    u32.keybd_event(0x11, 0, 0, 0)
    time.sleep(0.02)
    u32.keybd_event(0x56, 0, 0, 0)
    time.sleep(0.02)
    u32.keybd_event(0x56, 0, KEYUP, 0)
    time.sleep(0.02)
    u32.keybd_event(0x11, 0, KEYUP, 0)


# ---------- 直接「打字」而不是走剪贴板 ----------
# 小米电脑管家的跨屏协作会在剪贴板变化时弹窗（就是用户看到的那个），
# 所以改成 SendInput 逐字注入，剪贴板完全不动。
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def send_text(text, delay=0.004):
    """用 KEYEVENTF_UNICODE 逐字注入，绕过剪贴板（也就不会触发小米互传弹窗）。"""
    u32 = ctypes.windll.user32
    INPUT_KEYBOARD = 1
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002

    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        code = units[i] | (units[i + 1] << 8)
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = _INPUT(type=INPUT_KEYBOARD,
                         u=_INPUTUNION(ki=_KEYBDINPUT(0, code, flags, 0, None)))
            u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        time.sleep(delay)

def fg_window():
    """返回 (前台窗口句柄, 进程名)。输入前后都要确认是同一个窗口。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid.value)
        name = ""
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value).lower()
            finally:
                k.CloseHandle(h)
        return hwnd, name
    except Exception:
        return None, ""


def send_text_checked(text, target_hwnd, cid):
    """逐字注入，边打边确认目标窗口没变。

    返回 (已输入字数, 原因)。原因 None 表示全部成功。
    中途换窗口就停手 —— 宁可少打，也不要把私人内容打进别的程序。
    """
    u32 = ctypes.windll.user32
    INPUT_KEYBOARD = 1
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002

    units = text.encode("utf-16-le")
    sent = 0
    total = len(units) // 2
    for i in range(0, len(units), 2):
        if target_hwnd and u32.GetForegroundWindow() != target_hwnd:
            return sent, "window_changed"
        code = units[i] | (units[i + 1] << 8)
        ok = 0
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = _INPUT(type=INPUT_KEYBOARD,
                         u=_INPUTUNION(ki=_KEYBDINPUT(0, code, flags, 0, None)))
            ok = u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
            if ok != 1:
                return sent, "sendinput_failed"
        sent += 1
        if sent % 20 == 0:
            time.sleep(0.001)
        else:
            time.sleep(0.004)
    return sent if sent == total else sent, (None if sent == total else "partial")


def append_log(text):
    """每天一个 md。当天的文件写不进去（权限坏了、被占用）就换名字继续写。

    这条链路上「转写结果」比「日志文件叫什么」重要得多，不能因为写日志失败
    把整条任务标成 failed。
    """
    day = datetime.date.today().isoformat()
    line = "- %s  %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"), text)
    for name in (day + ".md", day + "-b.md", day + "-c.md"):
        try:
            with open(os.path.join(LOGDIR, name), "a", encoding="utf-8") as f:
                f.write(line)
            return True
        except OSError:
            continue
    return False


# ---------- 后端选择 ----------
def provider():
    forced = os.environ.get("VK_ASR", "").strip().lower()
    if forced == "deepseek":
        return "deepseek" if asr.available() else "none"
    if forced == "groq":
        return "groq" if asr_cloud.available() else "none"
    if asr_cloud.available():
        return "groq"
    if asr.available():
        return "deepseek"
    return "none"


_ffmpeg_path = None


def get_ffmpeg():
    global _ffmpeg_path
    if _ffmpeg_path is None:
        import imageio_ffmpeg
        _ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    return _ffmpeg_path


def to_wav(path):
    import subprocess
    tmp = path + ".16k.wav"
    subprocess.run(
        [get_ffmpeg(), "-nostdin", "-y", "-loglevel", "error", "-i", path,
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", tmp],
        check=True, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    return tmp


def _wav_seconds(wav):
    import wave
    with wave.open(wav, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def to_mp3(wav, bitrate="24k"):
    """上传用：16k 单声道 24kbps，15 秒约 43KB（原始 wav 要 463KB，10 倍差）。实测与 40k 识别结果一致。"""
    import subprocess
    out = wav[:-4] + ".up.mp3"
    subprocess.run(
        [get_ffmpeg(), "-nostdin", "-y", "-loglevel", "error", "-i", wav,
         "-ar", "16000", "-ac", "1", "-b:a", bitrate, "-f", "mp3", out],
        check=True, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    return out

def transcribe_full(wav, max_len=28.0):
    """超过 28 秒就切成几段分别识别再拼起来。

    原因：whisper 是 30 秒窗口，整段 60 秒送上去它只返回前 30 秒的内容，
    后半句直接丢；而手机上又设了 60 秒录音上限。两头一起改才完整。
    切点在「最安静的 100ms」上找，尽量不切断字词。
    """
    import subprocess
    import wave
    import numpy as np

    if _wav_seconds(wav) <= max_len:
        up = to_mp3(wav)
        try:
            return asr_cloud.transcribe(up)
        finally:
            try:
                os.remove(up)
            except OSError:
                pass

    with wave.open(wav, "rb") as w:
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    total = data.size / float(rate)

    cuts = [0.0]
    pos = 0.0
    while total - pos > max_len:
        lo = pos + max_len * 0.65
        hi = pos + max_len
        best_t, best_rms = hi, 1e18
        t = lo
        while t < hi:
            a = int(t * rate)
            seg = data[a:a + int(0.1 * rate)]
            if seg.size:
                rms = float(np.sqrt((seg * seg).mean()))
                if rms < best_rms:
                    best_rms, best_t = rms, t
            t += 0.1
        cuts.append(best_t)
        pos = best_t
    cuts.append(total)

    pieces = []
    texts = []
    try:
        for i in range(len(cuts) - 1):
            a, b = cuts[i], cuts[i + 1]
            out = "%s.part%d.wav" % (wav, i)
            subprocess.run(
                [get_ffmpeg(), "-nostdin", "-y", "-loglevel", "error",
                 "-ss", "%.3f" % a, "-t", "%.3f" % (b - a), "-i", wav,
                 "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", out],
                check=True, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            pieces.append(out)
            up = to_mp3(out)
            try:
                texts.append(asr_cloud.transcribe(up))
            finally:
                try:
                    os.remove(up)
                except OSError:
                    pass
        log("长录音切成 %d 段识别" % len(pieces))
        return "".join(texts)
    finally:
        for x in pieces:
            try:
                os.remove(x)
            except OSError:
                pass

def polish_text(text):
    if not POLISH or not text.strip():
        return text
    try:
        import polish
        out = polish.run(text)
        if out:
            log("AI 整理：%d 字" % len(out))
            return out
    except Exception as e:
        log("AI 整理跳过：%r" % (e,))
    return text


def is_silent(wav, threshold=500.0, frame=1600, min_frames=5):
    """静音就别送去识别了，否则 whisper 会编出「作词、曲：李宗盛」「321。」这种幻觉。

    两个条件同时满足才算「有声」：
      1. 按 100ms 窗口的 RMS >= threshold（不能整段平均，短句会被静音摊薄误杀）
      2. 超过阈值的窗口数 >= min_frames（防一次碰撞噪声被当成说话）
    实测：说话窗口 RMS 456~1471，纯静音窗口 55~100。
    """
    try:
        import wave
        import numpy as np
        with wave.open(wav, "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        if a.size == 0:
            return True
        loud = 0
        for i in range(0, a.size, frame):
            seg = a[i:i + frame]
            if seg.size and float(np.sqrt((seg * seg).mean())) >= threshold:
                loud += 1
                if loud >= min_frames:
                    return False
        return True
    except Exception:
        return False


def trim_silence(wav, frame_ms=30, thresh=260, margin_ms=150):
    """把首尾的静音裁掉（保留 150ms 余量）。

    收益：按住键后到开口前通常有 1~2 秒静音，裁掉能直接减少上传量和识别耗时；
    边缘留白少了，whisper 在开头/结尾瞎编的概率也低一些。
    """
    import wave
    import numpy as np

    try:
        with wave.open(wav, "rb") as w:
            rate = w.getframerate()
            ch = w.getnchannels()
            width = w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width != 2 or ch != 1:
            return False
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        step = max(1, int(rate * frame_ms / 1000.0))
        n = a.size // step
        if n < 4:
            return False
        rms = np.array([np.sqrt((a[i * step:(i + 1) * step] ** 2).mean()) for i in range(n)])
        loud = np.where(rms >= thresh)[0]
        if loud.size == 0:
            return False
        start = max(0, (loud[0] * step) - int(rate * margin_ms / 1000.0))
        end = min(a.size, (loud[-1] + 1) * step + int(rate * margin_ms / 1000.0))
        if end - start < rate * 0.4:      # 太短就别裁了
            return False
        if start < rate * 0.1 and end > a.size - rate * 0.1:
            return False                   # 本来就没多少余量
        out = wav[:-4] + ".trim.wav"
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(a[start:end].astype(np.int16).tobytes())
        os.replace(out, wav)
        log("裁掉首尾静音 %.1fs -> %.1fs" % (a.size / float(rate), (end - start) / float(rate)))
        return True
    except Exception as e:
        log("裁静音失败 %r" % (e,))
        return False


def normalize_wav(wav, target=0.8, max_gain=24.0):
    """音量太小的录音先拉起来再送云端，不然识别率明显下降。只放大不缩小。

    用户反馈「已经贴着麦克风说话」，实测峰值仍只有 1500~6000（满量程 32767），
    所以这里把目标峰值提到 0.9、最大增益放到 24 倍。
    """
    try:
        import wave
        import numpy as np
        with wave.open(wav, "rb") as w:
            params = w.getparams()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        if a.size == 0:
            return
        peak = float(np.abs(a).max())
        if peak <= 0:
            return
        gain = min(target * 32767.0 / peak, max_gain)
        if gain <= 1.2:
            log("音量足够，未放大（峰值 %.0f）" % peak)
            return
        a = np.clip(a * gain, -32768, 32767).astype(np.int16)
        with wave.open(wav, "wb") as w:
            w.setparams(params)
            w.writeframes(a.tobytes())
        log("音量放大 %.1fx（原峰值 %.0f）" % (gain, peak))
    except Exception:
        pass


# ───────────────────── 第二阶段：标识、校验、生命周期 ─────────────────────
import glob

CLIP_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?:vk|rec|c)_\d{8}_\d{6}(?:_[0-9a-f]{6})?)$", re.I)

MAX_QUEUE = int(os.environ.get("VK_MAX_QUEUE", "20"))
JOB_KEEP_DAYS = int(os.environ.get("VK_JOB_KEEP_DAYS", "7"))
FAILED_KEEP_DAYS = int(os.environ.get("VK_FAILED_KEEP_DAYS", "0"))   # 0 = 永不自动删


def clip_ok(cid):
    """只接受 UUID 或我们自己的两种命名，别的一律当不可信。"""
    if not cid or len(cid) > 64:
        return False
    if "/" in cid or "\\" in cid or ".." in cid:
        return False
    return bool(CLIP_RE.match(cid))


def looks_like_audio(path):
    """看文件头，不要只看大小：MP4 有 ftyp/moov/mdat，WAV 是 RIFF。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    if len(head) < 8:
        return False
    if head[4:8] in (b"ftyp", b"moov", b"mdat"):
        return True                      # MP4 / M4A
    if head[:4] == b"RIFF":              # WAV
        return True
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True                      # MPEG 音频（mp3）
    if head[:4] in (b"OggS", b"fLaC"):   # Ogg / FLAC
        return True
    return False


def sha256_of(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _sidecar(p):
    """转写过程中的中间文件（16k wav / trim / 上传用 mp3 / 分段），可以直接删。"""
    b = os.path.basename(p).lower()
    if b.endswith(".tmp"):
        return True
    if b.endswith(".16k.wav") or b.endswith(".trim.wav") or b.endswith(".up.mp3"):
        return True
    if ".part" in b and b.endswith(".wav"):
        return True
    return False


def _quarantine(p, why):
    """来路不明 / 损坏的文件挪进 quarantine 并留一张说明，不直接删。"""
    try:
        dst = os.path.join(QUARANTINE, "%s.%s"
                           % (os.path.basename(p), time.strftime("%Y%m%d_%H%M%S")))
        os.replace(p, dst)
        with open(dst + ".why.txt", "w", encoding="utf-8") as f:
            f.write(why + "\n")
        log("隔离 %s（%s）" % (os.path.basename(p), why))
        return True
    except OSError as e:
        log("隔离失败 %s：%r" % (os.path.basename(p), e))
        return False


def reconcile_jobs():
    """启动对账：把「任务记录 × 音频文件」的组合全部摆平。

    | 任务记录 | 音频 | 处理 |
    |---|---|---|
    | 有 | 有 | 没转写完 → 重新转写（**不自动输入**） |
    | 有 | 无 | needs_review（crash_before_transcribe） |
    | 无 | 有 | 认领成孤儿任务并重新转写（录音不丢） |
    | 无 | 无 | 什么都不做 |
    | 损坏 | 有 | 记录改名留档，音频重新认领 |
    | 损坏 | 无 | 记录改名留档 |

    两条硬规矩：
      1. 恢复出来的任务一律 `no_paste=True` —— 几小时后突然往当前焦点打字，
         是比少打一次严重得多的事故。
      2. 只删「可再生成的中间文件」，原始录音要么转写、要么隔离，不删。
    """
    stats = {"requeued": 0, "review": 0, "adopted": 0,
             "quarantined": 0, "cleaned": 0}
    seen = set()

    for p in glob.glob(os.path.join(JOB_DIR, "*.json")):
        cid = clip_of(p)
        try:
            with open(p, encoding="utf-8") as f:   # 必须用 with：
                st = json.load(f)                # 读失败时也要先关句柄，
            if not isinstance(st, dict):         # 否则后面 os.replace 会 WinError 32
                raise ValueError("not an object")
        except (OSError, ValueError):
            dst = os.path.join(QUARANTINE, os.path.basename(p) + "."
                               + time.strftime("%Y%m%d_%H%M%S") + ".corrupt")
            try:
                os.replace(p, dst)
                stats["quarantined"] += 1
                log("任务记录损坏，已隔离留档：%s" % os.path.basename(p))
            except OSError as e:
                log("损坏的任务记录挪不动（%s）：%s" % (os.path.basename(p), e))
            continue

        seen.add(cid)
        state = st.get("state")
        ps = st.get("paste_state")
        f = st.get("file") or ""
        ap = os.path.join(INBOX, f) if f else ""
        alive = bool(ap) and os.path.exists(ap)

        if ps in ("PASTE_PENDING", "PASTING"):
            job_write(cid, state="done", paste_state="NEEDS_REVIEW",
                      reason="crash_during_paste", chars=int(st.get("chars") or 0))
            stats["review"] += 1
        elif state in ("stored", "queued", "transcribing"):
            if alive:
                job_write(cid, state="queued", no_paste=True, recovered=True,
                          reason="crash_before_transcribe")
                jobs.put(ap)
                stats["requeued"] += 1
            else:
                job_write(cid, state="needs_review", no_paste=True,
                          reason="crash_before_transcribe")
                stats["review"] += 1
        elif state == "transcribed":
            # 转写完了、还没进输入就崩：不知道打没打，绝不重打
            job_write(cid, state="done", paste_state="NEEDS_REVIEW",
                      reason="crash_after_transcribe", chars=int(st.get("chars") or 0))
            stats["review"] += 1
        elif state in ("done", "empty") and alive:
            try:
                os.remove(ap)
                stats["cleaned"] += 1
            except OSError:
                pass

    # 孤儿音频：任务记录没了，但录音还在
    for p in glob.glob(os.path.join(INBOX, "*")):
        b = os.path.basename(p)
        if _sidecar(p):
            try:
                os.remove(p)
                stats["cleaned"] += 1
            except OSError:
                pass
            continue
        stem, ext = os.path.splitext(b)
        if ext.lower() not in (".m4a", ".wav", ".mp3", ".ogg", ".flac"):
            continue
        if stem in seen:
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if not os.access(p, os.R_OK):
            # 读不到通常是权限问题（不是文件坏了）。不要去隔离 —— 那也会失败，
            # 只会把「文件还在」变成一串没用的报错。
            log("音频读不到（权限？），跳过不动：%s" % b)
            continue
        if size < 1024 or not looks_like_audio(p):
            if _quarantine(p, "orphan_not_audio"):
                stats["quarantined"] += 1
            continue
        job_write(stem, state="queued", file=b, sha256=sha256_of(p),
                  t=os.path.getmtime(p), no_paste=True, recovered=True,
                  reason="orphan_audio")
        jobs.put(p)
        stats["adopted"] += 1

    if any(stats.values()):
        log("对账结果：重新转写 %(requeued)d｜孤儿认领 %(adopted)d｜"
            "待核查 %(review)d｜隔离 %(quarantined)d｜清理中间文件 %(cleaned)d" % stats)
    return stats


def cleanup_old():
    """只清确定已完成/失败的任务记录，绝不碰还在排队的。

    失败音频默认**永不自动删除**（FAILED_KEEP_DAYS=0），跟文档里
    「失败录音绝不删除」保持一致；要开自动清理再显式设天数。
    """
    now = time.time()
    for p in glob.glob(os.path.join(JOB_DIR, "*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            continue
        t = float(st.get("t") or 0)
        if st.get("state") in ("done", "empty", "needs_review") and t and \
                now - t > JOB_KEEP_DAYS * 86400:
            try:
                os.remove(p)
            except OSError:
                pass
    if FAILED_KEEP_DAYS <= 0:
        return
    for p in glob.glob(os.path.join(FAILED, "*")):
        try:
            if now - os.path.getmtime(p) > FAILED_KEEP_DAYS * 86400:
                os.remove(p)
        except OSError:
            pass

def transcribe_ex(path):
    p = provider()
    if p == "none":
        raise RuntimeError("没有配置云端 key（pc/groq_key.txt）")
    wav = to_wav(path)
    try:
        trim_silence(wav)
        if is_silent(wav):
            log("音频几乎无声，跳过识别")
            return "", ""
        normalize_wav(wav)
        if p == "groq":
            raw = transcribe_full(wav)
        else:
            raw, _ms = asr.transcribe_wav(wav)
        fixed = terms.fix(raw)          # 专有名词纠错（LSPosed / Thanox / priv-app …）
        polished = polish_text(fixed)
        # Groq 对中文常输出半角标点（`,` `?`），而润色只「补标点、不改写」，
        # 看到已有逗号就不动。这里再加一层确定性本地化，不依赖模型。
        return raw, terms.fix(punct.fix(polished))
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def transcribe(path):
    return transcribe_ex(path)[1]


# ───────────────────────── 任务记录（第二批加固） ─────────────────────────
JOB_DIR = os.path.join(DATA, "jobs")
os.makedirs(JOB_DIR, exist_ok=True)


def job_path(cid):
    return os.path.join(JOB_DIR, cid + ".json")


def job_read(cid):
    try:
        with open(job_path(cid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def job_write(cid, **kw):
    """合并写入，不要整份覆盖 —— 否则 sha256 之类的字段会被后续状态更新冲掉。"""
    cur = job_read(cid) or {}
    cur.update(kw)
    cur["clip_id"] = cid
    tmp = job_path(cid) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())      # 「任务记录已写完」必须真的落盘，否则崩溃后无从判断
        os.replace(tmp, job_path(cid))
    except OSError:
        pass


def clip_of(path):
    return os.path.splitext(os.path.basename(path))[0]


# ───────────────────── 自动输入前的判定 ─────────────────────
# 不再用「窗口类名」当判据：Chrome_RenderWidgetHostHWND 是整块渲染区、
# CoreWindow 是整个 UWP 窗口、普通 Edit 也可能是密码框。
# 现在问 UI Automation 真正的焦点控件（规则见 inputguard.py）。
def do_paste(cid, text):
    """独立的输入状态机。转写成功只是 TRANSCRIBED，能不能输入是另一回事。"""
    g = inputguard.verdict()
    exe = g.get("exe") or ""
    if not g["allow"]:
        clipped = set_clipboard(text) if CLIP_ON_REFUSE else False
        job_write(cid, state="done", paste_state="NEEDS_REVIEW",
                  reason=g["code"], guard=g["source"], guard_detail=g["detail"],
                  chars=0, clipboard=clipped, target=exe, text=text, t=time.time())
        log("未自动输入（%s｜%s｜%s）%s"
            % (g["code"], exe or "?", g["detail"],
               "，已放剪贴板" if clipped else "，未动剪贴板（内容只在任务记录里）"))
        return

    job_write(cid, state="pasting", paste_state="PASTING",
              guard=g["source"], target=exe, text=text, t=time.time())
    sent, why = send_text_checked(text, g["hwnd"], cid)
    if why is None:
        job_write(cid, state="done", paste_state="PASTED",
                  chars=sent, guard=g["source"], target=exe, text=text, t=time.time())
        log("已直接输入 %d 字（%s｜%s）" % (sent, exe or "?", g["source"]))
    else:
        # 中途换窗口 / 注入失败 / 只打了一半：一律不重打，标人工核查
        job_write(cid, state="done", paste_state="NEEDS_REVIEW",
                  reason=why, chars=sent, guard=g["source"], target=exe,
                  text=text, t=time.time())
        log("输入未完成（%s，已输 %d/%d 字），已标人工核查" % (why, sent, len(text)))


def worker():
    log("识别后端：%s" % {"groq": "Groq " + asr_cloud.GROQ_MODEL + "（云端）",
                          "deepseek": "DeepSeek ASR（云端）",
                          "none": "未配置（不会用本地模型）"}[provider()])
    log("AI 整理：%s" % ("开" if POLISH else "关"))
    while True:
        path = jobs.get()
        keep = False
        try:
            t0 = time.time()
            raw, text = transcribe_ex(path)
            cost = time.time() - t0
            cid = clip_of(path)
            if not text:
                job_write(cid, state="empty", raw=raw, t=time.time())
                log("(无内容) %s  用时 %.1fs" % (cid, cost))
                continue
            if LOG_FULL:
                log("转写完成 %.1fs（%s）：%s" % (cost, cid, text))
            else:
                # 默认只记 ID / 字数 / 耗时。日志会被备份、会被翻，
                # 转写正文不该出现在这里。
                log("转写完成 %.1fs：%d 字（%s）" % (cost, len(text), cid))
            if not append_log(text):
                log("听写归档写不进去（三处都失败），内容只在任务记录里（%s）" % cid)
            # 转写完成 ≠ 已经输入，两个状态分开记
            job_write(cid, state="transcribed", raw=raw, text=text, t=time.time())
            if AUTO_PASTE and os.environ.get("VK_INPUT", "type") == "type":
                cur = job_read(cid) or {}
                if cur.get("no_paste"):
                    job_write(cid, state="done", paste_state="NEEDS_REVIEW",
                              reason="recovered_after_crash", chars=0,
                              text=text, t=time.time())
                    log("崩溃恢复的任务不自动输入，内容在任务记录里（%s）" % cid)
                else:
                    do_paste(cid, text)
            else:
                ok = set_clipboard(text)
                job_write(cid, state="done", paste_state="CLIPBOARD",
                          raw=raw, text=text, t=time.time())
                log("剪贴板: %s" % ("OK" if ok else "失败"))
                if AUTO_PASTE:
                    time.sleep(0.15)
                    send_ctrl_v()
                    log("已发送 Ctrl+V")
        except Exception as e:
            # 转写失败就把录音留下，别把用户唯一的一份录没了
            log("处理失败: %r" % (e,))
            job_write(clip_of(path), state="failed", t=time.time())
            keep = True
        finally:
            if keep:
                try:
                    dst = os.path.join(FAILED, os.path.basename(path))
                    os.replace(path, dst)
                    log("录音已留底：failed/%s" % os.path.basename(dst))
                except OSError:
                    pass
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True

    def _authed(self):
        """校验令牌；不通过就 401 并返回 False。调用方必须先判断再读 body。"""
        ip = self.client_address[0] if self.client_address else "?"
        if not rate_ok(ip):
            self.send_error(429, "too many requests")
            return False
        got = self.headers.get("Authorization", "") or ""
        want = "Bearer " + TOKEN
        if not hmac.compare_digest(got, want):
            log("拒绝未授权请求：%s %s（来自 %s，%s）"
                % (self.command, self.path, ip, "有令牌但不对" if got else "没带令牌"))
            self.send_error(401, "unauthorized")
            return False
        return True

    def do_POST(self):
        """上传逻辑整体搬到 uploader.py；这里只留鉴权和异常兜底。"""
        try:
            if not self._authed():
                return
            import uploader
            uploader.handle_post(self)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as e:
            log("接收失败: %r" % (e,))
            try:
                self.send_error(500)
            except Exception:
                pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/jobs/"):
            cid = u.path[len("/jobs/"):]
            if not clip_ok(cid):          # 别拿这个接口当任意文件读
                self.send_error(400, "invalid clip id")
                return
            st = job_read(cid) or {"state": "unknown"}
            st["clip_id"] = cid
            body = json.dumps(st, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path in ("/", "/ping"):
            # 健康检查：只回一句标识，不含任何业务数据。
            # 注意必须保留 "inkvoice" 这个词 —— 手机靠它认人（局域网发现 + 连上没）
            body = b"inkvoice ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._authed():
            return
        if u.path == "/hotwords/version":
            text, _ = asr_cloud.hotwords_snapshot()
            body = json.dumps({"v": asr_cloud.hotwords_hash(text)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/hotwords":
            text, mtime = asr_cloud.hotwords_snapshot()
            body = json.dumps({"text": text, "mtime": mtime,
                               "v": asr_cloud.hotwords_hash(text)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = ("inkvoice ok | backend=" + provider()).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        src = sys.argv[2]
        log("测试转写：%s" % src)
        t0 = time.time()
        text = transcribe(src)
        log("结果（%.1fs）：%s" % (time.time() - t0, text or "(空)"))
        return

    for name, act in vault.scrub_legacy():
        log("明文凭据处理：%s → %s" % (name, act))
    asr_cloud.migrate_keys()
    gw = inputguard.warmup()
    log("输入判定：UIA=%s MSAA=%s%s"
        % (gw["uia"], gw["msaa"],
           "" if gw["uia"] else "（UIA 不可用 → 自动输入会全部拒绝，检查 comtypes）"))
    reconcile_jobs()
    cleanup_old()
    threading.Thread(target=worker, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        # 已经有一个实例在跑了，静默退出，避免两个进程抢同一个端口
        return
    # 有证书就跑 HTTPS（手机端做证书指纹固定），没有就还是 HTTP
    cert = os.path.join(DATA, "cert.pem")
    keyfile = os.path.join(DATA, "key.pem")
    if os.path.exists(cert) and os.path.exists(keyfile):
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, keyfile)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            log("已启用 HTTPS（手机端固定证书指纹）")
        except Exception as e:
            # 有证书却加载失败 = 配置坏了。这时候**不能**静默退回明文：
            # 手机端做了证书指纹固定，表面看只是「连不上」，但同一局域网里
            # 音频和转写结果已经变成明文可抓。宁可起不来，也不降级。
            log("HTTPS 证书加载失败：%r" % (e,))
            if os.environ.get("VK_TLS_FALLBACK", "0") == "1":
                log("已设 VK_TLS_FALLBACK=1，按旧行为退回 HTTP（不建议长期开着）")
            else:
                log("拒绝以明文 HTTP 启动（要临时降级请显式设 VK_TLS_FALLBACK=1）")
                return
    log("监听 0.0.0.0:%d  自动粘贴=%s" % (PORT, AUTO_PASTE))
    srv.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
