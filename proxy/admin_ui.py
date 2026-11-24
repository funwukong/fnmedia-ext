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
<script>
/* Apply saved appearance before the page is painted. */
const adminTheme = (() => {
  const key = 'fnmedia-ext.admin.theme.v1';
  const presets = {
    indigo: '#4f46e5', blue: '#2563eb', teal: '#0f766e',
    rose: '#be185d', amber: '#b45309', slate: '#475569',
    appleSky: '#007aff', appleMidnight: '#5ac8fa', appleSunset: '#ff6b35',
    appleForest: '#34c759', appleLavender: '#af52de',
    harmonyAurora: '#27c7b8', harmonySky: '#009db5', harmonyDream: '#8158d8'
  };
  const defaults = {mode: 'system', palette: 'indigo', custom: '#4f46e5', family: 'classic'};
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  const root = document.documentElement;
  const validHex = value => typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value);
  const normalize = value => {
    const {mode, palette, custom, family} = value || {};
    return {
      mode: ['system', 'light', 'dark'].includes(mode) ? mode : defaults.mode,
      palette: typeof palette === 'string' && (palette === 'custom' ||
        Object.prototype.hasOwnProperty.call(presets, palette)) ? palette : defaults.palette,
      custom: validHex(custom) ? custom.toLowerCase() : defaults.custom,
      family: ['classic', 'apple', 'harmony'].includes(family) ? family : defaults.family
    };
  };
  const decode = text => {
    try { return normalize(JSON.parse(text)); }
    catch { return {...defaults}; }
  };
  let state = {...defaults};
  try { state = decode(window.localStorage.getItem(key)); } catch {}

  const rgb = hex => hex.slice(1).match(/../g).map(channel => parseInt(channel, 16));
  const mix = (from, to, weight) => from.map((channel, i) => Math.round(channel + (to[i] - channel) * weight));
  const luminance = color => color.map(channel => {
    const v = channel / 255;
    return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
  }).reduce((total, channel, i) => total + channel * [.2126, .7152, .0722][i], 0);
  const contrast = (a, b) => {
    const [x, y] = [luminance(a), luminance(b)];
    return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
  };

  function apply() {
    const dark = state.mode === 'dark' || (state.mode === 'system' && system.matches);
    const accent = state.palette === 'custom' ? state.custom : presets[state.palette];
    const base = rgb(accent);
    const white = [255, 255, 255], black = [0, 0, 0];
    const surface = rgb(dark ? '#171c28' : '#ffffff');
    const canvas = rgb(dark ? '#10131c' : '#f4f6fb');
    const inset = rgb(dark ? '#1e2433' : '#f7f8fc');
    const soft = mix(surface, base, dark ? .20 : .09);
    // Keep the user’s chosen fill and derive a legible link, border and focus color.
    let ink = base;
    for (let step = 1; [surface, canvas, inset, soft].some(bg => contrast(ink, bg) < 4.5); step++) {
      ink = mix(base, dark ? white : black, Math.min(step / 20, 1));
    }
    const style = root.style;
    root.dataset.theme = dark ? 'dark' : 'light';
    root.dataset.themeFamily = state.family;
    style.setProperty('--accent-fill', accent);
    style.setProperty('--accent-on', contrast(base, white) >= contrast(base, black) ? '#fff' : '#000');
    style.setProperty('--accent', `rgb(${ink.join(',')})`);
    style.setProperty('--accent-soft', `rgb(${soft.join(',')})`);
  }

  let onChange = () => {};
  function commit(next, reset = false) {
    state = normalize(next);
    apply();
    let saved = true;
    try {
      if (reset) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, JSON.stringify(state));
    } catch { saved = false; }
    onChange(saved);
  }
  const onSystemChange = () => { if (state.mode === 'system') apply(); };
  if (system.addEventListener) system.addEventListener('change', onSystemChange);
  else if (system.addListener) system.addListener(onSystemChange);
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    // Re-read current storage so a queued event cannot overwrite a newer local choice.
    try { state = decode(window.localStorage.getItem(key)); } catch { return; }
    apply();
    onChange(true);
  });
  apply();
  return {
    presets: {...presets}, validHex,
    get: () => ({...state}),
    set: changes => commit({...state, ...changes}),
    reset: () => commit(defaults, true),
    subscribe: listener => { onChange = listener; }
  };
})();
</script>
<style>
/* Dashboard shell */
:root{
  color-scheme:light;
  --bg:#f4f6fb;--card:#fff;--inset:#f7f8fc;--line:#dde3ee;--control-line:#7d889d;
  --text:#172033;--muted:#526077;
  --accent:#4f46e5;--accent-fill:#4f46e5;--accent-on:#fff;--accent-soft:#efeeff;
  --ok:#16643f;--ok-soft:#e8f5ee;--warn:#90440b;--warn-soft:#fff1df;
  --err:#a62a3a;--err-soft:#fbecef;--info:#225ab2;--violet:#6940b4;
  --radius:16px;--shadow:0 1px 2px #14244208,0 4px 14px #14244205;
  --overlay:#13192980;
}
:root[data-theme=dark]{
  color-scheme:dark;
  --bg:#10131c;--card:#171c28;--inset:#1e2433;--line:#343e51;--control-line:#7785a0;
  --text:#edf1f9;--muted:#aab6cb;
  --ok:#8bdbad;--ok-soft:#1c372e;--warn:#fac984;--warn-soft:#3d2d21;
  --err:#ffacb3;--err-soft:#412731;--info:#a5c5ff;--violet:#c8b3ff;
  --shadow:0 2px 5px #0003;--overlay:#04060ab8;
}
/* Optional Apple/HarmonyOS-inspired glass treatments use the current mode palette. */
:root[data-theme-family=apple]{--radius:19px;--shadow:0 14px 38px #1d3e6818, inset 0 1px 0 #ffffff76}
:root[data-theme-family=harmony]{--radius:22px;--shadow:0 18px 44px #0b4b5c20, inset 0 1px 0 #ffffff80}
:root[data-theme-family=apple] .card,:root[data-theme-family=harmony] .card,
:root[data-theme-family=apple] .sidebar,:root[data-theme-family=harmony] .sidebar{
  backdrop-filter:blur(18px) saturate(135%);-webkit-backdrop-filter:blur(18px) saturate(135%)}
:root[data-theme-family=apple] .page-head,:root[data-theme-family=harmony] .page-head{
  background:linear-gradient(118deg,var(--accent-soft),var(--card) 68%)}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font:15px/1.6 system-ui,-apple-system,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  background:var(--bg);color:var(--text);overflow-wrap:anywhere}
a{color:var(--accent);text-underline-offset:.18em}
button,input,select,textarea{font:inherit}
button,summary{cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.layout{display:grid;grid-template-columns:244px minmax(0,1fr);min-height:100vh;min-height:100dvh}
.sidebar{padding:24px 16px;background:var(--card);border-right:1px solid var(--line);
  position:sticky;top:0;height:100vh;height:100dvh;overflow:auto;z-index:46;
  display:flex;flex-direction:column;gap:22px}
.brand{display:flex;align-items:center;gap:11px;padding:0 5px;min-width:0}
.brand-mark{width:40px;height:40px;border-radius:12px;flex:none;display:grid;place-items:center;
  background:var(--accent-fill);color:var(--accent-on)}
.brand-mark::before{content:"";margin-left:2px;border-block:6px solid transparent;border-left:10px solid currentColor}
.brand-name{font-weight:750;letter-spacing:-.025em;line-height:1.25}
.brand-sub{font-size:12px;color:var(--muted)}
.nav{display:grid;gap:4px}
.nav-item,.theme-trigger,.menu-trigger{display:flex;align-items:center;gap:10px;text-align:left;
  border:1px solid transparent;border-radius:9px;background:transparent;color:var(--muted);font-weight:570}
.nav-item{min-height:42px;padding:7px 11px}
.nav-item:hover,.theme-trigger:hover,.menu-trigger:hover{background:var(--inset);color:var(--text)}
.nav-item.active{background:var(--accent-soft);color:var(--accent);font-weight:700}
.nav-icon{display:inline-grid;place-items:center;font-size:17px;line-height:1;width:20px;flex:none}
.sidebar-tools{display:grid;gap:12px;margin-top:auto}
.theme-trigger,.menu-trigger{min-height:44px;padding:7px 10px;border-color:var(--line);color:var(--text)}
.theme-trigger::before{content:"";width:16px;height:16px;flex:none;border-radius:50%;
  border:1px solid var(--accent);background:linear-gradient(135deg,var(--accent-fill) 50%,var(--card) 50%)}
.menu-trigger{display:none}
.sidebar-foot{border-top:1px solid var(--line);padding:10px 4px 0;color:var(--muted)}
.sidebar-foot .dot{font-size:12px}
.main{min-width:0;width:100%;max-width:1224px;margin-inline:auto;padding:32px clamp(20px,3.2vw,48px) 48px}
.page{display:none}.page.active{display:block}
.page-head{padding:21px clamp(16px,2.5vw,28px);margin-bottom:18px;border:1px solid var(--line);
  border-radius:var(--radius);background:linear-gradient(112deg,var(--accent-soft),var(--card) 68%);box-shadow:var(--shadow)}
h2.page-title{margin:0;font-size:clamp(24px,3vw,34px);line-height:1.25;letter-spacing:-.025em}
.page-desc{color:var(--muted);font-size:14px;margin:7px 0 0}
.card{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:clamp(14px,2.2vw,22px);margin-bottom:14px;box-shadow:var(--shadow)}
.card-title{font-weight:700;font-size:16px;margin-bottom:5px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.card-sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.hint{color:var(--muted);font-size:13px;margin:6px 0}
.muted{color:var(--muted)}.sm{font-size:12px}
.stack{display:flex;flex-direction:column;gap:8px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.mt{margin-top:10px}
.skip-link{position:fixed;top:8px;left:8px;z-index:91;transform:translateY(-200%);
  background:var(--card);padding:9px 14px;border-radius:8px}
.skip-link:focus{transform:none}

/* ---------- 控件 ---------- */
input[type=text],input[type=number],input[type=password],textarea,select{
  background:var(--card);border:1px solid var(--control-line);border-radius:8px;color:var(--text);
  padding:8px 10px;width:100%;max-width:100%;font:inherit}
input[type=number]{width:auto}
textarea{resize:vertical;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
input:is(:focus),textarea:is(:focus),select:is(:focus){border-color:var(--accent)}
label.field{display:block;margin:10px 0}
label.field>span{display:block;color:var(--muted);font-size:12px;margin-bottom:4px}
.switch-row{display:flex;align-items:flex-start;gap:10px;padding:10px 0;cursor:pointer}
.switch-row input{width:17px;height:17px;margin-top:2px;accent-color:var(--accent);flex:none}
.switch-row .sub{display:block;font-size:13px;color:var(--muted)}
:is(.btn,button.small,.seg button){display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);
  background:var(--card);color:var(--text);border-radius:8px;padding:8px 14px;cursor:pointer;font-size:14px}
:is(.btn,button.small,.seg button):hover{border-color:var(--accent);color:var(--accent)}
:is(.btn,button.small).primary{background:var(--accent-fill);border-color:var(--accent);color:var(--accent-on)}
:is(.btn,button.small).primary:hover:not(:disabled){box-shadow:inset 0 0 0 2px var(--accent-on)}
:is(.btn.small,button.small){padding:5px 12px;font-size:13px;min-height:0}
:is(.btn,button.small):disabled{opacity:.5;cursor:default}
:is(.btn,button.small).danger{color:var(--err)}:is(.btn,button.small).danger:hover{background:var(--err-soft);border-color:var(--err)}
.btn-row{display:flex;gap:10px;align-items:center;margin:10px 0;flex-wrap:wrap}

/* ---------- 徽标/状态点 ---------- */
.badge{font-size:11px;padding:1px 8px;border-radius:99px;background:var(--accent-soft);color:var(--accent);font-weight:500}
.dot{display:inline-flex;align-items:center;gap:6px;font-size:13px}
.dot::before{content:"";width:9px;height:9px;border-radius:50%;background:var(--muted)}
.dot.ok::before{background:var(--ok)}.dot.bad::before{background:var(--err)}.dot.wait::before{background:var(--warn)}
.pill{font-size:11px;padding:2px 9px;border-radius:99px;white-space:nowrap;border:1px solid transparent}
.pill.on{color:var(--ok);background:var(--ok-soft);border-color:var(--line)}
.pill.off{color:var(--muted);background:var(--inset);border-color:var(--line)}
.pill.warn{color:var(--warn);background:var(--warn-soft);border-color:var(--line)}
.chip{display:inline-flex;align-items:center;gap:5px;background:var(--inset);border:1px solid var(--line);
  color:var(--text);border-radius:99px;padding:2px 6px 2px 11px;font-size:12px;margin:2px}
.chip b{cursor:pointer;font-weight:700;color:var(--muted);padding:0 3px;border-radius:99px}
.chip b:hover{color:var(--err)}
.tag{display:inline-block;font-size:10px;color:var(--accent);background:var(--accent-soft);
  border-radius:5px;padding:0 5px;margin-left:6px}
.tag.amber{color:var(--warn);background:var(--warn-soft)}
.tag.warn{color:var(--warn);background:var(--warn-soft)}
/* 统一订阅导入结果 */
.subgrp{border:1px solid var(--line);border-radius:9px;padding:8px 10px;margin:10px 0;background:var(--inset)}
.subgrp-t{font-size:13px;font-weight:600;margin-bottom:6px}
.subrow{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:13px;padding:3px 0}
.subrow.ok span:first-child{color:var(--ok,#1a8d3a)}
.subrow.muted{color:var(--muted)}

/* ---------- 实时播放诊断 ---------- */
.live-dev{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0;background:var(--inset)}
.live-rows{margin-top:6px;display:flex;flex-direction:column;gap:3px}
.live-row{display:flex;align-items:center;gap:6px;font-size:12px;flex-wrap:wrap}
.live-row .muted{font-size:11px}
.live-dev details{margin-top:6px}
.diag{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:13px}

/* ---------- 资源站列表：不分页，自适应列宽 + 可滚动 ---------- */
.src-wrap{max-height:min(62vh,620px);overflow:auto;border:1px solid var(--line);
  border-radius:10px}
.src-wrap.expanded{max-height:none;overflow:auto}
.src-wrap table{width:100%;min-width:580px;table-layout:fixed}
.src-wrap td,.src-wrap th{font-size:13px;padding:7px 9px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.src-wrap td.src-name{white-space:normal;line-height:1.45}
.src-wrap td.src-name .meta{overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;max-width:100%}
.src-wrap.compact td,.src-wrap.compact th{font-size:12px;padding:5px 7px}

/* ---------- 已缓存条目：容器内滚动 + 自适应缩放 ---------- */
.cache-wrap{max-height:340px;overflow:auto;border:1px solid var(--line);
  border-radius:10px;background:var(--card)}
.cache-wrap table{width:100%;table-layout:fixed}
.cache-wrap td,.cache-wrap th{font-size:13px;padding:7px 9px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cache-wrap td:first-child{white-space:normal;line-height:1.45}
.cache-wrap.compact{font-size:12px}
.cache-wrap.compact td,.cache-wrap.compact th{font-size:12px;padding:5px 7px}
.cache-more{margin-top:8px;display:flex;justify-content:center}
.cache-more button{font-size:13px}

/* ---------- 表格 ---------- */
.table-scroll{overflow:auto}
.table-scroll table{min-width:420px}
#page-sources .table-scroll table{min-width:540px}
table{width:100%;border-collapse:collapse;font-size:13px}
thead{background:var(--inset)}
tbody tr:hover{background:var(--inset)}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--muted);font-weight:500;font-size:12px}
tr:last-child td{border-bottom:0}
.meta{color:var(--muted);font-size:12px;word-break:break-all}
.del{color:var(--err);background:transparent;border:0;cursor:pointer;font-size:13px;padding:4px 6px}
.st{font-size:12px;white-space:nowrap}.st.ok{color:var(--ok)}.st.bad{color:var(--err)}.st.wait{color:var(--muted)}

/* ---------- 管理页开关/缓存卡片（移植上游 v0.2.54/69：导入按钮、缓存网格、代理模式段） ---------- */
label.btn,label.btn-sm{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  border:1px solid var(--accent);background:var(--card);color:var(--accent);border-radius:9px;
  padding:8px 15px;font-size:13.5px;cursor:pointer;white-space:nowrap;font-family:inherit}
label.btn-sm{padding:5px 12px;font-size:12.5px;border-radius:7px}
label.btn:focus-within,label.btn-sm:focus-within{outline:2px solid var(--accent);outline-offset:3px}
label.btn,label.btn-sm{position:relative}
.file-input{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer}
label.btn:hover,label.btn-sm:hover{background:var(--accent-soft)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;padding:2px;gap:2px;background:var(--inset);flex-wrap:wrap}
.seg button{border-color:transparent;background:transparent;padding:7px 14px;font-size:13px;color:var(--muted)}
.seg button.active{background:var(--accent-soft);color:var(--accent);font-weight:700}
.seg button:focus-visible{outline-offset:-3px}

/* ---------- 概览统计 ---------- */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,128px),1fr));gap:10px}
.stat{background:var(--inset);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat .n{font-size:clamp(23px,3vw,30px);font-weight:750;letter-spacing:-.035em;font-variant-numeric:tabular-nums}
.stat .l{font-size:12px;color:var(--muted)}
.lib-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr));gap:10px}
.lib{display:flex;align-items:center;gap:10px;background:var(--inset);border:1px solid var(--line);
  border-radius:10px;padding:10px 12px}
.lib .ic{font-size:20px}
.lib .t{font-weight:600;font-size:14px}
.lib .c{font-size:12px;color:var(--muted)}
.lib .pin{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--muted);
  cursor:pointer;white-space:nowrap;padding:2px 8px;border-radius:99px;border:1px solid var(--line);background:var(--card)}
.lib .pin input{margin:0;accent-color:var(--accent)}
.lib:has(.pin input:checked){border-color:var(--accent);background:var(--accent-soft)}

/* ---------- 优先级 ---------- */
.prio-input{width:74px}
.prio-bar{height:5px;border-radius:3px;background:linear-gradient(90deg,var(--accent),var(--accent-fill));margin-top:3px}

/* ---------- 弹窗/日志/toast ---------- */
pre.log{max-height:240px;overflow:auto;background:#0f1115;color:#d6dae0;border-radius:8px;
  padding:10px;font-size:11px;line-height:1.5;margin:6px 0 0;white-space:pre-wrap}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(8px);
  background:#222;color:#fff;padding:9px 18px;border-radius:99px;font-size:13px;opacity:0;
  transition:.22s;pointer-events:none;z-index:80;max-width:90vw}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.ok{background:var(--ok-soft);color:var(--ok);border:1px solid var(--ok)}
#toast.fail{background:var(--err-soft);color:var(--err);border:1px solid var(--err)}

/* Appearance dialog */
.theme-dialog{width:min(480px,calc(100% - 24px));max-height:calc(100dvh - 24px);overflow:auto;
  padding:clamp(14px,4vw,24px);border:1px solid var(--line);border-radius:var(--radius);
  background:var(--card);color:var(--text);box-shadow:0 18px 60px #0004}
.theme-dialog::backdrop{background:var(--overlay)}
.theme-head,.theme-actions{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.theme-head h2{margin:0;font-size:22px}
.theme-dialog fieldset{min-width:0;border:0;padding:0;margin:16px 0}
.theme-dialog legend{font-weight:700;margin-bottom:6px;padding:0}
.theme-options{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}
.theme-options label{display:flex;min-height:44px;align-items:center;justify-content:center;gap:6px;
  padding:5px;border:1px solid var(--line);border-radius:9px;cursor:pointer;font-size:13px}
.theme-options input{margin:0;accent-color:var(--accent)}
.theme-options label:has(input:checked){border-color:var(--accent);background:var(--accent-soft);color:var(--accent)}
.theme-swatches{display:grid;grid-template-columns:repeat(auto-fit,minmax(50px,1fr));gap:6px;margin-bottom:12px}
.theme-swatch{display:grid;justify-items:center;gap:4px;padding:6px 2px;min-height:48px;
  border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--text);font-size:12px}
.theme-swatch::before{content:"";width:18px;height:18px;border-radius:50%;background:var(--swatch);border:1px solid var(--control-line)}
.theme-swatch[aria-pressed=true]{border-color:var(--accent);background:var(--accent-soft);font-weight:750}
.theme-custom{display:flex;flex-wrap:wrap;align-items:end;gap:8px}
.theme-custom label{flex:1 1 100px;min-width:0;font-size:13px}
.theme-custom input{display:block;min-height:42px;margin-top:4px}
.theme-custom input[type=color]{width:56px;padding:4px;border:1px solid var(--control-line);
  border-radius:8px;background:var(--card);cursor:pointer}
.theme-custom input[aria-invalid=true]{border-color:var(--err)}
.theme-status{min-height:1.6em;margin:8px 0 0;color:var(--muted);font-size:13px}
.theme-error{color:var(--err)}

.source-add{display:grid;grid-template-columns:minmax(0,150px) minmax(0,1fr) auto auto;gap:10px;align-items:center}
.bottom-nav{display:none}
@media(max-width:760px){
  .source-add{grid-template-columns:repeat(2,minmax(0,1fr))}
  .source-add input[type=text]{grid-column:1/-1}
  .layout{display:block}
  .sidebar{height:auto;overflow:visible;flex-direction:row;align-items:center;justify-content:space-between;
    gap:6px;padding: max(8px,env(safe-area-inset-top)) max(10px,env(safe-area-inset-right)) 8px max(10px,env(safe-area-inset-left));
    border-right:0;border-bottom:1px solid var(--line)}
  .brand{gap:7px;padding:0}.brand-mark{width:32px;height:32px;border-radius:9px}.brand-name{font-size:14px}
  .sidebar-tools{display:flex;flex:none;gap:5px;margin:0}
  .theme-trigger,.menu-trigger{display:flex;padding:5px 7px;gap:5px;font-size:13px}
  .sidebar-foot{display:none}
  .nav{display:none;position:absolute;top:100%;inset-inline:8px;padding:8px;max-height:calc(100dvh - 132px);
    overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
  .nav.open{display:grid}
  .main{padding:14px max(12px,env(safe-area-inset-right)) calc(76px + env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-left))}
  .page-head{padding:16px;margin-bottom:12px}
  input[type=text],input[type=number],input[type=password],textarea,select{font-size:16px}
  button:not(.nav-item),label.btn,label.btn-sm{min-height:42px}
  .bottom-nav{display:flex;position:fixed;inset:auto 0 0;z-index:45;
    background:var(--card);border-top:1px solid var(--line);
    padding:3px max(4px,env(safe-area-inset-right)) calc(3px + env(safe-area-inset-bottom)) max(4px,env(safe-area-inset-left))}
  .bottom-nav .nav-item{flex:1;min-width:0;flex-direction:column;justify-content:center;min-height:48px;
    text-align:center;gap:3px;padding:3px 0;font-size:11px;white-space:nowrap}
  .bottom-nav .nav-icon{font-size:15px}
  #toast{bottom:calc(65px + env(safe-area-inset-bottom))}
}
@media(prefers-reduced-motion:reduce){#toast{transition:none}}
</style>
</head>
<body>
<a class="skip-link" href="#mainContent">跳到主内容</a>
<div class="layout">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true"></div>
      <div><div class="brand-name">fnmedia-ext</div><div class="brand-sub">管理台</div></div>
    </div>
    <nav class="nav" id="nav" aria-label="管理台页面">
      <button type="button" class="nav-item active" data-page="overview" aria-controls="page-overview" aria-current="page"><span class="nav-icon" aria-hidden="true">▦</span>概览</button>
      <button type="button" class="nav-item" data-page="library" aria-controls="page-library"><span class="nav-icon" aria-hidden="true">▤</span>虚拟媒体库</button>
      <button type="button" class="nav-item" data-page="quality" aria-controls="page-quality"><span class="nav-icon" aria-hidden="true">≋</span>片源质量</button>
      <button type="button" class="nav-item" data-page="sources" aria-controls="page-sources"><span class="nav-icon" aria-hidden="true">◎</span>资源站</button>
      <button type="button" class="nav-item" data-page="importsub" aria-controls="page-importsub"><span class="nav-icon" aria-hidden="true">⇩</span>导入订阅</button>
      <!-- >>> PRIVATE:SCANLOGIN -->
      <button type="button" class="nav-item" data-page="scanlogin" aria-controls="page-scanlogin"><span class="nav-icon" aria-hidden="true">◈</span>网盘登录</button>
      <!-- <<< PRIVATE:SCANLOGIN -->
      <!-- >>> PRIVATE:CATVOD -->
      <button type="button" class="nav-item" data-page="xiaoya" aria-controls="page-xiaoya"><span class="nav-icon" aria-hidden="true">⌘</span>小雅 / JS</button>
      <!-- <<< PRIVATE:CATVOD -->
      <button type="button" class="nav-item" data-page="search" aria-controls="page-search"><span class="nav-icon" aria-hidden="true">⌕</span>搜索</button>
      <button type="button" class="nav-item" data-page="about" aria-controls="page-about"><span class="nav-icon" aria-hidden="true">ⓘ</span>关于</button>
    </nav>
    <div class="sidebar-tools">
      <button type="button" class="theme-trigger" id="themeTrigger" aria-haspopup="dialog" aria-controls="themeDialog">外观</button>
      <button type="button" class="menu-trigger" id="menuTrigger" aria-controls="nav" aria-expanded="false">菜单</button>
      <div class="sidebar-foot"><span id="globalState" class="dot wait">检测中…</span></div>
    </div>
  </aside>

  <main class="main" id="mainContent" tabindex="-1">

    <!-- ============ 概览 ============ -->
    <section class="page active" id="page-overview" aria-labelledby="title-overview">
      <header class="page-head">
  <h2 class="page-title" id="title-overview" tabindex="-1">概览</h2>
        <p class="page-desc">在飞牛原生搜索与媒体库中注入在线影视，点击直接调用飞牛原生播放器。</p>
      </header>

      <div class="card">
        <div class="card-title">fnmedia-ext 服务</div>
        <label class="switch-row"><input type="checkbox" id="extensionEnabled" onchange="saveExtensionEnabled()"/>
          <span>启用 fnmedia-ext 服务<span class="sub">关闭后影视请求由飞牛官方服务处理，管理页仍可用；重新开启会重载资源站路由与热门榜单。</span></span></label>
        <div class="hint" id="extensionEnabledMsg"></div>
      </div>

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
          <span><b style="color:var(--info)">mixed</b> 合并官方</span>
          <span><b style="color:var(--warn)">upstream</b> 透传官方</span>
          <span><b style="color:var(--err)">4xx/5xx</b> 出错</span>
        </div>
        <div id="liveDevices"><div class="muted sm">暂无播放活动（在电视或手机上播一部片即可看到）</div></div>
      </div>

      <div class="card">
        <div class="card-title">运行诊断</div>
        <div class="hint">仅展示脱敏统计，不记录搜索词、影片标题或播放地址。</div>
        <div id="runtimeDiagnostics"><div class="muted sm">读取中…</div></div>
        <button class="small" style="margin-top:8px" onclick="loadRuntimeDiagnostics()">刷新诊断</button>
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

    </section>

    <!-- ============ 虚拟媒体库 ============ -->
    <section class="page" id="page-library" aria-labelledby="title-library">
      <header class="page-head">
  <h2 class="page-title" id="title-library" tabindex="-1">虚拟媒体库</h2>
        <p class="page-desc">插件在飞牛影视媒体库中虚拟出的内容。热门榜单与海量片库各自独立开关；每张库卡可单独「置顶」「隐藏」，虚拟库的显示顺序与相对本地库的位置可调；子账号能看哪些库在下方「子账号可见权限」里按人勾选。</p>
      </header>

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
        <p class="card-sub">独立源库 / 小雅 / 红果短剧。每张卡可单独置顶或隐藏（隐藏后所有账号都看不到）。</p>
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

      <div class="card" id="welfareCard" style="display:none">
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

    <section class="page" id="page-quality" aria-labelledby="title-quality">
      <header class="page-head">
  <h2 class="page-title" id="title-quality" tabindex="-1">片源质量优先级</h2>
        <p class="page-desc">热门资源点击搜源时，按「源质量权重 → 可播直链数」排序，优先选择高质量源。</p>
      </header>
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
        <div class="table-scroll" tabindex="0" role="region" aria-label="片源质量优先级列表">
        <table>
          <thead><tr><th>资源站</th><th style="width:120px">质量权重</th><th>相对优先级</th></tr></thead>
          <tbody id="prioRows"><tr><td colspan="3" class="muted">加载中…</td></tr></tbody>
        </table>
        </div>
        <div class="btn-row"><button class="primary small" onclick="savePriority()">保存权重</button></div>
      </div>
    </section>

    <!-- ============ 资源站 ============ -->
    <section class="page" id="page-sources" aria-labelledby="title-sources">
      <header class="page-head">
  <h2 class="page-title" id="title-sources" tabindex="-1">资源站</h2>
        <p class="page-desc">标准苹果 CMS 源与扩展源；启用后进入聚合搜索与海量片库。</p>
      </header>
      <div class="card">
        <div class="card-title">资源站列表 <span class="badge" id="srcCount"></span>
          <button class="small primary" id="srcRefreshBtn" onclick="refreshSources()" title="重建分类路由、清空搜索缓存，把列表与运行时状态对齐到当前配置（不重启插件）">⟳ 刷新</button>
          <button class="small" style="margin-left:auto" onclick="testAll()">全部测速</button>
          <button class="small" id="srcProbeBtn" onclick="sourceProbeStart()">批量探活</button>
          <button class="small ghost" id="srcProbeCancelBtn" style="display:none" onclick="sourceProbeCancel()">中止</button>
          <button class="small" onclick="cmsHealthCheck()" title="用热门词批量检测纯 CMS 源是否有效，失效写入健康画像触发熔断">CMS 批量体检</button>
          <button class="small" onclick="restoreBuiltin()">恢复内置源</button>
          <button class="small" onclick="exportCmsSources()" title="导出当前纯 CMS 源为 JSON 备份">⬇ 导出</button>
          <label class="btn-sm" style="border-color:var(--ok);background:var(--ok-soft);color:var(--ok)" title="从 JSON 文件导入 CMS 源，自动去重">
            ⬆ 导入<input type="file" id="cmsImportFile" accept=".json,application/json" class="file-input" onchange="importCmsFile(this)"/>
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
          <button class="small" id="srcExpandBtn" onclick="toggleSrcExpand()">展开全部</button>
        </div>
        <div class="src-wrap" id="srcWrap" tabindex="0" role="region" aria-label="资源站列表">
        <table>
          <thead><tr><th style="width:42px">启用</th><th style="width:96px">选项</th><th>资源站</th>
            <th style="width:130px">连通性</th><th style="width:70px">测试</th><th style="width:44px"></th></tr></thead>
          <tbody id="srcRows"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
        </table>
        </div>
        <div class="source-add mt" id="addGrid">
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
        <div id="srcProbeProgress" class="hint" style="display:none"></div>
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
        <div class="table-scroll" tabindex="0" role="region" aria-label="资源站连通性列表">
        <table>
          <thead><tr><th>源</th><th style="width:80px">样本</th>
            <th style="width:110px">成功率</th><th style="width:100px">平均延迟</th>
            <th style="width:90px">状态</th><th style="width:70px">操作</th></tr></thead>
          <tbody id="healthRows"><tr><td colspan="6" class="muted">播放几次后自动生成。</td></tr></tbody>
        </table>
        </div>
      </div>
    </section>

    <!-- ============ 导入 TVBox 订阅（独立页） ============ -->
    <section class="page" id="page-importsub" aria-labelledby="title-importsub">
      <header class="page-head">
  <h2 class="page-title" id="title-importsub" tabindex="-1">导入订阅</h2>
        <p class="page-desc">粘贴一个 TVBox / 影视仓 / OK影视 订阅 URL 或整段配置，自动识别并分类导入，<strong>导入即可在搜索与播放源中使用</strong>。支持 base64 与多订阅聚合。</p>
      </header>
      <div class="card">
        <div class="card-title">订阅抓取代理（可选）
          <label style="float:right;display:flex;align-items:center;gap:6px;font-size:12px;font-weight:400;cursor:pointer">
            <input type="checkbox" id="subProxyEnable" style="width:14px;height:14px;accent-color:var(--accent)" onchange="onSubProxyToggle()"/> 启用代理</label></div>
        <p class="card-sub">仅用于<strong>拉取订阅配置、DRPY 脚本</strong>（GitHub 等被墙/解析失败时可走 Clash）。<strong>播放、搜索、CMS 探测一律不走此代理</strong>。形如 <code>http://用户:密码@主机:端口</code>，清空=直连。</p>
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
          <!-- >>> PRIVATE:DRPY -->
          与 <strong>远程 JS 爬虫源（DRPY）</strong>
          <!-- <<< PRIVATE:DRPY -->
          能直接播放的<strong>自动启用</strong>；P2P/磁力/云盘等慢源与脚本暂未就绪的源导入后默认关闭，可在结果里开启或补传；
          网盘/直播/无法识别的源跳过并说明原因。</p>
        <div class="row"><input type="text" id="subUrl" placeholder="配置订阅 URL（支持 T4 / base64 / 多订阅聚合）" style="flex:2"/>
          <button class="primary small" onclick="importSubscription('url')">按 URL 导入</button>
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


      <!-- >>> PRIVATE:DRPY -->
      <div class="card" style="margin-top:14px">
        <div class="card-title">已导入的远程 JS 爬虫（DRPY）</div>
        <p class="card-sub">规则脚本来自订阅 URL，由本地 Node 运行时执行；脚本下载失败可重试。</p>
        <div id="drpyMgmt"><div class="muted sm">导入后自动显示。</div></div>
      </div>
      <!-- <<< PRIVATE:DRPY -->

    </section>

    <!-- >>> PRIVATE:SCANLOGIN -->
    <!-- ============ 网盘登录（第三方网盘） ============ -->
    <section class="page" id="page-scanlogin" aria-labelledby="title-scanlogin">
      <header class="page-head">
  <h2 class="page-title" id="title-scanlogin" tabindex="-1">网盘登录</h2>
        <p class="page-desc">选择网盘进行登录授权。第三方网盘登录后网盘高清线路才能自动转存并出原画。</p>
      </header>

      <div class="card">
        <div class="card-title">第三方网盘 <span class="pill off" id="panScanPill">未登录</span></div>
        <p class="card-sub">夸克 / UC / 阿里云盘。登录后「网盘高清」兜底线路才能自动转存并出原画。</p>
        <div class="btn-row">
          <button class="btn primary small" onclick="openPanScan()">打开网盘登录</button>
          <button class="btn small" onclick="refreshPanScan()">刷新登录状态</button>
        </div>
        <div class="hint" id="panScanMsg">未加载</div>
        <iframe id="panScanFrame" title="第三方网盘登录" data-src="/v/api/ext/pan/page"
          style="display:none;width:100%;height:640px;border:1px solid var(--line);border-radius:12px;margin-top:10px"></iframe>
      </div>

      <div class="card">
        <div class="card-title">一键重置</div>
        <p class="card-sub">清空所有设置与已导入的资源站列表，回到初始状态。重置后可在「资源站」里用「恢复内置」找回内置源。</p>
        <div class="btn-row">
          <button class="btn danger small" onclick="resetAll()">一键重置所有设置</button>
        </div>
        <div class="hint" id="resetMsg"></div>
      </div>

      <div class="card">
        <div class="card-title">PanSou 网盘搜索</div>
        <p class="card-sub">网盘搜索线路的来源。填自己的自托管 PanSou 实例地址（上游 <code>fish2018/pansou</code>）。</p>
        <label class="field"><span>实例地址</span>
          <input type="text" id="pansouUrl" placeholder="http://你的NAS:8888"/></label>
        <label class="field"><span>网盘类型（逗号分隔）</span>
          <input type="text" id="pansouClouds" placeholder="quark,uc,aliyun"/></label>
        <div class="row">
          <label class="field" style="max-width:140px"><span>单次超时（秒）</span>
            <input type="number" id="pansouTimeout" min="3" max="120" style="width:100%"/></label>
          <label class="field" style="max-width:140px"><span>结果条数上限</span>
            <input type="number" id="pansouLimit" min="1" max="80" style="width:100%"/></label>
        </div>
        <label class="switch-row"><input type="checkbox" id="pansouEnabled" checked/>
          <span>启用网盘搜索线路<span class="sub">搜索结果作为真实的一集出现在线路里，播放时自动转存</span></span></label>
        <div class="btn-row">
          <button class="btn primary small" onclick="savePansouCfg()">保存</button>
          <span class="hint" id="pansouCfgMsg"></span>
        </div>
      </div>

      <div class="card">
        <div class="card-title">网盘播放缓存</div>
        <p class="card-sub">转存缓存占满会导致无法播放。默认保留 1 天自动清理，也可手动清理。</p>
        <label class="switch-row"><input type="checkbox" id="panCacheAuto" checked/>
          <span>定时自动清理<span class="sub">已开启自动清理，到期自动清除网盘播放缓存</span></span></label>
        <label class="field"><span>缓存保留天数</span>
          <select id="panCacheDays">
            <option value="1">1 天</option>
            <option value="3">3 天</option>
            <option value="7">7 天</option>
          </select></label>
        <div class="btn-row">
          <button class="btn primary small" onclick="clearPanCache()">立即清理播放缓存</button>
          <button class="btn small" onclick="savePanCache()">保存设置</button>
        </div>
        <div class="hint" id="panCacheMsg">未清理</div>
      </div>
    </section>
    <!-- <<< PRIVATE:SCANLOGIN -->


    <!-- >>> PRIVATE:CATVOD -->
    <!-- ============ 小雅网盘与 DRPY ============ -->
    <section class="page" id="page-xiaoya" aria-labelledby="title-xiaoya">
      <header class="page-head">
  <h2 class="page-title" id="title-xiaoya" tabindex="-1">小雅网盘与 DRPY</h2>
        <p class="page-desc">小雅网盘（AList HTTP 接入）与 drpyS JS 爬虫（Node 运行时）。仅完整版本地运行。</p>
      </header>
      <div class="card">
        <div class="card-title">运行时
                  <span class="pill off" id="nodePill">Node(JS)</span></div>
        <h3 style="font-size:14px;margin:6px 0">小雅网盘（Alist 网盘聚合）</h3>
        <div class="row">
          <input type="text" id="xyServer" placeholder="小雅服务器，如 http://192.168.x.x:5678" style="flex:2"/>
          <input type="text" id="xyUser" placeholder="账号" style="max-width:130px"/>
          <input type="password" id="xyPass" placeholder="密码（留空不改）" style="max-width:170px" autocomplete="new-password"/>
          <button class="primary small" onclick="saveXiaoya()">保存</button>
        </div>
        <div class="hint" id="xyMsg"></div>
        <div class="row"><h3 style="font-size:14px;margin:6px 0">目录浏览</h3>
          <button class="small" onclick="xyCrumbUp()">上级</button>
          <span id="xyCrumb" class="muted sm"></span></div>
        <div class="table-scroll" tabindex="0" role="region" aria-label="网盘文件夹列表">
        <table>
          <thead><tr><th>分类 / 文件夹</th><th style="width:80px">操作</th></tr></thead>
          <tbody id="xyRows"><tr><td colspan="2" class="muted">保存并连接后显示分类</td></tr></tbody>
        </table>
        </div>
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
    <section class="page" id="page-search" aria-labelledby="title-search">
      <header class="page-head">
  <h2 class="page-title" id="title-search" tabindex="-1">搜索设置</h2>
      </header>
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
        <label class="field" style="max-width:260px"><span>冷片二级目录等待（秒）</span>
          <input type="number" id="folderBindWait" min="0" max="25" step="0.5" style="width:100%"/><span class="sub">冷片首次进二级线路墙时等搜索绑定的上限；0=本次不等、只后台绑定（重进即有）</span></label>
        <label class="switch-row"><input type="checkbox" id="posterSpeedBadge"/>
          <span>海报速度角标<span class="sub">列表卡标题后显示该片实测最快线路的速度（如 ⚡860KB/s）；只用竞速/播放中已测到的速度，没有实测值不显示</span></span></label>
        <label class="switch-row"><input type="checkbox" id="lineAutofail"/>
          <span>坏线自动切换<span class="sub">当前线路失效/文件不存在时自动换到可用线路，并短时跳过坏线（关闭则纯手动切线）</span></span></label>
        <label class="switch-row"><input type="checkbox" id="fixedLineDirect"/>
          <span>手动选线直接出流<span class="sub">手动选定某条线路后直接播放，不再先探活；已知坏线仍自动换到可用线路，自动起播不受影响</span></span></label>
        <label class="switch-row"><input type="checkbox" id="midPlaybackSwitch"/>
          <span>播放中静默换线<span class="sub">自动起播的线路在播放中变慢时，按已实测的分片耗时/吞吐静默切到同集更快的线，不打断播放；手动选线不受影响，30 秒内最多换一次</span></span></label>
        <label class="switch-row"><input type="checkbox" id="skipIntroEnabled"/>
          <span>跳过片头<span class="sub">剧集点播时从播放清单剪掉片头分片，从片头后开始播；只对剧集生效，电影/直播不剪；本页设置，保存后对全部客户端生效</span></span></label>
        <label class="field" style="max-width:260px"><span>片头跳过（秒，0-300）</span>
          <input type="number" id="skipIntroSeconds" min="0" max="300" style="width:100%"/></label>
        <label class="switch-row"><input type="checkbox" id="skipOutroEnabled"/>
          <span>跳过片尾<span class="sub">剧集点播时从播放清单剪掉片尾分片；片长小于片头+片尾时不剪</span></span></label>
        <label class="field" style="max-width:260px"><span>片尾跳过（秒，0-300）</span>
          <input type="number" id="skipOutroSeconds" min="0" max="300" style="width:100%"/></label>
        <label class="switch-row"><input type="checkbox" id="linePredict"/>
          <span>本片线路预测<span class="sub">浏览海报时对首屏影片的少数候选线做轻量探活，记下本片最快线；可信时起播直接用该线、跳过竞速</span></span></label>
        <label class="switch-row"><input type="checkbox" id="browseProbe"/>
          <span>浏览时探活<span class="sub">列表返回后，后台对首屏前几部影片做轻量探活（不拖慢列表；关闭则浏览不探活）</span></span></label>
        <label class="switch-row"><input type="checkbox" id="linePredictZeroRace"/>
          <span>可信预测零竞速直出<span class="sub">记忆新鲜且够快时点播放直接用预测线、跳过竞速；关时恢复全量竞速（预测线只作排序种子）</span></span></label>
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
        <div class="cache-wrap" id="cacheWrap">
          <table>
            <thead><tr><th>标题</th><th style="width:100px">来源</th><th style="width:70px">集数</th></tr></thead>
            <tbody id="rows"><tr><td colspan="3" class="muted">加载中…</td></tr></tbody>
          </table>
        </div>
        <div class="cache-more" id="cacheMore" style="display:none">
          <button class="small" onclick="toggleCacheAll()">显示更多</button>
        </div>
      </div>

      <!-- 一键清理：只留一个按钮，分项管理已去掉（用户要求） -->
      <div class="card">
        <div class="card-title">🗑 一键清理
          <span class="hint" style="font-weight:400">清除搜索、榜单、海量库运行缓存，不重启服务；后续访问自动重建</span>
        </div>
        <div class="btn-row">
          <button class="small danger" onclick="clearCaches(['search','charts','catalog'])">一键清理运行缓存</button>
        </div>
      </div>
    </section>

    <!-- ============ 关于 ============ -->
    <section class="page" id="page-about" aria-labelledby="title-about">
      <header class="page-head">
  <h2 class="page-title" id="title-about" tabindex="-1">关于</h2>
      </header>
      <div class="card">
        <div class="card-title">🎬 fnmedia-ext · 飞牛影视在线扩展</div>
        <p class="card-sub">在原生搜索 / 媒体库注入在线影视并直接调用飞牛原生播放器播放。</p>
        <div class="stack sm muted">
          <div>开发作者：<a href="https://github.com/funwukong/fnmedia-ext" target="_blank" rel="noopener">funwukong</a></div>
        </div>
        <div class="stack sm muted">
          <div>· 热门资源：豆瓣真实榜单四库，点击搜源、优先高质量源播放。</div>
          <div>· 海量片库：苹果 CMS 分类深翻，跨源聚合成海量四库与单源独立库。</div>
        </div>
        <div class="hint" id="aboutEdition"></div>
      </div>

      <div class="card">
        <div class="card-title">🗄️ 配置备份与恢复</div>
        <p class="card-sub">导出内容会自动脱敏。恢复前先预览差异，敏感字段保留当前设备值。</p>
        <div class="btn-row">
          <button class="small primary" onclick="exportConfigBackup()">导出脱敏配置</button>
          <label class="btn small">选择备份文件<input type="file" id="configRestoreFile" accept=".json" class="file-input" onchange="previewConfigRestore(this)"></label>
        </div>
        <div id="configRestorePreview" class="hint" style="white-space:pre-wrap;margin-top:8px"></div>
        <button id="configRestoreApply" class="small danger" style="display:none;margin-top:8px" onclick="applyConfigRestore()">确认恢复预览中的配置</button>
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
                     style="flex:1 1 240px;min-width:0">
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
            <div style="color:var(--warn);font-weight:600">新令牌（只显示一次，请立即复制保存）：</div>
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

<nav class="bottom-nav" id="bottomNav" aria-label="快捷导航">
  <button type="button" class="nav-item active" data-page="overview" aria-controls="page-overview" aria-current="page"><span class="nav-icon" aria-hidden="true">▦</span>概览</button>
  <button type="button" class="nav-item" data-page="library" aria-controls="page-library"><span class="nav-icon" aria-hidden="true">▤</span>媒体库</button>
  <button type="button" class="nav-item" data-page="quality" aria-controls="page-quality"><span class="nav-icon" aria-hidden="true">≋</span>质量</button>
  <button type="button" class="nav-item" data-page="sources" aria-controls="page-sources"><span class="nav-icon" aria-hidden="true">◎</span>资源站</button>
  <button type="button" class="nav-item" data-page="search" aria-controls="page-search"><span class="nav-icon" aria-hidden="true">⌕</span>搜索</button>
</nav>

<dialog id="themeDialog" class="theme-dialog" aria-labelledby="themeTitle" aria-describedby="themeHelp">
  <div class="theme-head">
    <h2 id="themeTitle">外观设置</h2>
    <button type="button" class="btn small" id="themeClose" autofocus>关闭</button>
  </div>
  <p class="hint" id="themeHelp">外观即时生效，偏好仅保存在当前浏览器。</p>
  <fieldset>
    <legend>界面风格</legend>
    <div class="theme-options">
      <label><input type="radio" name="themeFamily" value="classic" checked>经典</label>
      <label><input type="radio" name="themeFamily" value="apple">Apple Glass</label>
      <label><input type="radio" name="themeFamily" value="harmony">HarmonyOS</label>
    </div>
  </fieldset>
  <fieldset>
    <legend>显示模式</legend>
    <div class="theme-options">
      <label><input type="radio" name="themeMode" value="system" checked>跟随系统</label>
      <label><input type="radio" name="themeMode" value="light">浅色</label>
      <label><input type="radio" name="themeMode" value="dark">深色</label>
    </div>
  </fieldset>
  <fieldset>
    <legend>强调色</legend>
    <div class="theme-swatches" id="themeSwatches">
      <button type="button" class="theme-swatch" data-palette="indigo" aria-pressed="true">靛青</button>
      <button type="button" class="theme-swatch" data-palette="blue" aria-pressed="false">海蓝</button>
      <button type="button" class="theme-swatch" data-palette="teal" aria-pressed="false">青绿</button>
      <button type="button" class="theme-swatch" data-palette="rose" aria-pressed="false">玫红</button>
      <button type="button" class="theme-swatch" data-palette="amber" aria-pressed="false">琥珀</button>
      <button type="button" class="theme-swatch" data-palette="slate" aria-pressed="false">岩灰</button>
      <button type="button" class="theme-swatch" data-palette="appleSky" aria-pressed="false">清透蓝</button>
      <button type="button" class="theme-swatch" data-palette="appleMidnight" aria-pressed="false">深空黑</button>
      <button type="button" class="theme-swatch" data-palette="appleSunset" aria-pressed="false">日落橙</button>
      <button type="button" class="theme-swatch" data-palette="appleForest" aria-pressed="false">森林绿</button>
      <button type="button" class="theme-swatch" data-palette="appleLavender" aria-pressed="false">星光紫</button>
      <button type="button" class="theme-swatch" data-palette="harmonyAurora" aria-pressed="false">鸿蒙星耀</button>
      <button type="button" class="theme-swatch" data-palette="harmonySky" aria-pressed="false">鸿蒙晴空</button>
      <button type="button" class="theme-swatch" data-palette="harmonyDream" aria-pressed="false">鸿蒙绮境</button>
    </div>
    <form class="theme-custom" id="themeCustom" novalidate>
      <div><label for="themeColor">选色</label><input type="color" id="themeColor" value="#4f46e5"></div>
      <label for="themeHex">自定义色值
        <input type="text" id="themeHex" value="#4f46e5" placeholder="#4f46e5" maxlength="7" spellcheck="false" autocomplete="off" aria-describedby="themeError">
      </label>
      <button type="submit" class="btn primary">应用颜色</button>
    </form>
    <p class="hint theme-error" id="themeError" role="alert"></p>
  </fieldset>
  <div class="theme-actions">
    <button type="button" class="btn small" id="themeReset">恢复默认外观</button>
    <span id="themeSelection" class="badge"></span>
  </div>
  <p id="themeStatus" class="theme-status" role="status"></p>
</dialog>

<div id="toast" role="status"></div>

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

/* ---------- 外观与导航 ---------- */
const themeDialog = $("themeDialog");
const themeTrigger = $("themeTrigger");
const themeHex = $("themeHex");
const themeColor = $("themeColor");
const themeModes = $$('[name="themeMode"]');
const themeFamilies = $$('[name="themeFamily"]');
const themeSwatches = $$("#themeSwatches [data-palette]");
const menuTrigger = $("menuTrigger");
const mainNav = $("nav");

function clearThemeError(){
  themeHex.removeAttribute("aria-invalid");
  $("themeError").textContent = "";
}
function syncTheme(){
  const {mode, palette, custom, family} = adminTheme.get();
  themeModes.forEach(input => {input.checked = input.value === mode;});
  themeFamilies.forEach(input => {input.checked = input.value === family;});
  themeSwatches.forEach(button => button.setAttribute("aria-pressed", String(button.dataset.palette === palette)));
  themeColor.value = themeHex.value = custom;
  $("themeSelection").textContent = palette === "custom" ? "当前：自定义 " + custom : "当前：" +
    themeSwatches.find(button => button.dataset.palette === palette).textContent;
  clearThemeError();
}
adminTheme.subscribe(saved => {
  syncTheme();
  $("themeStatus").textContent = saved ? "外观已更新。" : "外观已应用，但当前浏览器无法保存偏好。";
});
themeModes.forEach(input => input.addEventListener("change", () => adminTheme.set({mode: input.value})));
themeFamilies.forEach(input => input.addEventListener("change", () => adminTheme.set({family: input.value})));
themeSwatches.forEach(button => {
  const palette = button.dataset.palette;
  button.style.setProperty("--swatch", adminTheme.presets[palette]);
  button.addEventListener("click", () => adminTheme.set({palette}));
});
themeColor.addEventListener("change", () => adminTheme.set({palette: "custom", custom: themeColor.value}));
themeHex.addEventListener("input", clearThemeError);
$("themeCustom").addEventListener("submit", event => {
  event.preventDefault();
  const custom = themeHex.value.trim();
  if(!adminTheme.validHex(custom)){
    themeHex.setAttribute("aria-invalid", "true");
    $("themeError").textContent = "请输入六位十六进制色值，例如 #4f46e5。";
    themeHex.focus();
    return;
  }
  adminTheme.set({palette: "custom", custom});
});
$("themeReset").addEventListener("click", () => adminTheme.reset());
themeTrigger.addEventListener("click", () => {
  setMenuOpen(false);
  syncTheme();
  $("themeStatus").textContent = "";
  themeDialog.showModal();
});
$("themeClose").addEventListener("click", () => themeDialog.close());
themeDialog.addEventListener("close", () => themeTrigger.focus());
syncTheme();

function setMenuOpen(open){
  mainNav.classList.toggle("open", open);
  menuTrigger.setAttribute("aria-expanded", String(open));
}
menuTrigger.addEventListener("click", () => {
  const open = menuTrigger.getAttribute("aria-expanded") !== "true";
  setMenuOpen(open);
  if(open) mainNav.querySelector('[aria-current="page"], .nav-item').focus();
});
document.addEventListener("keydown", event => {
  if(event.key === "Escape" && menuTrigger.getAttribute("aria-expanded") === "true"){
    setMenuOpen(false);
    menuTrigger.focus();
  }
});
document.addEventListener("click", event => {
  if(!mainNav.contains(event.target) && !menuTrigger.contains(event.target)) setMenuOpen(false);
});

/* ---------- 页面切换 ---------- */
async function switchPage(page){
  $$(".page").forEach(el=>el.classList.toggle("active",el.id==="page-"+page));
  $$("[data-page]").forEach(el=>{
    const current=el.dataset.page===page;
    el.classList.toggle("active",current);
    if(current)el.setAttribute("aria-current","page");
    else el.removeAttribute("aria-current");
  });
  window.scrollTo(0,0);
  if(page==="overview"){renderOverview();startLivePoll();}
  else{stopLivePoll();}
  if(page==="library"){loadLibs().then(renderLibrary);}
  if(page==="quality")renderQuality();
  if(page==="sources"){try{loadHealth();}catch(e){}try{initSrcProxy();}catch(e){}}
  if(page==="importsub"){try{if(!CFG)await loadStatus();}catch(e){}try{if(typeof initSubProxy==="function")initSubProxy();}catch(e){}try{loadAdfilter();}catch(e){}try{if(typeof loadDrpyMgmt==="function")loadDrpyMgmt();}catch(e){}}
  if(page==="about"){loadUpdate(false);try{loadAdminToken();}catch(e){}}
  // >>> PRIVATE:SCANLOGIN
  if(page==="scanlogin"){try{await refreshPanScan();}catch(e){}try{await loadPansouCfg();}catch(e){}}
  // <<< PRIVATE:SCANLOGIN
}
$$("[data-page]").forEach(button=>button.addEventListener("click",()=>{
  const page=button.dataset.page;
  setMenuOpen(false);
  switchPage(page);
  $("title-"+page).focus({preventScroll:true});
}));

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
    body.innerHTML='<span style="color:var(--warn)">检查更新失败：'+esc(e.message)+'（可能无法访问 GitHub）</span>';
    $u("updActions").style.display="flex";$u("updBtn").style.display="none";
    if(force)toast("检查更新失败："+e.message,"fail");
  }finally{
    if(rf){rf.disabled=false;rf.textContent="重新检查";}
  }
}
function renderUpdate(r){
  const ver=$u("updCur");
  ver.textContent=r.current_version||"—";
  ver.title="连续点击5次显示福利设置";
  ver.onclick=()=>{
    ver.dataset.clicks=String((Number(ver.dataset.clicks||0)+1));
    if(Number(ver.dataset.clicks)>=5){
      const card=$u("welfareCard");
      if(card)card.style.display="block";
      ver.dataset.clicks="0";toast("福利设置已显示");
    }
  };
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
    body.innerHTML='<span style="color:var(--warn)">'+esc(r.last_error||"检查更新失败")+'</span>';
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
const LIVE_ORIGIN={local:["local","var(--ok)"],mixed:["mixed","var(--info)"],
  upstream:["upstream","var(--warn)"],redirect:["redirect","var(--violet)"]};
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
    const kindColor={tv:"var(--warn)",android:"var(--ok)",ios:"var(--info)",web:"var(--violet)"}[dv.kind]||"var(--muted)";
    const sess=dv.session;
    const sessHtml=sess?`<div class="sm" style="margin-top:4px">
        ${sess.error?'<span class="tag amber">播放异常</span>':'<span class="tag">在播</span>'}
        <b>${esc(sess.title||"（未知名）")}</b>
        <span class="muted">· ${LIVE_STAGE[sess.stage]||sess.stage} · ${ago((d.now||dv.seen)-sess.at)}前</span>
        ${sess.error?`<div class="sm" style="color:var(--err);margin-top:2px">⚠ ${esc(sess.error)}</div>`:""}
      </div>`:"";
    const errBadge=dv.errors?` <span style="color:var(--err);font-weight:700">· ${dv.errors} 次错误</span>`:"";
    const rows=(dv.events||[]).slice(0,8).map(e=>{
      const st=e.status;const col=st>=400?"var(--err)":(st>=300?"var(--violet)":"var(--ok)");
      return `<div class="live-row">
        <span style="color:${col};font-weight:600;min-width:34px;display:inline-block">${st}</span>
        ${liveOrigin(e.origin)}
        <span class="tag" style="background:var(--inset);color:var(--muted)">${LIVE_STAGE[e.stage]||e.stage}</span>
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
    const color=f.severity==="err"?"var(--err)":(f.severity==="src"?"var(--info)":"var(--warn)");
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

async function loadRuntimeDiagnostics(){
  const box=$("runtimeDiagnostics");if(!box)return;
  try{
    const r=await jget("/v/api/ext/status");
    const d=(r&&r.data)||{}, metrics=d.search_metrics||{}, sources=metrics.sources||{};
    const pansou=d.pansou_metrics||{};
    const rows=Object.entries(sources).sort((a,b)=>(b[1].calls||0)-(a[1].calls||0)).slice(0,20);
    const tasks=d.background_tasks||[], hist=d.background_task_history||[];
    const cats=d.catalog&&d.catalog.categories||{};
    const routeN=Object.values(cats).reduce((n,x)=>n+(x.routes||0),0);
    box.innerHTML='<div class="sm"><b>分类路由</b> '+routeN+' 条 · '+esc(String(d.catalog&&d.catalog.updated_at||"尚无刷新时间"))+'</div>'+
      '<div class="sm" style="margin-top:6px"><b>后台任务</b> '+(tasks.length?tasks.map(t=>esc(t.name)+" · "+esc(String(t.running_seconds))+"s").join("；"):"当前无运行任务")+'</div>'+
      '<div class="sm" style="margin-top:6px"><b>搜索源统计</b></div>'+
      (rows.length?'<div class="sm muted">'+rows.map(([id,x])=>esc(id)+": "+x.calls+"次 / 成功"+x.success+" / 失败"+x.failed+" / 取消"+x.cancelled+" / 均值"+x.avg_duration_ms+"ms / 结果"+x.results).join("<br>")+'</div>':'<div class="sm muted">暂无搜索统计</div>')+
      '<div class="sm" style="margin-top:6px"><b>PanSou</b> '+(pansou.runs||0)+'次 · 搜索均值'+(pansou.avg_search_ms||0)+'ms · 转存均值'+(pansou.avg_resolve_ms||0)+'ms · 成功'+(pansou.resolve_ok||0)+' · 失败'+(pansou.failed||0)+' · 等待登录'+(pansou.waiting_login||0)+'</div>'+
      '<div class="sm" style="margin-top:6px"><b>最近后台任务</b> '+(hist.length?hist.slice(-8).reverse().map(t=>esc(t.name)+" · "+esc(t.state)+(t.error_type?" ("+esc(t.error_type)+")":"")).join("；"):"暂无任务历史")+'</div>';
  }catch(e){box.textContent="读取运行诊断失败："+e.message;}
}

function renderOverview(){
  loadRuntimeDiagnostics();
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
  const grp={standalone:"独立源库",xlib:"小雅",hgdj:"红果短剧"}[L.group]||"";
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
    const og=$("otherLibs");if(og)og.innerHTML=other.length?other.map(L=>libCardHtml(L,{standalone:"📺",xlib:"💾",hgdj:"🍅"}[L.group]||"📚")).join(""):"<div class=\"muted sm\">暂无独立源库 / 小雅 / 红果库。</div>";
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
let SRC_FILTER="",SRC_ENABLED="all",SRC_EXPANDED=false;
function srcFiltered(){const q=SRC_FILTER.trim().toLowerCase();
  return (CFG.sources||[]).filter(s=>{
    if(SRC_ENABLED==="on"&&!s.enabled)return false;
    if(SRC_ENABLED==="off"&&s.enabled)return false;
    if(q&&!(String(s.name||"").toLowerCase().includes(q)
      ||String(s.api||"").toLowerCase().includes(q)))return false;
    return true;});}
function onSrcFilter(v){SRC_FILTER=v;renderSources();}
function onSrcEnabledFilter(v){SRC_ENABLED=v;renderSources();}
function toggleSrcExpand(){
  SRC_EXPANDED=!SRC_EXPANDED;
  const w=$("srcWrap");
  if(w)w.classList.toggle("expanded",SRC_EXPANDED);
  const b=$("srcExpandBtn");
  if(b)b.textContent=SRC_EXPANDED?"收起":"展开全部";
  renderSources();
}
/* ---------- 资源站列表渲染（性能优化版） ----------
   旧实现两个致命点：
   1) 每行 querySelector × 6 + addEventListener × 6 —— 500 个源就是 3000 个
      监听器，每次筛选/切页整表重建，主线程卡死数百毫秒。
   2) innerHTML 一次性拼整表，几百行大字符串会让排版一次重算。
   现改为：
   - 事件委托：一个监听器挂在 tbody，靠 closest + data-* 分发，零 per-row 绑定。
   - 分帧渲染：首屏先铺 SRC_FIRST 行，剩余用 requestAnimationFrame 分批追加，
     首屏立刻可交互，滚动时后续行陆续填满。
   - 渲染代数（SRC_GEN）：筛选条件一变就 ++，旧的 rAF 批次发现代数不符自动放弃，
     避免"快速输入筛选词"时多批渲染互相覆盖。 */
const SRC_FIRST=60, SRC_CHUNK=120;
let SRC_GEN=0, SRC_ROWS_HTML=[], SRC_RAF=0;

function srcRowHtml(src){
  let extraCell="",extraTag="";
  // >>> PRIVATE:ADULT
  extraCell=FEAT_ADULT?`
      <label title="🔞 限制级独立库（18+）：与主库隔离、不进全局搜索；开启即表示你已年满 18 周岁并自行承担责任"
        style="display:inline-flex;align-items:center;gap:2px;font-size:11px;cursor:pointer;margin-left:4px">
        <input type="checkbox" class="ad" ${src.adult?"checked":""}
         ${(src.enabled&&src.standalone&&!(FEAT_DDUO&&src.xsrc))?"":"disabled"} style="width:15px;height:15px;accent-color:#be123c"/>🔞</label>`:"";
  // <<< PRIVATE:ADULT
  // >>> PRIVATE:DDUO
  extraTag=(FEAT_DDUO&&src.xsrc)?'<span class="tag amber" title="非苹果CMS自研站，WASM实时解码">WASM</span>':'';
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
      <td class="src-name" title="${esc(src.api)}"><div style="font-weight:500">${esc(src.name)}${src.kind==="t4"?'<span class="tag">T4</span>':''}${extraTag}${src.builtin?'':'<span class="tag amber">自定义</span>'}${src.standalone?'<span class="tag">媒体库</span>':''}</div>
        <div class="meta">${esc(src.api)}</div></td>
      <td class="st wait" data-st>未测</td>
      <td><button class="small" data-test>测试</button></td>
      <td><button class="del" data-del title="删除">✕</button></td>
    </tr>`;
}

function renderSources(){
  const srcsAll=srcFiltered();
  const all=CFG.sources||[];
  $("srcCount").textContent=all.filter(x=>x.enabled).length+"/"+all.length+" 启用";
  const pi=$("srcPageInfo");
  const wrap=$("srcWrap");
  if(wrap)wrap.classList.toggle("compact",srcsAll.length>40);
  const tb=$("srcRows");
  SRC_GEN++;
  const gen=SRC_GEN;
  if(SRC_RAF){cancelAnimationFrame(SRC_RAF);SRC_RAF=0;}
  SRC_ROWS_HTML=[];
  if(!srcsAll.length){
    tb.innerHTML='<tr><td colspan="6" class="muted">没有匹配的资源站（调整筛选条件或清空筛选）。</td></tr>';
    if(pi)pi.textContent="共 0 条";
  }else{
    // 首屏：同步铺前 SRC_FIRST 行
    const head=srcsAll.slice(0,SRC_FIRST);
    tb.innerHTML=head.map(srcRowHtml).join("");
    if(pi)pi.textContent=srcsAll.length>SRC_FIRST
      ? `共 ${srcsAll.length} 条（已显示 ${head.length}，向下滚动自动加载）`
      : `共 ${srcsAll.length} 条（已全部显示）`;
    // 余量：分帧追加，每帧 SRC_CHUNK 行
    let pos=SRC_FIRST;
    const step=()=>{
      if(gen!==SRC_GEN)return;           // 被新一轮渲染取代
      const slice=srcsAll.slice(pos,pos+SRC_CHUNK);
      if(!slice.length){SRC_RAF=0;
        if(pi)pi.textContent=`共 ${srcsAll.length} 条（已全部显示）`;
        return;}
      const frag=document.createElement("tbody");
      frag.innerHTML=slice.map(srcRowHtml).join("");
      while(frag.firstChild)tb.appendChild(frag.firstChild);
      pos+=SRC_CHUNK;
      SRC_RAF=requestAnimationFrame(step);
    };
    if(srcsAll.length>SRC_FIRST)SRC_RAF=requestAnimationFrame(step);
  }
  // T4 远程源清单
  const t4=(CFG.sources||[]).filter(s=>s.kind==="t4");
  const t4c=$("t4Count");if(t4c)t4c.textContent=t4.length?(t4.length+" 个"):"";
  const t4r=$("t4Rows");
  if(t4r)t4r.innerHTML=t4.length?t4.map(x=>{
    const bad=x.invalid?'<span class="tag warn">地址无效</span>':'<span class="tag">T4</span>';
    return `<div class="subrow"><span><b>${esc(x.name||x.api||"")}</b><br><span class="meta">${esc(x.api||"")}</span></span>${bad}</div>`;
  }).join(""):'<div class="muted sm">暂无 T4 源：去「导入订阅」导一次含 type=4 的订阅。</div>';
}

/* 事件委托：整张表只挂一个 change + 一个 click，靠 closest 分发。 */
function bindSrcDelegate(){
  const tb=$("srcRows");
  if(!tb||tb.dataset.bound)return;
  tb.dataset.bound="1";
  tb.addEventListener("change",e=>{
    const tr=e.target.closest("tr[data-id]");
    if(!tr)return;
    const id=tr.dataset.id;
    const t=e.target;
    if(t.classList.contains("en"))toggleEnable(id,t.checked);
    else if(t.classList.contains("sa"))toggleStandalone(id,t.checked);
    else if(t.classList.contains("lf"))toggleLoose(id,t.checked);
    // >>> PRIVATE:ADULT
    else if(FEAT_ADULT&&t.classList.contains("ad"))toggleAdult(id,t.checked,t);
    // <<< PRIVATE:ADULT
  });
  tb.addEventListener("click",e=>{
    const tr=e.target.closest("tr[data-id]");
    if(!tr)return;
    const id=tr.dataset.id;
    if(e.target.closest("[data-test]")){testOne(id,tr);return;}
    if(e.target.closest("[data-del]")){delSource(id,srcName(id));return;}
  });
}
async function refreshSources(){
  const btn=$("srcRefreshBtn");
  const old=btn?btn.textContent:"";
  if(btn){btn.disabled=true;btn.textContent="⟳ 刷新中…";}
  try{
    const r=await jpost("/v/api/ext/source/refresh",{scope:"all"});
    if(r.code!==0){toast("刷新失败："+(r.msg||""),"fail");return;}
    const d=r.data||{};
    // 用后端回传的最新源列表覆盖内存配置，顺带把健康等级灌进去。
    await loadStatus();
    bindSrcDelegate();
    renderSources();
    try{await loadHealth();}catch(_){}
    const c=d.counts||{};
    const rt=d.routes||{};
    let msg=`已刷新：${c.enabled||0}/${c.total||0} 启用`;
    if(rt.pending)msg+=` · 路由后台更新中`;
    else if(rt.error)msg+=` · 路由告警 ${rt.error}`;
    else if(rt.categories!==undefined)msg+=` · 分类 ${rt.categories}`;
    if(d.elapsed_ms!==undefined)msg+=` · ${d.elapsed_ms}ms`;
    toast(msg);
    const gs=$("globalState");
    if(gs)gs.textContent="接管运行中 · 已缓存 "+(STATUS&&STATUS.cached_titles||0);
  }catch(e){
    toast("刷新请求失败："+String(e),"fail");
  }finally{
    if(btn){btn.disabled=false;btn.textContent=old||"⟳ 刷新";}
  }
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
/* ---------- CMS 源批量探活（移植上游 beta.3，B9） ----------
   并发 4、700ms 轮询进度、可中止；已完成结果保留在行内状态。 */
let SRC_PROBE_TIMER=null;
async function sourceProbeStart(){
  try{
    await jpost("/v/api/ext/source/probe/start",{concurrency:4});
    $("srcProbeCancelBtn").style.display="";$("srcProbeProgress").style.display="";
    if(SRC_PROBE_TIMER)clearInterval(SRC_PROBE_TIMER);
    SRC_PROBE_TIMER=setInterval(sourceProbePoll,700);await sourceProbePoll();
  }catch(e){toast("启动探活失败："+e.message,"fail");}
}
async function sourceProbePoll(){
  try{
    const r=await jget("/v/api/ext/source/probe/status"),d=r.data||{};
    const unk=d.unknown_count||0, rem=d.removed_count||0;
    $("srcProbeProgress").textContent=`探活 ${d.done||0}/${d.total||0} · 可用 ${d.alive_count||0}`
      +(unk?` · 未定论 ${unk}`:"")+(rem?` · 已删 ${rem}`:"")
      +` · ${d.running?"进行中":"已结束"}`;
    (d.results||[]).forEach(x=>{
      const tr=$("srcRows").querySelector(`tr[data-id="${x.id}"]`);if(!tr)return;
      if(x.alive)setSt(tr,`✓ ${x.elapsed_ms||0}ms · ${x.count||0}条 · ${x.hit||"命中"}`,"ok");
      else if(x.state==="unknown")setSt(tr,"? "+(x.reason||"网络层失败，未定论")+"（未删除）","");
      else setSt(tr,"✕ "+(x.reason||x.error||"失效"),"bad");
      if(x.removed){
        const tds=tr.querySelectorAll("td");
        if(tds.length)tds[0].innerHTML='<span class="muted sm">已删除</span>';
      }
    });
    if(!d.running){
      if(SRC_PROBE_TIMER){clearInterval(SRC_PROBE_TIMER);SRC_PROBE_TIMER=null;}
      $("srcProbeCancelBtn").style.display="none";
      if(rem)setTimeout(loadSources,600);
    }
  }catch(e){/* 单次轮询失败下轮再试，不清空已保留结果 */}
}
async function sourceProbeCancel(){
  await jpost("/v/api/ext/source/probe/cancel",{});
  if(SRC_PROBE_TIMER){clearInterval(SRC_PROBE_TIMER);SRC_PROBE_TIMER=null;}
  $("srcProbeCancelBtn").style.display="none";
  $("srcProbeProgress").textContent="探活已中止，已完成结果保留";
}
/* ---------- CMS 源批量体检（移植上游 v0.2.71） ----------
   注意：导入订阅页的「一键全部体检」是另一功能（spScanStart，走
   体检 DRPY 源。
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
  const payload={standalone:standalone};
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

    try{await loadDrpyMgmt();}catch(e){}
  }catch(e){out.innerHTML='<div class="hint" style="color:var(--danger)">导入出错：'+esc(String(e))+'</div>';}
}
async function importFantaiying(){
  const out=$("subResult");
  out.innerHTML='<div class="muted sm">正在拉取内置「饭太硬」官方订阅（多镜像自动切换、自动解密），请稍候…</div>';
  try{
    const r=await jpost("/v/api/ext/source/import-subscription",
      {builtin:"fantaiying",standalone:$("subStandalone").checked});
    if(r.code!==0){out.innerHTML='<div class="hint" style="color:var(--danger)">导入失败：'+esc(r.msg||"")+'</div>';return;}
    renderSubResult(r.data||{});
    toast("饭太硬导入完成");
    try{loadAdfilter();}catch(e){}
    try{await loadStatus();}catch(e){}

    try{await loadDrpyMgmt();}catch(e){}
  }catch(e){out.innerHTML='<div class="hint" style="color:var(--danger)">导入出错：'+esc(String(e))+'</div>';}
}
function renderSubResult(d){
  const cms=d.cms||{},t4=d.t4||{},skips=d.skipped_detail||[];
  let h="";
  // 汇总
  const cmsOk=cms.added||0;
  const dr=d.drpy||{};
  const drOk=(dr.items||[]).filter(x=>x.enabled).length;
  const totalOk=cmsOk+drOk+(t4.added||0);
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

/* ---------- 搜索设置 / 噪音词 / 缓存（一键清理，2026-11-12 简化：分项管理已去掉） ---------- */
async function clearCaches(targets){
  const labels={titles:"在线资源缓存（会重启服务）",charts:"热门榜单",catalog:"海量库",search:"搜索缓存",health:"源健康画像"};
  const names=targets.map(t=>labels[t]||t).join("、");
  if(!confirm("确定清除："+names+" ？"))return;
  const r=await jpost("/v/api/ext/cache/clear",{targets});
  if(r.code===0){toast(r.msg||"已清除");
    if((r.data||{}).restart)setTimeout(()=>location.reload(),1800);
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
    folder_bind_wait:(()=>{const v=parseFloat($("folderBindWait").value);return Number.isFinite(v)?Math.max(0,Math.min(25,v)):8;})(),
    line_autofail:$("lineAutofail").checked,
    fixed_line_direct:$("fixedLineDirect").checked,
    mid_playback_switch:$("midPlaybackSwitch").checked,
    skip_intro_enabled:$("skipIntroEnabled").checked,
    skip_intro_seconds:Math.max(0,Math.min(300,parseInt($("skipIntroSeconds").value)||0)),
    skip_outro_enabled:$("skipOutroEnabled").checked,
    skip_outro_seconds:Math.max(0,Math.min(300,parseInt($("skipOutroSeconds").value)||0)),
    poster_speed_badge:$("posterSpeedBadge").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){toast("已保存");Object.assign(CFG,body);}else toast("保存失败 "+r.msg,"fail");
}
/* ---------- 本片线路预测开关（后端 /v/api/ext/linepredict/config 已在；上游 admin_ui 无此开关，按 fork 既有写法补） ---------- */
async function saveLinePredict(){
  const body={enabled:$("linePredict").checked,
    browse_probe:$("browseProbe").checked,
    max_lines:Math.max(1,Math.min(3,parseInt($("linePredictMax").value)||3)),
    ttl_min:Math.max(5,Math.min(120,parseInt($("linePredictTtl").value)||30)),
    zero_race:$("linePredictZeroRace").checked};
  const r=await jpost("/v/api/ext/linepredict/config",body);
  if(r.code===0){toast("预测设置已保存");Object.assign(CFG,{linepredict_enabled:body.enabled,browse_probe:body.browse_probe,linepredict_max_lines:body.max_lines,linepredict_ttl_min:body.ttl_min,linepredict_zero_race:body.zero_race});}
  else toast("保存失败 "+(r.msg||""),"fail");
}
async function resetLinePredict(){
  if(!confirm("清空已预测的本片最快线缓存？（不影响竞速实测记忆）"))return;
  const r=await jpost("/v/api/ext/linepredict/reset",{});
  if(r.code===0)toast("预测缓存已清空");else toast("清空失败 "+(r.msg||""),"fail");
}
let CACHE_ALL=[],CACHE_EXPANDED=false;
const CACHE_PAGE=30;
function toggleCacheAll(){
  CACHE_EXPANDED=!CACHE_EXPANDED;
  renderCache();
  const b=document.querySelector("#cacheMore button");
  if(b)b.textContent=CACHE_EXPANDED?"收起":"显示更多";
}
function renderCache(){
  const tb=$("rows");const wrap=$("cacheWrap");
  const list=CACHE_ALL;
  $("cacheN").textContent=list.length;
  const more=$("cacheMore");
  if(!list.length){
    tb.innerHTML='<tr><td colspan="3" class="muted">暂无，先去飞牛搜索任意中文片名</td></tr>';
    if(more)more.style.display="none";
    return;
  }
  const show=CACHE_EXPANDED?list:list.slice(0,CACHE_PAGE);
  tb.innerHTML=show.map(x=>`<tr><td><div style="font-weight:500">${esc(x.title)}</div>
    <div class="meta">${esc(x.year||"")} ${esc(x.area||"")} · ${esc(x.remarks||"")}</div></td>
    <td>${esc(x.provider)}</td><td>${x.episodes}</td></tr>`).join("");
  if(more){
    more.style.display=list.length>CACHE_PAGE?"flex":"none";
    const b=more.querySelector("button");
    if(b)b.textContent=CACHE_EXPANDED?"收起":("显示更多（还有 "+(list.length-CACHE_PAGE)+" 条）");
  }
  // 条目多时用紧凑排版，避免表格过高撑破容器
  if(wrap)wrap.classList.toggle("compact",list.length>60);
}
async function loadOnline(){
  const kw=$("kw").value.trim();
  const r=await jget("/v/api/ext/online?limit=500"+(kw?("&q="+encodeURIComponent(kw)):""));
  CACHE_ALL=r.data||[];
  CACHE_EXPANDED=false;
  renderCache();
}


// >>> PRIVATE:CATVOD
/* ---------- 小雅网盘与 DRPY ---------- */
let XY_PATH=[];
function setPill(id,ok,txt){const e=$(id);e.textContent=txt;e.className="pill "+(ok?"on":"off");}
async function loadPriv(){
  try{const c=await jget("/v/api/ext/catvod/credentials");const d=c.data||{};
    $("xyServer").value=d.server||"";$("xyUser").value=d.username||"";
    $("xyMsg").textContent=(d.has_password?"已保存密码（留空不修改）":"未设置密码")+" · 分类数 "+(d.drives||0);

  }catch(e){}
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
  if(r.code===0){toast("小雅配置已保存");$("xyPass").value="";$("xyMsg").textContent="已生效，分类数 "+(r.data.drives||0);}
  else toast(r.msg||"保存失败","fail");
}
async function savePanLib(){
  const r=await jpost("/v/api/ext/config",{pan_lib_enabled:$("panLibEnabled").checked});
  if(r.code===0){toast("小雅媒体库设置已保存");$("panLibMsg").textContent=$("panLibEnabled").checked?"已启用：进入媒体库时后台增量索引并补全海报":"已关闭";}
  else toast(r.msg||"保存失败","fail");
}
// <<< PRIVATE:CATVOD

// >>> PRIVATE:SCANLOGIN
async function loadPansouCfg(){
  try{
    const cfg=(await jget('/v/api/ext/status')).data.config||{};
    const set=(id,v)=>{const el=$(id);if(el)el.value=v;};
    set('pansouUrl',cfg.pansou_url||'');
    set('pansouClouds',cfg.pansou_cloud_types||'quark,uc,aliyun');
    set('pansouTimeout',cfg.pansou_timeout||30);
    set('pansouLimit',cfg.pansou_limit||30);
    const en=$('pansouEnabled');if(en)en.checked=cfg.pansou_enabled!==false;
  }catch(e){}
}
async function savePansouCfg(){
  const body={
    pansou_url:($('pansouUrl').value||'').trim().replace(/\/+$/,''),
    pansou_cloud_types:($('pansouClouds').value||'quark,uc,aliyun').trim(),
    pansou_timeout:parseInt($('pansouTimeout').value||'30',10),
    pansou_limit:parseInt($('pansouLimit').value||'30',10),
    pansou_enabled:!!$('pansouEnabled').checked};
  try{
    const j=await jpost('/v/api/ext/config',body);
    if(j.code!==0)throw new Error(j.msg||'保存失败');
    $('pansouCfgMsg').textContent='已保存';
    toast('网盘搜索设置已保存');
  }catch(e){$('pansouCfgMsg').textContent=e.message;toast('保存失败：'+e.message,'fail');}
}
async function savePanCache(){
  const days=parseInt($("panCacheDays").value||"1",10);
  const enabled=$("panCacheAuto").checked;
  try{
    const j=await jpost("/v/api/ext/config",{pan_cache_ttl_days:days,pan_cache_cleanup_enabled:enabled});
    if(j.code!==0)throw new Error(j.msg||"保存失败");
    $("panCacheMsg").textContent=enabled?("已开启自动清理，到期自动清除网盘播放缓存"):"已关闭自动清理";
    toast("网盘缓存设置已保存");
  }catch(e){$("panCacheMsg").textContent=e.message;toast("保存失败："+e.message,"fail");}
}
async function clearPanCache(){
  $("panCacheMsg").textContent="正在清理播放缓存，请稍候";
  try{
    const j=await jpost("/v/api/ext/pan/cache/clear",{});
    if(j.code!==0)throw new Error(j.msg||"清理失败");
    const d=j.data||{};
    if(!d.total){$("panCacheMsg").textContent="暂无播放缓存需要清理";toast("暂无播放缓存需要清理");return;}
    $("panCacheMsg").textContent="✅ 播放缓存清理完成";
    toast("✅ 播放缓存清理完成");
  }catch(e){$("panCacheMsg").textContent="清理失败："+e.message;toast("清理失败："+e.message,"fail");}
}
let CONFIG_RESTORE_DOC=null;
async function exportConfigBackup(){
  try{
    const r=await jget('/v/api/ext/config/export');
    if(r.code!==0)throw new Error(r.msg||'导出失败');
    const blob=new Blob([JSON.stringify(r.data,null,2)],{type:'application/json'});
    const a=document.createElement('a');a.href=URL.createObjectURL(blob);
    a.download='fnmedia-config-redacted.json';document.body.appendChild(a);a.click();
    setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},1000);
    toast('已导出脱敏配置');
  }catch(e){toast('导出失败：'+e.message,'fail');}
}
async function previewConfigRestore(input){
  const el=$('configRestorePreview'),btn=$('configRestoreApply');
  CONFIG_RESTORE_DOC=null;btn.style.display='none';
  try{
    const doc=JSON.parse(await input.files[0].text());
    const r=await jpost('/v/api/ext/config/restore-preview',doc);
    const d=r.data||{};
    if(!d.ok)throw new Error(d.reason||'预览失败');
    CONFIG_RESTORE_DOC=doc;
    el.textContent='校验通过\n变更字段：'+(d.changed_keys||[]).join('、')+'\n忽略字段：'+(d.ignored_keys||[]).join('、')+'\n脱敏字段：'+d.redacted_fields+' 个';
    btn.style.display='inline-block';
  }catch(e){el.textContent='预览失败：'+e.message;toast('配置预览失败','fail');}
}
async function applyConfigRestore(){
  if(!CONFIG_RESTORE_DOC||!confirm('确认恢复预览中的配置？敏感字段会保留当前值。'))return;
  try{
    const r=await jpost('/v/api/ext/config/restore',{confirm:true,document:CONFIG_RESTORE_DOC});
    if(r.code!==0||!(r.data||{}).ok)throw new Error(r.msg||r.data&&r.data.reason||'恢复失败');
    toast('配置恢复成功，页面即将刷新');setTimeout(()=>location.reload(),1000);
  }catch(e){toast('恢复失败：'+e.message,'fail');}
}
async function resetAll(){
  if(!confirm("确定一键重置？\n\n将清空所有设置与已导入的资源站列表。\n重置后可在「资源站」用「恢复内置」找回内置源。"))return;
  try{
    const j=await jpost("/v/api/ext/config/reset",{});
    if(j.code!==0)throw new Error(j.msg||"重置失败");
    $("resetMsg").textContent="✅ 重置完成";
    toast("✅ 重置完成，页面即将刷新");
    setTimeout(()=>location.reload(),1200);
  }catch(e){$("resetMsg").textContent="重置失败："+e.message;toast("重置失败："+e.message,"fail");}
}
async function openPanScan(){
  const f=$('panScanFrame');if(!f)return;
  if(!f.src)f.src=f.dataset.src;
  f.style.display='block';
  $('panScanMsg').textContent='在下方完成网盘登录（夸克 / UC / 阿里）。';
}
async function refreshPanScan(){
  try{
    const j=await jget('/v/api/ext/pan/status');const d=j.data||{};
    const logins=d.logins||{};
    const on=Object.keys(logins).filter(k=>logins[k]&&(logins[k].logged_in||logins[k].logged)).length;
    const el=$('panScanPill');
    if(el){el.textContent=on?('已登录 '+on):'未登录';el.className='pill '+(on?'on':'off');}
    $('panScanMsg').textContent=on?('已登录 '+on+' 个网盘，网盘高清线路可自动转存。'):'未登录第三方网盘。';
  }catch(e){$('panScanMsg').textContent='读取网盘登录状态失败：'+e.message;}
  await loadPanCacheCfg();
}
async function loadPanCacheCfg(){
  try{
    const cfg=(await jget('/v/api/ext/status')).data.config||{};
    const days=parseInt(cfg.pan_cache_ttl_days||0,10);
    const enabled=cfg.pan_cache_cleanup_enabled!==false;
    const el=$('panCacheAuto');if(el)el.checked=enabled;
    const sel=$('panCacheDays');
    if(sel){if(days===3||days===7)sel.value=String(days);else sel.value='1';}
    const msg=$('panCacheMsg');
    if(msg&&!msg.textContent.trim())msg.textContent=enabled?'已开启自动清理，到期自动清除网盘播放缓存':'已关闭自动清理';
  }catch(e){}
}
// <<< PRIVATE:SCANLOGIN

/* ---------- 初始化 ---------- */
async function loadStatus(){
  STATUS=await jget("/v/api/ext/status");
  CFG=STATUS.config;
  if($("extensionEnabled"))$("extensionEnabled").checked=CFG.extension_enabled!==false;
  PRIO=STATUS.source_priority||{};
}
async function saveExtensionEnabled(){
  const control=$("extensionEnabled"),msg=$("extensionEnabledMsg"),enabled=control.checked;
  control.disabled=true;
  try{
    const r=await jpost("/v/api/ext/config",{extension_enabled:enabled});
    if(r.code!==0)throw new Error(r.msg||"保存失败");
    if(CFG)CFG.extension_enabled=enabled;
    msg.textContent=enabled?"服务已开启，正在重新载入资源站路由与榜单。":"服务已关闭，影视请求已恢复官方处理。";
    toast(enabled?"fnmedia-ext 已开启":"fnmedia-ext 已关闭");
  }catch(e){
    control.checked=!enabled;msg.textContent="设置失败："+e.message;
    toast("服务开关保存失败","fail");
  }finally{control.disabled=false;}
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
  $("posterSpeedBadge").checked=CFG.poster_speed_badge!==false;
  $("lineAutofail").checked=CFG.line_autofail!==false;
  $("fixedLineDirect").checked=CFG.fixed_line_direct!==false;
  $("midPlaybackSwitch").checked=CFG.mid_playback_switch!==false;
  $("skipIntroEnabled").checked=CFG.skip_intro_enabled===true;
  $("skipIntroSeconds").value=CFG.skip_intro_seconds||0;
  $("skipOutroEnabled").checked=CFG.skip_outro_enabled===true;
  $("skipOutroSeconds").value=CFG.skip_outro_seconds||0;
  $("fastestMax").value=CFG.line_fastest_max||6;
  $("fastestTimeout").value=CFG.line_fastest_timeout||4;
  $("folderBindWait").value=(CFG.folder_bind_wait??8);
  $("linePredict").checked=CFG.linepredict_enabled!==false;
  $("linePredictZeroRace").checked=CFG.linepredict_zero_race!==false;
  $("browseProbe").checked=CFG.browse_probe!==false;  $("linePredictMax").value=CFG.linepredict_max_lines||3;
  $("linePredictTtl").value=CFG.linepredict_ttl_min||30;
  renderKw();bindSrcDelegate();renderSources();initSrcProxy();
  const gs=$("globalState");gs.textContent="接管运行中 · 已缓存 "+STATUS.cached_titles;gs.className="dot ok";
  const ed=$("aboutEdition");if(ed)ed.textContent=(FEAT_DDUO?"完整版（本地全功能）":"公开版")+" · 在飞牛原生搜索与媒体库中注入在线影视";
  loadOnline();
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
