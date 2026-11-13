package com.fnmedia.bridge;

import android.app.Application;
import android.content.Context;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

import org.json.JSONArray;
import org.json.JSONObject;

import dalvik.system.DexClassLoader;

/**
 * 免安装 bridge：经 app_process 在 Android 内运行，开本地 HTTP 端口，
 * 用 DexClassLoader 动态加载 TVBox 加固 spider jar，反射调用
 * com.github.catvod.crawler.Spider 的标准方法。
 *
 * 协议（body=JSON）：
 *   GET  /ping            -> {"ok":true,"abi":...}
 *   POST /call {"jar":"/sdcard/catvod/x.jar","class":"...","ext":..,"token":..,
 *               "method":"home|homeVod|category|detail|search|play", ...}
 *        -> {"ok":true,"result":<catvod JSON>} 或 {"ok":false,"error":...}
 *
 * jar 按 (路径,长度,lastModified) 缓存 ClassLoader；Spider 按 class+ext 缓存，
 * 以保留登录态。小雅等需要注入登录 token 的，复用与 JVM Sidecar 相同的反射手法：
 * init 后若存在私有字段 b(List drives) 与方法 f()，遍历调 D(token)。
 */
public class Bridge {

    static final int PORT = 9101;
    static File CODE_CACHE;
    static Context APP_CTX;

    // classLoaderKey -> DexClassLoader
    static final Map<String, ClassLoader> LOADERS = new HashMap<>();
    // spiderKey(class+ext) -> SpiderHolder
    static final Map<String, SpiderHolder> SPIDERS = new HashMap<>();

    static class SpiderHolder {
        Object spider;
        Class<?> cls;
    }

    static volatile boolean RUNNING = false;

    public static void main(String[] args) throws Exception {
        APP_CTX = buildSystemContext();
        File dataDir = new File(System.getProperty("java.io.tmpdir", "/data/local/tmp"));
        CODE_CACHE = new File(dataDir, "catvod-odex");
        if (!CODE_CACHE.exists()) CODE_CACHE.mkdirs();
        serve();
    }

    /** 供宿主 APK 的 Application/Service 在真实 App 进程内启动（native bridge 可用）。 */
    public static synchronized void start(Context appCtx) {
        if (RUNNING) return;
        RUNNING = true;
        APP_CTX = appCtx;
        CODE_CACHE = new File(appCtx.getCodeCacheDir(), "catvod-odex");
        if (!CODE_CACHE.exists()) CODE_CACHE.mkdirs();
        Thread t = new Thread(Bridge::serveSafe, "catvod-bridge");
        t.setDaemon(true);
        t.start();
    }

    static void serveSafe() { try { serve(); } catch (Throwable t) { t.printStackTrace(); } }

    static void serve() throws Exception {
        ServerSocket server = new ServerSocket(PORT);
        System.out.println("[catvod-bridge] listening " + PORT + " abi="
                + android.os.Build.SUPPORTED_ABIS[0]);
        java.util.concurrent.ExecutorService pool = Executors.newCachedThreadPool();
        while (true) {
            Socket sock = server.accept();
            pool.execute(() -> handle(sock));
        }
    }

    @SuppressWarnings("PrivateApi")
    static Context buildSystemContext() {
        // ActivityThread.systemMain 内部 new Handler()，必须先 Looper.prepare。
        if (android.os.Looper.myLooper() == null) android.os.Looper.prepare();
        try {
            Class<?> at = Class.forName("android.app.ActivityThread");
            Object thread = at.getMethod("systemMain").invoke(null);
            Method getSystemContext = at.getMethod("getSystemContext");
            Context ctx = (Context) getSystemContext.invoke(thread);
            if (ctx != null) {
                Context app = ctx.getApplicationContext();
                if (app != null) return app;
                return ctx;
            }
        } catch (Throwable t) {
            System.out.println("[catvod-bridge] systemMain: " + t);
        }
        try {
            Class<?> at = Class.forName("android.app.ActivityThread");
            Object am = at.getMethod("currentApplication").invoke(null);
            if (am instanceof Context) return (Context) am;
        } catch (Throwable ignored) {}
        return null;
    }

    /** 读一行 ASCII 请求头（以 \r\n 结束，返回去掉行尾的内容；连接结束返回 null）。 */
    static String readAsciiLine(java.io.DataInputStream din) throws java.io.IOException {
        java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream(128);
        int c;
        while ((c = din.read()) != -1) {
            if (c == '\r') {
                int c2 = din.read();
                if (c2 == '\n' || c2 == -1) break;
                b.write(c); b.write(c2);
            } else if (c == '\n') {
                break;
            } else {
                b.write(c);
            }
            if (b.size() > 65536) return null;
        }
        if (c == -1 && b.size() == 0) return null;
        return b.toString("ISO-8859-1");
    }

    static void handle(Socket sock) {
        try (Socket s = sock) {
            // 必须按“原始字节”读取：Content-Length 是字节数，而中文 UTF-8 是多字节，
            // 若用 Reader 按 char 读，char 数 < 字节数会永远等不满导致挂死。
            java.io.InputStream rawIn = s.getInputStream();
            // 请求行/头部是 ASCII，用 readLine 读字节行直到空行；body 另按字节读。
            java.io.DataInputStream din = new java.io.DataInputStream(rawIn);
            String reqLine = readAsciiLine(din);
            if (reqLine == null) return;
            String[] parts = reqLine.split(" ");
            String method = parts.length > 0 ? parts[0] : "GET";
            String path = parts.length > 1 ? parts[1] : "/";
            int contentLength = 0;
            String hline;
            while ((hline = readAsciiLine(din)) != null && !hline.isEmpty()) {
                if (hline.toLowerCase().startsWith("content-length:"))
                    contentLength = Integer.parseInt(hline.substring(15).trim());
            }
            byte[] bodyBytes;
            if (contentLength > 0) {
                bodyBytes = new byte[contentLength];
                din.readFully(bodyBytes);
            } else {
                bodyBytes = new byte[0];
            }
            String body = new String(bodyBytes, "UTF-8");
            Object response;
            int code = 200;
            if ("GET".equalsIgnoreCase(method) && path.startsWith("/ping")) {
                JSONObject pong = new JSONObject();
                pong.put("ok", true).put("abi", android.os.Build.SUPPORTED_ABIS[0])
                    .put("sdk", android.os.Build.VERSION.SDK_INT);
                response = pong;
            } else if ("GET".equalsIgnoreCase(method) && path.startsWith("/selftest")) {
                String q = path.contains("?") ? path.substring(path.indexOf('?') + 1) : "";
                for (String kv : q.split("&")) {
                    if (kv.startsWith("jar="))
                        System.setProperty("selftest.jar", java.net.URLDecoder.decode(kv.substring(4), "UTF-8"));
                }
                response = selftest();
            } else if ("POST".equalsIgnoreCase(method) && path.startsWith("/call")) {
                response = invoke(new JSONObject(body.toString()));
            } else {
                code = 404;
                response = new JSONObject().put("ok", false).put("error", "not found");
            }
            byte[] out = response.toString().getBytes("UTF-8");
            OutputStream os = s.getOutputStream();
            os.write(("HTTP/1.1 " + code + " OK\r\nContent-Type: application/json; charset=utf-8\r\n"
                    + "Content-Length: " + out.length + "\r\nConnection: close\r\n\r\n").getBytes());
            os.write(out);
            os.flush();
        } catch (Throwable t) {
            // 连接级错误直接丢弃
            System.out.println("[catvod-bridge] conn err: " + t);
        }
    }

    static JSONObject selftest() {
        JSONObject r = new JSONObject();
        try {
            BuildSpoof.apply();
            r.put("abi", android.os.Build.SUPPORTED_ABIS[0])
             .put("cacheDir", String.valueOf(APP_CTX.getCacheDir()))
             .put("nativeBridge", System.getProperty("os.arch"));
            // 从指定 jar 的 assets/ftyguard_v7.so 释放到 cache，再 System.load
            String jarPath = System.getProperty("selftest.jar", "");
            String loadMsg = "no jar";
            if (!jarPath.isEmpty()) {
                java.util.zip.ZipFile zf = new java.util.zip.ZipFile(jarPath);
                java.util.zip.ZipEntry ze = zf.getEntry("assets/ftyguard_v7.so");
                File outSo = new File(APP_CTX.getCacheDir(), "t7_" + System.nanoTime() + ".so");
                try (java.io.InputStream is = zf.getInputStream(ze);
                     java.io.FileOutputStream fo = new java.io.FileOutputStream(outSo)) {
                    byte[] b = new byte[8192]; int n;
                    while ((n = is.read(b)) > 0) fo.write(b, 0, n);
                }
                r.put("soSize", outSo.length());
                // 关键：先经 DexClassLoader 让 DexNative 类可被 JNI RegisterNatives
                // 找到（不触发 <clinit>），再在「能解析 DexNative」的前提下 load。
                String pre = "none";
                try {
                    ClassLoader cl = loaderFor(jarPath);
                    presetInit(cl);
                    Class<?> dn = Class.forName(
                        "com.github.catvod.spider.DexNative", false, cl);
                    r.put("dexNative", dn.getName());
                    // 列出其 native 方法名，供判断 .so 要注册哪些方法
                    StringBuilder nm = new StringBuilder();
                    for (java.lang.reflect.Method m : dn.getDeclaredMethods())
                        if (java.lang.reflect.Modifier.isNative(m.getModifiers()))
                            nm.append(m.getName()).append(",");
                    r.put("nativeMethods", nm.toString());
                    pre = "DexClassLoaded";
                } catch (Throwable t) {
                    pre = "DEX_FAIL:" + t;
                }
                r.put("preload", pre);
                try {
                    System.load(outSo.getAbsolutePath());
                    loadMsg = "LOAD_OK";
                } catch (Throwable t) {
                    StringBuilder sb = new StringBuilder(String.valueOf(t));
                    for (Throwable c = t.getCause(); c != null; c = c.getCause())
                        sb.append(" | cause=").append(c);
                    loadMsg = sb.toString();
                }
                r.put("load", loadMsg);
            }
            r.put("ok", true);
        } catch (Throwable t) {
            try { r.put("ok", false).put("fatal", String.valueOf(t)); } catch (Exception ignored) {}
        }
        return r;
    }

    static synchronized ClassLoader loaderFor(String jarPath) throws Exception {
        File jar = new File(jarPath);
        String key = jarPath + ":" + jar.length() + ":" + jar.lastModified();
        ClassLoader cl = LOADERS.get(key);
        if (cl != null) return cl;
        cl = new DexClassLoader(jarPath, CODE_CACHE.getAbsolutePath(),
                jar.getParent(), Bridge.class.getClassLoader());
        LOADERS.put(key, cl);
        return cl;
    }

    static synchronized Object result(JSONObject req) throws Exception {
        BuildSpoof.apply();   // 必须在加载加固类 / System.load 之前伪装 Build
        String jarPath = req.getString("jar");
        String className = req.getString("class");
        String ext = req.opt("ext") == null ? ""
                : (req.get("ext") instanceof String ? req.getString("ext")
                        : req.get("ext").toString());
        ClassLoader cl = loaderFor(jarPath);
        Class<?> cls = cl.loadClass(className);
        String skey = className + "|" + ext;
        SpiderHolder holder = SPIDERS.get(skey);
        Object spider;
        if (holder == null) {
            // 关键顺序：必须【先】把能读到 jar 内 assets 的 ClassLoader 与 Application
            // 注入加固版 Init 单例（Init$Loader 持有），【再】调用 Init.init(Application)。
            // 因为 DexNative.<clinit> 在 Init 类初始化阶段就会执行
            // Init.classLoader().getResourceAsStream("assets/ftyguard_v7.so")，并在
            // getLoader(context) 里用同一 ClassLoader 读取 assets/ftyshinidie.guard
            // 解密出真实爬虫 dex；若此刻 ClassLoader 字段还是 null，System.load/解密失败，
            // native getLoader 静默返回 null，随后 Init.getSpider 以 null 为参数回调
            // DexNative.getSpider -> ndk_translation 下 JNI CallObjectMethod(obj==null)
            // 直接 SIGABRT 杀进程（冷启动即崩的根因）。
            presetInit(cl);
            // 加固 jar：引导其 Init.init(Application)，native getLoader 才能生成
            // 加载“解密后真实爬虫”的 DexClassLoader（必须在构造 *Guard 之前）。
            bootHardenedInit(cl);
            Constructor<?> ctor = cls.getDeclaredConstructor();
            ctor.setAccessible(true);
            spider = ctor.newInstance();
            callInit(cls, spider, ext);
            holder = new SpiderHolder();
            holder.spider = spider;
            holder.cls = cls;
            SPIDERS.put(skey, holder);
        } else {
            spider = holder.spider;
        }
        // 注入登录 token（与 JVM Sidecar 一致）
        String token = req.optString("token", "");
        if (!token.isEmpty()) injectToken(spider, cls, token);
        return invokeMethods(cls, spider, req);
    }

    static JSONObject invoke(JSONObject req) throws Exception {
        try {
            Object r = result(req);
            JSONObject out = new JSONObject().put("ok", true);
            if (r instanceof String) {
                String s = (String) r;
                try { out.put("result", new JSONObject(s)); }
                catch (Exception e) { out.put("result", s); }
            } else if (r == null) {
                out.put("result", JSONObject.NULL);
            } else {
                out.put("result", JSONObject.wrap(r));
            }
            return out;
        } catch (Throwable t) {
            while (t instanceof java.lang.reflect.InvocationTargetException
                    && ((java.lang.reflect.InvocationTargetException) t).getCause() != null)
                t = ((java.lang.reflect.InvocationTargetException) t).getCause();
            if (t instanceof ExceptionInInitializerError
                    && ((ExceptionInInitializerError) t).getException() != null)
                t = ((ExceptionInInitializerError) t).getException();
            StringBuilder sw = new StringBuilder(t.getClass().getName());
            if (t.getMessage() != null) sw.append(": ").append(t.getMessage());
            for (StackTraceElement e : t.getStackTrace()) {
                sw.append("\n  at ").append(e.toString());
                if (sw.length() > 1500) break;
            }
            return new JSONObject().put("ok", false).put("error", sw.toString());
        }
    }

    static Application headlessApp() {
        // 始终返回一个重定向数据/缓存目录的假 Application（兼容各类加固/普通 jar）。
        try {
            Class<?> fake = Class.forName("com.fnmedia.bridge.HeadlessApp");
            java.lang.reflect.Constructor<?> c = fake.getDeclaredConstructor();
            c.setAccessible(true);
            return (Application) c.newInstance();
        } catch (Throwable t) {
            return APP_CTX instanceof Application ? (Application) APP_CTX : null;
        }
    }

    static void presetInit(ClassLoader cl) {
        Application app = headlessApp();
        try {
            Class<?> initCls = cl.loadClass("com.github.catvod.spider.Init");
            Object init = null;
            try { init = initCls.getMethod("get").invoke(null); } catch (Throwable ignored) {}
            // 1) 常见字段名（普通/旧版）
            for (String fn : new String[]{"c", "context", "ctx"}) {
                setIfType(initCls, init, fn, Application.class, app);
                setIfType(initCls, init, fn, Context.class, app);
            }
            // 2) 加固版混淆字段：遍历所有 Application/Context 字段注入
            if (init != null) for (Field f : initCls.getDeclaredFields()) {
                Class<?> t = f.getType();
                if (t == Application.class || t == Context.class) setField(init, f, app);
            }
            // 3) 加固版 ClassLoader 字段：注入能读到 jar 内 assets 的 ClassLoader
            if (init != null) for (Field f : initCls.getDeclaredFields()) {
                if (ClassLoader.class.isAssignableFrom(f.getType())) setField(init, f, cl);
            }
        } catch (Throwable ignored) {}
    }

    /** 加固 jar 引导：反射调用其 com.github.catvod.spider.Init.init(Application)。
     *  该方法内部经 native DexNative.getLoader(context) 生成解密用 DexClassLoader。
     *  只对确实含该加固 Init 的 jar 生效；普通纯 dex（小雅）无此方法则跳过。 */
    static void bootHardenedInit(ClassLoader cl) {
        try {
            Class<?> initCls;
            try {
                initCls = Class.forName("com.github.catvod.spider.Init", false, cl);
            } catch (ClassNotFoundException e) {
                return;
            }
            java.lang.reflect.Method mInit = null;
            for (java.lang.reflect.Method m : initCls.getDeclaredMethods()) {
                if (m.getName().equals("init") && m.getParameterCount() == 1
                        && m.getParameterTypes()[0].isAssignableFrom(APP_CTX.getClass())) {
                    mInit = m; break;
                }
            }
            if (mInit == null) return;
            mInit.setAccessible(true);
            try {
                mInit.invoke(null, APP_CTX);   // 需要 Application；HeadlessApp 即 Application
            } catch (Throwable t) {
                // 参数若严格要求 Application 类型而 ctx 是 ContextWrapper，尝试取 Application
                Throwable c = t.getCause() != null ? t.getCause() : t;
                throw c;
            }
            System.out.println("[catvod-bridge] hardened Init.init() bootstrapped");
        } catch (Throwable t) {
            System.err.println("[catvod-bridge] hardened init skip: " + t);
        }
    }

    static void setIfType(Class<?> cls, Object obj, String fn, Class<?> type, Object val) {
        if (obj == null) return;
        try { Field f = cls.getDeclaredField(fn); if (f.getType() == type) setField(obj, f, val); }
        catch (NoSuchFieldException ignored) {}
    }

    static void setField(Object obj, Field f, Object val) {
        try { f.setAccessible(true); f.set(obj, val); } catch (Throwable ignored) {}
    }

    static void callInit(Class<?> cls, Object spider, String ext) throws Exception {
        Context ctx = APP_CTX;
        try {
            Method m = cls.getMethod("init", Context.class, String.class);
            m.invoke(spider, ctx, ext);
            return;
        } catch (NoSuchMethodException ignored) {}
        cls.getMethod("init", Context.class).invoke(spider, ctx);
    }

    @SuppressWarnings("unchecked")
    static void injectToken(Object spider, Class<?> cls, String token) {
        try {
            try {
                Method mF = cls.getDeclaredMethod("f");
                mF.setAccessible(true);
                mF.invoke(spider);
            } catch (Throwable ignored) {}
            Field fb = cls.getDeclaredField("b");
            fb.setAccessible(true);
            Object drives = fb.get(spider);
            if (drives instanceof Iterable) {
                for (Object drive : (Iterable<Object>) drives) {
                    try {
                        drive.getClass().getMethod("D", String.class).invoke(drive, token);
                    } catch (Throwable ignored) {}
                }
            }
        } catch (Throwable ignored) {}
    }

    static Object invokeMethods(Class<?> cls, Object sp, JSONObject req) throws Exception {
        String m = req.optString("method", "home");
        switch (m) {
            case "home":
                return cls.getMethod("homeContent", boolean.class)
                        .invoke(sp, req.optBoolean("filter", true));
            case "homeVod":
                return cls.getMethod("homeVideoContent").invoke(sp);
            case "category": {
                HashMap<String, String> extend = new HashMap<>();
                JSONObject eo = req.optJSONObject("extend");
                if (eo != null) {
                    JSONArray names = eo.names();
                    if (names != null) for (int i = 0; i < names.length(); i++) {
                        String k = names.getString(i);
                        extend.put(k, eo.optString(k));
                    }
                }
                return cls.getMethod("categoryContent", String.class, String.class,
                                boolean.class, HashMap.class)
                        .invoke(sp, req.optString("tid", "1"), req.optString("pg", "1"),
                                req.optBoolean("filter", false), extend);
            }
            case "detail": {
                JSONArray arr = req.getJSONArray("ids");
                List<String> ids = new java.util.ArrayList<>();
                for (int i = 0; i < arr.length(); i++) ids.add(arr.getString(i));
                return cls.getMethod("detailContent", List.class).invoke(sp, ids);
            }
            case "search":
                try {
                    return cls.getMethod("searchContent", String.class, boolean.class,
                                    String.class)
                            .invoke(sp, req.optString("key"), req.optBoolean("quick", false),
                                    req.optString("pg", "1"));
                } catch (NoSuchMethodException e) {
                    return cls.getMethod("searchContent", String.class, boolean.class)
                            .invoke(sp, req.optString("key"), req.optBoolean("quick", false));
                }
            case "play": {
                JSONArray vip = req.optJSONArray("vipFlags");
                List<String> flags = new java.util.ArrayList<>();
                if (vip != null) for (int i = 0; i < vip.length(); i++) flags.add(vip.getString(i));
                return cls.getMethod("playerContent", String.class, String.class, List.class)
                        .invoke(sp, req.optString("flag", ""), req.optString("id", ""), flags);
            }
            default:
                throw new IllegalArgumentException("unknown method " + m);
        }
    }
}
