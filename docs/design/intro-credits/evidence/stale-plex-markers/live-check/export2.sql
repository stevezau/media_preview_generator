.mode json
.output /tmp/mk_inv/parts_raw.json
SELECT mp.id AS part_id, mi.metadata_item_id AS mid, mp.updated_at AS mp_updated, mp.extra_data AS extra, mp.file AS file
FROM media_parts mp JOIN media_items mi ON mi.id=mp.media_item_id
WHERE mp.deleted_at IS NULL AND mi.deleted_at IS NULL
  AND mi.metadata_item_id IN (SELECT DISTINCT metadata_item_id FROM taggings WHERE tag_id=124969);
.output /tmp/mk_inv/taggings_raw.json
SELECT t.id, t.metadata_item_id AS mid, t.text, t.time_offset, t.end_time_offset, t.created_at, t.extra_data, t."index"
FROM taggings t WHERE t.tag_id=124969;
