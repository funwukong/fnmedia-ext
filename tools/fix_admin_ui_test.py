"""把资源站分页相关测试改为「不分页」版本（2026-11-13）。

改动点：资源站列表不再分页（一次渲染全部 + 容器内滚动 + 展开/收起），
原 test_sources_pager_controls_present / test_sources_filter_and_page_logic
断言的旧分页行为已移除，替换为新行为断言。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "tests" / "test_admin_ui_p1.py"
s = p.read_text(encoding="utf-8")

OLD_START = "# ---------------- P1-16：资源站分页/筛选 ----------------"
OLD_END = '    out = _node_ok(script)\n    assert "src pager logic ok" in out\n'

i = s.index(OLD_START)
j = s.index(OLD_END, i) + len(OLD_END)

NEW = '''# ---------------- P1-16：资源站筛选 / 不分页（2026-11-13 改） ----------------

def _src_filter_block(src: str) -> str:
    """取筛选相关函数块（不再有 srcPage —— 列表已改为不分页）。"""
    start = src.index("let SRC_FILTER")
    end = src.index("function renderSources()")
    return src[start:end]


def test_sources_no_pager_controls_present():
    """2026-11-13：资源站列表不分页 —— 一次渲染全部 + 展开/收起 + 自适应。"""
    src = _src()
    for html_id in ("srcFilter", "srcEnabledFilter", "srcPageInfo",
                    "srcWrap", "srcExpandBtn"):
        assert f'id="{html_id}"' in src, html_id
    # 分页相关必须已移除
    assert "const SRC_PAGE_SIZE=20" not in src
    assert "function srcPage(d)" not in src
    assert 'onclick="srcPage(-1)"' not in src
    assert 'onclick="srcPage(1)"' not in src
    # 筛选 + 展开/收起仍在
    assert "function srcFiltered()" in src
    assert "function onSrcFilter(v)" in src
    assert "function onSrcEnabledFilter(v)" in src
    assert "function toggleSrcExpand()" in src
    assert 'onclick="toggleSrcExpand()"' in src
    # renderSources 一次渲染全部（不再切片）
    assert "srcsAll=srcFiltered()" in src
    assert "slice((SRC_PAGE-1)*SRC_PAGE_SIZE,SRC_PAGE*SRC_PAGE_SIZE)" not in src
    assert "没有匹配的资源站" in src
    # 容器自适应（列宽固定 + 可滚动 + 展开态）
    assert ".src-wrap{" in src
    assert "table-layout:fixed" in src
    assert ".src-wrap.expanded{" in src


def test_sources_filter_logic_with_node():
    """筛选逻辑（不再涉及翻页）。"""
    src = _src()
    block = _src_filter_block(src)
    script = (
        "let CFG={sources:[]};\\n"
        "for(let i=0;i<25;i++)CFG.sources.push(\\n"
        "  {id:'s'+i,name:'站'+i,api:'https://h'+i+'.example/api',enabled:i%2===0});\\n"
        "let rendered=0;function renderSources(){rendered++;}\\n"
        "let $=(id)=>({textContent:'',value:'',className:''});\\n"
        + block +
        "function eq(a,b,msg){if(a!==b)throw new Error(msg+': got '+a+' want '+b);}\\n"
        "eq(srcFiltered().length,25,'默认不过滤');\\n"
        "onSrcFilter('站2');\\n"
        "eq(srcFiltered().length,6,'关键字 站2 命中 站2/站20-24');\\n"
        "onSrcFilter('');onSrcEnabledFilter('on');\\n"
        "eq(srcFiltered().length,13,'仅已启用');\\n"
        "onSrcFilter('站2');\\n"
        "eq(srcFiltered().length,4,'关键字+仅已启用');\\n"
        "onSrcFilter('');onSrcEnabledFilter('all');\\n"
        "eq(srcFiltered().length,25,'清空筛选');\\n"
        "if(rendered<1)throw new Error('筛选未触发重绘');\\n"
        "console.log('src filter logic ok');\\n"
    )
    out = _node_ok(script)
    assert "src filter logic ok" in out
'''

p.write_text(s[:i] + NEW + s[j:], encoding="utf-8")
print("rewritten")
