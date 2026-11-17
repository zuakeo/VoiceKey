# 维护手册

给「半年后的自己」看的文档。目标：任何一个环节坏了，都能在 3 分钟内定位到是哪一段。

---

## 一、先记住这四条链路

整个系统只有四个可能断的地方，按顺序查，不要跳：

```
①  按键 → 录音        手机端 KeyService
②  录音 → 上传        手机网络 / 电脑端口
③  上传 → 文字        云端 ASR / 纠错
④  文字 → 光标        剪贴板 + Ctrl+V
```

### 每条链路怎么验证

| 环节 | 一条命令看出有没有通 |
|---|---|
| ① 录音 | `adb shell su -c 'ls -lt /sdcard/Android/data/<包名>/files/ \| head -3'` |
| ② 上传 | `tail -5 pc/out/receiver.log` 有没有「收到 xxx.m4a」 |
| ③ 识别 | 日志里有没有「转写完成 N.Ns：…」 |
| ④ 粘贴 | 日志里有没有「剪贴板: OK」「已发送 Ctrl+V」 |

**看到「收到 xxx.m4a」但没「转写完成」** → 卡在 ③（判静音了，或云端报错）。
**看到「转写完成」但光标没出字** → 卡在 ④（窗口不接受注入，或焦点跑了）。

---

## 二、日常检查（30 秒）

```bash
# 电脑端活着吗
netstat -ano | findstr :8765

# 手机端活着吗
adb shell su -c 'pidof com.zuakeo.voicekey'

# 守护在吗（Magisk 那层）
adb shell su -c 'ps -A | grep service.sh'
```

三个都在，就别动它。三个里少一个，往下看。

---

## 三、常见故障对照表

| 现象 | 最可能的原因 | 处理 |
|---|---|---|
| 按键完全没反应 | 进程死了 / 键码不对 | 看第四节 |
| 手机震了但电脑没字 | WiFi 断了 / 电脑端口没起 | 看第五节 |
| 电脑收到文件但没出字 | 判成静音，或粘贴失败 | 看第六节 |
| 识别出来是乱码 | 离麦克风太远 | 对着**底部**说话（见第七节） |
| 长句尾巴丢了 | 超 30 秒没切段 | 看第八节 |
| 偶尔蹦出「谢谢观看」 | 静音幻觉 | 已经有三重过滤，仍出现就提高门限 |
| 锁屏后再按没反应 | 锁屏阅读拦了 Activity | 看第九节 |

---

## 四、按键没反应

### 4.1 先确认进程

```bash
adb shell su -c 'pidof com.zuakeo.voicekey'
```

没有输出 = 进程没了。守护应该在 15 秒内拉起来，等半分钟再看：

```bash
adb shell su -c 'tail -5 /data/local/tmp/voicekeyd.log'
```

- 看到 `dead -> wake` 但进程还是没起来 → 大概率是**锁屏阅读**拦住了 Activity（第九节）
- 看到 `锁屏阅读模式中拉起` → 正常，等一下就好

### 4.2 再确认键码

```bash
adb shell su -c 'getevent -l /dev/input/event2'
```

按一下键，看有没有 `EV_KEY ... DOWN`。没有 → 按键根本不在这台设备这个节点上，换 eventN 再试。

### 4.3 手动触发一次

```bash
adb shell su -c 'sendevent /dev/input/event2 1 766 1; sendevent /dev/input/event2 0 0 0'
sleep 2
adb shell su -c 'sendevent /dev/input/event2 1 766 0; sendevent /dev/input/event2 0 0 0'
```

然后看 `pc/out/receiver.log`。能收到就说明按键链路没问题，是物理键本身的问题（接触不良/被系统占用）。

---

## 五、手机震了，电脑没字

### 5.1 排查顺序

```bash
# 1. 电脑端口
netstat -ano | findstr :8765        # 没有 → 双击 pc\start-silent.vbs

# 2. 手机 IP
adb shell ip -4 addr show wlan0 | grep inet     # 空 → WiFi 掉了

# 3. 手机能否连到电脑
adb shell su -c 'printf "GET / HTTP/1.0\r\n\r\n" | nc -w 3 <电脑IP> 8765'
# 期望：inkvoice ok | backend=groq
```

### 5.2 常见坑

- **Windows 把 WiFi 识别成「公用网络」**，防火墙拦入站 → 放行 `pythonw.exe` 的专用+公用
- **电脑 IP 变了**（DHCP）→ 路由器里给电脑做 DHCP 保留，或依赖 App 的自动找电脑功能
- **手机 WiFi 休眠断网** → `settings put global wifi_sleep_policy 2`

### 5.3 断网期间的录音不会丢

录音留在 `/sdcard/Android/data/<包名>/files/`，联网后：
- 网络恢复事件触发补推
- 30 秒兜底轮询再补一次
- 连续失败 3 次会扫描同网段找 8765

想手工清掉积压（避免补推一堆旧录音）：

```bash
adb shell su -c 'cd /sdcard/Android/data/<包名>/files && mkdir -p old && mv *.m4a old/'
```

---

## 六、收到文件但没出字

看日志最后三行，对症下药：

| 日志 | 含义 | 处理 |
|---|---|---|
| `音频几乎无声，跳过识别` | 门限判成静音 | 说话响一点，或降低 `is_silent(threshold=)` |
| `(无内容)` | 云端返回空（被幻觉过滤掉了） | 同上 |
| `剪贴板: OK` 但无 `已发送 Ctrl+V` | `VK_AUTOPASTE=0` | 改环境变量 |
| `已发送 Ctrl+V` 但没字 | 目标窗口不吃注入 | 换窗口试；管理员权限窗口收不到 |

---

## 七、识别不准怎么办

**先看是不是收音问题**，日志里会打印峰值：

```
音量放大 5.0x（原峰值 5229）     ← 峰值 < 6000，说明太小声
音量足够，未放大（峰值 27451）   ← 理想
```

峰值低的时候，**先调姿势**：麦克风在机身**底部**（Type-C 那一侧），把底边对着嘴，峰值能差 10 倍以上。

姿势没问题还错，按这个顺序处理：

1. **同音字/专有名词** → 加到 `pc/terms.py` 的 `CN_FIX`（中文整串替换）或 `RULES`（正则）
2. **行业词没识别对** → 加到 `pc/hotwords.txt`（影响 whisper 的 prompt）
3. **整体不准** → 换模型：`VK_GROQ_MODEL=whisper-large-v3`（默认已是）
4. **英文词被音译** → 只能靠纠错表，模型换再大也一样

改完 `terms.py` 要重启电脑端；改 `hotwords.txt` 立即生效。

---

## 八、长录音

- 手机端上限 3 分钟（`KeyService` 的 `setMaxDuration`）
- 电脑端超过 28 秒自动切段，切点找最安静的 100ms
- 日志出现 `长录音切成 N 段识别` 就是走了这条路

**但建议单次 30 秒内**：短句 2~4 秒返回，60 秒的音频要 17 秒，体验差一截。

---

## 九、锁屏阅读 / 省电模式把应用拦住

现象：`am start` 返回 `error code 102`，守护反复 `dead -> wake` 但拉不起来。

查：

```bash
adb shell settings get system locked_read_enable
adb shell settings get system reading_mode_enabled
```

是 1 就说明被拦。守护脚本已经会自动处理（临时关掉 → 拉起 → 恢复），手工应急：

```bash
adb shell su -c 'settings put system locked_read_enable 0; am start -n <包名>/.WakeActivity'
adb shell su -c 'settings put system locked_read_enable 1'
```

---

## 十、改 APK（唯一需要重启的改动）

```bash
# 1. 编译
bash build.sh

# 2. 推上去（不要用 adb install -r，persistent 包不允许）
adb push dist/VoiceKey-X.apk /data/local/tmp/v.apk
adb shell su -c 'cp -f /data/local/tmp/v.apk /data/adb/modules/voicekey_mokey/system/priv-app/VoiceKey/VoiceKey.apk'
adb shell su -c 'chmod 644 /data/adb/modules/voicekey_mokey/system/priv-app/VoiceKey/VoiceKey.apk'

# 3. 重启
adb reboot
```

**千万不要先删旧 APK 再拷新的**：中间态是「包还在、APK 没了」，开机会卡住。

### 改完了怎么验证真的生效

```bash
adb shell su -c 'md5sum /system/priv-app/VoiceKey/VoiceKey.apk'   # 和本地 dist 对比
```

注意：部分 ROM 不重扫 `priv-app` 的 APK，`dumpsys package` 里的 versionCode 可能一直显示旧值，**以 md5 为准**。

---

## 十一、回滚

所有改动前都有备份，放在 `D:\dsh\backup\`，条目名 `YYYYMMDD_HHMMSS_说明`。

```bash
# 回滚模块
adb shell su -c 'cp /path/to/backup/service.sh /data/adb/modules/voicekey_mokey/'
adb reboot

# 彻底卸载（恢复原厂）
adb shell su -c 'touch /data/adb/modules/voicekey_mokey/disable'
adb reboot
```

---

## 十二、每月可以做的事

- 删 `pc/out/` 里的旧日志（`.md` 和 `receiver.log`）
- 删设备上的 `files/sent/` 和 `files/old/`
- 检查 Groq 额度是否够用
- 更新 `pc/hotwords.txt`：把最近识别错的词补进去
