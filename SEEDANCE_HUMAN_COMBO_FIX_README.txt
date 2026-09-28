Seedance human-combination reference fix
=========================================

Scope
-----
- Keep the existing provenance / TOS / preflight implementation.
- Human-containing relationship combinations are NOT sent directly to Seedance.
- Such combinations are expanded to the current isolated production truths.
- Non-human combinations (scene + prop, etc.) may still be submitted directly.
- Duplicate references are removed after expansion.
- Seedance reference-count limits are checked after expansion.
- Missing isolated production truth fails before provider task creation.

Files to overwrite
------------------
app/providers/seedance.py
tests/test_seedance_provider.py

Validation performed
--------------------
python -m pytest tests/test_seedance_provider.py -q
Result: 16 passed

Install
-------
Extract this ZIP directly over F:\oiioii-harness

Then run:
  cd F:\oiioii-harness
  pytest tests/test_seedance_provider.py -q

After tests pass, fully restart Harness and rerun Shot 01 preview.
