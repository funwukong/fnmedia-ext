package com.fnmedia.bridge;
import android.app.Activity;
import android.os.Bundle;
public class BootActivity extends Activity {
    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        Bridge.start(getApplicationContext());
        try { startService(new android.content.Intent(this, BridgeService.class)); } catch (Throwable ignored) {}
        finish();
    }
}
