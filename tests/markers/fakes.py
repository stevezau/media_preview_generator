"""Small fakes for pipeline tests: registry, servers, publishers, online clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

from media_preview_generator.markers.publishers.base import Capability, CapabilityReport, Shown, compare_shown
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.ownership import find_owning_servers


def server_config(sid, stype, *, root="/media", enabled=True, markers=None, libraries=None, exclude_paths=None):
    if markers is None:
        markers = {"enabled": True, "library_ids": None}
        if stype is ServerType.PLEX:
            markers["plex"] = {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "restore"}
    return ServerConfig(
        id=sid,
        type=stype,
        name=sid.upper(),
        enabled=enabled,
        url=f"http://{sid}",
        auth={},
        libraries=libraries or [Library("1", "TV Shows", (root,))],
        markers=markers,
        exclude_paths=exclude_paths or [],
    )


@dataclass
class FakeRegistry:
    """Registry over real ``ServerConfig``s: ownership uses the real library/path matching."""

    configs_by_id: dict[str, ServerConfig]
    servers_by_id: dict[str, MagicMock] = field(default_factory=dict)

    def configs(self):
        return list(self.configs_by_id.values())

    def get_config(self, sid):
        return self.configs_by_id.get(sid)

    def get(self, sid):
        if sid not in self.servers_by_id:
            server = MagicMock(name=f"server-{sid}")
            server.id = sid
            server.resolve_remote_path_to_item_id.return_value = f"item-{sid}"
            server.get_external_ids.return_value = None
            # Every vendor reader answers "no markers there" unless a test says otherwise.
            server.get_markers.return_value = []
            server.get_media_segments.return_value = []
            server.get_bridge_markers.return_value = []
            server.get_chapter_markers.return_value = []
            # One version per item and no plugins, unless a test says otherwise.
            server.get_part_durations.return_value = []
            server.get_media_source_durations.return_value = []
            server.get_plugin_names.return_value = []
            self.servers_by_id[sid] = server
        return self.servers_by_id[sid]

    def find_owning_servers(self, path):
        return find_owning_servers(path, self.configs())


class FakeClient:
    def __init__(self, result: LookupResult):
        self.result = result
        self.calls = []

    def lookup(self, ids, *, duration_ms, priority, cancel_check=None):
        self.calls.append({"ids": ids, "duration_ms": duration_ms, "priority": priority, "cancel_check": cancel_check})
        return self.result


def ready_publisher(name="plex_db", types=("intro", "credits"), *, atomic_writes=None):
    """A ready publisher whose ``write`` returns what it was asked to show (the per-version contract).

    ``atomic_writes`` defaults to False for the Jellyfin bridge (the plugin may store before an error) and True for
    everything else. Tests that make ``write`` fail restore it with ``pub.write.side_effect = pub.succeed``. A write
    changes the server unless ``previous`` already was that set; ``shows`` answers "still ours" unless a test says
    otherwise.
    """
    from media_preview_generator.markers.models import MarkerType
    from media_preview_generator.markers.publishers.base import MarkerPublisher

    pub = MagicMock(spec=MarkerPublisher)
    pub.name = name
    pub.supported_types = frozenset(MarkerType(t) for t in types)
    pub.atomic_writes = (name != "jellyfin_bridge") if atomic_writes is None else atomic_writes
    pub.capability.return_value = CapabilityReport(Capability.READY, "ok")
    pub.project.side_effect = lambda ms: sorted(
        (m for m in ms if m.type in pub.supported_types), key=lambda m: (m.start_ms, m.type.value)
    )

    def succeed(item_id, markers, **kwargs):
        ours = pub.project(markers)
        previous = kwargs.get("previous")
        pub.last_write_changed = previous is None or [_served(m) for m in pub.project(previous)] != [
            _served(m) for m in ours
        ]
        return ours

    pub.succeed = succeed
    pub.write.side_effect = pub.succeed
    pub.last_write_changed = True
    pub.shows.return_value = Shown.OURS
    return pub


def _served(marker):
    return marker.type, marker.start_ms, marker.end_ms


class FakePlexItems:
    """Plex-like server state: one marker set per item across all its versions (plex-item-publish-design.md §2).

    ``parts`` maps an item id to the local paths of its versions. A type is written only when every version is decided,
    has that type and agrees within 2 s (the calling file's times are written). Rows of a type that isn't written are
    removed only when they are exactly what ``previous`` says we left there; ``previous=None`` removes nothing.
    """

    AGREEMENT_MS = 2_000

    def __init__(self, parts: dict[str, list[str]]):
        self.parts = parts
        self.shown: dict[str, list] = {}
        self.fail_next: Exception | None = None
        self.calls: list[dict] = []

    def publisher(self, sibling_markers, name="plex_db"):
        pub = ready_publisher(name, atomic_writes=True)

        def write(item_id, markers, *, previous, duration_ms, canonical_path, own_previous=None):
            self.calls.append(
                {
                    "item_id": item_id,
                    "path": canonical_path,
                    "markers": markers,
                    "previous": previous,
                    "own_previous": own_previous,
                }
            )
            if self.fail_next is not None:
                error, self.fail_next = self.fail_next, None
                raise error
            mine = {m.type: m for m in pub.project(markers)}
            desired = []
            for mtype, marker in mine.items():
                agree = True
                for path in self.parts[item_id]:
                    if path == canonical_path:
                        continue
                    decided = sibling_markers(path)  # None: that version was never decided
                    sibling = None if decided is None else decided.get(mtype)
                    if (
                        sibling is None
                        or max(abs(sibling.start_ms - marker.start_ms), abs(sibling.end_ms - marker.end_ms))
                        > self.AGREEMENT_MS
                    ):
                        agree = False
                        break
                if agree:
                    desired.append(marker)
            desired_types = {m.type for m in desired}
            ours_before = {_served(m) for m in (previous or [])}
            kept = [
                m for m in self.shown.get(item_id, []) if m.type not in desired_types and _served(m) not in ours_before
            ]
            before = self.served(item_id)
            self.shown[item_id] = sorted(kept + desired, key=lambda m: (m.start_ms, m.type.value))
            # own_previous: the real publisher takes the moved part's own copy of those markers off.
            pub.last_write_changed = self.served(item_id) != before or bool(own_previous)
            return sorted(desired, key=lambda m: (m.start_ms, m.type.value))

        def shows(item_id, ours):
            served: dict = {}
            for mtype, start, end in self.served(item_id):
                served.setdefault(mtype, []).append((start, end))
            return compare_shown(ours, served, others_alongside=False)

        pub.succeed = write
        pub.write.side_effect = write
        pub.shows.side_effect = shows
        return pub

    def served(self, item_id):
        return [_served(m) for m in self.shown.get(item_id, [])]
