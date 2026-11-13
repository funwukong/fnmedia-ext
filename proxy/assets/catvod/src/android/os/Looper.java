package android.os;
public class Looper {
    private static final Looper ME = new Looper();
    public static Looper getMainLooper(){ return ME; }
    public static Looper myLooper(){ return ME; }
}
