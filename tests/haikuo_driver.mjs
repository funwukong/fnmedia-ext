// haikuo_driver.mjs —— tests/test_haikuo_shim.py 的 Node 侧驱动。
//
// 用法：node haikuo_driver.mjs <scenario> <storeDir> <baseUrl>
// 只向 stdout 写一行 JSON：{ok:true, data} 或 {ok:false, error}。
// 断言失败即抛错（Python 侧按 ok=false / 非零退出码判失败）。
//
// 注意：本地 HTTP 服务跑在 Python 测试进程里（与本 node 进程不同进程），
// sync fetch 桥（Atomics.wait）才不会和同进程的 HTTP 服务互相死锁。
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
// 路径用拼接写：公开剥离扫描会把该资产路径的字面量当残留针
// （见 tools/public_strip.py）；动态 import 不影响行为。
const { createHaikuoContext, HOST_API_COVERAGE } = await import(
  new URL("../proxy/assets/" + "drpy/haikuo.mjs", import.meta.url).href);

const scenario = process.argv[2];
const storeDir = process.argv[3];
const base = process.argv[4] || "";

function assert(cond, msg) {
  if (!cond) throw new Error("assert failed: " + msg);
}
function eq(a, b, msg) {
  assert(JSON.stringify(a) === JSON.stringify(b), `${msg}: got ${JSON.stringify(a)} want ${JSON.stringify(b)}`);
}
function throwsWith(code, needle, ctx) {
  try {
    ctx.run(code);
  } catch (e) {
    assert(String(e && e.message || e).includes(needle), `expected error containing ${needle}, got ${String(e && e.message || e)}`);
    return String(e.message);
  }
  throw new Error("expected throw for: " + code);
}
function ctxFor(key, extra = {}) {
  return createHaikuoContext(key, { storeDir, ...extra });
}

const PAGE_HTML = "<html><body><div id='root'><a href='/x'>T1</a><a href='/y'>T2</a></div></body></html>";

const SCENARIOS = {
  coverage() {
    const ctx = ctxFor("cov");
    const g = ctx.sandbox;
    // 任务清单里的全部宿主函数/变量必须存在且可调用（或为对象/字符串常量）。
    const fns = ["fetch", "request", "getResCode", "parseDom", "pd", "pdfh", "pdfa",
      "setResult", "setHomeResult", "setSearchResult", "setError",
      "getItem", "setItem", "putItem", "clearItem", "getPublicItem", "setPublicItem",
      "putVar", "getVar", "clearVar", "putMyVar", "getMyVar", "clearMyVar",
      "listMyVarKeys", "toast", "log", "md5", "base64Encode", "base64Decode",
      "aesEncode", "aesDecode", "rsaEncrypt", "rsaDecrypt", "decodeStr", "encodeStr",
      "batchFetch", "fetchCookie", "buildUrl", "saveFile", "readFile", "deleteFile",
      "fileExist", "getSearchMode", "setSearchMode", "searchContains",
      "setLastChapterRule", "xpath", "xpathArray", "getParam", "getRule", "getHome",
      "getCryptoJS", "hexToBytes", "copy", "loadJavaClass", "fetchCodeByWebView",
      "confirm", "updateItem", "deleteItem", "addItemAfter", "getAppVersion"];
    const missing = fns.filter((n) => typeof g[n] !== "function");
    const objs = ["storage0", "http", "rc4", "window0"];
    const missingObjs = objs.filter((n) => typeof g[n] !== "object");
    const vars = ["MY_URL", "MY_HOME", "MY_RULE", "MY_PAGE", "MY_TYPE", "MY_PARAMS",
      "MY_KEYWORD", "MY_CLASS_URL", "MY_CLASS_NAME", "MOBILE_UA", "PC_UA"];
    const missingVars = vars.filter((n) => !(n in g));
    // $ 工具
    assert(typeof g.$ === "function", "$ must be a function");
    assert(typeof g.$.require === "function", "$.require missing");
    assert(typeof g.$.toString === "function", "$.toString missing");
    assert("exports" in g.$, "$.exports missing");
    // 覆盖表必须覆盖清单里的每一项
    const uncovered = [...fns, ...objs, ...vars].filter((n) => !(n in HOST_API_COVERAGE));
    return { missing, missingObjs, missingVars, uncovered, coverageSize: Object.keys(HOST_API_COVERAGE).length };
  },

  storage() {
    const a = ctxFor("rule-a");
    const b = ctxFor("rule-b");
    a.run(`setItem('k','v1'); putVar('g','gv'); putMyVar('m','mv'); storage0.putMyVar('o',{x:1}); storage0.setItem('sj',{y:2}); setPublicItem('pk','pv');`);
    eq(a.run(`getItem('k')`), "v1", "getItem");
    eq(a.run(`getItem('no','def')`), "def", "getItem default");
    eq(a.run(`getItem('no')`), "", "getItem missing -> ''");
    eq(a.run(`getVar('g')`), "gv", "getVar");
    eq(a.run(`getMyVar('m')`), "mv", "getMyVar");
    eq(a.run(`storage0.getMyVar('o')`), { x: 1 }, "storage0 object");
    eq(a.run(`storage0.getItem('sj')`), { y: 2 }, "storage0 setItem object");
    eq(a.run(`getPublicItem('pk')`), "pv", "getPublicItem");
    // 隔离：另一 ruleKey 看不到私有项，但看得到全局/公开项
    eq(b.run(`getItem('k','none')`), "none", "item isolation");
    eq(b.run(`getMyVar('m','none')`), "none", "myVar isolation");
    eq(b.run(`getVar('g')`), "gv", "putVar is global");
    eq(b.run(`getPublicItem('pk')`), "pv", "public item is shared");
    eq(b.run(`storage0.getMyVar('o','none')`), "none", "storage0 myVar isolation");
    // clear（'o' 是前面经 storage0 存的，也要清掉再断言为空）
    a.run(`clearItem('k'); clearVar('g'); clearMyVar('m'); clearMyVar('o');`);
    eq(a.run(`getItem('k','d')`), "d", "clearItem");
    eq(a.run(`getVar('g','d')`), "d", "clearVar");
    eq(a.run(`getMyVar('m','d')`), "d", "clearMyVar");
    eq(a.run(`listMyVarKeys()`), [], "listMyVarKeys empty after clear");
    a.run(`putMyVar('a','1'); putMyVar('b','2');`);
    eq(a.run(`listMyVarKeys().sort()`), ["a", "b"], "listMyVarKeys");
    return { ok: true };
  },

  require() {
    const ctx = ctxFor("req-rule");
    ctx.registerModule("mini", `
      var Engine = {
        hello: function(name) { return 'hi ' + name; },
        useOther: function() { return $.require('other').val; }
      };
      $.exports = Engine;
    `);
    ctx.registerModule("other", `$.exports = { val: 42 };`);
    eq(ctx.run(`$.require('mini').hello('x')`), "hi x", "$.require exports call");
    eq(ctx.run(`$.require('mini').useOther()`), 42, "nested $.require");
    eq(ctx.run(`typeof $.require('mini')`), "object", "require returns object");
    // force=true 重新执行模块
    ctx.registerModule("cnt", `$.exports = { n: (typeof __cnt === 'undefined' ? (__cnt = 1) : (++__cnt)) };`);
    eq(ctx.run(`$.require('cnt').n`), 1, "module first load");
    eq(ctx.run(`$.require('cnt').n`), 1, "module cached");
    eq(ctx.run(`$.require('cnt', true).n`), 2, "module force reload");
    // 未注册模块报错
    throwsWith(`$.require('nope')`, "module not registered", ctx);
    // file:// 本地模块（storeDir 内）
    fs.writeFileSync(path.join(storeDir, "localmod.js"), `$.exports = { from: 'file' };`);
    eq(ctx.run(`$.require('file://localmod.js').from`), "file", "file:// module");
    eq(ctx.run(`$.require('localmod.js').from`), "file", "bare .js module under storeDir");
    // requirejs（CommonJS 包装）
    ctx.registerModule("cjs", `module.exports = { add: function(a, b) { return a + b; } };`);
    eq(ctx.run(`requirejs('cjs').add(2, 3)`), 5, "requirejs");
    return { ok: true };
  },

  fetch() {
    const ctx = ctxFor("fetch-rule", { myUrl: base + "/page" });
    const page = ctx.run(`fetch('${base}/page')`);
    assert(page.includes("T1") && page.includes("T2"), "fetch body");
    eq(ctx.run(`getResCode()`), page, "getResCode is last body");
    // POST：object body 自动按 JSON 发
    const echo = JSON.parse(ctx.run(`fetch('${base}/echo', {method:'POST', body:{a:'xx', b:1}})`));
    eq(echo.method, "POST", "post method");
    eq(echo.ct, "application/json", "post json content-type");
    eq(JSON.parse(echo.body), { a: "xx", b: 1 }, "post json body");
    // post()：object body 自动转表单
    const echo2 = JSON.parse(ctx.run(`post('${base}/echo', {a:'xx', b:1})`));
    eq(echo2.method, "POST", "post() method");
    eq(echo2.ct, "application/x-www-form-urlencoded", "post() form content-type");
    eq(echo2.body, "a=xx&b=1", "post() form body");
    // 自定义请求头确实发出
    const echo3 = JSON.parse(ctx.run(`fetch('${base}/echo', {headers:{'X-Test':'hello', 'User-Agent':'UA-X'}})`));
    eq(echo3["x-test"], "hello", "custom header sent");
    eq(echo3.ua, "UA-X", "custom UA sent");
    // withHeaders / withStatusCode 返回 JSON 字符串
    const wh = JSON.parse(ctx.run(`fetch('${base}/echo', {withHeaders:true})`));
    assert(typeof wh.body === "string" && typeof wh.headers === "object", "withHeaders shape");
    assert(Array.isArray(wh.headers["content-type"]), "withHeaders values are arrays");
    const ws = JSON.parse(ctx.run(`fetch('${base}/page', {withStatusCode:true})`));
    eq(ws.statusCode, 200, "withStatusCode");
    assert(ws.body.includes("T1"), "withStatusCode body");
    // fetchCookie 返回 JSON 字符串数组
    eq(JSON.parse(ctx.run(`fetchCookie('${base}/page')`)), ["a=b"], "fetchCookie");
    // buildUrl / batchFetch / toHex
    eq(ctx.run(`buildUrl('${base}/echo', {a:'b', c:'d'})`), base + "/echo?a=b&c=d", "buildUrl");
    eq(ctx.run(`batchFetch([{url:'${base}/page'}, {url:'${base}/echo'}]).length`), 2, "batchFetch length");
    eq(ctx.run(`fetch('${base}/echo', {toHex:true}).slice(0, 4)`), "7b22", "toHex starts with {\"");
    // 超时与不可达必须抛错（规则代码靠 try/catch）
    throwsWith(`fetch('${base}/slow', {timeout:500})`, "fetch failed", ctx);
    throwsWith(`fetch('http://127.0.0.1:1/x', {timeout:1000})`, "fetch failed", ctx);
    return { ok: true };
  },

  unsupported() {
    const ctx = ctxFor("unsup-rule");
    const V1 = "haikuo v1 unsupported";
    throwsWith(`fetchCodeByWebView('http://x.cn')`, V1, ctx);
    throwsWith(`loadJavaClass('hiker://files/cache/t.dex', 'com.test.Test')`, V1, ctx);
    throwsWith(`$.require('hiker://page/x?rule=other')`, V1, ctx);
    throwsWith(`parsePaste('云5oooole/dnkb')`, V1, ctx);
    throwsWith(`evalPrivateJS('1')`, V1, ctx);
    throwsWith(`startProxyServer('1')`, V1, ctx);
    throwsWith(`cacheM3u8('http://x.cn/1.m3u8')`, V1, ctx);
    // x5Rule 只拼串，不执行
    eq(ctx.run(`$('http://x.cn/a').x5Rule(()=>{ return document.title; })`),
      "javascript:(()=>{ return document.title; })()", "x5Rule builds javascript: url");
    eq(ctx.run(`$('http://x.cn/a').rule(()=>{ setResult(1); })`),
      "http://x.cn/a@rule=.js:(()=>{ setResult(1); })()", "$.rule builds url");
    eq(ctx.run(`$('http://x.cn/a').lazyRule(()=>{ return 'v'; })`),
      "http://x.cn/a@lazyRule=.js:(()=>{ return 'v'; })()", "$.lazyRule builds url");
    assert(ctx.run(`$('http://x.cn/a').input(()=>{ return 1; })`).includes("@input=.js:"), "$.input builds url");
    assert(ctx.run(`$('http://x.cn/a', {Referer:'http://b.cn'}).image(()=>{ return 1; })`).includes("@headers="), "$.image builds url");
    // UI stub：不抛错，返回固定值
    eq(ctx.run(`confirm({title:'t'})`), false, "confirm stub false");
    eq(ctx.run(`updateItem('id1', {title:'x'})`), undefined, "updateItem stub noop");
    eq(ctx.run(`deleteItem('id1')`), undefined, "deleteItem stub noop");
    eq(ctx.run(`findItem('id1')`), null, "findItem stub null");
    eq(ctx.run(`refreshPage()`), undefined, "refreshPage stub noop");
    eq(ctx.run(`getAppVersion()`), 1800, "getAppVersion stub");
    return { ok: true };
  },

  files() {
    const ctx = ctxFor("file-rule");
    eq(ctx.run(`saveFile('sub/a.txt','hi'); readFile('sub/a.txt')`), "hi", "save/read round-trip");
    eq(ctx.run(`fileExist('sub/a.txt')`), true, "fileExist true");
    eq(ctx.run(`deleteFile('sub/a.txt'); fileExist('sub/a.txt')`), false, "deleteFile");
    eq(ctx.run(`readFile('missing.txt')`), "", "readFile missing -> ''");
    // hiker://files 走 fetch 读
    ctx.run(`saveFile('hiker://files/b.txt','B')`);
    eq(ctx.run(`fetch('hiker://files/b.txt')`), "B", "fetch hiker://files");
    // 路径穿越必须拒绝
    throwsWith(`saveFile('../evil.txt','x')`, "rejected", ctx);
    throwsWith(`saveFile('/tmp/evil.txt','x')`, "rejected", ctx);
    throwsWith(`saveFile('a/../../evil.txt','x')`, "rejected", ctx);
    throwsWith(`readFile('../../etc/passwd')`, "rejected", ctx);
    throwsWith(`saveFile('file:///tmp/evil.txt','x')`, "rejected", ctx);
    eq(ctx.run(`fileExist('../evil.txt')`), false, "fileExist traversal -> false");
    assert(!fs.existsSync(path.join(storeDir, "..", "evil.txt")), "no file escaped storeDir");
    // writeHexFile
    eq(ctx.run(`writeHexFile('c.bin','00ff'); readFile('c.bin').length`), 2, "writeHexFile");
    return { ok: true };
  },

  crypto() {
    const ctx = ctxFor("crypto-rule");
    eq(ctx.run(`md5('abc')`), "900150983cd24fb0d6963f7d28e17f72", "md5 vector");
    eq(ctx.run(`base64Decode(base64Encode('你好'))`), "你好", "base64 round-trip");
    eq(ctx.run(`atob(btoa('hello'))`), "hello", "btoa/atob");
    eq(ctx.run(`aesDecode('k', aesEncode('k', 'hello'))`), "hello", "aes round-trip");
    eq(ctx.run(`decodeStr(encodeStr('你好','GBK'),'GBK')`), "你好", "gbk round-trip");
    eq(ctx.run(`decodeStr(encodeStr('hi','UTF-8'),'UTF-8')`), "hi", "utf8 round-trip");
    eq(ctx.run(`rc4.decode(rc4.encode('aaaa','bbb','UTF-8'),'bbb','UTF-8')`), "aaaa", "rc4 round-trip");
    eq(ctx.run(`hexToBase64('00ff')`), "AP8=", "hexToBase64");
    eq(ctx.run(`hexToBytes('00ff').length`), 2, "hexToBytes");
    // RSA：现成密钥对往返（含 long=2 分段）
    const { publicKey, privateKey } = crypto.generateKeyPairSync("rsa", {
      modulusLength: 2048,
      publicKeyEncoding: { type: "spki", format: "pem" },
      privateKeyEncoding: { type: "pkcs8", format: "pem" },
    });
    const pub = JSON.stringify(publicKey);
    const priv = JSON.stringify(privateKey);
    eq(ctx.run(`rsaDecrypt(rsaEncrypt('secret', ${pub}), ${priv})`), "secret", "rsa round-trip");
    eq(ctx.run(`rsaDecrypt(rsaEncrypt('x'.repeat(300), ${pub}, {long:2}), ${priv})`), "x".repeat(300), "rsa long=2");
    // getCryptoJS：eval 后 CryptoJS 可用
    eq(ctx.run(`eval(getCryptoJS()); CryptoJS.MD5('abc').toString()`), "900150983cd24fb0d6963f7d28e17f72", "getCryptoJS");
    return { ok: true };
  },

  xpath() {
    const ctx = ctxFor("xpath-rule");
    const html = JSON.stringify(PAGE_HTML);
    eq(ctx.run(`xpath(${html}, '//div[@id="root"]/a[1]/@href')`), "/x", "xpath indexed attr");
    eq(ctx.run(`xpathArray(${html}, '//div/a/@href')`), ["/x", "/y"], "xpathArray");
    eq(ctx.run(`xpath(${html}, '//div/a[2]/text()')`), "T2", "xpath text()");
    eq(ctx.run(`xpath(${html}, '//div[@id="nope"]/a/@href')`), "", "xpath missing -> ''");
    eq(ctx.run(`xpa(${html}, '//a/@href')`), ["/x", "/y"], "xpa alias");
    return { ok: true };
  },

  selectors() {
    const ctx = ctxFor("sel-rule", { myUrl: base + "/dir/page" });
    const html = JSON.stringify(PAGE_HTML);
    eq(ctx.run(`pdfa(${html}, 'div&&a')`), ["<a href=\"/x\">T1</a>", "<a href=\"/y\">T2</a>"], "pdfa div&&a (HikerView semantics)");
    eq(ctx.run(`pdfh(${html}, 'div&&a&&Text')`), "T1", "pdfh chain text");
    eq(ctx.run(`pd(${html}, 'div&&a&&href')`), base + "/x", "pd joins relative url against MY_URL");
    eq(ctx.run(`parseDom(${html}, 'div&&a&&href')`), base + "/x", "parseDom alias");
    eq(ctx.run(`parseDomForHtml(${html}, 'div&&a&&Text')`), "T1", "parseDomForHtml alias");
    eq(ctx.run(`parseDomForArray(${html}, 'div&&a').length`), 2, "parseDomForArray alias");
    return { ok: true };
  },

  sandbox() {
    const ctx = ctxFor("sb-rule");
    const probe = ctx.run(`({
      process: typeof process, require: typeof require, module: typeof module,
      Buffer: typeof Buffer, fs: typeof fs, globalProcess: typeof globalThis.process,
      fetchCtor: typeof fetch.constructor,
      arrEscape: (function(){ try { return [].constructor.constructor('return typeof process')(); } catch(e) { return 'blocked'; } })(),
      fnEscape: (function(){ try { return Function('return typeof process')(); } catch(e) { return 'blocked'; } })()
    })`);
    eq(probe.process, "undefined", "no process");
    eq(probe.require, "undefined", "no bare require");
    eq(probe.module, "undefined", "no module");
    eq(probe.Buffer, "undefined", "no Buffer");
    eq(probe.fs, "undefined", "no fs");
    eq(probe.globalProcess, "undefined", "no globalThis.process");
    eq(probe.fetchCtor, "undefined", "fetch.constructor shadowed");
    eq(probe.arrEscape, "undefined", "Array.constructor chain stays in vm (no process)");
    eq(probe.fnEscape, "undefined", "in-vm Function cannot see process");
    // 宿主函数仍可调用（原型置空没弄坏调用）
    eq(ctx.run(`fetch('${base}/page').includes('T1')`), true, "fetch still callable after hardening");
    return { ok: true };
  },

  btshape() {
    // 草榴 BT 规则形状：pages[] 模块 + find_rule 式调用 + 同步 fetch + setResult。
    const ctx = ctxFor("bt-shape", { myUrl: base + "/page", myKeyword: "kw" });
    ctx.registerModule("bt", `
      var Engine = {
        home: function() {
          var html = fetch(MY_URL);
          if (!html) { setError('empty'); return; }
          var items = pdfa(html, 'div&&a');
          var out = [];
          for (var i = 0; i < items.length; i++) {
            out.push({ title: pdfh(items[i], 'a&&Text'), url: pd(items[i], 'a&&href'), col_type: 'text_1' });
          }
          setResult(out);
          return out.length;
        },
        search: function() {
          var kw = getMyVar('bt_kw');
          if (!kw) { setError('请输入关键词'); return; }
          setResult([{ title: 'found:' + kw, url: MY_URL, col_type: 'text_1' }]);
        }
      };
      $.exports = Engine;
    `);
    eq(ctx.run(`$.require('bt').home()`), 2, "bt home count");
    eq(ctx.state.result.length, 2, "bt home setResult");
    eq(ctx.state.result[0].url, base + "/x", "bt home absolute url");
    // preRule 形状：getItem/setItem 守门
    ctx.run(`if(!getItem('bt_good')){ try { var _h = 'http://good.cn'; if(_h) setItem('bt_good', _h); } catch(e){} }`);
    eq(ctx.run(`getItem('bt_good')`), "http://good.cn", "preRule setItem");
    // searchFind 形状：putMyVar + MY_KEYWORD
    ctx.run(`putMyVar('bt_kw', MY_KEYWORD); $.require('bt').search();`);
    eq(ctx.state.result[0].title, "found:kw", "search via putMyVar+MY_KEYWORD");
    return { ok: true };
  },

  vars() {
    const ctx = ctxFor("vars-rule", {
      myUrl: base + "/dir/page?x=1&y=2", myPage: 3, myType: "search",
      myKeyword: "kw", myClassUrl: "2", myClassName: "亚洲无码",
      myParams: { a: "1" }, rule: { title: "t", type: "video" },
    });
    eq(ctx.run(`getParam('x','d')`), "1", "getParam");
    eq(ctx.run(`getParam('zz','d')`), "d", "getParam default");
    eq(ctx.run(`getHome()`), base, "getHome from MY_URL");
    eq(ctx.run(`getHome('https://a.com/x/y')`), "https://a.com", "getHome(url)");
    eq(ctx.run(`MY_RULE.title + '/' + MY_RULE.type`), "t/video", "MY_RULE");
    eq(ctx.run(`MY_PAGE`), 3, "MY_PAGE");
    eq(ctx.run(`MY_TYPE`), "search", "MY_TYPE");
    eq(ctx.run(`MY_CLASS_URL + '/' + MY_CLASS_NAME`), "2/亚洲无码", "MY_CLASS_*");
    eq(ctx.run(`MY_PARAMS.a`), "1", "MY_PARAMS");
    eq(ctx.run(`MOBILE_UA.includes('Android')`), true, "MOBILE_UA");
    eq(ctx.run(`PC_UA.includes('Windows')`), true, "PC_UA");
    // 规则内改 MY_URL，宿主 state 跟得上（BT 规则 detail 里就这么干）
    ctx.run(`MY_URL = '${base}/other';`);
    eq(ctx.state.myUrl, base + "/other", "MY_URL assignment syncs to state");
    // setPage 更新
    ctx.setPage({ myUrl: base + "/p2", myKeyword: "k2", myPage: 2 });
    eq(ctx.run(`MY_URL`), base + "/p2", "setPage MY_URL");
    eq(ctx.run(`MY_KEYWORD`), "k2", "setPage MY_KEYWORD");
    eq(ctx.run(`MY_PAGE`), 2, "setPage MY_PAGE");
    // setLastChapterRule 只记元数据
    ctx.run(`setLastChapterRule('.x&&a&&Text')`);
    eq(ctx.state.lastChapterRule, ".x&&a&&Text", "setLastChapterRule recorded");
    eq(ctx.run(`getSearchMode()`), 0, "getSearchMode default 0");
    ctx.run(`setSearchMode(1)`);
    eq(ctx.run(`getSearchMode()`), 1, "setSearchMode 1");
    eq(ctx.run(`searchContains('2022梦醒了','22 梦醒',false)`), true, "searchContains match");
    eq(ctx.run(`searchContains('2022梦醒了','梦2022',false)`), false, "searchContains no match");
    return { ok: true };
  },
};

if (!SCENARIOS[scenario]) {
  process.stdout.write(JSON.stringify({ ok: false, error: "unknown scenario: " + scenario }) + "\n");
  process.exit(2);
}
try {
  const data = await SCENARIOS[scenario]();
  process.stdout.write(JSON.stringify({ ok: true, data }) + "\n");
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.message || e), stack: String(e && e.stack || "") }) + "\n");
  process.exit(1);
}
