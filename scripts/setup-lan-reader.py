#!/usr/bin/env python3
"""Create isolated, anonymous read-only views; generate a token-free LAN index.

Run on the Teable host. Default only prints a plan. Nginx must separately restrict
the app and /read to authorized company networks; share links do not enforce IPs.
"""
import argparse
import html
import json
from pathlib import Path
import runpy
import urllib.error
import urllib.parse
import urllib.request

BASE = 'bseCHXUwSZVcKSUFqDQ'
NAME = '局域网只读'
META = {'allowCopy': False, 'includeHiddenField': False,
        'includeRecords': False, 'allowEdit': False}


def anonymous(route, method='GET', payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request('http://127.0.0.1:3000/api' + route,
                                 data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read() or b'null')
    except urllib.error.HTTPError as error:
        return error.code, None


def render(entries):
    links = ''.join(
        '<a class="tab" target="reader" href="/share/{}/view">{}</a>'.format(
            urllib.parse.quote(e['shareId'], safe=''), html.escape(e['name']))
        for e in entries)
    first = urllib.parse.quote(entries[0]['shareId'], safe='')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>外设海外订单 · 只读</title>
<style>*{box-sizing:border-box}body{margin:0;font:15px system-ui,"Noto Sans CJK SC",sans-serif;color:#172238;background:#f3f6fa}
header{padding:16px 22px;background:white;border-bottom:1px solid #dbe2ec}h1{font-size:20px;margin:0 0 5px}
p{margin:0;color:#52627a;font-size:13px}nav{display:flex;gap:8px;flex-wrap:wrap;padding:12px 22px}
a{color:#2456aa;text-decoration:none}.tab{padding:8px 14px;background:white;border:1px solid #d7e0ed;border-radius:7px}
.tab:hover,.tab.active{color:white;background:#2456aa}header>a{float:right;padding:8px}
iframe{display:block;width:100%;height:calc(100vh - 150px);min-height:420px;border:0;background:white}</style>
<header><a href="/base/''' + BASE + '''">员工登录 / 编辑</a><h1>外设海外订单 · 只读</h1>
<p>公司局域网免登录查看。数据来自当前数据库；修改需使用已授权的员工账号。</p></header>
<nav>''' + links + '''</nav><iframe name="reader" title="数据库只读视图" src="/share/''' + first + '''/view"></iframe>
<script>const tabs=document.querySelectorAll('.tab');tabs[0].classList.add('active');
tabs.forEach(a=>a.addEventListener('click',()=>{tabs.forEach(x=>x.classList.remove('active'));a.classList.add('active')}));</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--work', type=Path, default=Path('/home/jjj/db_server_setup/lan-reader-20261021'))
    parser.add_argument('--helper', type=Path, default=Path('/home/jjj/db_server_setup/orders-import-20261020/import-teable-orders.py'))
    args = parser.parse_args()
    helper = runpy.run_path(str(args.helper))
    api = helper['Api']('http://127.0.0.1:3000', Path('/home/jjj/.config/db-server/teable-automation.token').read_text().strip())
    tables = api.call('GET', f'/base/{BASE}/table')
    if len(tables) != 7:
        raise RuntimeError('Expected the approved seven-table base; no changes made.')
    snapshots = []
    for table in tables:
        tid = table['id']
        views = api.call('GET', f'/table/{tid}/view')
        fields = api.call('GET', f'/table/{tid}/field')
        source = next((v for v in views if v['name'] == '全部月份'), views[0])
        visible = {f['id'] for f in fields if source['columnMeta'].get(f['id'], {}).get('hidden') is False}
        if not visible or source['type'] != 'grid':
            raise RuntimeError('Expected an explicitly configured grid with visible fields.')
        snapshots.append({'table': table, 'views': views, 'fields': fields,
                          'visible': sorted(visible), 'source': source})
    if not args.apply:
        print(json.dumps({'plan': [{'table': s['table']['name'], 'visibleFields': len(s['visible'])} for s in snapshots], 'shareMeta': META}, ensure_ascii=False))
        return
    args.work.mkdir(parents=True, exist_ok=True)
    args.work.chmod(0o700)
    backup = args.work / 'before-reader-private.json'
    if not backup.exists():
        helper['save'](backup, snapshots)
    entries = []
    for snapshot in snapshots:
        tid = snapshot['table']['id']
        existing = next((v for v in snapshot['views'] if v['name'] == NAME), None)
        if existing:
            view = existing
            if view['columnMeta'] != snapshot['source']['columnMeta']:
                raise RuntimeError('Existing reader columns differ; review before changing its access.')
        else:
            view = api.call('POST', f'/table/{tid}/view', {
                'name': NAME, 'type': 'grid', 'columnMeta': snapshot['source']['columnMeta'],
                'description': '公司局域网免登录只读；隐藏原始资料及自动化内部字段。',
                'shareMeta': META})
        vid = view['id']
        api.call('PUT', f'/table/{tid}/view/{vid}/share-meta', META)
        sid = view.get('shareId') if view.get('enableShare') else None
        if not sid:
            sid = api.call('POST', f'/table/{tid}/view/{vid}/enable-share')['shareId']
        try:
            status, result = anonymous(f'/share/{sid}/view')
            assert status == 200, 'Anonymous read failed'
            meta = result.get('shareMeta') or result.get('view', {}).get('shareMeta', {})
            assert all(meta.get(k) is False for k in META if k != 'allowEdit'), 'Share flags differ'
            # Community builds predating editable shares omit allowEdit entirely.
            assert meta.get('allowEdit') in (None, False), 'Editable sharing enabled'
            assert result.get('view', {}).get('type', 'grid') == 'grid', 'Unexpected writable form'
            allowed = set(snapshot['visible'])
            actual = {f['id'] for f in result['fields']}
            assert actual == allowed, 'Hidden columns exposed or visible columns missing'
            for record in result['records']:
                assert set(record['fields']) <= allowed, 'Hidden record values exposed'
            hidden = next((f['id'] for f in snapshot['fields'] if f['id'] not in allowed), None)
            if hidden:
                qs = urllib.parse.urlencode({'fieldKeyType': 'id', 'projection[]': hidden, 'take': 1})
                code, selected = anonymous(f'/share/{sid}/view/records?{qs}')
                assert code in (200, 400, 403), 'Unexpected projection response'
                if code == 200:
                    assert all(hidden not in r['fields'] for r in selected['records']), 'Hidden projection leaked'
            code, _ = anonymous(f'/table/{tid}/record', 'POST', {'records': []})
            assert code in (401, 403), 'Anonymous write was not denied'
            entries.append({'name': snapshot['table']['name'], 'tableId': tid,
                            'viewId': vid, 'shareId': sid, 'visibleFields': len(allowed)})
        except BaseException:
            api.call('POST', f'/table/{tid}/view/{vid}/disable-share')
            raise
    helper['save'](args.work / 'reader-state-private.json', {'baseId': BASE, 'views': entries})
    target = args.work / 'index.html'
    target.write_text(render(entries), encoding='utf-8')
    target.chmod(0o644)
    print(json.dumps({'verifiedAnonymousReadOnlyViews': len(entries), 'index': str(target), 'tables': [e['name'] for e in entries]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
