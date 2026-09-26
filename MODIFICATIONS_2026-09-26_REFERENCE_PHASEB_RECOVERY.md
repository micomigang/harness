# Reference Images Phase-B recovery fix

This delta fixes the rev4/rev5 failure where Phase A survived but every relationship combination disappeared and `reference_validation` incorrectly reported `combination_expected=0`.

Changes:
- Treats both explicit `char_x + scene_y + prop_z` relations and explicit canonical `combo__char_x__scene_y__prop_z` IDs as structured combination intent.
- Does not infer combinations from generic prose, category counts, comma-separated lists, or director summaries.
- Exposes durable `reference_images` workbench candidate history to the image provider.
- Reuses exact historical combination URLs by canonical combination key and refreshes `input_candidate_ids` from current Phase-A production truth.
- Calls Seedream only for a planned combination that has no reusable historical/current URL.
- Binding-only reconcile can restore missing combination items from historical reference candidates without rendering.
- Persists the recovered combination plan so validation cannot collapse back to `0/0` after a broken revision.

No EP02 asset names or 14/7/21 counts are hard-coded into runtime logic.
