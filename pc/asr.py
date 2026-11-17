"""DeepSeek 云端语音识别（ASR）

接口：POST https://api.deepseek.com/v1/tools/asr
- multipart/form-data，字段名 file
- 音频须为 16 位 PCM WAV 或 Ogg Opus，最长 120 秒、8 MiB
- 无语言参数，自动识别
- 返回 {"request_id": ..., "text": ..., "duration_ms": ...}
"""
import json
import os
import uuid
import urllib.request

API_URL = "https://api.deepseek.com/v1/tools/asr"
_HERE = os.path.dirname(os.path.abspath(__file__))


def get_key():
    """优先环境变量 DEEPSEEK_API_KEY，其次同目录 deepseek_key.txt"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    path = os.path.join(_HERE, "deepseek_key.txt")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


def available():
    return bool(get_key())


def transcribe_wav(wav_path, timeout=90):
    key = get_key()
    if not key:
        raise RuntimeError("未配置 DeepSeek API Key")

    boundary = "----VK" + uuid.uuid4().hex
    with open(wav_path, "rb") as f:
        audio = f.read()

    body = b"".join([
        ("--" + boundary + "\r\n").encode(),
        b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n',
        b"Content-Type: audio/wav\r\n\r\n",
        audio,
        b"\r\n",
        ("--" + boundary + "--\r\n").encode(),
    ])

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    text = (payload.get("text") or "").strip()
    return text, payload.get("duration_ms", 0)
