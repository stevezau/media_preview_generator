from load import *
import pickle, collections
items = pickle.load(open(os.path.join(D,'items.pkl'),'rb'))
part_by_file = {p['file']: p for p in parts}
item_by_mid = {i['mid']: i for i in items}
def cat(i):
    if i is None: return 'no_plex_marker'
    if i['part_match']: return 'fresh'
    if not i['part_key'] and i['stale_mtime']: return 'stale'
    if not i['part_key']: return 'nokey_oldmtime'
    return 'key_nomatch'
showfps = collections.defaultdict(collections.Counter)
for p in parts:
    if p['show']: showfps[p['show']][round(p['fps'] or 0, 2)] += 1
ev = collections.defaultdict(lambda: collections.defaultdict(list))
files = {}
for fid, path, missing in con.execute("select id, canonical_path, missing_since from files"):
    files[fid] = (path, missing)
for fid, src, typ, s, e in con.execute("select file_id, source, type, start_ms, end_ms from evidence where type is not null and type != ''"):
    ev[fid][(src, typ)].append((s, e))
FILEREAD = ('chapters', 'season_audio', 'credits_text')
rows = []
for fid, (path, missing) in files.items():
    p = part_by_file.get(path)
    if not p: continue
    fps = p['fps']
    i = item_by_mid.get(p['mid'])
    for typ in ('intro', 'credits'):
        online = [s for s in ('introdb', 'theintrodb') if (s, typ) in ev[fid]]
        fileread = [s for s in FILEREAD if (s, typ) in ev[fid]]
        app_server = ('server_markers', typ) in ev[fid]
        plex_now = i is not None and any(t[0] == typ for t in i['rows'])
        rows.append(dict(fid=fid, path=path, missing=missing, mid=p['mid'], show=p['show'] or '(movie)', fps=fps, typ=typ, online=online,
                         fileread=fileread, app_server=app_server, plex_now=plex_now, cat=cat(i) if plex_now else 'no_plex_marker',
                         native25=(p['show'] and showfps[p['show']].most_common(1)[0][0] == 25.0)))
pickle.dump(rows, open(os.path.join(D,'q5.pkl'),'wb'))
is25 = lambda r: r['fps'] and abs(r['fps'] - 25) < 0.05
print('markers.db files joined to Plex parts:', len({r['fid'] for r in rows}), ' 25fps files:', len({r['fid'] for r in rows if is25(r)}))
base = [r for r in rows if is25(r) and r['online'] and not r['fileread']]
print('25fps, online answer, no file-reading answer (file,type):', len(base), collections.Counter(r['typ'] for r in base))
a = [r for r in base if r['app_server']]; b = [r for r in base if r['plex_now']]
print('  ... + Plex marker in app evidence (snapshot):', len(a), collections.Counter(r['typ'] for r in a))
print('  ... + Plex marker in Plex DB now:', len(b), collections.Counter((r['typ'], r['cat']) for r in b))
print('  ... Plex DB now, by show/native25:')
for r in sorted(b, key=lambda r: r['path']): print('     ', r['typ'], r['cat'], 'native25' if r['native25'] else 'not-native', 'app_ev' if r['app_server'] else '-', r['online'], r['path'].split('/')[-1][:90])
