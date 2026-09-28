# 2026-09-28 Explicit music skip -> compose

## Purpose

Allow production to defer independent BGM generation while the music API is not configured, without pretending that music was rendered.

## Behavior

- Adds an explicit **跳过音乐，直接进入合成** UI action.
- Requires a complete deterministic batch-video coverage check and approved preview.
- Does not call Kimi for music generation, does not call a music provider, and does not create any new media.
- Creates an auditable `music` artifact with `mode=skipped_no_bgm`, `skipped=true`, and `music_api_called=false`.
- Existing Seedance dialogue / ambience / Foley audio is preserved in the shot videos.
- `compose` becomes runnable from `batch_video + music(skip)`; `music_plan` remains optional when music was explicitly skipped.
- If a valid music plan already exists it is attached to the skip record for later use, but it is not required to compose.
- Future changes to music/music_plan continue to invalidate downstream compose normally.

## Safety

The skip action is rejected when:

- any workspace job is still active;
- `preview_approved` is not approved;
- batch_video is not ready; or
- deterministic batch-video coverage is incomplete.

## Validation

- Backend tests verify skip creates no media and immediately exposes compose as the next stage.
- Backend tests verify missing music_plan can be bypassed only via explicit skip.
- Backend tests verify stale/incomplete batch cannot be bypassed.
- Frontend test verifies the explicit skip UI and endpoint binding.
