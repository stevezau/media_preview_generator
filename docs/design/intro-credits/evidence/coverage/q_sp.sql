select substr(mp.file, -50), datetime(mp.created_at,'unixepoch') part_created, datetime(mp.updated_at,'unixepoch') part_upd, mp.duration,
 (select group_concat(tg.text||':'||tg.time_offset||'@'||datetime(tg.created_at,'unixepoch'), ' ') from taggings tg where tg.metadata_item_id=mi.metadata_item_id and tg.tag_id=124969) marks,
 substr(mp.extra_data, instr(mp.extra_data,'pv:intros')+50, 70) part_intro
from media_parts mp join media_items mi on mi.id=mp.media_item_id
where mp.file like '%South Park (1997)%Season 01/%' order by mp.file;
