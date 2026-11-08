#!/usr/bin/env node
// sidecar.mjs —— drpyS 站点脚本的常驻 Node sidecar（catvod JSONL over stdin/stdout）。
//
// 启动（见 register.mjs / loader.mjs）：
//   /var/apps/nodejs_v24/target/bin/node --import ./register.mjs sidecar.mjs
//
// stdin 每行一个 JSON 请求：
//   {"id":"1","engine":"drpy",
//    "site":{"api":"<drpy2.min.js URL 或本地路径>","ext":"<规则脚本 URL 或本地路径或脚本文本>",...},
//    "method":"home|homeVod|category|detail|search|play",
//    "args":[...]}
// stdout 每行一个 JSON 响应：
//   {"id":"1","result":<catvod JSON>} 或 {"id":"1","error":"..."}
// 所有日志写 stderr。
//
// 同一 site（api + ext 做 key）只拉取一次规则文本并缓存；同一进程可顺序服务多个站点，
// 切换站点时用缓存的脚本文本重新 D.init()（引擎内部 rule 是模块单例）。
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

const HERE = path.dirname(fileURLToPath(import.meta.url));

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
const { syncRequest } = await import(path.join(HERE, "sync_http.mjs"));

// ---------- 选择器 ----------
const sel = await import(path.join(HERE, "selectors.mjs"));
await sel.initSelectors();

// ---------- 全局注入：必须在 import 引擎之前（引擎 top-level 引用 pdfh/pdfa/pd）----------
// 选择器需要随站点切换更新 MY_URL 基准（pd 的 urljoin）
const selectorState = { myUrl: "" };
const pdfh = sel.makePdfh();
const pdfa = sel.makePdfa();
const pd = sel.makePd(pdfh, () => selectorState.myUrl);

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

  // —— 存储：进程内 Map（local[space][key]）——
  const stores = new Map();
  function bucket(space) {
    const k = String(space || "_");
    if (!stores.has(k)) stores.set(k, new Map());
    return stores.get(k);
  }
  g.local = {
    get(space, key) { const b = bucket(space); return b.has(key) ? b.get(key) : ""; },
    set(space, key, val) { bucket(space).set(key, String(val)); },
    delete(space, key) { bucket(space).delete(key); },
    getOption(key) { return bucket("option").get(key) || ""; },
    setOption(key, val) { bucket("option").set(key, String(val)); },
  };
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
injectGlobals();

// ---------- 加载引擎（loader.mjs 负责 assets:// 与 UMD 包装、cheerio 挂全局）----------
const engineUrl = path.join(HERE, "drpy2.min.js");
let D;
try {
  ({ default: D } = await import(engineUrl));
} catch (e) {
  elog("engine load failed:", e && e.stack || e);
  process.exit(1);
}
elog("engine loaded; exports:", Object.keys(D).join(","));

// ---------- 站点缓存 ----------
// key -> { key, api, ext, ruleText, initialized:boolean }
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
    rec = { key, api: site.api || drpy2Default(), ext: site.ext, ruleText };
    sites.set(key, rec);
  }
  // 引擎 rule 为模块单例：切换站点或首次使用时重新 init（脚本文本分支，不再走网络）
  if (activeKey !== key) {
    selectorState.myUrl = "";
    await D.init(rec.ruleText);
    activeKey = key;
    elog("site activated:", key.slice(0, 120));
  }
  return rec;
}

// ---------- 方法分发 ----------
async function callMethod(method, args) {
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

function normalizeResult(result) {
  if (typeof result === "string") {
    const s = result.trim();
    if (!s) return {};
    try { return JSON.parse(s); } catch { return { raw: result }; }
  }
  if (result == null) return {};
  return result;
}

// MY_URL 在引擎里是模块级变量，宿主无法直接读；在每次请求后从缓存角度不重要——
// pd 的 urljoin 基准只影响选择器取相对链接，用站点 host 兜底。
function patchSelectorBase() {
  try {
    const rule = D.getRule ? D.getRule() : null;
    if (rule && rule.host && !selectorState.myUrl) selectorState.myUrl = rule.host;
  } catch {}
}

async function handleRequest(req) {
  const site = req.site || {};
  if (!site.ext && site.ext !== "") throw new Error("site.ext 必填（规则脚本 URL/路径/文本）");
  await ensureSite(site);
  patchSelectorBase();
  const t0 = Date.now();
  const raw = await callMethod(req.method, Array.isArray(req.args) ? req.args : []);
  elog(req.method, "ok", Date.now() - t0, "ms");
  return normalizeResult(raw);
}

// ---------- JSONL main loop ----------
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
let inflight = 0;
let stdinClosed = false;

// 引擎内部 rule/MY_URL/fetch_params 等都是模块级可变单例，切站后必须保证
// “init + 方法调用”原子执行：所有请求严格串行（JSONL 顺序即响应顺序）。
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

elog("drpy sidecar ready (node", process.version + ")");
