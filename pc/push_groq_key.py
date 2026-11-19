# -*- coding: utf-8 -*-
"""把电脑端保存的 Key 同步到手机（明文写入 prefs，App 首次读取时自动加密）。

第三轮评审点出的问题：上一版会把明文写进本地 `_gk.xml`，再 `adb push` 到
`/sdcard`，最后才删 —— 中间有好几秒 Key 躺在本机磁盘和共享存储上，
删还是普通删除。现在改成：

    内存里拼好 XML → 通过 stdin 直接写进 /data/local/tmp（umask 077）
    → 校验哈希一致 → cp 到位并修权限/SELinux → 删掉临时文件

命令行参数里不出现 Key，本地磁盘上不出现明文文件。
"""
import hashlib
import io
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import vault  # noqa: E402

PKG = "com.zuakeo.inknote"
PREFS = "/data/data/%s/shared_prefs/ink.xml" % PKG
TMP = "/data/local/tmp/_vk_ink.xml"


def su(cmd, data=None):
    """data 不为 None 时通过 stdin 喂给远端命令，避免出现在命令行里。"""
    return subprocess.run(["adb", "shell", "su -c '%s'" % cmd],
                          input=data, capture_output=True, text=True)


def xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def main():
    key = vault.load("groq_key", "GROQ_API_KEY")
    if not key:
        print("电脑端也读不到 Key，先检查配置")
        return 1
    xml = su("cat %s" % PREFS).stdout
    if "<map" not in xml:
        print("手机上还没有配置文件，先装一次 App")
        return 1

    tag = '<string name="groq">%s</string>' % xml_escape(key)
    if 'name="groq"' in xml:
        xml = re.sub(r'<string name="groq">.*?</string>', lambda m: tag, xml, flags=re.S)
    else:
        xml = xml.replace("</map>", "    " + tag + "\n</map>")

    # 1) 写进 /data/local/tmp（root 目录，临时文件先 umask 077）
    r = su("umask 077; cat > %s" % TMP, data=xml.encode("utf-8"))
    if r.returncode != 0:
        print("写入临时文件失败：", (r.stderr or "").strip()[:120])
        return 1

    # 2) 校验：手机上算 sha256，跟本地比（Key 不进命令行、不进日志）
    want = hashlib.sha256(xml.encode("utf-8")).hexdigest()
    got = su("sha256sum %s" % TMP).stdout.strip().split(" ")[0]
    if got != want:
        su("rm -f %s" % TMP)
        print("传输校验不一致，已放弃本次写入")
        return 1

    # 3) 原子替换：先写 .new，再 mv，避免 App 读到半截配置
    uid = su("stat -c %%u /data/data/%s" % PKG).stdout.strip()
    if not uid.isdigit():
        su("rm -f %s" % TMP)
        print("拿不到 App uid，中止（不猜）")
        return 1
    su("cp %s %s.new && chown %s:%s %s.new && chmod 660 %s.new && "
       "restorecon %s.new && mv %s.new %s; rm -f %s"
       % (TMP, PREFS, uid, uid, PREFS, PREFS, PREFS, PREFS, PREFS, TMP))

    # 4) 复核：文件里确实有 groq 字段，长度对得上
    back = su("grep -o 'name=\"groq\">[^<]*' %s" % PREFS).stdout
    tail = back.split(">")[-1].strip() if ">" in back else ""
    ok = bool(tail) and len(tail) == len(key)
    print("已写入手机（明文），App 下次读取会自动加密 | 长度校验：%s" % ("通过" if ok else "待核实"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
