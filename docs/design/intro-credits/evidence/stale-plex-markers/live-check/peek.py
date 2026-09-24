import sqlite3

SP = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad"
con = sqlite3.connect(f"file:{SP}/prod_markers.db?mode=ro", uri=True)
for q in [
    "select status, type, count(*) from decisions group by 1,2",
    "select source, origin, count(*) from evidence group by 1,2 order by 3 desc limit 30",
    "select canonical_path from files limit 3",
    "select distinct server_id from publish_state",
    "select decided_by, reason from decisions where status='decided' limit 8",
    "select distinct status from decisions",
    "select key, substr(value,1,200) from meta",
]:
    print("==", q)
    for r in con.execute(q):
        print(r)
