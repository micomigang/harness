# v12 Visual Localization / Prompt Compiler delta

This delta adds an explicit target-market visual-localization layer between source evidence and visual rendering.

## What changed

- Character / scene / prop agents now separate `source_visual_traits` from `localized_visual_design`.
- Visual agents now return `generation_prompt_en`, an English provider-facing visual prompt, while story/bible prose may stay in the localized story language.
- Manifest `continuity_lock` is preserved as `source_continuity_lock` in downstream asset bibles so source evidence remains traceable.
- Source-cultural visual details are no longer treated as automatically immutable production styling.
- For a France target market, the renderer explicitly translates culturally source-specific clothing/interiors/props into plausible French equivalents while preserving narrative function, identity, class contrast, recognisable colour/silhouette and story-critical continuity.
- Seedream candidate and reference-image prompts now use the target-market localization contract. The generated prompt is predominantly English; target-language dialogue or visible text remains separate.
- The visual candidate workbench shows source anchors, localized production design, localization notes and the English generation prompt when available.
- Selecting a candidate is presented as the downstream visual lock; source evidence is not itself the final production visual lock.

## Important behavior

For the current EP02 grandmother, a source red floral padded rural coat should not be copied literally just because it existed in the source. The visual provider is instructed to preserve the rural/modest/warm/red-burgundy recognition function while adapting the garment toward plausible French countryside styling.

## Compatibility

No database migration is required by this delta. Existing characters/scenes/props remain readable. Old artifacts without explicit localization fields still receive the provider-level target-market prompt compiler fallback; regenerating the design stage once is recommended so the localized design becomes visible and auditable in the Harness UI.
