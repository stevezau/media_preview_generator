"""WSGI entry point for gunicorn.

Usage (production — via wrapper.sh):
    gunicorn \\
        --bind 0.0.0.0:8080 \\
        --worker-class gthread \\
        --workers 1 \\
        "media_preview_generator.web.wsgi:app"

Usage (development):
    python -m media_preview_generator.web.app
"""

from ..shutdown import install_signal_handlers
from .app import create_app

app = create_app()
# After create_app: gunicorn's worker has installed its own handlers by now, and these run before them.
install_signal_handlers()
