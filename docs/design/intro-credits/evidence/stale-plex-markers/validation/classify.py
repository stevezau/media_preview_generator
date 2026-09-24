from load import *
import datetime as dt, json, pickle
f = lambda s: dt.datetime.utcfromtimestamp(s).strftime('%Y-%m-%d') if s else '-'

def fps_class(x):
    if x is None: return '?'
    if abs(x-25)<0.05: return '25'
    if abs(x-23.976)<0.01: return '23.976'
    if abs(x-24)<0.05: return '24'
    if abs(x-29.97)<0.02: return '29.97'
    if abs(x-30)<0.05: return '30'
    if abs(x-50)<0.1: return '50'
    if abs(x-59.94)<0.05: return '59.94'
    return f'{x:.2f}'

def part_entries(val):
    if not val: return []
    try: d = json.loads(val)
    except Exception: return None
    arr = d.get('MediaPartMarkersArray', {}).get('MediaPartMarker', [])
    if isinstance(arr, dict): arr=[arr]
    return [(int(m['startTimeOffset']), int(m['endTimeOffset'])) for m in arr]

items = []
for mid, trows in tags.items():
    ps = by_mid.get(mid)
    if not ps: continue
    plex_rows = [t for t in trows if not (mid in our_items and t['created_at'] >= OUR_START)]
    if not plex_rows: continue
    rec = dict(mid=mid, nparts=len(ps), show=ps[0]['show'] or '(movie)', season=ps[0]['season'], ep=ps[0]['ep'],
               mtype=ps[0]['mtype'], ours=mid in our_items)
    rec['created'] = max(t['created_at'] for t in plex_rows)
    rec['created_min'] = min(t['created_at'] for t in plex_rows)
    rec['texts'] = sorted({t['text'] for t in plex_rows})
    rec['rows'] = [(t['text'], t['time_offset'], t['end_time_offset'], t['created_at']) for t in plex_rows]
    p = max(ps, key=lambda x: x['part_id'])  # newest part
    rec.update(part_id=p['part_id'], mi_id=p['mi_id'], mtime=p['mp_updated'], dur=p['dur'], fps=p['fps'], fpsc=fps_class(p['fps']), file=p['file'])
    rec['mtimes'] = [x['mp_updated'] for x in ps]
    rec['fpss'] = [fps_class(x['fps']) for x in ps]
    # stale by mtime: every part's mtime is after the newest Plex marker row
    rec['stale_mtime'] = all((x['mp_updated'] or 0) > rec['created'] + 60 for x in ps)
    rec['any_part_newer'] = any((x['mp_updated'] or 0) > rec['created'] + 60 for x in ps)
    # part keys: does any part carry Plex's own detection result matching the taggings?
    match = False; haskey = False
    for x in ps:
        for key, text in (('pv_intros','intro'), ('pv_credits','credits')):
            if x['has_intros_key' if text=='intro' else 'has_credits_key'] == '1': haskey = True
            ents = part_entries(x[key])
            if ents:
                tr = {(t[1], t[2]) for t in rec['rows'] if t[0]==text}
                if tr and set(ents) >= tr: match = True
    rec['part_key'] = haskey; rec['part_match'] = match
    rec['past_end'] = any(t[2] > (p['dur'] or 10**12) + 3000 for t in rec['rows'])
    items.append(rec)
pickle.dump(items, open(os.path.join(D,'items.pkl'),'wb'))
print('items with Plex-own markers:', len(items), ' multi-part:', sum(i['nparts']>1 for i in items), ' ours-touched items:', sum(i['ours'] for i in items))
print('stale by mtime (all parts newer than markers):', sum(i['stale_mtime'] for i in items), ' any part newer:', sum(i['any_part_newer'] for i in items))
print('part has pv key:', sum(i['part_key'] for i in items), ' part key matches taggings:', sum(i['part_match'] for i in items))
print('stale_mtime & part_match (contradiction):', sum(i['stale_mtime'] and i['part_match'] for i in items))
print('past_end:', sum(i['past_end'] for i in items), ' past_end & stale:', sum(i['past_end'] and i['stale_mtime'] for i in items))
