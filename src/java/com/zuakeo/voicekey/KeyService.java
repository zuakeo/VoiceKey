package com.zuakeo.voicekey;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.media.MediaRecorder;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Build;
import android.os.IBinder;
import android.os.SystemClock;
import android.util.Log;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URLEncoder;
import java.text.SimpleDateFormat;
import java.util.Arrays;
import java.util.Date;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.Locale;

/**
 * 常驻前台服务：
 * 用 root 直接读 /dev/input/event2 的原始事件，识别墨智键（scancode 766），
 * 按下开录、松开停止。完全不经过 Android 输入框架，所以海信拦不拦都无所谓。
 *
 * 关于麦克风：这台机器只给「到过前台」的进程放行麦克风，纯后台拉起的进程
 * MediaRecorder.setAudioSource 会直接抛异常。所以进程起来后第一次亮屏会用
 * 1×1 透明壳把进程顶到前台一次（见 WakeActivity），之后就一直可用。
 */
public class KeyService extends Service {
    public static final String TAG = "VoiceKey";
    public static final String ACTION_STATE = "com.zuakeo.voicekey.STATE";
    public static final String EXTRA_STATE = "state";

    private static final int EV_KEY = 1;
    /** ↓↓↓ 适配你自己的设备只需要改这两行 ↓↓↓
     *  设备节点：adb shell su -c 'getevent -l' 看按键落在哪个 eventN
     *  键码：    getevent 输出里的 code（例：EV_KEY KEY_AI → 766） */
    private static final String EV_PATH = "/dev/input/event2";
    private static final int CODE_MOKEY = 766;
    /** ↑↑↑ 适配你自己的设备只需要改这两行 ↑↑↑ */
    private static final int MIN_RECORD_MS = 500;

    private static final int STATE_IDLE = 0;
    private static final int STATE_REC = 1;
    private static final int STATE_SAVED = 2;
    private static final int STATE_SHORT = 3;
    private static final int STATE_ERROR = 4;

    /**
     * 通知渠道用 IMPORTANCE_NONE：渠道被屏蔽后通知不会出现在状态栏/通知栏/锁屏，
     * 但 service 依然是前台服务（前台状态和通知是否可见是两件事），
     * 这样 Android 11 的「后台应用禁止用麦克风」限制才管不到我们。
     */
    private static final String CHANNEL = "vk_silent";

    private Thread readerThread;
    private volatile boolean running;
    private MediaRecorder recorder;
    private String currentPath;
    private long pressAt;
    private Process suProcess;

    /** 推送失败计数、重试状态、网络回调 */
    private volatile int failStreak = 0;
    private volatile boolean retrying = false;
    private Thread retryThread;
    private ConnectivityManager.NetworkCallback netCallback;

    /** 本进程是否已经点亮过麦克风 */
    private volatile boolean micBlessed = false;
    private BroadcastReceiver screenReceiver;

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel c = new NotificationChannel(CHANNEL, "语音键后台", NotificationManager.IMPORTANCE_NONE);
            c.setShowBadge(false);
            c.enableVibration(false);
            c.setSound(null, null);
            c.setLockscreenVisibility(Notification.VISIBILITY_SECRET);
            nm.createNotificationChannel(c);
            // 清掉旧版本留下的可见渠道
            try { nm.deleteNotificationChannel("vk"); } catch (Throwable ignored) { }
        }
        startForeground(1, buildNotification("就绪", "长按墨智键说话，松开结束"));

        // 熄屏时被拉起的进程麦克风是锁着的；等下一次亮屏/解锁补一次「点亮」。
        screenReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                if (!micBlessed) wakeSelf();
            }
        };
        IntentFilter f = new IntentFilter();
        f.addAction(Intent.ACTION_SCREEN_ON);
        f.addAction(Intent.ACTION_USER_PRESENT);
        try { registerReceiver(screenReceiver, f); } catch (Throwable ignored) { }

        // 开机时进程被系统直接拉起（persistent），此时屏幕多半已经是亮的，
        // 收不到 SCREEN_ON 广播，所以这里主动补一次点亮。
        try {
            android.os.PowerManager pm =
                    (android.os.PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null && pm.isInteractive() && !micBlessed) {
                new android.os.Handler(getMainLooper()).postDelayed(this::wakeSelf, 3000);
            }
        } catch (Throwable ignored) { }

        startNetworkWatch();
    }

    /** 用 root 拉起 1×1 透明壳，把本进程顶到前台一次；壳 1.2 秒后自己退场。 */
    private void wakeSelf() {
        new Thread(() -> {
            try {
                Process p = Runtime.getRuntime().exec(new String[]{
                        "su", "-c", "am start -n com.zuakeo.voicekey/.WakeActivity"});
                p.waitFor();
            } catch (Throwable ignored) { }
        }, "wake-self").start();
    }

    private void runSu(String cmd) {
        try {
            Runtime.getRuntime().exec(new String[]{"su", "-c", cmd});
        } catch (Throwable ignored) { }
    }

    /**
     * 只为「第一次」点亮麦克风：临时亮屏 → 透明壳顶上来自愈 → 4 秒后把屏幕关回去。
     * 只在从未点亮过时调用，之后按键不会再碰屏幕状态。
     */
    private void blessWithScreen() {
        boolean wasOff = false;
        try {
            android.os.PowerManager pm =
                    (android.os.PowerManager) getSystemService(POWER_SERVICE);
            wasOff = pm != null && !pm.isInteractive();
        } catch (Throwable ignored) { }
        if (wasOff) {
            runSu("input keyevent 224");   // KEYCODE_WAKEUP
        }
        wakeSelf();
        if (wasOff) {
            new android.os.Handler(getMainLooper()).postDelayed(
                    () -> runSu("input keyevent 223"), 4000);   // KEYCODE_SLEEP
        }
    }

    private Notification buildNotification(String title, String text) {
        return new Notification.Builder(this, CHANNEL)
                .setContentTitle(title)
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setOngoing(true)
                .setShowWhen(false)
                .setPriority(Notification.PRIORITY_MIN)
                .setOnlyAlertOnce(true)
                .build();
    }

    private void notifyState(String text) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.notify(1, buildNotification("语音键", text));
        Intent i = new Intent(ACTION_STATE);
        i.setPackage(getPackageName());
        i.putExtra(EXTRA_STATE, text);
        sendBroadcast(i);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (readerThread == null) {
            running = true;
            readerThread = new Thread(this::readLoop, "mokey-reader");
            readerThread.start();
        }
        return START_STICKY;
    }

    private void readLoop() {
        while (running) {
            try {
                suProcess = Runtime.getRuntime().exec(new String[]{"su", "-c", "cat " + EV_PATH});
                InputStream is = suProcess.getInputStream();
                byte[] buf = new byte[24];
                notifyState("监听中（root 已就绪）");
                while (running) {
                    if (readFully(is, buf) < 24) break;
                    int type = u16(buf, 16);
                    int code = u16(buf, 18);
                    int value = s32(buf, 20);
                    if (type == EV_KEY && code == CODE_MOKEY) {
                        if (value == 1) {
                            onPress();
                        } else if (value == 0) {
                            onRelease();
                        }
                    }
                }
                try { suProcess.destroy(); } catch (Exception ignored) { }
                suProcess = null;
            } catch (Throwable t) {
                Log.e(TAG, "readLoop", t);
                notifyState("root 读取中断，2 秒后重试");
            }
            if (running) {
                try { Thread.sleep(2000); } catch (InterruptedException ignored) { }
            }
        }
    }

    private static int readFully(InputStream is, byte[] b) throws Exception {
        int off = 0;
        while (off < b.length) {
            int n = is.read(b, off, b.length - off);
            if (n < 0) return off;
            off += n;
        }
        return off;
    }

    private static int u16(byte[] b, int o) {
        return (b[o] & 0xFF) | ((b[o + 1] & 0xFF) << 8);
    }

    private static int s32(byte[] b, int o) {
        return (b[o] & 0xFF) | ((b[o + 1] & 0xFF) << 8)
                | ((b[o + 2] & 0xFF) << 16) | ((b[o + 3] & 0xFF) << 24);
    }

    private void onPress() {
        if (recorder != null) return;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            notifyState("缺少录音权限，请打开 App 授权");
            buzz(new long[]{0, 80, 80, 80, 80, 80});
            return;
        }
        // 还没点亮过麦克风：这一次允许临时亮屏把权限换出来，之后就不再亮屏了
        if (!micBlessed) {
            blessWithScreen();
            try { Thread.sleep(1500); } catch (InterruptedException ignored) { }
        }
        try {
            File dir = getExternalFilesDir(null);
            if (dir == null) dir = new File(getFilesDir(), "rec");
            if (!dir.exists()) dir.mkdirs();
            String name = "rec_" + new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date()) + ".m4a";
            File f = new File(dir, name);
            currentPath = f.getAbsolutePath();

            // 依次尝试录音源：MIC 最干净；后台未点亮时 VOICE_RECOGNITION 往往仍放行，
            // 这条兜底让「锁屏阅读」这类不许起 Activity 的场景也能录音。
            MediaRecorder r = null;
            Throwable lastErr = null;
            int[] sources = {MediaRecorder.AudioSource.MIC,
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    MediaRecorder.AudioSource.DEFAULT};
            for (int src : sources) {
                MediaRecorder cand = new MediaRecorder();
                try {
                    cand.setAudioSource(src);
                    cand.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
                    cand.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
                    cand.setAudioSamplingRate(44100);
                    cand.setAudioChannels(1);
                    cand.setAudioEncodingBitRate(128000);
                    cand.setOutputFile(currentPath);
                    cand.setMaxDuration(180000);   // 3 分钟：60 秒会把长句尾巴切掉
                    cand.prepare();
                    cand.start();
                    r = cand;
                    if (src != MediaRecorder.AudioSource.MIC) {
                        Log.i(TAG, "fallback 录音源 = " + src);
                    }
                    break;
                } catch (Throwable e) {
                    lastErr = e;
                    try { cand.release(); } catch (Throwable ignored) { }
                }
            }
            if (r == null) {
                throw (lastErr != null ? lastErr : new IllegalStateException("no recorder"));
            }
            recorder = r;
            pressAt = SystemClock.elapsedRealtime();
            micBlessed = true;
            buzz(50);
            notifyState("● 录音中…");
        } catch (Throwable t) {
            Log.e(TAG, "onPress", t);
            recorder = null;
            micBlessed = false;
            String msg = String.valueOf(t.getMessage());
            if (msg.contains("setAudioSource")) {
                // 后台拉起的进程拿不到麦克风，拉起透明壳补一次点亮（亮屏时立即生效）
                notifyState("麦克风被系统限制，正在恢复…");
                wakeSelf();
            }
            notifyState("录音启动失败：" + t.getClass().getSimpleName());
            buzz(new long[]{0, 80, 80, 80, 80, 80});
        }
    }

    private void onRelease() {
        if (recorder == null) return;
        long dur = SystemClock.elapsedRealtime() - pressAt;
        try { recorder.stop(); } catch (Throwable ignored) { }
        try { recorder.release(); } catch (Throwable ignored) { }
        recorder = null;

        File f = currentPath == null ? null : new File(currentPath);
        if (dur < MIN_RECORD_MS) {
            if (f != null && f.exists()) f.delete();
            buzz(new long[]{0, 25, 60, 25});
            notifyState("按住太短，已丢弃");
        } else {
            long kb = (f != null && f.exists()) ? f.length() / 1024 : 0;
            buzz(new long[]{0, 35, 70, 35});
            notifyState("已保存 " + (f != null ? f.getName() : "") + "（" + kb + "KB，" + (dur / 1000.0) + "秒）");
            if (f != null) uploadAsync(f);
        }
        currentPath = null;
    }

    private File recDir() {
        File dir = getExternalFilesDir(null);
        if (dir == null) dir = new File(getFilesDir(), "rec");
        if (!dir.exists()) dir.mkdirs();
        return dir;
    }

    private File sentDir() {
        File d = new File(recDir(), "sent");
        if (!d.exists()) d.mkdirs();
        return d;
    }

    private void moveToSent(File f) {
        try {
            File dst = new File(sentDir(), f.getName());
            if (!f.renameTo(dst)) {
                // rename 失败就直接删，避免反复重推
                f.delete();
            }
        } catch (Throwable ignored) { }
    }

    /** 同步上传一个文件，成功返回 true。 */
    private boolean uploadOne(File f) {
        String base = getSharedPreferences("vk", MODE_PRIVATE).getString("server", "");
        if (base == null || base.trim().isEmpty()) return false;
        HttpURLConnection c = null;
        try {
            String root = base.trim();
            if (root.endsWith("/")) root = root.substring(0, root.length() - 1);
            String full = root + "/upload?name=" + URLEncoder.encode(f.getName(), "UTF-8");
            c = (HttpURLConnection) new URL(full).openConnection();
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setConnectTimeout(4000);
            c.setReadTimeout(20000);
            c.setFixedLengthStreamingMode((int) f.length());
            c.setRequestProperty("Content-Type", "application/octet-stream");
            OutputStream os = c.getOutputStream();
            FileInputStream is = new FileInputStream(f);
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) os.write(buf, 0, n);
            os.flush();
            os.close();
            is.close();
            int code = c.getResponseCode();
            failStreak = (code == 200) ? 0 : failStreak + 1;
            return code == 200;
        } catch (Throwable t) {
            failStreak++;
            Log.e(TAG, "upload", t);
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** 把录音 POST 到电脑。成功移到 sent/，失败留在原地等重试。 */
    private void uploadAsync(final File f) {
        final String base = getSharedPreferences("vk", MODE_PRIVATE).getString("server", "");
        if (base == null || base.trim().isEmpty()) {
            notifyState("已保存（未配置电脑地址）");
            return;
        }
        Thread t = new Thread(() -> {
            boolean ok = uploadOne(f);
            if (ok) {
                notifyState("已推送 " + f.getName());
                toast("已推送 " + f.getName());
                moveToSent(f);
            } else {
                notifyState("推送失败，网络恢复后自动重试");
                toast("推送失败，稍后自动重试");
            }
        }, "upload");
        t.start();
    }

    /** 网络恢复 / 周期性：把积压的录音补推上去。 */
    private void retryPending() {
        if (retrying) return;
        retrying = true;
        try {
            File[] fs = recDir().listFiles();
            if (fs == null) return;
            Arrays.sort(fs, (a, b) -> Long.compare(a.lastModified(), b.lastModified()));
            int done = 0;
            for (File f : fs) {
                if (!f.isFile() || !f.getName().endsWith(".m4a")) continue;
                if (System.currentTimeMillis() - f.lastModified() < 5000) continue;
                if (uploadOne(f)) {
                    moveToSent(f);
                    done++;
                    if (done >= 10) break;
                } else {
                    if (failStreak >= 3) rediscover();
                    break;
                }
            }
            if (done > 0) Log.i(TAG, "补推 " + done + " 条");
        } catch (Throwable t) {
            Log.e(TAG, "retryPending", t);
        } finally {
            retrying = false;
        }
    }

    /** 连续失败时扫一遍同网段，找到电脑就改掉 pref 里的地址。 */
    private void rediscover() {
        final String base = getSharedPreferences("vk", MODE_PRIVATE).getString("server", "");
        if (base == null) return;
        String host;
        try {
            host = new URL(base.trim()).getHost();
        } catch (Throwable t) {
            return;
        }
        final String prefix = host.substring(0, host.lastIndexOf('.') + 1);
        if (prefix.equals(host)) return;
        new Thread(() -> {
            final AtomicBoolean found = new AtomicBoolean(false);
            ExecutorService pool = Executors.newFixedThreadPool(24);
            for (int i = 1; i <= 254 && !found.get(); i++) {
                final String h = prefix + i;
                pool.execute(() -> {
                    if (found.get()) return;
                    if (probe(h)) {
                        if (found.compareAndSet(false, true)) {
                            String url = "http://" + h + ":8765";
                            getSharedPreferences("vk", MODE_PRIVATE).edit()
                                    .putString("server", url).apply();
                            Log.i(TAG, "重新找到电脑: " + url);
                            notifyState("已重新连上电脑 " + h);
                            toast("已重新连上电脑 " + h);
                        }
                    }
                });
            }
            pool.shutdown();
        }, "rediscover").start();
    }

    /** 探测某个 IP 的 8765 是不是我们的接收端。 */
    private boolean probe(String host) {
        Socket s = new Socket();
        try {
            s.connect(new InetSocketAddress(host, 8765), 180);
            s.setSoTimeout(600);
            OutputStream os = s.getOutputStream();
            os.write("GET / HTTP/1.0\r\n\r\n".getBytes("UTF-8"));
            os.flush();
            BufferedReader r = new BufferedReader(new InputStreamReader(s.getInputStream()));
            String line;
            int n = 0;
            while ((line = r.readLine()) != null && n < 20) {
                if (line.contains("inkvoice")) return true;
                n++;
            }
            return false;
        } catch (Throwable t) {
            return false;
        } finally {
            try { s.close(); } catch (Throwable ignored) { }
        }
    }

    /** 网络一恢复就补推；另外每 30 秒兜底扫一次积压。 */
    private void startNetworkWatch() {
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            if (cm != null) {
                netCallback = new ConnectivityManager.NetworkCallback() {
                    @Override
                    public void onAvailable(Network network) {
                        new Thread(() -> {
                            try { Thread.sleep(2500); } catch (InterruptedException ignored) { }
                            retryPending();
                        }, "retry-net").start();
                    }
                };
                cm.registerDefaultNetworkCallback(netCallback);
            }
        } catch (Throwable t) {
            Log.e(TAG, "netwatch", t);
        }

        retryThread = new Thread(() -> {
            while (running) {
                try {
                    Thread.sleep(30000);
                } catch (InterruptedException e) {
                    break;
                }
                if (running) retryPending();
            }
        }, "retry-loop");
        retryThread.start();
    }

    private void buzz(long ms) {
        buzz(new long[]{0, ms});
    }

    /** 推送结果提示。后台 Toast 在 Android 11 可能被系统丢掉，所以 root 兜底用通知栏以外的 cmd 也不行，
     *  这里先直接 Toast，失败再加日志。 */
    private void toast(final String msg) {
        new android.os.Handler(getMainLooper()).post(() -> {
            try {
                android.widget.Toast.makeText(getApplicationContext(), msg,
                        android.widget.Toast.LENGTH_SHORT).show();
            } catch (Throwable ignored) { }
        });
    }

    @SuppressWarnings("deprecation")
    private void buzz(long[] pattern) {
        try {
            android.os.Vibrator v = (android.os.Vibrator) getSystemService(VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) {
                if (Build.VERSION.SDK_INT >= 26) {
                    v.vibrate(android.os.VibrationEffect.createWaveform(pattern, -1));
                } else {
                    v.vibrate(pattern, -1);
                }
            }
        } catch (Throwable ignored) { }
    }

    @Override
    public void onDestroy() {
        running = false;
        if (screenReceiver != null) {
            try { unregisterReceiver(screenReceiver); } catch (Throwable ignored) { }
            screenReceiver = null;
        }
        if (recorder != null) {
            try { recorder.stop(); } catch (Throwable ignored) { }
            try { recorder.release(); } catch (Throwable ignored) { }
            recorder = null;
        }
        if (suProcess != null) {
            try { suProcess.destroy(); } catch (Throwable ignored) { }
        }
        if (readerThread != null) {
            readerThread.interrupt();
            readerThread = null;
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
