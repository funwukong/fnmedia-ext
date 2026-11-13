#!/usr/bin/env node
// 同步 HTTP 子进程：stdin 读一行 JSON {url,obj}，stdout 输出一行 {content,headers,status,buffer}。
// 被 host 通过 execFileSync 调用，使引擎的同步 req() 能拿到网络结果。
let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => (raw += c));
process.stdin.on("end", async () => {
  try {
    const { url, obj } = JSON.parse(raw || "{}");
    if (!url) { process.stdout.write(JSON.stringify({ content: "", headers: {}, status: 0 })); return; }
    const o = obj || {};
    const method = (o.method || "GET").toUpperCase();
    const headers = {};
    for (const [k, v] of Object.entries(o.headers || {})) if (k.toLowerCase() !== "content-length") headers[k] = v;
    const init = { method, headers, redirect: o.redirect === 0 ? "manual" : "follow" };
    if (o.body != null) init.body = typeof o.body === "string" ? o.body : JSON.stringify(o.body);
    const ctrl = new AbortController();
    const tmo = setTimeout(() => ctrl.abort(), (o.timeout || 20) * 1000);
    init.signal = ctrl.signal;
    const r = await fetch(url, init);
    clearTimeout(tmo);
    const buf = Buffer.from(await r.arrayBuffer());
    const h = {};
    r.headers.forEach((v, k) => { h[k] = v; });
    const out = { status: r.status, headers: h, content: buf.toString("utf8") };
    if (o.buffer === 2) out.buffer = buf.toString("base64");   // 二进制（图片等）
    process.stdout.write(JSON.stringify(out));
  } catch (e) {
    process.stdout.write(JSON.stringify({ content: "", headers: {}, status: 0, error: String(e && e.message || e) }));
  }
});
