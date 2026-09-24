import csv, sys, json, sqlite3, collections, bisect, os
csv.field_size_limit(10**9)
D = os.path.dirname(os.path.abspath(__file__))
SP = os.path.dirname(D)
OUR_START = 1790071200  # 2026-09-22 10:00 UTC

def rows(name):
    with open(os.path.join(D, name), newline='') as f:
        r = csv.DictReader(f, delimiter='\t', quoting=csv.QUOTE_NONE)
        yield from r

def intn(x):
    try: return int(float(x))
    except (TypeError, ValueError): return None

parts = [p for p in rows('parts.tsv')]
for p in parts:
    for k in ('part_id','mi_id','mid','mtype','m_created','m_added','mp_updated','mi_updated','dur','season','ep'):
        p[k] = intn(p[k])
    p['fps'] = float(p['fps']) if p['fps'] else None
by_mid = collections.defaultdict(list)
for p in parts: by_mid[p['mid']].append(p)

tags = collections.defaultdict(list)
for t in rows('taggings.tsv'):
    t['id']=int(t['id']); t['mid']=intn(t['mid']); t['time_offset']=intn(t['time_offset']); t['end_time_offset']=intn(t['end_time_offset']); t['created_at']=intn(t['created_at'])
    tags[t['mid']].append(t)

con = sqlite3.connect(f"file:{SP}/prod_markers.db?mode=ro", uri=True)
our_items = {int(r[0]) for r in con.execute("select item_id from item_publish_state union select item_id from publish_state where item_id is not null")}

# Calibration: lower bound on a part's creation time = running max of metadata created_at over parts with id <= this id
cal = sorted((p['part_id'], p['m_created']) for p in parts if p['m_created'])
ids = [c[0] for c in cal]; run = []; mx = 0
for _, c in cal:
    mx = max(mx, c); run.append(mx)
def part_lb(pid):
    i = bisect.bisect_right(ids, pid) - 1
    return run[i] if i >= 0 else 0
