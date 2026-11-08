// selectors.mjs —— drpyS 的 pdfh / pdfa / pd 宿主实现（Node + 定制 cheerio）。
//
// drpyS 选择器规则形如：
//   "selector&&attr"   单元素：取文本或属性
//   "sel1&&sel2&&attr" 链式：先选 sel1(first)，在其内再选 sel2
//   attr: Text/(空) -> 文本 trim；Html -> innerHTML；其它 -> 属性
//
// pdfa 返回数组。元素项是“节点包装”（见 wrap），引擎会先尝试 html.rr(html.ele)
// 转字符串、失败后把原对象传给下一次 pdfh/pdfa —— 因此包装对象可在双层定位
// （double:true）中一路带着 cheerio 上下文向下传。
//
// pd 同 pdfh，但结果疑似相对 URL 时用基准地址 urljoin（对齐引擎 pd2）。
import { pathToFileURL } from "node:url";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const cheerioUrl = pathToFileURL(path.join(HERE, "node_vendor", "cheerio.min.js")).href;

let cheerioLoad = null;
export async function initSelectors() {
  const mod = await import(cheerioUrl);
  cheerioLoad = mod.load;
  return mod;
}

function wrap($, el) {
  return { __drpyNode: true, $, el };
}

// html: HTML 字符串 | 节点包装 | cheerio 节点本身
function resolve(html) {
  if (html && html.__drpyNode) return { $: html.$, scope: html.el };
  const s = typeof html === "string" ? html : String(html || "");
  const $ = cheerioLoad(s);
  return { $, scope: null };
}

function looksLikeSelector(tok) {
  return /[.#[\]>+~ ]/.test(tok);
}

// 拆规则：多段 && 中最后一段若像属性名则为 attr，其余为选择器链
function splitRule(rule) {
  const r = String(rule || "").trim();
  if (!r) return { chain: [], attr: "" };
  const parts = r.split("&&");
  const last = parts[parts.length - 1].trim();
  if (parts.length >= 2 && !looksLikeSelector(last) && /^[A-Za-z_:*@-][\w:*@.\-]*$/.test(last)) {
    return { chain: parts.slice(0, -1).map(s => s.trim()).filter(Boolean), attr: last };
  }
  return { chain: parts.map(s => s.trim()).filter(Boolean), attr: "" };
}

// chain 前 n-1 段逐层 first()；返回最终上下文与最后一段选择器
function followChain($, scope, chain) {
  let ctx = scope;
  for (let i = 0; i < chain.length - 1; i++) {
    const sel = chain[i];
    if (sel === "body" || sel === "") {
      ctx = ctx ? $(ctx).closest("body").length ? $(ctx) : $("body").first() : $("body").first();
    } else {
      ctx = ctx ? $(ctx).find(sel).first() : $(sel).first();
    }
  }
  return { ctx, lastSel: chain[chain.length - 1] };
}

function pickValue($, el, attr) {
  const node = $(el);
  if (!attr || attr === "Text" || attr === "text") {
    // 对齐浏览器 innerText：折叠所有连续空白（含换行/全角空格）再 trim
    return (node.text() || "").replace(/[\s\u00A0\u3000]+/g, " ").trim();
  }
  if (attr === "Html" || attr === "html") return node.html() || "";
  let a = attr;
  if (a.startsWith("style::")) a = "style";
  const v = node.attr(a);
  return v == null ? "" : String(v);
}

function selectOne($, scope, chain) {
  if (chain.length === 0) {
    return scope ? $(scope) : ($.root ? $.root() : $("body").first());
  }
  const { ctx, lastSel } = followChain($, scope, chain);
  if (!lastSel || lastSel === "body") return ctx ? $(ctx) : ($.root ? $.root() : $("body").first());
  if (lastSel === ".") return ctx ? $(ctx) : $("body").first();
  return ctx ? $(ctx).find(lastSel).first() : $(lastSel).first();
}

export function makePdfh() {
  return function pdfh(html, rule) {
    try {
      const { $, scope } = resolve(html);
      const { chain, attr } = splitRule(rule);
      return pickValue($, selectOne($, scope, chain), attr);
    } catch {
      return "";
    }
  };
}

export function makePdfa() {
  // 返回节点包装数组（引擎双层定位依赖，pdfh 可继续在节点上向下选择）。
  return function pdfa(html, rule) {
    try {
      const { $, scope } = resolve(html);
      const { chain } = splitRule(rule);
      if (chain.length === 0) return scope ? [wrap($, scope)] : [wrap($, $.root ? $.root()[0] : $("body")[0])];
      const { ctx, lastSel } = followChain($, scope, chain);
      let list;
      if (!lastSel || lastSel === "body") list = ctx ? $(ctx) : $("body");
      else if (lastSel === ".") list = ctx ? $(ctx) : $("body");
      else list = ctx ? $(ctx).find(lastSel) : $(lastSel);
      const res = [];
      list.each((_, el) => res.push(wrap($, el)));
      return res;
    } catch {
      return [];
    }
  };
}

export function urljoin(base, href) {
  try {
    if (!href) return "";
    href = String(href).trim();
    if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("//")) return href;
    if (!base) return href;
    return new URL(href, base).href;
  } catch {
    return href;
  }
}

export function makePd(pdfh, getBaseUrl) {
  return function pd(html, rule, uri) {
    const ret = pdfh(html, rule);
    if (!ret) return ret;
    if (/^(ftp|magnet|thunder|ws):/.test(ret)) return ret;
    if (/(url|src|href|-original|-src|-play|-url|style)$/i.test(String(rule))) {
      if (/https?:\/\//.test(ret)) return ret.slice(ret.indexOf("http"));
      const base = uri || (getBaseUrl && getBaseUrl()) || "";
      return urljoin(base, ret);
    }
    return ret;
  };
}
