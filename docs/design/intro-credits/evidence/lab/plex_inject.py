"""Build SQL to inject markers into a Plex DB (lab only). Prints SQL to stdout."""
import json, sys, urllib.parse

def part_extra(existing: str, intros, credits) -> str:
    d = json.loads(existing) if existing else {}
    if intros is not None:
        d["pv:intros"] = json.dumps({"MediaPartMarkersArray": {"attributeName": "intros", "version": 5,
            "MediaPartMarker": [{"startTimeOffset": s, "endTimeOffset": e} for s, e in intros]}}, separators=(",", ":"))
    if credits is not None:
        d["pv:credits"] = json.dumps({"MediaPartMarkersArray": {"attributeName": "credits", "version": 4,
            "MediaPartMarker": [{"startTimeOffset": s, "endTimeOffset": e, "final": True} for s, e in credits]}}, separators=(",", ":"))
    d.pop("url", None)
    d = dict(sorted(d.items()))
    d["url"] = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}" for k, v in d.items())
    return json.dumps(d, separators=(",", ":"))

if __name__ == "__main__":
    existing = sys.stdin.read().strip()
    intros = json.loads(sys.argv[1]); credits = json.loads(sys.argv[2])
    print(part_extra(existing, intros, credits).replace("'", "''"))
