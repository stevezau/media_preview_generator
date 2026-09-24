"""Rewrite the §14 entry for the two playback speeds with the review's changes and numbers."""

from pathlib import Path

p = Path(
    "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3/"
    "docs/design/intro-credits/spec.md"
)
s = p.read_text()
anchor = "- 2026-09-24 · **Two playback speeds: season audio at one speed, online times on the file's clock**"
assert s.count(anchor) == 1
s = (
    s[: s.index(anchor)]
    + """- 2026-09-24 · **Two playback speeds: season audio at one speed, online times on the file's clock** (owner: automatic,
  at par or better than Plex's own markers). Bones S05–S08 on the owner's library mixes FUZEER WEB releases at 25 fps
  (a PAL speed-up: 4.3 % fast, pitch raised) with 23.976 Blu-rays. Season audio matched 0 of 85 WEB↔Blu-ray pairs of
  S05, so the WEB episodes missed the quorum; and IntroDB's times, taken from a 23.976 release, ran 4.3 % late on the
  25 fps files (S07E01: theme 310–338 s, IntroDB 324–354 s), so they "disagreed" with season audio: 41 of the 44 intro
  reviews in production. Now (§5.3 "Two playback speeds", §5.5 rule 12): the frame rate is read with the probe
  (`frame_rates`, with the identity it was read from); a group at two speeds is matched at the speed most of its files
  play at, the others on a retimed fingerprint (`asetrate`: 85 of 85 cross pairs match; `atempo`, which keeps the
  pitch, 0 of 85), answers read back at each file's own speed; IntroDB/TheIntroDB times (and importer copies of them)
  on a 25 fps or film-rate file are read scaled only when the raw times agree with no other source and the scaled ones
  do; on a 25 fps file a server's own marker at the raw online times confirms them only if the file's own check agrees
  with it too. Season audio v6 (`SEASON_AUDIO_VERSION`) and settings v18 (the decide-again job) bring stored answers up
  to date. Architecture review of the change (1 HIGH, 3 MED, 4 LOW, all fixed): scaling had also run on 23.976 files
  whose raw times already agreed (IntroDB 30–60 s with season audio 31–61.5 s published 31.3–62.6 s); pairs cached
  while a sibling's rate was unknown could be reused once it read as 25 fps (now each side's speed names the pair's
  version, and a changed rate drops the file's pairs); a sibling left out for a failed retimed fingerprint left an
  answer that never came due again (the signature now records the retimed fingerprint); a server's marker could
  confirm raw online times on a 25 fps file although Plex's own are the same mistake there; and the rate is read for a
  file stored before rates were only when an online answer or season audio needs it, a failed read remembered for a day.
  Measured on Bones S05–S08 (82 episodes; truth from frame checks: the "BONES" logo card − 4 s to the end of the
  "created by Hart Hanson" card, found by correlation, every episode ≥ 0.97), useful / wrong / missed: Plex's own
  **41 / 41 / 0** (every 25 fps file wrong: its 2025 markers sit at the film-rate times, as IntroDB's do, and skip
  13–18 s of story); ours before **38 / 0 / 44** (44 Needs review; 41 / 41 / 0 had Plex's markers counted as
  evidence); ours after **82 / 0 / 0**, with or without Plex's markers as evidence (season audio alone 72 / 0 / 10 →
  82 / 0 / 0; IntroDB and Plex's marker alone, no season audio, 41 / 41 / 0 → 41 / 0 / 41: the 25 fps files go to
  Needs review). No other set has a season at two speeds. Season audio alone measures as before: lab 118
  **91 / 12 / 15** (gate passed, port = reference), Accused **2 / 0 / 54**, held-out 175 **123 / 4 / 48** (Plex's own
  23 / 15 / 80, 0 / 0 / 56, 69 / 4 / 102). Decided with the files' real frame rates, season audio plus IntroDB's
  answers (fetched for the 350 files; 160 have an intro) plus Plex's own markers: lab 118 **86 / 5 / 27** before and
  after, Accused **2 / 0 / 54** (no IntroDB entries), held-out 175 **121 / 5 / 49 → 122 / 5 / 48** (12 of its files
  are 25 fps; the one change is Food Wars S01E05, a 23.976 file whose IntroDB intro, 11 s shorter than the real one,
  agreed with season audio only scaled: Needs review → useful, 3.5 s short). The library's two other mixed seasons
  (30 for 30 S04, Sort Of S03) get no season audio answer before or after. A release sped up with its pitch kept, and
  rates other than 23.976/24/25, are matched as they play, as before.
"""
)
p.write_text(s)
print("ok")
