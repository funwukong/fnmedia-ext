// loader.mjs —— drpyS Node 运行时的 ESM customization hooks（Node 20+/24 注册式 loader）。
//
// 职责：
//   1) 把引擎里的 assets:// 依赖映射到本地 vendor 文件；
//   2) 把同目录 UMD 库（node-rsa / pako / json5 / jinja）与 vendor crypto-js 包成 ESM，
//      同时在包装模块里把 UMD 挂到的浏览器全局名（NODERSA/pako/JSON5/jinja/CryptoJS）
//      注入 globalThis —— 引擎顶层直接引用这些“裸全局”；
//   3) 其余解析（含 ./gbk.js、./模板.js、中文文件名）交回 Node 默认解析。
//
// 用法：node --import ./register.mjs sidecar.mjs  （见 register.mjs）
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));           // .../assets/drpy
const VENDOR = path.join(HERE, "node_vendor");
const hereUrl = pathToFileURL(HERE + "/").href;

// specifier -> {file, globalName}：UMD 包装后同时补一个同名全局
const UMD_MAP = new Map([
  ["assets://js/lib/crypto-js.js", { file: path.join(VENDOR, "crypto-js.js"), global: "CryptoJS" }],
  ["./node-rsa.js", { file: path.join(HERE, "node-rsa.js"), global: "NODERSA" }],
  ["./pako.min.js", { file: path.join(HERE, "pako.min.js"), global: "pako" }],
  ["./json5.js", { file: path.join(HERE, "json5.js"), global: "JSON5" }],
  ["./jinja.js", { file: path.join(HERE, "jinja.js"), global: "jinja" }],
]);

// 定制 cheerio：用 vendor 版（含 jp / jinja2 具名导出），并把 jp/jinja2 补到 default 上
const CHEERIO_URL = "drpy:cheerio";

export async function resolve(specifier, context, nextResolve) {
  try {
    if (specifier === "assets://js/lib/cheerio.min.js") {
      return { url: CHEERIO_URL, shortCircuit: true };
    }
    if (UMD_MAP.has(specifier)) {
      return { url: "drpy:umd?" + encodeURIComponent(specifier), shortCircuit: true };
    }
    return await nextResolve(specifier, context);
  } catch (e) {
    return nextResolve(specifier, context);
  }
}

function umdWrapper(file, globalName) {
  const raw = readFileSync(file, "utf8");
  // 在受控函数作用域里执行 UMD：提供假 module/exports，UMD 会走 CommonJS 分支；
  // 执行后把 module.exports 同时挂到 globalThis（引擎顶层要裸全局），并 default 导出。
  return `
import { createRequire } from "node:module";
const require = createRequire(${JSON.stringify(path.join(HERE, "noop.js"))});
const module = { exports: {} };
const exports = module.exports;
const define = undefined;
(function (module, exports, require, globalThisArg) {
${raw}
})(module, exports, require, globalThis);
const _lib = module.exports;
if (_lib && typeof _lib === "object") globalThis.${globalName} = _lib;
else globalThis.${globalName} = _lib;
export default _lib;
`;
}

export async function load(url, context, nextLoad) {
  if (url === CHEERIO_URL) {
    const cheerioPath = path.join(VENDOR, "cheerio.min.js");
    return {
      format: "module", shortCircuit: true,
      source: `
        import _c, { jp as _jp, jinja2 as _jinja2, load as _load } from ${JSON.stringify(pathToFileURL(cheerioPath).href)};
        const cheerio = _c;
        cheerio.jp = _jp;
        cheerio.jinja2 = _jinja2;
        if (typeof _load === "function" && typeof cheerio.load !== "function") cheerio.load = _load;
        globalThis.cheerio = cheerio;
        export default cheerio;
        export { _jp as jp, _jinja2 as jinja2, _load as load };
      `,
    };
  }
  if (url.startsWith("drpy:umd?")) {
    const specifier = decodeURIComponent(url.slice("drpy:umd?".length));
    const cfg = UMD_MAP.get(specifier);
    if (!cfg) throw new Error("unknown umd specifier " + specifier);
    return { format: "module", shortCircuit: true, source: umdWrapper(cfg.file, cfg.global) };
  }
  return nextLoad(url, context);
}
