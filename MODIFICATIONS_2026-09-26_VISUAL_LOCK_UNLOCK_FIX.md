# Visual candidate lock/unlock fix

This delta fixes two generic visual-workbench problems.

1. A selected/locked candidate remains the downstream production truth, but it no longer hides a newly generated adjustment candidate in the overview. If a newer unselected candidate exists, the overview shows that candidate and offers “采用这个新候选”. The old selected image remains selected until the user explicitly switches.
2. Selected candidates can now be explicitly unlocked without deleting the image. Unlocking only clears the adoption pointer; the image remains a normal candidate. Relevant downstream artifacts are marked stale because their visual authority changed.

Important semantics:
- “selected / locked” controls downstream adoption only. It does not block Seedream candidate generation.
- Generating from a locked image keeps the locked image as production truth and creates a new pending candidate for comparison.
- Unlocking does not call Seedream and does not delete the candidate.
