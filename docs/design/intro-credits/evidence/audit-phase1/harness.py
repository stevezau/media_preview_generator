"""Shared harness: real pipeline + real MarkerStore + real decide(), fake servers/publishers/online clients."""
import os, sys, tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import os as _os
from pathlib import Path as _Path
# MPG_REPO: the checkout whose code is exercised (default: the repo holding this folder).
# MPG_EVIDENCE: the local-only evidence data (gitignored truth files; default: <repo>/docs/design/intro-credits/evidence).
REPO = _os.environ.get("MPG_REPO") or str(_Path(__file__).resolve().parents[5])
EVIDENCE = _os.environ.get("MPG_EVIDENCE") or f"{REPO}/docs/design/intro-credits/evidence"
HERE = _os.path.dirname(_os.path.abspath(__file__))
# Every default store, limiter and settings file lives under CONFIG_DIR (default /config, which on a Docker host can
# hold live service configs): a throwaway folder, set before the package is imported.
_os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="audit-phase1-config-")
sys.path.insert(0, REPO)
from media_preview_generator.markers import pipeline
from media_preview_generator.markers.models import Candidate, MarkerType as T, Source
from media_preview_generator.markers.pipeline import PipelineContext, check_item
from media_preview_generator.markers.probe import Chapter, MediaProbe
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library, ServerType
from tests.markers.fakes import FakeClient, FakeRegistry, ready_publisher, server_config

NO_DATA = LookupResult("no_data")

def make_file(root, rel):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"x" * 100)
    return p

def ctx_for(store, registry, *, sources, publish_when="high", detect=None, clients=None, force=False):
    raw = {"sources": sources, "publish_when": publish_when, "detect": detect or {"intro": True, "credits": True}}
    settings = load_global(validate_global(raw, None)[0])
    # live_config: the registry's configs stand in for the saved settings consent is read from before each write.
    return PipelineContext(registry=registry, config=MagicMock(), settings=settings, store=store,
                           priority=lambda: 2, ffprobe="ffprobe", force=force, clients=clients or {},
                           now=lambda: datetime(2026, 9, 14, tzinfo=timezone.utc), live_config=registry.get_config)

def run(ctx, path, publishers, probe, hints=None):
    with patch.object(pipeline, "probe_media", return_value=probe), \
         patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: publishers.get(cfg.id)):
        return check_item(ProcessableItem(canonical_path=path, server_id="", item_id_by_server=hints or {}, title="t"), ctx=ctx)

def show(store, path, out):
    rec = store.get_file(path)
    print("  outcome:", out.outcome_key if out else None, "|", out.message if out else "", "| rows:", [(r["server_id"], r["status"], r["message"]) for r in (out.publisher_rows if out else [])])
    for mtype, d in store.get_decisions(rec.id).items():
        if d.status.value != "disabled":
            print(f"   {mtype.value}: {d.status.value} ({d.reason})")
    for mtype, m in store.get_markers(rec.id).items():
        print(f"   PUBLISHED-SET {mtype.value} {m.start_ms/1000:.1f}-{m.end_ms/1000:.1f} by {m.decided_by}")
