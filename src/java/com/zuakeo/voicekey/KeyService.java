package com.zuakeo.voicekey;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.media.MediaRecorder;
import android.net.Uri;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Build;
import android.os.IBinder;
import android.os.SystemClock;
import android.provider.MediaStore;
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

    /** 正在投递的文件名，防 onRelease 与 retryPending 并发重复投递。 */
    private static final java.util.Set<String> INFLIGHT =
            java.util.Collections.synchronizedSet(new java.util.HashSet<String>());
    public static final String ACTION_STATE = "com.zuakeo.voicekey.STATE";
    public static final String EXTRA_STATE = "state";

    private static final int EV_KEY = 1;
    private static final int CODE_MOKEY = 766;
    private static final int MIN_RECORD_MS = 500;

    /** 震动节奏：靠长短和次数区分，不靠数拍子。
     *  开始 = 一记长震；结束 = 两记短震；太短 = 三记极短；出错 = 两记重震。 */
    private static final long[] VIB_START = {0, 200};
    private static final long[] VIB_STOP = {0, 80, 80, 80};
    private static final long[] VIB_SHORT = {0, 30, 60, 30, 60, 30};
    private static final long[] VIB_ERROR = {0, 300, 150, 300};

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

    /**
     * 找到侧键所在的事件节点。不写死 event2：
     * 从 /proc/bus/input/devices 里找名字叫 gpio-keys（或带 mokey）的那个。
     * 找不到再退回 event2（本机实测就是它）。
     */
    private String resolveEventNode() {
        try {
            Process p = Runtime.getRuntime().exec(
                    new String[]{"su", "-c", "cat /proc/bus/input/devices"});
            java.io.BufferedReader r = new java.io.BufferedReader(
                    new java.io.InputStreamReader(p.getInputStream()));
            String line;
            String name = null;
            String found = null;
            java.util.regex.Pattern pat = java.util.regex.Pattern.compile("(event\\d+)");
            while ((line = r.readLine()) != null) {
                if (line.startsWith("N: Name=")) {
                    name = line.toLowerCase();
                } else if (line.startsWith("H: Handlers=") && name != null) {
                    if (name.contains("gpio-keys") || name.contains("mokey")
                            || name.contains("keypad")) {
                        java.util.regex.Matcher m = pat.matcher(line);
                        if (m.find()) {
                            found = "/dev/input/" + m.group(1);
                            break;
                        }
                    }
                    name = null;
                }
            }
            try { p.waitFor(); } catch (InterruptedException ignored) { }
            if (found != null) {
                Log.i(TAG, "侧键节点：" + found);
                return found;
            }
        } catch (Throwable t) {
            Log.e(TAG, "resolveEventNode", t);
        }
        // 退回写死的节点是最后一招：明确记 WARN + 通知里说清楚，
        // 免得换设备后「按了没反应」还查不出原因。
        Log.w(TAG, "没找到 gpio-keys，退回 /dev/input/event2（可能不是侧键所在节点）");
        notifyState("侧键节点未识别，暂用 event2");
        return "/dev/input/event2";
    }

    private void readLoop() {
        String node = resolveEventNode();
        while (running) {
            try {
                suProcess = Runtime.getRuntime().exec(new String[]{"su", "-c", "cat " + node});
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
            buzz(VIB_ERROR);
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
            buzz(VIB_START);
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
            buzz(VIB_ERROR);
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
            buzz(VIB_SHORT);
            notifyState("按住太短，已丢弃");
        } else {
            long kb = (f != null && f.exists()) ? f.length() / 1024 : 0;
            buzz(VIB_STOP);
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

    private static boolean tlsReady = false;

    /** 电脑端开了 HTTPS 时，只认配置里那张证书的指纹。 */
    private void installTls() {
        if (tlsReady) return;
        String pin = getSharedPreferences("vk", MODE_PRIVATE).getString("tls_pin", "");
        if (pin == null || pin.trim().isEmpty()) {
            tlsReady = true;
            return;
        }
        final String want = pin.trim().toLowerCase();
        try {
            javax.net.ssl.SSLContext sc = javax.net.ssl.SSLContext.getInstance("TLS");
            sc.init(null, new javax.net.ssl.TrustManager[]{
                    new javax.net.ssl.X509TrustManager() {
                        public void checkClientTrusted(java.security.cert.X509Certificate[] c,
                                                       String a) { }
                        public void checkServerTrusted(java.security.cert.X509Certificate[] chain,
                                                       String a)
                                throws java.security.cert.CertificateException {
                            try {
                                java.security.MessageDigest md =
                                        java.security.MessageDigest.getInstance("SHA-256");
                                byte[] d = md.digest(chain[0].getEncoded());
                                StringBuilder sb = new StringBuilder();
                                for (byte b : d) sb.append(String.format("%02x", b));
                                if (!sb.toString().equalsIgnoreCase(want)) {
                                    throw new java.security.cert.CertificateException("pin mismatch");
                                }
                            } catch (java.security.NoSuchAlgorithmException e) {
                                throw new java.security.cert.CertificateException(e);
                            }
                        }
                        public java.security.cert.X509Certificate[] getAcceptedIssuers() {
                            return new java.security.cert.X509Certificate[0];
                        }
                    }
            }, new java.security.SecureRandom());
            javax.net.ssl.HttpsURLConnection.setDefaultSSLSocketFactory(sc.getSocketFactory());
            javax.net.ssl.HttpsURLConnection.setDefaultHostnameVerifier((h2, s2) -> true);
            tlsReady = true;
        } catch (Throwable t) {
            Log.e(TAG, "installTls", t);
        }
    }

    /** 同步上传一个文件，成功返回 true。 */
    private boolean uploadOne(File f) {
        installTls();
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
            String cid = f.getName();
            int dt = cid.lastIndexOf('.');
            if (dt > 0) cid = cid.substring(0, dt);
            c.setRequestProperty("X-Clip-Id", cid);
            String tok = getSharedPreferences("vk", MODE_PRIVATE).getString("token", "");
            if (tok != null && !tok.trim().isEmpty()) {
                c.setRequestProperty("Authorization", "Bearer " + tok.trim());
            }
            OutputStream os = c.getOutputStream();
            FileInputStream is = new FileInputStream(f);
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) os.write(buf, 0, n);
            os.flush();
            os.close();
            is.close();
            int code = c.getResponseCode();
            // 接受整个 2xx 家族。服务端为了表示「已入队」可能回 202，
            // 以前只认 200，结果成功的上传被判失败，录音被重复塞进记事本。
            boolean ok = (code >= 200 && code < 300);
            failStreak = ok ? 0 : failStreak + 1;
            return ok;
        } catch (Throwable t) {
            failStreak++;
            Log.e(TAG, "upload", t);
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** 查电脑上这个 clip 是否已经在队列/已完成。用于「上传响应丢了」的场景。 */
    private boolean alreadyOnPc(String base, String clip) {
        HttpURLConnection c = null;
        try {
            String root = base.trim();
            if (root.endsWith("/")) root = root.substring(0, root.length() - 1);
            c = (HttpURLConnection) new URL(
                    root + "/jobs/" + java.net.URLEncoder.encode(clip, "UTF-8")).openConnection();
            c.setConnectTimeout(3000);
            c.setReadTimeout(5000);
            String tok = getSharedPreferences("vk", MODE_PRIVATE).getString("token", "");
            if (tok != null && !tok.trim().isEmpty()) {
                c.setRequestProperty("Authorization", "Bearer " + tok.trim());
            }
            if (c.getResponseCode() != 200) return false;
            InputStream is = c.getInputStream();
            StringBuilder sb = new StringBuilder();
            byte[] buf = new byte[2048];
            int n;
            while ((n = is.read(buf)) > 0) sb.append(new String(buf, 0, n, "UTF-8"));
            is.close();
            // 服务端对不存在的任务返回 {}，存在的至少带 state 字段
            return sb.indexOf("\"state\"") >= 0;
        } catch (Throwable t) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** 有没有外网。
     *  优先问系统；拿不到（系统应用换 APK 后新权限常常没注册）就用 DNS 端口探活兜底。 */
    private boolean hasInternet() {
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            if (cm != null) {
                android.net.Network n = cm.getActiveNetwork();
                if (n != null) {
                    android.net.NetworkCapabilities cap = cm.getNetworkCapabilities(n);
                    if (cap != null) {
                        return cap.hasCapability(
                                        android.net.NetworkCapabilities.NET_CAPABILITY_INTERNET)
                                && cap.hasCapability(
                                        android.net.NetworkCapabilities.NET_CAPABILITY_VALIDATED);
                    }
                }
            }
        } catch (Throwable ignored) {
            // 没权限或系统不给，走下面的探活
        }
        // 探三个公共 DNS：连得上就算在线（总共最多约 1.5 秒）
        String[] hosts = {"223.5.5.5", "114.114.114.114", "8.8.8.8"};
        for (String h : hosts) {
            java.net.Socket s = new java.net.Socket();
            try {
                s.connect(new java.net.InetSocketAddress(h, 53), 500);
                return true;
            } catch (Throwable ignored) {
            } finally {
                try { s.close(); } catch (Throwable ignored) { }
            }
        }
        return false;
    }

    /** 录音交给便签 App。
     *  首选直接写进它的私有收件箱 /data/data/com.zuakeo.inknote/files/inbox/ 并 chown 成它的 uid；
     *  便签没装或没 root 时，退回 /sdcard/InkNote/pending（便签会用 root 来拉）。 */
    private boolean toInkNote(File f) {
        String name = f.getName();
        if (name.startsWith("rec_")) name = "vk_" + name.substring(4);
        // 先走系统媒体库（最干净，不依赖 root，也不受包可见性影响）
        if (publishToMediaStore(f, name)) return true;
        if (pushToInbox(f, name)) return true;
        return toPublicPending(f, name);
    }

    /** 把录音插进系统媒体库 Music/VoiceKey，由记事本自己去取。
     *  自己插自己的文件不需要任何存储权限；对方读只要存储权限。 */
    private boolean publishToMediaStore(File f, String name) {
        Uri u = null;
        try {
            ContentValues v = new ContentValues();
            v.put(MediaStore.Audio.Media.DISPLAY_NAME, name);
            v.put(MediaStore.Audio.Media.MIME_TYPE, "audio/mp4");
            v.put(MediaStore.Audio.Media.RELATIVE_PATH, "Music/VoiceKey");
            v.put(MediaStore.Audio.Media.IS_PENDING, 1);
            u = getContentResolver().insert(
                    MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, v);
            if (u == null) return false;
            OutputStream os = getContentResolver().openOutputStream(u);
            if (os == null) return false;
            FileInputStream is = new FileInputStream(f);
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) os.write(buf, 0, n);
            os.close();
            is.close();
            ContentValues done = new ContentValues();
            done.put(MediaStore.Audio.Media.IS_PENDING, 0);
            getContentResolver().update(u, done, null, null);
            f.delete();
            return true;
        } catch (Throwable t) {
            Log.e(TAG, "publishToMediaStore", t);
            try {
                if (u != null) getContentResolver().delete(u, null, null);
            } catch (Throwable ignored) { }
            return false;
        }
    }

    private static final String INK_PKG_DIR = "/data/data/com.zuakeo.inknote";

    /** root 直写便签的私有目录，省掉 FUSE 那道坑。 */
    /** 便签 App 的 uid：先问 PackageManager，拿不到再借 su 跑 `pm list packages -U`。
     *  Android 11 有包可见性限制，这台 ROM 加了 <queries> 也仍拿不到，所以必须有第二条路。 */
    private int inkUid() {
        try {
            return getPackageManager().getPackageInfo("com.zuakeo.inknote", 0)
                    .applicationInfo.uid;
        } catch (Throwable t) {
            Log.w(TAG, "PackageManager 查不到便签 uid：" + t);
        }
        try {
            Process p = Runtime.getRuntime().exec(
                    new String[]{"su", "-c", "pm list packages -U com.zuakeo.inknote"});
            java.io.BufferedReader r = new java.io.BufferedReader(
                    new java.io.InputStreamReader(p.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) sb.append(line).append(' ');
            p.waitFor();
            java.util.regex.Matcher m =
                    java.util.regex.Pattern.compile("uid:(\\d+)").matcher(sb);
            if (m.find()) return Integer.parseInt(m.group(1));
            Log.e(TAG, "pm 输出里没有 uid：" + sb);
        } catch (Throwable t) {
            Log.e(TAG, "inkUid via su", t);
        }
        return -1;
    }

    private boolean pushToInbox(File f, String name) {
        int uid = inkUid();
        if (uid <= 0) {
            Log.e(TAG, "pushToInbox: 查不到便签的 uid");
            return false;
        }
        String dst = INK_PKG_DIR + "/files/inbox/" + name;
        // 注意：这里不要在 shell 里用 $(stat ...) 求 uid。实测这条链上它会返回空，
        // 导致整段被跳过、每次都回落公共目录（root 660，便签读不到）。
        String cmd = "mkdir -p " + INK_PKG_DIR + "/files/inbox"
                // 先写 .tmp 再原子改名：便签那边只会看到完整文件（整改 P1-01）
                + " && cp '" + f.getAbsolutePath() + "' '" + dst + ".tmp'"
                + " && chown " + uid + ":" + uid + " '" + dst + ".tmp'"
                + " && chmod 600 '" + dst + ".tmp'"
                + " && mv -f '" + dst + ".tmp' '" + dst + "'"
                + " && rm -f '" + f.getAbsolutePath() + "' && echo INKOK";
        try {
            Process p;
            try {
                p = Runtime.getRuntime().exec(new String[]{"/system/bin/su", "-c", cmd});
            } catch (Throwable t) {
                // 极少数 ROM 上 /system/bin/su 不可执行，退回 PATH 里的 su
                p = Runtime.getRuntime().exec(new String[]{"su", "-c", cmd});
            }
            java.io.BufferedReader r = new java.io.BufferedReader(
                    new java.io.InputStreamReader(p.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) sb.append(line).append(' ');
            int code = p.waitFor();
            if (sb.indexOf("INKOK") >= 0) return true;
            Log.e(TAG, "pushToInbox: exit=" + code + " 输出=" + sb);
            dumpPushError(cmd, code, sb.toString());
        } catch (Throwable t) {
            Log.e(TAG, "pushToInbox", t);
            dumpPushError(cmd, -1, String.valueOf(t));
        }
        return false;
    }

    /** 把失败现场写到自己的目录，方便 adb 直接看（App 一定有写权限）。 */
    private void dumpPushError(String cmd, int code, String out) {
        try {
            java.io.File f = new java.io.File(
                    getExternalFilesDir(null), "_vk_push.log");
            java.io.FileWriter w = new java.io.FileWriter(f, true);
            w.write(new java.util.Date().toString() + "  exit=" + code
                    + "\n  cmd=" + cmd + "\n  out=" + out + "\n");
            w.close();
        } catch (Throwable ignored) { }
    }

    /** 老路子：写共享目录，便签靠 root 来拉。 */
    private boolean toPublicPending(File f, String name) {
        try {
            File dir = new File(android.os.Environment.getExternalStorageDirectory(),
                    "InkNote/pending");
            if (!dir.exists() && !dir.mkdirs()) return false;
            File dst = new File(dir, name);
            if (dst.exists()) dst.delete();
            if (f.renameTo(dst)) {
                // renameTo 不走 su 分支，权限保持 660 root:everybody，记事本读不到。
                // 这里补一句放开读权限（自己创建的文件才有效）。
                dst.setReadable(true, false);
                return true;
            }
            try {
                FileInputStream is = new FileInputStream(f);
                java.io.FileOutputStream os = new java.io.FileOutputStream(dst);
                byte[] buf = new byte[8192];
                int n;
                while ((n = is.read(buf)) > 0) os.write(buf, 0, n);
                os.close();
                is.close();
                f.delete();
                return true;
            } catch (Throwable ignored) {
                // 系统应用换 APK 后新权限可能没注册，走 root 兜底
            }
            Process p = Runtime.getRuntime().exec(new String[]{"su", "-c",
                    "mkdir -p " + dir.getAbsolutePath() + " && cp '" + f.getAbsolutePath()
                            + "' '" + dst.getAbsolutePath() + "' && chmod 666 '"
                            + dst.getAbsolutePath() + "' && rm -f '" + f.getAbsolutePath() + "'"});
            p.waitFor();
            return dst.exists();
        } catch (Throwable t) {
            Log.e(TAG, "toPublicPending", t);
            return false;
        }
    }

    /** 几百毫秒判断电脑在不在。 */
    private boolean quickReachable(String addr) {
        try {
            String a = addr.trim();
            if (!a.startsWith("http")) a = "http://" + a;
            String host = new java.net.URL(a).getHost();
            java.net.Socket s = new java.net.Socket();
            try {
                s.connect(new java.net.InetSocketAddress(host, 8765), 500);
                return true;
            } finally {
                try { s.close(); } catch (Throwable ignored) { }
            }
        } catch (Throwable t) {
            return false;
        }
    }

    /** 把录音 POST 到电脑。成功移到 sent/，失败留在原地等重试。
     *  没有外网时不推电脑，直接存进便签的待转写目录，等联网后由便签 App 转文字。 */
    private void uploadAsync(final File f) {
        final String base = getSharedPreferences("vk", MODE_PRIVATE).getString("server", "");
        if (base == null || base.trim().isEmpty()) {
            // 没配电脑地址：能上网就交给便签，不能就留在本机等重试
            if (hasInternet() && toInkNote(f)) {
                notifyState("已存下（未配置电脑地址）");
                toast("已存下（未配置电脑地址）");
            } else {
                notifyState("已保存（未配置电脑地址）");
            }
            return;
        }
        Thread t = new Thread(() -> {
            // 同一个文件可能被 onRelease 和 retryPending 同时处理（网络抖动时会撞上），
            // 撞上就会「电脑收一份、记事本又存一份」。这里按文件名做在途去重。
            if (!INFLIGHT.add(f.getName())) return;
            try {
                deliver(f);
            } finally {
                INFLIGHT.remove(f.getName());
            }
        }, "upload");
        t.start();
    }

    /** 真正的投递逻辑：先电脑，再记事本。抽出来是为了让上面的 try/finally 包住。 */
    private void deliver(final File f) {
        {
            final String base = getSharedPreferences("vk", MODE_PRIVATE).getString("server", "");
            // 电脑在内网，和外网通不通是两件事；以前先判 hasInternet()，
            // 一旦 Wi-Fi 被判「无外网」，录音就直接进了手机记事本，电脑收不到。
            String clip = f.getName();
            int dot = clip.lastIndexOf('.');
            if (dot > 0) clip = clip.substring(0, dot);
            if (quickReachable(base) && uploadOne(f)) {
                notifyState("已推送 " + f.getName());
                toast("已推送 " + f.getName());
                moveToSent(f);
                return;
            }
            // 上传没拿到成功响应 ≠ 电脑没收到：可能已经入队，只是响应在网络上丢了。
            // 这时再问一次任务状态，确认到了就按成功处理，避免电脑一份、记事本又一份。
            if (alreadyOnPc(base, clip)) {
                notifyState("已推送 " + f.getName() + "（电脑已确认收到）");
                moveToSent(f);
                return;
            }
            // 电脑没连上（没开机 / 不在同一网 / 没网）→ 一律交给记事本排队。
            // 以前这里是 `hasInternet() && toInkNote(f)`，没网时就不交了，
            // 录音只能躺在 recDir 里等 retryPending，用户在记事本里什么也看不到。
            // 记事本的 SyncService 自己会处理「没网先等着」：
            // 文件留在 inbox，通知栏显示「有 N 条录音等着联网转写」，联网后自动转写。
            if (toInkNote(f)) {
                boolean online = hasInternet();
                notifyState(online ? "电脑没连上，已存进记事本" : "没网，已存进记事本（联网后自动转写）");
                toast(online ? "电脑没连上，已存进记事本" : "没网，已存进记事本");
            } else {
                notifyState("推送失败，稍后自动重试");
                toast("推送失败，稍后自动重试");
            }
        }
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
