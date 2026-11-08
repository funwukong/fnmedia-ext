// sync_http.mjs —— 主线程侧“真同步”HTTP 桥。
//
// Node 没有同步 fetch。这里常驻一个 Worker 执行 async fetch，主线程发起请求后
// 通过 Atomics.wait 阻塞，直到 worker 回填结果 —— 对调用方表现为普通同步函数，
// 与 drpyS 引擎里大量同步 request()/req() 完全兼容，且无需每次 fork 子进程。
//
// 帧格式：SAB 前 4 字节为 Int32 小端长度，其后为 UTF-8 字节（worker 侧同构）。
//
// 导出：
//   syncRequest(url, obj) -> {content, headers, status, error?, url?, redirected?}
import { Worker } from "node:worker_threads";
import { fileURLToPath } from "node:url";
import path from "node:path";
import os from "node:os";
import fs from "node:fs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REQ_LEN = 1 << 20;       // 1MB 请求帧
const RES_META_LEN = 1 << 18;  // 256KB 响应元数据帧

const ctl = new Int32Array(new SharedArrayBuffer(8 * 4));
const reqSab = new SharedArrayBuffer(REQ_LEN);
const resSab = new SharedArrayBuffer(RES_META_LEN);
const reqBuf = new Uint8Array(reqSab);
const resBuf = new Uint8Array(resSab);

const worker = new Worker(path.join(HERE, "sync_http_worker.mjs"));
worker.unref();
worker.on("error", (e) => {
  process.stderr.write("[sync_http] worker error: " + (e && e.stack || e) + "\n");
});

// 启动握手：发送 init，等待 worker 就绪（ctl[0] 0 -> 1）
// 必须传 SharedArrayBuffer 本体；传 Int32Array 视图会让对端 new Int32Array(view) 时复制内存
worker.postMessage({ type: "init", ctl: ctl.buffer, req: reqSab, res: resSab });
{
  const t0 = Date.now();
  while (Atomics.load(ctl, 0) !== 1) {
    Atomics.wait(ctl, 0, 0, 10000);
    if (Atomics.load(ctl, 0) !== 1 && Date.now() - t0 > 10000) {
      throw new Error("sync_http worker boot timeout");
    }
    if (Atomics.load(ctl, 0) === 1) break;
  }
}

const enc = new TextEncoder();
const dec = new TextDecoder();

function writeFrame(u8, str) {
  const bytes = enc.encode(str || "");
  if (bytes.length + 4 > u8.length) {
    throw new Error("request metadata too large (" + bytes.length + " bytes, cap " + (u8.length - 4) + ")");
  }
  new DataView(u8.buffer, u8.byteOffset).setInt32(0, bytes.length, true);
  u8.set(bytes, 4);
}
function readFrame(u8) {
  const len = new DataView(u8.buffer, u8.byteOffset).getInt32(0, true);
  if (len <= 0) return "";
  return dec.decode(u8.subarray(4, 4 + len));
}

let seq = 0;

export function syncRequest(url, obj) {
  const mySeq = ++seq;
  // drpyS rule.timeout 为毫秒（默认 5000）；整体等待额外 +10s 给 worker 缓冲
  const tIn = (obj && obj.timeout) || 20000;
  const timeoutMs = (tIn < 100 ? tIn * 1000 : tIn) + 10000;
  const bodyPath = path.join(os.tmpdir(), "drpy_req_" + process.pid + "_" + mySeq + ".body");
  try {
    const payload = JSON.stringify({ url: String(url || ""), obj: obj || {} });
    writeFrame(reqBuf, payload);
  } catch (e) {
    return { content: "", headers: {}, status: 0, error: String(e && e.message || e) };
  }
  Atomics.store(ctl, 1, 1);
  const before = Atomics.load(ctl, 2);
  worker.postMessage({ type: "run", seq: mySeq, bodyPath });

  // 阻塞等待 worker 完成（信号 ctl[2] 递增）
  const waitRes = Atomics.wait(ctl, 2, before, timeoutMs);
  let content = "", meta = { status: 0, headers: {}, error: "" };
  try {
    if (waitRes === "timed out" || Atomics.load(ctl, 2) === before) {
      meta.error = "sync http timeout (" + timeoutMs + "ms)";
    } else {
      meta = JSON.parse(readFrame(resBuf) || "{}");
      content = fs.readFileSync(bodyPath, "utf8");
    }
  } catch (e) {
    meta.error = (meta.error ? meta.error + " | " : "") + String(e && e.message || e);
  } finally {
    try { fs.unlinkSync(bodyPath); } catch {}
  }
  return {
    content,
    headers: meta.headers || {},
    status: meta.status || 0,
    ...(meta.error ? { error: meta.error } : {}),
    ...(meta.url ? { url: meta.url } : {}),
    redirected: !!meta.redirected,
  };
}

export default syncRequest;
