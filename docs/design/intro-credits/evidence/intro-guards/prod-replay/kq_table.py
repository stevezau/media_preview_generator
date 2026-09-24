import sqlite3,re,collections,json,sys
db=sqlite3.connect('prod_markers.db')
plex=collections.defaultdict(list)
for line in open('kq_plex_markers.txt'):
    f,t,a,b,x=line.rstrip('\n').split('|',4)
    plex[f].append((t,int(a)/1000,int(b)/1000))
ours={}
for fid,mj,st in db.execute("select file_id,item_id and markers_json,status from publish_state"):
    pass
pub={fid:json.loads(mj) for fid,mj in db.execute("select file_id,markers_json from publish_state")}
want=sys.argv[1] if len(sys.argv)>1 else 'no_evidence'
rows=db.execute("select f.id,f.canonical_path,f.duration_ms,d.status,d.reason,d.decided_by,d.proposed_start_ms,d.proposed_end_ms from decisions d join files f on f.id=d.file_id where d.type='intro' and f.missing_since is null and d.status=? order by f.canonical_path",(want,)).fetchall()
def show(p):
    m=re.search(r'/TV Shows/([^/]+)/',p); return m.group(1)[:28] if m else p
for fid,p,dur,st,r,by,ps,pe in rows:
    ev=db.execute("select source,type,start_ms,end_ms,label from evidence where file_id=? and (type='intro' or type is null)",(fid,)).fetchall()
    evs=[]
    for s,t,a,b,l in ev:
        if t is None: evs.append(s+':none')
        else: evs.append(f"{s}:{a/1000:.1f}-{b/1000:.1f}")
    ourpub=[m for m in pub.get(fid,[]) if m[0]=='intro']
    pi=[f"{a:.1f}-{b:.1f}" for t,a,b in plex.get(p,[]) if t=='intro']
    se=re.search(r'S\d+E\d+',p); se=se.group(0) if se else '?'
    print(f"{show(p):28} {se:8} dur={dur/60000 if dur else 0:5.1f}m plexintro={pi} ourpub={bool(ourpub)} ev={sorted(set(evs))} reason={r}")
