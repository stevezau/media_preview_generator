SELECT mp.file, t.text, t.time_offset, t.end_time_offset, t.extra_data, mi.duration, mp.hash
FROM media_parts mp JOIN media_items mi ON mi.id = mp.media_item_id
LEFT JOIN taggings t ON t.metadata_item_id = mi.metadata_item_id
  AND t.tag_id IN (SELECT id FROM tags WHERE tag_type = 12) AND t.text IN ('intro', 'credits')
WHERE mp.file LIKE '%/Bones (2005) {tvdb-75682}/Season 0%'
ORDER BY mp.file, t.time_offset;
