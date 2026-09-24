import pathlib

p = pathlib.Path("/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/stale_fresh/live_check.py")
s = p.read_text()


def rep(old, new):
    global s
    assert s.count(old) == 1, old
    s = s.replace(old, new)


rep(
    "# Sonarr imports: latest import date per path",
    '''if STATUS == "replay":
    import shutil
    from dataclasses import replace

    from media_preview_generator.markers import pipeline as mp
    from media_preview_generator.markers.decide import DecisionContext, decide
    from media_preview_generator.markers.settings import load_global
    from media_preview_generator.markers.store import MarkerStore

    shutil.copy(f"{SP}/prod_markers.db", f"{D}/markers_copy.db")
    store = MarkerStore(f"{D}/markers_copy.db")
    gs = load_global(json.load(open(f"{D}/prod_markers_settings.json")))
    order = mp._decision_order(gs)
    frec = {
        fid: (dur, bool(movie), season)
        for fid, dur, movie, season in con.execute("select id, duration_ms, is_movie, season_key from files")
    }
    TYPES = frozenset({MarkerType.INTRO, MarkerType.CREDITS})

    def replay(fid, mtype, stale_flag):
        ev = store.get_evidence(fid)
        if stale_flag:
            ev = [replace(c, stale=True) if c.source.value == "server_markers" and c.type is mtype else c for c in ev]
        dur, is_movie, season_key = frec[fid]
        cw, cap = mp.credits_limits_ms(
            is_episode=season_key is not None, tv_window_s=gs.credits_tv_s, movie_window_s=gs.credits_movie_s
        )
        known, limit = store.get_intro_chapter_limit(fid)
        dctx = DecisionContext(
            dur or 0, is_movie, mp.APP_PUBLISH_WHEN, TYPES, order, limit if known else None,
            movie_credits_max_from_end_ms=cap, credits_window_ms=cw,
        )
        return decide([c for c in ev if c.source.value in set(order)], dctx, store.get_locked(fid))[mtype]


# Sonarr imports: latest import date per path''',
)
rep(
    """            dec = decisions.get((fid, mtype))
            if dec is None:""",
    """            before = None
            if STATUS == "replay":
                after, before = replay(fid, mtype, True), replay(fid, mtype, False)
                transitions[(text, before.status.value, after.status.value)] += 1
                m = after.marker if after.marker is not None else after.proposed
                dec = (m.start_ms, m.end_ms, after.reason + f" [{after.status.value}]") if m is not None else None
                if dec is not None and after.marker is None:
                    dec = None  # proposed only: not an answer of ours that would be published
            else:
                dec = decisions.get((fid, mtype))
            if dec is None:""",
)
rep("results = []\n", "results = []\ntransitions = collections.Counter()\n")
rep(
    """                    show=info["show"],
                )""",
    """                    show=info["show"],
                    before=None if before is None else (before.status.value, before.reason,
                                                        before.marker and (before.marker.start_ms, before.marker.end_ms)),
                )""",
)
rep(
    'print("unreadable parts (pv key not JSON):", unreadable_parts)',
    'print("unreadable parts (pv key not JSON):", unreadable_parts)\nfor k, v in sorted(transitions.items()):\n    print("transition before->after", k, v)',
)
p.write_text(s)
