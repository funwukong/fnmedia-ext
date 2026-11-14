"""第 3 项：资源站列表不分页 —— 自适应显示 + 点击展开全量。

现状：每页 20 条 + 上/下一页按钮。源一多就要翻页，且表格列宽不随容器
自适应（长地址撑破布局）。

改法：
  1. 去掉分页逻辑，列表一次渲染全部（筛选后的全部，不是当前页）；
  2. 表格放进固定高度可滚动容器，列宽自适应（table-layout:fixed），
     单元格超长省略号，悬停看全文；
  3. 顶部加「展开全部 / 收起」按钮：默认折叠（容器内滚动，高度自适应），
     点击后整表全展开（不再内部滚动），便于一次性看全部；
  4. 保留筛选与计数。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
done: list[str] = []


def edit(rel: str, old: str, new: str, tag: str) -> None:
    p = ROOT / rel
    if not p.exists():
        print(f"  [miss] {tag}")
        return
    s = p.read_text(encoding="utf-8")
    if new in s:
        print(f"  [skip] {tag}")
        return
    if old not in s:
        raise SystemExit(f"[FAIL] {tag}: 锚点未找到 in {rel}")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    done.append(tag)
    print(f"  [ok] {tag}")


# ---------- 1) CSS：资源站列表容器与自适应 ----------
edit("proxy/admin_ui.py",
     """/* ---------- 已缓存条目：容器内滚动 + 自适应缩放 ---------- */""",
     """/* ---------- 资源站列表：不分页，自适应列宽 + 可滚动 ---------- */
.src-wrap{max-height:min(62vh,620px);overflow:auto;border:1px solid var(--line);
  border-radius:10px}
.src-wrap.expanded{max-height:none;overflow:visible}
.src-wrap table{width:100%;table-layout:fixed}
.src-wrap td,.src-wrap th{font-size:13px;padding:7px 9px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.src-wrap td.src-name{white-space:normal;line-height:1.45}
.src-wrap td.src-name .meta{overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;max-width:100%}
.src-wrap.compact td,.src-wrap.compact th{font-size:12px;padding:5px 7px}

/* ---------- 已缓存条目：容器内滚动 + 自适应缩放 ---------- */""",
     "CSS src wrap")

# ---------- 2) HTML：去掉分页按钮，加展开/收起 ----------
edit("proxy/admin_ui.py",
     """          <span class="muted sm" id="srcPageInfo"></span>
          <button class="small" onclick="srcPage(-1)">上一页</button>
          <button class="small" onclick="srcPage(1)">下一页</button>
        </div>
        <div style="overflow-x:auto">
        <table>
          <thead><tr><th style="width:42px">启用</th><th style="width:96px">选项</th><th>资源站</th>
            <th style="width:150px">连通性</th><th style="width:70px">测试</th><th style="width:44px"></th></tr></thead>
          <tbody id="srcRows"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
        </table>
        </div>""",
     """          <span class="muted sm" id="srcPageInfo"></span>
          <button class="small" id="srcExpandBtn" onclick="toggleSrcExpand()">展开全部</button>
        </div>
        <div class="src-wrap" id="srcWrap">
        <table>
          <thead><tr><th style="width:42px">启用</th><th style="width:96px">选项</th><th>资源站</th>
            <th style="width:130px">连通性</th><th style="width:70px">测试</th><th style="width:44px"></th></tr></thead>
          <tbody id="srcRows"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
        </table>
        </div>""",
     "HTML src list no paging")

# ---------- 3) JS：去分页、全量渲染、展开/收起 ----------
edit("proxy/admin_ui.py",
     """let SRC_FILTER="",SRC_ENABLED="all",SRC_PAGE=1;const SRC_PAGE_SIZE=20;""",
     """let SRC_FILTER="",SRC_ENABLED="all",SRC_EXPANDED=false;""",
     "JS src page vars")

edit("proxy/admin_ui.py",
     """function onSrcFilter(v){SRC_FILTER=v;SRC_PAGE=1;renderSources();}
function onSrcEnabledFilter(v){SRC_ENABLED=v;SRC_PAGE=1;renderSources();}
function srcPage(d){SRC_PAGE=Math.max(1,SRC_PAGE+d);renderSources();}
function renderSources(){
  const srcsAll=srcFiltered();
  const pages=Math.max(1,Math.ceil(srcsAll.length/SRC_PAGE_SIZE));
  if(SRC_PAGE>pages)SRC_PAGE=pages;
  const srcs=srcsAll.slice((SRC_PAGE-1)*SRC_PAGE_SIZE,SRC_PAGE*SRC_PAGE_SIZE);
  const all=CFG.sources||[];
  $("srcCount").textContent=all.filter(x=>x.enabled).length+"/"+all.length+" 启用";
  const pi=$("srcPageInfo");
  if(pi)pi.textContent=`第 ${SRC_PAGE}/${pages} 页 · 本页 ${srcs.length} 条 · 筛选后 ${srcsAll.length} 条`;""",
     """function onSrcFilter(v){SRC_FILTER=v;renderSources();}
function onSrcEnabledFilter(v){SRC_ENABLED=v;renderSources();}
function toggleSrcExpand(){
  SRC_EXPANDED=!SRC_EXPANDED;
  const w=$("srcWrap");
  if(w)w.classList.toggle("expanded",SRC_EXPANDED);
  const b=$("srcExpandBtn");
  if(b)b.textContent=SRC_EXPANDED?"收起":"展开全部";
  renderSources();
}
function renderSources(){
  // 2026-11-13：不分页 —— 一次渲染筛选后的全部源；容器内滚动 + 列宽自适应；
  // 点「展开全部」则整表展开不再内部滚动。
  const srcsAll=srcFiltered();
  const srcs=srcsAll;
  const all=CFG.sources||[];
  $("srcCount").textContent=all.filter(x=>x.enabled).length+"/"+all.length+" 启用";
  const pi=$("srcPageInfo");
  if(pi)pi.textContent=`共 ${srcsAll.length} 条（已全部显示）`;
  const wrap=$("srcWrap");
  if(wrap)wrap.classList.toggle("compact",srcsAll.length>40);""",
     "JS src render no paging")

# ---------- 4) 表格单元格加 class 便于省略号 ----------
edit("proxy/admin_ui.py",
     """      <td><div style="font-weight:500">${esc(src.name)}${src.kind==="t4"?'<span class="tag">T4</span>':''}${extraTag}${src.builtin?'':'<span class="tag amber">自定义</span>'}${src.standalone?'<span class="tag">媒体库</span>':''}</div>
        <div class="meta">${esc(src.api)}</div></td>""",
     """      <td class="src-name" title="${esc(src.api)}"><div style="font-weight:500">${esc(src.name)}${src.kind==="t4"?'<span class="tag">T4</span>':''}${extraTag}${src.builtin?'':'<span class="tag amber">自定义</span>'}${src.standalone?'<span class="tag">媒体库</span>':''}</div>
        <div class="meta">${esc(src.api)}</div></td>""",
     "JS src cell class")

print()
print("done:", ", ".join(done) if done else "(none)")