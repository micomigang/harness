# 2026-09-24 · Source / Production Continuity Separation Fix

## Problem reproduced from EP02 characters rev3

The character designer correctly produced localized French visual designs, but the Harness post-processing layer overwrote the generated production `continuity_lock` with the Asset Manifest/source lock. It also cloned that same value into `source_continuity_lock`, so the two fields became identical and source-culture wardrobe leaked back into downstream production continuity.

## Fix

- `source_continuity_lock` is now an archival/source-evidence field.
- `continuity_lock` is now the localized target-market production authority.
- `production_continuity_lock` is persisted as an explicit alias of the production lock for downstream clarity.
- An explicit `source_continuity_lock` returned by the designer is preserved instead of being overwritten by the Asset Manifest lock.
- An explicit generated production lock is preserved instead of being overwritten by source continuity.
- For localized assets, the Harness will **not** fall back to the source lock when a production lock is missing; it records a deterministic validation failure instead of contaminating production data.
- The legacy/general `continuity` field is aligned to production continuity once explicit visual localization exists, preventing a third stale-source leakage path.
- Added `continuity_localization_validation` to stage artifacts.
- Character/scene/prop prompts now explicitly require separation of source and production continuity.
- Director review is instructed to treat the separation and deterministic validation as authoritative.
- UI now shows `成片连续性锁` separately from `源素材连续性（仅归档）`.

## Regression

`pytest -q`: 96 passed.

Two dedicated tests cover:
1. Explicit French production continuity surviving Asset Manifest enforcement.
2. Missing production continuity failing validation rather than silently falling back to source styling.
