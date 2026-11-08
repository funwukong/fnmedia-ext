package com.fnmedia.bridge;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;
public class BridgeService extends Service {
    @Override public void onCreate() { super.onCreate(); Bridge.start(getApplicationContext()); }
    @Override public int onStartCommand(Intent i, int f, int s) { Bridge.start(getApplicationContext()); return START_STICKY; }
    @Override public IBinder onBind(Intent i) { return null; }
}
