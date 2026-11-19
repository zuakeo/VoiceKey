# VoiceKey · 按住物理键说话，电脑上直接出字

把 Android 设备上一个**闲置的物理按键**改造成全局语音输入键：按住说话，松开后文字自动出现在**电脑**当前光标处。不弹窗、不亮屏、不换输入法。

最初是为一台海信墨水屏阅读器（TOUCH Lite / HITV205N）写的，用的是侧边那颗「墨智键」。核心链路与设备无关，只要满足下面的条件，任何 Android 设备都能照搬。

---

## 它是什么样的体验

```
按住侧键 ──→ 手机静默录音（屏幕不亮）
松开     ──→ 音频走 WiFi 到电脑
            ──→ 电脑调云端 ASR 转文字
            ──→ 纠错 + 补标点
            ──→ 自动粘贴到光标处
```

全程 3~5 秒。手机上只闪一下 Toast 提示「已推送」，其余什么都不会发生。

## 适用条件

| 条件 | 说明 |
|---|---|
| **Android + root** | 需要 Magisk（用模块把 APK 放进 `priv-app` 并设成 `persistent`） |
| **一个闲置的物理按键** | 墨智键 / 小米 AI 键 / 一加三段式键 / OPPO 自定义键 / 甚至音量键（不推荐） |
| **一台常开的电脑** | Windows / macOS / Linux 都行，只跑 Python，不需要显卡 |
| **同一个局域网** | 手机和电脑在同一 WiFi；电脑可以是热点提供方 |

> 不需要：Google 服务、无障碍权限、悬浮窗权限、输入法替换。

## 工作原理

```
┌─────────────┐   raw event    ┌──────────────┐
│ /dev/input/ │───(su cat)────▶│  KeyService  │
│  eventN     │                │  (常驻前台)   │
└─────────────┘                └──────┬───────┘
                                      │ 按下开录 / 松手停
                                      ▼
                               ┌──────────────┐
                               │ MediaRecorder│  AAC 44.1k mono
                               └──────┬───────┘
                                      │ HTTP POST
                                      ▼
                       ┌──────────────────────────────┐
                       │  PC: inkvoice.py :8765       │
                       │ 转码→裁静音→判静音→归一化      │
                       │ 再压成 24kbps mp3 上传         │
                       └────────────┬─────────────────┘
                                    │
                     ┌──────────────┴───────────────┐
                     ▼                              ▼
            ┌─────────────────┐          ┌──────────────────┐
            │ Groq whisper-v3 │          │ terms.py 纠错表    │
            │ （或 DeepSeek）  │          │ polish.py 补标点   │
            └─────────────────┘          └────────┬─────────┘
                                                  ▼
                                        剪贴板 + 模拟 Ctrl+V
```

三个设计取舍，都是被具体问题逼出来的：

1. **按键直接读 `/dev/input/eventN`**，不走 Android 输入框架 —— 厂商改键、系统拦截都管不到。
2. **进程必须「到过前台」才能用麦克风**（Android 11 起），所以有一个 1×1 透明 Activity 在关键时刻把进程顶上去一次；之后就不再打扰。
3. **识别放在电脑上** —— 手机零算力消耗、零模型下载，换模型只改电脑端一行。

## 目录结构

```
VoiceKey/
├── src/                        Android 应用源码（无 Gradle）
│   ├── AndroidManifest.xml
│   ├── java/com/zuakeo/voicekey/
│   │   ├── KeyService.java     核心：读按键、录音、上传、断网重推
│   │   ├── WakeActivity.java   1×1 透明壳，只为拿到麦克风权限
│   │   ├── BootReceiver.java
│   │   └── MainActivity.java   设置界面（服务器地址、键码）
│   └── res/
├── build.sh                    aapt2 + javac + d8 + apksigner 一条龙
├── tools/inject.py             把 dex 塞进 APK 的小工具
├── magisk/                     Magisk 模块：把 APK 变成系统常驻应用 + 守护脚本
├── pc/                         电脑端
│   ├── inkvoice.py             HTTP 收件 + 全流程编排
│   ├── uploader.py             上传接口：请求级保护、clip_id 语义、幂等
│   ├── inputguard.py           自动输入前的焦点控件判定（UIA + MSAA）
│   ├── asr_cloud.py            Groq / DeepSeek 云端识别
│   ├── polish.py               只补标点的 AI 整理
│   ├── punct.py                中文标点本地化（半角 → 全角）
│   ├── terms.py                专有名词 / 同音字纠错表
│   ├── vault.py                凭据加密（DPAPI）
│   ├── hotwords.txt            （自建，可选）# 分组注释，一行一个词
│   ├── supervisor.py           守护：8765 挂了自动重启
│   ├── start-silent.vbs        静默启动（Windows 开机自启用）
│   ├── audit.py                51 项自检
│   └── selftest_*.py           上传保护 / 输入判定 / 崩溃恢复 / 端到端
└── MAINTENANCE.md              维护手册（排查、改配置、升级）
```

## 快速开始

### 一、电脑端

```bash
cd pc
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 填上你自己的 API key（Groq 免费额度足够日常用）
echo "gsk_你的key" > groq_key.txt

python inkvoice.py              # 前台跑，按 Ctrl+C 停
```

看到 `监听 0.0.0.0:8765` 就说明在等活了。

长期使用（Windows）：

```
1. 双击 pc\start-silent.vbs          → 静默启动守护 + 服务
2. Win+R → shell:startup             → 把 start-silent.vbs 的快捷方式丢进去
```

### 二、设备端

**1. 找出你那个键的扫描码**

```bash
adb shell su -c 'getevent -l'
# 然后按一下那个键，看输出里的 eventN 和 code
# 例如：/dev/input/event2: EV_KEY KEY_AI DOWN  → 记下 event2 和 766
```

**2. 改两个地方**

| 位置 | 改什么 |
|---|---|
| `src/java/.../KeyService.java` | `CODE_MOKEY = 766` 改成你的 code；`EV_PATH` 改成你的 eventN |
| `pc/inkvoice.py` | 端口默认 8765，不用改 |

**3. 编译**

```bash
export ANDROID_SDK_ROOT=/path/to/sdk
export BUILD_TOOLS=34.0.0
export JAVA_HOME=/path/to/jdk17
export DEBUG_KEYSTORE=~/.android/debug.keystore
bash build.sh
```

**4. 装成系统应用（关键）**

```bash
# 模块结构：magisk/system/priv-app/VoiceKey/VoiceKey.apk
unzip -o dist/VoiceKey-*.apk -d /tmp/vk          # 不需要，直接放 APK 即可
adb push dist/VoiceKey-0.13.apk /sdcard/
adb shell su -c 'mkdir -p /data/adb/modules/voicekey_mokey/system/priv-app/VoiceKey'
adb shell su -c 'cp /sdcard/VoiceKey-0.13.apk /data/adb/modules/voicekey_mokey/system/priv-app/VoiceKey/VoiceKey.apk'
adb shell su -c 'cp /sdcard/service.sh /data/adb/modules/voicekey_mokey/ && chmod 755 /data/adb/modules/voicekey_mokey/service.sh'
adb reboot
```

**5. 打开应用填地址**

```
服务器地址：http://<电脑IP>:8765
```

电脑 IP 用 `ipconfig`（Windows）/ `ifconfig`（macOS/Linux）查，手机和电脑必须在同一网段。

## 配置说明

| 想改什么 | 改哪里 | 要重启吗 |
|---|---|---|
| 热词 | 自建 `pc/hotwords.txt`：一行一个词，支持 `#` 注释分组（不建也行） | 不用 |
| 错字纠正 | `pc/terms.py` 里的 `CN_FIX` / `RULES` | 重启电脑端服务 |
| 识别模型 | 环境变量 `VK_GROQ_MODEL`（默认 `whisper-large-v3`） | 重启服务 |
| 关掉 AI 补标点 | `VK_POLISH=0` | 重启服务 |
| 只进剪贴板不粘贴 | `VK_AUTOPASTE=0` | 重启服务 |
| 静音判定灵敏度 | `inkvoice.py` 的 `is_silent(threshold=500)` | 重启服务 |
| 录音码率/采样率 | `KeyService.java` 的 `setAudioSamplingRate` / `setAudioEncodingBitRate` | 重编译 |

## 踩过的坑（按痛的程度排序）

1. **麦克风只给「到过前台」的进程**。纯后台拉起的进程 `setAudioSource` 直接抛异常；用 root 的 `app_process` 录音能录但全是静音（uid 0 没有录音身份）。正解是一个 1×1 全透明、不可触摸的 Activity，存在 1.2 秒即退出。
2. **`persistent="true"` 的包不能用 `adb install -r` 更新**（`Persistent apps are not updateable`），必须换模块里的 APK 再重启。
3. **部分 ROM 的 PackageManager 不重扫 `/system/priv-app` 的 APK 变更**：代码会更新（dex 从当前文件加载），但 manifest（版本号、新权限）不会 —— 改权限只能卸载重装。
4. **锁屏阅读类功能会拦截一切 Activity 启动**（`am start` 返回 `error code 102`），连带把「点亮麦克风」的路堵死；需要在拉起前临时关掉相关 setting，拉起来后恢复。
5. **Whisper 是 30 秒窗口**：整段 60 秒送上去只返回前 30 秒。要长录音必须自己切段（本项目在 28 秒处按「最安静的 100ms」切）。
6. **静音会诱发幻觉**：「作词、曲：李宗盛」「谢谢观看」都是模型在没人声时编的。三重过滤（`no_speech_prob` / `avg_logprob` / `compression_ratio`）+ 能量门限才压得住。
7. **不能整段求平均能量判静音**：5 秒里只有 0.5 秒说话时会被摊薄误杀，要按 100ms 窗口取最大值。
8. **AI 整理会擅自改词**（把「不管用」改成「没用」），提示词必须收紧成「只补标点」。
9. **Groq 走 Cloudflare**，不带浏览器 UA 直接 `403 error code 1010`。
10. **剪贴板变化会触发第三方同步工具弹窗**（如小米电脑管家的跨屏协作），要么关掉它，要么改成直接注入字符。
11. **`\b` 在中文里不是单词边界**，中文夹英文的正则纠错全部失效，要用「前后不是字母数字」的断言。
12. **守护脚本别用 `sleep 2` 死循环**：应用空闲时 CPU 是 0，2 秒一次 fork 纯浪费，15 秒足够。
13. **`pythonw` 调 ffmpeg 会闪黑窗**：GUI 进程的子进程默认新开一个控制台，每个 `subprocess` 都要带 `creationflags=CREATE_NO_WINDOW`，否则每次说话屏幕上都闪一下。
14. **上传体积直接决定体感速度**：16k wav 15 秒要 463KB，压成 24kbps mp3 只有 43KB，识别结果一字不差；慢网络上能省好几秒。
15. **Groq 偶发 `RemoteDisconnected` / 429**：必须重试（本项目重试 3 次、退避 1.5s），否则用户看到的是「这次没出字」，会误以为按键坏了。
16. **返回码语义要和客户端对齐**：服务端「已入队」回 `202` 是对的，但客户端写的是 `code == 200`，
    于是成功被当成失败、录音被重复塞进兜底队列。要么两端都接受 2xx，要么就像本项目这样先让步回 200。
17. **`python x.py` 启动时 `import x` 会执行第二遍**：模块名是 `__main__`，`import x` 又建了一个模块对象，
    于是出现两份全局状态（这里是两份任务队列，上传进的那份没人消费）。
    入口处 `sys.modules.setdefault("x", sys.modules["__main__"])` 一行解决。
18. **`/sdcard` 的权限是 FUSE 合成的，`chmod` 无效**：root 写进去的文件固定 `root:everybody 660`，
    别的应用读不到。跨应用交付别走文件系统，走 MediaStore 或 ContentProvider。

## 没出字？按这个顺序查

```
1. 电脑端还活着吗
   Windows: netstat -ano | findstr :8765
   没有 → 双击 start-silent.vbs

2. 手机连上网了吗
   adb shell ip -4 addr show wlan0 | grep inet
   没有 → WiFi 掉了，录音会留在手机里，联网后自动补推

3. 录音有没有生成
   adb shell su -c 'ls -lt /sdcard/Android/data/com.zuakeo.voicekey/files/ | head -3'
   没有 → 按键没读到（键码不对，或进程被杀）

4. 有录音但没上传
   adb logcat -d -s VoiceKey:*   → 看是不是 ConnectException

5. 上传了但没粘贴
   看 pc/out/receiver.log 最后几行：判静音？识别为空？粘贴失败？
```

## 已知限制

- **单次录音 3 分钟上限**（建议 30 秒内，短句最快最准）
- **依赖云端**：断网时只能录不能转，恢复后自动补推
- **只对「能注入文本」的窗口有效**：以管理员身份运行的窗口、部分游戏、部分自绘界面收不到
- **首次亮屏**：进程刚起来且麦克风没点亮时，第一次按键会短暂亮屏（约 4 秒），之后不再亮

## 隐私

- 音频只发给你自己配的识别服务（默认 Groq），不经过任何中转服务器
- API key 存在本地 `pc/groq_key.txt`，不会上传
- 识别日志留在 `pc/out/`，可随时删

## 技术栈

| 环节 | 用什么 |
|---|---|
| 设备端 | Java（无 Gradle），`MediaRecorder` + root 读 evdev |
| 打包 | `aapt2` + `javac` + `d8` + `apksigner`，脚本见 `build.sh` |
| 电脑端 | Python 3.10+（`http.server` 起服务，零 Web 框架） |
| 音频处理 | ffmpeg（重采样）+ numpy（能量判定） |
| 语音识别 | **Groq `whisper-large-v3`**（主）/ DeepSeek ASR（备） |
| 文字整理 | Groq `qwen3.8-27b`（只补标点）/ DeepSeek chat（备） |
| AI 整理开关 | 环境变量 `VK_POLISH=0` 可整段关掉 |

## 开发过程

这个项目不是手写的，是**和一个 AI 编程助手一句一句磨出来的**：

- **WorkBuddy**（桌面端 Agent）负责执行：读设备日志、改代码、编译、刷模块、重启验证
- **DeepSeek V4.1** 负责推理：定位「按键为什么没反应」「麦克风为什么拿不到」「识别为什么少半句」这类因果链
- 全程约一天，从「按键能读到」到「锁屏可用、断网自动补、长录音分段」，一共刷了 13 个版本

有意思的是，**纠错表里的规则几乎全是被语音输入自己暴露出来的**：我对着它说话，它把我说的错词写在屏幕上，我再让助手把这个错法加进 `terms.py`。等于用这个工具在修这个工具。

## License

MIT，见 [LICENSE](LICENSE)。

维护与排查见 [MAINTENANCE.md](MAINTENANCE.md)。
