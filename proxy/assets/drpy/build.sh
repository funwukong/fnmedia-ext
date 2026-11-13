#!/bin/bash
# build.sh —— drpyS Node sidecar 自检脚本（纯 JS 无需编译）。
#   1) node --check 语法检查所有 .mjs / .cjs；
#   2) 启动 sidecar，实际跑一遍 txbei 的 home + category，校验非空。
# 任何一步失败以非 0 退出。
set -u
cd "$(dirname "$0")"

NODE_BIN="${NODE_BIN:-/var/apps/nodejs_v24/target/bin/node}"
PASS=1

say() { printf '\033[36m[build]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[build][FAIL]\033[0m %s\n' "$*" >&2; PASS=0; }

if [ ! -x "$NODE_BIN" ]; then
  # 退到 PATH 里的 node
  if command -v node >/dev/null 2>&1; then NODE_BIN="$(command -v node)"; else
    echo "[build][FAIL] 找不到 node：$NODE_BIN" >&2; exit 1
  fi
fi
say "node: $($NODE_BIN -v) @ $NODE_BIN"

# ---------- 1) 语法自检 ----------
say "语法检查 node --check ..."
for f in *.mjs *.cjs; do
  [ -e "$f" ] || continue
  if ! "$NODE_BIN" --check "$f" 2>/tmp/drpy_check.err; then
    fail "$f 语法错误："; cat /tmp/drpy_check.err >&2
  else
    say "  ok $f"
  fi
done

# ---------- 2) sidecar 实际跑通 txbei ----------
say "启动 sidecar 跑 txbei home + category ..."
OUT="$(printf '%s\n%s\n' \
  '{"id":"home","site":{"api":"./drpy2.min.js","ext":"./txbei.js"},"method":"home","args":[]}' \
  '{"id":"cat","site":{"api":"./drpy2.min.js","ext":"./txbei.js"},"method":"category","args":["2",1,{},{}]}' \
  | "$NODE_BIN" --import ./register.mjs sidecar.mjs 2>/tmp/drpy_sidecar.err)"

echo "$OUT" | "$NODE_BIN" -e '
let data = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", c => data += c);
process.stdin.on("end", () => {
  const lines = data.trim().split("\n").filter(Boolean);
  const byId = {};
  for (const l of lines) { try { const r = JSON.parse(l); byId[r.id] = r; } catch (e) {} }
  let bad = 0;
  const home = byId.home, cat = byId.cat;
  if (!home || home.error || !home.result || !(home.result.class||[]).length) {
    console.error("[build][FAIL] txbei home 无 class", JSON.stringify(home||null).slice(0,200)); bad++;
  } else {
    console.log("[build] home class:", home.result.class.map(c=>c.type_name).join("/"));
  }
  if (!cat || cat.error || !cat.result || !(cat.result.list||[]).length) {
    console.error("[build][FAIL] txbei category(2,1) 无 list", JSON.stringify(cat||null).slice(0,200)); bad++;
  } else {
    const v = cat.result.list[0];
    console.log("[build] category list:", cat.result.list.length, "条；首条:", v.vod_name, "/", v.vod_id.split("@@")[0]);
  }
  process.exit(bad ? 2 : 0);
});'
RC=$?
if [ "$RC" -ne 0 ]; then
  fail "sidecar txbei 链路验证失败（详见上方与 /tmp/drpy_sidecar.err）"
  tail -20 /tmp/drpy_sidecar.err >&2
else
  say "sidecar txbei home + category 验证通过"
fi

if [ "$PASS" -eq 1 ]; then
  say "全部通过 ✅"
  exit 0
else
  fail "存在失败项"
  exit 1
fi
