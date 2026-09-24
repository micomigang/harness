# Asset Manifest identity/schema delta — 2026-09-24

This delta is based on the current v12 line after the earlier Asset Manifest reconcile/director changes and the regenerate-route fix.

## What changed

1. Split screenplay requirement binding from production asset identity.
   - `script.asset_requirements` IDs such as `CHAR_GRANDMERE`, `LOC_FOYER`, `PROP_BAG` are now stored in `items[].source_requirement_keys`.
   - `items[].canonical_key` is the stable production/series identity and is no longer overwritten by screenplay IDs.
   - Current generated normalized keys (`char_*`, `scene_*`, `prop_*`) win over legacy aliases when semantically matched.

2. Extra legacy aliases can migrate to current normalized identities.
   - A current generated `scene_couloir_rdc` can replace legacy `set_ground_floor_corridor` for the same semantic scene without creating a duplicate.

3. Asset type metadata is explicit.
   - character: `appearance`, `costume`
   - scene: `key_set_elements`
   - prop: `physical_description`, `used_in_scenes`
   - Existing evidence-backed generation requirements are used as safe fallback metadata when the provider omits a typed field.

4. `continuity_lock` has one canonical encoding.
   - Always an array of immutable rule strings.
   - Legacy boolean `true/false` is interpreted as locked/unlocked intent only; it is not emitted as the schema type.

5. `manifest_validation` is expanded.
   - `required_baseline_coverage`
   - `missing_items`
   - `duplicate_canonical_semantic_check`
   - `illegal_asset_type_check`
   - `continuity_lock_check`
   - `typed_metadata_check`
   - `asset_type_counts`
   - Existing compatibility fields remain available.

6. Director/reviewer prompts now treat deterministic Harness validation as authoritative for schema coverage and do not demand screenplay IDs as manifest canonical keys or boolean lock encoding.

7. UI: object-valued artifact previews are rendered safely instead of showing `[object Object]`. Asset Manifest cards show validation status and screenplay binding keys.

## Regression status

`pytest -q` => 79 passed.

EP02 migration simulation using the uploaded real `script rev1` + legacy `asset_manifest rev2`, with the current normalized rev3-style keys, resolves to:

- character: 5
- scene: 5
- prop: 4
- required screenplay bindings: 11/11
- missing required: []
- duplicate canonical/semantic: []
- illegal types: []
- continuity lock violations: []
- validation status: pass

The item count remains evidence-derived; 14 is not a quota.
