"""Carry-over (spec §5.5 rule 15): when a file is replaced by one of the same length, what we had decided for a type
stays for the new file while nothing answers for it there.

Plex keeps an item's markers across a file replacement; deciding the new file from its own evidence alone did worse.
One episode's replacement, identical in length and showing the same opening, has no chapters, and its
season audio passed nothing: its 0-92 s intro (the old file's Intro chapter) was removed. Another episode's,
408 ms shorter, lost its intro the same way.

The rule is narrow on purpose:

- Only a type with no candidate at all from any source (``decide``: no evidence). A source that looked and found
  nothing gives no candidate, and counts the same: none of them tells an intro that isn't there from one it missed
  (a file without an intro chapter, a season match that didn't pass its guards, no crowd entry, no credit roll
  found). What does tell a different cut, the length, is checked instead. Any candidate of the type, even one that
  fails the sanity checks or leaves the type in review, is the new file's own evidence and wins; so do a user's lock
  and a type left off or to a server's own marker.
- Only within 1 s of the replaced file's length, and only a marker at least one of whose deciding sources is still
  turned on (a user's marker, or one carried before, whose sources aren't known, always counts).
- Not a marker only the detectors that read the file's content decided (season audio, credit text) when each of them
  read the new file at a **newer** version than the one that decided the replaced file's marker, with something to
  compare it with and not out of date, and found nothing: the detector's own update passed the old answer over. Small
  Prophets S01E05 and E06 kept season audio version 9's 0-12 s logo stretch this way after version 10, which passes
  it over, found nothing on the same files (sflix, 2026-09-28). The same version finding nothing on the new file is
  the case this rule is for (another encode's audio or picture), so that marker still carries, and so does one whose
  deciding version isn't known. A snapshot kept aside by a build before versions were kept with it (or by a rollback
  to one) counts as decided at most at ``VERSIONS_BEFORE_THEY_WERE_KEPT``. A chapter, an online
  answer or a user's marker still carries: a replacement without the chapter, or one an online source doesn't list,
  says nothing about the intro.
- The replaced file: an earlier identity at the same path (kept aside by ``MarkerStore.upsert_file``; not when the new
  file was moved here from another path it is known at), or a file last published to one of the new file's server
  items that is gone from disk (one still there is another version, decided on its own). Of these, the one stored
  last speaks for the type: if it had no marker, nothing is carried.
- When that can't be told now (a disk that doesn't answer in time, a server that can't name the item), a marker
  already carried stays: "can't tell" never takes one off the servers.

A carried marker is decided by ``carried_over`` alone, so whatever asks whether new evidence could change a type (the
season audio follow-ups, the weekly online re-check, the TheIntroDB recheck) treats it as undecided; the new file's
first answer of its own replaces it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from types import MappingProxyType
from typing import NamedTuple

from .decide import (
    EOF_CLAMP_MS,
    INTRO_RECAP_MAX_OVERLAP_MS,
    MIN_SEGMENT_MS,
    NO_EVIDENCE_REASON,
    PREVIEW_CREDITS_MAX_OVERLAP_MS,
    DecisionStatus,
    TypeDecision,
)
from .models import SERVER_SOURCES, Marker, MarkerType, Source
from .store import REPLACED_LENGTH_TOLERANCE_MS, FileRecord, MarkerStore, PreviousDecision

# ``Marker.decided_by`` of a carried marker (not a source: nothing answered for this file).
CARRIED_OVER = "carried_over"
CARRIED_OVER_REASON = "carried over from the file it replaced (same length)"
MAX_LENGTH_CHANGE_MS = REPLACED_LENGTH_TOLERANCE_MS
_START_SEGMENTS = (MarkerType.INTRO, MarkerType.RECAP)
# Types that must not overlap once both are decided (decide's cross-type checks), with how far they may.
_PAIRED = {
    MarkerType.INTRO: (MarkerType.RECAP, INTRO_RECAP_MAX_OVERLAP_MS),
    MarkerType.RECAP: (MarkerType.INTRO, INTRO_RECAP_MAX_OVERLAP_MS),
    MarkerType.CREDITS: (MarkerType.PREVIEW, PREVIEW_CREDITS_MAX_OVERLAP_MS),
    MarkerType.PREVIEW: (MarkerType.CREDITS, PREVIEW_CREDITS_MAX_OVERLAP_MS),
}
# ``decided_by`` entries that aren't a source the user can turn off: the user's own marker, and a carried one.
_ALWAYS_ON = frozenset({Source.USER.value, CARRIED_OVER})
_CONFIRMING_ONLY = frozenset(source.value for source in SERVER_SOURCES)

# What ``previous`` answers per type: the replaced file's decision, or None when it can't be told now.
Previous = Mapping[MarkerType, PreviousDecision | None]
# The newest versions a build without ``replaced_versions`` had (82dc2bc and every build before it, a rollback to one
# included): season audio v10 with end-picture check 3. A snapshot such a build kept aside was decided at most at these,
# so a newer version reading the new file is known to be newer; an equal one isn't.
VERSIONS_BEFORE_THEY_WERE_KEPT = MappingProxyType(
    {
        "season_audio": 2010,
        "season_audio_previous": 2010,
        # 8, not 82dc2bc's 7: #327 (credit text 8) may reach dev first and store 8 without the table; too high only
        # keeps a carried marker.
        "credits_text": 8,
    }
)


class ReadNow(NamedTuple):
    """A content detector's reading of the new file that is today's verdict on it (``pipeline._read_now``).

    Attributes:
        version: The version it read the file at, as stored (``evidence_versions``).
        step: What part of a stored version is the detector's own: compared modulo this, 0 compares it whole (credit
            text stores the user's window above it).
    """

    version: int
    step: int = 0

    def newer_than(self, stored: int) -> bool:
        """Whether this reading's version is newer than a stored answer's."""
        if self.step:
            return self.version % self.step > stored % self.step
        return self.version > stored


def is_carried_over(marker: Marker | None) -> bool:
    """Whether a stored marker was carried over from a replaced file."""
    return marker is not None and CARRIED_OVER in marker.decided_by


def previous_decisions(
    store: MarkerStore,
    rec: FileRecord,
    items: Iterable[tuple[str, str]],
    *,
    wanted: Collection[MarkerType],
    gone: Callable[[FileRecord], bool | None],
    items_known: bool = True,
) -> dict[MarkerType, PreviousDecision | None]:
    """What the files this one replaced last decided, per wanted type (the latest stored one's).

    Args:
        store: The markers store.
        rec: The new file.
        items: The new file's server items, ``(server_id, item_id)``.
        wanted: The types to look up.
        gone: Whether another file is gone from disk (``missing.gone_now``: None when that can't be told now); asked
            only about files with a decision of a wanted type, latest first, until one speaks.
        items_known: False when a server couldn't name the new file's item: a type nothing else speaks for can't be
            told then.

    Returns:
        Per wanted type, the decision of the replaced file stored last; None when it can't be told now; no entry when
        no replaced file decided it.
    """
    moved_here = store.moved_here(rec)
    candidates: list[tuple[PreviousDecision, FileRecord | None]] = []
    if not moved_here:
        candidates += [(decision, None) for decision in store.replaced_in_place(rec.id)]
    for other in store.files_published_to(items, other_than=rec.id):
        candidates += [(decision, other) for decision in store.previous_decisions_of(other.id)]
    states: dict[int, bool | None] = {}
    out: dict[MarkerType, PreviousDecision | None] = {}
    for mtype in wanted:
        entries = sorted((c for c in candidates if c[0].type is mtype), key=lambda c: c[0].seen_at, reverse=True)
        for decision, other in entries:
            if other is None:
                out[mtype] = decision
                break
            if other.id not in states:
                states[other.id] = gone(other)
            if states[other.id] is None:
                out[mtype] = None
                break
            if states[other.id]:
                out[mtype] = decision
                break
        else:
            if not items_known:
                out[mtype] = None
    return out


def carry_over(
    decisions: Mapping[MarkerType, TypeDecision],
    duration_ms: int,
    previous: Callable[[frozenset[MarkerType]], Previous],
    *,
    kept: Mapping[MarkerType, Marker] | None = None,
    enabled: Collection[str] | None = None,
    read_by: Callable[[MarkerType], Mapping[str, ReadNow]] | None = None,
) -> dict[MarkerType, TypeDecision]:
    """One file's decisions with the replaced file's markers kept for the types it has no evidence of.

    Args:
        decisions: The file's decisions (after every other rule).
        duration_ms: The file's length.
        previous: :func:`previous_decisions` for the given types, asked only when a type has no evidence.
        kept: The file's stored markers: a carried one stays when the replaced file can't be told now.
        enabled: The sources turned on (None: every source); a marker none of whose deciding sources is on isn't
            carried.
        read_by: A type's content detectors whose stored answer is today's verdict on the new file, per source their
            answers are stored under (``pipeline._content_read_by``), asked only for a marker that would be carried;
            a marker only they decided, each at an older version than the one it read the new file at, isn't carried,
            since the type has no evidence (none of them found it). None: no detector counts.

    Returns:
        The decisions, a carried type decided by ``carried_over`` with :data:`CARRIED_OVER_REASON`.
    """
    out = dict(decisions)
    wanted = frozenset(
        mtype
        for mtype, d in decisions.items()
        if d.status is DecisionStatus.NO_EVIDENCE and d.reason == NO_EVIDENCE_REASON
    )
    if not wanted:
        return out
    known = previous(wanted)
    for mtype in MarkerType:
        if mtype not in wanted or mtype not in known:
            continue
        held = known[mtype]
        if held is None:
            marker = (kept or {}).get(mtype)
            marker = marker if is_carried_over(marker) and not marker.locked else None
        elif (
            held.marker is None
            or abs(held.duration_ms - duration_ms) > MAX_LENGTH_CHANGE_MS
            or not _sources_on(held.decided_by, enabled)
            or (read_by is not None and _passed_over_by_a_newer_version(held, read_by(mtype)))
        ):
            continue
        else:
            marker = _fitted(mtype, held.marker, duration_ms)
        if marker is not None and not _overlaps(marker, out):
            out[mtype] = TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, CARRIED_OVER_REASON)
    return out


def _sources_on(decided_by: Iterable[str], enabled: Collection[str] | None) -> bool:
    """Whether a marker's deciding sources include one still turned on (always, for a user's or carried marker, or
    when every source counts)."""
    deciding = [s for s in decided_by if s not in _CONFIRMING_ONLY]
    if enabled is None or not deciding or any(s in _ALWAYS_ON for s in deciding):
        return True
    return any(s in enabled for s in deciding)


def _passed_over_by_a_newer_version(held: PreviousDecision, read_by: Mapping[str, ReadNow]) -> bool:
    """Whether every deciding source of a replaced file's marker (servers' confirming markers aside) is a content
    detector that read the new file now, at a newer version than the one that decided the marker, and found nothing
    (a marker with no known source, a user's or a carried one, never is; nor one whose deciding version isn't known)."""
    deciding = [s for s in held.decided_by if s not in _CONFIRMING_ONLY]
    versions = VERSIONS_BEFORE_THEY_WERE_KEPT if held.versions is None else held.versions
    return bool(deciding) and all(
        s in read_by and s in versions and read_by[s].newer_than(versions[s]) for s in deciding
    )


def _fitted(mtype: MarkerType, times: tuple[int, int], duration_ms: int) -> Marker | None:
    """The marker on the new file: its end clamped to the file's end; None when that leaves it too short, or an intro
    or recap running to the end."""
    start, end = times[0], min(times[1], duration_ms)
    if end - start < MIN_SEGMENT_MS:
        return None
    if mtype in _START_SEGMENTS and end >= duration_ms - EOF_CLAMP_MS:
        return None
    return Marker(mtype, start, end, (CARRIED_OVER,))


def _overlaps(marker: Marker, decisions: Mapping[MarkerType, TypeDecision]) -> bool:
    """Whether a carried marker overlaps a decided marker of the paired type (the file's own, or one carried before
    it) more than decide's cross-type checks allow."""
    other_type, allowed_ms = _PAIRED[marker.type]
    other = decisions.get(other_type)
    if other is None or other.status is not DecisionStatus.DECIDED or other.marker is None:
        return False
    overlap = min(marker.end_ms, other.marker.end_ms) - max(marker.start_ms, other.marker.start_ms)
    return overlap > allowed_ms
