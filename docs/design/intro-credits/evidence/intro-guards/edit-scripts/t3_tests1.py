import sys
p = sys.argv[1]
s = open(p).read()
old = '''def _season(*plants: Plant, length_s: float = 240.0) -> tuple[list[str], dict[str, np.ndarray]]:
    files = [f"/tv/Accused (2020)/Season 03/Accused (2020) - S03E{e:02d}.mkv" for e in range(1, EPISODES + 1)]'''
new = '''def _season(
    *plants: Plant, length_s: float = 240.0, episodes: int = EPISODES
) -> tuple[list[str], dict[str, np.ndarray]]:
    files = [f"/tv/Accused (2020)/Season 03/Accused (2020) - S03E{e:02d}.mkv" for e in range(1, episodes + 1)]'''
assert old in s
s = s.replace(old, new)
anchor = "def _pair(a_points, b_points, start_s, end_s, shift_s):"
cls = open(sys.argv[2]).read()
assert anchor in s
s = s.replace(anchor, cls + anchor, 1)
imp = "from media_preview_generator.markers.audio.matcher import Hit, IntroCandidate, IntroSegment, file_hits, intro_for"
assert imp in s
s = s.replace(imp, "from media_preview_generator.markers.audio.matcher import (\n    Hit,\n    IntroCandidate,\n    IntroSegment,\n    file_hits,\n    intro_candidates,\n    intro_for,\n)")
open(p, "w").write(s)
