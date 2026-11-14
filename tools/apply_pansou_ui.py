"""管理页：PanSou 网盘搜索设置（地址/云类型/超时/条数/开关）。

上游 pansou.py 的配置键与这里一一对应；默认实例已不可达，用户需改成
自己的自托管地址，否则网盘搜索线路没有结果。
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


# ---------- 1) admin_api：允许写 PanSou 配置 ----------
edit("proxy/admin_api.py",
     '        "pan_cache_ttl_days",\n',
     '        "pan_cache_ttl_days",\n'
     '        "pansou_enabled", "pansou_url", "pansou_cloud_types",\n'
     '        "pansou_src", "pansou_timeout", "pansou_limit",\n',
     "admin_api pansou allowed keys")

# ---------- 2) admin_ui：PanSou 设置卡片 ----------
edit("proxy/admin_ui.py",
     '''      <div class="card">
        <div class="card-title">网盘播放缓存</div>''',
     '''      <div class="card">
        <div class="card-title">PanSou 网盘搜索</div>
        <p class="card-sub">网盘搜索线路的来源。填自己的自托管 PanSou 实例地址（上游 <code>fish2018/pansou</code>）。</p>
        <label class="field"><span>实例地址</span>
          <input type="text" id="pansouUrl" placeholder="http://你的NAS:8888"/></label>
        <label class="field"><span>网盘类型（逗号分隔）</span>
          <input type="text" id="pansouClouds" placeholder="quark,uc,aliyun"/></label>
        <div class="row">
          <label class="field" style="max-width:140px"><span>单次超时（秒）</span>
            <input type="number" id="pansouTimeout" min="3" max="120" style="width:100%"/></label>
          <label class="field" style="max-width:140px"><span>结果条数上限</span>
            <input type="number" id="pansouLimit" min="1" max="80" style="width:100%"/></label>
        </div>
        <label class="switch-row"><input type="checkbox" id="pansouEnabled" checked/>
          <span>启用网盘搜索线路<span class="sub">搜索结果作为真实的一集出现在线路里，播放时自动转存</span></span></label>
        <div class="btn-row">
          <button class="btn primary small" onclick="savePansouCfg()">保存</button>
          <span class="hint" id="pansouCfgMsg"></span>
        </div>
      </div>

      <div class="card">
        <div class="card-title">网盘播放缓存</div>''',
     "admin_ui pansou cfg card")

# ---------- 3) admin_ui：JS 载入与保存 ----------
edit("proxy/admin_ui.py",
     '''async function savePanCache(){''',
     '''async function loadPansouCfg(){
  try{
    const cfg=(await jget('/v/api/ext/status')).data.config||{};
    const set=(id,v)=>{const el=$(id);if(el)el.value=v;};
    set('pansouUrl',cfg.pansou_url||'');
    set('pansouClouds',cfg.pansou_cloud_types||'quark,uc,aliyun');
    set('pansouTimeout',cfg.pansou_timeout||30);
    set('pansouLimit',cfg.pansou_limit||30);
    const en=$('pansouEnabled');if(en)en.checked=cfg.pansou_enabled!==false;
  }catch(e){}
}
async function savePansouCfg(){
  const body={
    pansou_url:($('pansouUrl').value||'').trim().replace(/\\/+$/,''),
    pansou_cloud_types:($('pansouClouds').value||'quark,uc,aliyun').trim(),
    pansou_timeout:parseInt($('pansouTimeout').value||'30',10),
    pansou_limit:parseInt($('pansouLimit').value||'30',10),
    pansou_enabled:!!$('pansouEnabled').checked};
  try{
    const j=await jpost('/v/api/ext/config',body);
    if(j.code!==0)throw new Error(j.msg||'保存失败');
    $('pansouCfgMsg').textContent='已保存';
    toast('网盘搜索设置已保存');
  }catch(e){$('pansouCfgMsg').textContent=e.message;toast('保存失败：'+e.message,'fail');}
}
async function savePanCache(){''',
     "admin_ui pansou cfg js")

# ---------- 4) admin_ui：进入网盘登录页时载入 ----------
edit("proxy/admin_ui.py",
     "  if(page===\"scanlogin\"){try{await refreshPanScan();}catch(e){}}",
     "  if(page===\"scanlogin\"){try{await refreshPanScan();}catch(e){}try{await loadPansouCfg();}catch(e){}}",
     "admin_ui scanlogin load cfg")

print()
print("done:", ", ".join(done) if done else "(none)")