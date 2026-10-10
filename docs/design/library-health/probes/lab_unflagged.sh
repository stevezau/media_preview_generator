#!/bin/bash
# Lists lab Plex parts that have a BIF on disk but no "mi:indexes" flag. Read-only (runs sqlite inside a throwaway container on a :ro mount copy).
docker run --rm -v mlab_plex_config:/c:ro python:3.12-alpine python -c '
import sqlite3, os, shutil, tempfile
P="/c/Library/Application Support/Plex Media Server"
d=tempfile.mkdtemp()
for s in ("", "-wal", "-shm"):
    f=P+"/Plug-in Support/Databases/com.plexapp.plugins.library.db"+s
    if os.path.exists(f): shutil.copy(f, d+"/db"+s)
c=sqlite3.connect(d+"/db")
n=0
for sec, iid, h, ed, upd in c.execute("select mi.library_section_id, mi.id, mp.hash, mp.extra_data, mp.updated_at from metadata_items mi join media_items mdi on mdi.metadata_item_id=mi.id join media_parts mp on mp.media_item_id=mdi.id where mi.metadata_type in (1,4) and mp.deleted_at is null"):
    if not h: continue
    b=f"{P}/Media/localhost/{h[0]}/{h[1:]}.bundle/Contents/Indexes/index-sd.bif"
    if os.path.exists(b):
        fl = "\"mi:indexes\":\"sd\"" in (ed or "")
        if not fl:
            n+=1
            if n<=5: print("unflagged", sec, iid, "bif_newer=", os.stat(b).st_mtime > (upd or 0))
print("total unflagged with bif:", n)
'
