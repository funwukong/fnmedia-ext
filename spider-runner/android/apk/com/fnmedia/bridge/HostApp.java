package com.fnmedia.bridge;
import android.app.Application;
public class HostApp extends Application {
    @Override public void onCreate() {
        super.onCreate();
        Bridge.start(getApplicationContext());
    }
}
