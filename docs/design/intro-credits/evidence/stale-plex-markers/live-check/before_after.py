import collections
import pickle

D = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/stale_fresh"
res = pickle.load(open(f"{D}/live_check_replay.pkl", "rb"))
c = collections.Counter()
for r in res:
    status, reason, marker = r["before"]
    same = marker == tuple(r["ours"])
    c[
        (
            r["type"],
            "agree" if r["agree"] else "disagree",
            f"before={status}",
            "same-answer" if same else "answer-changed",
        )
    ] += 1
    if not same:
        print(
            r["type"],
            "before:",
            status,
            marker,
            reason[:70],
            "| after:",
            r["ours"],
            r["reason"][:60],
            "|",
            r["path"].split("/")[-1][:60],
        )
for k, v in sorted(c.items()):
    print(k, v)
