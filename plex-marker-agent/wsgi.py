"""Gunicorn entry point for the Plex marker agent (``gunicorn wsgi:app``)."""

from __future__ import annotations

from plex_marker_agent import create_app

app = create_app()
