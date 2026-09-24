from load import *
import pickle, collections, re, json
items = pickle.load(open(os.path.join(D,'items.pkl'),'rb'))
chap = {}
for line in open(os.path.join(D,'chapters.tsv')):
    pid, js = line.rstrip('\n').split('\t', 1)
    try: chap[int(pid)] = json.loads(js)['Chapters']['Chapter']
    except Exception: pass
INTRO = re.compile(r'^(intro|opening|op|opening credits|opening titles|title sequence|main titles|theme|theme song)$', re.I)
CRED = re.compile(r'^(credits|end credits|ending|ed|ending credits|closing credits|end titles)$', re.I)
def cat(i):
    if i['part_match']: return 'fresh'
    if not i['part_key'] and i['stale_mtime']: return 'stale'
    if not i['part_key']: return 'nokey_oldmtime'
    return 'key_nomatch'
batch = collections.defaultdict(collections.Counter)
for i in items:
    if cat(i) != 'stale': batch[(i['show'], i['created'])][i['fpsc']] += 1
def speed(i):
    c = batch.get((i['show'], i['created']))
    if not c: return 'unknown'
    bf = c.most_common(1)[0][0]
    return 'same' if bf == i['fpsc'] else 'change'
out = []
for i in items:
    if i['nparts'] != 1: continue
    ch = chap.get(i['part_id'])
    if not ch: continue
    if isinstance(ch, dict): ch = [ch]
    for typ, rx in (('intro', INTRO), ('credits', CRED)):
        cs = [c for c in ch if rx.match((c.get('name') or '').strip())]
        pr = [t for t in i['rows'] if t[0] == typ]
        if len(cs) != 1 or not pr: continue
        cstart = float(cs[0]['start'])*1000; cend = float(cs[0]['end'])*1000
        if typ == 'credits':
            ps = min((t[1] + 2000 for t in pr), key=lambda s: abs(s - cstart))
        else:
            ps = min((t[1] for t in pr), key=lambda s: abs(s - cstart))
        d = ps - cstart
        tol = 3000 if typ == 'intro' else 5000
        v = 'right' if abs(d) <= tol else ('near' if abs(d) <= 2*tol+2000 else 'wrong')
        out.append(dict(mid=i['mid'], show=i['show'], typ=typ, cat=cat(i), fps=i['fpsc'], speed=speed(i), d=d, v=v, past_end=i['past_end']))
pickle.dump(out, open(os.path.join(D,'chapeval.pkl'),'wb'))
def fc(f): return f if f in ('25','23.976','24','29.97') else 'other'
tab = collections.defaultdict(collections.Counter)
for r in out: tab[(r['cat'], r['speed'], fc(r['fps']), r['typ'])][r['v']] += 1
for k in sorted(tab):
    c = tab[k]; n = sum(c.values())
    print(f"{str(k):60s} n={n:5d} right={c['right']:5d} near={c['near']:4d} wrong={c['wrong']:4d}  right%={100*c['right']/n:5.1f}")
