package android.net;
public abstract class Uri {
    public static Uri parse(String s){ return new StringUri(s); }
    public abstract String getHost();
    public abstract String getScheme();
    public abstract String getQueryParameter(String k);
    public abstract String getPath();
    public abstract String toString();
}
