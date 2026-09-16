select ls.id, ls.name, ls.section_type, count(mi.id) from library_sections ls left join metadata_items mi on mi.library_section_id=ls.id and mi.metadata_type in (1,4) group by ls.id;
select t.tag from tags t where t.tag_type=314 limit 5;
