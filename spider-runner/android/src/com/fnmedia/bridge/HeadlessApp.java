package com.fnmedia.bridge;

import android.app.Application;
import android.content.Context;
import android.content.SharedPreferences;

import java.io.File;

/** headless app_process 下可用的假 Application，数据/缓存目录重定向到可写位置。 */
public class HeadlessApp extends Application {
    private final File data = new File(
            System.getProperty("java.io.tmpdir", "/data/local/tmp"), "catvod-data");
    private File e(File f) { if (!f.exists()) f.mkdirs(); return f; }
    @Override public File getCacheDir() { return e(new File(data, "cache")); }
    @Override public File getCodeCacheDir() { return e(new File(data, "code_cache")); }
    @Override public File getFilesDir() { return e(new File(data, "files")); }
    @Override public File getNoBackupFilesDir() { return e(new File(data, "no_backup")); }
    @Override public File getExternalCacheDir() { return e(new File(data, "ext_cache")); }
    @Override public File getDir(String n, int m) { return e(new File(data, "app_" + n)); }
    @Override public File getDatabasePath(String n) { e(new File(data, "db")); return new File(data, "db/" + n); }
    @Override public SharedPreferences getSharedPreferences(String n, int m) { return null; }
    @Override public Context getApplicationContext() { return this; }
}
