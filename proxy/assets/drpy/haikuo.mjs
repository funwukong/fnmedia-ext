// haikuo.mjs —— 海阔（HikerView）小程序规则的 Node 宿主上下文（batch 1）。
//
// 目标：让海阔 JSON 规则（pages[] JS 引擎模块 + js: 解析片段）在 fnmedia-ext
// 的 Node sidecar 里跑起来。宿主 API 以官方手册为准：
//   https://docs.189.tyrantg.com/docs/hikerview/help_js.html
// 实测锚点：草榴 BT 规则（caoliu_bt_rule__1__0_b5ml.json）只用到了下述子集——
//   同步 fetch/post、getItem/setItem/clearItem、getMyVar、setResult/setError、
//   toast、copy、$.require/$.exports、MY_URL/MY_PAGE/MY_KEYWORD/MY_CLASS_URL/
//   MY_CLASS_NAME，以及规则内对 MY_URL 的赋值。
//
// 执行模型（与 drpy3 的区别）：
//   drpy3 的 Runtime 用 new Function 在主 realm 里执行规则代码、靠传参隔离；
//   这里按任务要求用 node:vm 建独立 context，只暴露宿主全局（比 drpy3 更严）。
//   规则代码拿不到 process / require / module / fs / Buffer；网络只能走宿主 fetch。
//   已知的 vm 逃逸面（宿主函数的 .constructor 链）已处理：所有宿主函数原型置空并
//   遮蔽 constructor，所有宿主对象用 null 原型，返回给规则的数组/对象/错误一律用
//   vm realm 的内建构造；宿主抛出的错误统一转成 vm realm 的 Error。
//   即便如此，Node 官方并不把 vm 当作安全边界——v1 的威胁模型是“写错的规则”，
//   不是“恶意规则”；任意 JS 上 NAS 的信任问题由 batch 2 的导入层再定。
//
// 存储语义（v1，全部进程内存、重启即失，见各函数注释）：
//   getItem/setItem/clearItem —— 按 ruleKey 隔离；getPublicItem/setPublicItem ——
//   跨规则共享；putVar/getVar —— 全局共享；putMyVar/getMyVar —— 按 ruleKey 隔离。
// 文件语义（v1，规则代码唯一的文件系统入口）：
//   saveFile/readFile/deleteFile/fileExist 只在 storeDir 之内操作，拒绝 ".." 与
//   逃出 base 的绝对路径；默认存明文（HikerView 会加密，v1 不做，见注释）。
//   java.io.File/FileOutputStream + java.lang.String 是最小兼容（batch 4，
//   仅写改写后的 m3u8 到 storeDir 再返回 file:// 路径；Python 侧再校验一次）。
//
// 本文件只做宿主上下文（batch 1）：不含规则加载器、不含非 JS 解析串解释器、
// 不含 lazy 评估器（batch 2）；sidecar.mjs 里只有一个最小 engine:"haikuo"
// eval 分支供测试与 batch 2 接线。
import vm from "node:vm";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import crypto from "node:crypto";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { pathToFileURL } from "node:url";

import { syncRequest } from "./sync_http.mjs";
import { initSelectors, makePdfh, makePd } from "./selectors.mjs";
import { gbkTool } from "./gbk.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const __require = createRequire(import.meta.url);
// 宿主侧 CryptoJS（UMD，require 走 module.exports 分支；只在宿主 realm 用，
// 规则代码想用 CryptoJS 需 eval(getCryptoJS())，与手册一致）。
const CryptoJS = __require("./node_vendor/crypto-js.js");

// 选择器与 cheerio 必须先初始化（sidecar.mjs 里也会调一次，重复调无害）。
await initSelectors();
const cheerioMod = await import(pathToFileURL(path.join(HERE, "node_vendor", "cheerio.min.js")).href);
const cheerioLoad = cheerioMod.load;
const _gbk = gbkTool();

// ---------- 常量（小阈值就地写死并注释；无新配置键） ----------
export const HAIKUO_VERSION = "haikuo-v1 (fnmedia node-sidecar)";
const FETCH_DEFAULT_TIMEOUT_MS = 10000; // 手册：fetch 默认 10 秒超时
const FETCH_MAX_TIMEOUT_MS = 30000;     // 规则自带的 fetch timeout 封顶 30 秒：
                                        // 同步桥在 Atomics.wait 里等 worker，vm 60 秒超时打断不了
                                        // 正在等待的桥调用；封顶后单次请求最长约 40 秒（worker
                                        // 超时 + 10 秒桥缓冲），总执行仍受 vm 60 秒与 sidecar
                                        // 90 秒两层上限约束，慢规则拖不垮 sidecar。
const RUN_TIMEOUT_MS = 60000;           // 单段规则 JS 在 vm 内的执行上限
const MAX_BATCH = 16;                   // 手册：batchFetch 超过 16 自动分批
const MOBILE_UA = "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36";
const PC_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
const V1_UNSUPPORTED = "haikuo v1 unsupported";

// ---------- 进程级存储（重启即失；见文件头） ----------
const _itemStores = new Map();   // ruleKey -> Map(key -> string)
const _myVarStores = new Map();  // ruleKey -> Map(key -> any)
const _publicStore = new Map();  // 跨规则共享 Map(key -> string)
const _globalVars = new Map();   // 跨规则共享 Map(key -> any)
const _requireCache = new Map(); // md5(url) -> { code, expire }（远程 require 缓存）

// ---------- HOST_API_COVERAGE：函数名 -> full / stub / unsupported ----------
// full = 已实现；stub = 无 UI/无真机能力，返回固定值或 no-op（命中时记 debug 日志）；
// unsupported = v1 明确不支持，调用即抛错（错误信息含 "haikuo v1 unsupported"）。
export const HOST_API_COVERAGE = {
  // 网络
  fetch: "full", request: "full", post: "full", fetchPC: "full", postPC: "full",
  getResCode: "full", batchFetch: "full", bf: "full", fetchCookie: "full",
  buildUrl: "full", fetchCache: "full", requireCache: "full", deleteCache: "full",
  // 注意：裸 require 不暴露（沙箱要求：不暴露 Node 的 require；HikerView 的远程
  // 代码块 require 见 requirejs / $.require，v1 不提供裸 require 全局）。
  require: "unsupported", requirejs: "full", downloadFile: "full", requireDownload: "full",
  saveImage: "full", getPath: "full", http: "full",
  // 解析
  parseDom: "full", parseDomForHtml: "full", parseDomForArray: "full",
  pd: "full", pdfh: "full", pdfa: "full", xpath: "full", xpathArray: "full", xpa: "full",
  // 结果 / 报错
  setResult: "full", setHomeResult: "full", setSearchResult: "full",
  setError: "full", error: "full",
  // 私有存储（手册称 clearItem 已移除，但现行规则仍在用，v1 保留）
  getItem: "full", setItem: "full", putItem: "full", clearItem: "full",
  getPublicItem: "full", setPublicItem: "full", clearPublicItem: "full",
  putVar: "full", getVar: "full", clearVar: "full",
  putMyVar: "full", getMyVar: "full", clearMyVar: "full", listMyVarKeys: "full",
  storage0: "full",
  // 提示 / 日志 / 剪贴板（无真机 UI，记录在 state）
  toast: "full", log: "full", print: "full", console: "full", copy: "stub",
  // $ 工具
  $: "full", "$.require": "full", "$.exports": "full", "$.toString": "full",
  "$.rule": "full", "$.lazyRule": "full", "$.x5Rule": "full", "$.input": "full",
  "$.image": "full",
  // 注入变量
  MY_URL: "full", MY_HOME: "full", MY_RULE: "full", MY_PAGE: "full",
  MY_TYPE: "full", MY_PARAMS: "full", MY_KEYWORD: "full",
  MY_CLASS_URL: "full", MY_CLASS_NAME: "full",
  MOBILE_UA: "full", PC_UA: "full", VERSION: "full", RKEY: "full", HOST: "full",
  // 编解码 / 加解密（aes/rsa 与 HikerView Java 实现的互通未验证，见注释）
  md5: "full", base64Encode: "full", base64Decode: "full", btoa: "full", atob: "full",
  aesEncode: "full", aesDecode: "full", rsaEncrypt: "full", rsaDecrypt: "full",
  decodeStr: "full", encodeStr: "full", getCryptoJS: "full",
  hexToBytes: "full", hexToBase64: "full", writeHexFile: "full", rc4: "full",
  window0: "full",
  // 搜索模式
  getSearchMode: "full", setSearchMode: "full", searchContains: "full",
  // 文件（仅 storeDir 内）
  saveFile: "full", readFile: "full", deleteFile: "full", fileExist: "full",
  writeFile: "full",
  // 规则 / 页面
  getParam: "full", getRule: "full", getHome: "full", setLastChapterRule: "full",
  setTimeout: "full", setInterval: "full", clearTimeout: "full", clearInterval: "full",
  // UI 相关：无真机界面，stub
  confirm: "stub", updateItem: "stub", deleteItem: "stub", deleteItemByCls: "stub",
  addItemAfter: "stub", addItemBefore: "stub", findItem: "stub", findItemsByCls: "stub",
  back: "stub", refreshPage: "stub", refreshX5WebView: "stub",
  setPageTitle: "stub", getPageTitle: "stub", setPagePicUrl: "stub",
  setPageParams: "stub", showLoading: "stub", hideLoading: "stub",
  addListener: "stub", getHomeSub: "stub", hasHomeSub: "stub", getLastRules: "stub",
  getRuleCount: "stub", getColTypes: "stub", getAppVersion: "stub", getIP: "stub",
  getCpuAbi: "stub", registerDNS: "stub", ipping: "stub", findReachableIP: "stub",
  registerTask: "stub", unRegisterTask: "stub", closeMe: "stub",
  // v1 明确不支持：调用即抛错
  loadJavaClass: "unsupported", fetchCodeByWebView: "unsupported",
  cacheM3u8: "unsupported", clearM3u8Ad: "unsupported", clearM3u8AdLazy: "unsupported",
  cacheM3u8WithPngProxy: "unsupported", convertM3u8WithPngProxy: "unsupported",
  startProxyServer: "unsupported", evalPrivateJS: "unsupported",
  getPastes: "unsupported", sharePaste: "unsupported", parsePaste: "unsupported",
};

export const HOST_API_NOTES = {
  clearItem: "手册称已移除，但草榴 BT 等现行规则仍在用，v1 保留为按 ruleKey 隔离的删除。",
  aesEncode: "CryptoJS 口令模式 AES（Salted__），与 HikerView Java 实现的互通未验证。",
  rsaEncrypt: "Node crypto RSA_PKCS1_PADDING；type 1 公钥加密 / 2 私钥加密，long=2 分段。",
  setLastChapterRule: "只记入 state.lastChapterRule 作元数据；v1 无收藏/二级页可应用。",
  "$.x5Rule": "只做 URL 拼串（javascript: 链接）；X5/WebView 执行走 fetchCodeByWebView，v1 不支持。",
  "$.rule": "只做 URL 拼串（@rule=.js:…）；解析执行是 batch 2 的 lazy/parse 解释器负责。",
  "$.lazyRule": "只做 URL 拼串（@lazyRule=.js:…）；解析执行是 batch 2 负责。",
  "$.input": "只做 URL 拼串（@input=.js:…）；输入弹窗本身无真机 UI，格式为最佳推断。",
  "$.image": "只做 URL 拼串（@headers={}@js=…）；图片解密执行是 batch 2 负责。",
  fetch: "同步语义（HikerView 的 fetch 直接返回字符串）；toHex/GBK 等见代码注释里的限制。",
  getItem: "进程内存、按 ruleKey 隔离、重启即失；缺失返回默认值（默认 \"\"）。",
  putItem: "setItem 的别名（HikerView 写法）；四条 API 规则的 preRule 与令牌缓存都在用它写 host/token，batch 1 漏了它，batch 2 补上。",
  getPublicItem: "进程内存、跨规则共享、重启即失；请勿存私密内容（与手册一致）。",
  saveFile: "只在 storeDir 内操作；v1 明文存放（HikerView 会加密），规则删除不自动清理。",
  parsePaste: "云剪贴板服务端点尚未确认，batch 2 导入层实现时再接。",
  xpath: "XPath 子集（// 与 /、标签、[@attr='v']、[n]、/@attr、/text()），非完整 XPath。",
  pdfa: "返回外层 HTML 字符串数组（与手册一致）；drpy 的 pdfa 返回节点包装，两者不同；'div&&a' 按 HikerView 语义取全部 a（不用 drpy 的末段属性误判）。",
  require: "沙箱要求不暴露 Node require；v1 无裸 require 全局，远程代码块走 requirejs/$.require。",
};

// ---------- 小工具 ----------
// 把宿主函数“钉死”：遮蔽 constructor、原型置空，堵住经 .constructor 拿宿主
// Function 再 Function("return process")() 的逃逸链。只对本文件自建的闭包用，
// 绝不改共享内建（如全局 setTimeout）——共享的先包一层再钉。
function hardenFn(fn) {
  try {
    Object.defineProperty(fn, "constructor", {
      value: undefined, writable: false, enumerable: false, configurable: false,
    });
  } catch { /* 忽略：钉不住的函数调用方仍可用，只是少一层防护 */ }
  try { Object.setPrototypeOf(fn, null); } catch { /* 同上 */ }
  return fn;
}

function nullObj() { return Object.create(null); }

function md5hex(s) {
  return crypto.createHash("md5").update(String(s), "utf8").digest("hex");
}

function stripJsPrefix(code) {
  let c = String(code ?? "");
  if (c.charCodeAt(0) === 0xfeff) c = c.slice(1);
  return c.replace(/^\s*js:/i, "");
}

// RC4（手册 rc4.encode/decode；encode 返回 hex，decode 接受 hex）
function rc4Crypt(dataBuf, keyBuf) {
  const s = new Uint8Array(256);
  for (let i = 0; i < 256; i++) s[i] = i;
  let j = 0;
  for (let i = 0; i < 256; i++) {
    j = (j + s[i] + keyBuf[i % keyBuf.length]) & 0xff;
    const t = s[i]; s[i] = s[j]; s[j] = t;
  }
  const out = Buffer.alloc(dataBuf.length);
  let a = 0; j = 0;
  for (let n = 0; n < dataBuf.length; n++) {
    a = (a + 1) & 0xff;
    j = (j + s[a]) & 0xff;
    const t = s[a]; s[a] = s[j]; s[j] = t;
    out[n] = dataBuf[n] ^ s[(s[a] + s[j]) & 0xff];
  }
  return out;
}

// ---------- XPath 子集 ----------
// 支持：// 与 /、标签名、*、[@attr='v']、[@attr]、[n]、[last()]、末尾 /@attr 与
// /text()。// 加 [n] 时按文档顺序取第 n 个（与 XPath“每父节点下第 n 个”的语义
// 有出入，见注释）；元素本身的 string-value 按 XPath 规范取全部后代文本。
function parseXPathStep(raw) {
  if (raw.startsWith("@")) return { kind: "attr", name: raw.slice(1) };
  if (raw === "text()") return { kind: "text" };
  const m = /^(\*|[A-Za-z_][\w:.-]*)(?:\[([^\]]*)\])?$/.exec(raw);
  if (!m) throw new Error("haikuo v1: unsupported xpath step: " + raw);
  const pred = (m[2] || "").trim();
  const step = { kind: "elem", tag: m[1], attr: null, attrVal: null, index: null, last: false };
  if (pred) {
    const am = /^@([A-Za-z_][\w:.-]*)(?:=(['"])(.*?)\2)?$/.exec(pred);
    if (am) { step.attr = am[1]; step.attrVal = am[3] !== undefined ? am[3] : null; }
    else if (/^\d+$/.test(pred)) step.index = parseInt(pred, 10);
    else if (pred === "last()") step.last = true;
    else throw new Error("haikuo v1: unsupported xpath predicate: [" + pred + "]");
  }
  return step;
}

function parseXPath(expr) {
  const steps = [];
  let i = 0;
  const s = String(expr || "").trim();
  while (i < s.length) {
    let axis = "/";
    if (s.startsWith("//", i)) { axis = "//"; i += 2; }
    else if (s[i] === "/") { i += 1; }
    else if (i === 0) { axis = "//"; }
    else throw new Error("haikuo v1: bad xpath: " + expr);
    let j = i;
    while (j < s.length && s[j] !== "/") j++;
    const raw = s.slice(i, j).trim();
    if (raw) steps.push({ axis, ...parseXPathStep(raw) });
    i = j;
  }
  if (!steps.length) throw new Error("haikuo v1: empty xpath");
  return steps;
}

function xpathEval(html, expr, firstOnly) {
  const $ = cheerioLoad(typeof html === "string" ? html : String(html ?? ""));
  const steps = parseXPath(expr);
  const last = steps[steps.length - 1];
  const valueStep = last.kind === "attr" || last.kind === "text";
  const elemSteps = valueStep ? steps.slice(0, -1) : steps;
  if (!elemSteps.length) throw new Error("haikuo v1: xpath needs an element step: " + expr);
  let nodes = [$.root()[0]];
  for (const st of elemSteps) {
    if (st.kind !== "elem") throw new Error("haikuo v1: xpath value step must be last: " + expr);
    let pool = [];
    for (const n of nodes) {
      const cands = st.axis === "//"
        ? $(n).find("*").toArray()
        : $(n).children().toArray();
      for (const c of cands) {
        if (st.tag !== "*" && String(c.tagName || "").toLowerCase() !== st.tag.toLowerCase()) continue;
        if (st.attr) {
          const v = $(c).attr(st.attr);
          if (v === undefined) continue;
          if (st.attrVal !== null && String(v) !== st.attrVal) continue;
        }
        pool.push(c);
      }
    }
    // [n]：子轴按父节点内顺序（逐父取第 n 个）；后代轴按文档顺序取第 n 个（近似）。
    if (st.index !== null || st.last) {
      if (st.axis === "/") {
        const kept = [];
        const byParent = new Map();
        for (const c of pool) {
          const p = c.parent || null;
          if (!byParent.has(p)) byParent.set(p, []);
          byParent.get(p).push(c);
        }
        for (const arr of byParent.values()) {
          kept.push(st.last ? arr[arr.length - 1] : arr[st.index - 1]);
        }
        pool = kept.filter(Boolean);
      } else {
        pool = st.last ? (pool.length ? [pool[pool.length - 1]] : []) : (pool[st.index - 1] ? [pool[st.index - 1]] : []);
      }
    }
    nodes = pool;
    if (!nodes.length) break;
  }
  const toValue = (n) => {
    if (last.kind === "attr") {
      const v = $(n).attr(last.name);
      return v === undefined ? "" : String(v);
    }
    if (last.kind === "text") return $(n).text();
    return $(n).text(); // 元素本身：XPath string-value = 全部后代文本
  };
  if (firstOnly) return nodes.length ? toValue(nodes[0]) : "";
  return nodes.map(toValue);
}

const _stateStores = new Map();
// ---------- 宿主上下文 ----------
// createHaikuoContext(ruleKey, opts)
//   opts: { storeDir, ua, logger, rule, myUrl, myPage, myType, myParams,
//           myKeyword, myClassUrl, myClassName, myHome, resCode, syncReq }
//   syncReq 可注入（测试用）；默认用 sync_http.mjs 的 syncRequest。
// 返回 { ruleKey, storeDir, sandbox, context, run, registerModule,
//        requireModule, listModules, setPage, getPage, state,
//        HOST_API_COVERAGE }。
//   sandbox 即 globals map（vm 全局对象本身）；run(code, filename) 在其中执行
//   JS 并返回结果（Promise 则原样返回，由调用方 await）。
export function createHaikuoContext(ruleKey, opts = {}) {
  const key = String(ruleKey || "haikuo");
  const logger = typeof opts.logger === "function"
    ? opts.logger
    : (...a) => process.stderr.write("[haikuo] " + a.map(String).join(" ") + "\n");
  const debug = (...a) => logger("[debug]", ...a);
  const storeDir = path.resolve(String(opts.storeDir || path.join(os.tmpdir(), "fnmedia-haikuo")));
  fs.mkdirSync(storeDir, { recursive: true });
  const syncReqImpl = typeof opts.syncReq === "function" ? opts.syncReq : syncRequest;
  const ua = String(opts.ua || MOBILE_UA);

  // vm realm 内建（createContext 后才有值；宿主函数在调用时才读，闭包即可）。
  let VArray; let VObject; let VError; let VUint8Array;
  const vArr = (items) => {
    const a = new VArray();
    for (const it of items || []) a.push(it);
    return a;
  };
  const stateStore = () => {
    if (!_stateStores.has(key)) _stateStores.set(key, {});
    return _stateStores.get(key);
  };
  const vErr = (msg) => new VError(String(msg));
  const unsupported = (name) => {
    throw vErr(`${V1_UNSUPPORTED}: ${name} (X5/WebView / Java / 云剪贴板等能力不在 v1 范围)`);
  };

  // ---------- 状态（宿主侧，不直接暴露给规则代码） ----------
  const state = {
    ruleKey: key,
    playContext: {},
    resolvePlay: {},
    myUrl: String(opts.myUrl || ""),
    myPage: Number(opts.myPage || 1),
    myType: String(opts.myType || ""),
    myParams: opts.myParams || {},
    myKeyword: String(opts.myKeyword || ""),
    myClassUrl: String(opts.myClassUrl || ""),
    myClassName: String(opts.myClassName || ""),
    myHome: String(opts.myHome || ""),
    result: null,
    resultKind: "",
    error: "",
    _cookie: "",
    lastBody: String(opts.resCode || ""),
    lastFetch: null,
    lastFetchUrl: "", // 最近一次成功 fetch 的 http(s) 终址（重定向后）：pd 的补全基准
    lastChapterRule: "",
    searchMode: 0,
    toasts: [],
    clipboard: "",
    pageTitle: "",
    pagePicUrl: "",
    pageParams: {},
    listeners: [],
    tasks: [],
    dns: {},
  };

  // ---------- 存储 ----------
  const itemStore = () => {
    if (!_itemStores.has(key)) _itemStores.set(key, new Map());
    return _itemStores.get(key);
  };
  const myVarStore = () => {
    if (!_myVarStores.has(key)) _myVarStores.set(key, new Map());
    return _myVarStores.get(key);
  };
  // value 只能是字符串的存取（setItem/getItem 系）；storage0 用 raw 版。
  const strGet = (store, k, def) => {
    const kk = String(k);
    if (store.has(kk)) return store.get(kk);
    return def === undefined ? "" : String(def);
  };
  const rawGet = (store, k, def) => (store.has(String(k)) ? store.get(String(k)) : def);

  function hostGetItem(k, def) { return strGet(itemStore(), k, def); }
  function hostSetItem(k, v) { const val = String(v ?? ""); itemStore().set(String(k), val); return val; }
  function hostClearItem(k) { itemStore().delete(String(k)); }
  function hostGetPublicItem(k, def) { return strGet(_publicStore, k, def); }
  function hostSetPublicItem(k, v) { const val = String(v ?? ""); _publicStore.set(String(k), val); return val; }
  function hostClearPublicItem(k) { _publicStore.delete(String(k)); }
  function hostPutVar(k, v) { _globalVars.set(String(k), v); return v; }
  function hostGetVar(k, def) { return rawGet(_globalVars, k, def); }
  function hostClearVar(k) { _globalVars.delete(String(k)); }
  function hostPutMyVar(k, v) { myVarStore().set(String(k), v); return v; }
  function hostGetMyVar(k, def) { return rawGet(myVarStore(), k, def); }
  function hostClearMyVar(k) { myVarStore().delete(String(k)); }
  function hostListMyVarKeys() { return vArr([...myVarStore().keys()]); }

  const storage0 = nullObj();
  Object.assign(storage0, {
    putVar: hostPutVar, getVar: hostGetVar, clearVar: hostClearVar,
    putMyVar: hostPutMyVar, getMyVar: hostGetMyVar, clearMyVar: hostClearMyVar,
    listMyVarKeys: hostListMyVarKeys,
    setItem(k, v) { itemStore().set(String(k), v); return v; },
    getItem(k, def) { return rawGet(itemStore(), k, def); },
    clearItem: hostClearItem,
    setPublicItem(k, v) { _publicStore.set(String(k), v); return v; },
    getPublicItem(k, def) { return rawGet(_publicStore, k, def); },
    clearPublicItem: hostClearPublicItem,
  });
  for (const f of Object.values(storage0)) if (typeof f === "function") hardenFn(f);

  // ---------- 文件（规则代码唯一的文件系统入口：仅 storeDir 内） ----------
  function resolveStorePath(name) {
    let rel = String(name || "");
    if (rel.startsWith("hiker://files/")) rel = rel.slice("hiker://files/".length);
    else if (rel.startsWith("file://")) rel = rel.slice("file://".length);
    else if (/^hiker:\/\//.test(rel)) throw vErr("haikuo v1: unsupported hiker:// path: " + rel);
    // 绝对路径一律拒绝（哪怕拼出来仍在 base 内也不静默改写，避免歧义）。
    if (rel.startsWith("/") || rel.startsWith("\\") || /^[A-Za-z]:[\\/]/.test(rel)) {
      throw vErr("haikuo v1: absolute path rejected: " + String(name));
    }
    const parts = rel.split(/[\\/]+/).filter(Boolean);
    if (!parts.length || parts.includes("..")) {
      throw vErr("haikuo v1: path traversal rejected: " + String(name));
    }
    const abs = path.resolve(storeDir, ...parts);
    if (abs !== storeDir && !abs.startsWith(storeDir + path.sep)) {
      throw vErr("haikuo v1: path escapes storeDir: " + String(name));
    }
    return abs;
  }
  function hostSaveFile(name, content) {
    const abs = resolveStorePath(name);
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    // v1 明文存放：HikerView 会自动加密，sidecar 无此能力，见文件头。
    fs.writeFileSync(abs, String(content ?? ""), "utf8");
    return true;
  }
  function hostReadFile(name) {
    const abs = resolveStorePath(name);
    try { return fs.readFileSync(abs, "utf8"); } catch { return ""; }
  }
  function hostDeleteFile(name) {
    const abs = resolveStorePath(name);
    try { fs.unlinkSync(abs); return true; } catch { return false; }
  }
  function hostFileExist(name) {
    try { return fs.existsSync(resolveStorePath(name)); } catch { return false; }
  }
  function hostGetPath(p) {
    return "file://" + resolveStorePath(p);
  }

  // ---------- java.io 最小兼容（HikerView Android 互操作，batch 4） ----------
  // 香蕉视频等规则用 java.io.File / FileOutputStream + java.lang.String
  // 把改写后的 m3u8 写到本地、再返回 file:// 路径；vm 里没有 java，须由宿主补。
  // 只补这三个类（File 读/建目录、FileOutputStream 写、String.getBytes/toString），
  // 其余 java.* 一律没有。路径只认 file:// 绝对路径或 storeDir 内相对路径，
  // 逃出 storeDir 即抛错；Python 侧接手 file:// 清单时还会再校验一次
  // （见 proxy/haikuo.py local_file_for_url 与 app.py 本地清单端口）。
  // FileOutputStream 不持 fd：首次写截断、之后追加写，避免长驻 sidecar 漏 fd。
  function resolveJavaPath(p) {
    let fp = String(p ?? "");
    if (fp.startsWith("file://")) {
      try { fp = fileURLToPath(fp); }
      catch { throw vErr("haikuo v1: bad file:// URL: " + String(p)); }
    }
    if (!path.isAbsolute(fp)) fp = path.resolve(storeDir, fp);
    else fp = path.resolve(fp);
    if (fp !== storeDir && !fp.startsWith(storeDir + path.sep)) {
      throw vErr("haikuo v1: java.io path escapes storeDir: " + String(p));
    }
    return fp;
  }
  function JavaIoFile(p) {
    const target = resolveJavaPath(p);
    return {
      getPath: () => target,
      getAbsolutePath: () => target,
      exists: () => { try { return fs.existsSync(target); } catch { return false; } },
      isDirectory: () => { try { return fs.statSync(target).isDirectory(); } catch { return false; } },
      mkdir: () => { try { fs.mkdirSync(target); return true; } catch { return false; } },
      mkdirs: () => { try { fs.mkdirSync(target, { recursive: true }); return true; } catch { return false; } },
      delete: () => { try { fs.unlinkSync(target); return true; } catch { return false; } },
    };
  }
  function JavaIoFileOutputStream(p) {
    const target = resolveJavaPath(p);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    let closed = false;
    let first = true;
    return {
      write: (data) => {
        if (closed) throw vErr("haikuo v1: write to closed FileOutputStream");
        let buf;
        if (Buffer.isBuffer(data)) buf = data;
        else {
          try { buf = Buffer.from(data); }
          catch { buf = Buffer.from(String(data ?? ""), "utf8"); }
        }
        if (first) { fs.writeFileSync(target, buf); first = false; }
        else fs.appendFileSync(target, buf);
      },
      flush: () => {},
      close: () => { closed = true; },
    };
  }
  function JavaLangString(s) {
    const text = String(s ?? "");
    return {
      getBytes: (_enc) => Buffer.from(text, "utf8"),
      toString: () => text,
      length: text.length,
    };
  }

  // ---------- 网络（同步语义：HikerView 的 fetch 直接返回字符串） ----------
  function headersAsArrays(headers) {
    const out = nullObj();
    for (const [k, v] of Object.entries(headers || {})) out[String(k).toLowerCase()] = [String(v)];
    return out;
  }
  // 原始请求：返回 { body, headers(扁平小写), statusCode, finalUrl }。
  // 限制：sync 桥（sync_http.mjs）把响应体按 UTF-8 解码后经临时文件回传，
  // 非 UTF-8（如 GBK）页面若含非法 UTF-8 字节会被 U+FFFD 替换、无法完全还原；
  // toHex 只是对已解码文本再取 UTF-8 字节的 hex，二进制体仅 best-effort。
  function doFetchRaw(url, options = {}) {
    const u = String(url || "");
    if (!u) throw vErr("fetch: empty url");
    if (u.startsWith("hiker://files/")) {
      const content = hostReadFile(u);
      state.lastBody = content;
      state.lastFetch = { url: u, status: 200 };
      return { body: content, headers: {}, statusCode: 200, finalUrl: u };
    }
    if (u.startsWith("hiker://page/")) {
      // batch 2：fetch('hiker://page/cz') 与 request() 同义——返回该页的
      // {name, path, rule} JSON（橙子 find_rule 靠 eval(JSON.parse(fetch(
      // 'hiker://page/cz')).rule) 载入引擎模块；batch 1 只有 request 支持，
      // fetch 会直接抛 unsupported）。
      const name = u.slice("hiker://page/".length).split(/[?#]/)[0];
      const rec = findModule(name);
      if (!rec) throw vErr("fetch: unknown page: " + name);
      const body = JSON.stringify({ name: rec.name, path: rec.path, rule: rec.code });
      state.lastBody = body;
      state.lastFetch = { url: u, status: 200 };
      return { body, headers: {}, statusCode: 200, finalUrl: u };
    }
    if (/^hiker:\/\//.test(u)) throw vErr("haikuo v1: unsupported hiker:// url in fetch: " + u);
    const o = options || {};
    const method = String(o.method || "GET").toUpperCase();
    const headers = {};
    for (const [k, v] of Object.entries(o.headers || {})) headers[k] = String(v);
    if (state._cookie && !Object.keys(headers).some((k) => k.toLowerCase() === "cookie"))
      headers["Cookie"] = state._cookie;
    const ctKey = Object.keys(headers).find((k) => k.toLowerCase() === "content-type");
    let body = null;
    if (o.body != null && method !== "GET" && method !== "HEAD") {
      if (typeof o.body === "object") {
        // 手册：fetch 的 object body 自动按 JSON 发（post 则按表单，见 hostPost）。
        body = JSON.stringify(o.body);
        if (!ctKey) headers["Content-Type"] = "application/json";
      } else {
        body = String(o.body);
      }
    }
    const rawTimeout = o.timeout != null ? Number(o.timeout) : FETCH_DEFAULT_TIMEOUT_MS;
    const timeoutMs = Number.isFinite(rawTimeout) && rawTimeout > 0
      ? Math.min(rawTimeout, FETCH_MAX_TIMEOUT_MS) : FETCH_DEFAULT_TIMEOUT_MS;
    if (o.inputStream) unsupported("fetch {inputStream:true}");
    const r = syncReqImpl(u, {
      method, headers, body,
      timeout: timeoutMs,
      redirect: o.redirect === false ? 0 : undefined,
      // 二进制/hex 请求必须透传到 worker，否则 worker 只会做 UTF-8 解码，
      // 密文与原始字节在到达规则前就已损坏（toHex 曾经因此永远拿不到 hex）。
      toHex: o.toHex === true,
      responseType: o.responseType === "bytes" ? "bytes" : undefined,
    });
    if (r.error || !r.status) {
      throw vErr("fetch failed: " + u + " (" + (r.error || ("status " + r.status)) + ")");
    }
    const res = {
      body: String(r.content ?? ""),
      bodyBase64: String(r.contentBase64 || ""),
      bodyHex: String(r.contentHex || ""),
      headers: r.headers || {},
      statusCode: Number(r.status),
      finalUrl: String(r.url || u),
    };
    state.lastBody = res.body;
    state.lastFetch = { url: u, status: res.statusCode };
    state.lastFetchUrl = res.finalUrl;
    return res;
  }
  function shapeFetch(res, o) {
    if (o.toHex) return String(res.bodyHex || res.contentHex || "");
    if (o.responseType === "bytes" || o.bytes === true) {
      const buf = res.bodyBase64
        ? Buffer.from(res.bodyBase64, "base64")
        : Buffer.from(res.body || "", "utf8");
      return buf;
    }
    if (o.onlyHeaders) return JSON.stringify({ headers: headersAsArrays(res.headers) });
    if (o.withHeaders || o.withStatusCode) {
      const out = { body: res.body, headers: headersAsArrays(res.headers) };
      if (o.withStatusCode) out.statusCode = res.statusCode;
      return JSON.stringify(out); // 手册：带 headers/statusCode 时返回 JSON 字符串
    }
    return res.body;
  }
  function hostFetch(url, options = {}) {
    const o = options || {};
    return shapeFetch(doFetchRaw(url, o), o);
  }
  function hostPost(url, bodyOrOpts, maybeOpts) {
    let opts = {};
    let body;
    if (maybeOpts !== undefined) { body = bodyOrOpts; opts = maybeOpts || {}; }
    else if (bodyOrOpts && typeof bodyOrOpts === "object") {
      // 两义：{body:{...}, headers...} 是 options；{a:'xx'} 无 body/headers
      // 等选项键时按 body 本身算（比手册更宽松，两种写法都支持）。
      const looksLikeOpts = bodyOrOpts.body !== undefined || bodyOrOpts.headers !== undefined
        || bodyOrOpts.method !== undefined || bodyOrOpts.timeout !== undefined
        || bodyOrOpts.withHeaders !== undefined || bodyOrOpts.withStatusCode !== undefined;
      if (looksLikeOpts) { opts = bodyOrOpts; body = opts.body; }
      else { body = bodyOrOpts; }
    }
    else { body = bodyOrOpts; }
    const o = { ...(opts || {}), method: "POST" };
    if (body !== undefined) {
      if (typeof body === "object") {
        // 手册：post 的 object body 自动转表单 a=xx&b=1。
        o.body = new URLSearchParams(Object.entries(body).map(([k, v]) => [k, String(v)])).toString();
        if (!Object.keys(o.headers || {}).some((k) => k.toLowerCase() === "content-type")) {
          o.headers = { ...(o.headers || {}), "Content-Type": "application/x-www-form-urlencoded" };
        }
      } else {
        o.body = String(body);
      }
    }
    return hostFetch(url, o);
  }
  function withUa(options, uaStr) {
    const o = { ...(options || {}) };
    const headers = { ...(o.headers || {}) };
    if (!Object.keys(headers).some((k) => k.toLowerCase() === "user-agent")) headers["User-Agent"] = uaStr;
    o.headers = headers;
    return o;
  }
  function hostFetchPC(url, options) { return hostFetch(url, withUa(options, PC_UA)); }
  function hostPostPC(url, b, o) { return hostPost(url, b, o === undefined ? undefined : withUa(o, PC_UA)); }
  function hostRequest(url, options = {}) {
    const u = String(url || "");
    if (u.startsWith("hiker://page/")) {
      const name = u.slice("hiker://page/".length).split(/[?#]/)[0];
      const rec = findModule(name);
      if (!rec) throw vErr("request: unknown page: " + name);
      return JSON.stringify({ name: rec.name, path: rec.path, rule: rec.code });
    }
    return hostFetch(u, options);
  }
  function hostGetResCode() { return state.lastBody; }
  function hostBatchFetch(list) {
    if (!Array.isArray(list)) throw vErr("batchFetch: argument must be an array");
    const out = [];
    for (let i = 0; i < list.length; i += MAX_BATCH) {
      for (const item of list.slice(i, i + MAX_BATCH)) {
        try { out.push(hostFetch(item.url, item.options || {})); }
        catch (e) { debug("batchFetch item failed:", e && e.message || e); out.push(""); }
      }
    }
    return vArr(out);
  }
  function hostFetchCookie(url, options = {}) {
    const res = doFetchRaw(url, options);
    const sc = res.headers["set-cookie"];
    if (sc !== undefined) state._cookie = String(sc);
    return JSON.stringify(sc === undefined ? [] : [String(sc)]);
  }
  function hostBuildUrl(url, params) {
    try {
      const u = new URL(String(url));
      for (const [k, v] of Object.entries(params || {})) u.searchParams.set(k, String(v));
      return u.href;
    } catch (e) { throw vErr("buildUrl: " + (e && e.message || e)); }
  }
  function hostFetchCache(url, hours) {
    const k = "fc:" + md5hex(url);
    const hit = _requireCache.get(k);
    if (hit && hit.expire > Date.now()) return hit.code;
    const body = hostFetch(url, {});
    _requireCache.set(k, { code: body, expire: Date.now() + Number(hours || 24) * 3600 * 1000 });
    return body;
  }
  function hostDeleteCache(url) {
    if (url === undefined) { _requireCache.clear(); return true; }
    return _requireCache.delete("fc:" + md5hex(url)) || _requireCache.delete("url:" + md5hex(url));
  }
  function hostDownloadFile(url, dest, headers) {
    const body = hostFetch(url, headers ? { headers } : {});
    hostSaveFile(dest, body);
    return String(dest);
  }
  function hostRequireDownload(url, dest) {
    if (hostFileExist(dest)) return String(dest);
    return hostDownloadFile(url, dest);
  }
  function hostSaveImage(urls, dest) {
    for (const u of String(urls || "").split("||")) {
      const t = u.trim();
      if (!t) continue;
      try { hostDownloadFile(t, dest); return true; } catch (e) { debug("saveImage failed:", t, e && e.message || e); }
    }
    return false;
  }


  // ---------- AES（原始 key/iv 模式，与 CryptoJS 口令模式并存） ----------
  // aesDecode/aesEncode 是 CryptoJS 口令模式（Salted__ + OpenSSL KDF），
  // 保留原语义不动；这里新增的是站点常用的「明文 key/iv」模式。
  function _aesCoerceBytes(value, encoding) {
    if (value == null) return null;
    if (typeof value === "object" && value && value.type === "Buffer"
        && Array.isArray(value.data)) return Buffer.from(value.data);
    const s = String(value);
    const enc = String(encoding || "").toLowerCase();
    if (enc === "hex") return Buffer.from(s.replace(/\s+/g, ""), "hex");
    if (enc === "base64") return Buffer.from(s.replace(/\s+/g, ""), "base64");
    // 未指定编码时按长度与字符集猜：32/48/64 位 hex → hex；否则按 utf8。
    const t = s.replace(/\s+/g, "");
    if (/^[0-9a-fA-F]{32}$/.test(t) || /^[0-9a-fA-F]{48}$/.test(t)
        || /^[0-9a-fA-F]{64}$/.test(t)) return Buffer.from(t, "hex");
    return Buffer.from(s, "utf8");
  }
  function _aesInputBytes(data, encoding) {
    if (data == null) return Buffer.alloc(0);
    if (typeof data === "object" && data && data.type === "Buffer"
        && Array.isArray(data.data)) return Buffer.from(data.data);
    if (ArrayBuffer.isView(data)) return Buffer.from(data.buffer, data.byteOffset, data.byteLength);
    const s = String(data);
    const enc = String(encoding || "base64").toLowerCase();
    if (enc === "utf8") return Buffer.from(s, "utf8");
    if (enc === "hex") return Buffer.from(s.replace(/\s+/g, ""), "hex");
    return Buffer.from(s.replace(/\s+/g, ""), "base64");
  }
  function _aesNoPaddingRemove(buf, padding) {
    const p = String(padding || "Pkcs7").toLowerCase();
    if (p === "nopadding" || p === "none") return buf;
    if (!buf.length) return buf;
    const pad = buf[buf.length - 1];
    if (pad > 0 && pad <= 16 && pad <= buf.length) {
      let ok = true;
      for (let i = buf.length - pad; i < buf.length; i++) {
        if (buf[i] !== pad) { ok = false; break; }
      }
      if (ok) return buf.subarray(0, buf.length - pad);
    }
    return buf;
  }
  function _aesOutput(buf, encoding) {
    const enc = String(encoding || "utf8").toLowerCase();
    if (enc === "buffer" || enc === "bytes") return buf;
    if (enc === "base64") return buf.toString("base64");
    if (enc === "hex") return buf.toString("hex");
    return buf.toString("utf8");
  }
  function aesCbcDecode(data, key, iv, options) {
    const o = options || {};
    const kb = _aesCoerceBytes(key, o.keyEncoding);
    const ivb = iv ? _aesCoerceBytes(iv, o.ivEncoding) : Buffer.alloc(16, 0);
    const db = _aesInputBytes(data, o.dataEncoding);
    if (!kb || kb.length !== 16) throw vErr("aesCbcDecode: key must be 16 bytes");
    if (ivb.length !== 16) throw vErr("aesCbcDecode: iv must be 16 bytes");
    if (db.length % 16 !== 0) throw vErr("aesCbcDecode: data length not multiple of 16");
    const decipher = crypto.createDecipheriv("aes-128-cbc", kb, ivb);
    decipher.setAutoPadding(String(o.padding || "Pkcs7").toLowerCase() !== "nopadding");
    const out = Buffer.concat([decipher.update(db), decipher.final()]);
    return _aesOutput(_aesNoPaddingRemove(out, o.padding), o.output);
  }
  function aesCbcEncode(data, key, iv, options) {
    const o = options || {};
    const kb = _aesCoerceBytes(key, o.keyEncoding);
    const ivb = iv ? _aesCoerceBytes(iv, o.ivEncoding) : Buffer.alloc(16, 0);
    const db = _aesInputBytes(data, o.dataEncoding || "utf8");
    if (!kb || kb.length !== 16) throw vErr("aesCbcEncode: key must be 16 bytes");
    if (ivb.length !== 16) throw vErr("aesCbcEncode: iv must be 16 bytes");
    const cipher = crypto.createCipheriv("aes-128-cbc", kb, ivb);
    cipher.setAutoPadding(String(o.padding || "Pkcs7").toLowerCase() !== "nopadding");
    const out = Buffer.concat([cipher.update(db), cipher.final()]);
    return _aesOutput(out, o.output);
  }
  function aesEcbDecode(data, key, options) {
    const o = options || {};
    const kb = _aesCoerceBytes(key, o.keyEncoding);
    const db = _aesInputBytes(data, o.dataEncoding);
    if (!kb || kb.length !== 16) throw vErr("aesEcbDecode: key must be 16 bytes");
    if (db.length % 16 !== 0) throw vErr("aesEcbDecode: data length not multiple of 16");
    const decipher = crypto.createDecipheriv("aes-128-ecb", kb, null);
    decipher.setAutoPadding(String(o.padding || "Pkcs7").toLowerCase() !== "nopadding");
    const out = Buffer.concat([decipher.update(db), decipher.final()]);
    return _aesOutput(_aesNoPaddingRemove(out, o.padding), o.output);
  }

  // ---------- 编解码 / 加解密 ----------
  function hostMd5(input) {
    const s = String(input || "");
    if (s.startsWith("hiker://files/") || s.startsWith("file://")) {
      return crypto.createHash("md5").update(fs.readFileSync(resolveStorePath(s))).digest("hex");
    }
    return md5hex(s);
  }
  const hostBase64Encode = (s) => Buffer.from(String(s ?? ""), "utf8").toString("base64");
  const hostBase64Decode = (s) => Buffer.from(String(s ?? "").replace(/\s+/g, ""), "base64").toString("utf8");
  const hostBtoa = (s) => Buffer.from(String(s ?? ""), "latin1").toString("base64");
  const hostAtob = (s) => Buffer.from(String(s ?? "").replace(/\s+/g, ""), "base64").toString("latin1");
  function hostAesEncode(k, input) {
    return CryptoJS.AES.encrypt(String(input ?? ""), String(k ?? "")).toString();
  }
  function hostAesDecode(k, input) {
    // CryptoJS 口令模式（Salted__ + OpenSSL KDF）；与 HikerView Java 实现互通未验证。
    return CryptoJS.AES.decrypt(String(input ?? ""), String(k ?? "")).toString(CryptoJS.enc.Utf8);
  }
  function rsaKeyBytes(keyPem, isPublic) {
    try {
      const kobj = isPublic ? crypto.createPublicKey(keyPem) : crypto.createPrivateKey(keyPem);
      return Math.floor((kobj.asymmetricKeyDetails?.modulusLength || 2048) / 8);
    } catch { return 256; } // 2048 位默认
  }
  function hostRsaEncrypt(data, key, options = {}) {
    const o = options || {};
    const type = Number(o.type || 1);
    const long = Number(o.long || 1);
    const buf = Buffer.from(String(data ?? ""), "utf8");
    const keyBytes = rsaKeyBytes(key, type === 1);
    const chunk = keyBytes - 11; // PKCS1Padding 开销
    if (long !== 2 && buf.length > chunk) {
      throw vErr("rsaEncrypt: data too long for single block (use long:2)");
    }
    const out = [];
    for (let i = 0; i < buf.length; i += chunk) {
      const part = buf.subarray(i, i + chunk);
      out.push(type === 1
        ? crypto.publicEncrypt({ key, padding: crypto.constants.RSA_PKCS1_PADDING }, part)
        : crypto.privateEncrypt({ key, padding: crypto.constants.RSA_PKCS1_PADDING }, part));
    }
    return Buffer.concat(out).toString("base64");
  }
  function hostRsaDecrypt(data, key, options = {}) {
    const o = options || {};
    const type = Number(o.type || 1);
    const buf = Buffer.from(String(data ?? "").replace(/\s+/g, ""), "base64");
    const keyBytes = rsaKeyBytes(key, type === 1);
    const out = [];
    for (let i = 0; i < buf.length; i += keyBytes) {
      const part = buf.subarray(i, i + keyBytes);
      out.push(type === 1
        ? crypto.privateDecrypt({ key, padding: crypto.constants.RSA_PKCS1_PADDING }, part)
        : crypto.publicDecrypt({ key, padding: crypto.constants.RSA_PKCS1_PADDING }, part));
    }
    return Buffer.concat(out).toString("utf8");
  }
  function hostDecodeStr(input, charset) {
    const cs = String(charset || "UTF-8").toUpperCase();
    const bytes = Buffer.from(String(input ?? ""), "latin1");
    if (cs === "UTF-8" || cs === "UTF8") return bytes.toString("utf8");
    // GBK/GB2312：Node 的 TextDecoder 不带 full-icu 时不支持 gbk，
    // 且 gbkTool.decode 只认 %XX 形态；这里走字节表直接还原。
    if (cs === "GBK" || cs === "GB2312" || cs === "GB18030") {
      try { return _gbk.decodeBytes(bytes); }
      catch (e) { debug("decodeStr GBK failed:", e && e.message); }
    }
    try {
      return new TextDecoder(cs.toLowerCase()).decode(bytes);
    } catch { return bytes.toString("latin1"); }
  }
  function hostEncodeStr(input, charset) {
    const cs = String(charset || "UTF-8").toUpperCase();
    const s = String(input ?? "");
    if (cs === "UTF-8" || cs === "UTF8") return Buffer.from(s, "utf8").toString("latin1");
    if (cs === "GBK" || cs === "GB2312") {
      // gbkTool.encode 返回 %XX 形式的 GBK 字节串，再还原为 latin1 字符串。
      return _gbk.encode(s).replace(/%([0-9A-Fa-f]{2})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
    }
    return Buffer.from(s, "utf8").toString("latin1");
  }
  function hostGetCryptoJS() {
    return fs.readFileSync(path.join(HERE, "node_vendor", "crypto-js.js"), "utf8");
  }
  function hostHexToBytes(hex) {
    const clean = String(hex || "").replace(/^0x/i, "").replace(/\s+/g, "");
    if (clean.length % 2) throw vErr("hexToBytes: odd length hex string");
    const bytes = new VUint8Array(clean.length / 2);
    for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
    return bytes;
  }
  function hostHexToBase64(hex) {
    return Buffer.from(hostHexToBytes(hex)).toString("base64");
  }
  function hostWriteHexFile(name, hex) {
    const abs = resolveStorePath(name);
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    fs.writeFileSync(abs, Buffer.from(hostHexToBytes(hex)));
    return true;
  }
  const rc4 = nullObj();
  rc4.encode = (str, k, charset) => {
    const data = Buffer.from(hostEncodeStr(str, charset || "UTF-8"), "latin1");
    return rc4Crypt(data, Buffer.from(String(k ?? ""), "utf8")).toString("hex");
  };
  rc4.decode = (hex, k, charset) => {
    const data = rc4Crypt(Buffer.from(String(hex || "").replace(/\s+/g, ""), "hex"), Buffer.from(String(k ?? ""), "utf8"));
    return hostDecodeStr(data.toString("latin1"), charset || "UTF-8");
  };
  hardenFn(rc4.encode); hardenFn(rc4.decode);
  const window0 = nullObj();
  window0.btoa = hostBtoa; window0.atob = hostAtob;
  hardenFn(window0.btoa); hardenFn(window0.atob);

  // ---------- 选择器（复用 selectors.mjs；pdfa 返回 HTML 字符串数组，与手册一致） ----------
  const selPdfh = makePdfh();
  // pd 补全基准：优先最近一次成功 fetch 的终址（引擎模块常先 fetch 绝对地址
  // 再 pd 相对链接；此时 MY_URL 仍是 hiker://empty 之类的规则导航地址，不能
  // 当基准），未 fetch 过时回退到 MY_URL。
  const selPd = makePd(selPdfh, () => state.lastFetchUrl || state.myUrl);
  function hostPdfh(html, rule) { return selPdfh(html, rule); }
  function hostPdfa(html, rule) {
    // HikerView 语义：&& 全是选择器层级（'div&&a' = div 下全部 a），与 drpy
    // selectors.mjs 不同——后者会把末段 'a' 误判为属性名，只返回 div 本身。
    // 所以 pdfa 不走 selPdfa，自己按链逐层找全部后代；pdfh/pd 仍复用 selectors。
    const segs = String(rule || "").split("&&").map((s) => s.trim()).filter(Boolean);
    let $; let scopeEl = null;
    if (html && html.__drpyNode) { $ = html.$; scopeEl = html.el; }
    else { $ = cheerioLoad(typeof html === "string" ? html : String(html ?? "")); }
    const out = new VArray();
    if (!segs.length) {
      out.push(scopeEl ? $.html(scopeEl) : $.html());
      return out;
    }
    let current = scopeEl ? [scopeEl] : [$.root()[0]];
    for (const seg of segs) {
      if (seg === ".") continue;
      const next = [];
      for (const n of current) {
        $(n).find(seg).each((_, el) => next.push(el));
      }
      current = next;
      if (!current.length) break;
    }
    for (const el of current) out.push($.html(el));
    return out;
  }
  function hostPd(html, rule, uri) { return selPd(html, rule, uri); }
  function hostXpath(html, expr) { return xpathEval(html, expr, true); }
  function hostXpathArray(html, expr) { return vArr(xpathEval(html, expr, false)); }

  // ---------- 结果 / 报错 / 提示 ----------
  function hostSetResult(v, kind) { state.result = v; state.resultKind = kind; return v; }
  function hostSetError(msg) {
    state.error = String(msg ?? "");
    logger("[haikuo error]", state.error);
  }
  function hostToast(msg) {
    state.toasts.push(String(msg ?? ""));
    logger("[haikuo toast]", String(msg ?? ""));
  }
  function hostLog(...args) { logger(...args); }
  function hostCopy(text) {
    state.clipboard = String(text ?? "");
    logger("[haikuo copy]", state.clipboard.slice(0, 80));
  }

  // ---------- 搜索模式 ----------
  function hostGetSearchMode() { return state.searchMode; }
  function hostSetSearchMode(m) { state.searchMode = Number(m) === 1 ? 1 : 0; return state.searchMode; }
  function hostSearchContains(text, k, ignoreCase) {
    const hay0 = String(text ?? "");
    const hay = ignoreCase ? hay0.toLowerCase() : hay0;
    const tokens = String(k ?? "").split(/\s+/).filter(Boolean);
    return tokens.every((t) => hay.includes(ignoreCase ? t.toLowerCase() : t));
  }

  // ---------- 规则 / 页面 ----------
  function hostGetParam(k, def) {
    try {
      const v = new URL(state.myUrl).searchParams.get(String(k));
      return v === null ? (def === undefined ? "" : def) : v;
    } catch { return def === undefined ? "" : def; }
  }
  function hostGetHome(url) {
    try {
      const u = new URL(String(url || state.myUrl || ""));
      return u.protocol + "//" + u.host;
    } catch { return ""; }
  }
  function hostSetLastChapterRule(rule) {
    state.lastChapterRule = String(rule ?? ""); // 只记元数据；v1 无收藏页可应用
  }

  // ---------- vm 执行 ----------
  // 所有宿主抛出的错误都已是 vm realm 的 Error；这里再把编译期 / 规则代码
  // 之外的宿主错误统一转成 vm realm Error，避免经 .constructor 逃逸。
  function execCode(code, filename) {
    const src = stripJsPrefix(code);
    let script;
    try {
      script = new vm.Script(src, { filename: filename || "haikuo.js" });
    } catch (e) {
      throw vErr("haikuo compile error (" + (filename || "") + "): " + (e && e.message || e));
    }
    try {
      return script.runInContext(vmContext, { timeout: RUN_TIMEOUT_MS });
    } catch (e) {
      if (e instanceof VError) throw e;
      throw vErr(String((e && e.message) || e));
    }
  }
  function run(code, filename) {
    const res = execCode(code, filename || "haikuo-eval.js");
    syncMyUrlFromSandbox();
    return res;
  }
  function syncMyUrlFromSandbox() {
    try {
      const u = sandbox.MY_URL;
      if (typeof u === "string" && u !== state.myUrl) state.myUrl = u;
      const p = sandbox.MY_PAGE;
      if (typeof p === "number" && p !== state.myPage) state.myPage = p;
    } catch { /* 规则代码删了 MY_URL 也不致命 */ }
  }

  // ---------- $ 工具与模块 ----------
  const modules = new Map(); // 规范化 path -> { path, name, code, loaded, loading, exports }
  const moduleAliases = new Map(); // 别名（basename 去 .js）-> 规范化 path
  function normalizeModulePath(p) {
    let s = String(p || "").trim();
    s = s.replace(/^hiker:\/\/page\//, "").replace(/^file:\/\//, "");
    s = s.replace(/^\/+/, "").replace(/\/+$/, "");
    return s;
  }
  function registerModule(p, code) {
    const norm = normalizeModulePath(p);
    if (!norm) throw vErr("registerModule: empty path");
    const rec = {
      path: norm,
      name: norm.split("/").pop().replace(/\.js$/i, ""),
      code: String(code ?? ""),
      loaded: false, loading: false, exports: undefined,
      // 无 $.exports 的是“片段页”（erji/so/de 等 js: 代码，每次进详情/
      // 搜索都要重新执行，不能缓存）；有 $.exports 的才是可缓存的引擎模块
      //（草榴 bt、合成引擎站 eng）。batch 2 判定，见 requireModule。
      isSnippet: !/\$\.exports\s*=/.test(String(code ?? "")),
    };
    modules.set(norm, rec);
    moduleAliases.set(rec.name, norm);
    return rec;
  }
  function findModule(spec) {
    const norm = normalizeModulePath(spec);
    if (modules.has(norm)) return modules.get(norm);
    if (moduleAliases.has(norm)) return modules.get(moduleAliases.get(norm));
    const noExt = norm.replace(/\.js$/i, "");
    if (modules.has(noExt)) return modules.get(noExt);
    if (moduleAliases.has(noExt)) return modules.get(moduleAliases.get(noExt));
    return null;
  }
  function requireModule(spec, force) {
    const rec = findModule(spec);
    if (!rec) throw vErr("haikuo: module not registered: " + String(spec));
    // 片段页（isSnippet）每次 require 都重新执行：详情页 snippet 第二次
    // 打开必须是另一部剧的详情，缓存会返回上一次的 setResult。引擎模块
    // 仍按 $.require 语义缓存（force=true 可强制重载）。
    if (rec.loaded && !force && !rec.isSnippet) return rec.exports;
    if (rec.loading) throw vErr("haikuo: circular $.require: " + rec.path);
    rec.loading = true;
    const prevExports = dollar.exports;
    dollar.exports = undefined;
    try {
      execCode(rec.code, "haikuo-module:" + rec.path);
      rec.exports = dollar.exports === undefined ? nullObj() : dollar.exports;
      rec.loaded = true;
      return rec.exports;
    } finally {
      rec.loading = false;
      dollar.exports = prevExports;
    }
  }
  function loadFileModule(absPath) {
    let code;
    try { code = fs.readFileSync(absPath, "utf8"); }
    catch { throw vErr("haikuo: cannot read module file: " + absPath); }
    const rel = path.relative(storeDir, absPath).split(path.sep).join("/");
    if (!modules.has(normalizeModulePath(rel))) registerModule(rel, code);
    return requireModule(rel);
  }
  function dollarRequire(spec, force) {
    const s = String(spec || "");
    if (/[?&]rule=/.test(s)) {
      throw vErr(`${V1_UNSUPPORTED}: cross-rule $.require (hiker://page/x?rule=Y)`);
    }
    if (/^https?:\/\//i.test(s)) {
      throw vErr(`${V1_UNSUPPORTED}: $.require of remote URL in v1 (use bare require(url) for remote code blocks)`);
    }
    if (s.startsWith("file://")) return loadFileModule(resolveStorePath(s));
    if (findModule(s)) return requireModule(s, force);
    // 未注册的纯名：试 storeDir 下同名 .js 文件（file:// 之外的本地模块写法）。
    if (/\.js$/i.test(s)) {
      try {
        const abs = resolveStorePath(s);
        if (fs.existsSync(abs)) return loadFileModule(abs);
      } catch (e) { if (e instanceof VError) throw e; }
    }
    throw vErr("haikuo: module not registered: " + s);
  }
  function resolveModuleCode(spec) {
    const s = String(spec || "");
    const rec = findModule(s);
    if (rec) return rec.code;
    if (/^https?:\/\//i.test(s)) return hostFetch(s, {});
    if (/\.js$/i.test(s)) {
      const abs = resolveStorePath(s);
      if (fs.existsSync(abs)) return fs.readFileSync(abs, "utf8");
    }
    throw vErr("haikuo: module not found: " + s);
  }
  function hostRequire(spec, fetchOpts) {
    const s = String(spec || "");
    if (/^https?:\/\//i.test(s)) {
      // 手册“远程模块引用”：按 md5(url) 缓存，取回后在顶层作用域 eval。
      const ck = "url:" + md5hex(s);
      const hit = _requireCache.get(ck);
      if (hit && hit.expire > Date.now()) { execCode(hit.code, "require:" + s); return undefined; }
      const code = hostFetch(s, fetchOpts || {});
      _requireCache.set(ck, { code, expire: Date.now() + 24 * 3600 * 1000 });
      execCode(code, "require:" + s);
      return undefined;
    }
    return dollarRequire(s);
  }
  function hostRequireJs(spec) {
    const code = resolveModuleCode(spec);
    // CommonJS 包装：在 vm 内执行，module/exports 均为 vm realm 对象。
    const wrapped = `(function(){ var module = {exports:{}}; var exports = module.exports; (function(module, exports){\n${stripJsPrefix(code)}\n})(module, module.exports); return module.exports; })()`;
    return execCode(wrapped, "requirejs:" + String(spec));
  }
  function hostRequireCache(url, hours) {
    const code = hostFetchCache(url, hours);
    execCode(code, "requireCache:" + String(url));
  }
  // $.toString(fn, ...args)：无参返回函数源码；有参返回立即调用形式（手册
  // registerTask 示例的写法）。
  function dollarToString(fn, ...args) {
    const src = typeof fn === "function" ? Function.prototype.toString.call(fn) : String(fn ?? "");
    if (!args.length) return src;
    return "(" + src + ")(" + args.map((a) => JSON.stringify(a) + "").join(",") + ")";
  }
  function dollar(url, headers) {
    const base = String(url || "");
    const hdrs = headers && typeof headers === "object" ? headers : {};
    const b = nullObj();
    // 只做 URL 拼串；拼出来的 @rule/@lazyRule/@input/@js 由 batch 2 的解释器执行。
    // 惰性调用：无参时拼 (fn)()；有参时把实参一并拼进立即调用（海阔播放
    // 链接就靠这个传参，如花魁 playLazy(fn, id, src, host, token)；batch 1
    // 只拼了 (fn)()，把实参丢了，播放函数拿到的全是 undefined）。
    const lazyCall = (fn, args) => (args.length
      ? dollarToString(fn, ...args)
      : "(" + dollarToString(fn) + ")()");
    b.rule = hardenFn((fn, ...args) => base + "@rule=.js:" + lazyCall(fn, args));
    b.lazyRule = hardenFn((fn, ...args) => base + "@lazyRule=.js:" + lazyCall(fn, args));
    // x5Rule 只拼 javascript: 链接；执行（WebView）是 v1 不支持的部分。
    b.x5Rule = hardenFn((fn) => "javascript:(" + dollarToString(fn) + ")()");
    b.input = hardenFn((fn, ...args) => base + "@input=.js:" + lazyCall(fn, args));
    b.image = hardenFn((fn, ...rest) => {
      void rest; // 占位参数 s1, s2…：手册未定其语义，v1 只拼串
      return base + "@headers=" + JSON.stringify(hdrs) + "@js=(" + dollarToString(fn) + ")()";
    });
    return b;
  }
  dollar.require = hardenFn(dollarRequire);
  dollar.toString = hardenFn(dollarToString);
  dollar.exports = undefined;

  // ---------- http 链式（手册“类似 ajax”封装；start 内同步执行） ----------
  function httpBuilder(method, url, opts) {
    const b = nullObj();
    let headers = { ...((opts && opts.headers) || {}) };
    let body = opts ? opts.body : undefined;
    let successFn = null;
    let errorFn = null;
    b.headers = hardenFn((h) => { Object.assign(headers, h || {}); return b; });
    b.success = hardenFn((fn) => { successFn = fn; return b; });
    b.error = hardenFn((fn) => { errorFn = fn; return b; });
    b.start = hardenFn(() => {
      try {
        const raw = hostFetch(url, { method, headers, body });
        let data = raw;
        try { data = JSON.parse(raw); } catch { /* 非 JSON 即原文 */ }
        if (successFn) successFn(data);
      } catch (e) {
        if (errorFn) errorFn(String((e && e.message) || e));
        else throw e;
      }
      return httpObj;
    });
    b.fetch = hardenFn((u2, o2) => httpBuilder("GET", u2, o2));
    b.post = hardenFn((u2, o2) => httpBuilder("POST", u2, o2));
    b.request = hardenFn((u2, o2) => httpBuilder("GET", u2, o2));
    return b;
  }
  const httpObj = nullObj();
  httpObj.fetch = hardenFn((u, o) => httpBuilder("GET", u, o));
  httpObj.post = hardenFn((u, o) => httpBuilder("POST", u, o));
  httpObj.request = hardenFn((u, o) => httpBuilder("GET", u, o));

  // ---------- UI 相关 stub（无真机界面；命中记 debug，不抛错） ----------
  function stub(name, ret) {
    return hardenFn((...args) => {
      debug("stub hit:", name, JSON.stringify(args).slice(0, 200));
      return typeof ret === "function" ? ret(...args) : ret;
    });
  }
  const stubConfirm = stub("confirm", false);
  const stubUpdateItem = stub("updateItem", undefined);
  const stubDeleteItem = stub("deleteItem", undefined);
  const stubDeleteItemByCls = stub("deleteItemByCls", undefined);
  const stubAddItemAfter = stub("addItemAfter", undefined);
  const stubAddItemBefore = stub("addItemBefore", undefined);
  const stubFindItem = stub("findItem", null);
  const stubFindItemsByCls = stub("findItemsByCls", () => vArr([]));
  const stubBack = stub("back", undefined);
  const stubRefreshPage = stub("refreshPage", undefined);
  const stubRefreshX5WebView = stub("refreshX5WebView", undefined);
  const stubShowLoading = stub("showLoading", undefined);
  const stubHideLoading = stub("hideLoading", undefined);
  const stubAddListener = stub("addListener", (...args) => {
    state.listeners.push({ event: String(args[0] ?? ""), code: String(args[1] ?? "") });
  });
  const stubGetHomeSub = stub("getHomeSub", () => vArr([]));
  const stubHasHomeSub = stub("hasHomeSub", false);
  const stubGetLastRules = stub("getLastRules", () => vArr([]));
  const stubGetRuleCount = stub("getRuleCount", "1");
  const stubGetColTypes = stub("getColTypes", () => vArr([
    "text_1", "text_2", "text_3", "pic_1", "pic_2", "pic_1_full",
    "list_1", "input", "video", "music",
  ]));
  const stubGetAppVersion = stub("getAppVersion", 1800);
  const stubGetIP = stub("getIP", "127.0.0.1");
  const stubGetCpuAbi = stub("getCpuAbi", "");
  const stubRegisterDNS = stub("registerDNS", (map) => {
    Object.assign(state.dns, map || {});
  });
  const stubIpping = stub("ipping", false);
  const stubFindReachableIP = stub("findReachableIP", "");
  const stubRegisterTask = stub("registerTask", (id) => {
    const tid = id ? String(id) : crypto.randomUUID();
    state.tasks.push(tid);
    return tid;
  });
  const stubUnRegisterTask = stub("unRegisterTask", (id) => {
    const i = state.tasks.indexOf(String(id));
    if (i >= 0) state.tasks.splice(i, 1);
    return true;
  });
  const stubCloseMe = stub("closeMe", undefined);

  // ---------- v1 不支持：调用即抛明确错误 ----------
  function unsupportedFn(name) {
    return hardenFn(() => unsupported(name));
  }

  // ---------- 沙箱组装 ----------
  // 注意：这里只放宿主全局。绝不放 process / require / module / fs / Buffer。
  // console 走 stderr（stdout 只留给 sidecar 的 JSONL 协议行）。
  const sandbox = {};
  const assign = (name, fn) => { sandbox[name] = typeof fn === "function" ? hardenFn(fn) : fn; };
  assign("fetch", hostFetch);
  assign("request", hostRequest);
  assign("post", hostPost);
  assign("fetchPC", hostFetchPC);
  assign("postPC", hostPostPC);
  assign("getResCode", hostGetResCode);
  assign("batchFetch", hostBatchFetch);
  assign("bf", hostBatchFetch);
  assign("fetchCookie", hostFetchCookie);
  assign("buildUrl", hostBuildUrl);
  assign("fetchCache", hostFetchCache);
  assign("requireCache", hostRequireCache);
  assign("deleteCache", hostDeleteCache);
  // 注意：裸 require 不暴露（沙箱要求：不暴露 Node 的 require；HikerView 的远程
  // 代码块 require 见 requirejs / $.require，v1 不提供裸 require 全局）。
  // 下面 hostRequire 保留给 batch 2 的规则加载器内部复用，不进 vm 全局。
  assign("requirejs", hostRequireJs);
  assign("downloadFile", hostDownloadFile);
  assign("requireDownload", hostRequireDownload);
  assign("saveImage", hostSaveImage);
  assign("getPath", hostGetPath);
  // java 最小兼容：只补规则写本地清单用到的三个类（见 resolveJavaPath 注释）。
  const javaIo = nullObj();
  javaIo.File = hardenFn(JavaIoFile);
  javaIo.FileOutputStream = hardenFn(JavaIoFileOutputStream);
  const javaLang = nullObj();
  javaLang.String = hardenFn(JavaLangString);
  const javaNs = nullObj();
  javaNs.io = javaIo;
  javaNs.lang = javaLang;
  sandbox.java = javaNs;
  assign("parseDom", hostPd);
  assign("parseDomForHtml", hostPdfh);
  assign("parseDomForArray", hostPdfa);
  assign("pd", hostPd);
  assign("pdfh", hostPdfh);
  assign("pdfa", hostPdfa);
  assign("xpath", hostXpath);
  assign("xpathArray", hostXpathArray);
  assign("xpa", hostXpathArray);
  assign("setResult", (v) => hostSetResult(v, "result"));
  assign("setHomeResult", (v) => hostSetResult(v, "home"));
  assign("setSearchResult", (v) => hostSetResult(v, "search"));
  assign("setError", hostSetError);
  assign("error", hostSetError);
  assign("getItem", hostGetItem);
  assign("setItem", hostSetItem);
  assign("putItem", hostSetItem); // HikerView 写法，setItem 的别名（batch 2 补）
  assign("clearItem", hostClearItem);
  assign("getPublicItem", hostGetPublicItem);
  assign("setPublicItem", hostSetPublicItem);
  assign("clearPublicItem", hostClearPublicItem);
  assign("putVar", hostPutVar);
  assign("getVar", hostGetVar);
  assign("clearVar", hostClearVar);
  assign("aesCbcDecode", aesCbcDecode);
  assign("aesCbcEncode", aesCbcEncode);
  assign("aesEcbDecode", aesEcbDecode);
  assign("putMyVar", hostPutMyVar);
  assign("getMyVar", hostGetMyVar);
  assign("clearMyVar", hostClearMyVar);
  assign("listMyVarKeys", hostListMyVarKeys);
  sandbox.storage0 = storage0;
  assign("toast", hostToast);
  assign("log", hostLog);
  assign("print", hostLog);
  assign("copy", hostCopy);
  sandbox.$ = dollar;
  hardenFn(dollar);
  sandbox.http = httpObj;
  assign("md5", hostMd5);
  assign("base64Encode", hostBase64Encode);
  assign("base64Decode", hostBase64Decode);
  assign("btoa", hostBtoa);
  assign("atob", hostAtob);
  assign("aesEncode", hostAesEncode);
  assign("aesDecode", hostAesDecode);
  assign("rsaEncrypt", hostRsaEncrypt);
  assign("rsaDecrypt", hostRsaDecrypt);
  assign("decodeStr", hostDecodeStr);
  assign("encodeStr", hostEncodeStr);
  assign("getCryptoJS", hostGetCryptoJS);
  assign("hexToBytes", hostHexToBytes);
  assign("hexToBase64", hostHexToBase64);
  assign("writeHexFile", hostWriteHexFile);
  sandbox.rc4 = rc4;
  sandbox.window0 = window0;
  assign("getSearchMode", hostGetSearchMode);
  assign("setSearchMode", hostSetSearchMode);
  assign("searchContains", hostSearchContains);
  assign("saveFile", hostSaveFile);
  assign("readFile", hostReadFile);
  assign("deleteFile", hostDeleteFile);
  assign("fileExist", hostFileExist);
  assign("writeFile", hostSaveFile);
  assign("getParam", hostGetParam);
  assign("getRule", () => sandbox.MY_RULE);
  assign("getHome", hostGetHome);
  assign("setLastChapterRule", hostSetLastChapterRule);
  // 定时器：包一层再钉，绝不直接改共享的全局 setTimeout。
  assign("setTimeout", (fn, ms, ...a) => setTimeout(fn, ms, ...a));
  assign("setInterval", (fn, ms, ...a) => setInterval(fn, ms, ...a));
  assign("clearTimeout", (id) => clearTimeout(id));
  assign("clearInterval", (id) => clearInterval(id));
  // UI stub
  assign("confirm", stubConfirm);
  assign("updateItem", stubUpdateItem);
  assign("deleteItem", stubDeleteItem);
  assign("deleteItemByCls", stubDeleteItemByCls);
  assign("addItemAfter", stubAddItemAfter);
  assign("addItemBefore", stubAddItemBefore);
  assign("findItem", stubFindItem);
  assign("findItemsByCls", stubFindItemsByCls);
  assign("back", stubBack);
  assign("refreshPage", stubRefreshPage);
  assign("refreshX5WebView", stubRefreshX5WebView);
  assign("setPageTitle", (t) => { debug("stub hit: setPageTitle"); state.pageTitle = String(t ?? ""); });
  assign("getPageTitle", () => state.pageTitle);
  assign("setPagePicUrl", (u) => { debug("stub hit: setPagePicUrl"); state.pagePicUrl = String(u ?? ""); });
  assign("setPageParams", (p) => { debug("stub hit: setPageParams"); state.pageParams = p || {}; });
  assign("showLoading", stubShowLoading);
  assign("hideLoading", stubHideLoading);
  assign("addListener", stubAddListener);
  assign("getHomeSub", stubGetHomeSub);
  assign("hasHomeSub", stubHasHomeSub);
  assign("getLastRules", stubGetLastRules);
  assign("getRuleCount", stubGetRuleCount);
  assign("getColTypes", stubGetColTypes);
  assign("getAppVersion", stubGetAppVersion);
  assign("getIP", stubGetIP);
  assign("getCpuAbi", stubGetCpuAbi);
  assign("registerDNS", stubRegisterDNS);
  assign("ipping", stubIpping);
  assign("findReachableIP", stubFindReachableIP);
  assign("registerTask", stubRegisterTask);
  assign("unRegisterTask", stubUnRegisterTask);
  assign("closeMe", stubCloseMe);
  // v1 不支持
  assign("loadJavaClass", unsupportedFn("loadJavaClass"));
  assign("fetchCodeByWebView", unsupportedFn("fetchCodeByWebView"));
  assign("cacheM3u8", unsupportedFn("cacheM3u8"));
  assign("clearM3u8Ad", unsupportedFn("clearM3u8Ad"));
  assign("clearM3u8AdLazy", unsupportedFn("clearM3u8AdLazy"));
  assign("cacheM3u8WithPngProxy", unsupportedFn("cacheM3u8WithPngProxy"));
  assign("convertM3u8WithPngProxy", unsupportedFn("convertM3u8WithPngProxy"));
  assign("startProxyServer", unsupportedFn("startProxyServer"));
  assign("evalPrivateJS", unsupportedFn("evalPrivateJS"));
  assign("getPastes", unsupportedFn("getPastes"));
  assign("sharePaste", unsupportedFn("sharePaste"));
  assign("parsePaste", unsupportedFn("parsePaste"));
  // 注入变量（标量直接赋值；对象走 vm 内 JSON 赋值，保证是 vm realm 对象）。
  sandbox.MY_URL = state.myUrl;
  sandbox.MY_PAGE = state.myPage;
  sandbox.MY_TYPE = state.myType;
  sandbox.MY_KEYWORD = state.myKeyword;
  sandbox.MY_CLASS_URL = state.myClassUrl;
  sandbox.MY_CLASS_NAME = state.myClassName;
  sandbox.MOBILE_UA = MOBILE_UA;
  sandbox.PC_UA = PC_UA;
  sandbox.VERSION = HAIKUO_VERSION;
  sandbox.RKEY = key;
  sandbox.HOST = state.myHome || "";
  const consoleObj = nullObj();
  consoleObj.log = hardenFn((...a) => logger(...a));
  consoleObj.info = hardenFn((...a) => logger(...a));
  consoleObj.warn = hardenFn((...a) => logger("[warn]", ...a));
  consoleObj.error = hardenFn((...a) => logger("[error]", ...a));
  consoleObj.debug = hardenFn((...a) => logger("[debug]", ...a));
  sandbox.console = consoleObj;

  const vmContext = vm.createContext(sandbox);
  // 注意：vm 内建不能从宿主侧的 sandbox 对象上直接读到（contextified 对象
  // 不暴露内建属性），必须在 context 内取值。
  VArray = vm.runInContext("Array", vmContext);
  VObject = vm.runInContext("Object", vmContext);
  VError = vm.runInContext("Error", vmContext);
  VUint8Array = vm.runInContext("Uint8Array", vmContext);
  // MY_HOME：未显式给时从 MY_URL 取 origin。
  state.myHome = state.myHome || hostGetHome(state.myUrl);
  sandbox.MY_HOME = state.myHome;
  sandbox.HOST = state.myHome;
  // MY_RULE / MY_PARAMS 走 vm 内赋值（vm realm 对象，避免宿主对象逃逸链）。
  const ruleObj = opts.rule && typeof opts.rule === "object" ? opts.rule : {};
  vm.runInContext("MY_RULE = " + JSON.stringify(ruleObj) + ";", vmContext);
  vm.runInContext("MY_PARAMS = " + JSON.stringify(state.myParams || {}) + ";", vmContext);

  function setPage(patch = {}) {
    if (patch.myUrl !== undefined || patch.url !== undefined) {
      state.myUrl = String(patch.myUrl ?? patch.url ?? state.myUrl);
      sandbox.MY_URL = state.myUrl;
      state.myHome = hostGetHome(state.myUrl);
      sandbox.MY_HOME = state.myHome;
      sandbox.HOST = state.myHome;
    }
    if (patch.myPage !== undefined || patch.page !== undefined) {
      state.myPage = Number(patch.myPage ?? patch.page ?? state.myPage);
      sandbox.MY_PAGE = state.myPage;
    }
    if (patch.myType !== undefined || patch.type !== undefined) {
      state.myType = String(patch.myType ?? patch.type ?? state.myType);
      sandbox.MY_TYPE = state.myType;
    }
    if (patch.myKeyword !== undefined || patch.keyword !== undefined) {
      state.myKeyword = String(patch.myKeyword ?? patch.keyword ?? state.myKeyword);
      sandbox.MY_KEYWORD = state.myKeyword;
    }
    if (patch.myClassUrl !== undefined || patch.classUrl !== undefined) {
      state.myClassUrl = String(patch.myClassUrl ?? patch.classUrl ?? state.myClassUrl);
      sandbox.MY_CLASS_URL = state.myClassUrl;
    }
    if (patch.myClassName !== undefined || patch.className !== undefined) {
      state.myClassName = String(patch.myClassName ?? patch.className ?? state.myClassName);
      sandbox.MY_CLASS_NAME = state.myClassName;
    }
    if (patch.myParams !== undefined || patch.params !== undefined) {
      state.myParams = patch.myParams ?? patch.params ?? {};
      vm.runInContext("MY_PARAMS = " + JSON.stringify(state.myParams || {}) + ";", vmContext);
    }
    if (patch.rule !== undefined) {
      vm.runInContext("MY_RULE = " + JSON.stringify(patch.rule || {}) + ";", vmContext);
    }
    if (patch.resCode !== undefined) state.lastBody = String(patch.resCode);
  }

  function getPage() {
    return {
      myUrl: state.myUrl, myPage: state.myPage, myType: state.myType,
      myParams: state.myParams, myKeyword: state.myKeyword,
      myClassUrl: state.myClassUrl, myClassName: state.myClassName,
      myHome: state.myHome,
    };
  }

  return {
    ruleKey: key,
    storeDir,
    sandbox, // globals map
    context: vmContext,
    run,
    registerModule,
    requireModule,
    listModules: () => [...modules.keys()],
    setPage,
    getPage,
    state,
    HOST_API_COVERAGE,
  };
}

// ---------- Batch 2：规则加载器 + 非 JS 解析串解释器 + lazy 评估器 ----------
// 本节只依赖本文件的 createHaikuoContext / stripJsPrefix，不动 drpy。
//
// 三种页面形态（五条实测规则 + 草榴 BT 规则覆盖了全部三种）：
//   1. 引擎模块页：pages[] 的 rule 是一段定义全局对象 / 以 $.exports 结尾
//      的 JS（如草榴 bt、合成引擎站 eng）——$.require(path) 调用它；
//   2. 引擎源码页：rule 是不带 js: 前缀、不带 $.exports 的源码（如橙子 cz，
//      定义 var CZ = {...}）——靠 eval(JSON.parse(fetch('hiker://page/cz'))
//      .rule) 载入，fetch('hiker://page/…') 由 batch 2 在 doFetchRaw 补齐；
//   3. 片段页：rule 是自包含的 js: 程序（如 API 规则的 erji/so/tag/tags、
//      橙子 so/de/syn）——每次进详情/搜索都重新执行（requireModule 对
//      isSnippet 页不缓存，见上）。
// 详情/搜索的入口 URL 形如 `<host>/detail/<slug>@rule=js:$.require(
// "hiker://page/erji")`；播放链接形如 `<input>@lazyRule=.js:(fn)(args)`。
export const HAIKUO_PLAY_TOKEN_PREFIX = "haikuo-play:";
const HAIKUO_MAX_RULE_BYTES = 2 * 1024 * 1024; // 规则 JSON 上限 2MB（五条实测规则最大 273KB）
const HAIKUO_DETAIL_PAGE_PATHS = ["erji", "de"]; // 详情页 snippet 的惯用 path
const HAIKUO_READ_TYPES = new Set(["read", "novel", "book", "小说", "阅读"]); // v1 只做 video

export function isHaikuoJsRule(s) {
  return String(s ?? "").trimStart().toLowerCase().startsWith("js:");
}

// 解析 + 校验一条规则 JSON（字符串或对象）。pages 是 JSON 字符串，需二次解析。
// read 型规则直接拒绝（v1 只做 video），错误信息写明原因。
export function parseHaikuoRule(ruleInput) {
  let raw = ruleInput;
  if (typeof raw === "string") {
    if (raw.length > HAIKUO_MAX_RULE_BYTES) throw new Error("haikuo: rule too large (>2MB)");
    try { raw = JSON.parse(raw); }
    catch (e) { throw new Error("haikuo: rule is not valid JSON: " + (e && e.message || e)); }
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("haikuo: rule must be a JSON object");
  }
  const title = String(raw.title ?? "").trim();
  if (!title) throw new Error("haikuo: rule.title is required");
  const type = String(raw.type ?? "").trim();
  if (!type) throw new Error("haikuo: rule.type is required");
  if (type !== "video" && HAIKUO_READ_TYPES.has(type.toLowerCase())) {
    throw new Error(`haikuo v1 unsupported: read-type rules are not supported in v1 (video only); got type=${JSON.stringify(type)}`);
  }
  if (type !== "video") {
    throw new Error(`haikuo: unsupported rule type ${JSON.stringify(type)} (v1 supports video only)`);
  }
  let pagesRaw = raw.pages;
  let pages = [];
  if (pagesRaw !== undefined && pagesRaw !== null && pagesRaw !== "") {
    // 幂等：已解析过的规则对象里 pages 是数组（createHaikuoSession 会重调
    // 本函数），直接复用；原始 JSON 里 pages 是字符串，需二次解析。
    if (typeof pagesRaw === "string") {
      try { pages = JSON.parse(pagesRaw); }
      catch (e) { throw new Error("haikuo: rule.pages is not valid JSON: " + (e && e.message || e)); }
    } else if (Array.isArray(pagesRaw)) {
      pages = pagesRaw;
    } else {
      throw new Error("haikuo: rule.pages must be a JSON string or an array");
    }
    if (!Array.isArray(pages)) throw new Error("haikuo: rule.pages must parse to an array");
  }
  const normPages = pages.map((p, i) => {
    if (!p || typeof p !== "object") throw new Error(`haikuo: pages[${i}] must be an object`);
    const pth = String(p.path ?? "").trim();
    if (!pth) throw new Error(`haikuo: pages[${i}].path is required`);
    const code = String(p.code ?? p.rule ?? p.js ?? "");
    if (!code) throw new Error(`haikuo: pages[${i}].rule is empty (path=${pth})`);
    return { path: pth, name: String(p.name ?? ""), col_type: String(p.col_type ?? ""), code };
  });
  // class_name/class_url 按 && 对齐切分（草榴 BT：7 个分类；五条短剧规则为空）
  const names = String(raw.class_name ?? "").split("&&").map((s) => s.trim());
  const urls = String(raw.class_url ?? "").split("&&").map((s) => s.trim());
  const classes = [];
  const n = Math.max(names.length, urls.length);
  for (let i = 0; i < n; i++) {
    const name = names[i] ?? "";
    const url = urls[i] ?? "";
    if (!name && !url) continue;
    classes.push({ name, url });
  }
  return {
    title,
    author: String(raw.author ?? ""),
    version: String(raw.version ?? ""),
    group: String(raw.group ?? ""),
    type,
    ua: String(raw.ua ?? ""),
    url: String(raw.url ?? ""),
    class_name: String(raw.class_name ?? ""),
    class_url: String(raw.class_url ?? ""),
    classes,
    col_type: String(raw.col_type ?? ""),
    detail_col_type: String(raw.detail_col_type ?? ""),
    find_rule: String(raw.find_rule ?? ""),
    search_url: String(raw.search_url ?? ""),
    searchFind: String(raw.searchFind ?? ""),
    detail_find_rule: String(raw.detail_find_rule ?? ""),
    preRule: String(raw.preRule ?? ""),
    pages: normPages,
    hasSearch: Boolean(String(raw.search_url ?? "").trim() || String(raw.searchFind ?? "").trim()),
    raw,
  };
}

// 占位替换：fyclass（分类 url）、fypage（页码）、fykey / **（关键词，URL 编码）。
// `hiker://empty` 表示不发请求（getResCode() 返回 ""），只当占位载体。
export function substituteHaikuoUrl(template, opts = {}) {
  let u = String(template ?? "");
  const kw = opts.keyword !== undefined && opts.keyword !== null ? String(opts.keyword) : "";
  const enc = encodeURIComponent(kw);
  u = u.split("**").join(enc).split("fykey").join(enc);
  if (opts.cls !== undefined && opts.cls !== null) u = u.split("fyclass").join(String(opts.cls));
  u = u.split("fypage").join(String(opts.page ?? 1));
  return u;
}

// 把 pages[] 全注册进 context（模块页与片段页都注册：fetch('hiker://page/x')
// 与 $.require('hiker://page/x') 都靠它找源码）。js: 前缀在注册时剥掉。
export function loadHaikuoRule(ctx, rule) {
  const modules = [];
  for (const p of rule.pages || []) {
    ctx.registerModule(p.path, stripJsPrefix(p.code));
    modules.push(p.path);
  }
  return { modules, pages: (rule.pages || []).map((p) => ({ path: p.path, name: p.name, col_type: p.col_type })) };
}

// ---------- 播放 token（不透明；Python 侧只当字符串透传） ----------
// token = "haikuo-play:" + base64url(JSON {v:1, k:ruleKey, kind, input, code})。
// 含原始 lazy/rule JS，但 base64url 编码后 Python 侧看不到明文规则代码；
// play 时在 sidecar 进程内解码、于该规则的新 context 里重算。
export function encodeHaikuoPlayToken(ruleKey, kind, input, code) {
  const payload = { v: 1, k: String(ruleKey || ""), kind, input: String(input ?? ""), code: String(code ?? "") };
  return HAIKUO_PLAY_TOKEN_PREFIX + Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
}
export function decodeHaikuoPlayToken(token) {
  const t = String(token ?? "");
  if (!t.startsWith(HAIKUO_PLAY_TOKEN_PREFIX)) return null;
  try {
    const payload = JSON.parse(Buffer.from(t.slice(HAIKUO_PLAY_TOKEN_PREFIX.length), "base64url").toString("utf8"));
    if (!payload || payload.v !== 1 || typeof payload.code !== "string" || typeof payload.input !== "string") return null;
    if (payload.kind !== "lazy" && payload.kind !== "rule") return null;
    return { ruleKey: String(payload.k ?? ""), kind: payload.kind, input: payload.input, code: payload.code };
  } catch { return null; }
}
export function isHaikuoPlayToken(u) {
  return String(u ?? "").startsWith(HAIKUO_PLAY_TOKEN_PREFIX);
}

// 搜索框等 UI 控件的 URL 本身就是一段 JS 函数表达式（Hiker 惯用法：
// 点击时执行函数、取返回值当 URL；函数体字符串里常含 "@rule=js:"）。
// 它不是可导航链接：切分 @rule=js: 后缀会切出残缺代码（`$.require(...)' ; }`），
// 故 tokenize 时原样放过；detail/play 遇到时给明确报错，不报编译错。
export function isHaikuoFunctionUrl(u) {
  return /^function\b/.test(String(u ?? "").trim());
}

// 列表里含 @lazyRule= / @rule=js: 的 URL 一律不在列表时执行，换成播放 token。
// ---------- URL 查询串 → MY_PARAMS ----------
// 影探4K 这类规则：20 部影片共用同一个详情页 URL
// （hiker://page/detail#...），真 id 在 extra.id 里；Python 侧把 id 拼进
// URL 查询串（?id=...）后，detail 须在跑页代码前把它解析进 MY_PARAMS，
// 否则规则读 MY_PARAMS.id / getParam('id') 拿不到 id，只会回“缺少影片参数”。
// 无查询串时返回 null（不碰已有的 MY_PARAMS，避免覆盖上游调用方预设的值）。
export function urlQueryParams(u) {
  const s = String(u ?? "");
  const qi = s.indexOf("?");
  if (qi < 0) return null;
  const hash = s.indexOf("#", qi);
  const query = s.slice(qi + 1, hash >= 0 ? hash : undefined);
  if (!query) return null;
  const params = {};
  for (const pair of query.split("&")) {
    if (!pair) continue;
    const eq = pair.indexOf("=");
    const k = decodeURIComponent((eq >= 0 ? pair.slice(0, eq) : pair).replace(/\+/g, " "));
    const v = eq >= 0 ? decodeURIComponent(pair.slice(eq + 1).replace(/\+/g, " ")) : "";
    if (k) params[k] = v;
  }
  return Object.keys(params).length ? params : null;
}

export function tokenizeHaikuoUrl(url, ruleKey) {
  const u = String(url ?? "");
  if (isHaikuoFunctionUrl(u)) return u;
  const li = u.indexOf("@lazyRule=");
  if (li >= 0) return encodeHaikuoPlayToken(ruleKey, "lazy", u.slice(0, li), u.slice(li + "@lazyRule=".length));
  const ri = u.indexOf("@rule=js:");
  if (ri >= 0) return encodeHaikuoPlayToken(ruleKey, "rule", u.slice(0, ri), u.slice(ri + "@rule=js:".length));
  return u;
}

// ---------- 播放结果分发 ----------
// 收到一条解析出的 URL 字符串，按后缀/协议决定：直链可播、磁力/下载不可播、
// video:// 与 x5Rule:// 是 v1 不支持（抛错）、普通 http(s) 页交 sniff。
export function dispatchHaikuoPlayResult(value) {
  const s = String(value ?? "").trim();
  if (!s) return { playable: false, reason: "empty play URL", action: "none", url: "" };
  if (s.startsWith("toast://")) {
    return { playable: false, reason: s.slice("toast://".length) || "toast", action: "toast", url: "" };
  }
  if (s.startsWith("video://")) {
    // video:// 后面通常是嗅探页/iframe 地址（规则里 r.frame / 播放页 URL）。
    // v1 直接抛错导致该规则整条不可播；这里改为交给嗅探器：普通 HTTP 抓取
    // 能解出 m3u8 就照常播，解不出再明确报不支持。
    const frame = s.slice("video://".length).trim();
    if (/^https?:\/\//i.test(frame)) {
      return { playable: false, sniff: true, url: frame, headers: {} };
    }
    throw new Error(`haikuo v1 unsupported: video play URLs need X5/WebView, which is not in v1`);
  }
  if (s.startsWith("x5Rule://")) {
    throw new Error(`haikuo v1 unsupported: ${s.slice(0, s.indexOf("://"))} play URLs need X5/WebView, which is not in v1`);
  }
  if (s.startsWith("magnet:")) return { playable: false, reason: "magnet/download not playable", action: "copy", url: s };
  if (s.startsWith("download://")) return { playable: false, reason: "magnet/download not playable", action: "display", url: s };
  // HikerView 头部写法：url;{K@V};{K2@V2}#isVideo=true#——先拆 fragment 与头部。
  const isVideoMarked = /#isVideo=true#/i.test(s);
  let url = s;
  const hashIdx = url.indexOf("#");
  if (hashIdx >= 0) url = url.slice(0, hashIdx);
  const headers = {};
  const headerRe = /;\{([^{}@]+)@([^{}]*)\}/g;
  let m;
  while ((m = headerRe.exec(url)) !== null) headers[m[1]] = m[2];
  url = url.replace(/;\{[^{}]*\}/g, "");
  const noQuery = url.split("?")[0].toLowerCase();
  if (noQuery.endsWith(".m3u8") || noQuery.endsWith(".mp4") || (isVideoMarked && /^https?:\/\//i.test(url))) {
    return { playable: true, url, headers };
  }
  // 本地清单（batch 4）：规则经 java.io 把改写后的 m3u8 写到 storeDir 后返回
  // file:// 路径；Python 侧校验后改走插件内 HTTP 端口（见 app._resolve_haikuo）。
  if (url.startsWith("file://") && noQuery.endsWith(".m3u8")) {
    return { playable: true, url, headers };
  }
  if (/^https?:\/\//i.test(url)) return { playable: false, sniff: true, url };
  if (url.startsWith("hiker://empty")) {
    return { playable: false, reason: "lazy rule returned a navigation URL, not a play URL", action: "none", url };
  }
  return { playable: false, reason: "unsupported play URL scheme", action: "none", url };
}

// 解析 @lazyRule= 之后的部分：[selector].js:<code>；selector 为空即 .js:<code>。
export function parseHaikuoLazyRest(rest) {
  const r = String(rest ?? "");
  const idx = r.indexOf(".js:");
  if (idx < 0) throw new Error("haikuo: unsupported @lazyRule form (missing .js:): " + r.slice(0, 80));
  return { selector: r.slice(0, idx), code: r.slice(idx + 4) };
}

// 在 ctx 里跑一段 lazy 码：先把 input 绑成全局（lazy 函数体 / .js: 代码都
// 靠它拿输入，如橙子 playLazy 里 JSON.parse(input)），再执行；全角分号
// ；；是海阔 lazy 码的多语句分隔符，先转回半角分号。
function evalHaikuoLazyCode(ctx, input, code) {
  let src = String(code ?? "").replace(/^\s*js:/i, "");
  src = src.split("；；").join(";").split("；").join(";");
  ctx.run("input = " + JSON.stringify(String(input ?? "")) + ";", "haikuo-lazy-input.js");
  return ctx.run(src, "haikuo-lazy.js");
}

// lazy 求值：selector 非空时先取回 base 页、用选择器抽出内容当 input
//（末段是 href/src 时用 pd 解析成绝对地址，否则 pdfh 取文本）；
// selector 为空时 input 就是 @lazyRule= 之前的原串。返回分发后的播放结果。
export function evaluateHaikuoLazy(ctx, input, rest) {
  const { selector, code } = parseHaikuoLazyRest(rest);
  let boundInput = String(input ?? "");
  if (selector) {
    const base = String(input ?? "");
    if (!/^https?:\/\//i.test(base)) {
      throw new Error("haikuo: lazyRule selector needs an http(s) base URL, got: " + base.slice(0, 80));
    }
    ctx.setPage({ myUrl: base });
    const html = ctx.run("fetch(" + JSON.stringify(base) + ")", "haikuo-lazy-fetch.js");
    const lastSeg = selector.split("&&").pop().trim();
    const fn = (lastSeg === "href" || lastSeg === "src") ? "pd" : "pdfh";
    boundInput = String(ctx.run(
      fn + "(" + JSON.stringify(String(html ?? "")) + "," + JSON.stringify(selector) + ")",
      "haikuo-lazy-select.js") ?? "");
  }
  const value = evalHaikuoLazyCode(ctx, boundInput, code);
  return dispatchHaikuoPlayResult(value);
}

// ---------- 非 JS 解析串解释器 ----------
// 纯解析串格式：list;title;pic;desc;url（; 分隔，&& 链式选择器）。
// title/desc 走 pdfh，pic/url 走 pd（相对地址按 MY_URL 补全）；
// 任一字段可带 `.js:` 后缀：先取选择器值，再以 input 绑定该值执行后缀代码，
// 返回值替换原值（后缀返回 undefined/null 时保留原值；选择器为空时
// input 即整段 item HTML）。
// 解析串解释入口：ctx 须已 setPage（pd 靠 MY_URL 补全相对地址）。
// 字段按位置选函数：pic/url 用 pd（补全相对地址），其余用 pdfh。
export function interpretHaikuoParseRule(ctx, ruleText, html, opts = {}) {
  const text = String(ruleText ?? "");
  if (!text.trim()) throw new Error("haikuo: empty parse rule");
  const segs = text.split(";").map((s) => s.trim());
  const [listSel = "", titleSel = "", picSel = "", descSel = "", urlSel = ""] = segs;
  const runField = (itemHtml, spec, fn) => {
    if (!spec) return "";
    const idx = spec.indexOf(".js:");
    let sel = spec;
    let js = "";
    if (idx >= 0) { sel = spec.slice(0, idx); js = spec.slice(idx + 4); }
    let value = "";
    if (sel) {
      value = String(ctx.run(
        fn + "(" + JSON.stringify(String(itemHtml)) + "," + JSON.stringify(sel) + ")",
        "haikuo-parse-field.js") ?? "");
    } else {
      value = String(itemHtml);
    }
    if (js) {
      const src = js.split("；；").join(";").split("；").join(";");
      ctx.run("input = " + JSON.stringify(value) + ";", "haikuo-parse-input.js");
      const r = ctx.run(src, "haikuo-parse-js.js");
      if (r !== undefined && r !== null) value = String(r);
    }
    return value;
  };
  let itemsHtml = [];
  if (!listSel) {
    itemsHtml = [String(html ?? "")];
  } else {
    const v = ctx.run(
      "pdfa(" + JSON.stringify(String(html ?? "")) + "," + JSON.stringify(listSel) + ")",
      "haikuo-parse-list.js");
    itemsHtml = JSON.parse(JSON.stringify(v ?? []));
  }
  const out = [];
  for (const itemHtml of itemsHtml) {
    out.push(normalizeHaikuoItem({
      title: runField(itemHtml, titleSel, "pdfh"),
      url: runField(itemHtml, urlSel, "pd"),
      desc: runField(itemHtml, descSel, "pdfh"),
      col_type: opts.colType || "",
      pic_url: runField(itemHtml, picSel, "pd"),
    }, opts.ruleKey || "haikuo", opts.colType || ""));
  }
  return out;
}

// 统一的结果归一化：setResult 的数组/单体/字符串都收成
// {title, url, desc, col_type, pic_url, extra}；url 里的 @lazyRule=/@rule=js:
// 换成播放 token；vm realm 的值经 JSON 往返转成宿主侧普通对象。
export function normalizeHaikuoItem(item, ruleKey, defaultColType) {
  if (typeof item === "string") item = { title: item };
  if (!item || typeof item !== "object") item = { title: String(item ?? "") };
  let plain;
  try { plain = JSON.parse(JSON.stringify(item)); }
  catch { plain = { title: String(item.title ?? "") }; }
  return {
    title: String(plain.title ?? plain.name ?? ""),
    url: tokenizeHaikuoUrl(String(plain.url ?? ""), ruleKey || "haikuo"),
    desc: String(plain.desc ?? ""),
    col_type: String(plain.col_type ?? defaultColType ?? ""),
    pic_url: String(plain.pic_url ?? plain.pic ?? plain.img ?? plain.imgUrl ?? ""),
    extra: (plain.extra && typeof plain.extra === "object" && !Array.isArray(plain.extra)) ? plain.extra : {},
  };
}

function collectHaikuoResult(ctx, runValue, ruleKey, defaultColType) {
  let raw = ctx.state.result;
  if ((raw === null || raw === undefined) && Array.isArray(runValue)) raw = runValue;
  if (raw === null || raw === undefined) {
    if (ctx.state.error) throw new Error("haikuo: " + ctx.state.error);
    return [];
  }
  const arr = Array.isArray(raw) ? raw : [raw];
  return arr.map((it) => normalizeHaikuoItem(it, ruleKey, defaultColType));
}

async function runMaybeAsync(v) {
  if (v && typeof v.then === "function") return await v;
  return v;
}

// ---------- 会话：一条规则的一次加载 + home/category/search/detail/play ----------
// home/search 前跑 preRule（MY_RULE 在建 context 时已注入）；
// 每次调用复用同一 context（getItem/putMyVar 等进程级存储本来就跨调用保留，
// find_rule 每次重跑、重定义 XJ/CZ 这类规则级全局）；
// play 每次建全新 context（token 里自带 lazy 码与输入，规则模块重注册）。
export function createHaikuoSession(ruleKey, ruleInput, opts = {}) {
  const rule = parseHaikuoRule(ruleInput);
  const key = String(ruleKey || rule.title || "haikuo");
  const storeDir = opts.storeDir;
  const logger = opts.logger;
  const uaOpt = (rule.ua && /Mozilla/.test(rule.ua)) ? rule.ua : undefined; // ua="mobile" 是关键字不是 UA 串，用默认移动 UA
  function newContext(page = {}) {
    const ctx = createHaikuoContext(key, {
      storeDir,
      logger,
      rule: rule.raw,
      ua: uaOpt,
      myUrl: page.myUrl || "",
      myPage: page.myPage || 1,
      myType: page.myType || "",
      myParams: page.myParams || {},
      myKeyword: page.myKeyword || "",
      myClassUrl: page.myClassUrl || "",
      myClassName: page.myClassName || "",
      syncReq: opts.syncReq,
    });
    loadHaikuoRule(ctx, rule);
    return ctx;
  }
  let ctx = newContext();
  const resetResult = (c) => { (c || ctx).state.result = null; (c || ctx).state.resultKind = ""; (c || ctx).state.error = ""; };
  const collect = (c, runValue, colType) => collectHaikuoResult(c, runValue, key, colType);
  function runPreRule() {
    if (rule.preRule && rule.preRule.trim()) ctx.run(rule.preRule, "haikuo-preRule.js");
  }
  async function runJsRule(code, page, colType) {
    ctx.setPage(page);
    resetResult();
    const value = await runMaybeAsync(ctx.run(code, "haikuo-rule.js"));
    return collect(ctx, value, colType);
  }
  async function runParseRule(code, page, colType) {
    ctx.setPage(page);
    resetResult();
    let html = "";
    const myUrl = String(page.myUrl || "");
    if (myUrl && !myUrl.startsWith("hiker://empty")) {
      html = String(await runMaybeAsync(ctx.run("fetch(" + JSON.stringify(myUrl) + ")", "haikuo-fetch.js")) ?? "");
    }
    return interpretHaikuoParseRule(ctx, code, html, { baseUrl: myUrl, colType, ruleKey: key });
  }
  function meta() {
    return {
      ruleKey: key,
      title: rule.title, type: rule.type, version: rule.version, group: rule.group,
      classes: rule.classes, hasSearch: rule.hasSearch,
      pages: rule.pages.map((p) => ({ path: p.path, name: p.name, col_type: p.col_type })),
    };
  }
  async function home(page = 1) {
    if (!rule.find_rule || !rule.find_rule.trim()) throw new Error(`haikuo: rule '${rule.title}' has no find_rule`);
    const myUrl = substituteHaikuoUrl(rule.url, { page });
    runPreRule();
    const pg = { myUrl, myPage: page, myType: "", myKeyword: "" };
    if (isHaikuoJsRule(rule.find_rule)) return runJsRule(rule.find_rule, pg, rule.col_type);
    return runParseRule(rule.find_rule, pg, rule.col_type);
  }
  async function category(cls, page = 1) {
    if (!rule.classes.length) {
      throw new Error(`haikuo: rule '${rule.title}' has no classes (categories come from home items)`);
    }
    const name = String(cls ?? "");
    const found = rule.classes.find((c) => c.name === name || c.url === name);
    const use = found || { name, url: name };
    const myUrl = substituteHaikuoUrl(rule.url, { cls: use.url, page });
    runPreRule();
    const pg = { myUrl, myPage: page, myClassUrl: use.url, myClassName: use.name };
    if (isHaikuoJsRule(rule.find_rule)) return runJsRule(rule.find_rule, pg, rule.col_type);
    return runParseRule(rule.find_rule, pg, rule.col_type);
  }
  async function search(keyword, page = 1) {
    if (!rule.searchFind && !rule.search_url) {
      throw new Error(`haikuo: rule '${rule.title}' has no search`);
    }
    const myUrl = substituteHaikuoUrl(rule.search_url || rule.url, { keyword, page });
    runPreRule();
    const pg = { myUrl, myPage: page, myType: "search", myKeyword: String(keyword ?? "") };
    if (isHaikuoJsRule(rule.searchFind)) return runJsRule(rule.searchFind, pg, rule.col_type);
    if (rule.searchFind && rule.searchFind.trim()) return runParseRule(rule.searchFind, pg, rule.col_type);
    throw new Error(`haikuo: rule '${rule.title}' has no searchFind`);
  }
  async function detail(url) {
    const u = String(url ?? "");
    if (!u) throw new Error("haikuo: detail url is empty");
    if (isHaikuoFunctionUrl(u)) throw new Error("haikuo v1: function-expression URLs are UI handlers (e.g. the search box), not a video/detail link");
    const tok = decodeHaikuoPlayToken(u);
    if (tok) {
      if (tok.kind === "lazy") throw new Error("haikuo: lazy play token is not a detail link (use play)");
      if (isHaikuoFunctionUrl(tok.input)) throw new Error("haikuo v1: function-expression URLs are UI handlers (e.g. the search box), not a video/detail link");
      const tokParams = urlQueryParams(tok.input);
      ctx.setPage({ myUrl: tok.input, myParams: tokParams || {} });
      resetResult();
      const value = await runMaybeAsync(ctx.run(tok.code, "haikuo-detail-rule.js"));
      return collect(ctx, value, rule.detail_col_type || rule.col_type);
    }
    const ri = u.indexOf("@rule=js:");
    if (ri >= 0) {
      const base = u.slice(0, ri);
      const ruleParams = urlQueryParams(base);
      ctx.setPage({ myUrl: base, myParams: ruleParams || {} });
      resetResult();
      const value = await runMaybeAsync(ctx.run(u.slice(ri + "@rule=js:".length), "haikuo-detail-rule.js"));
      return collect(ctx, value, rule.detail_col_type || rule.col_type);
    }
    if (u.startsWith("hiker://page/")) {
      const name = u.slice("hiker://page/".length).split(/[?#]/)[0];
      const pg = rule.pages.find((p) => p.path === name);
      if (!pg) throw new Error("haikuo: unknown detail page: " + name);
      const pageParams = urlQueryParams(u);
      ctx.setPage({ myUrl: u, myParams: pageParams || {} });
      resetResult();
      const value = await runMaybeAsync(ctx.run(pg.code, "haikuo-detail-page.js"));
      return collect(ctx, value, rule.detail_col_type || rule.col_type);
    }
    const detailParams = urlQueryParams(u);
    ctx.setPage({ myUrl: u, myParams: detailParams || {} });
    if (rule.detail_find_rule && rule.detail_find_rule.trim()) {
      if (isHaikuoJsRule(rule.detail_find_rule)) {
        resetResult();
        const value = await runMaybeAsync(ctx.run(rule.detail_find_rule, "haikuo-detail-find.js"));
        return collect(ctx, value, rule.detail_col_type || rule.col_type);
      }
      return runParseRule(rule.detail_find_rule, { myUrl: u, myPage: 1 }, rule.detail_col_type || rule.col_type);
    }
    const dp = rule.pages.find((p) => HAIKUO_DETAIL_PAGE_PATHS.includes(p.path));
    if (dp) {
      resetResult();
      const value = await runMaybeAsync(ctx.run(dp.code, "haikuo-detail-page.js"));
      return collect(ctx, value, rule.detail_col_type || rule.col_type);
    }
    if (isHaikuoJsRule(rule.find_rule)) {
      // 有些规则的 find_rule 按 MY_URL 分发详情分支；最后兜底跑它。
      resetResult();
      const value = await runMaybeAsync(ctx.run(rule.find_rule, "haikuo-detail-find.js"));
      return collect(ctx, value, rule.detail_col_type || rule.col_type);
    }
    throw new Error(`haikuo: rule '${rule.title}' has no detail rule (no detail_find_rule, no erji/de page)`);
  }
  async function play(tokenOrUrl) {
    const u = String(tokenOrUrl ?? "");
    if (!u) throw new Error("haikuo: play url/token is empty");
    if (isHaikuoFunctionUrl(u)) throw new Error("haikuo v1: function-expression URLs are UI handlers (e.g. the search box), not a video/detail link");
    const tok = decodeHaikuoPlayToken(u);
    if (tok) {
      if (isHaikuoFunctionUrl(tok.input)) throw new Error("haikuo v1: function-expression URLs are UI handlers (e.g. the search box), not a video/detail link");
      const pctx = newContext({ myUrl: tok.input });
      if (tok.kind === "rule") {
        resetResult(pctx);
        const value = await runMaybeAsync(pctx.run(tok.code, "haikuo-play-rule.js"));
        if (typeof value === "string") return dispatchHaikuoPlayResult(value);
        if (typeof pctx.state.result === "string") return dispatchHaikuoPlayResult(pctx.state.result);
        return { playable: false, reason: "@rule token resolved to a page, not a play URL", action: "detail", url: tok.input };
      }
      return evaluateHaikuoLazy(pctx, tok.input, tok.code);
    }
    const li = u.indexOf("@lazyRule=");
    if (li >= 0) {
      const pctx = newContext({ myUrl: u.slice(0, li) });
      return evaluateHaikuoLazy(pctx, u.slice(0, li), u.slice(li + "@lazyRule=".length));
    }
    const ri = u.indexOf("@rule=js:");
    if (ri >= 0) {
      const pctx = newContext({ myUrl: u.slice(0, ri) });
      resetResult(pctx);
      const value = await runMaybeAsync(pctx.run(u.slice(ri + "@rule=js:".length), "haikuo-play-rule.js"));
      if (typeof value === "string") return dispatchHaikuoPlayResult(value);
      if (typeof pctx.state.result === "string") return dispatchHaikuoPlayResult(pctx.state.result);
      return { playable: false, reason: "@rule token resolved to a page, not a play URL", action: "detail", url: u.slice(0, ri) };
    }
    return dispatchHaikuoPlayResult(u);
  }
  // 分类导航：分类按钮的懒代码（putMyVar 设分类变量 + refreshPage +
  // return 'hiker://empty'）是有状态导航，不是播放。先在新上下文里求值
  // 懒代码（putMyVar 落到按 ruleKey 共享的存储），再在会话上下文里重取
  // home(page) 拿到该分类的影片，最后恢复导航前的 myVars 快照，以免
  // 默认首页被分类变量污染。若懒代码解出来是可播地址，说明这不是分类
  // 按钮，明确抛错（调用方按硬错误处理，不给占位）。
  function snapshotMyVars() {
    const json = ctx.run("JSON.stringify(listMyVarKeys().map(function (k) { return [k, getMyVar(k)]; }))", "haikuo-nav-snapshot.js");
    const pairs = JSON.parse(String(json || "[]"));
    return Array.isArray(pairs) ? pairs : [];
  }
  function restoreMyVars(saved) {
    const keys = JSON.parse(JSON.stringify(ctx.run("listMyVarKeys()", "haikuo-nav-keys.js") || []));
    const exprs = [];
    for (const k of keys) exprs.push("clearMyVar(" + JSON.stringify(k) + ");");
    for (const pair of saved) {
      const v = pair && pair[1] === undefined ? null : (pair ? pair[1] : null);
      exprs.push("putMyVar(" + JSON.stringify(pair ? pair[0] : "") + "," + JSON.stringify(v) + ");");
    }
    if (exprs.length) ctx.run(exprs.join(""), "haikuo-nav-restore.js");
  }
  async function navigate(token, page = 1) {
    const t = String(token ?? "");
    if (!t) throw new Error("haikuo: navigate token is empty");
    const saved = snapshotMyVars();
    try {
      const res = await play(t);
      if (res && res.playable) throw new Error("haikuo: navigation button resolved to a play URL, not a category");
      return await home(page);
    } finally {
      try { restoreMyVars(saved); } catch { /* 恢复尽力而为，不掩盖主错误 */ }
    }
  }
  return {
    ruleKey: key,
    rule,
    meta,
    load: () => ({ ...meta(), modules: ctx.listModules() }),
    home, category, search, detail, play, navigate,
    context: () => ctx,
  };
}

export default createHaikuoContext;
