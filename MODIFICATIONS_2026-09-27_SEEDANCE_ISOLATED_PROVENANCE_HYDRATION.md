# Seedance isolated provenance hydration patch

Date: 2026-09-27

## Problem

After trusted character portraits were regenerated as Seedream 5.0 text-to-image outputs, archived to same-account TOS, selected, and reconciled into `reference_images rev9`, Shot 01 preflight still reported:

- `char_grandmere` missing same-account Ark original provenance
- `char_son` missing same-account Ark original provenance

The current `reference_images` artifact already carried the correct trusted character provenance. However, `SeedanceProvider._reference_items()` overlaid isolated character records with selected `reference_images` proxy candidates. Those proxy rows are binding mirrors and can have empty `provenance`, so they erased the trusted provenance before preflight.

## Fix

`app/providers/seedance.py` now distinguishes:

- isolated character / scene / prop references: hydrate from the current selected upstream `characters` / `scenes` / `props` production-truth candidate;
- relationship combinations: continue to hydrate from the current selected `reference_images` candidate.

This preserves the existing human-combination resolver while ensuring trusted character provenance, original URL, local original bytes, model, and TOS metadata reach Seedance preflight.

A provenance-less selected `reference_images` proxy can no longer override a newly trusted isolated character production truth.

## Tests

Added regression coverage for the exact failure mode: a stale/provenance-less `reference_images` proxy plus a trusted currently selected `characters` candidate must resolve and pass trusted portrait preflight using the upstream candidate.

Test results:

- `pytest -q tests/test_seedance_provider.py` -> `17 passed`
- `pytest -q` -> `149 passed`

## Scope

No storyboard, dialogue, sound, reference-image design, database, or UI content is modified by this patch.
