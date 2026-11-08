import com.github.catvod.crawler.Spider;
import java.lang.reflect.*;
import java.util.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import com.google.gson.Gson;

// 常驻 catvod 运行时：stdin 逐行读 JSONL 请求，stdout 逐行回 JSONL。
// 请求 {id,class,ext,method,...}；同一 (class,ext) 复用 Spider 实例（保留登录会话/token）。
public class Sidecar {
    static final Gson GSON = new Gson();
    static Spider spider; static String spiderKey=""; static Class<?> spiderClass=null;
    static PrintStream out;

    static String s(Map<String,Object>m,String k,String d){Object v=m.get(k);return v==null?d:String.valueOf(v);}

    @SuppressWarnings("unchecked")
    static Spider ensure(Map<String,Object> req) throws Exception {
        String cls=s(req,"class",""); Object extObj=req.get("ext");
        String ext=extObj==null?"":(extObj instanceof String?(String)extObj:GSON.toJson(extObj));
        String key=cls+""+ext;
        if (spider!=null && key.equals(spiderKey)) return spider;
        // 预置全局 Application
        Class<?> initCls=Class.forName("com.github.catvod.spider.Init");
        Object init=initCls.getMethod("get").invoke(null);
        Field cf=initCls.getDeclaredField("c"); cf.setAccessible(true);
        cf.set(init,new android.app.Application());
        Class<?> c=Class.forName(cls);
        Spider sp=(Spider)c.getDeclaredConstructor().newInstance();
        try { c.getMethod("init",android.content.Context.class,String.class).invoke(sp,new android.content.Context(),ext);}
        catch(NoSuchMethodException e){ c.getMethod("init",android.content.Context.class).invoke(sp,new android.content.Context());}
        spider=sp; spiderKey=key; spiderClass=c; return sp;
    }

    // 小雅等网盘源：init 后需注入登录 token（绕开 Android 登录弹窗）。
    // 触发私有 f() 解析 drives，再对每个 drive 调 D(token)。每次调用都刷新（token 会过期）。
    static void injectToken(Spider sp, Class<?> c, Map<String,Object> req) {
        Object to=req.get("token"); if(to==null) return;
        String token=String.valueOf(to); if(token.isEmpty()) return;
        try {
            Method mF=c.getDeclaredMethod("f"); mF.setAccessible(true);
            try { mF.invoke(sp); } catch(Throwable ignore) {}
            Field fb=c.getDeclaredField("b"); fb.setAccessible(true);
            Object drives=fb.get(sp);
            if(drives instanceof Iterable){
                for(Object drive:(Iterable<?>)drives){
                    try { drive.getClass().getMethod("D",String.class).invoke(drive,token);} catch(Throwable ignore){}
                }
            }
        } catch(Throwable ignore) {}
    }

    @SuppressWarnings("unchecked")
    static String call(Spider sp, Map<String,Object> req) throws Exception {
        String method=s(req,"method","home");
        switch(method){
            case "home": return sp.homeContent(Boolean.parseBoolean(s(req,"filter","false")));
            case "homeVideo": return sp.homeVideoContent();
            case "category": {
                HashMap<String,String> ex=new HashMap<>();
                Object eo=req.get("extend"); if(eo instanceof Map) for(var e:((Map<String,Object>)eo).entrySet()) ex.put(String.valueOf(e.getKey()),String.valueOf(e.getValue()));
                return sp.categoryContent(s(req,"tid","1"),s(req,"pg","1"),Boolean.parseBoolean(s(req,"filter","false")),ex);
            }
            case "detail": {
                List<String> ids=new ArrayList<>(); for(var v:(List<Object>)req.getOrDefault("ids",List.of())) ids.add(String.valueOf(v));
                return sp.detailContent(ids);
            }
            case "search": return sp.searchContent(s(req,"key",""),Boolean.parseBoolean(s(req,"quick","false")));
            case "play": return sp.playerContent(s(req,"flag",""),s(req,"id",""),new ArrayList<>());
            default: throw new IllegalArgumentException("unknown "+method);
        }
    }

    public static void main(String[] a) throws Exception {
        out=new PrintStream(System.out,true,StandardCharsets.UTF_8);
        BufferedReader br=new BufferedReader(new InputStreamReader(System.in,StandardCharsets.UTF_8));
        String line;
        while((line=br.readLine())!=null){
            line=line.trim(); if(line.isEmpty())continue;
            Map<String,Object> req; String id="";
            try { req=GSON.fromJson(line,Map.class); id=s(req,"id",""); }
            catch(Throwable t){ out.println(GSON.toJson(Map.of("id",id,"error","bad json: "+t))); continue; }
            try {
                Spider sp=ensure(req);
                injectToken(sp, spiderClass, req);
                String res=call(sp,req);
                Object parsed=res; try { parsed=GSON.fromJson(res,Object.class);} catch(Throwable ignore){}
                Map<String,Object> r=new HashMap<>(); r.put("id",id); r.put("result",parsed==null?"":parsed);
                out.println(GSON.toJson(r));
            } catch(Throwable t){
                StringWriter sw=new StringWriter(); t.printStackTrace(new PrintWriter(sw));
                Map<String,Object> r=new HashMap<>(); r.put("id",id); r.put("error",String.valueOf(t));
                out.println(GSON.toJson(r));
            }
        }
    }
}
