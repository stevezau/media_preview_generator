"""S12: chapter candidates are stored post-classification and never re-derived for an unchanged file, so a chapter-rule
fix (e.g. Task 5's cold-open rule) doesn't reach files scanned before the upgrade. Simulated by scanning with the
pre-fix rule (generic "Intro" kept next to "OP") and re-running with the current code."""
from harness import *
from media_preview_generator.markers.sources import chapters as chapters_mod
tmp = tempfile.mkdtemp(prefix="auditB-s12-")
root = tmp + "/media"
path = make_file(tmp, "media/TV/Mushoku Tensei (2021) {tvdb-1}/Season 01/Mushoku Tensei (2021) - S01E06.mkv")
probe = MediaProbe(1_420_000, (Chapter(0, 274_700, "Intro"), Chapter(274_700, 363_900, "OP"), Chapter(363_900, 1_300_000, "Part A"), Chapter(1_300_000, None, "ED")))
registry = FakeRegistry({"jf-1": server_config("jf-1", ServerType.JELLYFIN, root=root)})
registry.get("jf-1").get_external_ids.return_value = {"kind": "episode", "season": 1, "episode": 6}
pubs = {"jf-1": ready_publisher("jellyfin_bridge", types=("intro", "credits", "recap", "preview"))}
store = MarkerStore(tmp + "/markers.db")
sources = [{"id": "chapters", "enabled": True}]
def old_rules(p):  # the rule before Task 5 fix round 1: every intro-named chapter is a candidate
    out = []
    ends = chapters_mod._clamped_ends(p.chapters)
    for ch, end in zip(p.chapters, ends):
        t = chapters_mod.classify_chapter_title(ch.title)
        if t: out.append(Candidate(t, ch.start_ms, end, Source.CHAPTERS, origin=ch.title))
    return out
# An old build stored no chapter-rules version (the fixed code compares it; create=True keeps this runnable on old code).
with patch.object(pipeline, "chapter_candidates", side_effect=old_rules), \
     patch.object(pipeline, "CHAPTER_RULES_VERSION", None, create=True):
    print("scan with old build:"); show(store, path, run(ctx_for(store, registry, sources=sources), path, pubs, probe))
with patch.object(pipeline, "probe_media", return_value=probe) as probe_mock, \
     patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: pubs.get(cfg.id)):
    out = check_item(ProcessableItem(canonical_path=path, server_id="", item_id_by_server={}, title="t"), ctx=ctx_for(store, registry, sources=sources))
    print(f"upgrade, normal run (ffprobe called {probe_mock.call_count}x):"); show(store, path, out)
