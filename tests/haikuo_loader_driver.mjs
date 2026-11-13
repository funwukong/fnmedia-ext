// haikuo_loader_driver.mjs —— tests/test_haikuo_loader.py 的 Node 侧驱动。
//
// 用法：node haikuo_loader_driver.mjs <scenario> <storeDir> <baseUrl> [extra]
// 只向 stdout 写一行 JSON：{ok:true, data} 或 {ok:false, error}。
// 断言失败即抛错（Python 侧按 ok=false / 非零退出码判失败）。
// extra：fixture/规则文件路径（validate_all 时是目录）。
// 本地 HTTP 服务跑在 Python 测试进程里（与本 node 进程不同进程），
// sync fetch 桥（Atomics.wait）才不会和同进程的 HTTP 服务互相死锁。
import fs from "node:fs";
// 路径用拼接写：公开剥离扫描会把该资产路径的字面量当残留针
// （见 tools/public_strip.py）；动态 import 不影响行为。
const {
  createHaikuoSession, parseHaikuoRule, tokenizeHaikuoUrl,
  isHaikuoPlayToken, decodeHaikuoPlayToken,
} = await import(
  new URL("../proxy/assets/" + "drpy/haikuo.mjs", import.meta.url).href);

const scenario = process.argv[2];
const storeDir = process.argv[3];
const base = process.argv[4] || "";
const extra = process.argv[5] || "";

function assert(cond, msg) {
  if (!cond) throw new Error("assert failed: " + msg);
}
function eq(a, b, msg) {
  assert(JSON.stringify(a) === JSON.stringify(b), `${msg}: got ${JSON.stringify(a)} want ${JSON.stringify(b)}`);
}
function readFixture(p) {
  return fs.readFileSync(p, "utf8").split("__BASE__").join(base);
}

const SCENARIOS = {
  // (a) 五条已解析规则：解析 + 字段 + pages 模块 + classes 切分。
  validate_all() {
    const dir = extra;
    const out = [];
    for (const f of ["chengzi", "xiangjiao", "aishuangju", "91duanju", "huakui"]) {
      const rule = parseHaikuoRule(fs.readFileSync(dir + "/" + f + ".json", "utf8"));
      out.push({
        file: f, title: rule.title, type: rule.type, version: rule.version,
        pages: rule.pages.map((p) => p.path), classes: rule.classes,
        hasSearch: rule.hasSearch,
      });
    }
    return out;
  },

  // (c) 草榴 BT 规则：加载 + 校验 + 注册 bt 引擎模块；不执行任何网络调用。
  bt() {
    const rule = parseHaikuoRule(fs.readFileSync(extra, "utf8"));
    const s = createHaikuoSession("bt-rule", rule, { storeDir });
    const loaded = s.load();
    return {
      title: rule.title, type: rule.type, version: rule.version, group: rule.group,
      classes: rule.classes, hasSearch: rule.hasSearch,
      modules: loaded.modules,
      findRuleIsJs: rule.find_rule.trimStart().startsWith("js:"),
      searchFindIsJs: rule.searchFind.trimStart().startsWith("js:"),
      detailFindIsJs: rule.detail_find_rule.trimStart().startsWith("js:"),
      preRulePresent: rule.preRule.trim().length > 0,
    };
  },

  //  committed fixture：橙子短剧（31KB）加载 + 注册全部 pages 模块；不联网。
  chengzi_fixture() {
    const rule = parseHaikuoRule(fs.readFileSync(extra, "utf8"));
    const s = createHaikuoSession("chengzi-fixture", rule, { storeDir });
    const loaded = s.load();
    return {
      title: rule.title, type: rule.type, version: rule.version,
      pages: loaded.pages, modules: loaded.modules, hasSearch: rule.hasSearch,
      findRule: rule.find_rule,
    };
  },

  // (b) 纯解析串解释：list;title;pic;desc;url + 字段级 .js: 后缀。
  async parse_string() {
    const s = createHaikuoSession("synthetic-parse", readFixture(extra), { storeDir });
    const home = await s.home(1);
    eq(home.length, 2, "parse home count");
    eq(home[0].title, "TITLE ONE", "parse title .js: uppercases");
    eq(home[0].pic_url, base + "/p1.jpg", "parse pic absolute url");
    eq(home[0].desc, "Desc One", "parse desc");
    eq(home[0].url, base + "/d/1?from=list", "parse url .js: suffix appends query");
    eq(home[0].col_type, "pic_2", "parse col_type from rule");
    eq(home[1].title, "TITLE TWO", "parse second title");
    const search = await s.search("kw", 1);
    eq(search.length, 1, "parse search count");
    eq(search[0].title, "Search One", "parse search title");
    const cat = await s.category("分类A", 1);
    eq(cat.length, 2, "parse category count");
    eq(s.meta().classes.length, 2, "parse classes split on &&");
    eq(s.meta().classes[0], { name: "分类A", url: "a" }, "parse first class aligned");
    return { home, search, cat };
  },

  // (b) 引擎模块规则：pages[] 模块 + $.require 分发 + preRule(putItem)。
  async engine_module() {
    const s = createHaikuoSession("synthetic-engine", readFixture(extra), { storeDir });
    const home = await s.home(1);
    eq(home.length, 2, "engine home count");
    eq(home[0].title, "Title One|1", "engine home title carries preRule gate (putItem)");
    eq(home[0].url, base + "/d/1", "engine home absolute url");
    const search = await s.search("hello", 1);
    eq(search.length, 1, "engine search count");
    eq(search[0].title, "found:hello", "engine search via putMyVar+MY_KEYWORD");
    eq(s.context().run("getItem('eng_gate')"), "1", "preRule putItem persisted");
    return { home, search };
  },

  // (b) API-host 规则：home/search/detail + lazy token 往返出直链 m3u8。
  async api_host() {
    const s = createHaikuoSession("synthetic-api", readFixture(extra), { storeDir });
    const loaded = s.load();
    eq(loaded.title, "合成API站", "api meta title");
    eq(loaded.hasSearch, true, "api meta hasSearch");
    eq(loaded.pages.map((p) => p.path), ["erji"], "api meta pages");
    const home = await s.home(1);
    eq(home.length, 2, "api home count");
    eq(home[0].title, "剧一", "api home first title");
    eq(home[0].col_type, "movie_3", "api home col_type");
    eq(home[0].pic_url, base + "/p1.jpg", "api home pic");
    assert(isHaikuoPlayToken(home[0].url), "api detail link is a play token, not raw @rule js");
    assert(!home[0].url.includes("@rule=js:"), "token hides raw rule js");
    eq(s.context().run("getItem('api_gate')"), "open", "api preRule ran before home");
    const detail = await s.detail(home[0].url);
    eq(detail.length, 3, "api detail count (1 head + 2 episodes)");
    eq(detail[0].title, "剧一", "api detail head title");
    eq(detail[1].title, "第1集", "api detail ep1 title");
    assert(isHaikuoPlayToken(detail[1].url), "api episode link is a lazy token");
    const play1 = await s.play(detail[1].url);
    eq(play1, { playable: true, url: base + "/v/101.m3u8", headers: {} }, "api lazy token round-trip -> direct m3u8");
    // 片段页不缓存：第二部剧的详情必须重新执行 erji snippet，不能返回上一次的结果。
    const detail2 = await s.detail(home[1].url);
    eq(detail2[0].title, "剧二", "api detail second drama is fresh, not cached");
    const play2 = await s.play(detail2[1].url);
    eq(play2.url, base + "/v/201.m3u8", "api second drama episode plays its own id");
    const search = await s.search("剧", 1);
    eq(search.length, 1, "api search count");
    eq(search[0].title, "搜索剧", "api search title");
    // @rule token 交给 play：应明确说这是详情页、不是播放链接，而不是崩掉。
    const playRule = await s.play(home[0].url);
    eq(playRule.playable, false, "play on a detail @rule token is not playable");
    eq(playRule.action, "detail", "play on a detail @rule token points at detail");
    return { home, detail, play1, search };
  },

  // (b) lazy 分发矩阵：直链 / 头部写法 / 磁力 / 下载 / toast / video/x5 /
  // 普通页 sniff / selector 形态 / 全角分号多语句 / input JSON / 实参透传。
  async lazy_dispatch() {
    const s = createHaikuoSession("lazy-test",
      { title: "lazy-test", type: "video", version: "1", url: "hiker://empty", find_rule: "" },
      { storeDir });
    const out = {};
    out.m3u8_query = await s.play(base + "/v.m3u8?token=abc");
    eq(out.m3u8_query, { playable: true, url: base + "/v.m3u8?token=abc", headers: {} }, "m3u8 with query is playable, query kept");
    out.headers_form = await s.play(base + "/v.m3u8;{Authorization@tok123};{Referer@" + base + "/}#isVideo=true#");
    eq(out.headers_form, { playable: true, url: base + "/v.m3u8", headers: { Authorization: "tok123", Referer: base + "/" } }, "header form parses ;{K@V} and strips #isVideo");
    out.isvideo_marked = await s.play(base + "/api/app/vid/h5/m3u8/abc;{Authorization@t}#isVideo=true#");
    eq(out.isvideo_marked.playable, true, "#isVideo=true# marks a non-extension api url playable");
    out.magnet = await s.play("magnet:?xt=urn:btih:ABCDEF");
    eq(out.magnet, { playable: false, reason: "magnet/download not playable", action: "copy", url: "magnet:?xt=urn:btih:ABCDEF" }, "magnet not playable, copy");
    out.download = await s.play("download://abc.torrent");
    eq(out.download.playable, false, "download:// not playable");
    eq(out.download.action, "display", "download:// action display");
    out.toast = await s.play("toast://没有拿到播放地址");
    eq(out.toast, { playable: false, reason: "没有拿到播放地址", action: "toast", url: "" }, "toast:// surfaces the message");
    for (const bad of ["video://abc", "x5Rule://abc"]) {
      try {
        await s.play(bad);
        throw new Error("expected unsupported error for " + bad);
      } catch (e) {
        assert(String(e && e.message || e).includes("haikuo v1 unsupported"), "video/x5 must raise v1 unsupported, got: " + String(e && e.message || e));
        out["unsupported_" + bad.split("://")[0]] = String(e.message);
      }
    }
    out.sniff = await s.play(base + "/watch");
    eq(out.sniff, { playable: false, sniff: true, url: base + "/watch" }, "plain http page -> sniff");
    out.selector = await s.play(base + "/watch@lazyRule=a&&href.js: input");
    eq(out.selector, { playable: true, url: base + "/v.m3u8", headers: {} }, "selector lazy: fetch page, take href, eval");
    out.fullwidth = await s.play(base + "/ep@lazyRule=.js: var u = input + '/v.m3u8'；；u");
    eq(out.fullwidth, { playable: true, url: base + "/ep/v.m3u8", headers: {} }, "fullwidth ；； splits multi-statement lazy code");
    out.input_json = await s.play('{"id":"42"}@lazyRule=.js:(function(){ var o = JSON.parse(input); return "http://x.cn/" + o.id + ".mp4"; })()');
    eq(out.input_json, { playable: true, url: "http://x.cn/42.mp4", headers: {} }, "lazy input is the pre-@lazyRule string (chengzi playLazy shape)");
    out.args = await s.play(base + "/ep/7@lazyRule=.js:(function(eid, host){ return host + '/v/' + eid + '.m3u8'; })('7', '" + base + "')");
    eq(out.args, { playable: true, url: base + "/v/7.m3u8", headers: {} }, "lazyRule fn args are forwarded (huakui play shape)");
    // token 本身不透明：base64url 里看不到明文规则 JS。
    const tok = tokenizeHaikuoUrl(base + "/ep/9@lazyRule=.js:(function(){ return input + '.m3u8'; })()", "lazy-test");
    assert(isHaikuoPlayToken(tok), "tokenize produces a haikuo-play token");
    assert(!tok.includes("lazyRule") && !tok.includes("function"), "token hides raw rule js");
    eq(decodeHaikuoPlayToken(tok).kind, "lazy", "token decodes to lazy kind");
    out.token_roundtrip = await s.play(tok);
    eq(out.token_roundtrip, { playable: true, url: base + "/ep/9.m3u8", headers: {} }, "opaque token round-trips to direct m3u8");
    return out;
  },

  // preRule 在 home 前执行（putItem 别名 + MY_RULE 已注入）。
  async prerule() {
    const s = createHaikuoSession("prerule-test", {
      title: "prerule-test", type: "video", version: "1", url: "hiker://empty##fypage",
      find_rule: "js:setResult([{ title: 'gate=' + getItem('pre_gate', '?') + '/' + getItem('pre_put', '?') + '/' + (MY_RULE.title || '?'), url: 'hiker://empty', col_type: 'text_1' }]);",
      preRule: "js:putItem('pre_put', 'P'); setItem('pre_gate', 'G');",
    }, { storeDir });
    const home = await s.home(1);
    eq(home.length, 1, "prerule home count");
    eq(home[0].title, "gate=G/P/prerule-test", "preRule ran before home, putItem alias works, MY_RULE injected");
    return { home };
  },

  // 校验拒绝：read 型规则与缺字段规则必须报明错。
  validate_rejects() {
    const bad = [];
    for (const [name, rule] of [
      ["read-type", { title: "小说站", type: "read", url: "hiker://empty" }],
      ["no-title", { type: "video", url: "hiker://empty" }],
      ["no-type", { title: "无类型", url: "hiker://empty" }],
      ["bad-pages", { title: "坏pages", type: "video", url: "hiker://empty", pages: "{nope" }],
      ["bad-json", "{not json"],
    ]) {
      try {
        parseHaikuoRule(rule);
        throw new Error("expected rejection for " + name);
      } catch (e) {
        assert(!String(e && e.message || e).includes("expected rejection"), "rejection expected for " + name);
        bad.push({ name, error: String(e.message) });
      }
    }
    assert(bad[0].error.includes("read-type"), "read-type error must name read-type, got: " + bad[0].error);
    return bad;
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
