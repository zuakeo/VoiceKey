"""云端语音识别（OpenAI 兼容 /audio/transcriptions）

当前支持 Groq（whisper-large-v3-turbo），与你 SayIt 用的是同一个模型。
Key 由用户自己放在同目录 groq_key.txt（一行），程序只读不打印。

注意：Groq 前面挂了 Cloudflare，会按 UA 拦掉 urllib 的默认头（403 error code 1010），
这里必须伪装成浏览器 UA，踩过一次坑。

为什么不用 SayIt 的接口：实测 sayit.exe 没有任何监听端口，它是 Tauri + WebView2，
命令只走 webview 内部 IPC，外部进程调不到；它的 key 存在自己的配置里，按规定不读。
所以这里直连同一个云模型，SayIt 怎么更新都不影响。
"""
import json
import time
import os
import uuid
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = os.environ.get("VK_GROQ_MODEL", "whisper-large-v3")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 代码里不放任何个人/行业词；行业词只写在 hotwords.txt（不进仓库）
DEFAULT_PROMPT = "以下是简体中文口语听写，请加自然标点。"


def _key(filename, envname):
    v = os.environ.get(envname, "").strip()
    if v:
        return v
    p = os.path.join(HERE, filename)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


def groq_key():
    return _key("groq_key.txt", "GROQ_API_KEY")


def available():
    return bool(groq_key())


def prompt_text():
    """热词文件优先，一行一个词，拼成 whisper 的 prompt。"""
    p = os.path.join(HERE, "hotwords.txt")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                words = []
                # 先按行去掉注释，再拆顿号 —— 顺序反了会把注释里的顿号当成词
                for line in f.read().splitlines():
                    if line.strip().startswith("#"):
                        continue
                    for w in line.replace("、", "\n").splitlines():
                        w = w.strip()
                        if w:
                            words.append(w)
            if words:
                return ("以下是简体中文口语听写，加自然标点，数字和英文单词保持完整。常用词："
                        + "、".join(words) + "。")
        except OSError:
            pass
    return DEFAULT_PROMPT


def transcribe_wav(wav_path, timeout=90):
    """兼容旧调用：传入任意音频路径即可（wav/mp3/m4a）。"""
    return transcribe(wav_path, timeout)


def transcribe(audio_path, timeout=90):
    key = groq_key()
    if not key:
        raise RuntimeError("未配置 groq_key.txt")

    boundary = "----VK" + uuid.uuid4().hex
    with open(audio_path, "rb") as f:
        audio = f.read()

    parts = []
    for k, v in (
        ("model", GROQ_MODEL),
        ("language", "zh"),
        ("response_format", "verbose_json"),
        ("temperature", "0"),
        ("prompt", prompt_text()),
    ):
        parts.append(("--" + boundary + "\r\n").encode())
        parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode())
        parts.append(str(v).encode("utf-8") + b"\r\n")

    ext = audio_path.rsplit(".", 1)[-1].lower()
    if ext not in ("wav", "mp3", "m4a", "ogg", "webm", "flac"):
        ext = "wav"
    mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4",
            "ogg": "audio/ogg", "webm": "audio/webm", "flac": "audio/flac"}[ext]
    parts.append(("--" + boundary + "\r\n").encode())
    parts.append(('Content-Disposition: form-data; name="file"; filename="audio.%s"\r\n' % ext).encode())
    parts.append(("Content-Type: %s\r\n" % mime).encode())
    parts.append(b"\r\n")
    parts.append(audio)
    parts.append(b"\r\n")
    parts.append(("--" + boundary + "--\r\n").encode())

    req = urllib.request.Request(
        GROQ_URL,
        data=b"".join(parts),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    payload = None
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as e:          # RemoteDisconnected / 超时 / 连接重置
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    if payload is None:
        raise last_err if last_err else RuntimeError("识别请求失败")

    segs = payload.get("segments")
    if isinstance(segs, list) and segs:
        # 三重过滤，专治静音/噪声时 whisper 编出「作词、曲：李宗盛」「掌握到一切」这类幻觉：
        #   no_speech_prob 高 → 模型自己认为没人声
        #   avg_logprob 低   → 模型自己没把握
        #   compression_ratio 高 → 复读式幻觉
        keep = []
        for s in segs:
            if float(s.get("no_speech_prob", 0.0)) >= 0.6:
                continue
            if float(s.get("avg_logprob", 0.0)) < -1.0:
                continue
            if float(s.get("compression_ratio", 0.0)) > 2.4:
                continue
            keep.append(s)
        if not keep:
            return ""
        return "".join(s.get("text", "") for s in keep).strip()
    return (payload.get("text") or "").strip()
