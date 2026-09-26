# Seedream → Seedance image provenance

The Ark portrait input rule applies to original, same-account Seedream 5.0 pro/lite **text-to-image** outputs for 30 days. A newly downloaded or re-encoded local image is not, by itself, proof of Ark trust. The Seedream output URL normally expires after 24 hours. See the [Ark portrait guide](https://docs.volcengine.com/docs/ark/seedance-portrait-asset-guide?lang=zh) and [image API](https://docs.volcengine.com/docs/ark/image-generation-api?lang=en).

## 1. Generate and record

Every new Seedream image records model, generation mode, Ark account ID, generation time, original URL and its expiry, detected image format, SHA-256, byte count, and whether the saved bytes are the untouched provider output. Files are named by their detected format; a JPEG is saved as `.jpg`, not `.png`. Candidate records store the same provenance. Older images without trustworthy generation records remain **unknown**, even if a filename or prompt suggests they came from Seedream.

Person-containing combinations are generated directly from the locked character, scene, and prop text specifications. Targeted image-to-image edits remain available as visual candidates, but cannot pass the trusted portrait input gate simply because their output model is Seedream 5.0.

## 2. Keep originals in TOS

The app saves the response bytes unchanged. For review cycles longer than the original URL's 24-hour lifetime, configure an Ark-account-matched TOS bucket in `.env`:

```dotenv
ARK_ACCOUNT_ID=<Ark account ID>
TOS_ACCOUNT_ID=<same Ark account ID>
TOS_ACCESS_KEY=<TOS access key>
TOS_SECRET_KEY=<TOS secret key>
TOS_ENDPOINT=tos-cn-beijing.volces.com
TOS_REGION=cn-beijing
TOS_BUCKET=<private bucket name>
```

Install the optional SDK with `pip install -e ".[tos]"`. Once configured, new original images are uploaded unchanged to a deterministic TOS object key and the object size is checked. The app stores a `tos://` object URI, not a long-lived public URL. Before Seedance submission it creates a short-lived signed GET URL and checks that the URL responds from the app environment; Ark may still reject it. Configure the TOS credentials under the same account as both Ark API keys. If TOS is unconfigured or upload fails, the candidate remains available for review, but a later video run stops once the original signed URL expires.

To archive an existing candidate that already has verified original bytes, run `python scripts/archive_original_candidate.py <candidate-id>` after configuring TOS. The command refuses candidates with missing provenance or altered bytes; it never upgrades an old re-encoded image to “trusted.”

## 3. Preflight every bound image

Before creating a Seedance task, the app resolves the current selected candidate for each storyboard reference key. It checks the actual transport URL for every image. For images containing a person it also requires the same account, a qualifying text-to-image model and generation mode, a generation time within the 30-day trust window, and matching original file format and SHA-256. It uses the original Ark URL while valid, otherwise a signed TOS URL for the unchanged archived object. An expired original URL never silently falls back to local Base64 for a portrait. A failed check names the offending canonical asset and stops before a billable video task is created.

For legacy human images with unknown or altered provenance, generate a new text-to-image candidate or use a supported Ark preset/authorized-person asset with its corresponding authorization metadata. Merely uploading an old local file to TOS does not establish trusted origin.

## 4. Review, then bind

Generation creates an unselected candidate. The user selects it explicitly in the asset workbench. Selection changes the image used at video runtime by canonical key, preserves locked storyboard/dialogue/sound content revisions, marks the old preview stale, and resets preview approval to pending. A new preview must pass its shot-index and output-field checks before `batch_video` can be approved. TOS retention only solves URL lifespan; Ark may still reject an output under other safety rules.
