"""Replace the pipeline's frame-rate tests for a file known from before frame rates (lazy read, failure memory)."""

from pathlib import Path

p = Path(
    "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3/tests/markers/test_pipeline.py"
)
s = p.read_text()
start = s.index("    @staticmethod\n    def _known_from_before_frame_rates(store, media):")
end = s.index(
    '    @pytest.mark.parametrize("frame_rate", [25.0, None], ids=["25-fps", "no-rate"])\n    def test_decisions_are_made'
)
new = '''    @staticmethod
    def _known_from_before_frame_rates(store, reg, media, tmp_path, clients=None):
        """The file as a build from before frame rates left it: decided, with its answers, and no rate."""
        _run(_ctx(store, reg, clients=clients), media, {"plex-1": ready_publisher()}, probe=MediaProbe(DUR, ()))
        with sqlite3.connect(tmp_path / "markers.db") as older_build:
            older_build.execute("DELETE FROM frame_rates")
        rec = store.get_file(media)
        assert store.get_frame_rate(rec.id) == (False, None)
        return rec

    def test_without_an_online_answer_its_rate_isnt_read(self, store, media, tmp_path):
        reg = _registry(media, ServerType.PLEX)
        rec = self._known_from_before_frame_rates(store, reg, media, tmp_path)
        _, probe = _run(_ctx(store, reg), media, {"plex-1": ready_publisher()}, probe=MediaProbe(DUR, (), frame_rate=25.0))
        probe.assert_not_called()
        assert store.get_frame_rate(rec.id) == (False, None)

    def test_an_online_answer_has_its_rate_read_once(self, store, media, tmp_path):
        # IntroDB's times may come from a release at the other speed (decide reads them on the file's clock by it).
        reg = _registry(media, ServerType.PLEX)
        answer = _clients(introdb=LookupResult("ok", (Candidate(T.INTRO, 126_000, 157_000, Source.INTRODB),)))
        rec = self._known_from_before_frame_rates(store, reg, media, tmp_path, clients=answer)
        ctx = _ctx(store, reg)
        _, probe = _run(ctx, media, {"plex-1": ready_publisher()}, probe=MediaProbe(DUR, (), frame_rate=25.0))
        probe.assert_called_once_with(media, ffprobe="ffprobe")
        assert store.get_frame_rate(rec.id) == (True, 25.0)
        # A rate read for the first time can change how the season's other episodes match this one.
        assert ctx.answers_changed() is True
        _, probe = _run(_ctx(store, reg), media, {"plex-1": ready_publisher()}, probe=MediaProbe(DUR, ()))
        probe.assert_not_called()

    def test_a_rate_that_cant_be_read_is_left_unknown_and_not_read_again_for_a_day(self, store, media, tmp_path):
        reg = _registry(media, ServerType.PLEX)
        answer = _clients(introdb=LookupResult("ok", (Candidate(T.INTRO, 126_000, 157_000, Source.INTRODB),)))
        rec = self._known_from_before_frame_rates(store, reg, media, tmp_path, clients=answer)
        out, probe = _run(_ctx(store, reg), media, {"plex-1": ready_publisher()}, probe_effect=ProbeError("bad file"))
        assert probe.call_count == 1
        assert out.outcome_key != FileOutcome.FAILED.value
        assert store.get_frame_rate(rec.id) == (False, None)
        assert store.member_probe_failed_at(FileIdentity(media, rec.size, rec.mtime_ns)) is not None
        _, probe = _run(_ctx(store, reg), media, {"plex-1": ready_publisher()}, probe=MediaProbe(DUR, (), frame_rate=25.0))
        probe.assert_not_called()
        later = _ctx(store, reg, now=lambda: datetime(2026, 9, 14, 1, tzinfo=UTC))
        _, probe = _run(later, media, {"plex-1": ready_publisher()}, probe=MediaProbe(DUR, (), frame_rate=25.0))
        probe.assert_called_once_with(media, ffprobe="ffprobe")
        assert store.get_frame_rate(rec.id) == (True, 25.0)

'''
s = s[:start] + new + s[end:]
p.write_text(s)
print("ok")
