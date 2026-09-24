# 2026-09-24 · Visual Workbench UI usability delta

UI-only delta. No backend/orchestrator/continuity/regeneration logic changes.

## Changes

- Character / scene / prop artifact detail cards now use a long-form page canvas instead of the old 210px nested artifact-content scroller.
- Visual workbench detail cards expand with content; normal page scrolling is used.
- Candidate feedback textarea minimum height increased to 118px (still manually resizable up to 360px).
- Current Director review result is surfaced in the artifact header:
  - `总管已通过` for current-revision `proceed`
  - `总管待修` for `regenerate_current` / `wait_for_user`
- The pass badge tooltip clarifies that a `pending` item in the right-side manual approval area is a human approval state, not a Director rejection.
- Frontend cache version bumped to `20260924-v12-visual-workbench-long-canvas`.

## Current EP02 interpretation

Characters revision 4 already has Director review `proceed / pass`. There is no new backend conflict to repair in this delta. Any `assets_approved = pending` state is a separate manual approval gate.

## Verification

- `python -m pytest -q` -> 97 passed
- `node --check static/app.js` -> passed
