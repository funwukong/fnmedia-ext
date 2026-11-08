package android.net;
import java.util.HashMap;
public class StringUri extends Uri {
    private final String s;
    public StringUri(String s){this.s=s;}
    public String getScheme(){int i=s.indexOf("://");return i>0?s.substring(0,i):null;}
    public String getHost(){ try{ java.net.URI u=java.net.URI.create(s);return u.getHost(); }catch(Exception e){return null;} }
    public String getPath(){ try{ return java.net.URI.create(s).getPath(); }catch(Exception e){return null;} }
    public String getQueryParameter(String k){
        try{ java.net.URI u=java.net.URI.create(s); String q=u.getQuery(); if(q==null)return null;
            for(String kv:q.split("&")){String[] p=kv.split("=",2); if(p[0].equals(k))return p.length>1?java.net.URLDecoder.decode(p[1],"UTF-8"):"";} }catch(Exception e){}
        return null;
    }
    public String toString(){return s;}
}
