// sync_http_worker.mjs —— 常驻 fetch worker（sync_http.mjs 的子线程）。
//
// 主线程把请求写入共享内存后 Atomics.wait；本 worker 用 async fetch 执行，
// 把响应头/状态写回共享内存、响应体写入临时文件，再 Atomics.notify 唤醒主线程。
// 全程只依赖 Node 内置模块。
//
// ctl(Int32Array(8)) 布局：
//   [0] boot: 0 -> 1 表示 worker 就绪（就绪后 notify(0)）
//   [1] run 状态: 0 空闲, 1 有请求, 2 成功, 3 异常
//   [2] 完成信号：每完成一次请求 Atomics.add 1 并 notify(2)
//   [3] seq
//
// 请求/响应 SAB 帧格式：前 4 字节为 Int32 小端长度，其后为 UTF-8 字节。
import { parentPort } from "node:worker_threads";
import { writeFileSync } from "node:fs";

const REQ_LEN = 1 << 20;       // 1MB 请求帧
const RES_META_LEN = 1 << 18;  // 256KB 响应元数据帧

const enc = new TextEncoder();
const dec = new TextDecoder();

let ctl, reqBuf, resBuf;

function writeFrame(u8, str) {
  const bytes = enc.encode(str || "");
  if (bytes.length + 4 > u8.length) throw new Error("shared buffer overflow: need " + (bytes.length + 4) + " cap " + u8.length);
  new DataView(u8.buffer, u8.byteOffset).setInt32(0, bytes.length, true);
  u8.set(bytes, 4);
}
function readFrame(u8) {
  const len = new DataView(u8.buffer, u8.byteOffset).getInt32(0, true);
  if (len <= 0) return "";
  return dec.decode(u8.subarray(4, 4 + len));
}

async function doFetch(req) {
  const url = req.url;
  const o = req.obj || {};
  if (!url) return { status: 0, headers: {}, content: "" };
  const method = (o.method || "GET").toUpperCase();
  const headers = {};
  for (const [k, v] of Object.entries(o.headers || {})) {
    if (k.toLowerCase() === "content-length") continue;
    headers[k] = Array.isArray(v) ? v.join("; ") : String(v);
  }
  if (!Object.keys(headers).map(k => k.toLowerCase()).includes("user-agent")) {
    headers["User-Agent"] = "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36";
  }
  const init = {
    method,
    headers,
    redirect: o.redirect === 0 || o.redirect === "manual" ? "manual" : "follow",
  };
  // drpyS 对 object body 会挪到 data
  let body = o.body;
  if (body == null && o.data != null) body = o.data;
  if (body != null) init.body = typeof body === "string" ? body : (typeof body === "object" ? JSON.stringify(body) : String(body));
  // drpyS rule.timeout 单位是毫秒（默认 5000）；兼容调用方误传秒级小数
  const msTimeout = (o.timeout && o.timeout < 100) ? o.timeout * 1000 : (o.timeout || 20000);
  const ctrl = new AbortController();
  const tmo = setTimeout(() => ctrl.abort(), Math.max(500, msTimeout));
  init.signal = ctrl.signal;
  let r;
  try {
    r = await fetch(url, init);
  } finally {
    clearTimeout(tmo);
  }
  const buf = Buffer.from(await r.arrayBuffer());
  const h = {};
  r.headers.forEach((v, k) => { h[k] = v; });
  // drpyS 默认按 utf-8 读 content。
  return { status: r.status, headers: h, content: buf.toString("utf8"), redirected: r.redirected, finalUrl: r.url };
}

async function handle(seq, bodyPath) {
  let result, error = "", reqUrl = "";
  let req = {};
  try {
    req = JSON.parse(readFrame(reqBuf) || "{}");
    reqUrl = req.url || "";
    result = await doFetch(req);
  } catch (e) {
    error = String((e && e.message) || e) + (e && e.cause ? " | cause=" + (e.cause.code || e.cause.message || e.cause) : "");
    if (process.env.DRPY_SYNC_DEBUG) process.stderr.write("[sync_http_worker] " + error + " url=" + reqUrl + "\n");
    result = { status: 0, headers: {}, content: "" };
  }
  // 响应体走临时文件，元数据（含错误）走共享内存
  try {
    writeFileSync(bodyPath, result.content || "", { encoding: "utf8" });
  } catch (e) {
    error = (error ? error + " | " : "") + "bodyfile:" + (e && e.message);
  }
  const meta = JSON.stringify({
    status: result.status, headers: result.headers, error,
    url: result.finalUrl || "", redirected: !!result.redirected,
  });
  try {
    writeFrame(resBuf, meta);
  } catch (e) {
    writeFrame(resBuf, JSON.stringify({ status: result.status, headers: {}, error: "meta-overflow:" + e.message }));
  }
  Atomics.store(ctl, 1, error ? 3 : 2);
  Atomics.store(ctl, 3, seq);
  Atomics.add(ctl, 2, 1);
  Atomics.notify(ctl, 2);
}

parentPort.on("message", (msg) => {
  if (msg && msg.type === "init") {
    ctl = new Int32Array(msg.ctl);
    reqBuf = new Uint8Array(msg.req);
    resBuf = new Uint8Array(msg.res);
    Atomics.store(ctl, 0, 1);
    Atomics.notify(ctl, 0);
    return;
  }
  if (msg && msg.type === "run") {
    handle(msg.seq, msg.bodyPath);
    return;
  }
});
