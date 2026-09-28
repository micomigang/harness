# Seedance human-combination resolver patch

Date: 2026-09-27

## Scope

Only the Seedance runtime reference resolver and its tests are changed. No upstream creative artifacts or production content are regenerated.

## Behavior

- Relationship combinations containing any `char_*` are never submitted directly to Seedance.
- Human combinations expand through `source_id` / `source_ids` to the current isolated production truths.
- Canonical `combo__...` parsing is only a structural fallback for older records; it does not grant portrait provenance/trust.
- Current `selected=true` reference-image candidates continue to override stale candidate IDs by `canonical_key`.
- Isolated character references are ordered before scene/prop references; pure scene/prop combinations are kept last as optional context.
- References are de-duplicated by canonical key and final transport URL.
- The Seedance image-count limit is checked after expansion/de-duplication; oversized requests stop instead of being truncated.
- Preflight defensively rejects any human-containing combination that somehow reaches it directly.
- Shots with no explicit reference bindings no longer attach arbitrary global references, avoiding a preflight bypass through an empty key list.

## Validation

`pytest -q tests/test_seedance_provider.py` -> 16 passed

`pytest -q` -> 148 passed
