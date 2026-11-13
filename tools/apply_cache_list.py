"""第 7 项：已缓存条目过多 —— 默认缩放适配容器 + 「显示更多」展开全部。

做法：
  * 表格容器加 max-height + overflow:auto（超出可滚动，不再把页面撑长）；
  * 默认只渲染前 30 条，按容器宽度自适应列宽（字号/间距随宽度缩放）；
  * 底部加「显示更多」按钮，点击展开全部（再点收起）。
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


# ---------- CSS：缓存表容器与缩放 ----------
edit("proxy/admin_ui.py",
     """/* ---------- 表格 ---------- */""",
     """/* ---------- 已缓存条目：容器内滚动 + 自适应缩放 ---------- */
.cache-wrap{max-height:340px;overflow:auto;border:1px solid var(--line);
  border-radius:10px;background:#fff}
.cache-wrap table{width:100%;table-layout:fixed}
.cache-wrap td,.cache-wrap th{font-size:13px;padding:7px 9px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cache-wrap td:first-child{white-space:normal;line-height:1.45}
.cache-wrap.compact{font-size:12px}
.cache-wrap.compact td,.cache-wrap.compact th{font-size:12px;padding:5px 7px}
.cache-more{margin-top:8px;display:flex;justify-content:center}
.cache-more button{font-size:13px}

/* ---------- 表格 ---------- */""",
     "CSS cache wrap")

# ---------- HTML：容器 + 更多按钮 ----------
edit("proxy/admin_ui.py",
     """        <table style="margin-top:10px">
          <thead><tr><th>标题</th><th style="width:100px">来源</th><th style="width:70px">集数</th></tr></thead>
          <tbody id="rows"><tr><td colspan="3" class="muted">加载中…</td></tr></tbody>
        </table>""",
     """        <div class="cache-wrap" id="cacheWrap">
          <table>
            <thead><tr><th>标题</th><th style="width:100px">来源</th><th style="width:70px">集数</th></tr></thead>
            <tbody id="rows"><tr><td colspan="3" class="muted">加载中…</td></tr></tbody>
          </table>
        </div>
        <div class="cache-more" id="cacheMore" style="display:none">
          <button class="small" onclick="toggleCacheAll()">显示更多</button>
        </div>""",
     "HTML cache wrap")

# ---------- JS：分页渲染 + 缩放 + 更多 ----------
edit("proxy/admin_ui.py",
     """async function loadOnline(){
  const kw=$("kw").value.trim();
  const r=await jget("/v/api/ext/online?limit=500"+(kw?("&q="+encodeURIComponent(kw)):""));
  const list=r.data||[];$("cacheN").textContent=list.length;
  const tb=$("rows");
  if(!list.length){tb.innerHTML='<tr><td colspan="3" class="muted">暂无，先去飞牛搜索任意中文片名</td></tr>';return;}
  tb.innerHTML=list.map(x=>`<tr><td><div style="font-weight:500">${esc(x.title)}</div>
    <div class="meta">${esc(x.year||"")} ${esc(x.area||"")} · ${esc(x.remarks||"")}</div></td>
    <td>${esc(x.provider)}</td><td>${x.episodes}</td></tr>`).join("");
}""",
     """let CACHE_ALL=[],CACHE_EXPANDED=false;
const CACHE_PAGE=30;
function toggleCacheAll(){
  CACHE_EXPANDED=!CACHE_EXPANDED;
  renderCache();
  const b=document.querySelector("#cacheMore button");
  if(b)b.textContent=CACHE_EXPANDED?"收起":"显示更多";
}
function renderCache(){
  const tb=$("rows");const wrap=$("cacheWrap");
  const list=CACHE_ALL;
  $("cacheN").textContent=list.length;
  const more=$("cacheMore");
  if(!list.length){
    tb.innerHTML='<tr><td colspan="3" class="muted">暂无，先去飞牛搜索任意中文片名</td></tr>';
    if(more)more.style.display="none";
    return;
  }
  const show=CACHE_EXPANDED?list:list.slice(0,CACHE_PAGE);
  tb.innerHTML=show.map(x=>`<tr><td><div style="font-weight:500">${esc(x.title)}</div>
    <div class="meta">${esc(x.year||"")} ${esc(x.area||"")} · ${esc(x.remarks||"")}</div></td>
    <td>${esc(x.provider)}</td><td>${x.episodes}</td></tr>`).join("");
  if(more){
    more.style.display=list.length>CACHE_PAGE?"flex":"none";
    const b=more.querySelector("button");
    if(b)b.textContent=CACHE_EXPANDED?"收起":("显示更多（还有 "+(list.length-CACHE_PAGE)+" 条）");
  }
  // 条目多时用紧凑排版，避免表格过高撑破容器
  if(wrap)wrap.classList.toggle("compact",list.length>60);
}
async function loadOnline(){
  const kw=$("kw").value.trim();
  const r=await jget("/v/api/ext/online?limit=500"+(kw?("&q="+encodeURIComponent(kw)):""));
  CACHE_ALL=r.data||[];
  CACHE_EXPANDED=false;
  renderCache();
}""",
     "JS cache render")

print()
print("done:", ", ".join(done) if done else "(none)")