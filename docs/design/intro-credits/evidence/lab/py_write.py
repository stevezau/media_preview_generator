import json, sqlite3, sys, urllib.parse
sys.path.insert(0, "/lab"); from plex_inject import part_extra
db = "/plexcfg/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
c = sqlite3.connect(db, timeout=30); c.execute("PRAGMA busy_timeout=30000")
tag = c.execute("select id from tags where tag_type=12 and tag='' order by id limit 1").fetchone()[0]
item = 7
part_id, extra = c.execute("select mp.id, mp.extra_data from media_parts mp join media_items mi on mi.id=mp.media_item_id where mi.metadata_item_id=?", (item,)).fetchone()
with c:
    c.execute("delete from taggings where metadata_item_id=? and tag_id=? and text in ('intro','credits')", (item, tag))
    c.execute("insert into taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, created_at, extra_data) values (?,?,0,'intro',?,?,'',strftime('%s','now'),?)", (item, tag, 1361, 29678, '{"pv:version":"5","url":"pv%3Aversion=5"}'))
    c.execute("insert into taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, created_at, extra_data) values (?,?,1,'credits',?,?,'',strftime('%s','now'),?)", (item, tag, 1202524, 1269472, '{"pv:final":"1","pv:version":"4","url":"pv%3Afinal=1&pv%3Aversion=4"}'))
    c.execute("update media_parts set extra_data=? where id=?", (part_extra(extra, [[1361, 29678]], [[1202524, 1269472]]), part_id))
print("sqlite", sqlite3.sqlite_version, "tag", tag, "part", part_id, "integrity", c.execute("pragma quick_check").fetchone()[0])
