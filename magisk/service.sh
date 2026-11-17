#!/system/bin/sh
# VoiceKey 守护（兜底用）。
# 应用本身是 /system/priv-app 的 persistent 应用，正常由 AMS 拉起；
# 这个脚本只在「被 force-stop / 被冻结 / 锁屏阅读挡住」时补一脚。
#
# 关于锁屏阅读：该模式下系统会拦截一切 Activity 启动（am start 返回 error 102），
# 而唤醒麦克风必须靠 1×1 透明壳这个 Activity。所以拉起前临时关掉这两个开关，
# 拉起来后立刻恢复。

PKG=com.zuakeo.voicekey
LOG=/data/local/tmp/voicekeyd.log

until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 1; done
sleep 8

cmd appops set $PKG RUN_ANY_IN_BACKGROUND allow 2>/dev/null
cmd appops set $PKG START_FOREGROUND allow 2>/dev/null
dumpsys deviceidle whitelist +$PKG >/dev/null 2>&1

log() {
  [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 65536 ] && : > "$LOG"
  echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"
}

start_app() {
  lr=$(settings get system locked_read_enable 2>/dev/null)
  rdm=$(settings get system reading_mode_enabled 2>/dev/null)
  if [ "$lr" = "1" ] || [ "$rdm" = "1" ]; then
    settings put system locked_read_enable 0 2>/dev/null
    settings put system reading_mode_enabled 0 2>/dev/null
    sleep 1
    am start -n $PKG/.WakeActivity >/dev/null 2>&1
    sleep 4
    [ "$lr" = "1" ] && settings put system locked_read_enable 1 2>/dev/null
    [ "$rdm" = "1" ] && settings put system reading_mode_enabled 1 2>/dev/null
    log "锁屏阅读模式中拉起（开关已恢复 lr=$lr rm=$rdm）"
  else
    am start -n $PKG/.WakeActivity >/dev/null 2>&1
  fi
}

log "watchdog start (interval=15s)"
start_app

while true; do
  if [ -z "$(pidof $PKG)" ]; then
    log "dead -> wake"
    start_app
    sleep 5
  fi
  sleep 15
done
