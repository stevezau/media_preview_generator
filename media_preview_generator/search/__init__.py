"""Shared search abstraction for the Preview Inspector.

Each vendor's search endpoint behaves differently (Plex library filters, Emby
unranked substring hits, Jellyfin scope quirks), so queries are normalised and
results ranked client-side:

* :class:`~media_preview_generator.search.query.SearchQuery` parses the
  raw input once — extracts the show/movie title, season number, and
  episode number — so every vendor adapter sees the same normalised
  shape.
* :func:`~media_preview_generator.search.rank.rank_score` ranks
  candidate names against the parsed query so a good Emby match (The
  Boys) beats a coincidental token hit (Wonder Boys).
* Per-vendor ``search_items()`` overrides build on top: Plex via
  ``searchHubs()`` (cross-library), Emby/Jellyfin via two-pass
  ``NameStartsWith`` then ``searchTerm``-with-rank fallback.
"""

from .query import SearchQuery
from .rank import rank_score

__all__ = ["SearchQuery", "rank_score"]
