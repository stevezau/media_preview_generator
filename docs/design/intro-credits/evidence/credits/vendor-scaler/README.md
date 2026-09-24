# One scaler on every decode path (credits text version 4) — evidence

Spec §5.4 Frames, §14 2026-09-24 "Credits text version 4". `scale_cuda`, `scale_vaapi` and swscale's bicubic blurred
small text differently, so rule J answered differently per vendor (80-file set: NVIDIA 66 within 10 s, CPU 61,
Intel 58). Every decode now downloads the whole decoded frame in the stream's own format (NV12 / P010) and scales it
by the nearest pixel: the frames are bit-identical on every vendor. Full write-up, costs and the release checks:
`../small-text-retry.md`.

The scripts ran from the session scratchpad (`MEDIA_PREVIEW_TEXTDET_MODEL` pointed at `../bench/textdet-model/`,
local-only). Per-file results (`<tag>_<variant>.json`), logs and file lists are local-only; the captured Y planes
(`*.npz`, ~1 GB) and the harness decode caches were not kept.

## Headline numbers

- 15 hard files × 9 variants (`intel`, `nvidia`, `cpu`; `*dl` = download then swscale; `*fixneighbor` = the shipped
  nearest-pixel download path; `cpuneighbor`, `cpufast_bilinear`): with the shipped path every vendor gives the same
  frames and the same rule J answer.
- 80-file harness on Intel with the old scaler vs nearest (`h80_intel.json`, `h80_neighbor.json`).
- `mpgverify/`: 8 files through the worktree's own frame extraction on Intel VAAPI (UHD 770), CUDA (TITAN RTX) and the
  CPU: the same answer on all three for every file (`compare.json`, local-only).

## Files

| File | What |
|---|---|
| `run_vendor.py` | The app's `find_credits` on one file through one vendor, capturing every decoded frame's Y plane and boxes (Intel runs in a throwaway container on `plex`) |
| `batch.sh`, `batch_fix.sh`, `batch_flags.sh` | Every file of a list (`list3.tsv`, `list12.tsv`, `list15.tsv`, local-only) through every variant, one at a time |
| `cmp.py`, `summary.py`, `swap.py`, `planes.py`, `table.py` | Vendor-vs-vendor comparisons: same pts, luma differences, credit-row flips, rule J starts; `swap.py` swaps luma and boxes between vendors to find which one moves the answer |
| `harness_scaler.py`, `harness_intel.py` | The credits-text harness with one swscale filter on every path, and on Intel |
| `bench_intel.sh`, `bench_intel2.sh` | Intel decode timing, old path vs full-frame download |
| `mpgverify/verify.py`, `mpgverify/compare.py`, `mpgverify/run_all.sh` | The verification run per vendor and its comparison; `files.tsv`, `local/` (file list, query, provenance) are local-only |
