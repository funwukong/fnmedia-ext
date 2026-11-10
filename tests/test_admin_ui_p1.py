"""P1-16 / P1-22 管理台内联 JS 回归测试。

P1-22：默认权限开关（aclDefVis / aclDefOnline）显示必须与后端默认
True 一致——字段缺失时勾选。fail-open 回滚后用 ``!==false``，缺失字段
也被当成勾选；只有显式 false 才不勾选。

P1-16：资源站前端分页/筛选、启用开关失败回滚、切页停实时轮询。

JS 行为用 node 直接对文件里抽出的原函数求值（不是复述测试自己的
副本）；抽取失败即测试失败。剥离树上同样要跑通：断言与抽取只涉及
PRIVATE 标记块之外的代码。
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
UI = os.path.join(ROOT, "proxy", "admin_ui.py")


def _src() -> str:
    return open(UI, encoding="utf-8").read()


def _node_ok(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("本机无 node，跳过 JS 行为检查")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                    encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run([node, path], capture_output=True,
                           text=True, timeout=60)
    finally:
        os.unlink(path)
    assert r.returncode == 0, f"node 检查失败：\n{r.stdout}\n{r.stderr}"
    return r.stdout


# ---------------- P1-22：默认权限开关 ----------------

def test_acl_default_uses_not_false():
    src = _src()
    assert "acl_default_visible!==false" in src
    assert "acl_default_online!==false" in src
    assert "acl_default_visible===true" not in src
    assert "acl_default_online===true" not in src
    # HTML 初始无 checked：初始勾选由 JS 按 ACLDEF（默认 True）设置，
    # 不写死在 HTML 里。
    for input_id in ("aclDefVis", "aclDefOnline"):
        m = re.search(r'<input[^>]*id="%s"[^>]*>' % input_id, src)
        assert m, input_id
        assert "checked" not in m.group(0), input_id


def test_acl_default_three_states_with_node():
    src = _src()
    vis = re.search(
        r"r\.data&&r\.data\.acl_default_visible(?:===true|!==false)",
        src)
    on = re.search(
        r"r\.data&&r\.data\.acl_default_online(?:===true|!==false)",
        src)
    assert vis and on, "loadAccounts 里找不到 ACLDEF 判断表达式"
    script = (
        "const vis=(r)=>{return %s;};\n"
        "const onl=(r)=>{return %s;};\n"
        "const cases=[\n"
        # r.data 缺失时表达式短路为 undefined（假值）；实测中 data 恒在，字段缺失见下条
        "  [{}, false, false],\n"
        "  [{data:{}}, true, true],\n"
        "  [{data:{acl_default_visible:true,acl_default_online:true}}, true, true],\n"
        "  [{data:{acl_default_visible:false,acl_default_online:false}}, false, false],\n"
        "];\n"
        "for(const [r,wantV,wantO] of cases){\n"
        "  if(!!vis(r)!==wantV||!!onl(r)!==wantO)\n"
        "    throw new Error('ACLDEF 三态不符: '+JSON.stringify(r)\n"
        "      +' vis='+vis(r)+' onl='+onl(r));\n"
        "}\n"
        "console.log('acl three-state ok');\n"
        % (vis.group(0), on.group(0))
    )
    out = _node_ok(script)
    assert "acl three-state ok" in out


# ---------------- P1-16：资源站分页/筛选 ----------------

def _src_pager_block(src: str) -> str:
    start = src.index("let SRC_FILTER")
    end = src.index("function srcPage(d)")
    end = src.index("\n", end) + 1
    return src[start:end]


def test_sources_pager_controls_present():
    src = _src()
    for html_id in ("srcFilter", "srcEnabledFilter", "srcPageInfo"):
        assert f'id="{html_id}"' in src, html_id
    assert "const SRC_PAGE_SIZE=20" in src
    assert "function srcFiltered()" in src
    assert "function onSrcFilter(v)" in src
    assert "function onSrcEnabledFilter(v)" in src
    assert "function srcPage(d)" in src
    # renderSources 按筛选结果分页切片，并给出空筛选提示。
    assert "srcsAll=srcFiltered()" in src
    assert "slice((SRC_PAGE-1)*SRC_PAGE_SIZE,SRC_PAGE*SRC_PAGE_SIZE)" in src
    assert "没有匹配的资源站" in src


def test_sources_filter_and_page_logic_with_node():
    src = _src()
    block = _src_pager_block(src)
    script = (
        "let CFG={sources:[]};\n"
        "for(let i=0;i<25;i++)CFG.sources.push(\n"
        "  {id:'s'+i,name:'站'+i,api:'https://h'+i+'.example/api',enabled:i%2===0});\n"
        "let rendered=0;function renderSources(){rendered++;}\n"
        + block +
        "function eq(a,b,msg){if(a!==b)throw new Error(msg+': got '+a+' want '+b);}\n"
        "eq(srcFiltered().length,25,'默认不过滤');\n"
        "onSrcFilter('站2');\n"
        "eq(srcFiltered().length,6,'关键字 站2 命中 站2/站20-24');\n"
        "eq(SRC_PAGE,1,'筛选后回到第 1 页');\n"
        "onSrcFilter('');onSrcEnabledFilter('on');\n"
        "eq(srcFiltered().length,13,'仅已启用');\n"
        "onSrcFilter('站2');\n"
        "eq(srcFiltered().length,4,'关键字+仅已启用');\n"
        "onSrcFilter('');onSrcEnabledFilter('all');\n"
        "srcPage(2);eq(SRC_PAGE,3,'翻页');\n"
        "onSrcFilter('站2');eq(SRC_PAGE,1,'再次筛选回到第 1 页');\n"
        "if(rendered<1)throw new Error('筛选/翻页未触发重绘');\n"
        "console.log('src pager logic ok');\n"
    )
    out = _node_ok(script)
    assert "src pager logic ok" in out


# ---------------- P1-16：启用开关失败回滚 ----------------

def _fn_body(src: str, start_pat: str, end_pat: str) -> str:
    start = src.index(start_pat)
    end = src.index(end_pat, start)
    return src[start:end]


def test_toggle_enable_rolls_back_on_failure():
    src = _src()
    body = _fn_body(src, "async function toggleEnable(id,on){",
                    "async function toggleStandalone")
    assert 'catch(e){toast("保存失败","fail");renderSources();return;}' in body, \
        "toggleEnable 异常分支必须重绘回滚"
    assert 'else{toast(r.msg||"保存失败","fail");renderSources();}' in body, \
        "toggleEnable 失败分支必须重绘回滚"


def test_toggle_enable_rollback_with_node():
    src = _src()
    body = _fn_body(src, "async function toggleEnable(id,on){",
                    "async function toggleStandalone")
    script = (
        "let CFG={sources:[{id:'s1',enabled:false},{id:'s2',enabled:true}]};\n"
        "let rendered=0;function renderSources(){rendered++;}\n"
        "function toast(m){}\n"
        "function $(id){return {textContent:''};}\n"
        "let jpostImpl=async()=>({code:1,msg:'nope'});\n"
        "async function jpost(){return jpostImpl();}\n"
        + body +
        "(async()=>{\n"
        "  await toggleEnable('s1',true);\n"
        "  if(CFG.sources[0].enabled!==false)throw new Error('失败时 CFG 被改');\n"
        "  if(rendered!==1)throw new Error('失败时未重绘回滚 rendered='+rendered);\n"
        "  jpostImpl=async()=>{throw new Error('net');};\n"
        "  await toggleEnable('s1',true);\n"
        "  if(rendered!==2)throw new Error('异常时未重绘回滚 rendered='+rendered);\n"
        "  jpostImpl=async()=>({code:0});\n"
        "  await toggleEnable('s1',true);\n"
        "  if(CFG.sources[0].enabled!==true)throw new Error('成功时未应用');\n"
        "  console.log('toggleEnable rollback ok');\n"
        "})();\n"
    )
    out = _node_ok(script)
    assert "toggleEnable rollback ok" in out


# ---------------- P1-16：切页停实时轮询 ----------------

def test_live_poll_stop_present():
    src = _src()
    assert "function stopLivePoll(){ if(LIVE_TIMER){clearInterval(LIVE_TIMER);LIVE_TIMER=null;} }" in src
    assert "clearInterval(LIVE_TIMER)" in src
    # 切离概览页即停；切回概览才启。
    body = _fn_body(src, "async function switchPage(page){",
                    '$$("[data-page]").forEach(b=>b.addEventListener')
    assert "else{stopLivePoll();}" in body
    assert "startLivePoll();" in body


def test_live_poll_stops_when_leaving_overview_with_node():
    src = _src()
    live_decl = "let LIVE_TIMER=null;\n"
    start = _fn_body(src, "function startLivePoll(){",
                     "function renderOverview(){")
    switch = _fn_body(src, "async function switchPage(page){",
                      '$$("[data-page]").forEach(b=>b.addEventListener')
    script = (
        "const _set=setInterval,_clear=clearInterval;\n"
        "let liveIds=new Set();\n"
        "globalThis.setInterval=(f,ms)=>{const id=_set(f,ms);liveIds.add(id);return id;};\n"
        "globalThis.clearInterval=(id)=>{liveIds.delete(id);_clear(id);};\n"
        "globalThis.window={scrollTo(){}};\n"
        "async function loadLive(){}\n"
        "function renderOverview(){}\n"
        "function loadLibs(){return Promise.resolve();}\n"
        "function renderLibrary(){}\n"
        "function renderQuality(){}\n"
        "function loadHealth(){}\n"
        "function initSrcProxy(){}\n"
        "function loadCacheStatus(){}\n"
        "function loadGuardZone(){}\n"
        "function gbInit(){}\n"
        "function loadUpdate(){}\n"
        "function loadAdminToken(){}\n"
        "async function loadStatus(){}\n"
        "function loadAdfilter(){}\n"
        "function loadSpiderMgmt(){}\n"
        "function loadDrpyMgmt(){}\n"
        "function $$(sel){return [{id:'page-overview',dataset:{page:'overview'},classList:{toggle(){}}}];}\n"
        + live_decl + start + switch +
        "(async()=>{\n"
        "  await switchPage('overview');\n"
        "  if(liveIds.size!==1)throw new Error('概览页未启动轮询 live='+liveIds.size);\n"
        "  await switchPage('sources');\n"
        "  if(liveIds.size!==0)throw new Error('切离概览后轮询未停 live='+liveIds.size);\n"
        "  await switchPage('overview');\n"
        "  if(liveIds.size!==1)throw new Error('切回概览后轮询未重启 live='+liveIds.size);\n"
        "  await switchPage('library');\n"
        "  if(liveIds.size!==0)throw new Error('切到媒体库后轮询未停 live='+liveIds.size);\n"
        "  console.log('live poll stop ok');\n"
        "})();\n"
    )
    out = _node_ok(script)
    assert "live poll stop ok" in out
