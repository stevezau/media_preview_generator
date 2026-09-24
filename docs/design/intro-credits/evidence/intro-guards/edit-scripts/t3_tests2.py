import sys
p = sys.argv[1]
s = open(p).read()
start = s.index("def _choppy(")
end = s.index("class TestAccusedShapes:")
new_helper = '''@dataclass(frozen=True)
class Choppy:
    """A stretch with no dense core that the matcher still finds whole: ``pieces`` shared pieces of ``piece_s`` (under
    the 8 s core) 1.5 s apart (bridged by the matcher's 3.5 s gap), at one start for all or one per episode."""

    seed: int
    at: float | tuple[float | None, ...]
    pieces: int
    piece_s: float = 6.0

    @property
    def length_s(self) -> float:
        return self.pieces * self.piece_s + (self.pieces - 1) * 1.5

    def starts(self, episodes: int = EPISODES) -> list[float | None]:
        return [self.at] * episodes if isinstance(self.at, float) else list(self.at)

    def into(self, body: np.ndarray, episode: int) -> None:
        start = self.starts(len(self.at) if isinstance(self.at, tuple) else episode + 1)[episode]
        if start is None:
            return
        step, piece = _pts(self.piece_s + 1.5), _pts(self.piece_s)
        shared = np.concatenate([np.arange(k * step, k * step + piece) for k in range(self.pieces)])
        body[_pts(start) + shared] = _noise(self.seed, self.pieces * step)[shared]


'''
s = s[:start] + new_helper + s[end:]
s = s.replace("*plants: Plant, length_s: float = 240.0, episodes: int = EPISODES", "*plants: Plant | Choppy, length_s: float = 240.0, episodes: int = EPISODES")
old_body = '''        files, points = _season(*_choppy(20, at, pieces, piece_s))
        length_s = pieces * piece_s + (pieces - 1) * 1.5
        starts = [at] * EPISODES if isinstance(at, float) else list(at)
        matcher, guarded = _answers(files, points, Pictures())
        assert all(_near(seg, s, s + length_s - POINT_S) for seg, s in zip(matcher, starts, strict=True))'''
new_body = '''        stretch = Choppy(20, at, pieces, piece_s)
        files, points = _season(stretch)
        matcher, guarded = _answers(files, points, Pictures())
        ends = [s + stretch.length_s - POINT_S for s in stretch.starts()]
        assert all(_near(seg, s, e) for seg, s, e in zip(matcher, stretch.starts(), ends, strict=True))'''
assert old_body in s
s = s.replace(old_body, new_body)
s = s.replace("files, points = _season(*_choppy(20, at, 3), episodes=3)", "files, points = _season(Choppy(20, at, 3), episodes=3)")
s = s.replace("files, points = _season(*_choppy(20, 12.0, 5))", "files, points = _season(Choppy(20, 12.0, 5))")
assert "_choppy" not in s
open(p, "w").write(s)
