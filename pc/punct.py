# -*- coding: utf-8 -*-
"""中文标点本地化：把 Whisper 输出的半角标点改成全角。

为什么要单独做一层：Groq Whisper 对中文经常输出 `,` `?` `!`，而润色那一步
（polish.py）的提示词是「只补标点、不许改写」，模型看到已经有标点就不动了 ——
结果就是「今天下午三点,在二号会议室开会」，逗号是英文的。

这一层是**确定性**的，不依赖模型：
  - 逗号：只要一侧是中文，就转全角，并吃掉两侧多余空格
  - 问号/叹号/分号/冒号：紧跟在中文后面才转
  - 句号：前面是中文、后面是中文或结尾才转（这样 3.5 / v1.2 / 1,000 不会被改坏）
  - 省略号：中文后面跟 3 个以上的点 → ……

不碰的情况：
  - 纯英文句子（`Hello, world.` 原样保留）
  - 数字、版本号、千分位
  - 代码/路径里的标点
"""
import re

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK = "[%s]" % CJK

# 中文在前或在后，逗号都转全角；顺手吃掉多余空格
_COMMA_AFTER_CJK = re.compile(r"(%s)\s*,\s*" % _CJK)
_COMMA_BEFORE_CJK = re.compile(r"\s*,\s*(%s)" % _CJK)
# 紧跟中文的 ? ! ; :
_AFTER_CJK_PUNCT = {
    "?": "？",
    "!": "！",
    ";": "；",
    ":": "：",
}
# 中文 + 英文句点（后面是中文/空白/结尾）
_DOT_AFTER_CJK = re.compile(r"(%s)\s*\.(?=\s|$|%s)" % (_CJK, _CJK))
# 中文 + 三个以上点 → 省略号
_ELLIPSIS = re.compile(r"(%s)\s*\.{3,}" % _CJK)


def fix(text):
    if not text:
        return text or ""
    s = text
    s = _ELLIPSIS.sub(r"\1……", s)
    s = _DOT_AFTER_CJK.sub(r"\1。", s)
    for en, cn in _AFTER_CJK_PUNCT.items():
        s = re.sub(r"(%s)\s*%s" % (_CJK, re.escape(en)), r"\1" + cn, s)
    s = _COMMA_AFTER_CJK.sub(r"\1，", s)
    s = _COMMA_BEFORE_CJK.sub(r"，\1", s)
    # 全角标点后面多余的半角空格（中文排版里不该有）
    s = re.sub(r"([，。？！；：])\s+(?=%s)" % _CJK, r"\1", s)
    return s
