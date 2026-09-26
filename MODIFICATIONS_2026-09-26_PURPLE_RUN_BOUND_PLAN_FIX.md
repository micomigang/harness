# Purple run / bound-plan fix (2026-09-26)

This delta fixes two generic execution-path defects:

1. `validate_bound_plan()` now fingerprints the same selected visual-candidate state that Director Chat uses when it creates the bound plan. Previously the create/validate hashes could differ whenever selected candidates existed, causing a freshly confirmed plan to be rejected as stale.
2. The purple advance/generate button now reuses an already confirmed Director Chat plan when the composer is empty. It no longer asks Kimi to reconfirm the same plan immediately before `/run`. If the user types a new requirement, a fresh Director turn is still bound first.

The backend `/run` validation remains authoritative, so genuinely stale plans are still blocked.
