package com.github.catvod.crawler;
import android.content.Context;
import java.util.HashMap;
import java.util.List;
public abstract class Spider {
    public void init(Context context) {}
    public void init(Context context, String extend) {}
    public void destroy() {}
    public boolean isVideoFormat(String url) { return false; }
    public boolean isVideoFormat(String url, java.util.HashMap<String,String> headers) { return isVideoFormat(url); }
    public void manualVideoCheck() {}
    public Object proxyLocal() { return null; }
    public String homeContent(boolean filter) { return ""; }
    public String homeVideoContent() { return ""; }
    public String categoryContent(String tid, String pg, boolean filter, HashMap<String,String> extend) { return ""; }
    public String detailContent(List<String> ids) { return ""; }
    public String searchContent(String key, boolean quick) { return ""; }
    public String searchContent(String key, boolean quick, String pg) { return searchContent(key, quick); }
    public String playerContent(String flag, String id, List<String> vipFlags) { return ""; }
}
