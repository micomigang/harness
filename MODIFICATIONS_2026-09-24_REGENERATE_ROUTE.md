# v12 feedback regeneration route hotfix · 2026-09-24

## Problem

The chat-first UI calls:

`POST /api/workspaces/{workspace_id}/stages/{stage}/regenerate`

but the uploaded v12 backend did not register that route, so clicking
“反馈并重生成当前节点” returned HTTP 404 / `Not Found`.

## Fix

- Restored `StageRegenerationRequest` (`feedback`, max 12000 chars).
- Restored `POST /api/workspaces/{workspace_id}/stages/{stage}/regenerate`.
- The endpoint now:
  1. validates workspace/stage;
  2. calls `prepare_stage_regeneration()` so feedback is bound by the director;
  3. preserves project memory/conversation and invalidates downstream state through the existing orchestrator logic;
  4. enqueues the replacement stage job;
  5. returns `director_reply`, previous revision and reset information to the existing UI.
- Added regression tests proving the route is registered and the feedback/job request is wired correctly.

## Validation

`pytest -q` -> 76 passed.

## How to test after restart

Keep the existing Mamie EP02 workspace. Do not rerun source analysis or screenplay.
Open 制作对话流 / 资产解析, keep or re-enter the Asset Manifest feedback, then click
“反馈并重生成资产解析” once. A new queued/running asset_manifest job should appear instead
of `Not Found`, and the resulting revision can then be checked for canonical
character / scene / prop taxonomy and manifest_validation.
