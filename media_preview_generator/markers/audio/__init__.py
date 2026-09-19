"""Season audio matching for TV intros (spec §5.3)."""

# Seconds per chromaprint point: a 1365-sample hop at 11025 Hz (measured 0.1238 s, spec §5.3).
POINT_S = 4096 / 11025 / 3
