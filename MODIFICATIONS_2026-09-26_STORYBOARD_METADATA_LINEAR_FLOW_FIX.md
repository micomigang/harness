# Storyboard metadata reconcile + linear-flow fix (2026-09-26)

This patch fixes three generic storyboard-regeneration failure modes exposed by metadata-only continuity repairs.

1. **Reference metadata hydration is deterministic.**
   - The storyboard model decides which canonical reference belongs to a shot.
   - Harness now refreshes `candidate_id`, `url`, and `source_kind` from the approved `reference_images` production-truth artifact by canonical key.
   - A metadata-only storyboard repair no longer depends on the LLM copying volatile media URLs verbatim.

2. **Storyboard expected shot count no longer falls back to a stale workspace default during regeneration.**
   - Explicit bound wording such as `14 镜`, `14 镜头`, `14 分镜`, or `14 shots` is recognized.
   - If the current instruction does not change the count, regeneration/revalidation preserves the previous storyboard shot count before consulting the workspace default.
   - This prevents a valid 14-shot board from being validated against an old default such as 8.

3. **Current-stage review does not require downstream QA evidence.**
   - Regenerating storyboard intentionally invalidates downstream dialogue/sound/review artifacts in the linear workflow.
   - `continuity_qa` closure is therefore a later-stage action, not a storyboard acceptance criterion.
   - Reviewer-only demands for downstream QA proof are suppressed when deterministic storyboard validation passes.

Reviewer compact storyboard references now include `url`, so a reviewer can directly verify the hydrated binding contract.

Recommended recovery for an existing storyboard revision: use **重新校验当前分镜（不重生成）**. This hydrates missing reference metadata and recomputes validation without rewriting creative shot content.
