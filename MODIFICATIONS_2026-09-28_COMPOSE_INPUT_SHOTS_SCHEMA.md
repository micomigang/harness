# 2026-09-28 Compose input_shots schema fix

## What was wrong

The compose artifact exposed `input_shots` as an integer count and `input_sources` as the per-shot provenance list. The bound compose acceptance contract requires `input_shots` itself to be the per-shot array, so the Director reviewer correctly blocked delivery even though FFmpeg and ffprobe had succeeded.

## Fixed contract

`compose.content.input_shots` is now the canonical dynamic per-shot array. Each row records:

- `shot_index`
- `source_url`
- `source_path` (internal only)
- `source_path_present`
- `file_name`
- `file_size_bytes`
- `actual_duration_seconds`
- `expected_duration_seconds`
- `duration_delta_seconds`
- `reused_from`

`input_shot_count` is the numeric count. `input_sources` remains as a backward-compatible alias.

## Privacy

Literal local filesystem paths are kept internally but stripped from the external Director review context. The reviewer still sees source URL, file-presence evidence, filename/size, actual duration and expected-vs-actual duration evidence.

## Validation

Compose deterministic validation now checks the `input_shots` schema and records a full dynamic `shot_duration_comparison`. Delivery QA validates the array schema instead of interpreting `input_shots` as an integer.

## UI

The final-compose workbench now shows a collapsible per-shot input/duration audit table. Object-shaped Director deviations are rendered as readable text instead of `[object Object]`.

## Dynamic behavior

No runtime logic is hard-coded to 14 shots or 158 seconds. Shot rows and expected durations are derived from the currently approved storyboard.

## Validation results

- `pytest -q` -> 169 passed
- `node --check static/app.js` -> passed
