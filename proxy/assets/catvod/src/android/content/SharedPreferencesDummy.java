package android.content;
import java.util.HashMap;
public class SharedPreferencesDummy {
    private final HashMap<String,Object> m=new HashMap<>();
    public String getString(String k,String def){ Object v=m.get(k); return v==null?def:v.toString(); }
    public int getInt(String k,int def){ Object v=m.get(k); return v==null?def:(Integer)v; }
    public long getLong(String k,long def){ Object v=m.get(k); return v==null?def:(Long)v; }
    public boolean getBoolean(String k,boolean def){ Object v=m.get(k); return v==null?def:(Boolean)v; }
    public Editor edit(){ return new Editor(); }
    public class Editor {
        public Editor putString(String k,String v){ m.put(k,v); return this; }
        public Editor putInt(String k,int v){ m.put(k,v); return this; }
        public Editor putLong(String k,long v){ m.put(k,v); return this; }
        public Editor putBoolean(String k,boolean v){ m.put(k,v); return this; }
        public Editor remove(String k){ m.remove(k); return this; }
        public boolean commit(){ return true; }
        public void apply(){}
    }
}
