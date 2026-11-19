package com.zuakeo.voicekey;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.util.Arrays;
import java.util.Comparator;

public class MainActivity extends Activity {
    private TextView statusView;
    private TextView listView;
    private Button startBtn;
    private final Handler ui = new Handler(Looper.getMainLooper());

    private final BroadcastReceiver stateReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            String s = intent.getStringExtra(KeyService.EXTRA_STATE);
            if (s != null) statusView.setText(s);
        }
    };

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(buildUi());
        askPermissions();
        checkRoot();

        IntentFilter f = new IntentFilter(KeyService.ACTION_STATE);
        registerReceiver(stateReceiver, f);

        // 前台启动一次，绕过海信 MobileSafeService 对后台服务的限制
        startService(new Intent(this, KeyService.class));
    }

    @Override
    protected void onDestroy() {
        try { unregisterReceiver(stateReceiver); } catch (Throwable ignored) { }
        super.onDestroy();
    }

    private View buildUi() {
        int pad = dp(20);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad);
        root.setBackgroundColor(0xFFFFFFFF);

        TextView title = new TextView(this);
        title.setText("语音键");
        title.setTextSize(26);
        title.setTextColor(0xFF000000);
        title.setGravity(Gravity.START);
        root.addView(title);

        statusView = new TextView(this);
        statusView.setText("启动中…");
        statusView.setTextSize(17);
        statusView.setTextColor(0xFF000000);
        statusView.setPadding(0, dp(14), 0, dp(14));
        root.addView(statusView);

        startBtn = mkButton("停止监听");
        startBtn.setOnClickListener(v -> {
            if (startBtn.getText().toString().startsWith("停止")) {
                stopService(new Intent(this, KeyService.class));
                startBtn.setText("开始监听");
                statusView.setText("已停止");
            } else {
                startService(new Intent(this, KeyService.class));
                startBtn.setText("停止监听");
                statusView.setText("启动中…");
            }
        });
        root.addView(startBtn);

        Button perm = mkButton("授权录音");
        perm.setOnClickListener(v -> askPermissions());
        root.addView(perm);

        Button rootBtn = mkButton("检查 root");
        rootBtn.setOnClickListener(v -> checkRoot());
        root.addView(rootBtn);

        final SharedPreferences sp = getSharedPreferences("vk", MODE_PRIVATE);

        final EditText et = new EditText(this);
        et.setHint("http://192.0.2.10:8765");
        et.setTextSize(15);
        et.setSingleLine(true);
        et.setText(sp.getString("server", ""));
        LinearLayout.LayoutParams etLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        etLp.topMargin = dp(12);
        root.addView(et, etLp);

        Button save = mkButton("保存电脑地址");
        save.setOnClickListener(v -> {
            String url = et.getText().toString().trim();
            sp.edit().putString("server", url).apply();
            Toast.makeText(this, url.isEmpty() ? "已清空，录音只存本地" : "已保存：" + url,
                    Toast.LENGTH_SHORT).show();
            statusView.setText("电脑地址：" + (url.isEmpty() ? "（未设置）" : url));
        });
        root.addView(save);

        Button refresh = mkButton("刷新录音列表");
        refresh.setOnClickListener(v -> refreshList());
        root.addView(refresh);

        TextView dirLabel = new TextView(this);
        dirLabel.setTextSize(12);
        dirLabel.setTextColor(0xFF666666);
        dirLabel.setPadding(0, dp(16), 0, dp(6));
        File d = getExternalFilesDir(null);
        dirLabel.setText("录音目录：" + (d == null ? "?" : d.getAbsolutePath()));
        root.addView(dirLabel);

        ScrollView scroll = new ScrollView(this);
        listView = new TextView(this);
        listView.setTextSize(13);
        listView.setTextColor(0xFF000000);
        listView.setLineSpacing(dp(4), 1f);
        scroll.addView(listView);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f);
        root.addView(scroll, lp);

        ui.post(this::refreshList);
        return root;
    }

    private Button mkButton(String text) {
        Button btn = new Button(this);
        btn.setText(text);
        btn.setTextSize(17);
        btn.setAllCaps(false);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(52));
        lp.topMargin = dp(8);
        btn.setLayoutParams(lp);
        return btn;
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density);
    }

    private void askPermissions() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 10);
        } else {
            statusView.setText("录音权限已授予");
        }
    }

    private void checkRoot() {
        new Thread(() -> {
            String result;
            try {
                Process p = Runtime.getRuntime().exec(new String[]{"su", "-c", "id"});
                BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()));
                StringBuilder sb = new StringBuilder();
                String line;
                while ((line = r.readLine()) != null) sb.append(line).append(' ');
                int code = p.waitFor();
                result = code == 0 && sb.toString().contains("uid=0")
                        ? "root 正常：" + sb.toString().trim()
                        : "root 不可用（" + code + "）";
            } catch (Throwable t) {
                result = "root 调用失败：" + t.getClass().getSimpleName();
            }
            final String s = result;
            ui.post(() -> statusView.setText(s));
        }).start();
    }

    private void refreshList() {
        File dir = getExternalFilesDir(null);
        if (dir == null || !dir.exists()) {
            listView.setText("（目录不存在）");
            return;
        }
        File[] files = dir.listFiles((d, name) -> name.endsWith(".m4a") || name.endsWith(".wav"));
        if (files == null || files.length == 0) {
            listView.setText("（还没有录音）");
            return;
        }
        Arrays.sort(files, Comparator.comparingLong(File::lastModified).reversed());
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < files.length && i < 30; i++) {
            File f = files[i];
            sb.append(f.getName()).append("  ").append(f.length() / 1024).append(" KB\n");
        }
        sb.append("\n共 ").append(files.length).append(" 条");
        listView.setText(sb.toString());
    }
}
