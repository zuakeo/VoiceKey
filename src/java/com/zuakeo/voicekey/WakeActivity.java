package com.zuakeo.voicekey;

import android.app.Activity;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.WindowManager;

/**
 * 唤醒壳：1×1 透明窗口，肉眼不可见。
 *
 * 为什么必须是「有窗口的 Activity」而不是 Theme.NoDisplay：
 * 实测这台海信（Android 11）对后台进程不给麦克风——MediaRecorder.setAudioSource 直接抛异常；
 * 只有当进程真正处于前台（有 resumed 的 Activity）时，前台服务的麦克风才被放行。
 * 所以这里用一个 1×1 透明、不接收触摸、不抢焦点的窗口，把进程短暂顶到前台，
 * 起来服务后 1.2 秒自动 finish，屏幕上什么都看不到，也不影响锁屏/息屏。
 */
public class WakeActivity extends Activity {
    private final Handler h = new Handler(Looper.getMainLooper());
    private final Runnable finishTask = new Runnable() {
        @Override public void run() { finish(); overridePendingTransition(0, 0); }
    };

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        try {
            WindowManager.LayoutParams lp = getWindow().getAttributes();
            lp.width = 1;
            lp.height = 1;
            lp.gravity = Gravity.TOP | Gravity.START;
            lp.x = 0;
            lp.y = 0;
            lp.dimAmount = 0f;
            getWindow().setAttributes(lp);
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                    | WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                    | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS);
            getWindow().setDimAmount(0f);
        } catch (Throwable ignored) { }
    }

    @Override
    protected void onResume() {
        super.onResume();
        Intent i = new Intent(this, KeyService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                startForegroundService(i);
            } else {
                startService(i);
            }
        } catch (Throwable ignored) { }
        h.removeCallbacks(finishTask);
        h.postDelayed(finishTask, 1200);
    }

    @Override
    protected void onPause() {
        h.removeCallbacks(finishTask);
        super.onPause();
    }

    @Override
    public void onBackPressed() {
        finish();
    }
}
