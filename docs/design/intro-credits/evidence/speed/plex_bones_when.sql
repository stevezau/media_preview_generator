SELECT mp.file, t.text, t.time_offset, t.end_time_offset, t.created_at, t.extra_data, mi.duration,
  (SELECT count(*) FROM media_items m2 WHERE m2.metadata_item_id = mi.metadata_item_id) AS versions
FROM media_parts mp JOIN media_items mi ON mi.id = mp.media_item_id
JOIN taggings t ON t.metadata_item_id = mi.metadata_item_id
  AND t.tag_id IN (SELECT id FROM tags WHERE tag_type = 12) AND t.text = 'intro'
WHERE mp.file LIKE '%/Bones (2005) {tvdb-75682}/Season 07/%' OR mp.file LIKE '%/Bones (2005) {tvdb-75682}/Season 05/%E01%'
ORDER BY mp.file;
