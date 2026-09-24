from load import *
import pickle, collections
items = pickle.load(open(os.path.join(D,'items.pkl'),'rb'))
by_file = {}
for i in items:
    for p in by_mid[i['mid']]: by_file[p['file']] = i
# batch fps: siblings sharing the same marker created_at second, not candidates
def cat(i):
    if i['part_match']: return 'fresh'
    if not i['part_key'] and i['stale_mtime']: return 'stale'
    if not i['part_key']: return 'nokey_old_mtime'
    return 'key_nomatch'
batch = collections.defaultdict(collections.Counter)
for i in items:
    if cat(i) != 'stale': batch[(i['show'], i['created'])][i['fpsc']] += 1
def batch_fps(i):
    c = batch.get((i['show'], i['created']))
    return c.most_common(1)[0][0] if c else None

ev = collections.defaultdict(lambda: collections.defaultdict(list))
for fid, path, src, typ, s, e in con.execute("select f.id, f.canonical_path, e.source, e.type, e.start_ms, e.end_ms from evidence e join files f on f.id=e.file_id where e.type in ('intro','credits') and e.source in ('chapters','season_audio','credits_text')"):
    ev[path][(typ, src)].append((s, e))

def served(t):
    text, s, e = t[0], t[1], t[2]
    if text == 'credits': return s + 2000, e
    return s, e

res = []
for path, evd in ev.items():
    i = by_file.get(path)
    if not i: continue
    c = cat(i)
    for typ in ('intro', 'credits'):
        prow = [served(t) for t in i['rows'] if t[0] == typ]
        if not prow: continue
        srcs = [k for k in (('%s' % typ, 'chapters'), (typ, 'season_audio'), (typ, 'credits_text')) if k in evd]
        if not srcs: continue
        k = srcs[0]
        es, ee = evd[k][0]
        # nearest Plex row to evidence start
        ps, pe = min(prow, key=lambda r: abs(r[0] - es))
        d = ps - es
        verdict = 'right' if abs(d) <= 3000 else ('near' if abs(d) <= 8000 else 'wrong')
        if typ == 'intro' and ee is not None and verdict == 'right' and abs(pe - ee) > 3000: verdict = 'near'
        bf = batch_fps(i)
        speed = 'unknown' if bf is None else ('same' if bf == i['fpsc'] else f'{bf}->{i["fpsc"]}')
        res.append(dict(path=path, show=i['show'], cat=c, typ=typ, src=k[1], d=d, verdict=verdict, fps=i['fpsc'], speed=speed, created=i['created'], mtime=i['mtime']))
pickle.dump(res, open(os.path.join(D, 'q3.pkl'), 'wb'))
tab = collections.Counter((r['cat'], 'speedchg' if '->' in r['speed'] else r['speed'], r['fps'] if r['fps'] in ('25','23.976') else 'other', r['verdict']) for r in res)
for k, v in sorted(tab.items()): print(k, v)
