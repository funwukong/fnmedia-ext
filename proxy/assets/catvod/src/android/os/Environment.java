package android.os;
import java.io.File;
public class Environment {
    public static final String MEDIA_MOUNTED="mounted";
    public static File getExternalStorageDirectory(){ return new File(System.getProperty("java.io.tmpdir")); }
    public static File getExternalStoragePublicDirectory(String type){ return getExternalStorageDirectory(); }
    public static File getDataDirectory(){ return new File(System.getProperty("java.io.tmpdir")); }
    public static String getExternalStorageState(){ return MEDIA_MOUNTED; }
    public static File getDownloadCacheDirectory(){ return getExternalStorageDirectory(); }
}
