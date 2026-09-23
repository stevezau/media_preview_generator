"""Season audio tests share the process-wide count of stuck ffmpegs and ffprobes: none may leak from one test into the
next, where it would quietly stop fingerprints or probes starting."""

import pytest

from media_preview_generator.markers import probe
from media_preview_generator.markers.audio import fingerprint


@pytest.fixture(autouse=True)
def _no_stuck_processes():
    assert fingerprint.stalled_ffmpegs() == 0 and probe.stuck_processes(probe.FFPROBE_REAPER) == 0
    yield
    assert fingerprint.stalled_ffmpegs() == 0 and probe.stuck_processes(probe.FFPROBE_REAPER) == 0
