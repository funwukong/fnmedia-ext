import com.github.catvod.crawler.Spider;
import java.lang.reflect.*;
import java.util.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import com.google.gson.Gson;

public class Run {
    static String getStr(Map<String,Object> m, String k, String def){
        Object v=m.get(k); return v==null?def:String.valueOf(v);
    }
    @SuppressWarnings("unchecked")
    public static void main(String[] args) throws Exception {
        // 单次请求从 stdin 读 JSON：{class, method, ext, tid,pg,filter,extend,ids[],key,quick,flag,id}
        byte[] buf = System.in.readAllBytes();
        String reqText = new String(buf, StandardCharsets.UTF_8);
        Map<String,Object> req = new Gson().fromJson(reqText.isEmpty()?"{}":reqText, Map.class);
        String cls = getStr(req,"class","");
        String method = getStr(req,"method","home");
        Object extObj = req.get("ext");
        String ext = extObj==null ? "" : (extObj instanceof String ? (String)extObj : new Gson().toJson(extObj));

        // 预置全局 Application
        try {
            Class<?> initCls = Class.forName("com.github.catvod.spider.Init");
            Object init = initCls.getMethod("get").invoke(null);
            Field cf = initCls.getDeclaredField("c");
            cf.setAccessible(true);
            cf.set(init, new android.app.Application());
        } catch (Throwable t) { System.err.println("[warn] Init context: "+t); }

        Class<?> c = Class.forName(cls);
        Spider sp = (Spider) c.getDeclaredConstructor().newInstance();
        try { c.getMethod("init", android.content.Context.class, String.class)
                .invoke(sp, new android.content.Context(), ext); }
        catch (NoSuchMethodException e) { c.getMethod("init", android.content.Context.class)
                .invoke(sp, new android.content.Context()); }

        // 预置小雅登录态：触发私有 f() 解析 drives，再给每个 drive 注入 alist token（绕开 Android 登录弹窗）
        String token = getStr(req, "token", "");
        if (!token.isEmpty()) {
            try {
                Method mF = c.getDeclaredMethod("f"); mF.setAccessible(true); mF.invoke(sp);
                Field fb = c.getDeclaredField("b"); fb.setAccessible(true);
                Object drives = fb.get(sp);
                if (drives instanceof Iterable) {
                    for (Object drive : (Iterable<?>) drives) {
                        try { drive.getClass().getMethod("D", String.class).invoke(drive, token); }
                        catch (Throwable t2) { /* 单个盘注入失败忽略 */ }
                    }
                }
            } catch (Throwable t) { System.err.println("[warn] token inject: " + t); }
        }
        String res;
        switch (method) {
            case "home": res = sp.homeContent(Boolean.parseBoolean(getStr(req,"filter","false"))); break;
            case "homeVideo": res = sp.homeVideoContent(); break;
            case "category": {
                HashMap<String,String> ex = new HashMap<>();
                Object eo=req.get("extend"); if(eo instanceof Map) for(var e:((Map<String,Object>)eo).entrySet()) ex.put(String.valueOf(e.getKey()), String.valueOf(e.getValue()));
                res = sp.categoryContent(getStr(req,"tid","1"), getStr(req,"pg","1"),
                        Boolean.parseBoolean(getStr(req,"filter","false")), ex); break;
            }
            case "detail": {
                List<String> ids=new ArrayList<>(); for(var v:(List<Object>)req.getOrDefault("ids",List.of())) ids.add(String.valueOf(v));
                res = sp.detailContent(ids); break;
            }
            case "search": res = sp.searchContent(getStr(req,"key",""), Boolean.parseBoolean(getStr(req,"quick","false"))); break;
            case "play": res = sp.playerContent(getStr(req,"flag",""), getStr(req,"id",""), new ArrayList<>()); break;
            default: throw new IllegalArgumentException("unknown method "+method);
        }
        System.setOut(new PrintStream(System.out, true, StandardCharsets.UTF_8));
        System.out.print("\u0000RESULT\u0000");
        System.out.println(res==null?"":res);
    }
}
