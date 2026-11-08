package android.content;
import java.util.HashMap;
public class ContentValuesDummy extends HashMap<String,Object> {
    public ContentValuesDummy(){}
    public void put(String k,String v){ super.put(k,v); }
    public void put(String k,Long v){ super.put(k,v); }
    public void put(String k,Integer v){ super.put(k,v); }
}
