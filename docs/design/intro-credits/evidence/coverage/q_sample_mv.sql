.mode json
select 'movie' kind, mi.id, mi.title, mi.year, mi.duration,
  (select group_concat(t.tag) from taggings tg join tags t on t.id=tg.tag_id where tg.metadata_item_id=mi.id and t.tag_type=314) guids,
  (select group_concat(t.tag) from taggings tg join tags t on t.id=tg.tag_id where tg.metadata_item_id=mi.id and t.tag_type=2) genres,
  (select mp.file from media_items m2 join media_parts mp on mp.media_item_id=m2.id where m2.metadata_item_id=mi.id limit 1) file,
  (select mp.duration from media_items m2 join media_parts mp on mp.media_item_id=m2.id where m2.metadata_item_id=mi.id limit 1) file_ms
from metadata_items mi where mi.metadata_type=1 and mi.library_section_id=1 order by random() limit 120;
