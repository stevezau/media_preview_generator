.mode json
with eps as (
  select ep.id, sh.id show_id, sh.title show, se."index" season, ep."index" episode, ep.duration, sh.originally_available_at air,
    (select group_concat(t.tag) from taggings tg join tags t on t.id=tg.tag_id where tg.metadata_item_id=sh.id and t.tag_type=314) guids,
    (select group_concat(t.tag) from taggings tg join tags t on t.id=tg.tag_id where tg.metadata_item_id=sh.id and t.tag_type=2) genres,
    (select mp.file from media_items mi join media_parts mp on mp.media_item_id=mi.id where mi.metadata_item_id=ep.id limit 1) file,
    (select mp.duration from media_items mi join media_parts mp on mp.media_item_id=mi.id where mi.metadata_item_id=ep.id limit 1) file_ms
  from metadata_items ep join metadata_items se on se.id=ep.parent_id join metadata_items sh on sh.id=se.parent_id
  where ep.metadata_type=4 and ep.library_section_id=2 and se."index">0 order by random() limit 200)
select 'tv' kind, * from eps;
