# 2026-09-28 Batch video parallel / resume / UI patch

## Scope

This patch only changes batch-video execution and presentation. It does not redesign or regenerate locked upstream creative artifacts.

## Fixes

1. **Full storyboard coverage by default**
   - `VIDEO_BATCH_MAX_SHOTS` no longer silently truncates a normal full batch to the first 8 shots.
   - A `batch_max_shots` Director override still works for an explicitly requested partial test run.
   - Resume runs always target full storyboard coverage.

2. **Preview gate enforced twice**
   - Orchestrator already requires `preview_approved`.
   - Seedance provider now also refuses batch submission unless `preview_approved=approved`, before any billable task is created.

3. **No duplicate Shot 01 charge**
   - An approved preview is reused as the batch production item for its shot.
   - If an older batch accidentally contains a duplicate Shot 01, the approved preview overrides it.

4. **Safe resume of partial batches**
   - Existing succeeded batch clips are reused by `shot_index`.
   - A rerun of an 8/14 batch submits only the missing shots.
   - Partial batch artifacts are kept but marked stale so music/compose cannot advance until coverage is complete.

5. **Bounded parallel generation**
   - Ark still creates one Seedance task per shot.
   - Harness now runs independent shot tasks in bounded parallel waves.
   - `VIDEO_BATCH_CONCURRENCY` controls wave size; default is 3.
   - Every missing shot is preflighted before any new paid task is submitted.
   - If one task in a wave fails, the next wave is not submitted. Tasks already submitted in the current wave are allowed to finish.

6. **Batch-video workbench UI**
   - Batch card spans the full workspace width.
   - Removes the small nested scrolling output box.
   - Shows 14-shot coverage, missing shots, reused/new counts, concurrency, and fail-stop mode.
   - Compact shot cards open a detail modal with video playback and metadata.
   - Incomplete historical batches expose a `补齐缺失镜头（复用已成功）` action.
   - Director execution context and audit are collapsed by default.

## Environment

Recommended addition:

```ini
VIDEO_BATCH_CONCURRENCY=3
```

The old:

```ini
VIDEO_BATCH_MAX_SHOTS=8
```

may remain in `.env`; it is no longer used as a silent full-batch truncation limit.

## Current EP02 recovery path

After installing this patch:

1. Restart Harness and hard-refresh the browser.
2. Ensure Shot 01 preview is explicitly approved.
3. Open Batch Video.
4. Existing 1-8 successful clips are detected.
5. Click `补齐缺失 6 镜（复用已成功）`.
6. Harness reuses approved Shot 01 and existing successful batch clips, and submits only Shots 09-14.

## Validation

- `node --check static/app.js` — passed.
- `pytest -q` — 153 passed.
