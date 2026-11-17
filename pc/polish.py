"""AI 整理：只修标点和明显错别字，绝不改写句子。

用户反馈「感觉不准」，实测发现是这一步把「怎么不管用啊」改成了「怎么没用啊」——
所以这里把提示词收紧成「只补标点、只改错别字，禁止改写/同义替换/删词」。

优先用 Groq 的 chat 接口（与 SayIt 同一条链路）；没有 Groq key 时退回 DeepSeek。
Groq 走 Cloudflare，必须带浏览器 UA，否则 403 error code 1010。
"""
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("VK_POLISH_MODEL", "qwen/qwen3.8-27b")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SYSTEM_PROMPT = (
    "你是语音转写稿的标点修复工具，只做两件事：1. 补全或规范中文标点（，。？！），必要时断句；"
    "2. 把明显的错别字改成正确的同音字。"
    "禁止：改写句子、同义替换、润色、删掉重复词或口水词、增删任何信息。"
    "保持原文用词不变，只输出正文，不要解释。"
)


def _read(path):
    try:
        with open(os.path.join(HERE, path), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _groq_key():
    return os.environ.get("GROQ_API_KEY", "").strip() or _read("groq_key.txt")


def _deepseek_conf():
    """返回 (base, model, token)；缺一不可时返回 None。"""
    t = os.environ.get("DEEPSEEK_API_KEY", "").strip() or _read("deepseek_key.txt")
    if not t:
        return None
    base = os.environ.get("DEEPSEEK_BASE", "").strip() or "https://api.deepseek.com"
    model = os.environ.get("DEEPSEEK_MODEL", "").strip() or "deepseek-chat"
    return base, model, t


def available():
    return bool(_groq_key()) or _deepseek_conf() is not None


def _chat(url, model, token, text, timeout=30):
    body = json.dumps({
        "model": model,
        "temperature": 0.1,
        "max_tokens": 800,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json",
                 "User-Agent": UA,
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
    out = (out or "").strip()
    if out.startswith("“") and out.endswith("”"):
        out = out[1:-1]
    return out


def run(text):
    if len(text.strip()) < 4:
        return text
    key = _groq_key()
    if key:
        try:
            r = _chat(GROQ_CHAT_URL, GROQ_MODEL, key, text)
            if r:
                return r
        except Exception:
            pass
    conf = _deepseek_conf()
    if conf:
        base, model, token = conf
        try:
            r = _chat(base.rstrip("/") + "/chat/completions", model, token, text)
            if r:
                return r
        except Exception:
            pass
    return text
