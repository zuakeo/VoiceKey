# -*- coding: utf-8 -*-
"""命令行探针：看一眼「现在这个前台窗口能不能自动输入」。

用法：
    python guard_probe.py            当前前台
    python guard_probe.py --hwnd 123 指定窗口（排障用）

输出是 JSON，字段含义：
    allow   允不允许自动输入
    code    判定码（text_edit / secret_field / readonly / not_text_input …）
    detail  判定到的控件类型
    source  uia / msaa / class / none（用的哪套判据）

刻意做成独立脚本而不是 inputguard.py 的 __main__ 分支：
`python -m inputguard` 在这台机器上会让 comtypes 稳定段错误，
走这个文件调用同一批函数则正常。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inputguard as G  # noqa: E402


def main():
    hwnd = 0
    if "--hwnd" in sys.argv:
        try:
            hwnd = int(sys.argv[sys.argv.index("--hwnd") + 1], 0)
        except (IndexError, ValueError):
            print("--hwnd 需要一个整数")
            return 2
    t0 = time.time()
    v = G.verdict(hwnd=hwnd)
    v["elapsed_ms"] = int((time.time() - t0) * 1000)
    v["warmup"] = G.warmup()
    v["class"] = G.class_of(G.focus_hwnd(v.get("hwnd") or hwnd) or v.get("hwnd") or 0)
    print(json.dumps(v, ensure_ascii=False, indent=2))
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
