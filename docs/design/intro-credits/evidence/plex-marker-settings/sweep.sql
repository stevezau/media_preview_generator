-- Run against the prod Plex DB opened read-only; markers.db copy attached read-only.
ATTACH 'file:/tmp/smq_markers.db?mode=ro' AS m;

CREATE TEMP TABLE empties AS
SELECT e.file_id, f.canonical_path AS path, f.duration_ms, e.fetched_at, e.detail,
       CAST(strftime('%s', substr(e.fetched_at, 1, 19)) AS INTEGER) AS fetched_epoch
FROM m.evidence e JOIN m.files f ON f.id = e.file_id
WHERE e.source = 'server_markers' AND e.type IS NULL AND e.origin = '6c1e1100d1cf4024aad165698b356e11';

CREATE TEMP TABLE hits AS
SELECT x.file_id, x.path, x.duration_ms, x.fetched_at, x.fetched_epoch, x.detail,
       md.id AS item_id, md.library_section_id AS lib, md.metadata_type AS mtype,
       COALESCE(gp.title, md.title) AS show,
       (SELECT COUNT(*) FROM media_items mi2 WHERE mi2.metadata_item_id = md.id AND mi2.deleted_at IS NULL) AS versions,
       SUM(t.text = 'intro') AS n_intro,
       SUM(t.text = 'credits') AS n_credits,
       SUM(t.text = 'intro' AND t.created_at < x.fetched_epoch) AS intro_before,
       SUM(t.text = 'credits' AND t.created_at < x.fetched_epoch) AS credits_before,
       SUM(t.end_time_offset > x.duration_ms + 1000) AS past_end,
       MIN(t.created_at) AS first_marker
FROM empties x
JOIN media_parts mp ON mp.file = x.path AND mp.deleted_at IS NULL
JOIN media_items mi ON mi.id = mp.media_item_id AND mi.deleted_at IS NULL
JOIN metadata_items md ON md.id = mi.metadata_item_id
LEFT JOIN metadata_items p ON p.id = md.parent_id
LEFT JOIN metadata_items gp ON gp.id = p.parent_id
LEFT JOIN taggings t ON t.metadata_item_id = md.id AND t.tag_id = 124969 AND t.text IN ('intro', 'credits')
GROUP BY x.file_id, md.id;

.mode list
.separator |
SELECT 'empty evidence rows', COUNT(*), COUNT(DISTINCT file_id) FROM empties;
SELECT 'empty rows by detail', detail, COUNT(*) FROM empties GROUP BY detail;
SELECT 'matched to a Plex item', COUNT(DISTINCT file_id) FROM hits;
SELECT 'with Plex intro or credits now', COUNT(DISTINCT file_id) FROM hits WHERE n_intro > 0 OR n_credits > 0;
SELECT 'with Plex intro/credits created BEFORE our read', COUNT(DISTINCT file_id),
       SUM(intro_before > 0), SUM(credits_before > 0) FROM hits WHERE intro_before > 0 OR credits_before > 0;
SELECT 'multi-version items among those', COUNT(*) FROM hits WHERE (intro_before > 0 OR credits_before > 0) AND versions > 1;
SELECT 'with a marker past end of file among those', COUNT(*) FROM hits WHERE (intro_before > 0 OR credits_before > 0) AND past_end > 0;
.print
.print == per library (files with Plex markers that existed before our empty read) ==
SELECT ls.name, COUNT(*) AS files, SUM(h.intro_before > 0) AS with_intro, SUM(h.credits_before > 0) AS with_credits
FROM hits h LEFT JOIN library_sections ls ON ls.id = h.lib
WHERE h.intro_before > 0 OR h.credits_before > 0 GROUP BY ls.name ORDER BY files DESC;
.print
.print == per library, all empty-evidence files matched ==
SELECT ls.name, COUNT(*) FROM hits h LEFT JOIN library_sections ls ON ls.id = h.lib GROUP BY ls.name;
.print
.print == per show (TV) ==
SELECT h.show, COUNT(*) AS files, SUM(h.intro_before > 0) AS with_intro, SUM(h.credits_before > 0) AS with_credits,
       MIN(h.fetched_at), MAX(h.fetched_at)
FROM hits h JOIN library_sections ls ON ls.id = h.lib
WHERE (h.intro_before > 0 OR h.credits_before > 0) AND ls.section_type = 2
GROUP BY h.show ORDER BY files DESC;
.print
.print == read-time timeline of empty reads with pre-existing Plex markers (UTC hour) ==
SELECT substr(fetched_at, 1, 13), COUNT(*) FROM hits WHERE intro_before > 0 OR credits_before > 0 GROUP BY 1 ORDER BY 1;
.print
.print == earliest such reads ==
SELECT fetched_at, item_id, substr(path, 1, 90) FROM hits WHERE intro_before > 0 OR credits_before > 0 ORDER BY fetched_at LIMIT 5;
.print
.print == empty reads with NO Plex marker (control) timeline ==
SELECT substr(fetched_at, 1, 13), COUNT(*) FROM hits WHERE n_intro = 0 AND n_credits = 0 GROUP BY 1 ORDER BY 1;
