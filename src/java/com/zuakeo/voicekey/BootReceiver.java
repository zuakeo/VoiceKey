package com.zuakeo.voicekey;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Intent i = new Intent(context, KeyService.class);
        try {
            // BOOT_COMPLETED 属于前台服务启动豁免场景，用 startForegroundService 才不会被拦
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(i);
            } else {
                context.startService(i);
            }
        } catch (Throwable ignored) {
            // 兜底：Magisk 的 service.sh 会在开机后检查并拉起
        }
    }
}
