#!/usr/bin/env node
// sidecar.mjs —— drpyS 站点脚本的常驻 Node sidecar（catvod JSONL over stdin/stdout）。
//
// 启动（见 register.mjs / loader.mjs）：
//   /var/apps/nodejs_v24/target/bin/node --import ./register.mjs sidecar.mjs
//
// stdin 每行一个 JSON 请求：
//   {"id":"1","engine":"drpy",
//    "site":{"api":"<引擎文件或 drpy:// 别名>","ext":"<规则脚本 URL 或本地路径或脚本文本>",...},
//    "method":"home|homeVod|category|detail|search|play",
//    "args":[...]}
// stdout 每行一个 JSON 响应：
//   {"id":"1","result":<catvod JSON>} 或 {"id":"1","error":"..."}
// 所有日志写 stderr。
//
// 引擎选择（进程级，改开关需重启 sidecar）：
//   DRPY_ENGINE=drpy3（默认）—— drpy3 Runtime，每个 site 一个实例（rt.load 自动路由 load2x）；
//   DRPY_ENGINE=drpy2 —— 老路，drpy2.min.js 单例 + 切换站点时 D.init() 重置。
// drpy2.min.js 全保留作 fallback；drpy3 的 5 个 dist 文件与它并列存放。
//
// 同一 site（api + ext 做 key）只拉取一次规则文本并缓存；同一进程可顺序服务多个站点。
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";

const HERE = path.dirname(fileURLToPath(import.meta.url));

// 引擎开关：默认 drpy3；只有显式 DRPY_ENGINE=drpy2 才走老路
const ENGINE = String(process.env.DRPY_ENGINE || "drpy3").toLowerCase();
const USE_DRPY3 = ENGINE !== "drpy2";

// drpyS 宿主内置的 joinUrl：相对地址拼到基准页面，兼容协议相对、根相对、查询串等。
// 引擎里的 urljoin(base, href) 内部就是 return joinUrl(base, href)。
function joinUrl(fromPath, nowPath) {
  fromPath = String(fromPath || "");
  nowPath = String(nowPath || "");
  if (!nowPath) return fromPath;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(nowPath)) return nowPath;       // 绝对 http(s)
  if (nowPath.startsWith("//")) return fromPath.split("//")[0] + "//" + nowPath.slice(2);
  if (!fromPath) return nowPath;
  try {
    return new URL(nowPath, fromPath).href;
  } catch {
    return nowPath;
  }
}

// ---------- 日志：一律 stderr ----------
function elog(...a) {
  process.stderr.write("[sidecar] " + a.map(x => {
    try { return typeof x === "string" ? x : JSON.stringify(x); } catch { return String(x); }
  }).join(" ") + "\n");
}
// 引擎内部大量 console.log；重定向到 stderr，stdout 只保留协议行
const origConsole = { log: console.log.bind(console), error: console.error.bind(console), warn: console.warn.bind(console), info: console.info.bind(console) };
console.log = (...a) => process.stderr.write("[js] " + a.map(String).join(" ") + "\n");
console.info = console.log;
console.warn = (...a) => process.stderr.write("[js-warn] " + a.map(String).join(" ") + "\n");
console.error = (...a) => process.stderr.write("[js-err] " + a.map(String).join(" ") + "\n");

// ---------- 同步 HTTP（Atomics + 常驻 Worker），必须在引擎 import 前就绪 ----------
// drpy2 的同步 req 与 drpy3 load2x 片段的 syncReq 共用此桥；worker 线程有独立
// globalThis，不受主线程 drpy-core-lite fetch polyfill 影响。
const { syncRequest } = await import(path.join(HERE, "sync_http.mjs"));

// ---------- 选择器 ----------
const sel = await import(path.join(HERE, "selectors.mjs"));
await sel.initSelectors();

// 选择器需要随站点切换更新 MY_URL 基准（pd 的 urljoin）
const selectorState = { myUrl: "" };
const pdfh = sel.makePdfh();
const pdfa = sel.makePdfa();
const pd = sel.makePd(pdfh, () => selectorState.myUrl);

// ---------- 存储：进程内 Map（local[space][key]），两引擎共用 ----------
// drpy2 语义：local.get 缺失返回 ""（不返回调用方给的默认值）。
const stores = new Map();
function bucket(space) {
  const k = String(space || "_");
  if (!stores.has(k)) stores.set(k, new Map());
  return stores.get(k);
}
const local = {
  get(space, key) { const b = bucket(space); return b.has(key) ? b.get(key) : ""; },
  set(space, key, val) { bucket(space).set(key, String(val)); },
  delete(space, key) { bucket(space).delete(key); },
  getOption(key) { return bucket("option").get(key) || ""; },
  setOption(key, val) { bucket("option").set(key, String(val)); },
};

// drpy3 store 介质：按 (源 key, 键) 存取；缺失一律返回 "" 而不是框架传来的 def
//（drpy2 的 local.get 语义；makeStore 的 def 参数在此被忽略）。
const storeMedium = {
  get(ns, k, def) { return local.get(ns, k); },
  set(ns, k, v) { local.set(ns, k, v); return v; },
  delete(ns, k) { local.delete(ns, k); },
};

// ---------- 全局注入：仅 drpy2 路径使用（drpy3 不读 globalThis，改走 HostEnv 对象）----------
function injectGlobals() {
  const g = globalThis;
  // —— 引擎 top-level 直接引用的三个选择器 ——
  g.pdfh = pdfh;
  g.pdfa = pdfa;
  g.pd = pd;

  // —— 同步网络 ——
  g.req = (url, obj) => {
    const r = syncRequest(url, obj);
    if (r.error && process.env.DRPY_HTTP_DEBUG) elog("req", url, "->", r.status, r.error);
    return r;
  };

  // —— joinUrl：drpyS/QuickJS 宿主内置全局，引擎内部 urljoin() 直接包它 ——
  g.joinUrl = joinUrl;

  // —— 常量/UA ——
  g.VERSION = "drpy2 3.9.51beta2 (fnmedia node-sidecar)";
  g.RKEY = "";
  g.MY_URL = "";
  g.HOST = "";
  g.MOBILE_UA = "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36";

  // —— 本地代理（sidecar 不内置 HTTP 代理服务，给占位地址）——
  const PROXY_URL = "http://127.0.0.1:9978/proxy?do=js";
  g.getProxyUrl = () => PROXY_URL;
  g.getProxy = () => PROXY_URL;

  // —— 存储 ——
  g.local = local;
  // $cache：TTL 内存缓存
  const cacheStore = new Map();
  g.$cache = {
    get(key) {
      const it = cacheStore.get(key);
      if (!it) return null;
      if (it.expire && it.expire < Date.now()) { cacheStore.delete(key); return null; }
      return it.val;
    },
    set(key, val, ttl) { cacheStore.set(key, { val, expire: ttl ? Date.now() + ttl * 1000 : 0 }); },
    delete(key) { cacheStore.delete(key); },
  };

  // —— 日志 ——
  g.print = (...a) => process.stderr.write("[print] " + a.map(String).join(" ") + "\n");
  g.log = (...a) => process.stderr.write("[log] " + a.map(String).join(" ") + "\n");

  // —— 其它引擎可能 typeof 探测的宿主能力，给安全 noop/空实现 ——
  g.pdfh2 = pdfh; g.pdfa2 = pdfa; g.pd2 = pd;
  g.pdfl = undefined; // 存在则引擎自判 drpy2.1；保持 undefined 走 drpy2 分支
  g.OcrApi = { api: "", classification() { return ""; } };
  g.JSEncrypt = undefined;
}

// ---------- drpy3 异步 HTTP：node:http/https 直写，禁止用全局 fetch ----------
// drpy-core-lite 会用自带 polyfill 覆盖 globalThis.fetch，原生 http 不可被污染。
// 照抄上游 cli/node-host.mjs 的 httpRequest + req 包装（跟随重定向 ≤5 跳，redirect:0 禁止跟随）。
function rawRequest(url, { method = "GET", headers = {}, body = null, timeoutMs = 5000 }) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const mod = u.protocol === "https:" ? https : http;
    const req = mod.request(
      {
        protocol: u.protocol,
        hostname: u.hostname,
        port: u.port || (u.protocol === "https:" ? 443 : 80),
        path: u.pathname + u.search,
        method,
        headers,
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
      },
    );
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`timeout ${timeoutMs}ms`)));
    req.on("error", reject);
    if (body != null && method !== "GET" && method !== "HEAD") req.write(body);
    req.end();
  });
}

async function httpRequest(url, init = {}) {
  let current = String(url);
  let method = (init.method || "GET").toUpperCase();
  const headers = { ...(init.headers || {}) };
  let body = init.body != null ? init.body : null;
  const follow = init.follow !== false;
  for (let hop = 0; hop < 5; hop++) {
    const res = await rawRequest(current, { method, headers, body, timeoutMs: init.timeoutMs });
    if (follow && [301, 302, 303, 307, 308].includes(res.status) && res.headers.location) {
      const next = new URL(res.headers.location, current).href;
      if (res.status === 303 || ((res.status === 301 || res.status === 302) && method === "POST")) {
        method = "GET";
        body = null;
      }
      current = next;
      continue;
    }
    const flat = {};
    for (const [k, v] of Object.entries(res.headers)) flat[k] = Array.isArray(v) ? v.join(", ") : v;
    return { status: res.status, headers: flat, body: res.body };
  }
  throw new Error("too many redirects");
}

async function asyncReq(url, obj = {}) {
  const o = obj || {};
  try {
    let finalUrl = String(url);
    const method = (o.method || "GET").toUpperCase();
    const headers = { ...(o.headers || {}) };
    let body = null;
    if (o.data && method === "GET") {
      const u = new URL(finalUrl);
      for (const [k, v] of Object.entries(o.data)) u.searchParams.set(k, String(v));
      finalUrl = u.href;
    } else if (o.body != null && o.body !== "" && method !== "GET") {
      body = String(o.body);
    } else if (o.data && method !== "GET") {
      const hasCt = Object.keys(headers).some((k) => k.toLowerCase() === "content-type");
      const ctKey = Object.keys(headers).find((k) => k.toLowerCase() === "content-type") || "";
      if (String(headers[ctKey] || "").includes("json")) {
        body = JSON.stringify(o.data);
      } else {
        body = new URLSearchParams(o.data).toString();
        if (!hasCt) headers["Content-Type"] = "application/x-www-form-urlencoded";
      }
    }
    // drpy2 request() 语义：字符串 body 且未指定 Content-Type 时默认表单类型；
    // 缺了它斗鱼搜索等接口不解析 body 参数（返回“搜索词为空”）。
    if (body != null && body !== "" && method !== "GET" && method !== "HEAD"
      && !Object.keys(headers).some((k) => k.toLowerCase() === "content-type")) {
      headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8";
    }
    const res = await httpRequest(finalUrl, {
      method, headers, body,
      follow: o.redirect !== 0,
      timeoutMs: Math.min(o.timeout || 5000, 60000),
    });
    const outHeaders = { status: `HTTP/1.1 ${res.status}` };
    Object.assign(outHeaders, res.headers);
    let content;
    if (o.buffer === 1) {
      content = new Uint8Array(res.body);
    } else if (o.buffer === 2) {
      content = res.body.toString("base64");
    } else {
      const charset =
        (String(res.headers["content-type"] || "").match(/charset=([\w-]+)/i) || [])[1]
        || o.encoding || "utf-8";
      try {
        content = new TextDecoder(charset).decode(res.body);
      } catch {
        content = res.body.toString("utf8");
      }
    }
    return { content, headers: outHeaders };
  } catch (e) {
    // 出错不抛：返回 {content:'', headers:{error}}（drpy2 契约，个别源 try/catch 依赖）
    return { content: "", headers: { error: String((e && e.message) || e) } };
  }
}

// drpy3 syncReq（load2x 老源片段必需）：复用 sync_http.mjs 的 syncRequest，
// 不要用 curl 子进程版；返回 {content, headers}，status 行补进 headers.status。
function hostSyncReq(url, obj = {}) {
  const o = { ...(obj || {}) };
  // drpy2 request() 语义：字符串 body 且未指定 Content-Type 时默认表单类型
  //（斗鱼搜索等接口靠它解析 body 参数，缺了返回“搜索词为空”）。
  const method = String(o.method || "GET").toUpperCase();
  const headers = { ...(o.headers || {}) };
  if (method !== "GET" && method !== "HEAD" && typeof o.body === "string" && o.body
    && !Object.keys(headers).some((k) => k.toLowerCase() === "content-type")) {
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8";
  }
  const r = syncRequest(url, { ...o, headers });
  const resHeaders = { ...(r.headers || {}) };
  if (!resHeaders.status && r.status) resHeaders.status = `HTTP/1.1 ${r.status}`;
  if (r.error) resHeaders.error = r.error;
  return { content: r.content || "", headers: resHeaders };
}

// drpy3 pdfl（整列表批量解析）：fork 无 pdfl 宿主实现，用 pdfa 逐元素回退，
// 与框架内置回退同构（pdfa 返回节点包装对象，fork 的 pdfh/pd 能直接解析它）。
function hostPdfl(html, parse, listText, listUrl, myUrl) {
  const items = pdfa(html, parse) || [];
  const base = myUrl || selectorState.myUrl;
  return items.map((it) => `${pdfh(it, listText)}$${pd(it, listUrl, base)}`);
}

// drpy3 loadAsset：随源资产（wasm 等）相对 assets/drpy 读文件；二进制扩展名按字节读。
const BINARY_EXT = new Set([".wasm", ".ts", ".mp4", ".m4s", ".jpg", ".png", ".gif", ".webp"]);
function loadAsset(p) {
  const abs = path.isAbsolute(p) ? p : path.join(HERE, p);
  if (BINARY_EXT.has(path.extname(abs).toLowerCase())) {
    return new Uint8Array(fs.readFileSync(abs));
  }
  return fs.readFileSync(abs, "utf8");
}

const PROXY_URL = "http://127.0.0.1:9978/proxy?do=js";

// ---------- 加载引擎 ----------
let D = null;   // drpy2 单例
let rt = null;  // drpy3 Runtime（进程内一个）
if (USE_DRPY3) {
  const hostEnv = {
    engine: "node",
    version: process.version,
    req: asyncReq,
    syncReq: hostSyncReq,
    pdfh, pdfa, pd,
    pdfl: hostPdfl,
    joinUrl,
    store: storeMedium,
    log: (...a) => elog(...a),
    getProxy: () => PROXY_URL,
    loadAsset,
    env: {},
    action: false, // fork 没有 action 消费通道，初期显式关掉
  };
  let drpy3;
  try {
    drpy3 = await import(path.join(HERE, "drpy3.js"));
  } catch (e) {
    elog("drpy3 engine load failed:", e && e.stack || e);
    process.exit(1);
  }
  // 库全局（对接指南 §9.1，宿主义务）：peer 链只导出不挂 globalThis，
  // 源内裸名 CryptoJS 等必须由宿主挂上，否则加密源报 No crypto library available。
  // 只挂指南列出的 5 个：jinja/JSONPath 由各自脚本 side-effect 自挂，勿重复；勿挂 cheerio。
  try {
    const peer = await import(path.join(HERE, "drpy3-peer.js"));
    for (const name of ["CryptoJS", "JSEncrypt", "pako", "gbkTool", "模板"]) {
      if (peer[name] !== undefined && globalThis[name] === undefined) globalThis[name] = peer[name];
    }
  } catch (e) {
    elog("drpy3 peer lib globals mount failed:", e && e.message || e);
  }
  rt = new drpy3.Runtime(hostEnv);
  drpy3.exposeRuntimeGlobals(rt); // 裸名工具面（md5/CryptoJS 等），Runtime 建好后调一次
  elog("drpy3 engine loaded; capabilities:", JSON.stringify(rt.capabilities));
} else {
  injectGlobals();
  // 加载引擎（loader.mjs 负责 assets:// 与 UMD 包装、cheerio 挂全局）
  const engineUrl = path.join(HERE, "drpy2.min.js");
  try {
    ({ default: D } = await import(engineUrl));
  } catch (e) {
    elog("engine load failed:", e && e.stack || e);
    process.exit(1);
  }
  elog("engine loaded; exports:", Object.keys(D).join(","));
}

// ---------- 站点缓存 ----------
// drpy3：key -> { key, api, ext, src }（每站一个 Runtime 实例，load 后 init 一次）
// drpy2：key -> { key, api, ext, ruleText }（引擎 rule 为模块单例，切换站点时 D.init()）
const sites = new Map();

function isHttpUrl(s) { return /^https?:\/\//i.test(String(s || "")); }
function isFileUrl(s) { return /^file:\/\//.test(String(s || "")); }

function readLocalPath(p) {
  let fp = p;
  if (isFileUrl(p)) fp = fileURLToPath(p);
  if (!path.isAbsolute(fp)) fp = path.join(HERE, fp);
  return fs.readFileSync(fp, "utf8");
}

// 取规则脚本文本：http(s) URL -> 同步拉取（UA Mozilla，失败重试 2 次）；本地路径 -> 读文件；
// 其余一律视为脚本文本本身。
function fetchRuleText(ext) {
  if (isHttpUrl(ext)) {
    let lastErr = "";
    for (let i = 0; i < 3; i++) {
      const r = syncRequest(ext, {
        method: "GET",
        headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" },
        timeout: 15000,
      });
      if (r.status >= 200 && r.status < 300 && r.content && r.content.trim()) return r.content;
      lastErr = r.error || ("status " + r.status);
      elog("fetch ext retry", i + 1, ext, lastErr);
    }
    throw new Error("拉取规则脚本失败: " + ext + " (" + lastErr + ")");
  }
  if (isFileUrl(ext)) return readLocalPath(ext);
  // 看起来像文件路径（/ ./ ../ 或以 .js 结尾）：按本地文件处理，不存在则报错；
  // 避免把拼错的路径误当成“脚本文本”静默吞掉。
  const looksLikePath = path.isAbsolute(ext) || /^\.\.?[/\\]/.test(ext) || /\.(js|txt)$/i.test(ext);
  const candidate = path.isAbsolute(ext) ? ext : path.join(HERE, ext);
  if (looksLikePath) {
    if (!fs.existsSync(candidate)) throw new Error("规则脚本本地文件不存在: " + ext);
    return fs.readFileSync(candidate, "utf8");
  }
  // 否则当脚本文本（内嵌规则，通常含 "var rule"/中文键）
  return ext;
}

function siteKey(site) {
  return String(site.api || drpy2Default()) + "::" + String(site.ext || "");
}
function drpy2Default() { return path.join(HERE, "drpy2.min.js"); }

let activeKey = null;

async function ensureSite(site) {
  const key = siteKey(site);
  let rec = sites.get(key);
  if (!rec) {
    const ruleText = fetchRuleText(site.ext);
    if (USE_DRPY3) {
      // rt.load 自动识别 drpy2 特征并路由 load2x；init 只在首次 load 后调一次
      const src = await rt.load(ruleText, { key });
      await src.init("");
      rec = { key, api: site.api || "", ext: site.ext, src };
    } else {
      rec = { key, api: site.api || drpy2Default(), ext: site.ext, ruleText };
    }
    sites.set(key, rec);
  }
  // drpy2：引擎 rule 为模块单例，切换站点或首次使用时重新 init（脚本文本分支，不再走网络）。
  // drpy3：每站独立实例，切换无需重 init。
  if (activeKey !== key) {
    selectorState.myUrl = "";
    if (!USE_DRPY3) {
      await D.init(rec.ruleText);
    }
    activeKey = key;
    elog("site activated:", key.slice(0, 120));
  }
  return rec;
}

// ---------- 方法分发 ----------
async function callMethod(method, args, rec) {
  if (!USE_DRPY3) {
    switch (method) {
      case "home":
        return await D.home(args[0] ?? {}, args[1] ?? "", args[2] ?? "");
      case "homeVod":
        return await D.homeVod(args[0]);
      case "category":
        return await D.category(args[0] ?? "1", args[1] ?? 1, args[2] ?? {}, args[3] ?? {});
      case "detail":
        return await D.detail(Array.isArray(args[0]) ? args[0][0] : args[0]);
      case "search":
        return await D.search(args[0] ?? "", args[1] ?? false, args[2] ?? 1);
      case "play":
        return await D.play(args[0] ?? "", args[1] ?? "", args[2]);
      case "proxy":
        return await D.proxy(args[0]);
      case "sniffer":
        return D.sniffer();
      case "isVideo":
        return D.isVideo(args[0]);
      default:
        throw new Error("unknown method: " + method);
    }
  }
  const src = rec.src;
  switch (method) {
    case "home":
      return await src.home(args[0] ?? "");
    case "homeVod":
      return await src.homeVod(args[0]);
    case "category":
      return await src.category(args[0] ?? "1", args[1] ?? 1, args[2] ?? false, args[3] ?? {});
    case "detail":
      return await src.detail(Array.isArray(args[0]) ? args[0][0] : args[0]);
    case "search":
      return await src.search(args[0] ?? "", args[1] ?? false, args[2] ?? 1);
    case "play":
      return await src.play(args[0] ?? "", args[1] ?? "", args[2]);
    case "proxy":
      return await src.proxy(args[0]);
    case "sniffer":
      return src.sniffer();
    case "isVideo":
      return src.isVideo(args[0]);
    default:
      throw new Error("unknown method: " + method);
  }
}

function normalizeResult(result) {
  if (typeof result === "string") {
    const s = result.trim();
    if (!s) return {};
    try { return JSON.parse(s); } catch { return { raw: result }; }
  }
  if (result == null) return {};
  return result;
}

// MY_URL 在 drpy2 里是模块级变量，宿主无法直接读；在每次请求后从缓存角度不重要——
// pd 的 urljoin 基准只影响选择器取相对链接，用站点 host 兜底。
// drpy3 同理：src.rule.host 是 init 后定稿的 host。
function patchSelectorBase(rec) {
  try {
    const rule = USE_DRPY3 ? (rec.src && rec.src.rule) : (D.getRule ? D.getRule() : null);
    if (rule && rule.host && !selectorState.myUrl) selectorState.myUrl = rule.host;
  } catch {}
}

async function handleRequest(req) {
  const site = req.site || {};
  if (!site.ext && site.ext !== "") throw new Error("site.ext 必填（规则脚本 URL/路径/文本）");
  const rec = await ensureSite(site);
  patchSelectorBase(rec);
  const t0 = Date.now();
  const raw = await callMethod(req.method, Array.isArray(req.args) ? req.args : [], rec);
  elog(req.method, "ok", Date.now() - t0, "ms");
  return normalizeResult(raw);
}

// ---------- JSONL main loop ----------
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
let inflight = 0;
let stdinClosed = false;

// 引擎内部 rule/MY_URL/fetch_params 等都是模块级可变单例，切站后必须保证
// “init + 方法调用”原子执行：所有请求严格串行（JSONL 顺序即响应顺序）。
// drpy3 下每站独立实例 + load2x 实例自带串行，JSONL 链仍保留不动。
let chain = Promise.resolve();
function enqueue(line) {
  chain = chain
    .then(() => processLine(line))
    .catch((e) => elog("queue error:", e && e.stack || e));
  return chain;
}

async function processLine(line) {
  let req;
  try {
    req = JSON.parse(line);
  } catch (e) {
    process.stdout.write(JSON.stringify({ id: null, error: "bad request json: " + e.message }) + "\n");
    return;
  }
  try {
    const result = await handleRequest(req);
    process.stdout.write(JSON.stringify({ id: req.id, result }) + "\n");
  } catch (e) {
    const msg = String((e && e.stack) || e);
    elog("method", req.method, "failed:", msg);
    process.stdout.write(JSON.stringify({ id: req.id, error: String((e && e.message) || e) }) + "\n");
  } finally {
    inflight--;
    if (stdinClosed && inflight === 0) setImmediate(() => process.exit(0));
  }
}

rl.on("line", (line) => {
  if (!line.trim()) return;
  inflight++;
  enqueue(line).catch((e) => elog("queue error:", e && e.stack || e));
});
rl.on("close", () => {
  stdinClosed = true;
  if (inflight === 0) setImmediate(() => process.exit(0));
});

process.on("uncaughtException", (e) => {
  // 消费方提前关闭管道（head/exit）时写 stdout 会 EPIPE：静默退出即可
  if (e && (e.code === "EPIPE" || e.code === "ERR_STREAM_DESTROYED" || e.code === "ERR_STREAM_PREMATURE_CLOSE")) {
    process.exit(0);
  }
  elog("uncaught:", e && e.stack || e);
});
process.on("unhandledRejection", (e) => elog("unhandledRejection:", e && (e.stack || String(e))));
// stdout 关闭时不再写
process.stdout.on("error", (e) => {
  if (e && e.code === "EPIPE") process.exit(0);
  elog("stdout error:", e && e.stack || e);
});

elog("drpy sidecar ready (node", process.version + ", engine=" + (USE_DRPY3 ? "drpy3" : "drpy2") + ")");
