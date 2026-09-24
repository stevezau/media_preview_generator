import sys
p = sys.argv[1]
s = open(p).read()
def rep(old, new):
    global s
    assert s.count(old) == 1, old[:90]
    s = s.replace(old, new)
rep('''from media_preview_generator.markers.audio import POINT_S, fingerprint, season
from media_preview_generator.markers.models import Source
''', '''from media_preview_generator.markers.audio import POINT_S, season
from media_preview_generator.markers.models import Source
from media_preview_generator.markers.store import MarkerStore
''')
rep('''from tests.markers.audio.test_season import DUR, INTRO, N_POINTS, SEASON_RAW, _Audio, _spec
''', '''from tests.markers.audio.test_season import INTRO, N_POINTS, SEASON_RAW, _Audio, _spec
''')
rep('''@pytest.fixture
def disks(tmp_path):''', '''@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


@pytest.fixture
def disks(tmp_path):''')
rep('''            assert season.season_audio_answer_outdated(ctx, e8) is False
            os.utime(others[3], ns=(1, 1))  # a sibling on the other disk changed
            ctx.store.upsert_file(
                season.FileIdentity(others[3], os.stat(others[3]).st_size, 1),
                duration_ms=DUR,
                season_key=os.path.dirname(others[3]),
                is_movie=False,
            )
            assert season.season_audio_answer_outdated(ctx, e8) is True


def test_fingerprint_window_covers_the_planted_intros():
    assert max(OFFSETS.values()) + 240 < int(fingerprint.window_s(DUR) / POINT_S)
''', '''            assert season.season_audio_answer_outdated(ctx, e8) is False
            os.utime(others[3], ns=(1, 1))  # a sibling on the other disk changed
            assert season.season_audio_answer_outdated(ctx, e8) is True
''')
open(p, "w").write(s)
