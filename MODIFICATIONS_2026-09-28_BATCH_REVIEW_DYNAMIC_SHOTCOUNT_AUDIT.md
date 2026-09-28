# 2026-09-28 Batch review dynamic shot-count audit

## Important invariant

Batch size is never a production constant. The authoritative shot set is the current
approved storyboard artifact (`storyboard.content.shots`), whose count and shot indices
come from the upstream story/sample-video analysis and storyboard approval process.

For EP02 that set currently happens to contain 14 shots. Another episode may contain
6, 9, 18, 27, or any other validated count without code changes.

## Runtime behavior

- Seedance batch generation loads the current storyboard `shots` list and targets the
  whole list unless the user/director explicitly requests a partial test run.
- `requested`, `completed`, `storyboard_shots`, missing indices and coverage are derived
  from that current storyboard at runtime.
- The batch UI reads the current storyboard shot indices and renders coverage dynamically.
- Batch review receives the complete `batch_video.items` list rather than the generic
  ten-item preview truncation.
- Deterministic batch validation derives `expected_shot_indices` from the storyboard and
  subordinates LLM structural claims to that validation.
- Reviewer false-negative suppression is now dynamic: no `14`, `11-14`, or `Shot 14`
  runtime assumption is used to decide whether a structural/audit complaint is false.
- Sound audit checks (`ducking.active`, `negative_audio`) are keyed dynamically by the
  shot indices mentioned in the review and by the current storyboard indices.

## EP02

EP02 currently resolves to 14 shots because its approved storyboard does. The value 14
appears in EP02-specific regression fixtures only; those tests prove the known 14-shot
case and do not define production behavior.

## Validation

- `node --check static/app.js` — passed.
- `pytest -q` — 157 passed.
- Added a non-14 regression test using a 6-shot storyboard to prove dynamic batch
  coverage/reviewer guarding.
