# Reference recovered-combination preview fix

Fixes the state where reference_images is structurally complete (for example 14/14 isolated and 7/7 combinations, validation pass) but recovered Phase B cards still show “暂无候选”.

Changes:
- Metadata-only reference binding reconcile now hydrates a current-revision workbench candidate row for each combination recovered from historical reference candidate media.
- Exact restored media URL is preferred, then selected historical media, then newest historical media.
- No Seedream call is made by reconcile.
- UI falls back to the artifact item's own URL when a current-revision candidate row is temporarily absent, so recovered combinations remain visible and inspectable.
- Reference detail view can use that artifact image as the base for later adjustments.

This is generic behavior; no episode-specific asset names or counts are hard-coded.
