package android.os;
public class Handler {
    public Handler(){}
    public Handler(Looper l){}
    public Handler(Looper l, android.util.Log.Callback cb){}
    public boolean post(Runnable r){ r.run(); return true; }
    public boolean postDelayed(Runnable r, long d){ return false; }
    public void removeCallbacks(Runnable r){}
    public Looper getLooper(){ return Looper.getMainLooper(); }
}
