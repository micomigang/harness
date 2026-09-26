# Seedance preview/provider fix — 2026-09-26

This incremental patch targets the first preview/batch-video provider boundary.

## What changed

- Preserves both the primary-model and fallback-model Ark errors instead of hiding the primary failure behind the fallback failure.
- Falls back only for model-availability errors (for example `ModelNotOpen` / model 404), not for arbitrary 400/403 request errors.
- Sends `resolution`, `ratio`, `duration`, and `generate_audio` as Ark request-body parameters instead of pseudo command-line flags appended to the text prompt.
- Resolves reference images from each storyboard shot's own `asset_bindings.reference_images`; relationship combinations are preferred and local `/media/...` paths are mapped back to provider-reachable `remote_url` values from `reference_images`.
- Preserves multi-speaker dialogue lines (`lines`, `dialogue_lines`, or list-style `dialogue`) instead of flattening a shot to one speaker/text field.
- Adds audit fields to successful video results: `resolution`, `ratio`, `generate_audio`, `reference_image_count`, and `reference_keys`.

## Important: ModelNotOpen is an account permission issue

This patch cannot activate an Ark model for the account. If Ark returns `ModelNotOpen`, activate the exact `VIDEO_MODEL` for the same account/API key in Ark Console, or change `.env` to a model that account already has enabled.

Recommended current primary model:

```env
VIDEO_MODEL=doubao-seedance-2-5-260628
```

If `doubao-seedance-2-0-260128` is not enabled, either activate it too or disable fallback explicitly:

```env
VIDEO_FALLBACK_MODEL=
```

Restart Harness after changing `.env`.

## Verification

`tests/test_seedance_provider.py`: 5 passed.
