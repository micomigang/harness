# 2026-09-28 Music plan / compose UI and JSON recovery patch

## Scope

This is an incremental patch for the post-batch stages. It does not regenerate or modify locked storyboard, dialogue, sound, assets, references, preview, or batch-video clips.

## Music-plan execution fixes

- `music_plan` now depends explicitly on the locked storyboard and sound plan in addition to batch video, script, and dialogue plan.
- The Kimi music-director request uses a dedicated compact prompt context:
  - script: target market / localization / beats / continuity notes only;
  - storyboard: every current shot index, duration and story beat;
  - dialogue: every current shot dialogue/timing row;
  - sound: every current shot ambience/ducking/negative-audio row;
  - batch: every current shot index/duration/status only.
- Character candidates, reference-image payloads, Seedance URLs, provenance blobs and unrelated long artifacts are not sent to `music_plan`.
- The shot count remains dynamic and is always derived from the current storyboard.
- The music prompt now requires compact JSON-only output with no planning prose.
- If Kimi still returns a substantively useful but malformed JSON response, Harness performs one JSON-schema repair pass using only the failed model output. It does not re-run any video/image provider and records `_parse_recovery` when used.

## Deterministic music-plan validation

Every successful `music_plan` now receives `music_plan_validation` with:

- expected storyboard shot indices/count;
- covered shot indices;
- missing shots;
- cue count;
- invalid cue ranges;
- total storyboard duration;
- dialogue shot indices;
- structural authority = harness.

No fixed 14-shot assumption is used.

## UI fixes

### Full-film music plan

Adds a full-width `music_plan` workbench with:

- validation/coverage summary;
- global style;
- actual render mode;
- global ducking policy;
- cue cards with shot range, timeline, mood, instrumentation, intensity and dialogue avoidance;
- explicit no-BGM/silence cues;
- collapsed raw JSON / execution context instead of a generic JSON blob.

### Music rendering

Adds a dedicated `music` workbench that clearly states whether an independent BGM file was actually rendered. The current local-media fallback no longer mentions a hard-coded Seedance model version and explicitly states when only Seedance native shot audio is being retained.

### Final compose

Adds a full-width `compose` workbench with:

- final-master video player;
- input-shot coverage;
- H.264/AAC encoding facts;
- music mode;
- local path;
- open/download actions;
- collapsed execution JSON.

Compose now sorts shots by `shot_index` and refuses to assemble an incomplete/out-of-order storyboard batch.

### Failed JSON page

Historical `Model response could not be parsed...` errors are shown as a concise actionable message instead of dumping the full Kimi response into the production stream. The raw error remains available as the job-row title/tooltip.

## Browser cache

Static asset version bumped to:

- `app.js?v=20260928-v17-music-compose-ui`
- `style.css?v=20260928-v17-music-compose-ui`

## Validation

- `node --check static/app.js` — passed
- `pytest -q` — 160 passed
