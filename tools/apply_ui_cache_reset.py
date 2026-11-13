"""第 3/4/6 项的 UI 改动：
  3) 网盘播放缓存：手动清理按钮 + 保留天数（1/3/7）+ 自动清理开关；
  4) 「扫码登录」→「网盘登录」；
  6) 一键重置：清空所有设置与资源站列表（可恢复内置）。
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
        raise SystemExit(f"[FAIL] {tag}: 锚点未找到 in {rel}\n---\n{old[:400]}")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    done.append(tag)
    print(f"  [ok] {tag}")


# ---------- 4) 文案：扫码登录 → 网盘登录 ----------
edit("proxy/admin_ui.py",
     '''      <button class="nav-item" data-page="scanlogin">扫码登录</button>''',
     '''      <button class="nav-item" data-page="scanlogin">网盘登录</button>''',
     "nav 文案 网盘登录")

edit("proxy/admin_ui.py",
     '''      <h2 class="page-title">扫码登录</h2>
      <p class="page-desc">各站点的登录入口集中在这里。第三方网盘登录后网盘高清线路才能自动转存；B 站登录后可拉取高清与会员内容。</p>''',
     '''      <h2 class="page-title">网盘登录</h2>
      <p class="page-desc">选择网盘进行登录授权。第三方网盘登录后网盘高清线路才能自动转存；B 站登录后可拉取高清与会员内容。</p>''',
     "页面标题 网盘登录")

# ---------- 3) 网盘区：缓存清理 + 天数 + 开关 ----------
edit("proxy/admin_ui.py",
     '''        <div class="hint" id="panScanMsg">未加载</div>
        <iframe id="panScanFrame" title="第三方网盘扫码登录" data-src="/v/api/ext/pan/page"
          style="display:none;width:100%;height:640px;border:1px solid var(--line);border-radius:12px;margin-top:10px"></iframe>
      </div>''',
     '''        <div class="hint" id="panScanMsg">未加载</div>
        <iframe id="panScanFrame" title="第三方网盘扫码登录" data-src="/v/api/ext/pan/page"
          style="display:none;width:100%;height:640px;border:1px solid var(--line);border-radius:12px;margin-top:10px"></iframe>
      </div>

      <div class="card">
        <div class="card-title">网盘播放缓存</div>
        <p class="card-sub">转存缓存占满会导致无法播放。默认保留 1 天自动清理，也可手动清理。</p>
        <label class="switch-row"><input type="checkbox" id="panCacheAuto" checked/>
          <span>定时自动清理<span class="sub">已开启自动清理，到期自动清除网盘播放缓存</span></span></label>
        <label class="field"><span>缓存保留天数</span>
          <select id="panCacheDays">
            <option value="1">1 天</option>
            <option value="3">3 天</option>
            <option value="7">7 天</option>
          </select></label>
        <div class="btn-row">
          <button class="btn primary small" onclick="clearPanCache()">立即清理播放缓存</button>
          <button class="btn small" onclick="savePanCache()">保存设置</button>
        </div>
        <div class="hint" id="panCacheMsg">未清理</div>
      </div>''',
     "网盘缓存 UI")

# ---------- 6) 一键重置 ----------
edit("proxy/admin_ui.py",
     '''      <div class="card">
        <div class="card-title">网盘播放缓存</div>''',
     '''      <div class="card">
        <div class="card-title">一键重置</div>
        <p class="card-sub">清空所有设置与已导入的资源站列表，回到初始状态。重置后可在「资源站」里用「恢复内置」找回内置源。</p>
        <div class="btn-row">
          <button class="btn danger small" onclick="resetAll()">一键重置所有设置</button>
        </div>
        <div class="hint" id="resetMsg"></div>
      </div>

      <div class="card">
        <div class="card-title">网盘播放缓存</div>''',
     "一键重置 UI")

# ---------- JS ----------
edit("proxy/admin_ui.py",
     '''// >>> PRIVATE:SCANLOGIN
async function openPanScan(){''',
     '''// >>> PRIVATE:SCANLOGIN
async function savePanCache(){
  const days=parseInt($("panCacheDays").value||"1",10);
  const enabled=$("panCacheAuto").checked;
  try{
    const j=await jpost("/v/api/ext/config",{pan_cache_ttl_days:days,pan_cache_cleanup_enabled:enabled});
    if(j.code!==0)throw new Error(j.msg||"保存失败");
    $("panCacheMsg").textContent=enabled?("已开启自动清理，到期自动清除网盘播放缓存"):"已关闭自动清理";
    toast("网盘缓存设置已保存");
  }catch(e){$("panCacheMsg").textContent=e.message;toast("保存失败："+e.message,"fail");}
}
async function clearPanCache(){
  $("panCacheMsg").textContent="正在清理播放缓存，请稍候";
  try{
    const j=await jpost("/v/api/ext/pan/cache/clear",{});
    if(j.code!==0)throw new Error(j.msg||"清理失败");
    const d=j.data||{};
    if(!d.total){$("panCacheMsg").textContent="暂无播放缓存需要清理";toast("暂无播放缓存需要清理");return;}
    $("panCacheMsg").textContent="✅ 播放缓存清理完成";
    toast("✅ 播放缓存清理完成");
  }catch(e){$("panCacheMsg").textContent="清理失败："+e.message;toast("清理失败："+e.message,"fail");}
}
async function resetAll(){
  if(!confirm("确定一键重置？\\n\\n将清空所有设置与已导入的资源站列表。\\n重置后可在「资源站」用「恢复内置」找回内置源。"))return;
  try{
    const j=await jpost("/v/api/ext/config/reset",{});
    if(j.code!==0)throw new Error(j.msg||"重置失败");
    $("resetMsg").textContent="✅ 重置完成";
    toast("✅ 重置完成，页面即将刷新");
    setTimeout(()=>location.reload(),1200);
  }catch(e){$("resetMsg").textContent="重置失败："+e.message;toast("重置失败："+e.message,"fail");}
}
async function openPanScan(){''',
     "JS 缓存清理与重置")

print()
print("done:", ", ".join(done) if done else "(none)")