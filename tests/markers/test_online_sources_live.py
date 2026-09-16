"""Live smoke test against the three public APIs (3 requests). Run manually: pytest -m integration -n 0 -s."""

import pytest

from media_preview_generator.markers.models import MarkerType, MediaIds
from media_preview_generator.markers.sources.introdb import IntroDbClient
from media_preview_generator.markers.sources.online import http_session
from media_preview_generator.markers.sources.skipdb import SkipDbClient
from media_preview_generator.markers.sources.theintrodb import TheIntroDbClient

pytestmark = pytest.mark.integration
RM = MediaIds("episode", tmdb="60625", imdb="tt2861424", season=1, episode=1)


@pytest.fixture(autouse=True)
def _fail_unmocked_network_fast():
    """Overrides the root conftest's resolver block: this file exists to make real requests."""
    yield


class _HeaderSpySession:
    """Real shared session that remembers each response's status and rate/usage headers for the report."""

    def __init__(self):
        self.seen = []

    def get(self, url, **kwargs):
        resp = http_session().get(url, **kwargs)
        interesting = {k: v for k, v in resp.headers.items() if "limit" in k.lower() or k.lower() == "retry-after"}
        self.seen.append((url, resp.status_code, interesting))
        return resp


@pytest.mark.parametrize(
    "client_cls", [TheIntroDbClient, IntroDbClient, SkipDbClient], ids=["tidb", "introdb", "skipdb"]
)
def test_rick_and_morty_s01e01_has_an_intro_near_2m07(client_cls, tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))  # usage persistence goes to a throwaway markers.db
    spy = _HeaderSpySession()
    result = client_cls(session=spy).lookup(RM, duration_ms=1_321_472, priority=1)
    print(f"\n{client_cls.__name__}: {spy.seen} -> {result}")
    assert len(spy.seen) == 1
    assert result.status == "ok", result.detail
    intro = next(c for c in result.candidates if c.type is MarkerType.INTRO)
    assert 120_000 <= intro.start_ms <= 135_000 and 150_000 <= intro.end_ms <= 165_000
