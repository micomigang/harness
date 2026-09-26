# 2026-09-26 Dialogue Plan Review + UI Fix

This incremental patch is intended to be applied after the storyboard review/UI fix.

## Fixes

- Prevents the director reviewer from seeing only the first 10 `dialogue_plan.items`.
- Adds `dialogue_validation` owned by Harness, using the current storyboard shot sequence as the structural contract.
- Normalizes compatibility aliases (`text` <-> `dialogue_text`, `delivery` -> `delivery_note`, `lip_sync` -> `lip_sync_target` where safe).
- Suppresses reviewer-only false negatives about missing tail shot indices when deterministic dialogue validation passes.
- Adds metadata-only `POST /api/workspaces/{workspace_id}/dialogue-plan/revalidate` so an existing dialogue plan can be rechecked without asking Kimi to regenerate text.
- Adds a dedicated dialogue/lip-sync workbench UI showing all shot rows, speaker, dialogue, timing, lip-sync target, delivery note, subtitle and silent-shot state.
- Removes the tiny status-only/scroller presentation for dialogue plans.

## Generality

No episode-specific shot count, character ID, dialogue text or silent-shot list is hardcoded. Expected coverage is derived from the current storyboard artifact.

## Verification

- Python compile: pass
- JavaScript syntax (`node --check`): pass
- Targeted regression tests for complete dialogue preview, deterministic dialogue validation and review guard: 3 passed
