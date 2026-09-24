"""Score season-step variants on the four truth sets (lab118=lists, held-out=scale_clean, accused, chap_clean) with the
app's code (main checkout, dev) and cached end-picture shares (decodes any new share on the NVIDIA GPU)."""
import collections, os, pickle, sys, time
OLD = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/a98735d3-c717-46ce-a02b-854ad26af8ae/scratchpad/intro-pick"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
import media_preview_generator.markers.audio.season as S  # noqa: E402
from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.markers.audio.matcher import intro_candidates, file_hits, meets_quorum  # noqa: E402
assert S.__file__.startswith("/home/data/workspace/plex_generate_vid_previews/"), S.__file__
sys.path.insert(1, OLD)
from evaluate import eval_groups  # noqa: E402
from lab import Season, judge  # noqa: E402

reader = end_picture.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0")
CACHE = os.path.join(HERE, "kq_shares.pkl")
shares = pickle.load(open(CACHE, "rb"))
new_decodes = 0

def ep_oracle(target):
    def check(c):
        global new_decodes
        members = [h for h in c.members if os.path.exists(h.partner)]
        got = []
        for hit in end_picture.partners(members):
            off = hit.partner_start_s - hit.start_s
            key = (target, hit.partner, round(c.segment.start_s, 3), round(c.segment.end_s, 3), round(off, 3))
            if key not in shares:
                try:
                    shares[key] = reader.share(target, hit.partner, c.segment.start_s, c.segment.end_s, off)
                except end_picture.ReadFailedError:
                    shares[key] = None
                new_decodes += 1
            got.append(shares[key])
        return end_picture.passes(got)
    return check

# ---- variants: each returns the segment for one target -------------------------------------------------------------
def walk(target, files, pts, runs, *, core_early_only=False, skip_no_quorum=False, silence_in_walk=False,
         min_file_start=S.MIN_FILE_START_LENGTH_S, file_start_s=S.FILE_START_S, early_s=end_picture.EARLY_START_S,
         late_core=None, stop_after_quorum_fail=True, core_max_len=None, core_exempt_min_start=0.0, late_len_for_no_core=None, late_no_core_min_support=0):
    others = len(files) - 1
    oracle = ep_oracle(target)
    for c in intro_candidates(file_hits(target, files, runs)):
        seg = c.segment
        if not meets_quorum(seg.support, others):
            if skip_no_quorum:
                continue
            return None
        if seg.start_s < file_start_s and seg.end_s - seg.start_s < min_file_start:
            continue
        early = seg.start_s <= end_picture.EARLY_START_S
        need_core = S.MIN_DENSE_CORE_S if (early or not core_early_only) else (late_core or 0.0)
        if core_max_len is not None and seg.end_s - seg.start_s >= core_max_len and seg.start_s >= core_exempt_min_start:
            need_core = 0.0
        if late_len_for_no_core is not None and not early:
            need_core = 0.0 if (seg.end_s - seg.start_s >= late_len_for_no_core and seg.support >= late_no_core_min_support) else S.MIN_DENSE_CORE_S
        if need_core and S.dense_core_s(target, c, pts) < need_core:
            continue
        if seg.start_s <= early_s and not oracle(c):
            continue
        if silence_in_walk and S._mostly_silence(pts[target], seg):
            continue
        return None if S._mostly_silence(pts[target], seg) else seg
    return None

VARIANTS = {
    "shipped": dict(),
    "core early only": dict(core_early_only=True),
    "skip no-quorum": dict(skip_no_quorum=True),
    "core early only + skip no-quorum": dict(core_early_only=True, skip_no_quorum=True),
    "silence in walk": dict(silence_in_walk=True),
    "late core>=5": dict(core_early_only=True, late_core=5.0),
    "late core>=6": dict(core_early_only=True, late_core=6.0),
    "late core>=7": dict(core_early_only=True, late_core=7.0),
    "late core>=6 + skip nq": dict(core_early_only=True, late_core=6.0, skip_no_quorum=True),
    "late core>=5 + skip nq": dict(core_early_only=True, late_core=5.0, skip_no_quorum=True),
    "late: end-picture not core": dict(core_early_only=True, early_s=1e9),
    "late: end-picture not core + skip nq": dict(core_early_only=True, early_s=1e9, skip_no_quorum=True),
    "fs15 + late: end-picture not core": dict(core_early_only=True, early_s=1e9, min_file_start=15.0),
    "fs15 + late: ep not core + skip nq": dict(core_early_only=True, early_s=1e9, min_file_start=15.0, skip_no_quorum=True),
    "core only <20 s": dict(core_max_len=20.0),
    "core only <30 s": dict(core_max_len=30.0),
    "core only <45 s": dict(core_max_len=45.0),
    "core only <60 s": dict(core_max_len=60.0),
    "fs15 + core only <30 s": dict(core_max_len=30.0, min_file_start=15.0),
    "fs15 + skip nq": dict(min_file_start=15.0, skip_no_quorum=True),
    "fs15 + core<30 + skip nq": dict(core_max_len=30.0, min_file_start=15.0, skip_no_quorum=True),
    "core exempt >=30 s off file start": dict(core_max_len=30.0, core_exempt_min_start=2.0),
    "core exempt >=45 s off file start": dict(core_max_len=45.0, core_exempt_min_start=2.0),
    "fs15 + core exempt >=30 off start": dict(core_max_len=30.0, core_exempt_min_start=2.0, min_file_start=15.0),
    "late no-core if len>=10": dict(late_len_for_no_core=10.0),
    "late no-core if len>=10 + skip nq": dict(late_len_for_no_core=10.0, skip_no_quorum=True),
    "late no-core if len>=12": dict(late_len_for_no_core=12.0),
    "fs15 + late no-core if len>=10": dict(late_len_for_no_core=10.0, min_file_start=15.0),
    "fs15 + late no-core if len>=10 + skip nq": dict(late_len_for_no_core=10.0, min_file_start=15.0, skip_no_quorum=True),
    "COMBO fs15 + late<10 core + long exempt": dict(late_len_for_no_core=10.0, min_file_start=15.0, core_max_len=30.0, core_exempt_min_start=2.0),
    "late<10 core + long exempt": dict(late_len_for_no_core=10.0, core_max_len=30.0, core_exempt_min_start=2.0),
    "COMBO sup3": dict(late_len_for_no_core=10.0, late_no_core_min_support=3, min_file_start=15.0, core_max_len=30.0, core_exempt_min_start=2.0),
    "COMBO sup2": dict(late_len_for_no_core=10.0, late_no_core_min_support=2, min_file_start=15.0, core_max_len=30.0, core_exempt_min_start=2.0),
    "fs15 + long exempt": dict(min_file_start=15.0, core_max_len=30.0, core_exempt_min_start=2.0),
    "COMBO sup2 + skip nq": dict(late_len_for_no_core=10.0, late_no_core_min_support=2, min_file_start=15.0, core_max_len=30.0, core_exempt_min_start=2.0, skip_no_quorum=True),
    "REC late(>=10s sup>=2) + early long exempt": dict(late_len_for_no_core=10.0, late_no_core_min_support=2, core_max_len=30.0, core_exempt_min_start=2.0),
    "REC-A early long exempt only": dict(core_max_len=30.0, core_exempt_min_start=2.0),
    "REC-B late(>=10s sup>=2) only": dict(late_len_for_no_core=10.0, late_no_core_min_support=2),
    "fs15 + late core>=6": dict(core_early_only=True, late_core=6.0, min_file_start=15.0),
    "file-start min 15 s": dict(min_file_start=15.0),
    "file-start(<6 s) min 15 s": dict(min_file_start=15.0, file_start_s=6.0),
}

def run(mode, names):
    tallies = {n: collections.Counter() for n in names}
    details = {n: {} for n in names}
    for _season, files, fps, truth in eval_groups(mode):
        Sn = Season(files, fps)
        for f, tr in truth.items():
            for n in names:
                seg = None
                if len(Sn.files) >= 2 and f in Sn.files:
                    seg = walk(f, Sn.files, fps, Sn.runs, **VARIANTS[n])
                v = ("wrong" if seg else "none-ok") if tr is None else judge(seg[:2] if seg else None, tr)
                tallies[n][v] += 1
                details[n][f] = (tuple(seg) if seg else None, tr, v)
    return tallies, details

if __name__ == "__main__":
    names = [n for n in VARIANTS if not os.environ.get("ONLY") or n in os.environ["ONLY"].split(",")]
    out = {}
    for mode in sys.argv[1:]:
        t0 = time.time()
        tallies, details = run(mode, names)
        out[mode] = details
        pickle.dump(shares, open(CACHE, "wb"))
        for n in names:
            t = tallies[n]
            print(f"{mode:12} {n:36} useful {t['useful']:3} wrong {t['wrong']:3} missed {t['missed']:3} none-ok {t['none-ok']}", flush=True)
        print(f"   ({time.time()-t0:.0f}s, new decodes so far {new_decodes})", flush=True)
    pickle.dump(out, open(os.path.join(HERE, "kq_variants_details.pkl"), "wb"))
