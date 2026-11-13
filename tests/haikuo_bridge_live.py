"""Read-only four-rule production bridge probe; never prints signed URLs/cookies.
Run with the project venv: python tests/haikuo_bridge_live.py [report.json].
The episode predicate is compiled from app.py (no app import/startup effects).
"""
from __future__ import annotations
import ast
import collections
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'proxy'))
os.environ['NO_PROXY'] = os.environ['no_proxy'] = 'localhost,127.0.0.1'
os.environ['HAIKUO_STORE_DIR'] = tempfile.mkdtemp(prefix='hk-bridge-live-')
import httpx
import features
# Read-only Haikuo probe: do not initialize unrelated account/source modules.
features.BILIBILI = False
import haikuo as hk
import providers
import drpy

RULES = [('h41ce81bcc9e9', '影探4K'), ('h3cb690512974', '91短剧'),
         ('h91845e095e9f', '鱼安影视'), ('h2aefa89d8f26', '4K动漫')]

def production_filter():
    tree = ast.parse((ROOT / 'proxy/app.py').read_text())
    names = {'_HAIKUO_DETAIL_NAV_COL_TYPES', '_HAIKUO_IMAGE_SUFFIXES',
             '_HAIKUO_RECOMMEND_MARKERS', '_HAIKUO_DETAIL_ACTION_TITLES',
             '_haikuo_detail_action', '_haikuo_filter_episodes'}
    selected = [n for n in tree.body if
                (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names)
                or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets))]
    ns = {'re': re}
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'app.py:episode-filter', 'exec'), ns)
    return ns['_haikuo_filter_episodes']

FILTER = production_filter()

def safe_error(e):
    s = re.sub(r'(?:https?://|haikuo-play:)[^\s\"\']+', '<redacted-url>', str(e))
    s = re.sub(r'(?i)(cookie|token|authorization|signature)\s*[:=]\s*[^\s,]+', r'\1=<redacted>', s)
    return type(e).__name__ + ': ' + s[:220]

def role_counts(items):
    return dict(collections.Counter(providers._haikuo_item_role(v) for v in items))

def shape(data):
    b = data.lstrip(b'\xef\xbb\xbf\r\n ')
    if b.startswith(b'#EXTM3U'): return 'm3u8'
    if b'ftyp' in b[:24] or b'moof' in b[:24] or b'styp' in b[:24]: return 'mp4/fmp4'
    if len(b) > 188 and b[0] == 0x47 and b[188] == 0x47: return 'mpeg-ts'
    if b[:16].lower().startswith((b'<!doctype', b'<html')): return 'html'
    return 'binary' if b else 'empty'

def fetch_small(client, url, headers, cap=262144, ranged=False):
    h = dict(headers or {})
    if ranged: h['Range'] = 'bytes=0-2047'
    with client.stream('GET', url, headers=h) as r:
        data = bytearray()
        for part in r.iter_bytes(2048):
            data.extend(part[:cap-len(data)])
            if len(data) >= cap: break
        return r.status_code, bytes(data), str(r.url)

def verify(res):
    url = str(res.get('url') or '')
    headers = res.get('headers') or {}
    out = {'playable': bool(res.get('playable')), 'sniff': bool(res.get('sniff'))}
    if url.startswith('file://'):
        p = hk.local_file_for_url(url)
        out['local_playlist'] = bool(p and hk.local_file_is_playlist(p))
        return out
    if not url.startswith(('https://', 'http://')): return out
    try:
        with httpx.Client(timeout=18, follow_redirects=True, trust_env=False) as client:
            for depth in range(3):
                status, body, final = fetch_small(client, url, headers, ranged=depth == 0)
                out.setdefault('http', []).append({'status': status, 'shape': shape(body), 'bytes': len(body)})
                if status not in (200, 206) or shape(body) != 'm3u8': break
                text = body.decode('utf-8', 'replace')
                out['aes128'] = 'METHOD=AES-128' in text
                links = [x.strip() for x in text.splitlines() if x.strip() and not x.startswith('#')]
                if not links: break
                if '#EXT-X-STREAM-INF:' in text:
                    url = urljoin(final, links[0]); continue
                status, segment, _ = fetch_small(client, urljoin(final, links[0]), headers, cap=2048, ranged=True)
                out['segment'] = {'status': status, 'shape': shape(segment), 'bytes': len(segment)}
                break
    except Exception as exc:
        out['verify_error'] = safe_error(exc)
    return out

def run_one(key, name):
    source = {'id': 'haikuo-sub-' + key, 'name': name, 'haikuo': True,
              'api': 'haikuo://sub/' + key + '.json'}
    out = {'rule': name, 'key': key}
    start = time.monotonic()
    try:
        meta = hk.load(key)
        home = hk.home(key, 1)
        out['home'] = {'count': len(home), 'roles': role_counts(home)}
        classes = meta.get('classes') or []
        folders = [x for x in home if providers._haikuo_item_role(x) == 'folder'
                   and not re.search('搜索|设置|收藏|历史', x.get('title', ''))]
        folders.sort(key=lambda x: 0 if '最新电影' in x.get('title', '') else 1)
        if classes:
            category = hk.category(key, classes[0]['url'], 1)
        elif folders:
            category = hk.navigate(key, folders[0]['url'], 1)
        else:
            category = home
            out['category_note'] = 'no recognized category; video selection from home'
        out['category'] = {'count': len(category), 'roles': role_counts(category)}
        videos = [v for v in category if providers._haikuo_item_role(v) == 'video']
        if not videos:
            videos = [v for v in home if providers._haikuo_item_role(v) == 'video']
            out['detail_from_home_fallback'] = True
        out['attempts'] = []
        for v in videos[:2]:
            attempt = {}
            out['attempts'].append(attempt)
            try:
                mapped = providers._haikuo_map_item(source, v)
                attempt['extra_id_present'] = bool((v.get('extra') or {}).get('id'))
                attempt['mapped_identity_distinct'] = mapped.vod_id != str(v.get('url') or '')
                try:
                    detail = hk.detail(key, mapped.vod_id)
                    episodes = FILTER(detail)
                    attempt['detail'] = {'count': len(detail), 'episodes': len(episodes)}
                except Exception as exc:
                    if 'lazy play token is not a detail link' not in str(exc): raise
                    episodes = [{'url': mapped.vod_id}]
                    attempt['detail'] = {'direct_lazy': True}
                attempt['plays'] = []
                for ep in episodes[:5]:
                    res = hk.play(key, ep['url'])
                    check = verify(res)
                    if not res.get('playable') and not res.get('sniff'):
                        check['reason'] = safe_error(RuntimeError(res.get('reason') or 'not playable'))
                    attempt['plays'].append(check)
                    if res.get('playable'): break
            except Exception as exc: attempt['error'] = safe_error(exc)
            if any(p.get('segment', {}).get('status') in (200, 206) for p in attempt.get('plays', [])): break
    except Exception as exc: out['error'] = safe_error(exc)
    out['seconds'] = round(time.monotonic()-start, 2)
    return out

def main():
    results = []
    try:
        for key, name in RULES:
            row = run_one(key, name)
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if len(sys.argv) > 1:
                Path(sys.argv[1]).write_text(json.dumps(results, ensure_ascii=False, indent=2))
    finally: drpy._sidecar.stop()
    return results

if __name__ == '__main__': main()
