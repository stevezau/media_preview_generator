.mode tabs
.headers on
WITH show AS (SELECT id FROM metadata_items WHERE metadata_type=2 AND title='Bones'),
seasons AS (SELECT id, "index" AS sn FROM metadata_items WHERE parent_id IN (SELECT id FROM show)),
eps AS (SELECT e.id, s.sn, e."index" AS en, e.added_at, e.created_at AS e_created, e.updated_at AS e_updated FROM metadata_items e JOIN seasons s ON e.parent_id=s.id)
SELECT eps.sn, eps.en, eps.id AS mid,
  datetime(eps.added_at,'unixepoch') AS ep_added,
  mi.id AS miid, mi.frames_per_second AS fps, datetime(mi.created_at,'unixepoch') AS mi_created, datetime(mi.updated_at,'unixepoch') AS mi_updated,
  mp.id AS mpid, datetime(mp.created_at,'unixepoch') AS mp_created, datetime(mp.updated_at,'unixepoch') AS mp_updated, mp.duration AS dur,
  (SELECT group_concat(t.text||':'||t.time_offset||'-'||t.end_time_offset||'@'||datetime(t.created_at,'unixepoch'), ' | ') FROM taggings t WHERE t.metadata_item_id=eps.id AND t.tag_id=124969) AS markers,
  mp.file
FROM eps JOIN media_items mi ON mi.metadata_item_id=eps.id AND mi.deleted_at IS NULL
JOIN media_parts mp ON mp.media_item_id=mi.id AND mp.deleted_at IS NULL
ORDER BY eps.sn, eps.en;
