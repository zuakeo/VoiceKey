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
INBOX = os.path.join(BASE, "inbox")
LOGDIR = os.path.join(BASE, "out")
os.makedirs(INBOX, exist_ok=True)
os.makedirs(LOGDIR, exist_ok=True)

PORT = 8765
AUTO_PASTE = os.environ.get("VK_AUTOPASTE", "1") == "1"
POLISH = os.environ.get("VK_POLISH", "1") == "1"

import asr
import terms
import asr_cloud

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


def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    print(line, flush=True)
    try:
        p = os.path.join(LOGDIR, "receiver.log")
        if os.path.exists(p) and os.path.getsize(p) > 1024 * 1024:
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


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

def append_log(text):
    day = datetime.date.today().isoformat()
    with open(os.path.join(LOGDIR, day + ".md"), "a", encoding="utf-8") as f:
        f.write("- %s  %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"), text))


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


def transcribe(path):
    p = provider()
    if p == "none":
        raise RuntimeError("没有配置云端 key（pc/groq_key.txt）")
    wav = to_wav(path)
    try:
        trim_silence(wav)
        if is_silent(wav):
            log("音频几乎无声，跳过识别")
            return ""
        normalize_wav(wav)
        if p == "groq":
            text = transcribe_full(wav)
        else:
            text, _ms = asr.transcribe_wav(wav)
        text = terms.fix(text)          # 专有名词纠错（LSPosed / Thanox / priv-app …）
        text = polish_text(text)
        return terms.fix(text)          # 整理有可能改回错字，再兜一次
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def worker():
    log("识别后端：%s" % {"groq": "Groq " + asr_cloud.GROQ_MODEL + "（云端）",
                          "deepseek": "DeepSeek ASR（云端）",
                          "none": "未配置（不会用本地模型）"}[provider()])
    log("AI 整理：%s" % ("开" if POLISH else "关"))
    while True:
        path = jobs.get()
        try:
            t0 = time.time()
            text = transcribe(path)
            cost = time.time() - t0
            if not text:
                log("(无内容) %s  用时 %.1fs" % (os.path.basename(path), cost))
                continue
            log("转写完成 %.1fs：%s" % (cost, text))
            append_log(text)
            if AUTO_PASTE and os.environ.get("VK_INPUT", "clip") == "type":
                # 默认直接打字，剪贴板不动 —— 小米电脑管家就不会弹「跨屏协作」窗口
                send_text(text)
                log("已直接输入 %d 字" % len(text))
            else:
                ok = set_clipboard(text)
                log("剪贴板: %s" % ("OK" if ok else "失败"))
                if AUTO_PASTE:
                    time.sleep(0.15)
                    send_ctrl_v()
                    log("已发送 Ctrl+V")
        except Exception as e:
            log("处理失败: %r" % (e,))
        finally:
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

    def do_POST(self):
        try:
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 50 * 1024 * 1024:
                self.send_error(400, "bad length")
                return
            name = (qs.get("name", ["rec.m4a"])[0] or "rec.m4a")
            name = os.path.basename(name)
            if not (name.endswith(".m4a") or name.endswith(".wav")):
                name += ".m4a"
            path = os.path.join(INBOX, "%d_%s" % (int(time.time() * 1000), name))
            with open(path, "wb") as f:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            log("收到 %s (%d 字节)" % (name, n))
            jobs.put(path)
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            log("接收失败: %r" % (e,))
            try:
                self.send_error(500)
            except Exception:
                pass

    def do_GET(self):
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

    threading.Thread(target=worker, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        # 已经有一个实例在跑了，静默退出，避免两个进程抢同一个端口
        return
    log("监听 0.0.0.0:%d  自动粘贴=%s" % (PORT, AUTO_PASTE))
    srv.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
