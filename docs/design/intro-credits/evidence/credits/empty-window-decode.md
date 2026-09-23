# A keyframe pass over a window with no keyframe (2026-09-23)

**Question.** The credit text detector's steps before its tail (spec §5.4 step 8, §14 2026-09-23) can ask for a
window that holds no keyframe. Does the keyframe pass then give no frames? On the GPU, `frames.run_decode` treats no
frames as a GPU failure, and the worker reruns the whole file on the CPU.

**Answer: no.** ffmpeg gives the first keyframe *after* the window and exits 0, on the GPU and on the CPU alike, for
H.264 in MP4 and Matroska and for VP9 in WebM. For a later step that keyframe is the first row the step before it
already read, and `rule_j.rows_before` drops it, so a keyframe-free step adds nothing. The first step's is the tail's
own first row, which `rule_j.joined_before` drops the same way. A keyframe pass gives no frames only when no keyframe
follows its start anywhere in the file (the VP9 tail whose container flags none, spec §5.4 Frames).

## Setup

- Image `ghcr.io/stevezau/media_preview_generator:pr-241` (`b24aec4acad3`, `GIT_SHA=16fed407`), ffmpeg 8.1.2, on
  `storage` with a Quadro P5000 (`-hwaccel cuda`, `scale_cuda`).
- The command is the app's own: `frames.decode_command(..., keyframes_only=True)` from this checkout, VP9 with its
  `noise=drop=not(key)` packet drop. `empty_window_decode.py` beside this file makes the clips and runs it.
- Clips: `testsrc2` 1280x720 at 24 fps for 100 s with a keyframe every 10 s (`-g 240 -keyint_min 240
  -sc_threshold 0`, keyframes at 0, 10, ... 90 s): H.264 (`libx264 -bf 2`) in MP4 and in Matroska, VP9
  (`libvpx-vp9 -deadline realtime -cpu-used 8`) in WebM.
- Windows: `[12, 32)` holds the 20 s and 30 s keyframes; `[41, 43)` and `[41, 49.9)` hold none.

## Result

| Clip | Decode | `[12, 32)` | `[41, 43)` | `[41, 49.9)` |
|---|---|---|---|---|
| H.264 MP4 | GPU | exit 0, 20, 30 | exit 0, **50** | exit 0, **50** |
| H.264 MP4 | CPU | exit 0, 20, 30 | exit 0, **50** | exit 0, **50** |
| H.264 MKV | GPU | exit 0, 20, 30 | exit 0, **50** | exit 0, **50** |
| H.264 MKV | CPU | exit 0, 20, 30 | exit 0, **50** | exit 0, **50** |
| VP9 WebM | GPU | exit 0, 20, 30 | exit 0, **50** | exit 0, **50** |
| VP9 WebM | CPU | exit 0, 20, 30 | exit 0, **50** | exit 0, **50** |

Frame times are `showinfo`'s `pts_time`, in seconds.

## What it changed

The first cut of the steps' 30 s minimum (commit f67980ea) was justified by "a sliver with no keyframe is a false
GPU failure", and it read "no frames" on a later step as no rows through a narrower exception. Neither can happen, so
the exception is gone. The minimum stays, for what it does save: a decode of its own -- an ffmpeg start, a seek and a
hardware decoder brought up -- for a window that holds a keyframe or two at most, and often only gives back one
already read. The detector tests' fake decoder now answers a keyframe-free window the way ffmpeg does.
