# 2026-09-26 — Sound Plan review + UI fix

This incremental patch fixes the same preview-truncation failure mode previously seen in storyboard/dialogue_plan, and replaces the generic sound-plan renderer with a readable shot workbench.

## Backend
- `sound_plan.items` is normalized against the complete current storyboard shot sequence.
- Adds deterministic `sound_validation` with expected/actual shot coverage, index continuity, missing fields, and dialogue/silent shot diagnostics.
- Kimi review receives the complete compact sound-plan item sequence instead of the generic first-10 preview.
- If `sound_validation.status=pass`, tail-shot count/missing-11–14 false negatives caused only by old preview truncation are suppressed; genuine semantic sound-design deviations remain reviewable.
- Adds `POST /api/workspaces/{workspace_id}/sound-plan/revalidate` for metadata/review-only revalidation; it does not regenerate sound design.
- Complete compact sound_plan context is also passed downstream (e.g. continuity review), so later stages do not lose shots after item 10.

## UI
- Dedicated 1:1 sound-plan workbench, responsive 3/2/1-column layout.
- Shows each shot's ambience, Foley, cues, dialogue ducking, negative-audio constraints, and dialogue/silent context.
- Removes the generic tall empty/inner-scroll presentation for sound_plan.
- Adds `重新校验当前音效（不重生成）`.

## Scope
- Does not change storyboard/dialogue content.
- Does not create audio, BGM, video, or new dialogue.
- Expected count derives from the current storyboard; no EP02-specific count is hard-coded.
