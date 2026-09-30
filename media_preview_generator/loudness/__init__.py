"""Plex loudness: run Plex's own loudnorm analysis of audio streams on this app's workers and store it in Plex.

Plex's server analyses one audio stream at a time for "Normalize Loudness". This kind runs the identical ffmpeg
command per stream in the shared worker pool and writes the result where Plex keeps it (``media_streams.extra_data``
``ln:*`` fields), so Plex's own job finds those streams done.
"""
