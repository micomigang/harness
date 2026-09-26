# Storyboard review + UI fix (2026-09-26)

## Root cause fixed

1. The art-director review preview applied the generic list cap to `storyboard.shots`, so a 14-shot artifact was exposed to the reviewer as only the first 10 shots. The reviewer then falsely reported shots 11-14 / tail beats as missing and computed duration against the truncated preview.
2. Director upstream planning also used a generic list cap. Full storyboard shot sequences and full compact reference-image contracts now remain available to downstream planning.
3. Storyboard rendering used the generic artifact renderer plus `.artifact-content { max-height: 210px; overflow:auto; }`, producing the cramped nested-scroll/raw-prompt UI.

## Changes

- Keep the complete storyboard shot sequence in a compact reviewer preview.
- Keep complete compact storyboard and reference-image contracts in director upstream context.
- Add deterministic `storyboard_validation` for:
  - requested/actual shot count;
  - index continuity;
  - required per-shot fields;
  - reference binding completeness;
  - duration sum.
- Reconcile `estimated_seconds` to the exact sum of shot durations and preserve the model value as `estimated_seconds_model` when it differed.
- Add a storyboard review guard so reviewer-only count/index/duration false negatives cannot force a regeneration when deterministic validation passes. Semantic review remains model-owned.
- Add `POST /api/workspaces/{workspace_id}/storyboard/revalidate` to re-check the current storyboard without regenerating creative shots.
- Add a storyboard workbench UI: responsive shot cards, duration, story beat, asset/reference chips, collapsible camera/blocking/provider prompt, structural validation banner, and `重新校验当前分镜（不重生成）`.
- Remove the tiny nested storyboard scroll pane while keeping long provider prompts collapsed by default.

## Generality

No episode-specific shot count, beat IDs, character names, scene names, prop names, or candidate IDs are hard-coded. The expected shot count is resolved from the bound execution override, an explicit numeric shot-count instruction, or workspace settings.

## Verification

- Python syntax compile: pass
- JavaScript syntax check: pass
- Focused automated tests: 29 passed
