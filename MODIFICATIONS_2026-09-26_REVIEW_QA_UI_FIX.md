# 2026-09-26 Consistency QA review UI fix

## What changed

- Added a dedicated `review` workbench UI instead of rendering consistency QA through the generic artifact list.
- Review summary now shows Fail / Warn / Pass counts and makes blocking status immediately visible.
- `blocking_failures` are surfaced first with remediation/owner when available.
- Full QA checks are rendered as compact cards with evidence, owner, remediation, and status.
- The verbose director execution context is collapsed by default so it no longer creates a tall mostly-empty artifact panel.
- The review artifact card is allowed to grow naturally; the old generic 210px nested scroller no longer controls this stage.

## Review-context reliability

- Full `review.checks` and `blocking_failures` are supplied to the director reviewer instead of being clipped by generic list preview caps.
- Full `sound_plan.items` are preserved in compact director/upstream context so continuity QA can reason over the complete shot sequence.
- `sound_validation` is carried through director preview/upstream context.
- Added review guidance so a structurally correct QA report may recommend `adjust_next_stage` when the actual problem is an upstream blocker; the review stage itself should not be regenerated just because it found failures.

## Tests

- JavaScript syntax check passes.
- Python compilation passes.
- `tests/test_openai_compat.py`: 22 passed.
