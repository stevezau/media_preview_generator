import sqlite3,re,collections,sys
db=sqlite3.connect('prod_markers.db')
rows=db.execute("select f.canonical_path,d.status,d.reason,d.decided_by,d.decided_at,f.is_movie from decisions d join files f on f.id=d.file_id where d.type='intro' and f.missing_since is null").fetchall()
def show(p):
    m=re.search(r'/(?:TV Shows|TV|tv|Anime|Kids)[^/]*/([^/]+)/',p)
    return m.group(1) if m else p.split('/')[-3] if p.count('/')>3 else p
c=collections.defaultdict(collections.Counter)
for p,st,r,by,at,mv in rows:
    c[st][('MOVIE:' if mv else '')+show(p)]+=1
for st in c:
    print('==',st,sum(c[st].values()))
    for k,v in c[st].most_common(40): print('  ',v,k)
