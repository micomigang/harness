# 2026-09-28 Final compose audit + audio normalization

## Why

A successful local FFmpeg compose could still be reviewed as `regenerate_current` because the external director preview intentionally omitted the private `local_path` and did not expose compose-specific execution evidence. The old compose command also re-encoded AAC without explicitly pinning the output to 32 kHz stereo, so a source-shot sample-rate mismatch (for example one 44.1 kHz clip among 32 kHz clips) was not explicitly auditable.

## Changes

- Final compose now explicitly writes AAC at 32,000 Hz / 2 channels (`-ar 32000 -ac 2`).
- Every input shot is ffprobed before compose and its audio codec/sample rate/channel count is recorded.
- The final master is ffprobed after FFmpeg and records:
  - FFmpeg return code
  - ffprobe return code
  - video codec and dimensions
  - audio codec, sample rate and channels
  - actual final duration
  - dynamic expected duration from the current storyboard
  - duration delta/tolerance
  - source shots that required normalization
- Adds `compose_validation`, a deterministic Harness authority derived from the current storyboard + source files + FFmpeg + ffprobe.
- External Kimi review receives the audit facts but still does **not** receive the literal private filesystem path. `local_path_present=true` and the deterministic validation are the review evidence.
- Adds a compose review guard: when `compose_validation.status=pass`, false review claims about missing local_path/input_shots/encoding/ffmpeg/ffprobe/32-kHz normalization are suppressed instead of forcing another compose.
- Final-compose UI now surfaces dynamic target/actual duration, ffprobe video/audio facts, normalization status, resampled shot indices, and deterministic validation checks.

## Dynamic behavior

No episode shot count or duration is hard-coded. Expected shot indices and expected master duration are always derived from the currently approved storyboard.

## Validation

- `pytest -q` — 167 passed
- `node --check static/app.js` — passed
