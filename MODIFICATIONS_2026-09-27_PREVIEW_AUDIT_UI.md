# 2026-09-27 Preview audit + UI patch

Scope: Seedance preview execution/audit and preview review UI only. No script, asset, reference-image, storyboard, dialogue-plan, sound-plan, or continuity content is regenerated or modified by this patch.

## Seedance preview audit

- Persist `request_payload_digest` with SHA-256, prompt SHA-256, resolved reference keys, upstream revisions, and technical parameters.
- Persist `assembly_log` showing storyboard/dialogue/sound hydration without duplicating the full provider request body.
- Persist `preflight_checks` for the reference resolver, human-combination expansion, trusted provenance/TOS checks, and technical params.
- Persist `provider_generate_audio_echo` when Ark returns that field.
- Probe the downloaded MP4 with the configured FFmpeg sibling `ffprobe` and persist `audio_probe` (`has_audio`, codec, channels, sample rate when available). Probe failure is audit-only and does not turn a successfully generated/billed video into a failed job.
- `FFMPEG_PATH` already configured by the Harness is reused; no new env variable is required.

## Preview approval semantics

- A Director review action of `wait_for_user` on the `preview` stage now means exactly "waiting for human preview approval".
- It is no longer shown as "Director needs repair" for preview.
- `preview_approved` can be confirmed normally when the current preview review is `wait_for_user`; no force-approval warning is required.
- `regenerate_current` remains blocking.

## Preview UI

- Adds a dedicated full-width preview workbench instead of the generic artifact body with nested scrollbars.
- Keeps the video player constrained and centered for portrait 9:16 output.
- Adds quick status cards for model/output/reference/audio.
- Adds compact audit pills and hydration summary.
- Adds collapsed reference/preflight and payload-digest sections.
- Adds "open in new window", "download preview", and an in-card "confirm this preview" action.
- Historical preview artifacts without the new audit fields still render cleanly and are labeled as historical/no audit record.

## Explicit non-change

Dialogue/prompt behavior is not changed by this patch. The user confirmed the generated preview already contains the expected spoken dialogue.

## Validation

- `node --check static/app.js` passed.
- `pytest -q` -> `150 passed`.
