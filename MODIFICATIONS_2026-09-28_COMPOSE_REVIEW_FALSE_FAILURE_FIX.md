# 2026-09-28 Compose review false-failure fix

## What this fixes

1. Compose review no longer truncates `input_shot_indices` / `expected_shot_indices` to the first 10 values.
2. Compose now records a full per-shot `input_sources` audit list. Literal local filesystem paths remain hidden from the external reviewer, while shot index, media URL, file presence, filename and file size remain auditable.
3. Duration tolerance is dynamic, based on current storyboard/input shot count: `max(1.0s, 0.1s * input_shots)`. This tolerates normal frame/AAC packet-boundary drift from concatenating many clips without accepting genuine missing timeline.
4. The compose artifact records the requested resolution/ratio inherited from generated shots.
5. Reviewer guidance explicitly recognizes `720x1280` as the correct raster for `720p 9:16`; `1080x1920` is 1080p and must not be required for a 720p request.
6. When deterministic `compose_validation.status=pass`, false reviewer claims about truncated indices, resolution, duration tolerance, FFmpeg/ffprobe evidence, or audio normalization are suppressed.
7. Compose UI shows requested technical spec and dynamic duration tolerance alongside actual ffprobe results.

## Important

All shot counts and duration tolerances are dynamic. No episode-specific `14` or `158s` constants are used in runtime logic.

## Validation

- `pytest -q` -> 168 passed
- `node --check static/app.js` -> passed
