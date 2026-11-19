# -*- coding: utf-8 -*-
"""自检脚本共用的请求助手：有证书就走 HTTPS（本机自检不校验证书）。"""
import io
import os
import ssl
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
HOST_NAME = "127.0.0.1"


def has_tls():
    return os.path.exists(os.path.join(BASE, "cert.pem"))


def base_url():
    return ("https://" if has_tls() else "http://") + HOST_NAME + ":8765"


def context():
    if not has_tls():
        return None
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def req(path, method="GET", data=None, headers=None, timeout=30):
    r = urllib.request.Request(base_url() + path, data=data, method=method,
                               headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=context()) as f:
            return f.status, f.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return type(e).__name__, b""


def token():
    p = os.path.join(BASE, "secret.txt")
    return io.open(p, encoding="utf-8").read().strip() if os.path.exists(p) else ""


def authed():
    return {"Authorization": "Bearer " + token()}
