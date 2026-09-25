from __future__ import annotations

import base64
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any

import httpx

from .base import WorkflowProvider
from app.guidance import sanitize_parameter_overrides


class SeedreamError(RuntimeError):
    pass


class SeedreamProvider(WorkflowProvider):
    """Volcengine Ark Seedream reference-image adapter.

    Besides the normal ``reference_images`` stage this provider exposes a small
    targeted candidate API used by the Harness asset workbench.  Candidates are
    intentionally user-triggered so a text/spec regeneration never silently
    spends image-generation budget.
    """

    name = "seedream"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        size: str = "2K",
        max_assets: int = 6,
        output_dir: Path,
        client: httpx.Client | None = None,
    ):
        if not api_key or not model:
            raise ValueError("IMAGE_API_KEY and IMAGE_MODEL are required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.size = size
        self.max_assets = max(1, max_assets)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client = client

    def generate(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        if stage != "reference_images":
            raise NotImplementedError(f"Seedream does not handle stage: {stage}")
        workspace_id = str(context["workspace"]["id"])
        directive = context.get("execution_directive") or {}
        params = sanitize_parameter_overrides(stage, directive.get("parameter_overrides"))
        max_assets = int(params.get("max_assets", self.max_assets))
        explicit_max_assets = "max_assets" in params
        prompt_addendum = str(directive.get("prompt_addendum") or "").strip()
        localization_context = self._visual_localization_context(context)
        library_lookup: dict[str, dict[str, Any]] = {}
        for library in context.get("asset_library_context", []) or []:
            for library_item in library.get("items", []) or []:
                if isinstance(library_item, dict) and library_item.get("id"):
                    library_lookup[str(library_item["id"])] = library_item

        selected_lookup: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in context.get("asset_candidates", []) or []:
            if not isinstance(candidate, dict) or not candidate.get("selected"):
                continue
            stage_name = str(candidate.get("stage") or "")
            key = str(candidate.get("canonical_key") or "")
            if stage_name in {"characters", "scenes", "props"} and key:
                selected_lookup[(stage_name, key)] = candidate

        sources = self._asset_sources(context)
        anchored_sources: list[tuple[str, dict[str, Any]]] = []
        for stage_name, item in sources:
            enriched = dict(item)
            library_id = str(item.get("library_asset_id") or "")
            if library_id and library_id in library_lookup:
                enriched["_library_asset"] = library_lookup[library_id]
            selected = selected_lookup.get((stage_name, str(item.get("canonical_key") or "")))
            if selected:
                enriched["_selected_candidate"] = selected
            anchored_sources.append((stage_name, enriched))
        if not anchored_sources:
            raise SeedreamError("No character, scene or prop assets available")

        # Combination intent must come from the user-bound stage instruction, not
        # from the director's paraphrase.  The previous implementation scanned the
        # whole director plan and accidentally interpreted category summaries such
        # as "all characters" / "all scenes" as relationship combinations.
        combination_sources = [
            str(context.get("user_instruction") or "").strip(),
            str(directive.get("effective_instruction") or "").strip(),
        ]
        combinations: list[list[str]] = []
        seen_combinations: set[tuple[str, ...]] = set()
        for combination_text in combination_sources:
            for group in self._extract_reference_combinations(combination_text):
                signature = tuple(group)
                if signature in seen_combinations:
                    continue
                seen_combinations.add(signature)
                combinations.append(group)
        # Explicit isolated+combination requirements are user authorization for the
        # necessary calls. Do not silently drop props/the seventh combination merely
        # because IMAGE_MAX_ASSETS defaults below the explicitly bound plan. The
        # Harness safety ceiling remains 12 fresh image calls in one reference run.
        unselected_required = sum(
            1 for _, item in anchored_sources
            if not item.get("_selected_candidate") and not item.get("_library_asset")
        )
        generation_budget = (
            max_assets
            if explicit_max_assets
            else max(max_assets, min(12, len(combinations) + unselected_required))
        )
        used_generation_budget = 0

        items: list[dict[str, Any]] = []
        isolated_by_key: dict[str, dict[str, Any]] = {}
        missing_isolated: list[str] = []
        client, owns_client = self._get_client()
        try:
            for stage_name, item in anchored_sources:
                canonical_key = str(item.get("canonical_key") or item.get("id") or "").strip()
                if not canonical_key:
                    continue
                canonical_kind = self._canonical_source_kind(stage_name)
                selected = item.get("_selected_candidate") if isinstance(item.get("_selected_candidate"), dict) else {}
                if selected and selected.get("url"):
                    ref_item = {
                        "source_kind": canonical_kind,
                        "source_id": canonical_key,
                        "source_ids": [canonical_key],
                        "name": item.get("name") or item.get("id") or canonical_key,
                        "library_asset_id": item.get("library_asset_id"),
                        "canonical_key": canonical_key,
                        "reference_key": canonical_key,
                        "model": selected.get("model") or self.model,
                        "remote_url": selected.get("remote_url") or "",
                        "url": selected.get("url") or "",
                        "local_path": selected.get("local_path") or "",
                        "status": "selected_candidate",
                        "origin": "upstream_selected_candidate",
                        "candidate_id": selected.get("id"),
                        "prompt": str(selected.get("prompt") or item.get("generation_prompt_en") or ""),
                    }
                    items.append(ref_item)
                    isolated_by_key[canonical_key] = ref_item
                    continue
                library_asset = item.get("_library_asset") if isinstance(item.get("_library_asset"), dict) else {}
                decision = str(item.get("reuse_decision") or item.get("decision") or "").upper()
                ref = (library_asset.get("content") or {}).get("reference_image") if library_asset else {}
                if decision == "REUSE" and library_asset and (library_asset.get("preview_url") or (ref or {}).get("url")):
                    ref_item = {
                        "source_kind": canonical_kind,
                        "source_id": canonical_key,
                        "source_ids": [canonical_key],
                        "name": item.get("name") or item.get("id") or canonical_key,
                        "library_asset_id": library_asset.get("id"),
                        "canonical_key": canonical_key,
                        "reference_key": canonical_key,
                        "model": "series-library-reuse",
                        "remote_url": (ref or {}).get("remote_url") or "",
                        "url": library_asset.get("preview_url") or (ref or {}).get("url") or "",
                        "local_path": (ref or {}).get("local_path") or "",
                        "status": "reused",
                        "prompt": str(item.get("generation_prompt_en") or ""),
                    }
                    items.append(ref_item)
                    isolated_by_key[canonical_key] = ref_item
                    continue
                if used_generation_budget >= generation_budget:
                    missing_isolated.append(canonical_key)
                    continue
                generated = self._generate_one(
                    client, workspace_id, stage_name, item, prompt_addendum,
                    localization_context=localization_context,
                )
                generated.update({
                    "source_kind": canonical_kind,
                    "source_id": canonical_key,
                    "source_ids": [canonical_key],
                    "canonical_key": canonical_key,
                    "reference_key": canonical_key,
                    "status": "candidate",
                })
                items.append(generated)
                isolated_by_key[canonical_key] = generated
                used_generation_budget += 1

            missing_combinations: list[dict[str, Any]] = []
            generated_combinations = 0
            for combo_index, source_ids in enumerate(combinations, start=1):
                missing = [key for key in source_ids if key not in isolated_by_key]
                if missing:
                    missing_combinations.append({"source_ids": source_ids, "missing_inputs": missing, "reason": "missing isolated reference"})
                    continue
                if used_generation_budget >= generation_budget:
                    missing_combinations.append({"source_ids": source_ids, "missing_inputs": [], "reason": "generation budget reached"})
                    continue
                reference_values: list[str] = []
                input_candidate_ids: list[str] = []
                for key in source_ids:
                    ref_item = isolated_by_key[key]
                    value = self._reference_image_value(
                        str(ref_item.get("remote_url") or ref_item.get("url") or ""),
                        str(ref_item.get("local_path") or ""),
                    )
                    if value:
                        reference_values.append(value)
                    if ref_item.get("candidate_id"):
                        input_candidate_ids.append(str(ref_item.get("candidate_id")))
                if len(reference_values) < 2:
                    missing_combinations.append({"source_ids": source_ids, "missing_inputs": [], "reason": "fewer than two usable reference images"})
                    continue
                reference_key = "combo__" + "__".join(source_ids)
                prompt = self._combination_prompt(source_ids, prompt_addendum)
                combo = self._request_and_download(
                    client,
                    workspace_id=workspace_id,
                    source_kind="combination",
                    item={"id": reference_key, "canonical_key": reference_key, "name": " + ".join(source_ids)},
                    prompt=prompt,
                    output_subdir="reference_images",
                    reference_values=reference_values[:10],
                )
                combo.update({
                    "source_kind": "combination",
                    "source_id": list(source_ids),
                    "source_ids": list(source_ids),
                    "canonical_key": reference_key,
                    "reference_key": reference_key,
                    "name": " + ".join(source_ids),
                    "status": "candidate",
                    "input_candidate_ids": input_candidate_ids,
                    "prompt": prompt,
                })
                items.append(combo)
                generated_combinations += 1
                used_generation_budget += 1
        finally:
            if owns_client:
                client.close()

        selected_count = sum(
            1 for item in items
            if item.get("source_kind") in {"character", "scene", "prop"}
            and (item.get("candidate_id") or item.get("status") in {"selected_candidate", "upstream_adopted"})
        )
        isolated_items = [item for item in items if item.get("source_kind") in {"character", "scene", "prop"}]
        combination_items = [item for item in items if item.get("source_kind") == "combination"]
        isolated_count = len(isolated_items)
        combination_count = len(combination_items)
        allowed_source_kinds = {"character", "scene", "prop", "combination"}
        invalid_source_kinds = sorted({
            str(item.get("source_kind") or "")
            for item in items
            if str(item.get("source_kind") or "") not in allowed_source_kinds
        })
        planned_signatures = [tuple(group) for group in combinations]
        actual_signatures = [tuple(item.get("source_ids") or item.get("source_id") or []) for item in combination_items]
        missing_signature_set = [list(sig) for sig in planned_signatures if sig not in actual_signatures]
        unexpected_signature_set = [list(sig) for sig in actual_signatures if sig not in planned_signatures]
        reference_keys = [str(item.get("reference_key") or item.get("canonical_key") or "") for item in items]
        duplicate_reference_keys = sorted({key for key in reference_keys if key and reference_keys.count(key) > 1})
        validation_status = (
            "pass"
            if not missing_isolated
            and not missing_combinations
            and isolated_count == len(anchored_sources)
            and combination_count == len(combinations)
            and not missing_signature_set
            and not unexpected_signature_set
            and not invalid_source_kinds
            and not duplicate_reference_keys
            else "fail"
        )
        return {
            "items": items,
            "requested": len(anchored_sources) + len(combinations),
            "completed": len(items),
            "model": self.model,
            "status": "succeeded" if not missing_isolated and not missing_combinations else "partial",
            "selection_coverage": {
                "upstream_selected_count": selected_count,
                "upstream_selected_keys": sorted(
                    str(item.get("canonical_key") or "")
                    for item in isolated_items
                    if item.get("candidate_id") or item.get("status") in {"selected_candidate", "upstream_adopted"}
                ),
                "isolated_completed": isolated_count,
                "isolated_expected": len(anchored_sources),
                "combination_completed": combination_count,
                "combination_expected": len(combinations),
            },
            "reference_plan": {
                "isolated_keys": [str(item.get("canonical_key") or item.get("id") or "") for _, item in anchored_sources],
                "combinations": combinations,
                "generation_budget": generation_budget,
                "generation_calls_used": used_generation_budget,
            },
            "missing_isolated": missing_isolated,
            "missing_combinations": missing_combinations,
            "reference_validation": {
                "status": validation_status,
                "allowed_source_kinds": sorted(allowed_source_kinds),
                "isolated_expected": len(anchored_sources),
                "isolated_completed": isolated_count,
                "combination_expected": len(combinations),
                "combination_completed": combination_count,
                "planned_combinations": [list(group) for group in combinations],
                "actual_combinations": [list(group) for group in actual_signatures],
                "missing_planned_combinations": missing_signature_set,
                "unexpected_combinations": unexpected_signature_set,
                "invalid_source_kinds": invalid_source_kinds,
                "duplicate_reference_keys": duplicate_reference_keys,
            },
            "note": "上游工作台中已采用的视觉候选直接作为 isolated reference truth；只有缺失 isolated reference 或用户明确列出的 relationship combination 才调用 Seedream。主题资产库是否为空不影响已采用候选的有效性。",
        }

    @staticmethod
    def _canonical_source_kind(stage_name: str) -> str:
        return {"characters": "character", "scenes": "scene", "props": "prop"}.get(stage_name, stage_name)

    @staticmethod
    def _extract_reference_combinations(text: str) -> list[list[str]]:
        """Extract only *explicit* relationship combinations from user wording.

        A combination is considered explicit when canonical asset keys are joined
        with a plus sign on the same line, e.g. ``char_a + scene_b + prop_c``.
        Merely mentioning several asset keys in a category summary must never create
        a combination.  This keeps the parser generic while preventing director
        paraphrases from manufacturing all-character/all-scene/all-prop composites.
        """
        combinations: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        key_pattern = re.compile(r"\b(?:char|scene|prop)_[A-Za-z0-9_]+\b")
        plus_pattern = re.compile(r"[+＋]")
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line or not plus_pattern.search(line):
                continue
            keys: list[str] = []
            for match in key_pattern.findall(line):
                if match not in keys:
                    keys.append(match)
            if len(keys) < 2:
                continue
            # Require at least one plus-separated relation between the extracted
            # keys.  This intentionally ignores prose lists separated by commas,
            # slashes or parentheses.
            fragments = [part.strip() for part in plus_pattern.split(line)]
            joined_keys: list[str] = []
            for fragment in fragments:
                found = key_pattern.findall(fragment)
                if found:
                    joined_keys.append(found[-1])
            if len(joined_keys) < 2:
                continue
            normalized: list[str] = []
            for key in joined_keys:
                if key not in normalized:
                    normalized.append(key)
            if len(normalized) < 2:
                continue
            signature = tuple(normalized[:10])
            if signature in seen:
                continue
            seen.add(signature)
            combinations.append(list(signature))
        return combinations[:12]

    @staticmethod
    def _combination_prompt(source_ids: list[str], prompt_addendum: str = "") -> str:
        prompt = (
            "Photorealistic cinematic storyboard-ready reference composition for a localized French screen drama. "
            "Use ALL supplied reference images as authoritative visual identity inputs. Preserve the exact face/body identity, wardrobe, prop material/shape and scene architecture shown in those references. "
            "Do not redesign or reinterpret any canonical asset. Compose them naturally into one coherent production frame with plausible scale, contact, perspective and lighting. "
            "No text, no captions, no watermark. Bound canonical assets: " + ", ".join(source_ids) + "."
        )
        if prompt_addendum:
            prompt += " Art director requirements: " + prompt_addendum
        return prompt

    def generate_candidate(
        self,
        *,
        workspace_id: str,
        source_kind: str,
        item: dict[str, Any],
        feedback: str = "",
        reference_url: str = "",
        reference_local_path: str = "",
        localization_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate exactly one visual candidate for one asset.

        If a prior candidate is supplied it is sent back to Seedream as an image
        reference, turning a free-form reroll into a targeted edit that preserves
        unspecified identity/layout/design details.
        """
        if source_kind not in {"characters", "scenes", "props", "reference_images"}:
            raise SeedreamError(f"Unsupported candidate source kind: {source_kind}")
        if source_kind == "reference_images":
            prompt = str(item.get("prompt") or "").strip() or (
                "Photorealistic continuity-preserving production reference. Preserve all canonical identities, materials, architecture, proportions and composition unless explicitly changed."
            )
        else:
            prompt = self._prompt(source_kind, item, localization_context=localization_context)
        feedback = str(feedback or "").strip()
        has_reference = bool(reference_url or reference_local_path)
        if feedback:
            if has_reference:
                prompt += (
                    "\nTargeted edit of the supplied reference image. Preserve identity, composition, "
                    "layout and all unspecified design details. Change only what the art director asks: "
                    + feedback
                )
            else:
                prompt += "\nArt director targeted adjustment: " + feedback
        elif has_reference:
            prompt += "\nCreate a close continuity-preserving variation of the supplied reference image."

        client, owns_client = self._get_client()
        try:
            result = self._request_and_download(
                client,
                workspace_id=workspace_id,
                source_kind=source_kind,
                item=item,
                prompt=prompt,
                output_subdir="asset_candidates",
                reference_url=reference_url,
                reference_local_path=reference_local_path,
            )
        finally:
            if owns_client:
                client.close()
        result["feedback"] = feedback
        result["prompt"] = prompt
        result["reference_used"] = has_reference
        return result

    @staticmethod
    def _asset_sources(context: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        result: list[tuple[str, dict[str, Any]]] = []
        for artifact in context.get("artifacts", []):
            kind = str(artifact.get("kind", ""))
            if kind not in {"characters", "scenes", "props"}:
                continue
            for item in artifact.get("content", {}).get("items", []):
                result.append((kind, item))
        return result

    def _generate_one(
        self,
        client: httpx.Client,
        workspace_id: str,
        source_kind: str,
        item: dict[str, Any],
        prompt_addendum: str = "",
        *,
        localization_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = self._prompt(source_kind, item, localization_context=localization_context)
        if prompt_addendum:
            prompt += "\nArt director requirements: " + prompt_addendum
        return self._request_and_download(
            client,
            workspace_id=workspace_id,
            source_kind=source_kind,
            item=item,
            prompt=prompt,
            output_subdir="reference_images",
        )

    def _request_and_download(
        self,
        client: httpx.Client,
        *,
        workspace_id: str,
        source_kind: str,
        item: dict[str, Any],
        prompt: str,
        output_subdir: str,
        reference_url: str = "",
        reference_local_path: str = "",
        reference_values: list[str] | None = None,
    ) -> dict[str, Any]:
        # Each Harness candidate/reference image is intentionally generated by
        # one independent request.  Do not send ``sequential_image_generation``:
        # that option is model-specific and several Seedream/Ark image models
        # reject the request with InvalidParameter even when set to "disabled".
        # Batch generation is orchestrated by Harness at the asset level instead.
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "stream": False,
            "response_format": "url",
            "watermark": False,
        }
        references = [str(value) for value in (reference_values or []) if str(value).strip()]
        if not references:
            reference = self._reference_image_value(reference_url, reference_local_path)
            if reference:
                references = [reference]
        if references:
            payload["image"] = references[0] if len(references) == 1 else references[:10]
        response = client.post(
            f"{self.base_url}/images/generations",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.is_error:
            raise SeedreamError(
                f"Seedream generation failed ({response.status_code}): {response.text[:800]}"
            )
        data = response.json().get("data") or []
        remote_url = str(data[0].get("url", "")) if data else ""
        if not remote_url:
            raise SeedreamError("Seedream response did not contain an image URL")

        source_id = str(item.get("id") or item.get("canonical_key") or item.get("name") or source_kind)
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", source_id).strip("-") or "asset"
        target_dir = self.output_dir / workspace_id / output_subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            f"{source_kind}-{safe_id}-{uuid.uuid4().hex[:10]}.png"
            if output_subdir == "asset_candidates"
            else f"{source_kind}-{safe_id}.png"
        )
        target = target_dir / filename
        download = client.get(remote_url)
        download.raise_for_status()
        target.write_bytes(download.content)
        relative = target.relative_to(self.output_dir).as_posix()
        return {
            "source_kind": source_kind,
            "source_id": source_id,
            "name": item.get("name") or source_id,
            "library_asset_id": item.get("library_asset_id"),
            "canonical_key": item.get("canonical_key"),
            "reuse_decision": item.get("reuse_decision"),
            "model": self.model,
            "remote_url": remote_url,
            "url": f"/media/{relative}",
            "local_path": str(target),
            "status": "succeeded",
            "prompt": prompt,
        }

    @staticmethod
    def _reference_image_value(reference_url: str, reference_local_path: str) -> str:
        local = Path(str(reference_local_path or ""))
        if local.is_file() and local.stat().st_size <= 10 * 1024 * 1024:
            mime = mimetypes.guess_type(local.name)[0] or "image/png"
            encoded = base64.b64encode(local.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{encoded}"
        url = str(reference_url or "").strip()
        if url.startswith("https://") or url.startswith("http://"):
            return url
        return ""

    @staticmethod
    def _visual_localization_context(context: dict[str, Any]) -> dict[str, Any]:
        workspace = context.get("workspace") if isinstance(context.get("workspace"), dict) else {}
        settings = workspace.get("settings") if isinstance(workspace.get("settings"), dict) else {}
        script: dict[str, Any] = {}
        for artifact in context.get("artifacts", []) or []:
            if isinstance(artifact, dict) and str(artifact.get("kind") or "") == "script":
                candidate = artifact.get("content")
                if isinstance(candidate, dict):
                    script = candidate
                break
        return {
            "target_market": str(settings.get("target_market") or script.get("target_market") or "").strip(),
            "target_language": str(settings.get("target_language") or script.get("language") or "").strip(),
            "localization_strategy": str(script.get("localization_strategy") or "").strip(),
            "adaptation_notes": script.get("adaptation_notes") or [],
            "localization_map": script.get("localization_map") or [],
        }

    @staticmethod
    def _market_visual_rules(source_kind: str, target_market: str) -> list[str]:
        market = str(target_market or "").strip().lower()
        rules = [
            "Translate culturally source-specific styling into a plausible target-market equivalent; do not copy source-culture visual markers literally merely because they appear in source evidence.",
            "Preserve narrative function, stable identity, age, class/status contrast, recognisable silhouette/colour family and story-critical continuity anchors.",
            "Do not invent a new plot fact, role, relationship or prop function during visual localization.",
        ]
        if market in {"france", "french", "fr", "fr-fr"} or "france" in market:
            if source_kind == "characters":
                rules.extend([
                    "Use plausible contemporary/metropolitan-French or French-countryside wardrobe for the character's class and age.",
                    "For source-culture rural padded/floral clothing, preserve warmth, modest rural signal and any important red/burgundy identity colour, but translate the garment into a restrained French countryside quilted field jacket/coat, cardigan, wool skirt/trousers, practical leather shoes or other plausible French equivalent.",
                    "Avoid an oversized Chinese-style floral padded cotton jacket, qipao/cheongsam styling, East-Asian folk costume cues or decorative motifs unless the localized screenplay explicitly requires them.",
                ])
            elif source_kind == "scenes":
                rules.extend([
                    "Use architecture, furniture and domestic styling coherent with the localized French setting and social class.",
                    "Avoid Chinese/East-Asian mansion motifs, lattice screens, red-gold ceremonial decoration or source-culture furniture unless explicitly retained by the localized screenplay.",
                ])
            else:
                rules.extend([
                    "Use plausible French/European domestic object design, materials, tableware and ornament for the localized setting.",
                    "Avoid Chinese/East-Asian decorative motifs or tableware conventions unless they are story-critical and explicitly retained.",
                ])
        return rules

    @staticmethod
    def _prompt(source_kind: str, item: dict[str, Any], *, localization_context: dict[str, Any] | None = None) -> str:
        name = str(item.get("name") or item.get("id") or item.get("canonical_key") or "asset").strip()
        source_continuity = item.get("source_continuity_lock") or item.get("continuity_lock") or item.get("continuity") or []
        localization_context = localization_context or {}
        target_market = str(localization_context.get("target_market") or (item.get("visual_localization") or {}).get("target_market") or "").strip()
        target_language = str(localization_context.get("target_language") or (item.get("visual_localization") or {}).get("target_language") or "").strip()

        def text(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, (list, tuple)):
                return "; ".join(str(x).strip() for x in value if str(x).strip())
            if isinstance(value, dict):
                return "; ".join(f"{k}: {text(v)}" for k, v in value.items() if v not in (None, "", [], {}))
            return str(value)

        explicit_en = str(item.get("generation_prompt_en") or "").strip()
        localized_design = text(item.get("localized_visual_design"))
        source_traits = text(item.get("source_visual_traits"))
        localization_notes = text(item.get("visual_localization_notes"))
        market_rules = SeedreamProvider._market_visual_rules(source_kind, target_market)
        localization_header = [
            "VISUAL LOCALIZATION CONTRACT (authoritative for rendering):",
            f"Target market: {target_market or 'use the localized screenplay market'}.",
            f"Story/visible language: {target_language or 'use the localized screenplay language when text is required'}.",
            "Provider-facing visual instructions are intentionally written in English for image/video generation; this does not change dialogue language.",
            *[f"- {rule}" for rule in market_rules],
        ]
        if localization_context.get("localization_strategy"):
            localization_header.append("Screenplay localization strategy: " + str(localization_context.get("localization_strategy")))
        if localized_design:
            localization_header.append("Localized production design (prefer this over literal source styling): " + localized_design)
        if localization_notes:
            localization_header.append("Localization mapping notes: " + localization_notes)
        if explicit_en:
            localization_header.append("Curated English production prompt (primary design instruction): " + explicit_en)

        if source_kind == "characters":
            details = [
                f"Subject: {name}",
                f"Source visual anchors to preserve by FUNCTION, not necessarily literal source-culture styling: {source_traits or text(item.get('appearance') or item.get('description'))}",
                f"Character identity/appearance: {text(item.get('appearance') or item.get('description'))}",
                f"Source/current wardrobe evidence to CULTURALLY ADAPT where needed: {text(item.get('wardrobe') or item.get('costume'))}",
                f"Performance/body-language anchors: {text(item.get('performance'))}",
                f"Source continuity constraints (translate culturally if needed; never lose narrative function): {text(source_continuity)}",
            ]
            lead = (
                "Photorealistic cinematic character reference sheet for a localized French screen drama. "
                "Show the same person as a clear head-and-shoulders portrait and a full-body front view in one clean neutral studio composition. "
                "Stable face and body proportions; wardrobe must follow the localized production design rather than blindly copying source-culture costume. No scene action."
            )
        elif source_kind == "scenes":
            details = [
                f"Location: {name}",
                f"Source visual anchors to preserve by FUNCTION: {source_traits or text(item.get('location') or item.get('description'))}",
                f"Spatial layout: {text(item.get('layout') or item.get('key_set_elements'))}",
                f"Lighting: {text(item.get('lighting'))}",
                f"Time/weather: {text(item.get('time_weather'))}",
                f"Source continuity constraints: {text(source_continuity)}",
            ]
            lead = (
                "Photorealistic cinematic environment reference for a localized French screen drama. "
                "Wide production-design view with readable geography, architecture, entrances, furniture anchors and lighting. "
                "Use target-market architecture and decor rather than source-culture styling unless explicitly retained. No characters, no captions, no watermark."
            )
        else:
            details = [
                f"Prop: {name}",
                f"Source visual anchors to preserve by FUNCTION: {source_traits or text(item.get('description') or item.get('physical_description'))}",
                f"Physical design: {text(item.get('description') or item.get('physical_description'))}",
                f"Material/scale: {text(item.get('material_scale') or item.get('material'))}",
                f"State: {text(item.get('state'))}",
                f"Source continuity constraints: {text(source_continuity)}",
            ]
            lead = (
                "Photorealistic cinematic production-prop reference for a localized French screen drama. "
                "Isolated hero view on a neutral background with clear material, shape, scale cues and story-relevant state. "
                "Use target-market domestic/product design rather than source-culture ornament unless explicitly retained. No hands, no characters, no captions, no watermark."
            )
        library = item.get("_library_asset") if isinstance(item.get("_library_asset"), dict) else {}
        library_anchor = ""
        if library:
            library_anchor = (
                "\nReuse anchor from thematic asset library: "
                + str(library.get("name") or "")
                + ". "
                + str(library.get("description") or "")
                + " Continuity metadata: "
                + text(library.get("content") or {})
                + ". Keep canonical identity stable; when the library asset is source-culture specific, obey any explicit localized_visual_design/VARIANT requirement rather than blindly copying cultural styling."
            )
        return lead + "\n" + "\n".join(localization_header + [part for part in details if not part.endswith(": ")]) + library_anchor

    def _get_client(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.Client(timeout=180, follow_redirects=True), True
