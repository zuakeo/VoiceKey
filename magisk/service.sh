#!/system/bin/sh
# 语音键 + 便签 守护（兜底用）。
#
# VoiceKey 本身是 /system/priv-app 的 persistent 应用，正常由 AMS 拉起；
# 便签 InkNote 是普通应用，海信会拦它开机自启，所以两个都在这里补一脚。
#
# 关于锁屏阅读：该模式下系统会拦截一切 Activity 启动（am start 返回 error 102），
# 而唤醒麦克风必须靠 1×1 透明壳这个 Activity。所以拉起前临时关掉这两个开关，
# 拉起来后立刻恢复。

VK=com.zuakeo.voicekey
INK=com.zuakeo.inknote
LOG=/data/local/tmp/voicekeyd.log

until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 1; done
sleep 8

for P in $VK $INK; do
  cmd appops set $P RUN_ANY_IN_BACKGROUND allow 2>/dev/null
  cmd appops set $P START_FOREGROUND allow 2>/dev/null
  dumpsys deviceidle whitelist +$P >/dev/null 2>&1
done

log() {
  [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 65536 ] && : > "$LOG"
  echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"
}

# 锁屏阅读开着时任何 am start 都会被拒（error 102）。返回原来的开关值，拉起后恢复。
unlock_read() {
  lr=$(settings get system locked_read_enable 2>/dev/null)
  rdm=$(settings get system reading_mode_enabled 2>/dev/null)
  if [ "$lr" = "1" ] || [ "$rdm" = "1" ]; then
    settings put system locked_read_enable 0 2>/dev/null
    settings put system reading_mode_enabled 0 2>/dev/null
    sleep 1
    echo "$lr $rdm"
  fi
}

restore_read() {
  [ -z "$1" ] && return
  lr=$(echo "$1" | cut -d' ' -f1)
  rdm=$(echo "$1" | cut -d' ' -f2)
  [ "$lr" = "1" ] && settings put system locked_read_enable 1 2>/dev/null
  [ "$rdm" = "1" ] && settings put system reading_mode_enabled 1 2>/dev/null
}

start_vk() {
  saved=$(unlock_read)
  am start -n $VK/.WakeActivity >/dev/null 2>&1
  sleep 4
  restore_read "$saved"
  [ -n "$saved" ] && log "锁屏阅读中拉起语音键（已恢复 $saved）"
}

start_ink() {
  # 便签走广播唤醒服务，不弹界面（am start-service 在这台机器上解析不了组件）
  am broadcast -a $INK.WAKE -n $INK/.BootReceiver >/dev/null 2>&1
  sleep 2
  [ -z "$(pidof $INK)" ] && {
    saved=$(unlock_read)
    am start -n $INK/.MainActivity >/dev/null 2>&1
    sleep 3
    restore_read "$saved"
    log "广播没拉起来，改用界面拉起便签"
  }
}

# 把 /sdcard/InkNote/pending 里的录音搬进记事本私有收件箱。
# 语音键在 App 内写私有目录会失败（包可见性 + FUSE 权限），只能回落这个公共目录；
# 而公共目录是 root 660，记事本读不到。这里由 root 搬运，最省事。
import_pending() {
  P=/sdcard/InkNote/pending
  [ -d "$P" ] || return
  set -- "$P"/*.m4a
  [ -e "$1" ] || return
  U=$(stat -c %u /data/data/$INK 2>/dev/null)
  [ -n "$U" ] || { log "import: 取不到便签 uid"; return; }
  D=/data/data/$INK/files/inbox
  mkdir -p "$D" 2>/dev/null
  for f in "$P"/*.m4a; do
    [ -f "$f" ] || continue
    n=$(basename "$f")
    if cp "$f" "$D/$n.tmp" 2>/dev/null && chown $U:$U "$D/$n.tmp" && chmod 600 "$D/$n.tmp" && mv -f "$D/$n.tmp" "$D/$n"; then
      [ "$(stat -c %s "$D/$n" 2>/dev/null)" = "$(stat -c %s "$f" 2>/dev/null)" ] || { rm -f "$D/$n"; log "import 大小不符，保留: $n"; continue; }
      rm -f "$f"
      log "import -> $n"
    else
      rm -f "$D/$n.tmp" 2>/dev/null
      log "import failed: $n"
    fi
  done
}

# 语音键把「电脑不可达」的录音插进系统媒体库 Music/VoiceKey。
# 记事本在 App 内查媒体库没跑通（权限/ROM 限制），这里由 root 直接搬文件本体，
# 搬完删掉媒体库的行，免得音乐库里留一堆指向已删文件的幽灵条目。
import_media() {
  M=/sdcard/Music/VoiceKey
  [ -d "$M" ] || return
  set -- "$M"/*.m4a
  [ -e "$1" ] || return
  U=$(stat -c %u /data/data/$INK 2>/dev/null)
  [ -n "$U" ] || return
  D=/data/data/$INK/files/inbox
  mkdir -p "$D" 2>/dev/null
  for f in "$M"/*.m4a; do
    [ -f "$f" ] || continue
    n=$(basename "$f")
    # 刚写进来的先别动：语音键还在写（IS_PENDING），这时候搬可能搬走半截
    [ $(( $(date +%s) - $(stat -c %Y "$f") )) -lt 3 ] && continue
    if cp "$f" "$D/$n.tmp" 2>/dev/null && chown $U:$U "$D/$n.tmp" && chmod 600 "$D/$n.tmp" && mv -f "$D/$n.tmp" "$D/$n"; then
      # 落盘校验：目标存在且大小一致，才删源文件和媒体库记录
      if [ "$(stat -c %s "$D/$n" 2>/dev/null)" = "$(stat -c %s "$f" 2>/dev/null)" ]; then
        rm -f "$f"
        content delete --uri content://media/external/audio/media \
          --where "_display_name='$n'" >/dev/null 2>&1
        log "media import -> $n"
      else
        rm -f "$D/$n" 2>/dev/null
        log "media import 大小不符，保留源文件: $n"
      fi
    else
      rm -f "$D/$n.tmp" 2>/dev/null
      log "media import failed: $n"
    fi
  done
}

log "watchdog start (interval=15s)"
[ -z "$(pidof $VK)" ] && start_vk
[ -z "$(pidof $INK)" ] && start_ink

while true; do
  if [ -z "$(pidof $VK)" ]; then
    log "voicekey dead -> wake"
    start_vk
    sleep 5
  fi
  if [ -z "$(pidof $INK)" ]; then
    log "inknote dead -> start"
    start_ink
    sleep 5
  fi
  import_pending
  import_media
  sleep 15
done
