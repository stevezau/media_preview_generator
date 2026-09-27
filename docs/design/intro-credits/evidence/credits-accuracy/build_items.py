"""Build the verdict set (items.json): every frame-checked marker of the audit, plus the Plex-comparison files, with a
truth estimate, the stored evidence file id and the file's kind.

truth: {"start": s, "end": s|None, "tol": seconds the truth is known to} for credits (start only matters) and intros.
"""

import json
import os

from common import HERE, load, prod_db

db = prod_db()
verdicts = load("verdicts.json")
census_v = load("census_verdicts.json")
sample = {x["sid"]: x for x in load("sample.json")}
census = {x["cid"]: x for x in load("census.json")}
plex = load("plex_results.json")


def fileinfo(fid):
    row = db.execute("select canonical_path,duration_ms,season_key,is_movie from files where id=?", (fid,)).fetchone()
    return {"path": row[0], "dur": row[1], "episode": row[2] is not None, "movie": bool(row[3])}


# Truths read off the Plex comparison sheets (frames/P##.jpg descriptions), seconds.
PLEX_TRUTH = {
    "P01": 5243.0, "P02": 5315.0, "P03": 7050.0, "P04": 5641.5, "P05": 7108.0, "P06": 5572.2, "P07": 5847.0,
    "P08": 5735.8, "P09": 6741.0, "P10": 5088.0, "P11": 5549.0, "P12": 7111.0, "P13": 6188.0, "P14": 7724.0,
    "P15": 6957.0, "P16": 7324.8, "P17": 7269.0, "P18": 5826.0, "P19": 5466.5, "P20": 9568.0, "P22": 6503.0,
    "P23": 5311.0, "P24": 5952.0, "P25": 5003.0, "P26": 4636.8, "P28": 5813.8, "P29": 4420.3, "P30": 5202.8,
    "P31": 5769.0, "P33": 3121.0, "P34": 3365.0, "P35": 3376.0, "P36": 3330.0, "P37": 3072.0, "P38": 3060.0,
    "P39": 3252.0, "P40": 2939.0, "P41": 1308.0, "P42": 3464.0, "P43": 3991.0, "P44": 3179.0, "P45": 2963.0,
    "P46": 3229.0, "P47": 3404.0, "P49": 2577.1, "P50": 2578.6, "P51": 2577.2, "P52": 2555.6,
}  # fmt: skip
UNCLEAR_P = {"P21", "P27", "P32", "P48", "P53"}

items = []
for sid, x in sample.items():
    v = verdicts[sid]
    verdict, text, direction, off = v
    s, e = x["s"] / 1000, x["e"] / 1000
    truth = None
    if verdict == "right":
        truth = {"start": s, "end": e, "tol": 5.0, "approx": True}
    elif verdict == "wrong":
        if x["type"] == "credits":
            truth = {"start": s + off if direction == "early" else s - off, "end": None, "tol": 3.0, "approx": False}
    items.append({"id": sid, "fid": x["fid"], "type": x["type"], "ours": [s, e], "by": x["by"], "verdict": verdict,
                  "direction": direction, "off": off, "text": text, "truth": truth, **fileinfo(x["fid"]),
                  "set": "sample"})  # fmt: skip

for cid, x in census.items():
    verdict, text, direction, off = census_v[cid]
    s, e = x["s"] / 1000, x["e"] / 1000
    truth = None
    if verdict == "right":
        truth = {"start": s, "end": e, "tol": 5.0, "approx": True}
    elif x["type"] == "credits":
        truth = {"start": s + off if direction == "early" else s - off, "end": None, "tol": 3.0, "approx": False}
    items.append({"id": cid, "fid": x["fid"], "type": x["type"], "ours": [s, e], "by": x["by"], "verdict": verdict,
                  "direction": direction, "off": off, "text": text, "truth": truth, **fileinfo(x["fid"]),
                  "set": "census"})  # fmt: skip

# The intro edges of the three wrong intros, from the audit's frame descriptions.
INTRO_TRUTH = {"S58": (11.0, 106.0), "S67": (6.0, 36.0), "S71": (5.0, 112.0)}
for it in items:
    if it["id"] in INTRO_TRUTH:
        a, b = INTRO_TRUTH[it["id"]]
        it["truth"] = {"start": a, "end": b, "tol": 3.0, "approx": False}

# The Plex comparison: every credits file with a stored, non-stale Plex read.
plex_items = []
for r in plex:
    if r["type"] != "credits":
        continue
    pid = r.get("pid")
    ours_s = r["ours"][0] / 1000
    if pid is None:
        truth = {"start": ours_s, "end": None, "tol": 5.0, "approx": True}
        ours_v, plex_v = "right", "right"
    elif pid in UNCLEAR_P:
        truth = None
        ours_v, plex_v = r["ours_v"][0], r["plex_v"][0]
    else:
        truth = {"start": PLEX_TRUTH[pid], "end": None, "tol": 3.0, "approx": r["ours_v"][0] == "right"}
        ours_v, plex_v = r["ours_v"][0], r["plex_v"][0]
    plex_items.append({"id": pid or f"A{r['fid']}", "fid": r["fid"], "type": "credits", "ours": r["ours"],
                       "by": r["by"], "published": r["published"], "plex_s": r["plex_s"] / 1000,
                       "ours_v": ours_v, "plex_v": plex_v, "truth": truth, **fileinfo(r["fid"]),
                       "set": "plex"})  # fmt: skip

for it in items + plex_items:
    it["on_disk"] = os.path.exists(it["path"])

json.dump({"verdict": items, "plex": plex_items}, open(HERE / "items.json", "w"), indent=1)
print(len(items), "verdict items;", sum(i["type"] == "credits" for i in items), "credits;", len(plex_items), "plex")
print("missing on disk:", [i["id"] for i in items + plex_items if not i["on_disk"]])
wrong_credits = [i["id"] for i in items if i["type"] == "credits" and i["verdict"] == "wrong"]
print("verdict credits wrong:", len(wrong_credits), wrong_credits)
print("plex: ours wrong", sum(i["ours_v"] == "wrong" for i in plex_items if i["truth"] is not None),
      "plex wrong", sum(i["plex_v"] == "wrong" for i in plex_items if i["truth"] is not None))  # fmt: skip
