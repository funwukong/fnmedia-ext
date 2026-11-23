package android.content;
import java.io.File;
public class Context {
    public static final String WINDOW_SERVICE="window";
    private final File cacheDir=new File(System.getProperty("java.io.tmpdir"),"fnmedia-catvod-cache");
    private final File filesDir=new File(System.getProperty("java.io.tmpdir"),"fnmedia-catvod-files");
    public Context(){ cacheDir.mkdirs(); filesDir.mkdirs(); }
    public Object getSystemService(String n){ return null; }
    public android.content.pm.PackageManager getPackageManager(){ return new android.content.pm.PackageManager(); }
    public String getPackageName(){ return "com.github.catvod"; }
    public Context getApplicationContext(){ return this; }
    public File getCacheDir(){ return cacheDir; }
    public File getFilesDir(){ return filesDir; }
    public File getExternalCacheDir(){ return cacheDir; }
    public File getExternalFilesDir(String type){ return filesDir; }
    public File getDir(String name,int mode){ File f=new File(filesDir,name); f.mkdirs(); return f; }
    public android.content.SharedPreferencesDummy getSharedPreferences(String name,int mode){ return new android.content.SharedPreferencesDummy(); }
    public ClassLoader getClassLoader(){ return getClass().getClassLoader(); }
    public android.content.res.ResourcesDummy getResources(){ return new android.content.res.ResourcesDummy(); }
}
