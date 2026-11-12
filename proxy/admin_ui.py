#!/usr/bin/env python3
"""飞牛影视·在线扩展 管理台（单文件 HTML，无外部依赖）。

布局对齐 fnmusic-ext 管理台：桌面左侧导航 + 移动端底部导航，多页卡片。
完整版专属区块用可剥离标记包裹，公开构建会被打包脚本整块移除。
"""

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>fnmedia-ext 管理台</title>
<style>
:root{
  --bg:#f4f6f8;--card:#fff;--line:#e3e8ee;--text:#1c2733;--muted:#68788a;
  --accent:#2f6fed;--accent-soft:#e8f0fe;--ok:#1a9e5c;--warn:#d97706;
  --err:#d64545;--radius:12px;--shadow:0 1px 3px rgba(16,30,54,.08);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  background:var(--bg);color:var(--text)}
.layout{display:flex;min-height:100vh;min-height:100dvh}

/* ---------- 侧栏 ---------- */
.sidebar{width:220px;flex:none;padding:20px 14px;background:var(--card);
  border-right:1px solid var(--line);display:flex;flex-direction:column;gap:16px;
  position:sticky;top:0;height:100vh;height:100dvh}
.brand{display:flex;align-items:center;gap:10px;padding:0 8px}
.brand-mark{width:38px;height:38px;border-radius:10px;flex:none;display:flex;
  align-items:center;justify-content:center;font-size:20px;background:var(--accent-soft)}
.brand-name{font-weight:700}
.brand-sub{font-size:12px;color:var(--muted)}
.nav{display:flex;flex-direction:column;gap:4px}
.nav-item{position:relative;text-align:left;padding:10px 12px;border:0;border-radius:8px;
  background:transparent;color:var(--text);font-size:15px;cursor:pointer}
.nav-item:hover{background:var(--accent-soft)}
.nav-item.active{background:var(--accent);color:#fff}
.sidebar-foot{margin-top:auto;font-size:12px;color:var(--muted);padding:0 8px}

/* ---------- 主区 ---------- */
.main{flex:1;min-width:0;width:100%;padding:24px 28px 96px;max-width:900px}
.page{display:none}.page.active{display:block}
h2.page-title{margin:4px 0 4px;font-size:20px}
.page-desc{color:var(--muted);font-size:13px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px 18px;margin-bottom:14px;box-shadow:var(--shadow)}
.card-title{font-weight:600;margin-bottom:4px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.card-sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.hint{color:var(--muted);font-size:13px;margin:6px 0}
.muted{color:var(--muted)}.sm{font-size:12px}
.stack{display:flex;flex-direction:column;gap:8px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.mt{margin-top:10px}

/* ---------- 控件 ---------- */
input[type=text],input[type=number],input[type=password],textarea,select{
  background:#fff;border:1px solid var(--line);border-radius:8px;color:var(--text);
  padding:8px 10px;width:100%;font:inherit}
input[type=number]{width:auto}
textarea{resize:vertical;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent)}
label.field{display:block;margin:10px 0}
label.field>span{display:block;color:var(--muted);font-size:12px;margin-bottom:4px}
.switch-row{display:flex;align-items:flex-start;gap:10px;padding:10px 0;cursor:pointer}
.switch-row input{width:17px;height:17px;margin-top:2px;accent-color:var(--accent);flex:none}
.switch-row .sub{display:block;font-size:13px;color:var(--muted)}
.btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);
  background:#fff;color:var(--text);border-radius:8px;padding:8px 14px;cursor:pointer;font-size:14px}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{opacity:.92;color:#fff}
.btn.small{padding:5px 12px;font-size:13px;min-height:0}
.btn:disabled{opacity:.5;cursor:default}
.btn.danger{color:var(--err)}.btn.danger:hover{background:var(--err);color:#fff;border-color:var(--err)}
.btn-row{display:flex;gap:10px;align-items:center;margin:10px 0;flex-wrap:wrap}

/* ---------- 徽标/状态点 ---------- */
.badge{font-size:11px;padding:1px 8px;border-radius:99px;background:var(--accent-soft);color:var(--accent);font-weight:500}
.dot{display:inline-flex;align-items:center;gap:6px;font-size:13px}
.dot::before{content:"";width:9px;height:9px;border-radius:50%;background:var(--muted)}
.dot.ok::before{background:var(--ok)}.dot.bad::before{background:var(--err)}.dot.wait::before{background:var(--warn)}
.pill{font-size:11px;padding:2px 9px;border-radius:99px;white-space:nowrap;border:1px solid transparent}
.pill.on{color:var(--ok);background:#e7f6ee;border-color:#bfe6d0}
.pill.off{color:var(--muted);background:#eef1f5;border-color:var(--line)}
.pill.warn{color:#b45309;background:#fef3e2;border-color:#f3d9b0}
.chip{display:inline-flex;align-items:center;gap:5px;background:#eef1f5;border:1px solid var(--line);
  color:var(--text);border-radius:99px;padding:2px 6px 2px 11px;font-size:12px;margin:2px}
.chip b{cursor:pointer;font-weight:700;color:var(--muted);padding:0 3px;border-radius:99px}
.chip b:hover{color:var(--err)}
.tag{display:inline-block;font-size:10px;color:var(--accent);background:var(--accent-soft);
  border-radius:5px;padding:0 5px;margin-left:6px}
.tag.amber{color:#b45309;background:#fef3e2}
.tag.warn{color:#b45309;background:#fef3e2}
/* 统一订阅导入结果 */
.subgrp{border:1px solid var(--line);border-radius:9px;padding:8px 10px;margin:10px 0;background:#fbfcfe}
.subgrp-t{font-size:13px;font-weight:600;margin-bottom:6px}
.subrow{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:13px;padding:3px 0}
.subrow.ok span:first-child{color:var(--ok,#1a8d3a)}
.subrow.muted{color:var(--muted)}

/* ---------- 实时播放诊断 ---------- */
.live-dev{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0;background:#fbfcfe}
.live-rows{margin-top:6px;display:flex;flex-direction:column;gap:3px}
.live-row{display:flex;align-items:center;gap:6px;font-size:12px;flex-wrap:wrap}
.live-row .muted{font-size:11px}
.live-dev details{margin-top:6px}
.diag{background:#fff;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:13px}

/* ---------- 表格 ---------- */
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--muted);font-weight:500;font-size:12px}
tr:last-child td{border-bottom:0}
.meta{color:var(--muted);font-size:12px;word-break:break-all}
.del{color:var(--err);background:transparent;border:0;cursor:pointer;font-size:13px;padding:4px 6px}
.st{font-size:12px;white-space:nowrap}.st.ok{color:var(--ok)}.st.bad{color:var(--err)}.st.wait{color:var(--muted)}

/* ---------- 管理页开关/缓存卡片（移植上游 v0.2.54/69：导入按钮、缓存网格、代理模式段） ---------- */
label.btn,label.btn-sm{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  border:1px solid var(--accent);background:#fff;color:var(--accent);border-radius:9px;
  padding:8px 15px;font-size:13.5px;cursor:pointer;white-space:nowrap;font-family:inherit}
label.btn-sm{padding:5px 12px;font-size:12.5px;border-radius:7px}
label.btn:hover,label.btn-sm:hover{background:var(--accent-soft)}
.cache-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin:10px 0}
.cache-item{border:1px solid var(--line);border-radius:10px;padding:11px 12px;background:#fafbfc}
.cache-item .cn{font-size:13px;font-weight:600}
.cache-item .cv{font-size:12px;color:var(--muted);margin:3px 0 9px;word-break:break-all}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#f1f3f6;flex-wrap:wrap}
.seg button{border:none!important;background:transparent!important;padding:7px 14px;font-size:13px;color:var(--muted)!important;border-radius:0!important;box-shadow:none!important}
.seg button.active{background:var(--accent)!important;color:#fff!important;font-weight:600}

/* ---------- 概览统计 ---------- */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat{background:#fafbfc;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat .n{font-size:22px;font-weight:700}
.stat .l{font-size:12px;color:var(--muted)}
.lib-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.lib{display:flex;align-items:center;gap:10px;background:#fafbfc;border:1px solid var(--line);
  border-radius:10px;padding:10px 12px}
.lib .ic{font-size:20px}
.lib .t{font-weight:600;font-size:14px}
.lib .c{font-size:12px;color:var(--muted)}
.lib .pin{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--muted);
  cursor:pointer;white-space:nowrap;padding:2px 8px;border-radius:99px;border:1px solid var(--line);background:#fff}
.lib .pin input{margin:0;accent-color:var(--accent)}
.lib:has(.pin input:checked){border-color:var(--accent);background:var(--accent-soft)}

/* ---------- 优先级 ---------- */
.prio-input{width:74px}
.prio-bar{height:5px;border-radius:3px;background:linear-gradient(90deg,#cfe0fb,#2f6fed);margin-top:3px}

/* ---------- 弹窗/日志/toast ---------- */
pre.log{max-height:240px;overflow:auto;background:#0f1115;color:#d6dae0;border-radius:8px;
  padding:10px;font-size:11px;line-height:1.5;margin:6px 0 0;white-space:pre-wrap}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(8px);
  background:#222;color:#fff;padding:9px 18px;border-radius:99px;font-size:13px;opacity:0;
  transition:.22s;pointer-events:none;z-index:80;max-width:90vw}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.ok{background:var(--ok)}#toast.fail{background:var(--err)}

/* ---------- 移动端 ---------- */
.bottom-nav{display:none}
@media(max-width:760px){
  .sidebar{display:none}
  .main{padding:max(16px,env(safe-area-inset-top,0px)) 14px
    calc(64px + env(safe-area-inset-bottom,8px));max-width:100%}
  h2.page-title{font-size:18px}
  input[type=text],input[type=password],textarea,select{font-size:16px}
  .btn{min-height:42px}
  .bottom-nav{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:45;
    height:calc(58px + env(safe-area-inset-bottom,0px));box-sizing:border-box;align-items:center;
    background:rgba(255,255,255,.97);border-top:1px solid var(--line);backdrop-filter:blur(6px);
    padding-bottom:env(safe-area-inset-bottom,0px)}
  .bottom-nav .nav-item{flex:1;display:flex;align-items:center;justify-content:center;
    height:58px;text-align:center;padding:0 2px;font-size:12px;border-radius:0;white-space:nowrap}
  .bottom-nav .nav-item.active{background:transparent;color:var(--accent);font-weight:700}
}
/* 加固影视浏览：海报墙 + 播放器 */
.gb-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:10px;margin-top:6px}
.gb-card{cursor:pointer;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--card);
  transition:transform .12s,box-shadow .12s;display:flex;flex-direction:column}
.gb-card:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.12)}
.gb-poster{position:relative;width:100%;aspect-ratio:2/3;background:#0f1115 center/cover no-repeat;display:block}
.gb-rem{position:absolute;right:4px;top:4px;background:rgba(0,0,0,.65);color:#fff;font-size:10px;
  padding:1px 6px;border-radius:99px;max-width:90%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gb-cap{padding:5px 6px;font-size:12px;line-height:1.3}
.gb-t{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:30px}
.gb-src{color:var(--muted);font-size:10px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gb-mask{position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:9999;display:flex;align-items:center;
  justify-content:center;padding:14px}
.gb-player{width:min(960px,100%);background:#111;border-radius:12px;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.gb-player-bar{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#1c1f26;color:#eee;font-size:13px}
</style>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js" async></script>
</head>
<body>
<div class="layout">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">🎬</div>
      <div><div class="brand-name">fnmedia-ext</div><div class="brand-sub">管理台</div></div>
    </div>
    <nav class="nav" id="nav">
      <button class="nav-item active" data-page="overview">概览</button>
      <button class="nav-item" data-page="library">虚拟媒体库</button>
      <button class="nav-item" data-page="quality">片源质量</button>
      <button class="nav-item" data-page="sources">资源站</button>
      <button class="nav-item" data-page="importsub">导入订阅</button>
      <!-- >>> PRIVATE:GUARD -->
      <button class="nav-item" data-page="tvbox">TVBox 加固</button>
      <button class="nav-item" data-page="pan" data-pan-nav style="display:none">☁️ 网盘</button>
      <!-- <<< PRIVATE:GUARD -->
      <!-- >>> PRIVATE:CATVOD -->
      <button class="nav-item" data-page="xiaoya">小雅 / JS</button>
      <!-- <<< PRIVATE:CATVOD -->
      <button class="nav-item" data-page="search">搜索</button>
      <button class="nav-item" data-page="about">关于</button>
    </nav>
    <div class="sidebar-foot">
      <span id="globalState" class="dot wait">检测中…</span>
    </div>
  </aside>

  <main class="main">

    <!-- ============ 概览 ============ -->
    <section class="page active" id="page-overview">
      <h2 class="page-title">概览</h2>
      <p class="page-desc">在飞牛原生搜索与媒体库中注入在线影视，点击直接调用飞牛原生播放器。</p>

      <div class="card">
        <div class="card-title">虚拟媒体库</div>
        <p class="card-sub">两个方向：① 各类目热门资源（豆瓣真实榜单，点击搜源播放）；② 海量片库（资源站分类深翻）。</p>
        <div class="lib-grid" id="ovLibs"><div class="muted sm">加载中…</div></div>
      </div>

      <div class="card">
        <div class="card-title">本设备识别 <span class="badge" id="ovClientKind">-</span></div>
        <div class="hint" id="ovClient">读取中…</div>
        <div class="muted sm" id="ovClientUa" style="word-break:break-all"></div>
      </div>

      <div class="card">
        <div class="card-title">实时播放诊断
          <span class="muted sm" style="font-weight:400">
            <span class="dot ok" id="livePulse" style="margin-left:8px"><span id="liveState">实时</span></span>
            <a href="#" id="liveClear" style="margin-left:10px">清空</a>
            <a href="#" id="liveCopy" style="margin-left:10px">📋 复制诊断信息</a>
          </span>
        </div>
        <div class="hint">显示各端（电视/手机/iOS/网页）最近 30 分钟的播放请求：走本地构造还是透传官方、当前在播、是否报错。每 3 秒刷新。</div>
        <div id="liveDiagnosis" style="margin:6px 0"></div>
        <div style="display:flex;gap:12px;flex-wrap:wrap;margin:6px 0 10px" class="sm muted">
          <span>来源：<b style="color:var(--ok)">local</b> 插件构造</span>
          <span><b style="color:#2563eb">mixed</b> 合并官方</span>
          <span><b style="color:#b45309">upstream</b> 透传官方</span>
          <span><b style="color:var(--err)">4xx/5xx</b> 出错</span>
        </div>
        <div id="liveDevices"><div class="muted sm">暂无播放活动（在电视或手机上播一部片即可看到）</div></div>
      </div>

      <div class="card">
        <div class="card-title">统计</div>
        <div class="stat-grid">
          <div class="stat"><div class="n" id="ovSrcN">-</div><div class="l">启用资源站</div></div>
          <div class="stat"><div class="n" id="ovCacheN">-</div><div class="l">已缓存在线条目</div></div>
          <div class="stat"><div class="n" id="ovHotN">-</div><div class="l">热门榜单条目</div></div>
          <div class="stat"><div class="n" id="ovCatN">-</div><div class="l">海量库可用类目</div></div>
        </div>
        <div class="hint" id="ovHotMeta"></div>
      </div>

      <!-- >>> PRIVATE:GUARD -->
      <div class="card">
        <div class="card-title">外挂运行时（TVBox 加固源）
          <span class="pill off" id="ovAndroid">Android</span></div>
        <div class="stack" id="ovRt"><div class="muted sm">检测中…</div></div>
      </div>
      <!-- <<< PRIVATE:GUARD -->
    </section>

    <!-- ============ 虚拟媒体库 ============ -->
    <section class="page" id="page-library">
      <h2 class="page-title">虚拟媒体库</h2>
      <p class="page-desc">插件在飞牛影视媒体库中虚拟出的内容。热门榜单与海量片库各自独立开关；每张库卡可单独「置顶」「隐藏」，虚拟库的显示顺序与相对本地库的位置可调；子账号能看哪些库在下方「子账号可见权限」里按人勾选。</p>

      <div class="card">
        <div class="card-title">① 热门资源（各类目真实榜单）</div>
        <p class="card-sub">海报 / 评分 / 排名来自豆瓣榜单，后台定时刷新、打开秒读缓存；
          点进某部片时才用片名实时搜索资源站解析播放。点击播放时<strong>优先高质量源</strong>。</p>
        <div class="lib-grid" id="hotLibs"></div>
        <label class="switch-row">
          <input type="checkbox" id="hotEnabled"/>
          <span>在飞牛影视媒体库显示热门四库
            <span class="sub">🎬热门电影 · 📺热门剧集 · 🌸动漫新番 · 🎤热门综艺（独立开关，不影响海量四库）</span></span>
        </label>
        <label class="field" style="max-width:240px">
          <span>榜单自动刷新间隔（小时，1–24）</span>
          <input type="number" id="hotInterval" min="1" max="24"/>
        </label>
        <div class="hint" id="hotStatus">等待加载状态…</div>
        <div class="btn-row">
          <button class="primary small" onclick="saveHotCfg()">保存热门设置</button>
          <button class="small" onclick="refreshHot()">立即刷新榜单</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title">② 海量片库（资源站分类深翻）</div>
        <p class="card-sub">直接按苹果 CMS 分类深翻资源站，跨源聚合成「🌐海量片库」四库（电影 / 剧集 / 动漫 / 综艺），规则不变。</p>
        <div class="lib-grid" id="catLibs"></div>
        <label class="switch-row">
          <input type="checkbox" id="catEnabled"/>
          <span>在飞牛影视媒体库显示海量四库
            <span class="sub">🎞海量电影 · 📺海量剧集 · 🌸海量动漫 · 🎤海量综艺（独立开关，不影响热门四库）</span></span>
        </label>
        <div class="btn-row">
          <button class="primary small" onclick="saveCatCfg()">保存海量设置</button>
        </div>
        <div class="hint">海量库在分类路由建立后自动出现；也可在「资源站」页把某个站单独生成一张媒体库卡片。</div>
      </div>

      <div class="card">
        <div class="card-title">③ 其他注入库</div>
        <p class="card-sub">独立源库 / 加固 / 小雅 / 红果短剧。每张卡可单独置顶或隐藏（隐藏后所有账号都看不到）。</p>
        <div class="lib-grid" id="otherLibs"></div>
      </div>

      <div class="card">
        <div class="card-title">④ 虚拟库位置与排序</div>
        <p class="card-sub">虚拟库在飞牛影视媒体库列表里的相对位置与相互顺序。选「插到本地库之后」时需再选一座本地库作锚点（本地库列表从飞牛读取，读不到时该选项不可用）。</p>
        <label class="field" style="max-width:280px">
          <span>虚拟库相对本地库的位置</span>
          <select id="libPlacement">
            <option value="front">排在本地库之前</option>
            <option value="back">排在本地库之后</option>
            <option value="after">插到指定本地库之后</option>
          </select>
        </label>
        <label class="field" style="max-width:280px">
          <span>锚点本地库（仅“插到指定本地库之后”时生效）</span>
          <select id="libAnchor"><option value="">— 未选择 —</option></select>
        </label>
        <div class="hint">顺序调整：每张库卡上的 ↑ ↓ 按钮会即时保存新顺序。</div>
        <div class="btn-row">
          <button class="primary small" onclick="savePlacement()">保存位置</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title">⑤ 子账号可见权限</div>
        <p class="card-sub">按飞牛账号勾选能看到的虚拟库；不勾选任何人表示该账号看不到任何虚拟库，点「恢复默认」取消该账号的限制。账号从飞牛官方数据库读取（只读），识别不到会话的账号按下方默认处理。</p>
        <label class="switch-row">
          <input type="checkbox" id="aclDefVis"/>
          <span>未设置过权限的账号默认能看到虚拟库
            <span class="sub">关掉后，未设置权限的账号默认看不到虚拟库；管理员始终可见，识别不到会话时按此开关处理</span></span>
        </label>
        <label class="switch-row">
          <input type="checkbox" id="aclDefOnline"/>
          <span>未设置过权限的账号默认能用在线搜索
            <span class="sub">关掉后，未设置权限的账号搜不到在线片源（只搜本地库）</span></span>
        </label>
        <div class="btn-row">
          <button class="primary small" onclick="saveAclDefaults()">保存默认权限</button>
        </div>
        <div id="acctList" style="margin-top:10px"></div>
      </div>

      <div class="card">
        <div class="card-title">🔞 福利（独立开关）</div>
        <p class="card-sub">仅在已勾选「福利分类」的资源站里浏览成人向片库。默认关闭：完全不建路由、不抓取、不显示。</p>
        <label class="switch-row">
          <input type="checkbox" id="welfareEnabled"/>
          <span>启用福利库
            <span class="sub">需配合「资源站」页的福利分类标记才生效</span></span>
        </label>
        <div class="btn-row">
          <button class="primary small" onclick="saveWelfareCfg()">保存扩展设置</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title">T4 选片</div>
        <div class="lib-grid" id="t4Libs"></div>
        <label class="switch-row">
          <input type="checkbox" id="t4Enabled" onchange="saveT4Cfg()"/>
          <span>在飞牛影视媒体库显示 T4 选片</span>
        </label>
      </div>
    </section>

    <!-- ============ 片源质量 ============ -->

    <section class="page" id="page-quality">
      <h2 class="page-title">片源质量优先级</h2>
      <p class="page-desc">热门资源点击搜源时，按「源质量权重 → 可播直链数」排序，优先选择高质量源。</p>
      <div class="card">
        <label class="switch-row">
          <input type="checkbox" id="preferQ" checked/>
          <span>热门资源优先高质量源
            <span class="sub">关闭后只按可播直链总数选择来源</span></span>
        </label>
        <div class="btn-row">
          <button class="primary small" onclick="savePreferQ()">保存</button>
        </div>
      </div>
      <div class="card">
        <div class="card-title">各源质量权重 <span class="badge" id="prioCount"></span>
          <button class="small" style="margin-left:auto" onclick="resetPriority()">恢复默认权重</button>
        </div>
        <p class="card-sub">数值越大越优先（-100–100，留空/0 用内省默认）。修改后点底部「保存权重」。</p>
        <table>
          <thead><tr><th>资源站</th><th style="width:120px">质量权重</th><th>相对优先级</th></tr></thead>
          <tbody id="prioRows"><tr><td colspan="3" class="muted">加载中…</td></tr></tbody>
        </table>
        <div class="btn-row"><button class="primary small" onclick="savePriority()">保存权重</button></div>
      </div>
    </section>

    <!-- ============ 资源站 ============ -->
    <section class="page" id="page-sources">
      <h2 class="page-title">资源站</h2>
      <p class="page-desc">标准苹果 CMS 源与扩展源；启用后进入聚合搜索与海量片库。</p>
      <div class="card">
        <div class="card-title">资源站列表 <span class="badge" id="srcCount"></span>
          <button class="small" style="margin-left:auto" onclick="testAll()">全部测速</button>
          <button class="small" onclick="cmsHealthCheck()" title="用热门词批量检测纯 CMS 源是否有效，失效写入健康画像触发熔断">CMS 批量体检</button>
          <button class="small" onclick="restoreBuiltin()">恢复内置源</button>
          <button class="small" onclick="exportCmsSources()" title="导出当前纯 CMS 源为 JSON 备份">⬇ 导出</button>
          <label class="btn-sm" style="border-color:var(--ok);background:var(--ok);color:#fff" title="从 JSON 文件导入 CMS 源，自动去重">
            ⬆ 导入<input type="file" id="cmsImportFile" accept=".json,application/json" style="display:none" onchange="importCmsFile(this)"/>
          </label>
        </div>
        <div class="row" style="margin:8px 0;gap:8px;flex-wrap:wrap;align-items:center">
          <input type="text" id="srcFilter" placeholder="筛选：名称 / 接口地址" style="flex:2;min-width:160px" oninput="onSrcFilter(this.value)"/>
          <select id="srcEnabledFilter" onchange="onSrcEnabledFilter(this.value)" style="max-width:130px">
            <option value="all">全部状态</option>
            <option value="on">仅已启用</option>
            <option value="off">仅已停用</option>
          </select>
          <span class="muted sm" id="srcPageInfo"></span>
          <button class="small" onclick="srcPage(-1)">上一页</button>
          <button class="small" onclick="srcPage(1)">下一页</button>
        </div>
        <div style="overflow-x:auto">
        <table>
          <thead><tr><th style="width:42px">启用</th><th style="width:96px">选项</th><th>资源站</th>
            <th style="width:150px">连通性</th><th style="width:70px">测试</th><th style="width:44px"></th></tr></thead>
          <tbody id="srcRows"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
        </table>
        </div>
        <div class="row mt" style="display:grid;grid-template-columns:150px 1fr 90px auto;gap:10px" id="addGrid">
          <input type="text" id="addName" placeholder="名称（可选）"/>
          <input type="text" id="addApi" placeholder="苹果CMS JSON 接口，如 https://xxx.com/api.php/provide/vod/at/json"/>
          <label style="display:flex;align-items:center;gap:4px;font-size:12px;cursor:pointer">
            <input type="checkbox" id="addStandalone" style="width:15px;height:15px;accent-color:var(--accent)"/>独立库</label>
          <button class="primary small" style="justify-content:center" onclick="addSource()">添加</button>
        </div>
        <div class="hint">「独立库」= 该站单独生成一张媒体库卡片；「非过滤」= 不按标题/备注过滤解说/预告/花絮；
          <!-- >>> PRIVATE:ADULT -->
          「🔞」= 把该独立库标记为限制级（18+）：与主库隔离、不进全局搜索，开启需二次确认；
          <!-- <<< PRIVATE:ADULT -->
          删除内置源仅隐藏，可恢复。批量导入 TVBox 订阅请用左侧「导入订阅」。</div>
        <div id="cmsHealthResult" class="muted sm" style="margin-top:8px"></div>
      </div>

      <!-- 资源站取源代理（移植上游 v0.2.46/55；后端 /v/api/ext/source-proxy 已在） -->
      <details class="card">
        <summary class="card-title" style="cursor:pointer">🌐 资源站取源代理（可选 · 直连慢时）</summary>
        <p class="card-sub" style="margin-top:8px">仅用于搜索、分类深翻、测速和播放地址解析；视频分片仍直连，不占代理带宽。留空即直连，不改变现有播放策略。</p>
        <div class="row">
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
            <input type="checkbox" id="srcProxyEnable" style="width:14px;height:14px;accent-color:var(--accent)" onchange="onSrcProxyToggle()"/> 已配置代理</label>
        </div>
        <div class="row">
          <input type="text" id="srcProxyUrl" placeholder="http://Clash:密码@192.168.x.x:7890" style="flex:2" disabled/>
          <button class="small primary" id="srcProxySaveBtn" onclick="saveSrcProxy(true)" disabled>保存并测速</button>
          <button class="small ghost danger" id="srcProxyClearBtn" onclick="clearSrcProxy()" disabled>清空</button>
        </div>
        <div class="seg" id="srcProxyMode" style="margin-top:10px" role="tablist">
          <button type="button" data-mode="auto" onclick="setSrcProxyMode('auto')">🪄 智能（推荐）</button>
          <button type="button" data-mode="on" onclick="setSrcProxyMode('on')">始终走代理</button>
          <button type="button" data-mode="off" onclick="setSrcProxyMode('off')">全部直连</button>
        </div>
        <div class="hint" id="srcProxyModeHint" style="margin-top:6px"></div>
        <div id="srcProxyMsg" class="muted sm" style="margin-top:6px"></div>
      </details>

      <div class="card">
        <div class="card-title">📡 T4 远程源 <span class="badge" id="t4Count"></span></div>
        <div class="hint">导入订阅时识别到的 type=4 远程源，已接入 T4 协议：在「T4 选片」媒体库中可搜可播。重新导入含 T4 的订阅会自动追加到这里；T4 源统一走选片库，不设独立媒体库。</div>
        <div id="t4Rows" style="margin-top:8px"><div class="muted sm">暂无 T4 源：去「导入订阅」导一次含 type=4 的订阅。</div></div>
      </div>

      <div class="card">
        <div class="card-title">源健康画像
          <span class="hint" style="font-weight:400">按真实播放探测统计成功率/延迟，死源自动降权 3 分钟</span>
          <button class="small" style="margin-left:auto" onclick="loadHealth()">刷新</button>
        </div>
        <table>
          <thead><tr><th>源</th><th style="width:80px">样本</th>
            <th style="width:110px">成功率</th><th style="width:100px">平均延迟</th>
            <th style="width:90px">状态</th><th style="width:70px">操作</th></tr></thead>
          <tbody id="healthRows"><tr><td colspan="6" class="muted">播放几次后自动生成。</td></tr></tbody>
        </table>
      </div>
    </section>

    <!-- ============ 导入 TVBox 订阅（独立页） ============ -->
    <section class="page" id="page-importsub">
      <h2 class="page-title">导入订阅</h2>
      <p class="page-desc">粘贴一个 TVBox / 影视仓 / OK影视 订阅 URL 或整段配置，自动识别并分类导入，<strong>导入即可在搜索与播放源中使用</strong>。支持 base64 与多订阅聚合。</p>
      <div class="card">
        <div class="card-title">订阅抓取代理（可选）
          <label style="float:right;display:flex;align-items:center;gap:6px;font-size:12px;font-weight:400;cursor:pointer">
            <input type="checkbox" id="subProxyEnable" style="width:14px;height:14px;accent-color:var(--accent)" onchange="onSubProxyToggle()"/> 启用代理</label></div>
        <p class="card-sub">仅用于<strong>拉取订阅配置、爬虫 jar、DRPY 脚本</strong>（GitHub 等被墙/解析失败时可走 Clash）。<strong>播放、搜索、CMS 探测一律不走此代理</strong>。形如 <code>http://用户:密码@主机:端口</code>，清空=直连。</p>
        <div class="row">
          <input type="text" id="subProxyUrl" placeholder="http://Clash:密码@192.168.x.x:7890" style="flex:2" disabled/>
          <button class="small" id="subProxySaveBtn" onclick="saveSubProxy(true)" disabled>保存并测试</button>
          <button class="small" id="subProxyClearBtn" onclick="clearSubProxy()" disabled>清空(直连)</button>
        </div>
        <div id="subProxyMsg" class="muted sm" style="margin-top:6px"></div>
      </div>
      <div class="card">
        <div class="card-title">🛡 播放去广告（m3u8 流层）
          <label style="float:right;display:flex;align-items:center;gap:6px;font-size:12px;font-weight:400;cursor:pointer">
            <input type="checkbox" id="adfEnable" style="width:14px;height:14px;accent-color:var(--accent)" onchange="toggleAdfilter(this.checked)"/> 启用去广告</label></div>
        <p class="card-sub">播放时实时检测当前线路并去除广告，兼容 TVBox / OK影视 Pro：<strong>① 动态检测</strong>——按分片间断边界、切片时长模式、CDN 切换、片头/片中位置实时识别广告段（无需订阅自带规则）；<strong>② 订阅 rules</strong>——导入订阅时自动收录其去广告正则，按线路域名精准剥除；<strong>③ 选集过滤</strong>——剔除“关注公众号/加微信/下载APP”假选集。识别不确定或可能误伤时自动保留，不影响播放。</p>
        <div style="margin:6px 0">
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
            <input type="checkbox" id="adfDynamic" style="width:14px;height:14px;accent-color:var(--accent)" onchange="toggleAdfilterDynamic(this.checked)"/>
            播放时动态检测（识别订阅未提供规则的广告段）</label>
        </div>
        <div style="margin:6px 0">
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
            <input type="checkbox" id="adfSps" style="width:14px;height:14px;accent-color:var(--accent)" onchange="toggleAdfilterSps(this.checked)"/>
            SPS 指纹检测（按视频 SPS 编码参数识别广告段；关闭则跳过该层检测）</label>
        </div>
        <div id="adfStatus" class="muted sm" style="margin-top:6px">正在加载去广告规则…</div>
      </div>
      <div class="card">
        <div class="card-title">TVBox / T4 订阅</div>
        <p class="card-sub">自动识别并分类：标准苹果CMS影视站（type 0/1）
          <!-- >>> PRIVATE:GUARD -->
          、<strong>Guard 加固秒播源</strong>、<strong>普通爬虫源（csp_，自动下载/补传 jar）</strong>（完整版，本地安卓运行时执行）
          <!-- <<< PRIVATE:GUARD -->
          <!-- >>> PRIVATE:DRPY -->
          与 <strong>远程 JS 爬虫源（DRPY）</strong>
          <!-- <<< PRIVATE:DRPY -->
          能直接播放的<strong>自动启用</strong>；P2P/磁力/云盘等慢源与 jar/脚本暂未就绪的源导入后默认关闭，可在结果里开启或补传；
          网盘/直播/无法识别的源跳过并说明原因。</p>
        <div class="row"><input type="text" id="subUrl" placeholder="配置订阅 URL（支持 T4 / base64 / 多订阅聚合）" style="flex:2"/>
          <button class="primary small" onclick="importSubscription('url')">按 URL 导入</button>
          <!-- >>> PRIVATE:SPIDER -->
          <button class="small" style="border-color:var(--accent);color:var(--accent)" onclick="importFantaiying()" title="官方维护、在线影视+网盘聚合；自动解密、智能跳过 x86 会崩溃的加固源">⚡ 一键饭太硬</button>
          <!-- <<< PRIVATE:SPIDER -->
        </div>
        <label class="field"><textarea id="subText" rows="7" placeholder="或直接粘贴配置 JSON（含 sites 数组）…"></textarea></label>
        <div class="btn-row">
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
            <input type="checkbox" id="subStandalone" style="width:15px;height:15px;accent-color:var(--accent)"/>
            苹果CMS源同时「作为独立媒体库」</label>
          <button class="primary small" onclick="importSubscription('text')">解析并导入</button>
        </div>
        <div id="subResult"></div>
      </div>

      <!-- >>> PRIVATE:SPIDER -->
      <div class="card" style="margin-top:14px">
        <div class="card-title">已导入的爬虫源 <button class="small" style="float:right" onclick="loadSpiderMgmt()">刷新</button></div>
        <p class="card-sub">普通 csp_ 爬虫按各自订阅的 jar 在本地安卓运行时执行。jar 自动下载失败时可在此手动补传，补传后非风险源自动启用。点「测试」可验证该源能否搜索、取详情，勾选试播还会尝试解析首集播放地址。</p>
        <div class="row" style="margin:4px 0 8px;gap:8px;align-items:center;flex-wrap:wrap">
          <input type="text" id="spProbeKw" value="庆余年" placeholder="测试关键词" style="max-width:150px"/>
          <label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">
            <input type="checkbox" id="spProbePlay" style="width:14px;height:14px;accent-color:var(--accent)"/>同时试播(首集)</label>
          <button class="small" id="spScanBtn" onclick="spScanStart()">一键全部体检</button>
          <button class="small" id="spDelBtn" onclick="spDeleteChecked()" style="display:none;border-color:var(--danger);color:var(--danger)">删除勾选失效源</button>
          <span class="muted sm">体检较慢（逐源真机调用）；默认仅勾选确认失效/报错的源，可手动调整。</span>
        </div>
        <div id="spScanBar" class="muted sm" style="display:none;margin:2px 0 8px"></div>
        <div id="spScanResult" style="margin-bottom:10px"></div>
        <div id="spiderMgmt"><div class="muted sm">点击「刷新」加载。</div></div>
      </div>
      <!-- <<< PRIVATE:SPIDER -->

      <!-- >>> PRIVATE:DRPY -->
      <div class="card" style="margin-top:14px">
        <div class="card-title">已导入的远程 JS 爬虫（DRPY）</div>
        <p class="card-sub">规则脚本来自订阅 URL，由本地 Node 运行时执行；脚本下载失败可重试。</p>
        <div id="drpyMgmt"><div class="muted sm">导入后自动显示。</div></div>
      </div>
      <!-- <<< PRIVATE:DRPY -->
    </section>

    <!-- >>> PRIVATE:GUARD -->
    <!-- ============ TVBox 加固（GUARD） ============ -->
    <section class="page" id="page-tvbox">
      <h2 class="page-title">TVBox 加固源</h2>
      <p class="page-desc">DexGuard 加固源在本地 redroid 外挂运行时中解密执行。一键部署会从 Docker Hub 拉取【公开、不含私有 jar】的运行时镜像，自动建容器并连接；私有加固 jar 仅随完整版插件下发、本机灌入。仅完整版。</p>

      <div class="card">
        <div class="card-title">外挂运行时（JVM / Node / redroid 安卓容器）
          <span class="pill off" id="androidPill">Android</span>
          <span class="muted sm" id="guardMeta">检测中…</span>
        </div>
        <div id="sidecarDeps" class="row sm muted" style="gap:8px">检测中…</div>
        <div class="btn-row">
          <button class="primary small" onclick="sidecarInstall('all')" id="scInstallAll">一键部署外挂运行时（自动拉镜像并连接）</button>
          <button class="small" onclick="sidecarInstall('runner-only')">仅部署 JVM/JS</button>
          <button class="small" onclick="sidecarInstall('android-only')">仅预置 redroid</button>
          <button class="small" onclick="sidecarReconnect()">重接 bridge</button>
        </div>
        <div class="hint" id="sidecarState"></div>
        <div class="card" style="margin:10px 0 0;background:#fafbfc">
          <div class="card-title" style="font-size:14px">Android 加固运行时（redroid 轻量容器 · 开机自启常驻）
            <span class="pill" id="emuStatePill">—</span></div>
          <p class="muted sm" id="emuState" style="margin:4px 0">检测中…</p>
          <div class="btn-row">
            <button class="small" onclick="androidStart()" id="emuStartBtn">▶ 启动运行时</button>
            <button class="small" onclick="androidStop()" id="emuStopBtn">■ 停止并释放内存</button>
          </div>
          <div class="hint" id="emuHint">redroid 为轻量容器化 Android（约 0.7–0.9GB，冷启约 10–30 秒，含 ARM 翻译）。<b>默认开机自启并常驻</b>：宿主重启或容器异常退出会自动拉起，随时可播加固源；不需要时点「停止并释放内存」即可，停止后不再自启，直到再次点「启动运行时」。JVM/JS 源常驻、不依赖安卓容器。</div>
        </div>
        <details><summary class="muted sm" style="cursor:pointer">安装日志</summary>
          <pre class="log" id="sidecarLog"></pre></details>
        <div class="hint">跨机部署：在装好完整版插件的其他飞牛机器点「一键部署」即可自动拉镜像/建容器/连接（首次约 2.1GB）。redroid 需内核 binderfs（fnOS 6.18 已内建，脚本自动挂载），无需传统虚拟化。公开镜像不含任何第三方私有 jar。</div>
      </div>

      <div class="card">
        <div class="card-title">导入加固源</div>
        <p class="card-sub">csp_*Guard 加固源已与苹果CMS源合并到<strong>统一订阅导入</strong>：粘贴一份 TVBox 订阅即可自动识别，
          能播的加固源自动启用，P2P/磁力/云盘等风险源默认关闭，可在导入报告里单独开启。本页只负责执行 jar、安卓运行时与已导入加固源的管理。</p>
        <button class="primary small" onclick="switchPage('importsub')">前往统一导入（导入订阅页）</button>
      </div>

      <div class="card">
        <div class="card-title">媒体库展示方式</div>
        <label class="switch-row">
          <input type="checkbox" id="guardZoneEnabled" onchange="saveGuardZone()"/>
          <span>把每个启用的加固源生成一张独立「专区媒体库」卡片
            <span class="sub">默认关闭：比特 / 糯米 / 花花等加固源只作为<b>播放时可切换的线路</b>和搜索源，不在媒体库里产生（常为空的）专区库。需要海报墙专区时再打开。</span></span>
        </label>
        <label class="switch-row">
          <input type="checkbox" id="guardNativeEnabled" onchange="saveGuardNative()"/>
          <span>在飞牛影视里注入单张「🛡加固影视」聚合媒体库
            <span class="sub">默认开启：后台预取各加固源首页推荐，在飞牛 App / TV 原生媒体库里生成一张可直接点播的聚合海报墙（秒开、不卡）。需要按源/分类精细浏览时用本页下方的「加固影视浏览」。</span></span>
        </label>
      </div>

      <div class="card">
        <div class="card-title">加固执行 jar（自备）</div>
        <p class="card-sub">运行 csp_*Guard 加固源需要对应的兼容 spider jar。安装包不附带任何第三方 jar，请选择你自己有权使用的 <b>.jar</b> 上传；上传后自动下发到 Android 运行时（容器未启动则下次启动时自动下发）。jar 仅保存在本机，不会被打包或上传到任何公共镜像。</p>
        <div class="row">
          <input type="file" id="guardJarFile" accept=".jar,application/java-archive,application/zip" style="flex:2"/>
          <button class="primary small" onclick="uploadGuardJar()">上传并下发</button>
        </div>
        <div class="hint" id="guardJarMsg">检测 jar 状态中…</div>
      </div>

      <div class="card">
        <div class="card-title">加固源列表
          <input type="text" id="guardFilter" placeholder="筛选名称…" oninput="renderGuardRows()"
                 style="max-width:170px;margin-left:auto"/></div>
        <table>
          <thead><tr><th>加固源</th><th style="width:70px">可搜索</th><th style="width:64px">类型</th>
            <th style="width:64px">启用</th><th style="width:60px">操作</th></tr></thead>
          <tbody id="guardRows"><tr><td colspan="5" class="muted">连接 spider-runner 后显示</td></tr></tbody>
        </table>
      </div>

      <div class="card">
        <div class="card-title">🎬 加固影视浏览
          <span class="muted sm" style="font-weight:400">直接浏览各加固源分类片单，点海报即播（首次打开需唤醒安卓运行时，可能等待十几秒）</span>
        </div>
        <div class="row sm" style="gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
          <button class="small primary" id="gbHotBtn" onclick="gbLoadHot()">🔥 聚合热门</button>
          <select id="gbSource" class="input" style="max-width:180px" onchange="gbOnSource()"></select>
          <select id="gbClass" class="input" style="max-width:150px" onchange="gbOnClass()"></select>
          <span class="muted sm" id="gbMsg" style="margin-left:auto"></span>
        </div>
        <div id="gbGrid" class="gb-grid"></div>
        <div class="btn-row" id="gbPager" style="display:none">
          <button class="small" id="gbPrev" onclick="gbPage(-1)">← 上一页</button>
          <span class="muted sm" id="gbPageNo">1</span>
          <button class="small" id="gbNext" onclick="gbPage(1)">下一页 →</button>
        </div>
      </div>
    </section>

    <!-- 加固影视页内播放器 -->
    <div id="gbPlayerMask" class="gb-mask" style="display:none">
      <div class="gb-player">
        <div class="gb-player-bar">
          <b id="gbPlayTitle" style="margin-right:8px"></b>
          <span class="muted sm" id="gbPlaySite"></span>
          <button class="small" style="margin-left:auto" onclick="gbClosePlayer()">关闭 ✕</button>
        </div>
        <video id="gbVideo" controls autoplay playsinline style="width:100%;max-height:70vh;background:#000"></video>
        <div class="hint" style="padding:6px 2px">仅本机浏览器播放；部分源有防盗链，若黑屏可换一个源。</div>
      </div>
    </div>

    <!-- ============ 网盘（夸克转存播放，仅完整版） ============ -->
    <section class="page" id="page-pan">
      <h2 class="page-title">网盘播放</h2>
      <p class="page-desc">登录网盘后，资源站里的网盘分享链接（夸克 / UC / 阿里 / 百度 / 迅雷 / 115 / 天翼 / 移动，以及「四盘」聚合源）会被自动识别；已适配的网盘可转存原画播放，不占用安卓加固运行时。凭据仅保存在本机 NAS。</p>
      <div class="card">
        <div id="panFrameWrap" style="border-radius:10px;overflow:hidden;border:1px solid #e5e7eb;background:#fff">
          <iframe id="panFrame" title="网盘登录" data-src="/v/api/ext/pan/page"
                  style="width:100%;height:780px;border:0;display:block;background:#fff"></iframe>
        </div>
      </div>
    </section>
    <!-- <<< PRIVATE:GUARD -->

    <!-- >>> PRIVATE:CATVOD -->
    <!-- ============ 小雅 / JS（CATVOD） ============ -->
    <section class="page" id="page-xiaoya">
      <h2 class="page-title">私有爬虫运行时</h2>
      <p class="page-desc">小雅网盘（catvod JVM 爬虫）与 drpyS JS 爬虫（Node 运行时）。仅完整版本地运行。</p>
      <div class="card">
        <div class="card-title">运行时
          <span class="pill off" id="jvmPill">JVM</span>
          <span class="pill off" id="nodePill">Node(JS)</span></div>
        <h3 style="font-size:14px;margin:6px 0">小雅网盘（Alist 网盘聚合）</h3>
        <div class="row">
          <input type="text" id="xyServer" placeholder="小雅服务器，如 http://192.168.x.x:5678" style="flex:2"/>
          <input type="text" id="xyUser" placeholder="账号" style="max-width:130px"/>
          <input type="password" id="xyPass" placeholder="密码（留空不改）" style="max-width:170px" autocomplete="new-password"/>
          <button class="primary small" onclick="saveXiaoya()">保存</button>
          <button class="small" onclick="browseXiaoya()">连通并载入目录</button>
        </div>
        <div class="hint" id="xyMsg"></div>
        <div class="row"><h3 style="font-size:14px;margin:6px 0">目录浏览</h3>
          <button class="small" onclick="xyCrumbUp()">上级</button>
          <span id="xyCrumb" class="muted sm"></span></div>
        <table>
          <thead><tr><th>分类 / 文件夹</th><th style="width:80px">操作</th></tr></thead>
          <tbody id="xyRows"><tr><td colspan="2" class="muted">保存并连接后显示分类</td></tr></tbody>
        </table>
      </div>
      <div class="card">
        <div class="card-title">小雅分类媒体库</div>
        <label class="switch-row"><input type="checkbox" id="panLibEnabled"/>
          <span>按小雅目录分类生成媒体库<span class="sub">在「媒体库」新增 电影/剧集/综艺/动漫/纪录片/短剧 多张卡片，后台增量索引并逐步补全海报；播放为网盘 4K/原盘文件，客户端直连网盘 CDN</span></span></label>
        <div class="hint" id="panLibMsg"></div>
        <div class="btn-row"><button class="primary small" onclick="savePanLib()">保存</button></div>
      </div>
    </section>
    <!-- <<< PRIVATE:CATVOD -->

    <!-- ============ 搜索 ============ -->
    <section class="page" id="page-search">
      <h2 class="page-title">搜索设置</h2>
      <div class="card">
        <label class="switch-row"><input type="checkbox" id="onlineFirst"/>
          <span>在线资源结果前排展示<span class="sub">在线片库更齐全；关闭则本地优先</span></span></label>
        <label class="field" style="max-width:240px"><span>每个关键词最多注入条数</span>
          <input type="number" id="maxn" min="1" max="50" style="width:100%"/></label>
        <label class="switch-row"><input type="checkbox" id="junk"/>
          <span>按关键词过滤噪音<span class="sub">标题或备注命中噪音词则不注入</span></span></label>
        <label class="field"><span>噪音关键词 <span id="kwN" class="muted"></span>
          <span style="float:right">
            <button class="small" type="button" onclick="resetKw()">恢复默认</button>
            <button class="small" type="button" onclick="toggleKwEdit()">批量编辑</button>
          </span></span>
          <div id="kwChips" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px"></div>
          <div id="kwEdit" style="display:none;margin-bottom:8px">
            <textarea id="kwArea" rows="4" placeholder="每行一个，或逗号/顿号分隔，如：解说,预告片,花絮,枪版"></textarea>
            <button class="small" type="button" onclick="applyKwText()" style="margin-top:6px">应用文本</button>
          </div>
          <div class="row"><input type="text" id="kwAdd" placeholder="添加关键词后回车" style="flex:1"/>
            <button class="small" type="button" onclick="addKw()">添加</button></div>
        </label>
        <div class="btn-row"><button class="primary small" onclick="saveCfg()">保存设置</button></div>
      </div>

      <div class="card">
        <div class="card-title">播放线路</div>
        <label class="switch-row"><input type="checkbox" id="lineFastest"/>
          <span>自动选最快线路<span class="sub">起播时并行测速、自动选首字节最快的线路；同一集短时间内固定用它</span></span></label>
        <label class="field" style="max-width:260px"><span>参与测速的线路数</span>
          <input type="number" id="fastestMax" min="2" max="12" style="width:100%"/></label>
        <label class="field" style="max-width:260px"><span>测速超时（秒）</span>
          <input type="number" id="fastestTimeout" min="1" max="10" step="0.5" style="width:100%"/></label>
        <label class="switch-row"><input type="checkbox" id="lineAutofail"/>
          <span>坏线自动切换<span class="sub">当前线路失效/文件不存在时自动换到可用线路，并短时跳过坏线（关闭则纯手动切线）</span></span></label>
        <label class="switch-row"><input type="checkbox" id="fixedLineDirect"/>
          <span>手动选线直接出流<span class="sub">手动选定某条线路后直接播放，不再先探活；已知坏线仍自动换到可用线路，自动起播不受影响</span></span></label>
        <label class="switch-row"><input type="checkbox" id="linePredict"/>
          <span>本片线路预测<span class="sub">浏览海报时对首屏影片的少数候选线做轻量探活，起播优先用已探到的最快线（只作竞速种子，不固定线路）</span></span></label>
        <label class="field" style="max-width:260px"><span>预测线路数（1-3）</span>
          <input type="number" id="linePredictMax" min="1" max="3" style="width:100%"/></label>
        <label class="field" style="max-width:260px"><span>预测缓存有效期（分钟，5-120）</span>
          <input type="number" id="linePredictTtl" min="5" max="120" style="width:100%"/></label>
        <div class="btn-row"><button class="primary small" onclick="saveLines()">保存播放设置</button>
          <button class="small" onclick="saveLinePredict()">保存预测设置</button>
          <button class="small" onclick="resetLinePredict()">清空预测缓存</button></div>
      </div>

      <div class="card">
        <div class="card-title">已缓存在线条目 <span class="badge" id="cacheN"></span></div>
        <div class="row"><input type="text" id="kw" placeholder="按标题过滤（在飞牛搜索过的会出现在这里）" style="flex:1"/>
          <button class="small" onclick="loadOnline()">刷新</button></div>
        <table style="margin-top:10px">
          <thead><tr><th>标题</th><th style="width:100px">来源</th><th style="width:70px">集数</th></tr></thead>
          <tbody id="rows"><tr><td colspan="3" class="muted">加载中…</td></tr></tbody>
        </table>
      </div>

      <!-- 运行缓存管理（移植上游 v0.2.54） -->
      <div class="card">
        <div class="card-title">🗑 运行缓存管理
          <span class="hint" style="font-weight:400">缓存异常或占用过大时清除，后续访问自动重建</span>
          <button class="small" style="margin-left:auto" onclick="loadCacheStatus()">刷新</button>
        </div>
        <div id="cacheGrid" class="cache-grid"><div class="muted sm">读取中…</div></div>
        <div class="btn-row">
          <button class="small danger" onclick="clearCaches(['search'])">仅清搜索缓存</button>
          <button class="small danger" onclick="clearCaches(['charts','catalog'])">清榜单+海量库</button>
          <button class="small danger" onclick="clearCaches(['search','charts','catalog'])">一键清运行缓存（不重启）</button>
          <button class="small danger" onclick="clearCaches(['health'])">重置源健康画像</button>
          <button class="small danger" onclick="clearCaches(['titles'])">清在线资源缓存（将重启）</button>
        </div>
      </div>
    </section>

    <!-- ============ 关于 ============ -->
    <section class="page" id="page-about">
      <h2 class="page-title">关于</h2>
      <div class="card">
        <div class="card-title">🎬 fnmedia-ext · 飞牛影视在线扩展</div>
        <p class="card-sub">在原生搜索 / 媒体库注入在线影视并直接调用飞牛原生播放器播放。</p>
        <div class="stack sm muted">
          <div>开发作者：<a href="https://github.com/funwukong/fnmedia-ext" target="_blank" rel="noopener">funwukong</a></div>
        </div>
        <div class="stack sm muted">
          <div>· 热门资源：豆瓣真实榜单四库，点击搜源、优先高质量源播放。</div>
          <div>· 海量片库：苹果 CMS 分类深翻，跨源聚合成海量四库与单源独立库。</div>
          <!-- >>> PRIVATE:GUARD -->
          <div>· TVBox 加固源：本地 Android 外挂运行时执行 DexGuard 源（完整版）。</div>
          <!-- <<< PRIVATE:GUARD -->
        </div>
        <div class="hint" id="aboutEdition"></div>
      </div>

      <div class="card" id="updCard">
        <div class="card-title">🔄 版本更新</div>
        <div class="stack sm">
          <div class="muted">当前版本：<b id="updCur">…</b><span id="updEdition" class="muted"></span></div>
          <div id="updBody" class="muted">正在检查更新…</div>
          <div id="updActions" style="display:none;gap:8px;flex-wrap:wrap;align-items:center">
            <button class="btn primary" id="updBtn">立即更新</button>
            <button class="btn" id="updRefresh">重新检查</button>
            <a class="btn" id="updLink" target="_blank" rel="noopener" style="text-decoration:none">查看 Release</a>
          </div>
          <div id="updProgress" class="hint" style="display:none"></div>
          <details style="margin-top:4px">
            <summary class="muted sm" style="cursor:pointer">下载慢 / 直连失败？点此设置更新代理（可选）</summary>
            <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">
              <input id="updProxy" type="text" class="input" placeholder="http://用户:密码@代理IP:端口（仅用于下载更新，留空直连）"
                     style="flex:1;min-width:240px">
              <button class="btn" id="updProxySave">保存代理</button>
            </div>
            <div class="hint">仅本机保存、不随安装包分发；保存后点“重新检查/立即更新”生效。</div>
          </details>
        </div>
      </div>

      <div class="card" id="tokenCard">
        <div class="card-title">🔑 管理令牌
          <button class="small" style="margin-left:auto" onclick="loadAdminToken()">刷新</button></div>
        <p class="card-sub">登录校验用的令牌：新令牌 256 位；轮换后旧令牌在宽限期（默认 24 小时）内仍可用，宽限期过后失效。老令牌升级后继续可用；默认不按年龄强制过期（久未登录也不会被锁死）。令牌原文只在轮换成功那一次显示，请立即复制保存。</p>
        <div class="stack sm">
          <div>当前令牌：<code id="tokMask">…</code> <span class="muted" id="tokBits"></span></div>
          <div id="tokAge" class="muted">读取中…</div>
          <div id="tokGrace" class="muted"></div>
          <div id="tokLimit" class="muted"></div>
          <div id="tokNewWrap" style="display:none">
            <div style="color:var(--warn,#b45309);font-weight:600">新令牌（只显示一次，请立即复制保存）：</div>
            <div class="row"><input type="text" id="tokNew" readonly style="flex:1" onclick="this.select()"/>
              <button class="small" onclick="copyNewToken()">复制</button></div>
          </div>
          <div class="row"><input type="password" id="tokTest" placeholder="粘贴令牌，验证是否有效" style="flex:1" autocomplete="off"/>
            <button class="small" onclick="testAdminToken()">验证令牌</button></div>
          <div id="tokTestMsg" class="hint"></div>
          <div class="btn-row"><button class="small danger" onclick="rotateAdminToken()">重新生成令牌</button></div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">⚖️ 免责声明与版权声明</div>
        <div class="stack sm muted" style="line-height:1.7">
          <div><b>1. 工具性质：</b>本扩展是<b>开源、免费</b>的技术研究与个人媒体库整理工具，仅在你自己的 NAS / 设备上本地运行。它是一个中立的“播放器 / 接口聚合”技术框架，本身<b>不提供、不存储、不上传、不分发、不转码</b>任何影视、图片、字幕、播放地址或资源站点信息，也<b>不内置任何资源数据库、订阅、站点地址、爬虫或执行 jar</b>。</div>
          <div><b>2. 不内置源、由你自备：</b>所有在线内容均来自你<b>自行添加</b>的第三方接口、TVBox 订阅，或你本人上传的脚本 / 执行 jar；是否添加、添加什么、是否启用，完全由你决定并自行承担。请仅使用你拥有合法授权或有权访问的内容与组件。</div>
          <div><b>3. 版权归原权利人：</b>第三方接口返回内容的版权归原权利人所有。本项目不依附、不代表任何内容提供方，不对任何第三方内容的合法性、准确性、可用性负责；如涉及版权问题，请联系内容实际提供方处理。</div>
          <div><b>4. 仅供学习交流：</b>本工具仅供学习、研究与个人合法播放自有或已获授权内容使用，<b>不得用于商业用途，不得下载、录制、再传播任何侵权内容</b>。请遵守你所在地的法律法规。</div>
          <div><b>5. 风险与责任：</b>第三方接口可能失效、变更或包含不当内容；使用过程中产生的流量、账号、费用与一切法律责任均由<b>使用者本人</b>承担，与本工具作者无关。请合法、自律使用，并妥善管理设备与访问权限，避免 18 周岁以下人员接触不适宜内容。</div>
          <div><b>6. 侵权处理：</b>如任何权利人认为本工具的某项技术实现涉及其合法权益，可通过<a href="https://github.com/funwukong/fnmedia-ext" target="_blank" rel="noopener">项目仓库</a> Issue 联系作者 funwukong，核实后将及时处理。</div>
        </div>
      </div>
    </section>
  </main>
</div>

<nav class="bottom-nav" id="bottomNav">
  <button class="nav-item active" data-page="overview">概览</button>
  <button class="nav-item" data-page="library">媒体库</button>
  <button class="nav-item" data-page="quality">质量</button>
  <button class="nav-item" data-page="sources">资源站</button>
  <!-- >>> PRIVATE:GUARD -->
  <button class="nav-item" data-page="tvbox">加固</button>
  <button class="nav-item" data-page="pan" data-pan-nav style="display:none">网盘</button>
  <!-- <<< PRIVATE:GUARD -->
  <button class="nav-item" data-page="search">搜索</button>
</nav>

<div id="toast"></div>

<script>
const $=(id)=>document.getElementById(id);
const $$=(sel)=>Array.from(document.querySelectorAll(sel));
let CFG=null,KWS=[],STATUS=null,PRIO={};
// >>> PRIVATE:FEATURE_FLAGS
const FEAT_DDUO=true,FEAT_ADULT=true;
// <<< PRIVATE:FEATURE_FLAGS

function toast(m,kind){const t=$("toast");t.textContent=m;t.className="show "+(kind==="fail"?"fail":"ok");
  clearTimeout(window._tt);window._tt=setTimeout(()=>t.className="",2200);}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
async function jget(p,timeoutMs){const ctrl=new AbortController();
  const t=setTimeout(()=>ctrl.abort(),timeoutMs||15000);
  try{const r=await fetch(p,{signal:ctrl.signal});if(!r.ok)throw new Error(p+" "+r.status);return r.json();}
  finally{clearTimeout(t);}}
async function jpost(p,b){const r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(b||{})});return r.json();}

/* ---------- 页面切换 ---------- */
async function switchPage(page){
  $$(".page").forEach(el=>el.classList.toggle("active",el.id==="page-"+page));
  $$("[data-page]").forEach(el=>el.classList.toggle("active",el.dataset.page===page));
  window.scrollTo(0,0);
  if(page==="overview"){renderOverview();startLivePoll();}
  else{stopLivePoll();}
  if(page==="library"){loadLibs().then(renderLibrary);}
  if(page==="quality")renderQuality();
  if(page==="sources"){try{loadHealth();}catch(e){}try{initSrcProxy();}catch(e){}}
  if(page==="search"){try{loadCacheStatus();}catch(e){}}
  if(page==="tvbox"){try{loadGuardZone();}catch(e){}try{gbInit();}catch(e){}}
  // >>> PRIVATE:GUARD
  if(page==="pan"){const f=$("panFrame");if(f&&!f.src)f.src=f.dataset.src;}
  // <<< PRIVATE:GUARD
  if(page==="importsub"){try{if(!CFG)await loadStatus();}catch(e){}try{if(typeof initSubProxy==="function")initSubProxy();}catch(e){}try{loadAdfilter();}catch(e){}try{if(typeof loadSpiderMgmt==="function")loadSpiderMgmt();}catch(e){}try{if(typeof loadDrpyMgmt==="function")loadDrpyMgmt();}catch(e){}}
  if(page==="about"){loadUpdate(false);try{loadAdminToken();}catch(e){}}
}
$$("[data-page]").forEach(b=>b.addEventListener("click",()=>switchPage(b.dataset.page)));

/* ---------- 版本更新 ---------- */
let UPD_TIMER=null;
function $u(id){return document.getElementById(id);}
function updEdLabel(ed){return ed==="full"?"全功能版":"轻量版";}
async function loadUpdate(force){
  const rf=$u("updRefresh"),body=$u("updBody");
  if(force){
    if(rf){rf.disabled=true;rf.textContent="检查中…";}
    if(body)body.innerHTML='<span style="color:var(--ok,#16a34a)">正在向 GitHub 检查更新…</span>';
  }
  try{
    const r=await jget("/v/api/ext/update/check"+(force?"?force=1":""));
    renderUpdate(r);
    if(force){
      const u=r.update;
      if(r.status==="error")toast("检查失败："+(r.last_error||"无法访问 GitHub"),"fail");
      else if(u&&u.update_available)toast("发现新版本 v"+u.version);
      else toast("检查完成：已是最新版本"+(u&&u.version?" v"+u.version:""));
    }
  }catch(e){
    $u("updCur").textContent="—";
    body.innerHTML='<span style="color:var(--warn,#b45309)">检查更新失败：'+esc(e.message)+'（可能无法访问 GitHub）</span>';
    $u("updActions").style.display="flex";$u("updBtn").style.display="none";
    if(force)toast("检查更新失败："+e.message,"fail");
  }finally{
    if(rf){rf.disabled=false;rf.textContent="重新检查";}
  }
}
function renderUpdate(r){
  $u("updCur").textContent=r.current_version||"—";
  $u("updEdition").textContent=" · "+updEdLabel(r.edition);
  const u=r.update;
  const busy=["checking","downloading","installing"].includes(r.status);
  const body=$u("updBody"),actions=$u("updActions"),btn=$u("updBtn"),
        prog=$u("updProgress"),link=$u("updLink");
  if(link){if(u&&u.html_url){link.href=u.html_url;link.style.display="";}else{link.style.display="none";}}
  if(busy){
    body.innerHTML='<span style="color:var(--ok,#16a34a)">'+esc(r.progress||"处理中…")+'</span>';
    actions.style.display="flex";btn.style.display="none";
    prog.style.display="";prog.textContent="请稍候，安装完成后服务会自动重启，管理页稍后刷新即可。";
    return;
  }
  if(r.status==="done"){
    body.innerHTML='<span style="color:var(--ok,#16a34a)">✅ 已更新到 v'+esc(r.updated_version||"")+'，服务正在重启…请稍后刷新页面。</span>';
    actions.style.display="flex";btn.style.display="none";prog.style.display="none";return;
  }
  if(r.status==="error" && (!u || !u.update_available)){
    body.innerHTML='<span style="color:var(--warn,#b45309)">'+esc(r.last_error||"检查更新失败")+'</span>';
    actions.style.display="flex";btn.style.display="none";prog.style.display="none";return;
  }
  if(u && u.update_available){
    const edTip=u.edition==="full"?"（全功能版）":"（轻量版）";
    const size=u.asset_size?` · ${(u.asset_size/1048576).toFixed(1)}MB`:"";
    body.innerHTML='<div><span style="color:var(--ok,#16a34a);font-weight:600">发现新版本 v'+esc(u.version)+edTip+'</span>'+size+'</div>'
      +'<div class="muted sm" style="white-space:pre-wrap;margin-top:6px;max-height:180px;overflow:auto">'+esc((u.notes||"").slice(0,1200))+'</div>';
    actions.style.display="flex";btn.style.display="";btn.disabled=false;btn.textContent="立即更新到 v"+u.version;
    prog.style.display="none";
  }else{
    const v=u&&u.version?("v"+u.version):"";
    const lc=r.last_check?(' · 上次检查 ' + new Date(r.last_check*1000).toLocaleString()):"";
    body.innerHTML='<span style="color:var(--muted,#666)">已是最新版本'+(v?"（GitHub 最新 "+v+"）":"")+'。</span>'
      +'<div class="hint sm" style="margin-top:4px">'+lc+'</div>';
    actions.style.display="flex";btn.style.display="none";prog.style.display="none";
  }
}
async function pollUpdate(){
  try{
    const r=await jget("/v/api/ext/update/status");
    renderUpdate(r);
    if(["downloading","installing"].includes(r.status)){
      UPD_TIMER=setTimeout(pollUpdate,1500);
    }else{UPD_TIMER=null;}
  }catch(e){}
}
document.addEventListener("DOMContentLoaded",()=>{
  const btn=document.getElementById("updBtn"),rf=document.getElementById("updRefresh");
  if(btn)btn.addEventListener("click",async()=>{
    if(!confirm("将从 GitHub 下载与当前版本类型一致的安装包（自动校验 sha256），并通过飞牛应用中心覆盖安装。\n\n安装期间本服务会短暂停止并自动重启（约十几秒），播放记录与已导入源会保留。\n\n确定立即更新？"))return;
    btn.disabled=true;btn.textContent="开始更新…";
    try{
      const rr=await fetch("/v/api/ext/update/now",{method:"POST",
        headers:{"Content-Type":"application/json","x-fnmedia-admin":"1"},
        body:JSON.stringify({confirm:true})});
      const r=await rr.json();
      if(r.code!==0){alert(r.msg||"更新启动失败");btn.disabled=false;loadUpdate(true);return;}
      pollUpdate();
    }catch(e){alert("更新启动失败："+e.message);btn.disabled=false;}
  });
  if(rf)rf.addEventListener("click",()=>loadUpdate(true));
  const ps=document.getElementById("updProxySave");
  if(ps)ps.addEventListener("click",async()=>{
    const v=(document.getElementById("updProxy").value||"").trim();
    if(v && !/^(https?|socks5h?):\/\//.test(v)){alert("代理地址需以 http:// 或 https:// 或 socks5:// 开头");return;}
    try{
      const r=await jpost("/v/api/ext/config",{update_proxy:v});
      if(r&&r.ok!==false){ps.textContent="已保存 ✓";setTimeout(()=>ps.textContent="保存代理",1500);loadUpdate(true);}
      else{alert("保存失败");}
    }catch(e){alert("保存失败："+e.message);}
  });
  // 回填已保存的更新代理
  (async()=>{try{
    const r=await jget("/v/api/ext/status");
    const inp=document.getElementById("updProxy");
    if(inp&&r.config&&r.config.update_proxy)inp.value=r.config.update_proxy;
  }catch(e){}})();
});

/* ---------- 管理令牌 ---------- */
function fmtTokAge(sec){
  if(sec==null)return "创建时间未知（老令牌，升级时起算）";
  if(sec<3600)return Math.max(0,Math.floor(sec/60))+" 分钟前创建";
  if(sec<86400)return (sec/3600).toFixed(1)+" 小时前创建";
  return (sec/86400).toFixed(1)+" 天前创建";
}
async function loadAdminToken(){
  const ageEl=$("tokAge");if(!ageEl)return;
  try{
    const r=await jget("/v/api/ext/admin/token");const d=r.data||r;
    $("tokMask").textContent=d.masked||"(未配置)";
    $("tokBits").textContent=d.length?("共 "+d.length+" 字符 · 约 "+d.bits+" 位"):"";
    let age=fmtTokAge(d.age_seconds);
    if(d.expires_at){age+="；有效期至 "+new Date(d.expires_at*1000).toLocaleString()
      +(d.expired?"（已过期）":"");}
    else{age+="；不按年龄强制过期";}
    ageEl.textContent=age;
    $("tokGrace").textContent=d.has_previous
      ?("旧令牌宽限期剩余 "+Math.floor((d.grace_remaining_seconds||0)/3600)+" 小时 "
        +Math.floor(((d.grace_remaining_seconds||0)%3600)/60)+" 分钟")
      :"无轮换中的旧令牌。";
    $("tokLimit").textContent="登录限流："+(d.window_seconds||900)+" 秒内连续失败 "
      +(d.max_failures||5)+" 次后锁定 "+Math.round((d.lockout_seconds||900)/60)+" 分钟。";
  }catch(e){ageEl.textContent="令牌状态读取失败："+e.message;}
}
async function rotateAdminToken(){
  if(!confirm("将重新生成管理令牌（256 位）。\n\n旧令牌在宽限期（默认 24 小时）内仍可用，之后失效。\n新令牌只在成功后显示一次，请立即复制保存。\n\n确定重新生成？"))return;
  try{
    const r=await jpost("/v/api/ext/admin/token/rotate",{confirm:true});
    if(r.code!==0){toast(r.msg||"重新生成失败","fail");return;}
    const w=$("tokNewWrap");w.style.display="";
    const inp=$("tokNew");inp.value=r.data.new_token||"";inp.select();
    toast("已重新生成，请立即复制保存新令牌");
    loadAdminToken();
  }catch(e){toast("重新生成失败："+e.message,"fail");}
}
function copyNewToken(){
  const inp=$("tokNew");if(!inp||!inp.value)return;
  inp.select();
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(inp.value).then(()=>toast("已复制"),()=>toast("复制失败，请手动选中复制","fail"));}
  else{document.execCommand("copy");toast("已复制");}
}
async function testAdminToken(){
  const v=($("tokTest").value||"").trim();const m=$("tokTestMsg");
  if(!v){m.textContent="请先粘贴令牌。";return;}
  try{
    const r=await jpost("/v/api/ext/admin/login",{token:v});
    if(r.code===0){m.textContent="✅ 令牌有效"+(r.data&&r.data.via==="grace"?"（宽限期内旧令牌）":"")+"。";$("tokTest").value="";}
    else{m.textContent="❌ "+(r.msg||"令牌不正确")
      +(r.data&&r.data.retry_after_seconds?("，请 "+r.data.retry_after_seconds+" 秒后再试"):"");}
    loadAdminToken();
  }catch(e){m.textContent="验证失败："+e.message;}
}

/* ---------- 概览 ---------- */
/* ---------- 本设备识别（自检） ---------- */
async function loadWhoami(){
  try{
    const r=await jget("/v/api/ext/whoami");const d=r.data||{};
    const kindMap={tv:"TV / 电视盒子（严格模型）",android:"Android 手机/平板",
      ios:"iPhone / iPad",web:"网页版",unknown:"未识别（按宽松处理）"};
    $("ovClientKind").textContent=d.label||"-";
    $("ovClient").textContent=(kindMap[d.kind]||d.kind)
      +(d.strict_card?" · 已启用严格字段规整":" · 宽松字段")
      +(d.is_mobile?" · 移动端":"");
    $("ovClientUa").textContent=d.ua||"";
  }catch(e){$("ovClient").textContent="识别失败："+e;}
}

/* ---------- 实时播放诊断 ---------- */
let LIVE_TIMER=null;
const LIVE_STAGE={library:"媒体库",browse:"片单",search:"搜索",detail:"详情",
  season:"季",episode:"选集",streams:"线路",play:"起播",media:"播放",
  hls:"清单",seg:"分片",direct:"直链",favorite:"收藏",history:"历史"};
const LIVE_ORIGIN={local:["local","var(--ok)"],mixed:["mixed","#2563eb"],
  upstream:["upstream","#b45309"],redirect:["redirect","#7c3aed"]};
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function ago(a){return a<5?"刚刚":a<60?Math.floor(a)+"秒前":Math.floor(a/60)+"分前";}
function liveOrigin(o){const [t,col]=LIVE_ORIGIN[o]||[o,"var(--muted)"];
  return `<span class="tag" style="color:${col};background:transparent;border:1px solid ${col}33">${t}</span>`;}
async function loadLive(){
  if(!document.getElementById("liveDevices"))return;
  let d;
  try{ d=(await jget("/v/api/ext/live")).data||{devices:[]}; }
  catch(e){ $("liveState").textContent="连接失败"; $("livePulse").className="dot bad"; return; }
  $("liveState").textContent="实时"; $("livePulse").className="dot ok";
  LIVE_NOW=d.now||Date.now()/1000;
  const devs=d.devices||[];
  if(!devs.length){$("liveDevices").innerHTML='<div class="muted sm">暂无播放活动（在电视或手机上播一部片即可看到）</div>';return;}
  $("liveDevices").innerHTML=devs.map(dv=>{
    const kindColor={tv:"#b45309",android:"var(--ok)",ios:"#2563eb",web:"#7c3aed"}[dv.kind]||"var(--muted)";
    const sess=dv.session;
    const sessHtml=sess?`<div class="sm" style="margin-top:4px">
        ${sess.error?'<span class="tag amber">播放异常</span>':'<span class="tag">在播</span>'}
        <b>${esc(sess.title||"（未知名）")}</b>
        <span class="muted">· ${LIVE_STAGE[sess.stage]||sess.stage} · ${ago((d.now||dv.seen)-sess.at)}前</span>
        ${sess.error?`<div class="sm" style="color:var(--err);margin-top:2px">⚠ ${esc(sess.error)}</div>`:""}
      </div>`:"";
    const errBadge=dv.errors?` <span style="color:var(--err);font-weight:700">· ${dv.errors} 次错误</span>`:"";
    const rows=(dv.events||[]).slice(0,8).map(e=>{
      const st=e.status;const col=st>=400?"var(--err)":(st>=300?"#7c3aed":"var(--ok)");
      return `<div class="live-row">
        <span style="color:${col};font-weight:600;min-width:34px;display:inline-block">${st}</span>
        ${liveOrigin(e.origin)}
        <span class="tag" style="background:#eef1f5;color:var(--muted)">${LIVE_STAGE[e.stage]||e.stage}</span>
        ${e.title?`<span class="sm">${esc(e.title)}</span>`:""}
        <span class="muted sm" style="margin-left:auto;white-space:nowrap">${e.note?esc(e.note):""} ${ago((d.now||dv.seen)-e.t)}</span>
      </div>`;
    }).join("");
    return `<div class="live-dev">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span class="badge" style="background:${kindColor}18;color:${kindColor}">${esc(dv.label)}</span>
        <span class="muted sm">${esc(dv.ip)} · ${dv.reqs} 请求${errBadge} · 最近 ${ago(dv.ago)}</span>
      </div>
      ${sessHtml}
      <div class="live-rows">${rows}</div>
      <details class="muted sm"><summary style="cursor:pointer">UA</summary><span style="word-break:break-all">${esc(dv.ua)}</span></details>
    </div>`;
  }).join("");
  renderDiagnosis(devs);
}

/* ---------- 错误自动归因 ---------- */
let LIVE_DATA=[];let LIVE_NOW=0;
const STAGE_NAME={library:"媒体库",browse:"片单",search:"搜索",detail:"详情",
  season:"季",episode:"选集",streams:"线路",play:"起播",media:"播放",
  hls:"清单",seg:"分片",direct:"直链",favorite:"收藏",history:"历史"};
// 归因规则：按 阶段+状态+来源 推断根因与建议
function diagnoseDevice(dv){
  const errs=(dv.events||[]).filter(e=>e.err);
  if(!errs.length)return null;
  const byCode={};errs.forEach(e=>byCode[e.status]=(byCode[e.status]||0)+1);
  const first=errs[errs.length-1]; // 时间最早的错误
  const last=errs[0];
  let cause="",fix="",severity="warn";
  const st=last.status, stage=last.stage, origin=last.origin;
  if(st===403){
    if(["seg","hls","direct","media"].includes(stage)){cause="源站/CDN 鉴权拒绝（防盗链或链接过期）";fix="该资源站该线路受限，请切换其它线路或换源；属源站问题，非插件故障。";}
    else if(origin==="upstream"){cause="飞牛官方后端返回 403（权限/登录态）";fix="检查飞牛账号登录与媒体库访问权限。";}
    else{cause="请求被拒绝（403）";fix="重试或更换线路。";}
  }else if(st===404){
    if(["hls","seg","direct","media"].includes(stage)){cause="该线路解析不出可播地址（片源失效/无 HLS）";fix="播放源本身问题：请切换线路或换一个同名资源。";severity="src";}
    else if(stage==="detail"||stage==="browse"){cause="条目尚未解析出片源或已失效";fix="稍后重试，或用顶部搜索同名资源。";}
    else{cause="资源不存在（404）";fix="刷新或换源。";}
  }else if(st===502||st===504){
    cause=stage==="seg"?"分片上游抓取失败/超时":"插件向上游或资源站取数失败（502/504）";
    fix="资源站临时抖动，通常会自动重试；若反复出现请切换线路/源。";
  }else if(st===503){cause="暂无可播片源";fix="该条目暂未匹配到可播资源，稍后再试或搜索同名。";}
  else if(st===500){cause="服务端处理错误（500）";fix="请连同下方诊断信息反馈，多为某个异常响应头/数据形态，会持续完善兼容。";severity="err";}
  else if(origin==="upstream"&&st>=400){cause=`飞牛官方后端返回 ${st}`;fix="非插件注入路径，检查官方服务/网络。";}
  else{cause=`请求失败（HTTP ${st}，环节：${STAGE_NAME[stage]||stage}）`;fix="重试；如反复出现请复制诊断信息反馈。";}
  return {dv,cause,fix,severity,st,stage:STAGE_NAME[stage]||stage,
    at:last?ago(LIVE_NOW-last.t):"",count:errs.length,byCode};
}
function renderDiagnosis(devs){
  LIVE_DATA=devs;
  const box=$("liveDiagnosis");if(!box)return;
  const findings=devs.map(diagnoseDevice).filter(Boolean);
  if(!findings.length){box.innerHTML="";return;}
  const cls={warn:"amber",src:"",err:""};
  box.innerHTML=findings.slice(0,3).map(f=>{
    const color=f.severity==="err"?"var(--err)":(f.severity==="src"?"#2563eb":"#b45309");
    return `<div class="diag" style="border-left:3px solid ${color}">
      <div><b style="color:${color}">${esc(f.dv.label)}</b> · ${esc(f.stage)} · ${f.count} 次错误
        ${f.dv.session&&f.dv.session.title?(" · "+esc(f.dv.session.title)):""}</div>
      <div class="sm" style="margin-top:2px"><b>归因：</b>${esc(f.cause)}</div>
      <div class="sm muted" style="margin-top:2px"><b>建议：</b>${esc(f.fix)}</div>
    </div>`;
  }).join("");
}
async function copyDiagnosis(){
  try{
    const devs=LIVE_DATA;
    const now=new Date();
    const lines=[];
    lines.push("fnmedia-ext 播放诊断 · "+now.toLocaleString());
    lines.push("服务: "+location.host+" · 设备数 "+devs.length);
    devs.forEach((dv,i)=>{
      lines.push("");
      lines.push(`[${i+1}] ${dv.label} · ${dv.ip} · 请求${dv.reqs} 错误${dv.errors} · 最近${ago(dv.ago)}`);
      lines.push("UA: "+dv.ua);
      if(dv.session)lines.push("当前: "+(dv.session.title||"")+" · 环节"+(STAGE_NAME[dv.session.stage]||dv.session.stage)+(dv.session.error?(" · 异常 "+dv.session.error):" · 正常"));
      const f=diagnoseDevice(dv);
      if(f){lines.push("归因: "+f.cause);lines.push("建议: "+f.fix);}
      (dv.events||[]).slice(0,12).reverse().forEach(e=>{
        lines.push(`  ${e.status} [${e.origin}] ${STAGE_NAME[e.stage]||e.stage} ${e.p.replace(/[&<>]/g,"")} ${e.note||""}`);
      });
    });
    const text=lines.join("\n");
    try{await navigator.clipboard.writeText(text);}
    catch(_){const ta=document.createElement("textarea");ta.value=text;document.body.appendChild(ta);ta.select();document.execCommand("copy");ta.remove();}
    toast("诊断信息已复制，可直接粘贴反馈");
  }catch(e){toast("复制失败："+e,"fail");}
}
function startLivePoll(){ if(LIVE_TIMER)return; LIVE_TIMER=setInterval(loadLive,3000); loadLive(); }
function stopLivePoll(){ if(LIVE_TIMER){clearInterval(LIVE_TIMER);LIVE_TIMER=null;} }

function renderOverview(){
  if(!STATUS)return;
  const c=STATUS.charts&&STATUS.charts.counts||{};
  const hotN=(c.movie||0)+(c.tv||0)+(c.anime||0)+(c.variety||0);
  $("ovSrcN").textContent=CFG.sources.filter(s=>s.enabled).length;
  $("ovCacheN").textContent=STATUS.cached_titles||0;
  $("ovHotN").textContent=hotN;
  const cats=(STATUS.catalog&&STATUS.catalog.categories)||{};
  $("ovCatN").textContent=Object.values(cats).filter(x=>x.routes>0).length+"/4";
  $("ovHotMeta").textContent=(STATUS.charts&&STATUS.charts.refreshing?"正在刷新…":"最近刷新："+fmtTs(STATUS.charts&&STATUS.charts.updated_at))
    +` · 电影${c.movie||0}/剧${c.tv||0}/动漫${c.anime||0}/综艺${c.variety||0}`
    +(STATUS.charts&&STATUS.charts.last_error?(" · "+STATUS.charts.last_error):"");
  $("ovLibs").innerHTML=libCards(c,cats);
}
function libCards(chartCounts,cats){
  const hot=[["🎬","热门电影",chartCounts.movie||0],["📺","热门剧集",chartCounts.tv||0],
    ["🌸","动漫新番",chartCounts.anime||0],["🎤","热门综艺",chartCounts.variety||0]];
  const catMap={movie:"海量电影",tv:"海量剧集",anime:"海量动漫",variety:"海量综艺"};
  const cat=[["🌐",catMap.movie,(cats.movie||{}).routes||0],["🌐",catMap.tv,(cats.tv||{}).routes||0],
    ["🌐",catMap.anime,(cats.anime||{}).routes||0],["🌐",catMap.variety,(cats.variety||{}).routes||0]];
  const item=(arr,label)=>arr.map(([ic,t,n])=>
    `<div class="lib"><div class="ic">${ic}</div><div><div class="t">${t}</div><div class="c">${label} ${n}</div></div></div>`).join("");
  return item(hot,"榜单")+item(cat,"路由");
}

/* ---------- 虚拟媒体库 ---------- */
let LIBS=[];
let ACCOUNTS=[];
let ACLDEF={visible:true,online:true};
let LIBPLACE="front",LIBANCHOR="",LIBORDER=[];
async function loadLibs(){
  try{const r=await jget("/v/api/ext/libs");LIBS=(r.data&&r.data.libs)||[];
    LIBPLACE=(r.data&&r.data.placement)||"front";
    LIBANCHOR=(r.data&&r.data.anchor)||"";
    LIBORDER=(r.data&&r.data.order)||[];
    if($("catEnabled"))$("catEnabled").checked=!!(r.data&&r.data.catalog_enabled);
  }catch(e){LIBS=[];}
  await loadAccounts();
  await loadLocalLibs();
}
async function loadAccounts(){
  try{const r=await jget("/v/api/ext/accounts");
    ACCOUNTS=(r.data&&r.data.accounts)||[];
    ACLDEF={visible:r.data&&r.data.acl_default_visible!==false,
            online:r.data&&r.data.acl_default_online!==false};
    if($("aclDefVis"))$("aclDefVis").checked=ACLDEF.visible;
    if($("aclDefOnline"))$("aclDefOnline").checked=ACLDEF.online;
  }catch(e){ACCOUNTS=[];}
  renderAccounts();
}
async function loadLocalLibs(){
  try{const r=await jget("/v/api/ext/local-libs");
    const sel=$("libAnchor");if(!sel)return;
    const cur=sel.value||LIBANCHOR;
    sel.innerHTML=`<option value="">— 未选择 —</option>`+((r.data&&r.data.libs)||[])
      .map(l=>`<option value="${esc(l.guid)}">${esc(l.name||l.guid)}</option>`).join("");
    sel.value=cur;
  }catch(e){}
}
function libOrderKeys(){
  const keys=LIBS.map(x=>x.key);
  const ordered=LIBORDER.filter(k=>keys.includes(k));
  keys.forEach(k=>{if(!ordered.includes(k))ordered.push(k);});
  return ordered;
}
async function toggleHide(key,el){
  try{
    const r=await jpost("/v/api/ext/lib/visibility",{key,hidden:el.checked});
    if(r.code===0){toast(el.checked?"已隐藏（所有账号都看不到）":"已恢复显示");
      LIBS.forEach(x=>{if(x.key===key)x.hidden=el.checked;});renderLibrary();}
    else{el.checked=!el.checked;toast(r.msg||"设置失败","fail");}
  }catch(e){el.checked=!el.checked;toast("设置失败","fail");}
}
async function moveLib(key,dir){
  const keys=libOrderKeys();
  const i=keys.indexOf(key);const j=i+dir;
  if(i<0||j<0||j>=keys.length)return;
  [keys[i],keys[j]]=[keys[j],keys[i]];
  try{
    const r=await jpost("/v/api/ext/lib/order",{order:keys});
    if(r.code===0){LIBORDER=keys;toast("顺序已保存");renderLibrary();}
    else toast(r.msg||"保存失败","fail");
  }catch(e){toast("保存失败","fail");}
}
async function savePlacement(){
  const placement=$("libPlacement")?$("libPlacement").value:"front";
  const anchor=$("libAnchor")?$("libAnchor").value:"";
  try{
    const r=await jpost("/v/api/ext/lib/order",{order:libOrderKeys(),placement,anchor});
    if(r.code===0){LIBPLACE=placement;LIBANCHOR=anchor;toast("位置已保存");}
    else toast(r.msg||"保存失败","fail");
  }catch(e){toast("保存失败","fail");}
}
async function saveAcl(guid,btn){
  const card=btn.closest("[data-acct]");if(!card)return;
  const allow=[...card.querySelectorAll("input[data-libkey]:checked")].map(x=>x.getAttribute("data-libkey"));
  const on=card.querySelector("input[data-online]");
  if(on&&on.checked)allow.push("__online__");
  try{
    const r=await jpost("/v/api/ext/account/acl",{guid,allow});
    if(r.code===0){toast("已保存该账号的可见库");
      ACCOUNTS.forEach(a=>{if(a.guid===guid){a.allowed=allow;a.configured=true;}});renderAccounts();}
    else toast(r.msg||"保存失败","fail");
  }catch(e){toast("保存失败","fail");}
}
async function resetAcl(guid,btn){
  try{
    const r=await jpost("/v/api/ext/account/acl",{guid,allow:null});
    if(r.code===0){toast("已恢复默认（按默认权限处理）");
      ACCOUNTS.forEach(a=>{if(a.guid===guid){a.allowed=null;a.configured=false;}});renderAccounts();}
    else toast(r.msg||"保存失败","fail");
  }catch(e){toast("保存失败","fail");}
}
async function saveAclDefaults(){
  const body={acl_default_visible:$("aclDefVis")?$("aclDefVis").checked:true,
    acl_default_online:$("aclDefOnline")?$("aclDefOnline").checked:true};
  try{
    const r=await jpost("/v/api/ext/account/defaults",body);
    if(r.code===0){ACLDEF={visible:body.acl_default_visible,online:body.acl_default_online};toast("默认权限已保存");}
    else toast(r.msg||"保存失败","fail");
  }catch(e){toast("保存失败","fail");}
}
function renderAccounts(){
  const box=$("acctList");if(!box)return;
  if(!ACCOUNTS.length){box.innerHTML=`<div class="muted sm">账号加载中…（若一直为空，说明 NAS 上 trim.media 的数据库目录不可读）。</div>`;return;}
  box.innerHTML=ACCOUNTS.map(a=>{
    const libs=LIBS.map(L=>{
      const checked=a.allowed===null?ACLDEF.visible:a.allowed.includes(L.key);
      return `<label style="display:inline-flex;gap:4px;align-items:center;margin:2px 8px 2px 0;font-size:13px">
        <input type="checkbox" data-libkey="${esc(L.key)}" ${checked?"checked":""}/> ${esc(L.title)}</label>`;
    }).join("");
    const onChecked=a.allowed===null?ACLDEF.online:a.allowed.includes("__online__");
    return `<div class="lib" data-acct="${esc(a.guid)}" style="margin-bottom:8px">
      <div style="flex:1;min-width:0"><div class="t">${esc(a.username||a.guid)}${a.is_admin?" <span class=\"muted sm\">管理员</span>":""}
        ${a.configured?"":" <span class=\"muted sm\">未设置，按默认</span>"}</div>
      <div style="margin-top:4px">${libs||"<span class=\"muted sm\">暂无虚拟库</span>"}</div>
      <div style="margin-top:6px"><label style="font-size:13px"><input type="checkbox" data-online="${esc(a.guid)}" ${onChecked?"checked":""}/> 允许在线搜索</label></div>
      <div class="btn-row" style="margin-top:6px">
        <button class="btn sm primary" onclick="saveAcl('${esc(a.guid)}',this)">保存该账号</button>
        <button class="btn sm" onclick="resetAcl('${esc(a.guid)}',this)">恢复默认</button>
      </div></div></div>`;
  }).join("");
}
function catKeyOf(title){
  if(/电影/.test(title))return "movie"; if(/剧/.test(title))return "tv";
  if(/动漫|番/.test(title))return "anime"; if(/综艺/.test(title))return "variety"; return "";
}
function libCardHtml(L,ic){
  const grp={standalone:"独立源库",guard:"加固",xlib:"小雅",hgdj:"红果短剧"}[L.group]||"";
  const sub=L.group==="t4"?(L.count+" 个源"):L.group==="hot"?(L.count+" 条"):(L.count?("路由 "+L.count):(grp||"建库中…"));
  return `<div class="lib" data-g="${L.guid}"><div class="ic">${ic}</div>
    <div style="flex:1;min-width:0"><div class="t">${esc(L.title)}${L.hidden?" <span class=\"muted sm\">已隐藏</span>":""}</div>
      <div class="c">${sub}</div>
      <div style="display:flex;gap:4px;margin-top:4px;flex-wrap:wrap">
        <button class="btn sm" onclick="moveLib('${esc(L.key)}',-1)" title="上移">↑</button>
        <button class="btn sm" onclick="moveLib('${esc(L.key)}',1)" title="下移">↓</button>
      </div></div>
    <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end">
      <label class="pin" title="在飞牛影视媒体库列表中置顶">
        <input type="checkbox" ${L.pinned?"checked":""} onchange="togglePin('${L.guid}',this)"/>置顶</label>
      <label class="pin" title="隐藏后所有账号都看不到这张库">
        <input type="checkbox" ${L.hidden?"checked":""} onchange="toggleHide('${esc(L.key)}',this)"/>隐藏</label>
    </div>
  </div>`;
}
async function togglePin(guid,el){
  try{
    const r=await jpost("/v/api/ext/lib/pin",{guid,pinned:el.checked});
    if(r.code===0){toast(el.checked?"已置顶":"已取消置顶");LIBS.forEach(x=>{if(x.guid===guid)x.pinned=el.checked;});}
    else{el.checked=!el.checked;toast(r.msg||"设置失败","fail");}
  }catch(e){el.checked=!el.checked;toast("设置失败","fail");}
}
function renderLibrary(){
  if(!STATUS)return;
  $("hotEnabled").checked=CFG.hot_library_enabled!==false;
  if($("catEnabled"))$("catEnabled").checked=CFG.catalog_library_enabled!==false;
  if($("welfareEnabled"))$("welfareEnabled").checked=CFG.welfare_library_enabled===true;
  if($("t4Enabled"))$("t4Enabled").checked=CFG.t4_library_enabled!==false;
  if($("t4Libs"))$("t4Libs").innerHTML=LIBS.filter(x=>x.group==="t4").map(L=>libCardHtml(L,"📡")).join("");
  $("hotInterval").value=Math.round((CFG.hot_refresh_interval||21600)/3600);
  const c=STATUS.charts&&STATUS.charts.counts||{};
  const cats=(STATUS.catalog&&STATUS.catalog.categories)||{};
  const hotIc={movie:"🎬",tv:"📺",anime:"🌸",variety:"🎤"};
  if(LIBS.length){
    $("hotLibs").innerHTML=LIBS.filter(x=>x.group==="hot").map(L=>libCardHtml(L,hotIc[catKeyOf(L.title)]||"🔥")).join("");
    $("catLibs").innerHTML=LIBS.filter(x=>x.group==="catalog").map(L=>libCardHtml(L,"🌐")).join("");
    const other=LIBS.filter(x=>x.group!=="hot"&&x.group!=="catalog");
    const og=$("otherLibs");if(og)og.innerHTML=other.length?other.map(L=>libCardHtml(L,{standalone:"📺",guard:"🛡",xlib:"💾",hgdj:"🍅"}[L.group]||"📚")).join(""):"<div class=\"muted sm\">暂无独立源库 / 加固 / 小雅 / 红果库。</div>";
  }else{
    $("hotLibs").innerHTML=[["movie","🎬","热门电影",c.movie||0],["tv","📺","热门剧集",c.tv||0],["anime","🌸","动漫新番",c.anime||0],["variety","🎤","热门综艺",c.variety||0]]
        .map(([k,ic,t,n])=>`<div class="lib"><div class="ic">${ic}</div><div><div class="t">${t}</div><div class="c">${n} 条</div></div></div>`).join("");
    $("catLibs").innerHTML=[["movie","🌐海量电影"],["tv","🌐海量剧集"],["anime","🌐海量动漫"],["variety","🌐海量综艺"]]
      .map(([k,t])=>{const n=(cats[k]||{}).routes||0;
        return `<div class="lib"><div class="ic">🌐</div><div><div class="t">${t}</div><div class="c">${n?("路由 "+n):"建库中…"}</div></div></div>`;}).join("");
  }
  $("hotStatus").textContent=(STATUS.charts&&STATUS.charts.refreshing?"正在后台刷新榜单…":"最近刷新："+fmtTs(STATUS.charts&&STATUS.charts.updated_at))
    +` · 数据源 ${(STATUS.charts&&STATUS.charts.provider_ok)||0}/${(STATUS.charts&&STATUS.charts.provider_total)||0}`
    +(STATUS.charts&&STATUS.charts.last_error?(" · "+STATUS.charts.last_error):"");
  if($("libPlacement"))$("libPlacement").value=LIBPLACE;
  if($("libAnchor")&&LIBANCHOR)$("libAnchor").value=LIBANCHOR;
  renderAccounts();
}
function fmtTs(ts){if(!ts)return"尚未刷新";const d=new Date(ts*1000),p=n=>String(n).padStart(2,"0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;}
async function saveHotCfg(){
  const body={hot_library_enabled:$("hotEnabled").checked,
    hot_refresh_interval:Math.max(1,Math.min(24,parseInt($("hotInterval").value)||6))*3600};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){Object.assign(CFG,body);toast("热门设置已保存");await loadLibs();renderLibrary();}else toast(r.msg||"保存失败","fail");
}
async function saveCatCfg(){
  const body={catalog_library_enabled:$("catEnabled").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){Object.assign(CFG,body);toast("海量四库设置已保存");await loadLibs();renderLibrary();}
  else toast(r.msg||"保存失败","fail");
}
async function saveT4Cfg(){
  const body={t4_library_enabled:$("t4Enabled").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){Object.assign(CFG,body);toast("T4 选片设置已保存");await loadLibs();renderLibrary();}
  else toast(r.msg||"保存失败","fail");
}
async function saveWelfareCfg(){
  const body={welfare_library_enabled:$("welfareEnabled").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){Object.assign(CFG,body);toast(body.welfare_library_enabled?"已启用福利库":"已关闭福利库");}
  else toast(r.msg||"保存失败","fail");
}
async function refreshHot(){
  toast("开始刷新榜单…");
  const r=await jpost("/v/api/ext/hot/refresh",{});
  await loadStatus();await loadLibs();renderLibrary();
  toast(r.code===0?"榜单已刷新":(r.msg||"刷新失败"),r.code===0?"ok":"fail");
}

/* ---------- 片源质量 ---------- */
function renderQuality(){
  if(!STATUS)return;
  $("preferQ").checked=CFG.prefer_quality_source!==false;
  PRIO=STATUS.source_priority||{};
  const ov=CFG.source_priority||{};
  $("prioCount").textContent=Object.keys(ov).length+" 自定义";
  const rows=CFG.sources.map(s=>{
    const eff=PRIO[s.id]||0, custom=ov[s.id];
    const w=Math.max(4,Math.min(100,(eff+20)));
    return `<tr data-id="${s.id}">
      <td><div style="font-weight:500">${esc(s.name)} ${s.enabled?'':'<span class="muted sm">(停用)</span>'}</div>
        <div class="meta">${esc(s.api)}</div></td>
      <td><input type="number" class="prio-input" min="-100" max="100" data-prio
        value="${custom!=null?custom:''}" placeholder="${eff}"/></td>
      <td style="min-width:120px"><div class="prio-bar" style="width:${w}%"></div>
        <span class="muted sm">${custom!=null?('自定义 '+custom):'默认 '+eff}</span></td></tr>`;
  }).join("");
  $("prioRows").innerHTML=rows||'<tr><td colspan="3" class="muted">暂无源</td></tr>';
}
function collectPriority(){const m={};$$("[data-prio]").forEach(inp=>{
  const v=inp.value.trim();if(v!=="")m[inp.closest("tr").dataset.id]=parseInt(v)||0;});return m;}
async function savePriority(){
  const r=await jpost("/v/api/ext/source/priority",{priority:collectPriority()});
  if(r.code===0){toast("权重已保存");await loadStatus();renderQuality();}else toast(r.msg||"保存失败","fail");
}
async function resetPriority(){
  const r=await jpost("/v/api/ext/source/priority",{priority:{}});
  if(r.code===0){toast("已恢复默认权重");await loadStatus();renderQuality();}else toast("失败","fail");
}
async function savePreferQ(){
  const r=await jpost("/v/api/ext/config",{prefer_quality_source:$("preferQ").checked});
  if(r.code===0){CFG.prefer_quality_source=$("preferQ").checked;toast("已保存");}else toast("失败","fail");
}

/* ---------- 资源站 ---------- */
/* 资源站分页/筛选（P1-16）：纯前端分页/筛选，不改后端接口；CFG.sources
   已在页内。筛选或切换启用状态时回到第 1 页；列表缩短时 renderSources
   会把超出的页码夹回最后一页。 */
let SRC_FILTER="",SRC_ENABLED="all",SRC_PAGE=1;const SRC_PAGE_SIZE=20;
function srcFiltered(){const q=SRC_FILTER.trim().toLowerCase();
  return (CFG.sources||[]).filter(s=>{
    if(SRC_ENABLED==="on"&&!s.enabled)return false;
    if(SRC_ENABLED==="off"&&s.enabled)return false;
    if(q&&!(String(s.name||"").toLowerCase().includes(q)
      ||String(s.api||"").toLowerCase().includes(q)))return false;
    return true;});}
function onSrcFilter(v){SRC_FILTER=v;SRC_PAGE=1;renderSources();}
function onSrcEnabledFilter(v){SRC_ENABLED=v;SRC_PAGE=1;renderSources();}
function srcPage(d){SRC_PAGE=Math.max(1,SRC_PAGE+d);renderSources();}
function renderSources(){
  const srcsAll=srcFiltered();
  const pages=Math.max(1,Math.ceil(srcsAll.length/SRC_PAGE_SIZE));
  if(SRC_PAGE>pages)SRC_PAGE=pages;
  const srcs=srcsAll.slice((SRC_PAGE-1)*SRC_PAGE_SIZE,SRC_PAGE*SRC_PAGE_SIZE);
  const all=CFG.sources||[];
  $("srcCount").textContent=all.filter(x=>x.enabled).length+"/"+all.length+" 启用";
  const pi=$("srcPageInfo");
  if(pi)pi.textContent=`第 ${SRC_PAGE}/${pages} 页 · 本页 ${srcs.length} 条 · 筛选后 ${srcsAll.length} 条`;
  $("srcRows").innerHTML=srcs.length?srcs.map(src=>{
    let extraCell="",extraTag="";
    // >>> PRIVATE:ADULT
    extraCell=FEAT_ADULT?`
      <label title="🔞 限制级独立库（18+）：与主库隔离、不进全局搜索；开启即表示你已年满 18 周岁并自行承担责任"
        style="display:inline-flex;align-items:center;gap:2px;font-size:11px;cursor:pointer;margin-left:4px">
        <input type="checkbox" class="ad" ${src.adult?"checked":""}
         ${(src.enabled&&src.standalone&&!(FEAT_DDUO&&src.xsrc))?"":"disabled"} style="width:15px;height:15px;accent-color:#be123c"/>🔞</label>`:"";
    // <<< PRIVATE:ADULT
    // >>> PRIVATE:DDUO
    extraTag=(FEAT_DDUO&&src.xsrc&&(src.api||"").indexOf("guard://")!==0)?'<span class="tag amber" title="非苹果CMS自研站，WASM实时解码">WASM</span>':'';
    // <<< PRIVATE:DDUO
    return `<tr data-id="${src.id}">
      <td><input type="checkbox" class="en" ${src.enabled?"checked":""} style="accent-color:var(--accent);width:16px;height:16px"/></td>
      <td style="white:nowrap">
        <label title="把该资源站单独生成一张媒体库卡片" style="display:inline-flex;align-items:center;gap:2px;font-size:11px;cursor:pointer">
          <input type="checkbox" class="sa" ${src.standalone?"checked":""}
           ${(src.enabled&&src.kind!=="t4"&&!(FEAT_DDUO&&src.xsrc))?"":"disabled"} style="width:15px;height:15px;accent-color:var(--accent)"/>库</label>
        <label title="非过滤：独立库内不按标题/备注过滤解说/预告/花絮" style="display:inline-flex;align-items:center;gap:2px;font-size:11px;cursor:pointer;margin-left:4px">
          <input type="checkbox" class="lf" ${src.loose_filter?"checked":""}
           ${(src.enabled&&src.standalone&&!(FEAT_DDUO&&src.xsrc))?"":"disabled"} style="width:15px;height:15px;accent-color:var(--warn)"/>非过滤</label>
        ${extraCell}
      </td>
      <td><div style="font-weight:500">${esc(src.name)}${src.kind==="t4"?'<span class="tag">T4</span>':''}${extraTag}${src.builtin?'':'<span class="tag amber">自定义</span>'}${src.standalone?'<span class="tag">媒体库</span>':''}</div>
        <div class="meta">${esc(src.api)}</div></td>
      <td class="st wait" data-st>未测</td>
      <td><button class="small" data-test>测试</button></td>
      <td><button class="del" data-del title="删除">✕</button></td>
    </tr>`;
  }).join(""):'<tr><td colspan="6" class="muted">没有匹配的资源站（调整筛选条件或清空筛选）。</td></tr>';
  $$("#srcRows tr").forEach(tr=>{
    const id=tr.dataset.id;
    tr.querySelector(".en").addEventListener("change",()=>toggleEnable(id,tr.querySelector(".en").checked));
    const sa=tr.querySelector(".sa");sa.addEventListener("change",()=>toggleStandalone(id,sa.checked));
    const lf=tr.querySelector(".lf");if(lf)lf.addEventListener("change",()=>toggleLoose(id,lf.checked));
    // >>> PRIVATE:ADULT
    const ad=tr.querySelector(".ad");if(FEAT_ADULT&&ad)ad.addEventListener("change",()=>toggleAdult(id,ad.checked,ad));
    // <<< PRIVATE:ADULT
    tr.querySelector("[data-test]").addEventListener("click",()=>testOne(id,tr));
    tr.querySelector("[data-del]").addEventListener("click",()=>delSource(id,srcName(id)));
  });
  // T4 远程源清单（已接入：进 sources 且 kind=t4，在 T4 选片库中可搜可播）
  const t4=(CFG.sources||[]).filter(s=>s.kind==="t4");
  $("t4Count").textContent=t4.length?(t4.length+" 个"):"";
  $("t4Rows").innerHTML=t4.length?t4.map(x=>{
    const bad=x.invalid?'<span class="tag warn">地址无效</span>':'<span class="tag">T4</span>';
    return `<div class="subrow"><span><b>${esc(x.name||x.api||"")}</b><br><span class="meta">${esc(x.api||"")}</span></span>${bad}</div>`;
  }).join(""):'<div class="muted sm">暂无 T4 源：去「导入订阅」导一次含 type=4 的订阅。</div>';
}
function srcName(id){const s=CFG.sources.find(x=>x.id===id);return s?s.name:id;}
async function toggleEnable(id,on){
  const sources=CFG.sources.map(s=>({id:s.id,enabled:s.id===id?on:s.enabled}));
  let r;
  try{r=await jpost("/v/api/ext/config",{sources});}
  catch(e){toast("保存失败","fail");renderSources();return;}
  if(r.code===0){CFG.sources.find(x=>x.id===id).enabled=on;
    $("srcCount").textContent=CFG.sources.filter(x=>x.enabled).length+"/"+CFG.sources.length+" 启用";toast(on?"已启用":"已停用");}
  else{toast(r.msg||"保存失败","fail");renderSources();}
}
async function toggleStandalone(id,on){
  const r=await jpost("/v/api/ext/source/standalone",{id,standalone:on});
  if(r.code===0){const s=CFG.sources.find(x=>x.id===id);if(s){s.standalone=on;if(!on){s.loose_filter=false;s.adult=false;}}
    renderSources();toast(on?"已生成独立媒体库（后台建库中）":"已取消独立媒体库");}
  else{toast(r.msg||"操作失败","fail");renderSources();}
}
async function toggleLoose(id,on){
  const r=await jpost("/v/api/ext/source/loose-filter",{id,loose_filter:on});
  if(r.code===0){const s=CFG.sources.find(x=>x.id===id);if(s)s.loose_filter=on;renderSources();toast(on?"已开启非过滤":"已恢复严格过滤");}
  else{toast(r.msg||"操作失败","fail");renderSources();}
}
// >>> PRIVATE:ADULT
async function toggleAdult(id,on,el){
  if(on){const ok=confirm("将该源标记为 🔞 限制级独立库（18+）？\n该库与主库隔离、不进入全局搜索，开启即表示你已年满 18 周岁并自行承担责任。");
    if(!ok){el.checked=false;return;}}
  const r=await jpost("/v/api/ext/source/adult",{id,adult:on});
  if(r.code===0){const s=CFG.sources.find(x=>x.id===id);if(s)s.adult=on;renderSources();toast(on?"已标记为 🔞 限制级":"已取消 🔞 限制级标记");}
  else{toast(r.msg||"操作失败","fail");renderSources();}
}
// <<< PRIVATE:ADULT
function setSt(tr,html,cls){const el=tr.querySelector("[data-st]");el.className="st "+cls;el.innerHTML=html;}
async function testOne(id,tr){
  setSt(tr,"测试中…","wait");tr.querySelector("[data-test]").disabled=true;
  try{const r=await jpost("/v/api/ext/source/test",{id});const d=r.data;
    if(d.ok)setSt(tr,`✓ ${d.elapsed_ms}ms · ${d.count}条${d.direct?(' · '+d.direct+'直链'):''}`,"ok");
    else setSt(tr,"✕ "+(d.error||"失败")+(d.elapsed_ms?(" · "+d.elapsed_ms+"ms"):""),"bad");
  }catch(e){setSt(tr,"✕ "+e,"bad");}
  tr.querySelector("[data-test]").disabled=false;
}
async function testAll(){
  toast("正在并发测速…");
  // 分页后仅当前页的行在 DOM 里：测速请求仍覆盖全部源，这里只刷新当前页行的状态。
  $$("#srcRows [data-st]").forEach(el=>{el.className="st wait";el.textContent="测试中…";});
  const r=await jpost("/v/api/ext/source/test-all",{});
  (r.data||[]).forEach(d=>{const tr=$("srcRows").querySelector(`tr[data-id="${d.id}"]`);if(!tr)return;
    if(d.ok)setSt(tr,`✓ ${d.elapsed_ms}ms · ${d.count}条${d.direct?(' · '+d.direct+'直链'):''}`,"ok");
    else setSt(tr,"✕ "+(d.error||"失败"),"bad");});
  toast("测速完成");
}
/* ---------- CMS 源批量体检（移植上游 v0.2.71） ----------
   注意：导入订阅页的「一键全部体检」是另一功能（spScanStart，走
   /v/api/ext/spider/scan，体检爬虫/加固/DRPY 源），不要动它。
   这里只体检纯 CMS 源：任一热门词搜到即有效，失效写入健康画像触发熔断。 */
async function cmsHealthCheck(){
  const box=$("cmsHealthResult");if(box)box.textContent="正在用热门词批量检测纯 CMS 源，请稍候…";
  try{
    const r=await jpost("/v/api/ext/source/health-check",{});
    if(r.code!==0){if(box)box.textContent="体检失败："+(r.msg||"");return;}
    const d=r.data||{},res=d.results||[];
    const dead=res.filter(x=>!x.alive);
    let h=`体检完成：共 ${d.total} 个纯 CMS 源，有效 ${d.alive} 个，失效 ${d.dead} 个（关键词：${(d.keywords||[]).join(" / ")}）。`;
    if(dead.length)h+='<br>失效：'+dead.map(x=>esc(x.name||x.id)+(x.error?('（'+esc(x.error)+'）'):'')).join("、")+'（已记入健康画像，达阈值自动熔断）';
    if(box)box.innerHTML=h;
    toast(`CMS 体检完成：${d.alive}/${d.total} 有效`);
    try{loadHealth();}catch(e){}
  }catch(e){if(box)box.textContent="体检请求失败："+String(e);}
}
async function loadHealth(){
  const box=$("healthRows");if(!box)return;box.innerHTML='<tr><td colspan="6" class="muted">加载中…</td></tr>';
  try{
    const r=await (await fetch("/v/api/ext/source-health")).json();
    const rows=(r.data&&r.data.sources)||[];
    if(!rows.length){box.innerHTML='<tr><td colspan="6" class="muted">还没有数据，正常播放几次后会自动统计。</td></tr>';return;}
    const G=["健康","慢","高失败率","冷却中"];
    const GC=["ok","wait","bad","bad"];
    box.innerHTML=rows.map(s=>{
      const pct=Math.round((s.success_rate||0)*100);
      return `<tr><td>${s.key}</td><td>${s.samples}</td>
        <td>${pct}%</td><td>${(s.avg_latency||0).toFixed(1)}s</td>
        <td class="st ${GC[s.grade]||''}">${G[s.grade]||""}</td>
        <td><button class="small" onclick="resetHealth('${s.key}')">清零</button></td></tr>`;
    }).join("");
  }catch(e){box.innerHTML='<tr><td colspan="6" class="muted">读取失败</td></tr>';}
}
async function resetHealth(key){
  await jpost("/v/api/ext/source-health/reset",{key});loadHealth();
}
async function addSource(){
  const name=$("addName").value.trim(),api=$("addApi").value.trim(),standalone=$("addStandalone").checked;
  if(!api){toast("请填写接口地址","fail");return;}
  const r=await jpost("/v/api/ext/source/add",{name,api,standalone});
  if(r.code===0){toast("已添加");$("addName").value="";$("addApi").value="";$("addStandalone").checked=false;
    await loadStatus();const tr=$("srcRows").querySelector(`tr[data-id="${r.data.id}"]`);if(tr&&!standalone)testOne(r.data.id,tr);}
  else toast(r.msg||"添加失败","fail");
}
/* ---------- 资源站取源代理（搜索/分类/测速，视频流仍直连；移植上游 v0.2.46/55） ---------- */
function initSrcProxy(){
  const p=(CFG&&CFG.source_proxy)||"";
  const en=$("srcProxyEnable"),inp=$("srcProxyUrl"),sb=$("srcProxySaveBtn"),cb=$("srcProxyClearBtn"),msg=$("srcProxyMsg");
  if(!en)return;
  inp.value=p;en.checked=!!p;inp.disabled=!en.checked;sb.disabled=!en.checked;cb.disabled=!p;
  msg.textContent=p?"取源请求按下方模式决定是否经代理，视频流仍直连":"当前未设代理（全部直连）";
  msg.style.color="";renderSrcProxyMode();
}
const SRC_MODE_HINT={
  auto:"🪄 智能：默认直连；只有直连失败的资源站自动切代理，其它站保持直连。",
  on:"始终走代理：所有资源站搜索/分类/测速/解析都经代理，视频流仍直连。",
  off:"全部直连：忽略已配置的代理地址。"
};
function renderSrcProxyMode(){
  const mode=(CFG&&CFG.source_proxy_mode)||"auto",seg=$("srcProxyMode"),hint=$("srcProxyModeHint");
  if(!seg)return;
  seg.querySelectorAll("button").forEach(b=>b.classList.toggle("active",b.dataset.mode===mode));
  if(hint)hint.textContent=SRC_MODE_HINT[mode]||SRC_MODE_HINT.auto;
}
async function setSrcProxyMode(mode){
  const proxy=$("srcProxyUrl").value.trim();
  if(mode!=="off"&&proxy&&proxy!==((CFG&&CFG.source_proxy)||""))await saveSrcProxy(false);
  const r=await jpost("/v/api/ext/source-proxy",{proxy:(CFG&&CFG.source_proxy)||"",mode});
  if(r.code===0){CFG.source_proxy_mode=(r.data&&r.data.mode)||mode;renderSrcProxyMode();
    toast({auto:"已切换为智能模式",on:"已切换为始终走代理",off:"已切换为全部直连"}[mode]);
  }else toast(r.msg||"切换失败","fail");
}
function onSrcProxyToggle(){
  const en=$("srcProxyEnable"),inp=$("srcProxyUrl"),sb=$("srcProxySaveBtn");
  inp.disabled=!en.checked;sb.disabled=!en.checked;if(en.checked)inp.focus();
}
async function saveSrcProxy(test){
  const inp=$("srcProxyUrl"),msg=$("srcProxyMsg"),proxy=inp.value.trim();
  if(!proxy){toast("请填写代理地址，或点清空直连","fail");return;}
  msg.textContent="保存并测试资源站连通性…";msg.style.color="";
  try{
    const mode=(CFG&&CFG.source_proxy_mode)||"auto";
    const r=await jpost("/v/api/ext/source-proxy",{proxy,mode});
    if(r.code!==0){msg.textContent="未保存："+(r.msg||"格式错误");msg.style.color="var(--err)";return;}
    CFG.source_proxy=proxy;CFG.source_proxy_mode=(r.data&&r.data.mode)||mode;renderSrcProxyMode();
    const d=r.data||{};
    if(test){msg.textContent=d.test_ok?("✓ 已保存，经代理测速成功（"+(d.test_msg||"")+"）")
      :("已保存，但经代理测速失败："+(d.test_msg||"可稍后在资源站页测速"));
      msg.style.color=d.test_ok?"var(--ok)":"var(--warn)";
    }else msg.textContent="已保存";
    $("srcProxyClearBtn").disabled=false;toast("源代理已保存");
  }catch(e){msg.textContent="请求失败："+String(e);msg.style.color="var(--err)";}
}
async function clearSrcProxy(){
  if(!confirm("清空资源站代理地址并全部直连？"))return;
  const r=await jpost("/v/api/ext/source-proxy",{proxy:"",mode:"off"});
  if(r.code===0){CFG.source_proxy="";CFG.source_proxy_mode="off";
    $("srcProxyUrl").value="";$("srcProxyEnable").checked=false;
    $("srcProxyUrl").disabled=true;$("srcProxySaveBtn").disabled=true;$("srcProxyClearBtn").disabled=true;
    renderSrcProxyMode();$("srcProxyMsg").textContent="已清空，当前直连";toast("已清空代理");
  }else toast(r.msg||"失败","fail");
}
// ---------- 订阅抓取代理（仅拉订阅/jar/脚本，不影响播放） ----------
function initSubProxy(){
  const p=(CFG&&CFG.subscription_proxy)||"";
  const en=$("subProxyEnable"),inp=$("subProxyUrl"),sb=$("subProxySaveBtn"),cb=$("subProxyClearBtn"),msg=$("subProxyMsg");
  if(!en)return;
  inp.value=p;en.checked=!!p;
  inp.disabled=!en.checked;sb.disabled=!en.checked;cb.disabled=!p;
  msg.textContent=p?"当前经代理拉取订阅资源":"当前直连（未设代理）";
  msg.style.color="";
}
function onSubProxyToggle(){
  const en=$("subProxyEnable"),inp=$("subProxyUrl"),sb=$("subProxySaveBtn");
  inp.disabled=!en.checked;sb.disabled=!en.checked;
  if(en.checked)inp.focus();
}
async function saveSubProxy(test){
  const inp=$("subProxyUrl"),msg=$("subProxyMsg");
  const proxy=inp.value.trim();
  if(!proxy){toast("请填写代理地址，或点清空直连","fail");return;}
  msg.textContent="保存并测试连通性…";msg.style.color="";
  try{
    const r=await jpost("/v/api/ext/subscription-proxy",{proxy});
    if(r.code!==0){msg.textContent="未保存："+(r.msg||"格式错误");msg.style.color="var(--danger)";return;}
    const d=r.data||{};
    if(test){
      msg.textContent=d.test_ok?("✓ 已保存，代理连通（"+(d.test_msg||"")+"），仅作用于订阅资源抓取")
                               :("已保存，但代理测试失败："+(d.test_msg||""));
      msg.style.color=d.test_ok?"var(--ok,#3a9)":"var(--warn,#d59a2b)";
    }else{msg.textContent="已保存";}
    $("subProxyClearBtn").disabled=false;
    toast("代理已保存");
  }catch(e){msg.textContent="请求失败："+String(e);msg.style.color="var(--danger)";}
}
async function clearSubProxy(){
  if(!confirm("清空订阅代理、改为直连？"))return;
  const r=await jpost("/v/api/ext/subscription-proxy",{proxy:""});
  if(r.code===0){$("subProxyUrl").value="";$("subProxyEnable").checked=false;
    $("subProxyUrl").disabled=true;$("subProxySaveBtn").disabled=true;$("subProxyClearBtn").disabled=true;
    $("subProxyMsg").textContent="已清空，当前直连";$("subProxyMsg").style.color="";toast("已切回直连");}
  else toast(r.msg||"失败","fail");
}
async function loadAdfilter(){
  const box=$("adfStatus"), cb=$("adfEnable");
  try{
    const r=await jget("/v/api/ext/adfilter/status");
    if(r.code!==0){box.textContent="去广告状态读取失败";return;}
    const d=r.data||{};
    if(cb)cb.checked=!!d.enabled;
    const dyn=$("adfDynamic"); if(dyn)dyn.checked=d.dynamic!==false;
    const sps=$("adfSps"); if(sps)sps.checked=d.sps!==false;
    let h="";
    if(d.enabled) h+='<span style="color:var(--ok,#3a9)">● 已启用</span>　';
    else h+='<span class="muted">○ 已关闭</span>　';
    h+="已收录订阅规则 <b>"+(d.rule_groups||0)+"</b> 组 / 域名 "+(d.hosts||[]).length
       +" 个 / 规则 "+(d.regex_count||0)+" 条";
    if((d.groups||[]).length){
      h+='<div class="muted sm" style="margin-top:6px">覆盖域名：'
        +d.groups.map(g=>esc((g.name||"")+"("+(g.hosts||[]).join(",")+")")).slice(0,12).join("、")
        +(d.groups.length>12?" 等":"")+'</div>';
    }else{
      h+='<div class="muted sm" style="margin-top:6px">尚无订阅自带规则；仍会通用清理标准广告标记。导入带 rules 的订阅后自动收录。</div>';
    }
    box.innerHTML=h;
  }catch(e){box.textContent="去广告状态读取失败："+esc(String(e));}
}
async function toggleAdfilter(on){
  try{
    const r=await jpost("/v/api/ext/adfilter/toggle",{enabled:!!on});
    if(r.code===0){toast(on?"已开启播放去广告":"已关闭播放去广告");loadAdfilter();}
    else{toast(r.msg||"操作失败","fail");}
  }catch(e){toast(String(e),"fail");}
}
async function toggleAdfilterDynamic(on){
  try{
    const r=await jpost("/v/api/ext/adfilter/toggle",{dynamic:!!on});
    if(r.code===0){toast(on?"已开启播放时动态检测":"已关闭动态检测");loadAdfilter();}
    else toast(r.msg||"操作失败","fail");
  }catch(e){toast(String(e),"fail");}
}
async function toggleAdfilterSps(on){
  try{
    const r=await jpost("/v/api/ext/adfilter/toggle",{sps:!!on});
    if(r.code===0){toast(on?"已开启 SPS 指纹检测":"已关闭 SPS 指纹检测");loadAdfilter();}
    else toast(r.msg||"操作失败","fail");
  }catch(e){toast(String(e),"fail");}
}
async function importSubscription(kind){
  const standalone=$("subStandalone").checked;
  const payload={standalone:standalone,enable_guard:"smart"};
  if(kind==="url"){payload.url=$("subUrl").value.trim();if(!payload.url){toast("请填写订阅 URL","fail");return;}}
  else{payload.text=$("subText").value.trim();if(!payload.text){toast("请粘贴配置 JSON","fail");return;}}
  const out=$("subResult");out.innerHTML='<div class="muted sm">正在解析订阅并探测源，请稍候…</div>';
  try{
    const r=await jpost("/v/api/ext/source/import-subscription",payload);
    if(r.code!==0){out.innerHTML='<div class="hint" style="color:var(--danger)">导入失败：'+esc(r.msg||"")+'</div>';return;}
    renderSubResult(r.data||{});
    toast("导入完成");
    try{loadAdfilter();}catch(e){}
    try{await loadStatus();}catch(e){}
    try{await loadGuard();}catch(e){}
    try{await loadSpiderMgmt();}catch(e){}
    try{await loadDrpyMgmt();}catch(e){}
  }catch(e){out.innerHTML='<div class="hint" style="color:var(--danger)">导入出错：'+esc(String(e))+'</div>';}
}
async function subToggleGuard(key,on){
  try{
    const r=await jpost("/v/api/ext/guard/toggle",{key,enabled:!!on});
    if(r.code===0){toast(on?"已启用":"已停用");try{await loadStatus();}catch(e){}}
    else toast(r.msg||"操作失败","fail");
  }catch(e){toast(String(e),"fail");}
}
async function importFantaiying(){
  const out=$("subResult");
  out.innerHTML='<div class="muted sm">正在拉取内置「饭太硬」官方订阅（多镜像自动切换、自动解密），请稍候…</div>';
  try{
    const r=await jpost("/v/api/ext/source/import-subscription",
      {builtin:"fantaiying",standalone:$("subStandalone").checked,enable_guard:"smart"});
    if(r.code!==0){out.innerHTML='<div class="hint" style="color:var(--danger)">导入失败：'+esc(r.msg||"")+'</div>';return;}
    renderSubResult(r.data||{});
    toast("饭太硬导入完成");
    try{loadAdfilter();}catch(e){}
    try{await loadStatus();}catch(e){}
    try{await loadGuard();}catch(e){}
    try{await loadSpiderMgmt();}catch(e){}
    try{await loadDrpyMgmt();}catch(e){}
  }catch(e){out.innerHTML='<div class="hint" style="color:var(--danger)">导入出错：'+esc(String(e))+'</div>';}
}
function goDeploy(){switchPage("tvbox");setTimeout(()=>{const b=$("scInstallAll");if(b)b.click();},80);}
function renderSubResult(d){
  const cms=d.cms||{},t4=d.t4||{},g=d.guard||{},rt=d.runtime,skips=d.skipped_detail||[];
  let h="";
  // 运行时引导（加固源已登记但运行时未就绪 / 公开构建）
  if(g && !g.skipped_build && g.items && g.items.length && rt && !rt.ready){
    h+='<div style="margin:10px 0;padding:10px 12px;border:1px solid var(--warn,#d59a2b);border-radius:8px;background:rgba(213,154,43,.08)">'
     + '<div style="font-weight:600;margin-bottom:4px">已导入 '+g.items.length+' 个加固秒播源，但本地安卓运行时未就绪</div>'
     + '<div class="muted sm" style="margin-bottom:8px">'+esc(rt.reason||"运行时未连接")+'。源已保存，部署运行时后立即可播，无需重新导入。</div>'
     + '<button class="primary small" onclick="goDeploy()">一键部署外挂运行时</button></div>';
  }
  if(g && g.skipped_build){
    h+='<div class="hint" style="margin:10px 0">订阅中含加固源，但当前为公开构建（无安卓运行时），已仅导入苹果CMS源。</div>';
  }
  // 汇总
  const cmsOk=cms.added||0, gOk=(g.items||[]).filter(x=>x.enabled).length;
  const csp=d.csp||{}, dr=d.drpy||{};
  const cspOk=(csp.items||[]).filter(x=>x.enabled).length;
  const drOk=(dr.items||[]).filter(x=>x.enabled).length;
  const totalOk=cmsOk+gOk+cspOk+drOk+(t4.added||0);
  h+='<div style="margin:8px 0;font-weight:600">共扫描 '+(d.scanned||0)+' 个站点'
    +(d.subscriptions?('（含 '+d.subscriptions+' 个子订阅）'):'')+'，新增登记 <b style="color:var(--ok,#3a9)">'+totalOk+'</b> 个。</div>';
  // 苹果CMS
  if(cms.candidates||cms.added||cms.duplicate){
    h+='<div class="subgrp"><div class="subgrp-t">🍿 苹果CMS影视站　新增 '+cmsOk+' · 已存在 '+(cms.duplicate||0)+'</div>';
    (cms.added_names||[]).forEach(n=>{h+='<div class="subrow ok"><span>＋ '+esc(n)+'</span><span class="tag">已启用</span></div>';});
    h+='</div>';
  }
  if(t4.candidates||t4.added||t4.duplicate){
    h+='<div class="subgrp"><div class="subgrp-t">T4 远程源　新增 '+(t4.added||0)+' · 已存在 '+(t4.duplicate||0)+'</div>';
    (t4.added_names||[]).forEach(n=>{h+='<div class="subrow ok"><span>＋ '+esc(n)+'</span><span class="tag">已登记</span></div>';});
    h+='</div>';
  }
  // 加固
  if(g && g.items && g.items.length){
    h+='<div class="subgrp"><div class="subgrp-t">🛡️ Guard 加固秒播源　自动启用 '+gOk+' · 风险默认关 '+(g.risk_names||[]).length+'</div>';
    g.items.forEach(it=>{
      const tag=it.enabled?'<span class="tag">已启用</span>'
        : ('<span class="tag warn">'+(it.risk?esc(it.risk_reason||"风险源"):"默认关")+'</span>');
      const cb='<label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">'
        +'<input type="checkbox" '+(it.enabled?"checked":"")
        +' onchange="subToggleGuard(\''+esc(it.key)+'\',this.checked)" style="width:14px;height:14px;accent-color:var(--accent)"/>启用</label>';
      h+='<div class="subrow"><span>'+esc(it.name||it.key)+'</span><span style="display:flex;align-items:center;gap:8px">'+tag+cb+'</span></div>';
    });
    h+='</div>';
  }
  if(g && g.error){h+='<div class="hint" style="color:var(--danger)">加固源写入失败：'+esc(g.error)+'</div>';}
  // 普通 csp_ 爬虫
  if(csp && csp.skipped_build){
    h+='<div class="hint" style="margin:10px 0">订阅中含普通爬虫源，但当前为公开构建（无安卓运行时），已跳过。</div>';
  } else if(csp && (csp.items||[]).length){
    const jar=csp.jar||{};
    const jarBad=jar && jar.ok===false;
    h+='<div class="subgrp"><div class="subgrp-t">🕷️ 普通爬虫源（csp_）　自动启用 '+cspOk+' / '+(csp.items||[]).length
      +(jarBad?' <span class="tag warn">jar 未就绪</span>':'')+'</div>';
    if(jarBad){
      h+='<div style="margin:6px 0;padding:8px 10px;border:1px solid var(--warn,#d59a2b);border-radius:8px;background:rgba(213,154,43,.08)">'
       + '<div class="muted sm">爬虫 jar 自动下载失败：'+esc(jar.error||"源站不可达")+'</div>'
       + '<div class="muted sm" style="margin:3px 0 6px;word-break:break-all">'+esc(jar.url||"")+'</div>'
       + '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
       + '<button class="small" onclick="retryJar(this,\''+esc(jar.url||"")+'\')">重试下载</button>'
       + '<label class="small" style="border:1px solid var(--line,#ccc);border-radius:6px;padding:4px 10px;cursor:pointer">手动补传 jar'
       + '<input type="file" accept=".jar,.zip" style="display:none" onchange="uploadJar(this,\''+esc(jar.url||"")+'\')"></label></div></div>';
    }
    csp.items.forEach(it=>{
      const state=it.enabled?'<span class="tag">已启用</span>'
        : (it.risk?'<span class="tag warn">风险源·默认关</span>'
           : (it.has_jar?'<span class="tag warn">默认关</span>':'<span class="tag warn">缺 jar</span>'));
      const cb='<label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">'
        +'<input type="checkbox" '+(it.enabled?"checked":"")
        +' onchange="spToggle(this,\''+esc(it.key)+'\')" style="width:14px;height:14px;accent-color:var(--accent)"/>启用</label>';
      h+='<div class="subrow"><span>'+esc(it.name||it.key)+'</span><span style="display:flex;align-items:center;gap:8px">'+state+cb+'</span></div>';
    });
    h+='</div>';
  }
  if(csp && csp.error){h+='<div class="hint" style="color:var(--danger)">普通爬虫写入失败：'+esc(csp.error)+'</div>';}
  // 远程 DRPY
  if(dr && dr.skipped_build){
    h+='<div class="hint" style="margin:10px 0">订阅中含远程 JS 爬虫，但当前为公开构建（无 Node 运行时），已跳过。</div>';
  } else if(dr && (dr.items||[]).length){
    h+='<div class="subgrp"><div class="subgrp-t">🧩 远程 JS 爬虫（DRPY）　就绪并启用 '+drOk+' / '+dr.items.length+'</div>';
    dr.items.forEach(it=>{
      const state=it.enabled?'<span class="tag">已启用</span>'
        :'<span class="tag warn">脚本未就绪</span>';
      h+='<div class="subrow"><span>'+esc(it.name||it.key)+'</span>'+state+'</div>';
    });
    h+='<div class="muted sm" style="padding:4px 2px">脚本若 401/下载失败，可在下方「已导入的远程 JS 爬虫」处重试。</div></div>';
  }
  if(dr && dr.error){h+='<div class="hint" style="color:var(--danger)">远程JS导入失败：'+esc(dr.error)+'</div>';}
  // 跳过
  if(skips.length){
    h+='<details class="subgrp" style="margin-top:8px"><summary class="subgrp-t" style="cursor:pointer">⏭️ 跳过 '+ (d.skipped||skips.length) +' 个（网盘/直播/无法识别/探测失败）</summary>';
    const by={};skips.forEach(s=>{const r=s.reason||"其他";(by[r]=by[r]||[]).push(s.name);});
    Object.keys(by).forEach(r=>{h+='<div class="subrow muted"><span>'+esc(by[r].slice(0,10).join("、"))+(by[r].length>10?" 等":"")+'</span><span class="tag">'+esc(r)+'</span></div>';});
    h+='</details>';
  }
  $("subResult").innerHTML=h;
}
// ---------- 已导入普通爬虫源管理 ----------
/* >>> PRIVATE:SPIDER */
async function loadSpiderMgmt(){
  const box=$("spiderMgmt");if(!box)return;
  try{
    const r=await jget("/v/api/ext/spider/status");
    if(r.code!==0){box.innerHTML='<div class="hint">加载失败</div>';return;}
    const d=r.data||{},sites=d.sites||[];
    if(!sites.length){box.innerHTML='<div class="muted sm">暂无导入的普通爬虫源。</div>';return;}
    // 按 jar_url 聚合缺 jar 提示
    const jarMissing=[...new Set(sites.filter(s=>!s.jar_ready).map(s=>s.jar_url))];
    let h='';
    if(!d.available){
      h+='<div style="margin-bottom:8px;padding:8px 10px;border:1px solid var(--warn,#d59a2b);border-radius:8px;background:rgba(213,154,43,.08)" class="sm">'
        +'本地安卓运行时未就绪，已导入的爬虫源暂不可播放；可在「TVBox加固源」页一键部署。</div>';
    }
    jarMissing.forEach(ju=>{
      if(!ju)return;
      h+='<div style="margin-bottom:8px;padding:8px 10px;border:1px solid var(--warn,#d59a2b);border-radius:8px;background:rgba(213,154,43,.08)">'
       +'<div class="muted sm" style="word-break:break-all">jar 未就绪：'+esc(ju)+'</div>'
       +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:5px">'
       +'<button class="small" onclick="retryJar(this,\''+esc(ju)+'\')">重试下载</button>'
       +'<label class="small" style="border:1px solid var(--line,#ccc);border-radius:6px;padding:4px 10px;cursor:pointer">手动补传 jar'
       +'<input type="file" accept=".jar,.zip" style="display:none" onchange="uploadJar(this,\''+esc(ju)+'\')"></label></div></div>';
    });
    h+='<div class="subgrp"><div class="subgrp-t">共 '+sites.length+' 个 · 启用 '+(d.enabled||0)+'</div>';
    sites.forEach(s=>{
      const tag=s.enabled?'<span class="tag">已启用</span>'
        : (s.risk?'<span class="tag warn">风险源</span>'
           : (s.jar_ready?'<span class="tag warn">已停用</span>':'<span class="tag warn">缺jar</span>'));
      const cb='<label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">'
        +'<input type="checkbox" '+(s.enabled?"checked":"")+' onchange="spToggle(this,\''+esc(s.key)+'\')" '
        +'style="width:14px;height:14px;accent-color:var(--accent)"/>启用</label>';
      const del='<button class="small" style="padding:2px 8px" onclick="spRemove(\''+esc(s.key)+'\',\''+esc((s.name||s.key).replace(/'/g,""))+'\')">删</button>';
      const test='<button class="small" style="padding:2px 8px" onclick="spProbe(this,\''+esc(s.key)+'\')">测试</button>';
      h+='<div class="subrow"><span>'+esc(s.name||s.key)+'</span><span style="display:flex;align-items:center;gap:8px">'+tag+cb+test+del+'</span></div>';
      h+='<div id="probe-'+esc(s.key)+'" class="muted sm" style="display:none;margin:0 0 6px;padding:6px 10px;border-radius:6px;background:rgba(127,127,127,.08)"></div>';
    });
    h+='</div>';
    box.innerHTML=h;
  }catch(e){box.innerHTML='<div class="hint" style="color:var(--danger)">'+esc(String(e))+'</div>';}
}
async function spToggle(el,key){
  try{
    const r=await jpost("/v/api/ext/spider/toggle",{key,enabled:el.checked});
    if(r.code===0){toast(el.checked?"已启用":"已停用");await Promise.all([loadStatus(),loadSpiderMgmt()]);}
    else{toast(r.msg||"操作失败","fail");el.checked=!el.checked;}
  }catch(e){toast(String(e),"fail");el.checked=!el.checked;}
}
async function spRemove(key,name){
  if(!confirm("确定删除爬虫源「"+name+"」？"))return;
  const r=await jpost("/v/api/ext/spider/remove",{key});
  if(r.code===0){toast("已删除");await Promise.all([loadStatus(),loadSpiderMgmt()]);}else toast(r.msg||"删除失败","fail");
}
function _probeBox(key){const b=document.getElementById("probe-"+key);if(b){b.style.display="block";}return b;}
async function spProbe(btn,key){
  const kw=($("spProbeKw")&&$("spProbeKw").value||"庆余年").trim()||"庆余年";
  const doPlay=$("spProbePlay")?$("spProbePlay").checked:false;
  const box=_probeBox(key);
  const old=btn?btn.textContent:"";if(btn){btn.disabled=true;btn.textContent="测试中…";}
  if(box)box.innerHTML='<span class="muted">正在'+(doPlay?'搜索/详情/取播放':'搜索/详情')+'，真机调用约需数秒…</span>';
  try{
    const r=await jpost("/v/api/ext/spider/probe",{key,keyword:kw,do_play:doPlay});
    if(r.code!==0){if(box)box.innerHTML='<span style="color:var(--danger)">失败：'+esc(r.msg||"")+'</span>';return;}
    const d=r.data||{};
    let badge,color;
    if(d.ok===false){
      if(d.verdict==="keyword_miss"){badge="○ 源正常，仅该关键词无资源";color="var(--ok,#3a9)";}
      else if(d.verdict==="search_broken"){badge="△ 源有资源但搜索接口异常";color="var(--warn,#d59a2b)";}
      else if(d.verdict==="spider_error"){badge="⚠ 爬虫代码报错";color="var(--warn,#d59a2b)";}
      else if(d.verdict==="dead_empty"){badge="✗ 空源/已失效（取不到任何资源）";color="var(--danger)";}
      else if(d.verdict==="dead_runtime"){badge="✗ 源失效/无法访问";color="var(--danger)";}
      else{badge="✗ 不可用（"+(d.stage||"")+"）";color="var(--danger)";}
    }
    else if(d.playable===true){badge="✓ 可搜索且能取播放地址";color="var(--ok,#3a9)";}
    else if(d.playable===false){badge="△ 搜索可用，暂不能在此环境取播放";color="var(--warn,#d59a2b)";}
    else{badge="✓ 搜索/详情可用";color="var(--ok,#3a9)";}
    let h='<b style="color:'+color+'">'+esc(badge)+'</b> · 关键词「'+esc(kw)+'」';
    if(d.search_ms!=null)h+=' · 搜索'+d.search_ms+'ms';
    if(d.detail_ms!=null)h+=' · 详情'+d.detail_ms+'ms';
    if(d.play_ms!=null)h+=' · 取播放'+d.play_ms+'ms';
    if(d.sample)h+='<br>样例：'+esc(d.sample)+(d.episodes?(' · 解析到 '+d.episodes+' 集'):'');
    if(d.first_ep)h+=' · 首集「'+esc(d.first_ep)+'」';
    if(d.play_url)h+='<br><span style="word-break:break-all">播放地址：'+esc(d.play_url)+'</span>';
    if(d.note)h+='<br>'+esc(d.note);
    if(d.error)h+='<br><span style="color:'+(d.verdict==='keyword_miss'?'var(--ok,#3a9)':'var(--warn,#d59a2b)')+'">'+esc(d.error)+'</span>';
    if(d.evidence)h+='<br><span class="muted">诊断：'+esc(d.evidence)+'</span>';
    if(box)box.innerHTML=h;
  }catch(e){
    if(box)box.innerHTML='<span style="color:var(--danger)">'+esc(String(e))+'</span>';
  }finally{if(btn){btn.disabled=false;btn.textContent=old;}}
}

// ---------- 一键批量体检 ----------
const SP_VLABEL={playable:"可搜索且可取播放",usable:"可搜索（未/不可取流）",keyword_miss:"源正常·仅该词无片",
 search_broken:"有内容但搜索异常",needs_config:"网盘/工具类·需配置",spider_error:"爬虫代码报错",dead_runtime:"源失效/无法访问",
 dead_empty:"空源/已失效",jar:"jar 未就绪",runtime:"运行时未就绪",unknown:"未知"};
const SP_VCOLOR={playable:"var(--ok,#3a9)",usable:"var(--ok,#3a9)",keyword_miss:"var(--ok,#3a9)",
 search_broken:"var(--warn,#d59a2b)",needs_config:"var(--muted)",spider_error:"var(--danger)",dead_runtime:"var(--danger)",
 dead_empty:"var(--danger)",jar:"var(--warn,#d59a2b)",runtime:"var(--warn,#d59a2b)",unknown:"var(--muted)"};
let _spPoll=null;
async function spScanStart(){
  const kw=($("spProbeKw").value||"庆余年").trim()||"庆余年";
  const doPlay=$("spProbePlay").checked;
  const bar=$("spScanBar"),res=$("spScanResult"),btn=$("spScanBtn");
  bar.style.display="block";res.innerHTML="";
  btn.disabled=true;btn.textContent="体检中…";
  try{
    const r=await jpost("/v/api/ext/spider/scan",{keyword:kw,do_play:doPlay,only_enabled:false});
    if(r.code!==0){btn.disabled=false;btn.textContent="一键全部体检";bar.textContent="启动失败："+(r.msg||"");return;}
    bar.textContent="共 "+r.data.total+" 个源，开始体检…";
    if(_spPoll)clearInterval(_spPoll);
    _spPoll=setInterval(spScanPoll,1500);
    spScanPoll();
  }catch(e){btn.disabled=false;btn.textContent="一键全部体检";bar.textContent=String(e);}
}
async function spScanPoll(){
  try{
    const r=await jget("/v/api/ext/spider/scan");
    if(r.code!==0)return;
    const d=r.data||{},bar=$("spScanBar");
    bar.textContent="进度 "+d.done+" / "+d.total+(d.running?"（体检中…）":"（完成）");
    spRenderScan(d.results||[]);
    if(!d.running){
      clearInterval(_spPoll);_spPoll=null;
      const b=$("spScanBtn");b.disabled=false;b.textContent="重新体检";
    }
  }catch(e){/*轮询偶发忽略*/}
}
function spRenderScan(results){
  const box=$("spScanResult");
  if(!results.length){box.innerHTML="";return;}
  const groups={};
  results.forEach(r=>{(groups[r.verdict]=groups[r.verdict]||[]).push(r);});
  const order=["playable","usable","keyword_miss","search_broken","needs_config","spider_error","dead_runtime","dead_empty","jar","runtime","unknown"];
  let h='<div class="subgrp" style="max-height:none">';
  order.forEach(v=>{
    const arr=groups[v];if(!arr||!arr.length)return;
    const dead=["dead_runtime","dead_empty","spider_error"].includes(v);
    h+='<div class="subgrp-t" style="margin-top:8px;color:'+(SP_VCOLOR[v]||"")+'">'
      +(dead?'<input type="checkbox" onclick="spToggleGroup(\''+v+'\',this.checked)" style="width:13px;height:13px;accent-color:var(--accent);margin-right:5px"/>':'')
      +esc(SP_VLABEL[v]||v)+'（'+arr.length+'）'+(dead?' <span class="muted sm">默认勾选=建议删除</span>':'')+'</div>';
    arr.forEach(r=>{
      // 每行都可勾选（默认仅确认失效源勾选）；其它分组如需删可手动勾
      const ck='<input type="checkbox" class="spdel" data-v="'+v+'" data-key="'+esc(r.key)+'" '
        +(r.suggest_delete?'checked':'')+' style="width:13px;height:13px;accent-color:var(--accent);margin-right:6px"/>';
      const extra=r.sample?(' 《'+esc(r.sample)+'」'+(r.episodes?(' '+r.episodes+'集'):'')):'';
      const why=r.evidence||r.reason||'';
      h+='<div class="subrow" style="align-items:flex-start">'
        +'<span>'+ck+esc(r.name)+' <span class="muted sm">'+esc(r.key)+'</span></span>'
        +'<span class="muted sm" style="max-width:60%;text-align:right">'+extra+(why?(' · '+esc(why)):'')+'</span></div>';
    });
  });
  h+='</div>';
  box.innerHTML=h;
  const n=box.querySelectorAll('.spdel:checked').length;
  const db=$("spDelBtn");db.style.display=n?'inline-block':'none';
  db.textContent="删除勾选失效源（"+n+"）";
  box.querySelectorAll('.spdel').forEach(c=>c.onchange=()=>{
    const nn=box.querySelectorAll('.spdel:checked').length;
    db.style.display=nn?'inline-block':'none';db.textContent="删除勾选失效源（"+nn+"）";});
}
function spToggleGroup(v,on){
  document.querySelectorAll('.spdel[data-v="'+v+'"]').forEach(c=>c.checked=on);
  const box=$("spScanResult"),db=$("spDelBtn"),nn=box.querySelectorAll('.spdel:checked').length;
  db.style.display=nn?'inline-block':'none';db.textContent="删除勾选失效源（"+nn+"）";
}
async function spDeleteChecked(){
  const keys=[...document.querySelectorAll('.spdel:checked')].map(c=>c.getAttribute("data-key"));
  if(!keys.length){toast("未勾选任何源","fail");return;}
  if(!confirm("确定删除选中的 "+keys.length+" 个失效/报错源？删除后需重新导入才能恢复。"))return;
  try{
    const r=await jpost("/v/api/ext/spider/remove-many",{keys});
    if(r.code!==0){toast(r.msg||"删除失败","fail");return;}
    toast("已删除 "+(r.data.count||0)+" 个源");
    await Promise.all([loadStatus(),loadSpiderMgmt()]);
    // 从体检结果里移除已删项
    const cur=await jget("/v/api/ext/spider/scan");
    const live=new Set((cur.data.results||[]).map(x=>x.key));
    document.querySelectorAll('.spdel').forEach(c=>{if(keys.includes(c.getAttribute("data-key")))c.closest('.subrow').remove();});
  }catch(e){toast(String(e),"fail");}
}
function _b64File(file){return new Promise((res,rej)=>{const f=new FileReader();f.onload=()=>res(f.result.split(",")[1]);f.onerror=rej;f.readAsDataURL(file);});}
async function uploadJar(input,jarUrl){
  const file=input.files&&input.files[0];if(!file)return;
  const btn=input.parentElement;const old=btn.innerHTML;btn.innerHTML='上传中…';
  try{
    const data_b64=await _b64File(file);
    const r=await jpost("/v/api/ext/spider/jar",{jar_url:jarUrl,name:file.name,data_b64});
    if(r.code===0){toast("jar 已补传"+((r.data.auto_enabled||[]).length?("，已启用 "+r.data.auto_enabled.length+" 个源"):""));
      await Promise.all([loadStatus(),loadSpiderMgmt()]);}
    else toast(r.msg||"上传失败","fail");
  }catch(e){toast(String(e),"fail");}
  btn.innerHTML=old;input.value="";
}
async function retryJar(btn,jarUrl){
  const old=btn.innerHTML;btn.innerHTML='下载中…';
  try{
    const r=await jpost("/v/api/ext/spider/jar-retry",{jar_url:jarUrl});
    if(r.code===0){toast("下载成功"+((r.data.auto_enabled||[]).length?("，已启用 "+r.data.auto_enabled.length+" 源"):""));
      await Promise.all([loadStatus(),loadSpiderMgmt()]);}
    else toast(r.msg||"下载仍失败，可手动补传","fail");
  }catch(e){toast(String(e),"fail");}
  btn.innerHTML=old;
}
/* <<< PRIVATE:SPIDER */
/* >>> PRIVATE:DRPY */
// ---------- 已导入远程 DRPY 管理 ----------
async function loadDrpyMgmt(){
  const box=$("drpyMgmt");if(!box)return;
  try{
    const r=await jget("/v/api/ext/drpy/remote");
    if(r.code!==0){box.innerHTML='<div class="hint">加载失败</div>';return;}
    const d=r.data||{},sites=d.sites||[];
    if(!sites.length){box.innerHTML='<div class="muted sm">暂无导入的远程 JS 爬虫。</div>';return;}
    let h='<div class="subgrp"><div class="subgrp-t">共 '+sites.length+' 个</div>';
    sites.forEach(s=>{
      const tag=s.enabled?'<span class="tag">已启用</span>'
        :'<span class="tag warn">'+(s.script_ready?'已停用':'脚本未就绪')+'</span>';
      const cb=s.script_ready?('<label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">'
        +'<input type="checkbox" '+(s.enabled?"checked":"")+' onchange="drToggle(this,\''+esc(s.key)+'\')" '
        +'style="width:14px;height:14px;accent-color:var(--accent)"/>启用</label>'):'';
      const rt=s.script_ready?'':'<button class="small" style="padding:2px 8px" onclick="drRetry(\''+esc(s.key)+'\')">重试脚本</button>';
      const del='<button class="small" style="padding:2px 8px" onclick="drRemove(\''+esc(s.key)+'\')">删</button>';
      h+='<div class="subrow"><span>'+esc(s.name)+'</span><span style="display:flex;align-items:center;gap:8px">'+tag+cb+rt+del+'</span></div>';
    });
    h+='</div>';
    box.innerHTML=h;
  }catch(e){box.innerHTML='<div class="hint" style="color:var(--danger)">'+esc(String(e))+'</div>';}
}
async function drToggle(el,key){
  try{
    const r=await jpost("/v/api/ext/drpy/remote-toggle",{key,enabled:el.checked});
    if(r.code===0){toast(el.checked?"已启用":"已停用");await Promise.all([loadStatus(),loadDrpyMgmt()]);}
    else{toast(r.msg||"操作失败","fail");el.checked=!el.checked;}
  }catch(e){toast(String(e),"fail");el.checked=!el.checked;}
}
async function drRemove(key){
  if(!confirm("确定删除该远程 JS 源？"))return;
  const r=await jpost("/v/api/ext/drpy/remote-remove",{key});
  if(r.code===0){toast("已删除");await Promise.all([loadStatus(),loadDrpyMgmt()]);}else toast(r.msg||"删除失败","fail");
}
async function drRetry(key){
  const r=await jpost("/v/api/ext/drpy/remote-retry",{key});
  if(r.code===0){toast("脚本下载成功，可启用");await loadDrpyMgmt();}else toast(r.msg||"重试失败","fail");
}
/* <<< PRIVATE:DRPY */
async function delSource(id,name){
  if(!confirm("确定删除资源站「"+name+"」？"))return;
  const r=await jpost("/v/api/ext/source/delete",{id});
  if(r.code===0){toast("已删除");await loadStatus();}else toast(r.msg||"删除失败","fail");
}
async function restoreBuiltin(){
  const r=await jpost("/v/api/ext/source/restore",{});
  if(r.code===0){toast("已恢复内置源");await loadStatus();}else toast("失败","fail");
}

/* ---------- CMS 源导入 / 导出（移植上游 v0.2.69） ---------- */
async function exportCmsSources(){
  try{
    const r=await jget("/v/api/ext/source/export"),list=(r.data&&r.data.sources)||[];
    if(!list.length){toast("当前没有可导出的 CMS 源","fail");return;}
    const blob=new Blob([JSON.stringify(r.data,null,2)],{type:"application/json"});
    const a=document.createElement("a");a.href=URL.createObjectURL(blob);
    a.download="fnmedia-cms-sources.json";document.body.appendChild(a);a.click();
    setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},1000);
    toast("已导出 "+list.length+" 个 CMS 源");
  }catch(e){toast("导出失败："+e.message,"fail");}
}
async function importCmsFile(input){
  const f=input.files&&input.files[0];if(!f)return;
  try{
    const text=await f.text();let data;
    try{data=JSON.parse(text);}catch(_){toast("文件不是合法 JSON","fail");input.value="";return;}
    const body=Array.isArray(data)?{sources:data}:data;
    const r=await jpost("/v/api/ext/source/import-file",body);
    if(r.code===0){const d=r.data||{};
      toast(`导入完成：新增 ${d.added}，已存在跳过 ${d.skipped}，忽略非CMS ${d.ignored_noncms}`);
      await loadStatus();renderSources();
    }else toast(r.msg||"导入失败","fail");
  }catch(e){toast("导入失败："+e.message,"fail");}
  finally{input.value="";}
}

/* ---------- 搜索设置 / 噪音词 / 缓存（缓存管理移植上游 v0.2.54） ---------- */
async function loadCacheStatus(){
  const box=$("cacheGrid");if(!box)return;
  try{
    const r=await jget("/v/api/ext/cache"),d=r.data||{};
    const fmtB=n=>n>=1048576?(n/1048576).toFixed(2)+" MB":(n>=1024?(n/1024).toFixed(1)+" KB":n+" B");
    const items=[
      ["titles","在线资源缓存",d.titles&&(fmtB(d.titles.bytes)+" · "+d.titles.entries+" 条"),"清后将重启服务"],
      ["charts","热门榜单缓存",d.charts&&fmtB(d.charts.bytes),"后台自动重抓"],
      ["catalog","海量库分类/分页",d.catalog&&(d.catalog.routes+" 路由 · "+d.catalog.pages+" 分页"),"下次浏览重建"],
      ["search","搜索结果内存缓存",d.search&&(d.search.entries+" 条"),"即时清空不重启"],
      ["health","源健康画像",d.health&&(d.health.entries+" 源"),"清空后重新画像"],
    ];
    box.innerHTML=items.map(([k,label,val,note])=>`<div class="cache-item">
      <div class="cn">${label}</div><div class="cv">${val||"-"}<br><span style="opacity:.7">${note}</span></div>
      <button class="small danger" style="width:100%" onclick="clearCaches(['${k}'])">清除</button></div>`).join("");
  }catch(e){box.innerHTML='<div class="muted sm">读取失败</div>';}
}
async function clearCaches(targets){
  const labels={titles:"在线资源缓存（会重启服务）",charts:"热门榜单",catalog:"海量库",search:"搜索缓存",health:"源健康画像"};
  const names=targets.map(t=>labels[t]||t).join("、");
  if(!confirm("确定清除："+names+" ？"))return;
  const r=await jpost("/v/api/ext/cache/clear",{targets});
  if(r.code===0){toast(r.msg||"已清除");
    if((r.data||{}).restart)setTimeout(()=>location.reload(),1800);
    else setTimeout(loadCacheStatus,600);
  }else toast(r.msg||"清除失败","fail");
}
function renderKw(){
  $("kwN").textContent="（共 "+KWS.length+" 个）";
  $("kwChips").innerHTML=KWS.map((k,i)=>`<span class="chip">${esc(k)}<b data-i="${i}" title="删除">×</b></span>`).join("");
  $("kwChips").querySelectorAll("b").forEach(b=>b.onclick=()=>{KWS.splice(+b.dataset.i,1);saveKw();});
}
async function saveKw(){
  const r=await jpost("/v/api/ext/junk-keywords",{keywords:KWS});
  if(r.code===0){KWS=r.data.junk_keywords;CFG.junk_keywords=KWS.slice();renderKw();toast("关键词已保存");}
  else toast(r.msg||"保存失败","fail");
}
async function addKw(){
  const v=$("kwAdd").value.trim();if(!v)return;
  v.split(/[,，、\s]+/).filter(Boolean).forEach(k=>{if(!KWS.includes(k))KWS.push(k);});
  $("kwAdd").value="";await saveKw();
}
function toggleKwEdit(){const box=$("kwEdit");const show=box.style.display==="none";
  box.style.display=show?"block":"none";if(show)$("kwArea").value=KWS.join("\n");}
async function applyKwText(){
  KWS=$("kwArea").value.split(/[,，、\n\r;；\s]+/).map(s=>s.trim()).filter(Boolean).filter((k,i,a)=>a.indexOf(k)===i);
  $("kwEdit").style.display="none";await saveKw();
}
async function resetKw(){
  if(!confirm("恢复为默认关键词列表？"))return;
  const r=await jpost("/v/api/ext/junk-keywords",{reset:true});
  if(r.code===0){KWS=r.data.junk_keywords;CFG.junk_keywords=KWS.slice();renderKw();$("kwEdit").style.display="none";toast("已恢复默认");}
  else toast("失败","fail");
}
async function saveCfg(){
  const body={max_per_keyword:parseInt($("maxn").value)||12,filter_junk:$("junk").checked,online_first:$("onlineFirst").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){toast("已保存");Object.assign(CFG,body);}else toast("保存失败 "+r.msg,"fail");
}
async function saveLines(){
  const body={line_fastest:$("lineFastest").checked,
    line_fastest_max:Math.max(2,Math.min(12,parseInt($("fastestMax").value)||6)),
    line_fastest_timeout:Math.max(1,Math.min(10,parseFloat($("fastestTimeout").value)||4)),
    line_autofail:$("lineAutofail").checked,
    fixed_line_direct:$("fixedLineDirect").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){toast("已保存");Object.assign(CFG,body);}else toast("保存失败 "+r.msg,"fail");
}
/* ---------- 本片线路预测开关（后端 /v/api/ext/linepredict/config 已在；上游 admin_ui 无此开关，按 fork 既有写法补） ---------- */
async function saveLinePredict(){
  const body={enabled:$("linePredict").checked,
    max_lines:Math.max(1,Math.min(3,parseInt($("linePredictMax").value)||3)),
    ttl_min:Math.max(5,Math.min(120,parseInt($("linePredictTtl").value)||30))};
  const r=await jpost("/v/api/ext/linepredict/config",body);
  if(r.code===0){toast("预测设置已保存");Object.assign(CFG,{linepredict_enabled:body.enabled,linepredict_max_lines:body.max_lines,linepredict_ttl_min:body.ttl_min});}
  else toast("保存失败 "+(r.msg||""),"fail");
}
async function resetLinePredict(){
  if(!confirm("清空已预测的本片最快线缓存？（不影响竞速实测记忆）"))return;
  const r=await jpost("/v/api/ext/linepredict/reset",{});
  if(r.code===0)toast("预测缓存已清空");else toast("清空失败 "+(r.msg||""),"fail");
}
async function loadOnline(){
  const kw=$("kw").value.trim();
  const r=await jget("/v/api/ext/online?limit=500"+(kw?("&q="+encodeURIComponent(kw)):""));
  const list=r.data||[];$("cacheN").textContent=list.length;
  const tb=$("rows");
  if(!list.length){tb.innerHTML='<tr><td colspan="3" class="muted">暂无，先去飞牛搜索任意中文片名</td></tr>';return;}
  tb.innerHTML=list.map(x=>`<tr><td><div style="font-weight:500">${esc(x.title)}</div>
    <div class="meta">${esc(x.year||"")} ${esc(x.area||"")} · ${esc(x.remarks||"")}</div></td>
    <td>${esc(x.provider)}</td><td>${x.episodes}</td></tr>`).join("");
}

// >>> PRIVATE:GUARD
/* ---------- TVBox 加固（GUARD） ---------- */
let GUARD_SITES=[],SC_TIMER=null;
async function loadGuardZone(){
  try{
    if(!CFG) await loadStatus();
    const cb=$("guardZoneEnabled");
    if(cb) cb.checked=!!(CFG && CFG.guard_zone_enabled===true);
    const cn=$("guardNativeEnabled");
    if(cn) cn.checked=!(CFG && CFG.guard_native_enabled===false); // 默认开
  }catch(e){}
}
async function saveGuardZone(){
  const on=$("guardZoneEnabled").checked;
  try{
    const r=await jpost("/v/api/ext/config",{guard_zone_enabled:on});
    if(r.code===0){if(CFG)CFG.guard_zone_enabled=on; toast(on?"已开启加固源专区媒体库":"已关闭：加固源仅作播放线路，不再生成专区库");}
    else toast(r.msg||"保存失败","fail");
  }catch(e){toast(String(e),"fail");}
}
async function saveGuardNative(){
  const on=$("guardNativeEnabled").checked;
  try{
    const r=await jpost("/v/api/ext/config",{guard_native_enabled:on});
    if(r.code===0){if(CFG)CFG.guard_native_enabled=on; toast(on?"已在飞牛影视注入「🛡加固影视」聚合库":"已关闭「🛡加固影视」聚合库");}
    else toast(r.msg||"保存失败","fail");
  }catch(e){toast(String(e),"fail");}
}

/* ============ 加固影视浏览器 ============ */
let GB_READY=false, GB_SOURCES=[], GB_VIEW="hot", GB_SID="", GB_TID="", GB_PG=1, GB_HASMORE=false, GB_HLS=null;
async function gbInit(){
  if(GB_READY)return; GB_READY=true;
  gbMsg("加载加固源分类…");
  try{
    const r=await jget("/v/api/ext/guard/browse?mode=sites");
    if(r.code!==0){gbMsg(r.msg||"加固运行时未就绪");return;}
    // 过滤纯直播/体育回放源（保留有电影/剧/动漫/综艺分类的源）
    GB_SOURCES=(r.data.sources||[]).filter(s=>{
      const names=s.classes.map(c=>c.name).join(" ");
      const hasVod=/电影|剧集|电视剧|连续剧|动漫|动画|番剧|综艺|短剧|影视|全部/.test(names);
      const isLiveOnly=/赛事直播|体育直播|虎牙|斗鱼|直播/.test(names)&&!/电影|电视剧|剧集|动漫|综艺/.test(names);
      return hasVod&&!isLiveOnly;
    });
    const sel=$("gbSource");
    sel.innerHTML='<option value="">— 选择加固源看分类 —</option>'
      +GB_SOURCES.map(s=>`<option value="${esc(s.key)}">${esc(s.name)}</option>`).join("");
    await gbLoadHot();
  }catch(e){gbMsg("加载失败："+e.message);}
}
function gbMsg(t){const m=$("gbMsg");if(m)m.textContent=t;}
function gbSetLoading(t){const g=$("gbGrid");g.innerHTML=`<div class="muted sm" style="grid-column:1/-1;padding:18px 4px">${esc(t)}</div>`;}
async function gbLoadHot(){
  GB_VIEW="hot";GB_PG=1;$("gbPager").style.display="none";
  gbSetLoading("正在唤醒安卓运行时并聚合各加固源热门，首次约需 10–30 秒…");
  gbMsg("聚合热门中…");
  try{
    const r=await jget("/v/api/ext/guard/browse?mode=hot&limit=80");
    if(r.code!==0){$("gbGrid").innerHTML="";gbMsg(r.msg||"加载失败");return;}
    gbRender((r.data.list)||[]);gbMsg(`聚合热门 · ${(r.data.list||[]).length} 部`);
  }catch(e){gbMsg("聚合失败："+e.message);$("gbGrid").innerHTML="";}
}
function gbOnSource(){
  const key=$("gbSource").value; GB_SID=key;
  const cls=$("gbClass");
  if(!key){cls.innerHTML="";return;}
  const s=GB_SOURCES.find(x=>x.key===key);
  cls.innerHTML=(s?s.classes:[]).map(c=>`<option value="${esc(c.tid)}">${esc(c.name)}</option>`).join("");
  gbOnClass();
}
function gbOnClass(){GB_TID=$("gbClass").value;GB_PG=1;gbLoadCategory();}
function gbPage(d){GB_PG=Math.max(1,GB_PG+d);gbLoadCategory();}
async function gbLoadCategory(){
  if(!GB_SID||!GB_TID)return;
  GB_VIEW="category";
  gbSetLoading(`取片单中（第 ${GB_PG} 页，首次可能较慢）…`);gbMsg("加载中…");
  try{
    const r=await jget(`/v/api/ext/guard/browse?mode=category&sid=${encodeURIComponent(GB_SID)}&tid=${encodeURIComponent(GB_TID)}&pg=${GB_PG}&limit=30`);
    if(r.code!==0){$("gbGrid").innerHTML="";gbMsg(r.msg||"加载失败");return;}
    const L=r.data.list||[];GB_HASMORE=!!r.data.has_more;
    gbRender(L);
    $("gbPager").style.display="flex";$("gbPageNo").textContent="第 "+GB_PG+" 页";
    $("gbPrev").disabled=GB_PG<=1;$("gbNext").disabled=!GB_HASMORE;
    const s=GB_SOURCES.find(x=>x.key===GB_SID);
    gbMsg(`${s?s.name:GB_SID} · ${L.length} 部`);
  }catch(e){gbMsg("加载失败："+e.message);$("gbGrid").innerHTML="";}
}
function gbRender(L){
  const g=$("gbGrid");
  if(!L.length){g.innerHTML='<div class="muted sm" style="grid-column:1/-1;padding:14px">该分类暂无影片或源未响应。</div>';return;}
  g.innerHTML=L.map(c=>`<div class="gb-card" onclick='gbPlay(${JSON.stringify(c).replace(/'/g,"&#39;")})'>
      <div class="gb-poster" style="background-image:url('${c.pic?esc(c.pic):""}')">${c.remarks?`<span class="gb-rem">${esc(c.remarks)}</span>`:""}</div>
      <div class="gb-cap"><div class="gb-t">${esc(c.title)}</div><div class="gb-src">${esc(c.site)}</div></div>
    </div>`).join("");
}
async function gbPlay(c){
  $("gbPlayerMask").style.display="flex";
  $("gbPlayTitle").textContent=c.title;$("gbPlaySite").textContent=c.site;
  const video=$("gbVideo");
  if(GB_HLS){try{GB_HLS.destroy();}catch(e){}GB_HLS=null;}
  video.removeAttribute("src");try{video.load();}catch(e){}
  const url=c.play+(c.play.includes("?")?"&":"?")+(c.line_key?("line="+encodeURIComponent(c.line_key)+"&"):"");
  try{
    if(video.canPlayType("application/vnd.apple.mpegurl")){video.src=url;}
    else if(window.Hls&&Hls.isSupported()){GB_HLS=new Hls({maxBufferLength:30});GB_HLS.loadSource(url);GB_HLS.attachMedia(video);
      GB_HLS.on(Hls.Events.ERROR,(e,d)=>{if(d&&d.fatal)toast("播放失败，可换源重试："+d.type,"fail");});}
    else{toast("当前浏览器不支持 HLS 播放","fail");}
  }catch(e){toast("播放失败："+e.message,"fail");}
}
function gbClosePlayer(){
  const video=$("gbVideo");video.pause();video.removeAttribute("src");try{video.load();}catch(e){}
  if(GB_HLS){try{GB_HLS.destroy();}catch(e){}GB_HLS=null;}
  $("gbPlayerMask").style.display="none";
}
async function loadGuard(){
  try{
    const r=await jget("/v/api/ext/guard/status");const d=r.data||{};GUARD_SITES=d.sites||[];
    const ok=!!d.available;
    const p=$("androidPill");p.textContent="Android "+(ok?"就绪":"不可用");p.className="pill "+(ok?"on":"off");
    const op=$("ovAndroid");if(op){op.textContent="Android "+(ok?"就绪":"不可用");op.className="pill "+(ok?"on":"off");}
    $("guardMeta").textContent=(d.runner||"")+" · 内置 "+(d.builtin||0)+" · 导入 "+(d.imported||0)+" · 共 "+GUARD_SITES.length+" 源";
    renderGuardRows();
  }catch(e){
    const p=$("androidPill");p.textContent="Android 不可用";p.className="pill off";
    const op=$("ovAndroid");if(op){op.textContent="Android 不可用";op.className="pill off";}
    $("guardMeta").textContent="spider-runner 未连接";
  }
}
function renderGuardRows(){
  const q=($("guardFilter")?.value||"").trim();
  const rows=GUARD_SITES.filter(s=>!q||(s.name||"").includes(q)).map(s=>{
    const tag=s.builtin?'<span class="muted">内置</span>':'<span style="color:var(--ok)">导入</span>';
    const del=s.builtin?"":`<button class="small danger" onclick="removeGuard('${s.key}')">删</button>`;
    const cfg=s.need_config?' <span title="需要外部配置" style="color:var(--err)">⚠</span>':"";
    return `<tr><td>${esc(s.name)}${cfg}<div class="meta">${esc(s.key)}</div></td>
      <td>${s.searchable===false?"否":"是"}</td><td>${tag}</td>
      <td><input type="checkbox" ${s.enabled?"checked":""} onchange="toggleGuard('${s.key}',this.checked)"
        style="width:16px;height:16px;accent-color:var(--accent)"/></td><td>${del}</td></tr>`;
  }).join("");
  $("guardRows").innerHTML=rows||'<tr><td colspan="5" class="muted">无匹配加固源</td></tr>';
}
async function toggleGuard(key,on){
  try{await jpost("/v/api/ext/guard/toggle",{key,enabled:on});await loadGuard();}
  catch(e){toast("开关失败: "+e,"fail");await loadGuard();}
}
async function removeGuard(key){
  if(!confirm("删除该导入源？"))return;
  try{await jpost("/v/api/ext/guard/remove",{key});await loadGuard();}catch(e){toast("删除失败: "+e,"fail");}
}
async function loadGuardJar(){
  const $m=$("guardJarMsg");if(!$m)return;
  try{
    const r=await jget("/v/api/ext/guard/jar"),d=r.data||{};
    if(d.present){
      const kb=(d.size/1024).toFixed(0);
      $m.style.color=d.on_device?"var(--ok)":"";
      $m.textContent=`已自备 jar（${kb} KB）`+(d.on_device?" · 已下发到 Android 运行时 ✓":
        (d.container_running?" · 待下发":" · redroid 未启动，将在启动时自动下发"));
    }else{
      $m.style.color="var(--err)";
      $m.textContent="尚未提供加固 jar：请选择你自己有权使用的 .jar 上传，否则加固源无法播放（JVM/JS 标准源不受影响）。";
    }
  }catch(e){$m.style.color="var(--err)";$m.textContent="jar 状态获取失败: "+e;}
}
async function uploadGuardJar(){
  const fi=$("guardJarFile"),$m=$("guardJarMsg");
  const f=fi&&fi.files&&fi.files[0];
  if(!f){$m.style.color="var(--err)";$m.textContent="请先选择 .jar 文件";return;}
  if(f.size>60*1024*1024){$m.style.color="var(--err)";$m.textContent="jar 过大（上限 60MB）";return;}
  $m.style.color="";$m.textContent="读取并上传中（"+(f.size/1024).toFixed(0)+" KB）…";
  try{
    const buf=await f.arrayBuffer();
    let bin="",u8=new Uint8Array(buf),CH=0x8000;
    for(let i=0;i<u8.length;i+=CH)bin+=String.fromCharCode.apply(null,u8.subarray(i,i+CH));
    const data_b64=btoa(bin);
    const r=await jpost("/v/api/ext/guard/jar",{name:f.name,data_b64});
    if(r.code!==0){$m.style.color="var(--err)";$m.textContent="上传失败："+(r.msg||"");return;}
    $m.style.color="var(--ok)";$m.textContent="✓ "+(r.data.hint||("已保存（"+(r.data.size/1024).toFixed(0)+" KB）"));
    fi.value="";await loadGuardJar();
  }catch(e){$m.style.color="var(--err)";$m.textContent="上传出错: "+e;}
}
function depPill(ok,txt){return `<span class="pill ${ok?"on":"off"}">${txt}</span>`;}
async function loadSidecar(){
  try{
    const r=await jget("/v/api/ext/sidecar/status");const d=r.data||{};
    const up=!!d.redroid_container;
    const deps=[depPill(d.binderfs!==false,"binderfs"),depPill(d.docker,"Docker"),
      depPill(d.redroid_image,"redroid 镜像"),depPill(up,"redroid 容器"),
      depPill(d.runner_ok&&d.java,"JVM"),depPill(d.runner_ok&&d.node,"Node"),
      depPill(d.runner_ok&&d.android,"Android 加固")].join(" ");
    $("sidecarDeps").innerHTML=deps;
    const ov=$("ovRt");if(ov)ov.innerHTML=deps;
    const ready=d.runner_ok&&d.android;
    const el=$("sidecarState");
    el.textContent=ready?"✅ 加固运行时就绪，可导入并启用加固源":(d.installing?"⏳ 安装进行中…":"⚠ 尚未就绪，可点“一键安装”");
    el.style.color=ready?"var(--ok)":(d.installing?"var(--warn)":"var(--err)");
    if(d.installing){startSidecarPoll();}else{stopSidecarPoll();refreshSidecarLog();}
    renderEmu(d,up);
  }catch(e){const x=$("sidecarDeps");x.textContent="状态获取失败: "+e;}
}
function renderEmu(d,up){
  const pill=$("emuStatePill"),st=$("emuState");
  if(!pill)return;
  up=(up===undefined)?!!d.redroid_container:up;
  const ready=!!(d.runner_ok&&d.android);
  if(ready){pill.textContent="就绪";pill.className="pill on";}
  else if(up){pill.textContent="启动中";pill.className="pill warn";}
  else{pill.textContent="已停止（省内存）";pill.className="pill off";}
  let s=up?(ready?"redroid 运行中，加固源可用":"redroid 已启动，正在冷启并接通 bridge（约10–30秒）…")
          :"redroid 已手动停止（停止后不再自启，点“启动运行时”恢复常驻），JVM/JS 源仍正常。";
  st.textContent=s;
  $("emuStartBtn").disabled=up;$("emuStopBtn").disabled=!up;
}
let EMU_POLL=null;
async function androidStart(){
  try{toast("正在启动 Android 加固运行时…");
    const r=await jpost("/v/api/ext/sidecar/android/start",{});
    if(r.code!==0){toast(r.msg||"启动失败","fail");return;}
    await loadSidecar();
    if(EMU_POLL)clearInterval(EMU_POLL);
    EMU_POLL=setInterval(loadSidecar,3000);  // 冷启期持续刷新；redroid 约10–30秒
    setTimeout(()=>{if(EMU_POLL){clearInterval(EMU_POLL);EMU_POLL=null;}},240000);
  }catch(e){toast("启动失败: "+e,"fail");}
}
async function androidStop(){
  if(!confirm("停止 redroid 并立即释放约 0.7–0.9GB 内存？加固源将暂时不可用，JVM/JS 源不受影响。"))return;
  try{const r=await jpost("/v/api/ext/sidecar/android/stop",{});
    if(EMU_POLL){clearInterval(EMU_POLL);EMU_POLL=null;}
    await loadSidecar();
    toast((r.data&&r.data.ok)?"已停止，内存已释放":"停止失败","fail");
  }catch(e){toast("停止失败: "+e,"fail");}
}
async function refreshSidecarLog(){
  try{const r=await jget("/v/api/ext/sidecar/log");const d=r.data||{};
    const el=$("sidecarLog");el.textContent=d.log||"";el.scrollTop=el.scrollHeight;return d.running;
  }catch(e){return false;}
}
function startSidecarPoll(){if(SC_TIMER)return;
  SC_TIMER=setInterval(async()=>{const running=await refreshSidecarLog();
    if(!running){stopSidecarPoll();await loadSidecar();await loadGuard();}},2500);}
function stopSidecarPoll(){if(SC_TIMER){clearInterval(SC_TIMER);SC_TIMER=null;}}
async function sidecarInstall(what){
  const tip=what==="all"?"将安装 JVM+Node runner 与 Android 模拟器（首次需下载镜像，耗时较长）。继续？":
    what==="android-only"?"将安装/修复 Android KVM 模拟器。继续？":"将安装/修复 JVM+Node runner。继续？";
  if(!confirm(tip))return;
  try{await jpost("/v/api/ext/sidecar/install",{what});startSidecarPoll();await refreshSidecarLog();await loadSidecar();}
  catch(e){toast("启动安装失败: "+e,"fail");}
}
async function sidecarReconnect(){
  try{const r=await jpost("/v/api/ext/sidecar/reconnect",{});
    toast(r.data&&r.data.ok?"网络已重接":("重接失败: "+((r.data&&r.data.error)||r.data&&r.data.out||"")),r.data&&r.data.ok?"ok":"fail");
    await loadSidecar();
  }catch(e){toast("重接失败: "+e,"fail");}
}
// <<< PRIVATE:GUARD

// >>> PRIVATE:CATVOD
/* ---------- 小雅 / JS（CATVOD） ---------- */
let XY_PATH=[];
function setPill(id,ok,txt){const e=$(id);e.textContent=txt;e.className="pill "+(ok?"on":"off");}
async function loadPriv(){
  try{const c=await jget("/v/api/ext/catvod/credentials");const d=c.data||{};
    $("xyServer").value=d.server||"";$("xyUser").value=d.username||"";
    $("xyMsg").textContent=(d.has_password?"已保存密码（留空不修改）":"未设置密码")+" · 分类数 "+(d.drives||0);
    setPill("jvmPill",!!d.available,"JVM "+(d.available?"就绪":"不可用"));
  }catch(e){setPill("jvmPill",false,"JVM 不可用");}
  try{const cfg=(await jget("/v/api/ext/status")).data.config||{};
    $("panLibEnabled").checked=cfg.pan_lib_enabled!==false;
  }catch(e){}
  try{const h=await jget("/v/api/ext/drpy/status");
    setPill("nodePill",!!(h.data&&h.data.available),"Node(JS) "+(h.data&&h.data.available?"就绪":"不可用"));
  }catch(e){setPill("nodePill",false,"Node(JS) 不可用");}
}
async function saveXiaoya(){
  const body={server:$("xyServer").value.trim(),username:$("xyUser").value.trim()};
  const pw=$("xyPass").value;if(pw)body.password=pw;
  const r=await jpost("/v/api/ext/catvod/credentials",body);
  if(r.code===0){toast("小雅配置已保存");$("xyPass").value="";$("xyMsg").textContent="已生效，分类数 "+(r.data.drives||0);browseXiaoya();}
  else toast(r.msg||"保存失败","fail");
}
async function savePanLib(){
  const r=await jpost("/v/api/ext/config",{pan_lib_enabled:$("panLibEnabled").checked});
  if(r.code===0){toast("小雅媒体库设置已保存");$("panLibMsg").textContent=$("panLibEnabled").checked?"已启用：进入媒体库时后台增量索引并补全海报":"已关闭";}
  else toast(r.msg||"保存失败","fail");
}
async function browseXiaoya(tid){
  const rows=$("xyRows");rows.innerHTML='<tr><td colspan="2" class="muted">加载中…</td></tr>';
  try{let url,cls,list;
    if(tid){url="/v/api/ext/catvod/category?tid="+encodeURIComponent(tid)+"&pg=1";XY_PATH.push(tid);}
    else{url="/v/api/ext/catvod/home";XY_PATH=[];}
    const j=await jget(url);
    if(tid){list=(j.data&&j.data.list)||[];}else{cls=(j.data&&j.data.classes)||[];}
    $("xyCrumb").textContent=tid?("分类 "+tid):"全部";
    if(!tid){rows.innerHTML=cls.map(c=>`<tr><td>${esc(c.type_name||"")}</td>
        <td><button class="small" onclick="browseXiaoya('${c.type_id}')">打开</button></td></tr>`).join("")
        ||'<tr><td colspan="2" class="muted">无分类（检查服务器/账号密码）</td></tr>';
    }else{rows.innerHTML=list.slice(0,40).map(v=>`<tr><td>${esc(v.vod_name||"")}
        <span class="muted">${esc(v.vod_remarks||"")}</span></td><td class="muted">可搜索/播放</td></tr>`).join("")
        ||'<tr><td colspan="2" class="muted">该分类暂无条目</td></tr>';}
  }catch(e){rows.innerHTML='<tr><td colspan="2" class="muted">连接失败：'+esc(e)+'</td></tr>';}
}
function xyCrumbUp(){browseXiaoya();}
// <<< PRIVATE:CATVOD

/* ---------- 初始化 ---------- */
async function loadStatus(){
  STATUS=await jget("/v/api/ext/status");
  CFG=STATUS.config;
  PRIO=STATUS.source_priority||{};
}
async function init(){
  // 首个请求若在插件重启窗口内发出可能一直挂起（旧版无超时、无重试，
  // 页面会永远停在“检测中…”）：15 秒超时，失败重试 3 次，仍失败才报未接管。
  let lastErr=null;
  for(let i=0;i<3;i++){
    try{await loadStatus();lastErr=null;break;}
    catch(e){lastErr=e;const gs=$("globalState");
      gs.textContent="检测中…（重试 "+(i+1)+"/3）";
      await new Promise(r=>setTimeout(r,1000));}
  }
  if(lastErr)throw lastErr;
  KWS=Array.isArray(CFG.junk_keywords)?CFG.junk_keywords.slice():[];
  $("maxn").value=CFG.max_per_keyword;
  $("junk").checked=CFG.filter_junk;
  $("onlineFirst").checked=CFG.online_first!==false;
  $("lineFastest").checked=CFG.line_fastest!==false;
  $("lineAutofail").checked=CFG.line_autofail!==false;
  $("fixedLineDirect").checked=CFG.fixed_line_direct!==false;
  $("fastestMax").value=CFG.line_fastest_max||6;
  $("fastestTimeout").value=CFG.line_fastest_timeout||4;
  $("linePredict").checked=CFG.linepredict_enabled!==false;
  $("linePredictMax").value=CFG.linepredict_max_lines||3;
  $("linePredictTtl").value=CFG.linepredict_ttl_min||30;
  renderKw();renderSources();initSrcProxy();loadCacheStatus();
  const gs=$("globalState");gs.textContent="接管运行中 · 已缓存 "+STATUS.cached_titles;gs.className="dot ok";
  const ed=$("aboutEdition");if(ed)ed.textContent=(FEAT_DDUO?"完整版（本地全功能）":"公开版")+" · 在飞牛原生搜索与媒体库中注入在线影视";
  loadOnline();
  // >>> PRIVATE:GUARD
  if($("guardCard")||document.getElementById("page-tvbox")){loadGuard();loadSidecar();loadGuardJar();}
  // 网盘（夸克）能力仅全功能构建提供：探测到才显示「网盘」标签。
  fetch("/v/api/ext/pan/status").then(r=>r.json()).then(j=>{
    if(j&&j.data&&j.data.supported){
      $$("[data-pan-nav]").forEach(b=>b.style.display="");
    }
  }).catch(()=>{});
  // <<< PRIVATE:GUARD
  // >>> PRIVATE:CATVOD
  if(document.getElementById("page-xiaoya"))loadPriv();
  // <<< PRIVATE:CATVOD
  renderOverview();
  loadWhoami();
  startLivePoll();
}
$("liveClear").addEventListener("click",async e=>{e.preventDefault();
  try{await fetch("/v/api/ext/live/clear",{method:"POST"});await loadLive();toast("已清空实时记录");}catch(_){}});
$("liveCopy").addEventListener("click",e=>{e.preventDefault();copyDiagnosis();});
$("kwAdd").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();addKw();}});
$("kw").addEventListener("input",()=>{clearTimeout(window._t);window._t=setTimeout(loadOnline,250);});
init().catch(e=>{const gs=$("globalState");gs.textContent="未接管/接口不可用";gs.className="dot bad";console.error(e);});
</script>
</body>
</html>
"""
