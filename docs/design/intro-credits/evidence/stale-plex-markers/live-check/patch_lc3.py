import pathlib

p = pathlib.Path(
    "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/stale_fresh/live_check.py"
)
s = p.read_text()


def rep(old, new):
    global s
    assert s.count(old) == 1, old
    s = s.replace(old, new)


rep(
    "ep_of_path = {}\n",
    """def tv_key(path):
    i = path.find("/TV Shows/")
    return path[i:] if i >= 0 else path


imports_by_path = {tv_key(a): b for a, b in imports_by_path.items()}
imports_by_ep = {ep: [(ts, tv_key(pth)) for ts, pth in lst] for ep, lst in imports_by_ep.items()}
ep_of_path = {}
""",
)
rep(
    """    if "/Movies/" in path:
        return "movie"
""",
    """    if "/Movies/" in path:
        return "movie"
    path = tv_key(path)
""",
)
p.write_text(s)
