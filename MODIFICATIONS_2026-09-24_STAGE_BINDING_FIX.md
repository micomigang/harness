# Stage regeneration binding fix — 2026-09-24

## Root cause
After a generation job finished, the front-end cleared `chatStageOverride`. The Director Chat then fell through to `next_actions[0]`. For example, after `characters` finished and the next suggested action was `scenes`, the yellow regenerate button was implicitly bound to `scenes`. Because no ready `scenes` artifact existed yet, the button became disabled / inert even though the textarea contained feedback intended for the ready `characters` revision.

The backend `POST /api/workspaces/{workspace_id}/stages/{stage}/regenerate` route was present; the failure was front-end stage targeting.

## Changes
- Keep the composer attached to the most recently completed ready artifact after a job succeeds.
- Recover the latest reviewable stage from persisted succeeded jobs after page refresh/restart.
- Bind the yellow regenerate button to an explicit `data-stage` when rendered, rather than re-inferring the stage at click time.
- When the user explicitly advances to the next node, rebind Director Chat to that next stage before building the director plan.
- Allow `chatStageGuidance()` to accept an explicit stage override so next-stage planning cannot accidentally write guidance back to the previous completed node.
- Show `当前评审节点` for the completed artifact being reviewed.
- Bump static asset version to `20260924-v12-stage-binding-fix` to avoid stale browser JS cache.

## Expected EP02 behavior
After `characters rev1` is ready and `scenes` is the next action:
- Director Chat remains bound to `characters`.
- Yellow button reads `反馈并重生成角色` and sends to `/stages/characters/regenerate`.
- Purple `生成场景` remains available and, when clicked, explicitly switches planning/execution to `scenes`.
