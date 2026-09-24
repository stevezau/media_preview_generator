ATTACH 'file:/tmp/smq_markers.db?mode=ro' AS m;
CREATE TEMP TABLE kept AS
SELECT k.item_id, k.type, p.file_id, f.duration_ms, f.canonical_path AS path, d.status,
       COALESCE(mk.start_ms, d.proposed_start_ms) AS our_start, COALESCE(mk.end_ms, d.proposed_end_ms) AS our_end, d.decided_by
FROM m.item_kept_types k
JOIN m.publish_state p ON p.server_id = k.server_id AND p.item_id = k.item_id
JOIN m.files f ON f.id = p.file_id
LEFT JOIN m.decisions d ON d.file_id = p.file_id AND d.type = k.type
LEFT JOIN m.markers mk ON mk.file_id = p.file_id AND mk.type = k.type
WHERE k.server_id = '6c1e1100d1cf4024aad165698b356e11';
.separator |
SELECT 'kept (item,type) rows', COUNT(*), COUNT(DISTINCT item_id) FROM kept;
SELECT 'kept where our decision is DECIDED', type, COUNT(*) FROM kept WHERE status = 'decided' GROUP BY type;
SELECT 'kept Plex row past end of file (+1s)', k.type, COUNT(DISTINCT k.item_id)
FROM kept k JOIN taggings t ON t.metadata_item_id = CAST(k.item_id AS INTEGER) AND t.tag_id = 124969 AND t.text = k.type
WHERE t.end_time_offset > k.duration_ms + 1000 GROUP BY k.type;
SELECT 'kept & decided, Plex start >10s from ours', k.type, COUNT(DISTINCT k.item_id)
FROM kept k JOIN taggings t ON t.metadata_item_id = CAST(k.item_id AS INTEGER) AND t.tag_id = 124969 AND t.text = k.type
WHERE k.status = 'decided' AND ABS(t.time_offset - k.our_start) > 10000 GROUP BY k.type;
.print == Bones intro kept ==
SELECT k.item_id, substr(k.path, 60, 40), k.our_start, k.our_end, k.decided_by, t.time_offset, t.end_time_offset
FROM kept k JOIN taggings t ON t.metadata_item_id = CAST(k.item_id AS INTEGER) AND t.tag_id = 124969 AND t.text = k.type
WHERE k.path LIKE '%/Bones (2005)%' AND k.type = 'intro';
