import pathlib

p = pathlib.Path("/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/stale_fresh/live_check.py")
s = p.read_text()
start = s.index("parts_by_mid = collections.defaultdict(list)")
end = s.index("con = sqlite3.connect(")
new = '''parts_by_mid = collections.defaultdict(list)
part_info = {}
unreadable_parts = 0
for p in rows("parts.tsv"):
    part_info[p["file"]] = dict(
        mid=intn(p["mid"]),
        fps=float(p["fps"]) if p["fps"] else None,
        dur=intn(p["dur"]),
        show=p["show"],
        mtime=intn(p["mp_updated"]),
    )
for p in json.load(open(f"{D}/parts_raw.json")):
    parts_by_mid[p["mid"]].append(plex_db._Part(p["part_id"], 0, p["file"], p["extra"], None, p["mp_updated"]))

tags_by_mid = collections.defaultdict(list)
for t in json.load(open(f"{D}/taggings_raw.json")):
    tags_by_mid[t["mid"]].append(
        plex_db._TaggingRow(
            t["id"], t["index"] or 0, t["text"], t["time_offset"], t["end_time_offset"], None, t["extra_data"],
            t["created_at"],
        )
    )

'''
s = s[:start] + new + s[end:]
s = s.replace(
    "select file_id, type, proposed_start_ms, proposed_end_ms, reason from decisions where status='decided'",
    "select file_id, type, proposed_start_ms, proposed_end_ms, reason from decisions where status=?",
)
s = s.replace(
    "for fid, typ, s, e, reason in con.execute(\n",
    "STATUS = sys.argv[1] if len(sys.argv) > 1 else 'decided'\nfor fid, typ, s, e, reason in con.execute(\n",
)
s = s.replace(
    "decisions where status=?\"\n):",
    "decisions where status=?\", (STATUS,)\n):",
)
s = s.replace('open(f"{D}/live_check.pkl", "wb")', 'open(f"{D}/live_check_{STATUS}.pkl", "wb")')
p.write_text(s)
