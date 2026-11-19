# -*- coding: utf-8 -*-
"""punct.fix 的单元测试。全部是「不该改坏」的边界，比 happy path 更重要。

跑法：python selftest_punct.py
"""
import sys

sys.path.insert(0, ".")
import punct  # noqa: E402

CASES = [
    # (输入, 期望)
    ("今天下午三点,在二号会议室开会", "今天下午三点，在二号会议室开会"),
    ("怎么样?我去看看!", "怎么样？我去看看！"),
    ("第一点;第二点:收尾", "第一点；第二点：收尾"),
    ("好的.", "好的。"),
    ("好的. 明天继续", "好的。明天继续"),
    ("等等...再想想", "等等……再想想"),
    ("他说,可以", "他说，可以"),
    ("这里 , 有个空格", "这里，有个空格"),
    # 不该动的
    ("精度是 3.5 毫米", "精度是 3.5 毫米"),
    ("版本 v1.2.3 已发", "版本 v1.2.3 已发"),
    ("共 1,000 条", "共 1,000 条"),
    ("Hello, world.", "Hello, world."),
    ("路径 /data/adb/modules", "路径 /data/adb/modules"),
    ("LSPosed / Thanox", "LSPosed / Thanox"),
    ("123,456", "123,456"),
    ("", ""),
]


def main():
    bad = 0
    for src, want in CASES:
        got = punct.fix(src)
        ok = got == want
        if not ok:
            bad += 1
        print(("  [OK]   " if ok else "  [FAIL] ") + "%-26s → %s%s"
              % (src[:26], got, "" if ok else "   （期望 %s）" % want))
    print("\n结果：%d / %d 通过" % (len(CASES) - bad, len(CASES)))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
