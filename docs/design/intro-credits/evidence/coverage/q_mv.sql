select mp.file, min(tg.time_offset), mp.duration from taggings tg join media_items mi on mi.metadata_item_id=tg.metadata_item_id join media_parts mp on mp.media_item_id=mi.id join metadata_items md on md.id=tg.metadata_item_id
where tg.tag_id=124969 and tg.text='credits' and md.metadata_type=1 and mp.file like '/data_16tb%/Movies/%' and mp.file like '%1080p%' and mp.file not like '%trailer%' and mp.duration > 4800000
group by mp.id order by random() limit 14;
