"""语音转写的专有名词纠错表。

ASR 对 LSPosed / Thanox 这类生造词、priv-app 这种带连字符的词基本是音译，
热词只能提高概率，不能保证。这里在识别后做一次确定性的替换，改完再送去整理。

注意：不能用 \\b 做边界 —— 中文和英文之间不算单词边界，会导致规则全部失效。
统一用「前后不是字母数字」的断言。
"""
import re


def _w(body):
    return r"(?i)(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])"


def _wc(body):
    """区分大小写的版本：给 ddr 这种「大写是别的意思」的词用。"""
    return r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])"


RULES = [
    # 框架 / 工具（生造词，最容易错）
    (_w(r"ls\s*-?\s*pos(e|ed)?"), "LSPosed"),
    (_w(r"(si|tha|ta|shi|zen|the)\s*-?\s*nox"), "Thanox"),
    (_w(r"thanos|shinox|zenox"), "Thanox"),
    (_w(r"magic\s*k|magisck|magisk"), "Magisk"),
    # Android 目录 / 命令
    (_w(r"prev[\s\-]?app|priv\s+app"), "priv-app"),
    (_w(r"log\s*cat"), "logcat"),
    (_w(r"adb\s*shell"), "adb shell"),
    # 本项目
    (_w(r"voice\s*key|voicekey"), "VoiceKey"),
    (_w(r"say\s*it"), "SayIt"),
    # 常见格式词，只统一大小写
    (_w(r"excel"), "Excel"),
    (_w(r"pdf"), "PDF"),
    (_w(r"a4"), "A4"),
    (_w(r"apk"), "APK"),
    # 工程/办公常用词：修正音近错写 (2026-11-17 实测 site→Sate)
    (_w(r"sate|sight|cite"), "site"),
    # 2026-11-17 用户指定：金额单位「第纳尔」被听成 ddr
    # 代价：DDR 内存/颗粒会被误改，工地场景基本不出现，先这么定
    # 只认小写 ddr：大写 DDR 多半指内存，不能一律换掉
    (_wc(r"ddr"), "第纳尔"),
    # 普通名词统一小写（避免句中出现 Drawing / Report 这种别扭的大写）
    (_w(r"report"), "report"),
    (_w(r"email"), "email"),
    (_w(r"meeting"), "meeting"),
    (_w(r"budget"), "budget"),
    (_w(r"drawing"), "drawing"),
    (_w(r"sample"), "sample"),
    (_w(r"scan"), "scan"),
    (_w(r"invoice"), "invoice"),
    (_w(r"quotation|quatation"), "quotation"),
    # 「这玩意」被模型听成一个英文词
    (_w(r"dryer|dryer\s*one"), "这玩意"),
]

_COMPILED = [(re.compile(p), r) for p, r in RULES]

# 中文同音错字：直接整串替换（这些串本身不是正常词，误伤概率极低）
CN_FIX = [
    ("工章", "公章"),
    ("成台", "承台"),
    ("成材", "承台"),   # 用户 2026-11-17 确认：说「三桩成材」实际指「三桩承台」
    ("单装", "单桩"),   # 同上：「单装」实际指「单桩」
    # 「变成」被听成「编程」：只改带后缀的明确句式，
    # 单独一个「编程」保留（用户有时真在说编程）
    ("编程成", "变成"),
    ("编程了", "变成了"),
    ("编程什么样", "变成什么样"),
    ("主号", "轴号"),   # 2026-11-17 用户确认：图纸上的「轴号」被听成「主号」
    ("传播音", "长录音"),  # 「长录音」被听成「传播音」
    ("潮录音", "长录音"),  # 「长录音」被听成「潮录音」
    ("推得上", "对得上"),  # 2026-11-17：「对得上」被听成「推得上」
    ("台证", "台账"),   # 2026-11-17 用户指定：物资/签证台账常被听成「台证」
    ("安学分", "安学丰"),  # 2026-11-17 用户指定：人名
    ("控制的", "空着的"),  # 2026-11-17 用户指定：注意「受控的/控制的」正常语句会被改
    # 2026-11-17 用户指定（工地口述常错）
    ("交注", "浇筑"),   # 「浇筑」被听成「交注」
    ("奔车", "泵车"),
    ("打航", "打夯"),   # 马如虎报「打航」＝打夯
    ("生化图", "深化图"),  # 「深化图」被听成「生化图」，图纸/图纸深化都覆盖
    ("两板", "梁板"),   # 「梁板」被听成「两板」
    # 2026-11-18 用户指定：「属地」（属地用工劳务合同）被听成「鼠弟」
    ("鼠弟", "属地"),
]


def fix(text):
    if not text:
        return text
    out = text
    for rx, rep in _COMPILED:
        out = rx.sub(rep, out)
    for a, b in CN_FIX:
        if a != b:
            out = out.replace(a, b)
    return out
