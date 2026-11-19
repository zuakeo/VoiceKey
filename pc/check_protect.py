# -*- coding: utf-8 -*-
"""自检：本机保存的访问凭据是否已加密，程序是否仍能正常读取。"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vault  # noqa: E402
import asr_cloud  # noqa: E402


def main():
    enc = glob.glob(os.path.join(vault.HERE, "*.enc"))
    plain = glob.glob(os.path.join(vault.HERE, "*_key.txt"))
    print("有加密存储的文件：", bool(enc))
    print("还有明文文件：", bool(plain))
    val = asr_cloud.groq_key()
    print("程序读取正常：", bool(val))

    vault.save("selftest_roundtrip", "abc123")
    ok = vault.load("selftest_roundtrip") == "abc123"
    print("加密往返正常：", ok)
    for p in glob.glob(os.path.join(vault.HERE, "selftest_roundtrip.enc")):
        os.remove(p)
    return 0 if (enc and not plain and val and ok) else 1


if __name__ == "__main__":
    sys.exit(main())
