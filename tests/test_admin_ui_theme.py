"""Exercise the real pre-paint theme script with mocked browser state."""
import re

from test_admin_ui_p1 import _node_ok, _src


def test_admin_theme_restores_switches_and_resets():
    head = _src().split("</head>", 1)[0]
    bootstrap = re.search(r"<script>(.*?)</script>", head, re.S).group(1)
    assert head.index(bootstrap) < head.index("<style>")
    _node_ok(r'''
const assert = require('node:assert/strict');
const key = 'fnmedia-ext.admin.theme.v1';
const root = {dataset:{}, style:new Map()};
root.style.setProperty = root.style.set.bind(root.style);
const system = {matches:true, addEventListener(_, fn){this.change = fn;}};
const window = {matchMedia:()=>system, addEventListener(type, fn){this[type] = fn;}};
const document = {documentElement:root};
const store = new Map([['unrelated','untouched'], [key, JSON.stringify({
  mode:'light', palette:'custom', custom:'#FFFF00'
})]]);
let blocked = false;
Object.defineProperty(window, 'localStorage', {get(){
  if(blocked) throw new Error('storage blocked');
  return {getItem:k=>store.get(k), setItem:(k,v)=>store.set(k,v), removeItem:k=>store.delete(k)};
}});
''' + bootstrap + r'''
assert.deepEqual(adminTheme.get(), {mode:'light', palette:'custom', custom:'#ffff00', family:'classic'});
assert.equal(root.dataset.theme, 'light');
assert.equal(root.style.get('--accent-on'), '#000');
assert.equal(root.style.get('--accent-fill'), '#ffff00');
assert.equal(adminTheme.validHex('#123'), false);
const feedback = [];
adminTheme.subscribe(saved=>feedback.push(saved));
adminTheme.set({mode:'dark', palette:'teal'});
assert.equal(root.dataset.theme, 'dark');
assert.equal(root.style.get('--accent-fill'), '#0f766e');
assert.equal(JSON.parse(store.get(key)).custom, '#ffff00');
system.matches = false;
system.change();
assert.equal(root.dataset.theme, 'dark');
adminTheme.set({mode:'system'});
assert.equal(root.dataset.theme, 'light');
system.matches = true;
system.change();
assert.equal(root.dataset.theme, 'dark');
store.set(key, JSON.stringify({mode:'invalid', palette:['teal'], custom:'not a color'}));
window.storage({key});
assert.deepEqual(adminTheme.get(), {mode:'system', palette:'indigo', custom:'#4f46e5', family:'classic'});
store.set(key, '{broken');
window.storage({key});
assert.equal(root.style.get('--accent-fill'), '#4f46e5');
blocked = true;
adminTheme.set({mode:'light', palette:'custom', custom:'#000000'});
assert.equal(root.style.get('--accent-on'), '#fff');
assert.equal(root.dataset.theme, 'light');
assert.equal(feedback.at(-1), false);
blocked = false;
adminTheme.reset();
assert.equal(store.has(key), false);
assert.equal(store.get('unrelated'), 'untouched');
assert.equal(root.dataset.theme, 'dark');
assert.equal(root.style.get('--accent-fill'), '#4f46e5');
assert.equal(feedback.at(-1), true);

// Derived ink and button labels meet normal-text contrast in both modes.
function rgb(value){
  if(value.startsWith('rgb')) return value.match(/\d+/g).map(Number);
  const hex = value.slice(1);
  return (hex.length === 3 ? [...hex].map(c=>c+c) : hex.match(/../g)).map(c=>parseInt(c,16));
}
function lum(value){
  return rgb(value).map(n=>n/255).map(n=>n<=.04045?n/12.92:((n+.055)/1.055)**2.4)
    .reduce((acc,n,i)=>acc+n*[.2126,.7152,.0722][i],0);
}
function assertContrast(fg,bg){
  const [a,b] = [lum(fg),lum(bg)];
  assert.ok((Math.max(a,b)+.05)/(Math.min(a,b)+.05)>=4.5, `${fg} on ${bg}`);
}
for(const [mode,surfaces] of Object.entries({
  light:['#fff','#f4f6fb','#f7f8fc'], dark:['#171c28','#10131c','#1e2433']
})){
  for(const custom of [...Object.values(adminTheme.presets), '#ffffff', '#000000', '#777777', '#ffff00']){
    adminTheme.set({mode, palette:'custom', custom});
    for(const surface of [...surfaces,root.style.get('--accent-soft')]){
      assertContrast(root.style.get('--accent'),surface);
    }
    assertContrast(root.style.get('--accent-on'),root.style.get('--accent-fill'));
  }
}
''')


def test_harmony_theme_presets_are_available_and_accessible():
    src = _src()
    for palette in ("appleSky", "appleMidnight", "appleSunset", "appleForest",
                    "appleLavender", "harmonyAurora", "harmonySky", "harmonyDream"):
        assert f'data-palette="{palette}"' in src
        assert palette in src.split("const presets = {", 1)[1].split("};", 1)[0]
    assert 'name="themeFamily" value="apple"' in src
    assert 'name="themeFamily" value="harmony"' in src


def test_extension_switch_is_present_on_overview():
    src = _src()
    assert 'id="extensionEnabled" onchange="saveExtensionEnabled()"' in src
    assert 'id="extensionEnabledMsg"' in src
    assert 'CFG.extension_enabled!==false' in src
