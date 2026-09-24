ATTACH 'file:/tmp/smq_markers.db?mode=ro' AS m;
CREATE TEMP TABLE empties AS
SELECT e.file_id, f.canonical_path AS path, f.duration_ms, f.missing_since, e.fetched_at,
       CAST(strftime('%s', substr(e.fetched_at, 1, 19)) AS INTEGER) AS fetched_epoch
FROM m.evidence e JOIN m.files f ON f.id = e.file_id
WHERE e.source = 'server_markers' AND e.type IS NULL AND e.origin = '6c1e1100d1cf4024aad165698b356e11';
CREATE TEMP TABLE hits AS
SELECT x.file_id, x.path, x.fetched_at, md.id AS item_id, md.library_section_id AS lib, COALESCE(gp.title, md.title) AS show,
       SUM(t.text = 'intro' AND t.created_at < x.fetched_epoch) AS intro_before,
       SUM(t.text = 'credits' AND t.created_at < x.fetched_epoch) AS credits_before,
       SUM(t.created_at < 1790467200) AS pre_feature
FROM empties x
JOIN media_parts mp ON mp.file = x.path AND mp.deleted_at IS NULL
JOIN media_items mi ON mi.id = mp.media_item_id AND mi.deleted_at IS NULL
JOIN metadata_items md ON md.id = mi.metadata_item_id
LEFT JOIN metadata_items p ON p.id = md.parent_id
LEFT JOIN metadata_items gp ON gp.id = p.parent_id
LEFT JOIN taggings t ON t.metadata_item_id = md.id AND t.tag_id = 124969 AND t.text IN ('intro', 'credits')
GROUP BY x.file_id, md.id;
.separator |
SELECT 'empties total / missing from disk / present', COUNT(*), SUM(missing_since IS NOT NULL), SUM(missing_since IS NULL) FROM empties;
SELECT 'present but not found in Plex by path', COUNT(*) FROM empties WHERE missing_since IS NULL AND file_id NOT IN (SELECT file_id FROM hits);
SELECT 'matched', COUNT(*) FROM hits;
SELECT 'affected (Plex marker existed before our read)', COUNT(*), SUM(intro_before>0), SUM(credits_before>0), SUM(pre_feature>0) FROM hits WHERE intro_before>0 OR credits_before>0;
SELECT 'by library', ls.name, COUNT(*) FROM hits h JOIN library_sections ls ON ls.id=h.lib WHERE intro_before>0 OR credits_before>0 GROUP BY 2;
SELECT 'matched but Plex had nothing at read time', COUNT(*) FROM hits WHERE COALESCE(intro_before,0)=0 AND COALESCE(credits_before,0)=0;
SELECT 'Movies affected', substr(path, 1, 100), fetched_at FROM hits WHERE lib=1 AND (intro_before>0 OR credits_before>0);
