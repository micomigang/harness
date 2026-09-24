from __future__ import annotations

import base64
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

from app.agents import agent_for_stage, system_prompt_for_stage
from app.guidance import allowed_parameter_description, sanitize_parameter_overrides

from .base import WorkflowProvider
from .source_media import SourceMediaError, SourceMediaProcessor


TASK_PROMPTS = {
    "analysis": "Analyze only supplied evidence. Return logline, source_summary, evidence_timeline, visual_style_targets, genre, constraints, questions, and evidence_limitations. The user message may mention candidate people or relationships; treat those only as search hints, never as confirmed facts or a fixed cast count. Discover all materially supported people from evidence, and mark identity/relationship uncertainty explicitly instead of forcing a candidate roster. Cite timestamps when describing visible evidence and do not infer unseen events between frames. Keep evidence_timeline concise and selective (prefer key beats/cuts; normally no more than 80 entries) rather than emitting one item per frame.",
    "script": (
        "Create a production-ready LOCALIZED SCREENPLAY from the approved source analysis; never return only a synopsis or translated beats. "
        "Treat workspace settings, project_memory, user_instruction, execution_directive and any linked asset_library_context as binding production context unless they conflict with evidence or the agent boundary. "
        "Separate SOURCE FACTS from LOCALIZATION DECISIONS. You may invent target-market equivalents (names, institutions, class markers, idioms, places) only as explicit adaptation decisions; never present those invented equivalents as if they were observed source facts. "
        "Avoid unnecessary pseudo-specific invention such as funding rounds, promotions, executive titles or family relationships unless the source evidence or user instruction requires them. If a precise detail is not needed to preserve dramatic function, use a conservative generic equivalent. "
        "Use workspace.settings.target_language as the output language and workspace.settings.target_market as the default cultural market when present. Localization means cultural adaptation, not word-for-word translation. "
        "For France specifically, write idiomatic metropolitan French unless the user requests another register; keep tu/vous choices consistent, use plausible French names/locations/institutions and euros where money is relevant, and avoid caricature or random French stereotypes. "
        "Preserve source story order, character functions, conflict, reveals, emotional reversals and cliffhanger unless the user explicitly asks for structural adaptation. "
        "Return title, language, target_market, localization_strategy, localization_map, episode_sections, beats, scenes, continuity_rules, adaptation_notes, open_questions, source_fact_register, adaptation_decisions, and asset_requirements. "
        "source_fact_register must separate confirmed, inferred, and unknown facts. adaptation_decisions must list each invented/localized choice with rationale and the source function it preserves. "
        "If the source contains a suspected episode/segment boundary, episode_sections must represent it explicitly. Do not hide a long post-boundary sequence inside one giant 'hook' scene. Mark scenes with production_scope (primary_episode, continuation_reference, or teaser). A teaser should be short and only carry the minimum next-episode hook unless the user explicitly asks to produce the continuation too. "
        "asset_requirements must define stable canonical IDs for every recurring character, location and story-critical prop needed downstream, with type, canonical_name, source_function, continuity_lock and preferred_library_asset_id when a linked asset library contains a suitable reusable asset. "
        "localization_map must be an array of {source_element, localized_element, rationale, preserve_function}. beats are concise structural beats only. "
        "scenes are the actual screenplay and each scene must include scene_id, heading, source_time_range, production_scope, objective, summary, action, dialogue, source_fact_basis, adaptation_decisions, cultural_adaptations, continuity_notes, and ending_hook. "
        "dialogue must be an array of {speaker, text, intent, register}; write short, speakable, idiomatic lines rather than literary class-exposition or translated moralizing. Where source wording or relationship is uncertain, preserve dramatic function and mark uncertainty in source_fact_basis/adaptation_notes/open_questions instead of fabricating a precise source quote or kinship fact. Cultural equivalents must be socially plausible in the target market; do not justify a made-up convention as if it were a real French norm. "
        "The screenplay must be detailed enough that character, scene, prop, storyboard and dialogue agents can work without re-interpreting vague summaries."
    ),
    "asset_manifest": (
        "Resolve the approved screenplay into a production Asset Manifest. script.asset_requirements is the authoritative MINIMUM REQUIREMENT contract, but its screenplay IDs are source bindings rather than the final manifest key namespace. Every listed requirement MUST be covered exactly once through items[].source_requirement_keys. items[].canonical_key is the stable normalized production identity used downstream and by the series library; do not overwrite it with CHAR_*/LOC_*/PROP_* screenplay IDs merely to prove coverage. Verify against screenplay scenes and continuity_rules and add only genuinely necessary extra reusable scenes or story-critical physical props. "
        "Consult linked asset_library_context, especially auto-managed libraries belonging to the current series. For every recurring character, reusable scene and story-critical prop return one item with manifest_id, asset_type (character/scene/prop), script_identity, canonical_key, source_requirement_keys, source_function, used_in, continuity_lock, decision, library_asset_id, confidence, reason, variant_requirements, generation_requirements. Prefer normalized lowercase manifest keys such as char_*, scene_*, prop_* when creating or migrating an identity. continuity_lock MUST be an array of concrete immutable continuity requirements (empty array means unlocked); never return a boolean. Costume/wardrobe is metadata of a character asset, not a standalone asset_type. Scene/set/location must use asset_type=scene. "
        "Typed metadata is required explicitly: character items include appearance and costume; scene items include key_set_elements; prop items include physical_description and used_in_scenes. These fields describe evidence-supported production traits and must not invent unsupported story facts. "
        "decision must be exactly REUSE, VARIANT or CREATE. REUSE means the existing canonical asset can be used unchanged. VARIANT means the same canonical identity/location/prop must be preserved but a bounded state change is needed (wardrobe, time-of-day, prop open/closed, etc.). CREATE means no suitable reusable asset exists and downstream asset agents must create one automatically. "
        "Never invent a library_asset_id. If there is no suitable library item, choose CREATE instead of asking the user to upload one. Prefer same-series canonical assets over generic thematic assets. Do not block merely because the library is empty. "
        "On regeneration, return a FULL manifest, not a patch: preserve all still-valid existing items and apply the requested additions/changes without deleting unrelated prior items. Asset counts are evidence-derived, not quotas: do not copy a historical/example count or force a 5/5/4 shape unless the actual screenplay/video evidence supports those distinct assets. Treat an approximate expected count as a diagnostic, not permission to invent or drop assets. Before answering, self-audit that all script.asset_requirements are covered and that requested extra props/locations are still present. Return items plus summary counts reuse_count, variant_count, create_count and unresolved_matches."
    ),
    "characters": "Build the character bible from the approved screenplay AND asset_manifest. Obey each character manifest decision. For REUSE, preserve the referenced library asset identity and continuity locks; do not redesign it. For VARIANT, preserve canonical identity and only change variant_requirements. For CREATE, design a new production-ready character specification automatically in the requested direction; absence of a library match is not a reason to stop. Return an items array. Each item needs id, manifest_id, name, appearance, wardrobe, performance, voice, used_in, continuity, canonical_key, reuse_decision (REUSE/VARIANT/CREATE), library_asset_id or null, continuity_lock, variant_key, reuse_decision_reason.",
    "scenes": "Build scene/location specifications from the approved screenplay AND asset_manifest. Obey scene decisions: REUSE keeps the referenced library location unchanged; VARIANT preserves the canonical layout/geography and only applies the requested state/time/lighting variation; CREATE automatically designs a new reusable location when no suitable library asset exists. Return an items array. Each item needs id, manifest_id, name, location, layout, lighting, time_weather, used_in, continuity, canonical_key, reuse_decision, library_asset_id or null, continuity_lock, variant_key, reuse_decision_reason. This is visual background design, never music.",
    "props": "Build only story-critical physical props from the approved screenplay AND asset_manifest. Obey prop decisions: REUSE keeps the canonical design; VARIANT preserves identity while applying a bounded state change such as open/closed/full/empty; CREATE automatically creates a new production prop specification when the series library has no match. Each item needs id, manifest_id, name, description, material_scale, state, used_in, continuity, canonical_key, reuse_decision, library_asset_id or null, continuity_lock, variant_key, reuse_decision_reason.",
    "storyboard": "Create the requested number of coherent vertical-video shots. Return shots and estimated_seconds. Each shot needs index, duration_seconds, story_beat, asset_bindings with existing IDs, camera, blocking, visual_prompt, and status. Do not include dialogue_prompt or audio_prompt because dedicated agents own them.",
    "dialogue_plan": "Return items aligned one-to-one with storyboard shots. Each item needs shot_index, speaker_id or null, text, language, timing, subtitle, lip_sync, and status. Use status silent for shots without speech.",
    "sound_plan": "Return items aligned one-to-one with storyboard shots. Each item needs shot_index, ambience, foley, cues, ducking, negative_audio, and status. Exclude spoken words and background-music composition.",
    "review": "Perform read-only QA. Return checks and blocking_failures. Every check needs name, status as pass/warn/fail, evidence, owner, and remediation. A fail must be listed in blocking_failures; do not silently repair inputs.",
    "music_plan": "Return global_style, cues, ducking, render_mode, and status. Each cue needs start_shot, end_shot, mood, instrumentation, intensity, and dialogue_avoidance. Exclude Foley and dialogue.",
}


DIRECTOR_SYSTEM_PROMPT = (
    "You are the Art Director / Orchestrator of a gated short-video production harness and the user's persistent production collaborator in an ongoing conversation. "
    "Your job is not to create the stage artifact itself. Respond to the user's latest message, take account of prior conversation, persistent project memory, current upstream artifacts and the existing effective instruction, then compile the result into a bounded execution directive for the target stage. "
    "When the user changes their mind, revise the effective instruction rather than blindly appending contradictory requirements. When they refer to the result just generated, acknowledge what is actually present in the artifact context and explain how the requested change will affect regeneration or the next stage. "
    "Respect stage ownership, upstream evidence, approvals, linked thematic asset libraries, series continuity and provider capabilities. For asset-related stages, prefer already-approved same-series canonical assets; when none match, instruct downstream agents to CREATE the missing asset automatically rather than asking the user to upload resources. Never invent unsupported provider parameters. "
    "Technical parameter overrides are user-controlled: do not change segment duration, frame count, parallelism, shot count, resolution, ratio, audio flags or other execution parameters unless the user's latest message explicitly requests that specific technical change. You may recommend a change in reply/warnings, but parameter_overrides must remain {} without explicit user authorization. "
    "Return exactly one JSON object with keys: reply, effective_instruction, interpretation, prompt_addendum, parameter_overrides, acceptance_criteria, memory_candidates, warnings, questions, requires_user_input. "
    "reply is a concise conversational response to the user. effective_instruction is the consolidated stage requirement that should be bound to the next execution, with obsolete/contradictory older instructions removed. "
    "parameter_overrides may contain only keys explicitly listed in allowed_parameter_overrides. If none are needed, return {}. "
    "memory_candidates is an array of durable project facts/preferences worth remembering, each as {text, scope, reason}; do not store temporary one-off execution details as durable memory. "
    "questions should contain only genuinely blocking ambiguities; otherwise make a conservative interpretation and continue. "
    "When interaction_mode is regenerate_current, treat the current-stage artifact as a draft under revision. Preserve parts that already satisfy the user, merge the latest feedback with still-valid earlier requirements, and change only what is necessary unless the user asks for a full rewrite. Do not ask the user to restate prior requirements. "
    "In regenerate_current mode, reply must explicitly summarize what you understood, what will be kept, what will change, and any downstream work that will be invalidated. effective_instruction must be a consolidated replacement instruction rather than an append-only transcript. "
    "For asset_manifest specifically, the canonical pipeline asset types are character, scene and prop. Treat costume/wardrobe references in older guidance as character metadata and set/location references as scene assets unless the latest user message explicitly changes the data model; do not preserve stale legacy type wording as a quota. script.asset_requirements IDs are source bindings and belong in source_requirement_keys; the manifest canonical_key is a separate stable normalized production identity. Do not require canonical_key to equal CHAR_*/LOC_*/PROP_* screenplay IDs. continuity_lock is canonically an array of immutable rule strings; historical true/false wording expresses lock intent only. "
    "Asset Manifest counts are evidence-derived. Never copy an exact historical total (for example 10/12/14 or a 5/5/4 shape) from conversation_history, an older review, existing_effective_instruction or a previous manifest into effective_instruction/acceptance_criteria as a binding target. Preserve content-specific requests that remain valid, but let script.asset_requirements plus supported extras determine the total. If a harness-owned asset_manifest_contract is present, it overrides conflicting historical count/taxonomy/key/lock wording."
)

DIRECTOR_REVIEW_SYSTEM_PROMPT = (
    "You are the Art Director / Orchestrator reviewing a stage that has just finished. "
    "Compare the actual generated artifact with the user's bound requirement, project memory and execution directive. "
    "Do not regenerate the artifact. Give the user a concise production-review response that states what was achieved, any important deviation or uncertainty, and what they can do next. "
    "Use the supplied next_actions to keep recommendations consistent with the gated workflow. "
    "Return exactly one JSON object with keys: reply, assessment, matched_requirements, deviations, suggested_adjustments, recommended_action, next_stage_focus, memory_candidates. "
    "recommended_action should be one of proceed, approve_current, regenerate_current, adjust_next_stage, wait_for_user. Use regenerate_current when the artifact materially violates a bound acceptance criterion, invents unsupported facts as source evidence, or is structurally unusable downstream; do not recommend approval merely because an artifact exists. "
    "For asset_manifest, generated_artifact.content.manifest_validation is the deterministic harness authority for schema and screenplay coverage. Treat missing_required_keys / required_baseline_coverage as authoritative for script.asset_requirements coverage. screenplay IDs belong in source_requirement_keys and are NOT required to equal canonical_key; do not flag normalized char_*/scene_*/prop_* canonical keys merely because the screenplay uses CHAR_*/LOC_*/PROP_* IDs. Treat duplicate_canonical_semantic_check, illegal_asset_type_check, continuity_lock_check and typed_metadata_check as authoritative when present. continuity_lock is canonically encoded as an array of rule strings; if older user/director prose says continuity_lock=true/false, interpret that only as lock intent and never flag the array encoding as a deviation. Review the complete compact items list supplied by the harness rather than assuming it is truncated. You may still flag additional assets explicitly required by the bound instruction. Do not invent a target item count; an approximate expected count is diagnostic only. Never recommend regeneration merely to restore old costume/set taxonomy, screenplay IDs as manifest keys, boolean lock encoding, or historical 10/12/14 counts when deterministic validation passes."
)

SEGMENT_ANALYSIS_TASK = (
    "You are analyzing one time-bounded segment of a source video. "
    "Return exactly one compact JSON object with keys: media_name, segment_index, segment_start_seconds, segment_end_seconds, "
    "summary, key_events, characters_seen, locations_seen, props_seen, visual_style_cues, unresolved_questions, evidence_timeline, and limitations. "
    "Use only the supplied frames and textual context. Any people/relationships mentioned by the user are candidate search hints only; discover evidence-supported entities independently and never force a fixed cast count. Be concise: summary <= 120 words; key_events <= 6; characters_seen <= 6; "
    "locations_seen <= 4; props_seen <= 6; visual_style_cues <= 5; unresolved_questions <= 4; evidence_timeline <= 8; limitations <= 5. "
    "Do not repeat the same fact in multiple fields and do not write long prose."
)

SEGMENT_SYNTHESIS_TASK = (
    "Synthesize the provided segment analyses into one global source-analysis JSON object. "
    "Return logline, source_summary, evidence_timeline, visual_style_targets, genre, constraints, questions, and evidence_limitations. "
    "Merge duplicate information, preserve important timestamps from the segment analyses, and stay faithful to the evidence. "
    "Keep evidence_timeline concise and selective (normally <= 80 entries)."
)


ASSET_INGEST_SYSTEM_PROMPT = (
    "You route uploaded reusable production resources into thematic asset libraries. "
    "Use the user's instruction, existing library names/themes and file metadata. Do not invent that you visually inspected a file unless an image was actually provided. "
    "Return exactly one JSON object with keys: reply, library_id, create_library, new_library_name, new_library_theme, new_library_description, default_asset_type, per_file_types, per_file_metadata, tags, notes. "
    "per_file_metadata maps each filename to {description, tags, continuity_notes}; for provided images, describe only visible reusable production traits and do not infer identity. "
    "asset types are character, scene, prop, reference, audio, video, document, other. Prefer an existing library when the user's wording clearly matches it; create a new library only when the user clearly requests a new theme or no suitable library exists."
)

class OpenAICompatibleProvider(WorkflowProvider):
    name = "openai-compatible"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        media_processor: SourceMediaProcessor | None = None,
        segment_seconds: float = 30.0,
        segment_max_frames: int = 12,
        segment_parallelism: int = 4,
        analysis_cache_enabled: bool = True,
        connect_timeout_seconds: float = 30.0,
        read_timeout_seconds: float = 900.0,
    ):
        if not api_key or not model:
            raise ValueError("OPENAI_COMPAT_API_KEY and OPENAI_COMPAT_MODEL are required")
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.media_processor = media_processor
        self.segment_seconds = max(5.0, float(segment_seconds))
        self.segment_max_frames = max(1, int(segment_max_frames))
        self.segment_parallelism = max(1, int(segment_parallelism))
        self.analysis_cache_enabled = bool(analysis_cache_enabled)
        self.connect_timeout_seconds = max(1.0, float(connect_timeout_seconds))
        self.read_timeout_seconds = max(30.0, float(read_timeout_seconds))

    def _report_progress(self, progress: int, message: str) -> None:
        callback = getattr(self, "_progress_callback", None)
        if callable(callback):
            try:
                callback(progress, message)
            except Exception:
                pass

    def plan_stage(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        agent = agent_for_stage(stage)
        planner_context = {
            "next_stage": stage,
            "agent": agent.as_dict(),
            "workspace": self._sanitize_for_external(context.get("workspace", {})),
            "project_memory": str(context.get("project_memory") or ""),
            "existing_effective_instruction": str(context.get("existing_instruction") or ""),
            "user_instruction": str(context.get("user_instruction") or ""),
            "interaction_mode": str(context.get("interaction_mode") or "conversation"),
            "conversation_history": self._compact_conversation_history(context.get("conversation_history", [])),
            "upstream_context": self._director_upstream_context(stage, context),
            "allowed_parameter_overrides": allowed_parameter_description(stage),
            "asset_library_context": self._sanitize_for_external(context.get("asset_library_context", [])),
        }
        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": DIRECTOR_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(planner_context, ensure_ascii=False)},
            ],
        }
        response_data, response_text = self._post_json(payload)
        try:
            content = response_data["choices"][0]["message"]["content"]
            plan = self._parse_json_object(content)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "Director response could not be parsed as JSON. Response preview: "
                + self._response_preview(response_data if response_data else response_text)
            ) from exc
        plan["parameter_overrides"] = sanitize_parameter_overrides(
            stage,
            plan.get("parameter_overrides"),
            user_instruction=str(context.get("user_instruction") or ""),
            require_explicit_user_request=True,
        )
        plan.setdefault("reply", "")
        plan.setdefault("effective_instruction", str(context.get("existing_instruction") or context.get("user_instruction") or ""))
        plan.setdefault("interpretation", "")
        plan.setdefault("prompt_addendum", "")
        plan.setdefault("acceptance_criteria", [])
        plan.setdefault("memory_candidates", [])
        plan.setdefault("warnings", [])
        plan.setdefault("questions", [])
        plan.setdefault("requires_user_input", False)
        plan["stage"] = stage
        plan["planner_model"] = self.model
        return plan

    def review_stage_result(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        review_context = {
            "stage": stage,
            "workspace": self._sanitize_for_external(context.get("workspace", {})),
            "project_memory": str(context.get("project_memory") or ""),
            "user_instruction": str(context.get("user_instruction") or ""),
            "execution_directive": self._sanitize_for_external(context.get("execution_directive", {})),
            "generated_artifact": self._director_artifact_preview(context.get("artifact", {})),
            "conversation_history": self._compact_conversation_history(context.get("conversation_history", [])),
            "next_actions": self._sanitize_for_external(context.get("next_actions", [])),
        }
        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": DIRECTOR_REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(review_context, ensure_ascii=False)},
            ],
        }
        response_data, response_text = self._post_json(payload)
        try:
            content = response_data["choices"][0]["message"]["content"]
            review = self._parse_json_object(content)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "Director review could not be parsed as JSON. Response preview: "
                + self._response_preview(response_data if response_data else response_text)
            ) from exc
        review.setdefault("reply", "")
        review.setdefault("assessment", "")
        review.setdefault("matched_requirements", [])
        review.setdefault("deviations", [])
        review.setdefault("suggested_adjustments", [])
        review.setdefault("recommended_action", "proceed")
        review.setdefault("next_stage_focus", "")
        review.setdefault("memory_candidates", [])
        review["planner_model"] = self.model
        return review

    def plan_asset_ingest(self, context: dict[str, Any]) -> dict[str, Any]:
        clean_context = self._sanitize_for_external(context)
        parts: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(clean_context, ensure_ascii=False)}]
        image_count = 0
        for item in context.get("files", []) if isinstance(context.get("files"), list) else []:
            if image_count >= 12 or not isinstance(item, dict):
                continue
            path = Path(str(item.get("local_path") or ""))
            if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            parts.append({"type": "text", "text": f"Uploaded reusable asset candidate: {item.get('name') or path.name}"})
            parts.append({"type": "image_url", "image_url": {"url": self._image_data_uri(path)}})
            image_count += 1
        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": ASSET_INGEST_SYSTEM_PROMPT},
                {"role": "user", "content": parts if image_count else json.dumps(clean_context, ensure_ascii=False)},
            ],
        }
        response_data, response_text = self._post_json(payload)
        try:
            content = response_data["choices"][0]["message"]["content"]
            result = self._parse_json_object(content)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Asset ingest routing could not be parsed as JSON. Response preview: " + self._response_preview(response_data if response_data else response_text)) from exc
        result.setdefault("reply", "")
        result.setdefault("library_id", "")
        result.setdefault("create_library", False)
        result.setdefault("new_library_name", "")
        result.setdefault("new_library_theme", "")
        result.setdefault("new_library_description", "")
        result.setdefault("default_asset_type", "other")
        result.setdefault("per_file_types", {})
        result.setdefault("per_file_metadata", {})
        result.setdefault("tags", [])
        result.setdefault("notes", "")
        return result

    @classmethod
    def _compact_conversation_history(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        compact: list[dict[str, Any]] = []
        for item in value[-16:]:
            if not isinstance(item, dict):
                continue
            compact.append(
                {
                    "stage": item.get("stage"),
                    "role": item.get("role"),
                    "content": str(item.get("content") or "")[:2000],
                }
            )
        return compact

    @classmethod
    def _compact_manifest_items(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        fields = (
            "manifest_id", "asset_type", "canonical_key", "source_requirement_keys", "script_identity",
            "source_function", "used_in", "continuity_lock", "decision",
            "library_asset_id", "confidence", "reason", "appearance", "costume",
            "key_set_elements", "physical_description", "used_in_scenes",
        )
        compact: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            compact.append({key: item.get(key) for key in fields if key in item})
        return compact

    @classmethod
    def _compact_asset_requirements(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        fields = (
            "canonical_id", "canonical_key", "type", "asset_type",
            "canonical_name", "source_function", "used_in",
            "continuity_lock", "preferred_library_asset_id",
        )
        compact: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            compact.append({key: item.get(key) for key in fields if key in item})
        return compact

    @classmethod
    def _director_artifact_preview(cls, artifact: Any) -> Any:
        if not isinstance(artifact, dict):
            return cls._sanitize_for_external(artifact)
        kind = str(artifact.get("kind") or "")
        preview = {
            "kind": artifact.get("kind"),
            "status": artifact.get("status"),
            "revision": artifact.get("revision"),
            "provider": artifact.get("provider"),
        }
        content = artifact.get("content")
        if not isinstance(content, dict):
            preview["content"] = cls._sanitize_for_external(content)
            return preview
        selected: dict[str, Any] = {}
        for key in (
            "logline", "source_summary", "title", "language", "target_market",
            "localization_strategy", "localization_map", "episode_sections", "beats", "scenes",
            "continuity_rules", "adaptation_notes", "open_questions",
            "source_fact_register", "adaptation_decisions", "asset_requirements",
            "items", "manifest_validation", "shots", "checks", "blocking_failures", "status", "note",
            "estimated_seconds", "url"
        ):
            if key not in content:
                continue
            value = content[key]
            if key == "items" and kind == "asset_manifest":
                value = cls._compact_manifest_items(value)
            elif key == "asset_requirements":
                value = cls._compact_asset_requirements(value)
            elif isinstance(value, list):
                value = value[:10]
            selected[key] = value
        preview["content"] = cls._sanitize_for_external(selected)
        return preview

    @classmethod
    def _director_upstream_context(
        cls, stage: str, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        cleaned = cls._prepare_prompt_context(stage, context)
        artifacts = []
        for artifact in cleaned.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            kind = str(artifact.get("kind") or "")
            content = artifact.get("content")
            # Keep the director call compact, but never truncate the screenplay asset
            # contract or the current Asset Manifest while planning its regeneration.
            preview: Any = content
            if isinstance(content, dict):
                preview = {}
                keys = [
                    "logline", "source_summary", "title", "language", "target_market",
                    "beats", "scenes", "items", "shots", "checks", "blocking_failures",
                    "status", "note", "estimated_seconds",
                ]
                if stage == "asset_manifest":
                    keys.extend(["continuity_rules", "asset_requirements", "manifest_validation"])
                for key in keys:
                    if key not in content:
                        continue
                    value = content[key]
                    if key == "items" and kind == "asset_manifest":
                        value = cls._compact_manifest_items(value)
                    elif key == "asset_requirements":
                        value = cls._compact_asset_requirements(value)
                    elif isinstance(value, list):
                        value = value[:12]
                    preview[key] = value
            artifacts.append(
                {
                    "kind": artifact.get("kind"),
                    "status": artifact.get("status"),
                    "revision": artifact.get("revision"),
                    "content": preview,
                }
            )
        return artifacts

    def _execution_parameters(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        directive = context.get("execution_directive") or {}
        return sanitize_parameter_overrides(stage, directive.get("parameter_overrides"))

    def generate(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        execution_params = self._execution_parameters(stage, context)
        segment_seconds = float(execution_params.get("segment_seconds", self.segment_seconds))
        segment_max_frames = int(execution_params.get("segment_max_frames", self.segment_max_frames))
        segment_parallelism = int(execution_params.get("segment_parallelism", self.segment_parallelism))
        if stage not in TASK_PROMPTS:
            raise NotImplementedError(
                f"Stage {stage} needs a media provider; the LLM adapter cannot render media."
            )
        prompt_context = self._prepare_prompt_context(stage, context)
        source_media: list[dict[str, Any]] = []
        if stage == "analysis":
            self._report_progress(22, "正在读取视频缓存 / 解析源视频")
        else:
            self._report_progress(30, f"正在调用 Kimi：{stage}")
        media_warning = ""
        if stage == "analysis" and self.media_processor:
            source = next(
                (
                    artifact.get("content", {})
                    for artifact in context.get("artifacts", [])
                    if artifact.get("kind") == "source"
                ),
                {},
            )
            try:
                source_media = self.media_processor.process(
                    str(context["workspace"]["id"]), list(source.get("files", []))
                )
            except SourceMediaError as exc:
                media_warning = str(exc)
            self._report_progress(28, f"视频取证完成：{len(source_media)} 个唯一源视频")
            prompt_context["source_media_technical_summary"] = [
                {
                    "name": item.get("name"),
                    "duration_seconds": item.get("duration_seconds"),
                    "size_bytes": item.get("size_bytes"),
                    "format": item.get("format"),
                    "video_stream": item.get("video_stream"),
                    "audio_stream": item.get("audio_stream"),
                    "sampling": item.get("sampling"),
                    "model_frame_timestamps": [
                        frame.get("timestamp_seconds")
                        for frame in item.get("frames", [])
                        if frame.get("selected_for_model")
                    ],
                }
                for item in source_media
            ]

        if stage == "analysis" and source_media:
            result = self._generate_segmented_analysis(
                prompt_context=prompt_context,
                source_media=source_media,
                segment_seconds=segment_seconds,
                segment_max_frames=segment_max_frames,
                segment_parallelism=segment_parallelism,
            )
        else:
            result = self._request_stage_json(
                stage=stage,
                prompt_context=prompt_context,
                source_media=source_media,
            )

        if source_media:
            result["source_media"] = [
                self._sanitize_media_for_storage(item) for item in source_media
            ]
        if media_warning:
            result["source_media_warning"] = media_warning
        return result

    def _generate_segmented_analysis(
        self,
        *,
        prompt_context: dict[str, Any],
        source_media: list[dict[str, Any]],
        segment_seconds: float | None = None,
        segment_max_frames: int | None = None,
        segment_parallelism: int | None = None,
    ) -> dict[str, Any]:
        segment_seconds = float(self.segment_seconds if segment_seconds is None else segment_seconds)
        segment_max_frames = int(self.segment_max_frames if segment_max_frames is None else segment_max_frames)
        segment_parallelism = int(self.segment_parallelism if segment_parallelism is None else segment_parallelism)
        segments: list[dict[str, Any]] = []
        for media in source_media:
            media_segments = self._build_media_segments(media, segment_seconds=segment_seconds, segment_max_frames=segment_max_frames)
            for segment in media_segments:
                segments.append({"media": media, "segment": segment})

        if not segments:
            return self._request_stage_json(
                stage="analysis",
                prompt_context=prompt_context,
                source_media=source_media,
            )

        completed: list[dict[str, Any]] = []
        failures: list[str] = []
        max_workers = min(segment_parallelism, len(segments))
        total_segments = len(segments)
        self._report_progress(
            32,
            f"Kimi 分段分析：共 {total_segments} 段，最多 {max_workers} 路并行",
        )

        if max_workers <= 1:
            for item in segments:
                try:
                    completed.append(
                        self._analyze_segment_cached(
                            prompt_context, item["media"], item["segment"]
                        )
                    )
                    done = len(completed) + len(failures)
                    self._report_progress(
                        32 + int(48 * done / total_segments),
                        f"Kimi 分段分析 {done}/{total_segments}",
                    )
                except Exception as exc:
                    segment = item["segment"]
                    failures.append(
                        f"segment {segment.get('segment_index')} "
                        f"[{segment.get('start_seconds')}-{segment.get('end_seconds')}s]: {exc}"
                    )
                    done = len(completed) + len(failures)
                    self._report_progress(
                        32 + int(48 * done / total_segments),
                        f"Kimi 分段分析 {done}/{total_segments}（存在失败段，已保存成功缓存）",
                    )
                    done = len(completed) + len(failures)
                    self._report_progress(
                        32 + int(48 * done / total_segments),
                        f"Kimi 分段分析 {done}/{total_segments}（存在失败段，已保存成功缓存）",
                    )
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(
                        self._analyze_segment_cached,
                        prompt_context,
                        item["media"],
                        item["segment"],
                    ): item
                    for item in segments
                }
                for future in as_completed(future_map):
                    item = future_map[future]
                    try:
                        completed.append(future.result())
                        done = len(completed) + len(failures)
                        self._report_progress(
                            32 + int(48 * done / total_segments),
                            f"Kimi 分段分析 {done}/{total_segments}",
                        )
                    except Exception as exc:
                        segment = item["segment"]
                        failures.append(
                            f"segment {segment.get('segment_index')} "
                            f"[{segment.get('start_seconds')}-{segment.get('end_seconds')}s]: {exc}"
                        )

        # Successful segments write their cache before returning, so even if one
        # parallel request fails, the next run resumes from only the missing parts.
        if failures:
            raise RuntimeError(
                "Segmented source analysis incomplete. Successful segments were cached; "
                "rerun the same analysis to resume only failed/missing segments. "
                + " | ".join(failures[:8])
            )

        completed.sort(
            key=lambda item: (
                str(item["result"].get("media_name") or ""),
                float(item["result"].get("segment_start_seconds") or 0.0),
                int(item["result"].get("segment_index") or 0),
            )
        )
        segment_results = [item["result"] for item in completed]
        segment_cache_keys = [item["cache_key"] for item in completed]
        cache_hits = sum(1 for item in completed if item["cache_hit"])
        cache_misses = len(completed) - cache_hits

        analysis_plan = {
            "segment_seconds": segment_seconds,
            "segment_max_frames": segment_max_frames,
            "segment_parallelism": segment_parallelism,
            "segment_count": len(segment_results),
            "cache_enabled": self.analysis_cache_enabled,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "resumed_from_cache": cache_hits > 0,
        }
        summary_context = {
            "workspace": self._stable_workspace_context(prompt_context),
            "source_media_technical_summary": prompt_context.get(
                "source_media_technical_summary", []
            ),
            "segment_analysis_plan": analysis_plan,
            "segment_analyses": segment_results,
        }

        synthesis_key = self._synthesis_cache_key(
            prompt_context=prompt_context,
            segment_cache_keys=segment_cache_keys,
        )
        synthesis_path = self._analysis_cache_dir(prompt_context) / f"final-{synthesis_key}.json"
        self._report_progress(84, "分段分析完成，正在生成全局素材分析")
        result = self._load_cached_result(synthesis_path, synthesis_key)
        synthesis_cache_hit = result is not None
        if result is None:
            result = self._request_json(
                task=self._task_with_directive(SEGMENT_SYNTHESIS_TASK, prompt_context),
                user_content=json.dumps(summary_context, ensure_ascii=False),
                stage="analysis",
            )
            if self.analysis_cache_enabled:
                self._write_cached_result(synthesis_path, synthesis_key, result)

        result["segment_analyses"] = segment_results
        analysis_plan["synthesis_cache_hit"] = synthesis_cache_hit
        result["analysis_plan"] = analysis_plan
        self._report_progress(92, "素材分析汇总完成")
        return result

    def _build_media_segments(self, media: dict[str, Any], *, segment_seconds: float | None = None, segment_max_frames: int | None = None) -> list[dict[str, Any]]:
        segment_seconds = float(self.segment_seconds if segment_seconds is None else segment_seconds)
        segment_max_frames = int(self.segment_max_frames if segment_max_frames is None else segment_max_frames)
        if self.media_processor:
            return self.media_processor.build_analysis_segments(
                media,
                segment_seconds=segment_seconds,
                max_frames_per_segment=segment_max_frames,
            )
        frames = [
            frame for frame in media.get("frames", []) if frame.get("selected_for_model")
        ]
        if not frames:
            return []
        return [
            {
                "segment_index": 0,
                "start_seconds": 0.0,
                "end_seconds": float(media.get("duration_seconds") or 0.0),
                "duration_seconds": float(media.get("duration_seconds") or 0.0),
                "frame_count": len(frames[: segment_max_frames]),
                "frames": frames[: segment_max_frames],
            }
        ]

    def _analyze_segment_cached(
        self,
        prompt_context: dict[str, Any],
        media: dict[str, Any],
        segment: dict[str, Any],
    ) -> dict[str, Any]:
        cache_key = self._segment_cache_key(prompt_context, media, segment)
        cache_path = self._analysis_cache_dir(prompt_context) / f"segment-{cache_key}.json"
        cached = self._load_cached_result(cache_path, cache_key)
        if cached is not None:
            return {"result": cached, "cache_key": cache_key, "cache_hit": True}

        result = self._analyze_segment(prompt_context, media, segment)
        if self.analysis_cache_enabled:
            self._write_cached_result(cache_path, cache_key, result)
        return {"result": result, "cache_key": cache_key, "cache_hit": False}

    def _analyze_segment(
        self,
        prompt_context: dict[str, Any],
        media: dict[str, Any],
        segment: dict[str, Any],
    ) -> dict[str, Any]:
        user_content = self._build_segment_user_content(prompt_context, media, segment)
        result = self._request_json(
            task=self._task_with_directive(SEGMENT_ANALYSIS_TASK, prompt_context),
            user_content=user_content,
            stage="analysis",
        )
        result.setdefault("media_name", media.get("name"))
        result.setdefault("segment_index", segment.get("segment_index"))
        result.setdefault("segment_start_seconds", segment.get("start_seconds"))
        result.setdefault("segment_end_seconds", segment.get("end_seconds"))
        return result

    def _analysis_cache_dir(self, prompt_context: dict[str, Any]) -> Path:
        workspace_id = str((prompt_context.get("workspace") or {}).get("id") or "unknown")
        if self.media_processor is not None:
            root = self.media_processor.output_dir
        else:
            root = Path("data") / "outputs"
        path = root / workspace_id / "analysis_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _segment_cache_key(
        self,
        prompt_context: dict[str, Any],
        media: dict[str, Any],
        segment: dict[str, Any],
    ) -> str:
        payload = {
            "version": 3,
            "model": self.model,
            "task": system_prompt_for_stage(
                "analysis", self._task_with_directive(SEGMENT_ANALYSIS_TASK, prompt_context)
            ),
            "stable_context": self._stable_analysis_context(prompt_context),
            "media": {
                "name": media.get("name"),
                "source_sha256": media.get("source_sha256"),
                "size_bytes": media.get("size_bytes"),
                "duration_seconds": media.get("duration_seconds"),
                "sampling": media.get("sampling"),
            },
            "segment": {
                "index": segment.get("segment_index"),
                "start": segment.get("start_seconds"),
                "end": segment.get("end_seconds"),
                "frames": [
                    {
                        "timestamp_seconds": frame.get("timestamp_seconds"),
                        "kind": frame.get("kind"),
                        "size_bytes": frame.get("size_bytes"),
                    }
                    for frame in segment.get("frames", [])
                ],
            },
        }
        return self._hash_payload(payload)

    def _synthesis_cache_key(
        self,
        *,
        prompt_context: dict[str, Any],
        segment_cache_keys: list[str],
    ) -> str:
        payload = {
            "version": 3,
            "model": self.model,
            "task": system_prompt_for_stage(
                "analysis", self._task_with_directive(SEGMENT_SYNTHESIS_TASK, prompt_context)
            ),
            "stable_context": self._stable_analysis_context(prompt_context),
            "segment_cache_keys": segment_cache_keys,
        }
        return self._hash_payload(payload)

    @staticmethod
    def _hash_payload(payload: Any) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _stable_workspace_context(prompt_context: dict[str, Any]) -> dict[str, Any]:
        workspace = prompt_context.get("workspace") or {}
        return {
            "id": workspace.get("id"),
            "title": workspace.get("title"),
            "brief": workspace.get("brief"),
            "settings": workspace.get("settings"),
        }

    @classmethod
    def _stable_analysis_context(cls, prompt_context: dict[str, Any]) -> dict[str, Any]:
        source_artifact: dict[str, Any] = {}
        for artifact in prompt_context.get("artifacts", []):
            if artifact.get("kind") == "source":
                source_artifact = artifact
                break
        source_content = source_artifact.get("content") or {}
        files = []
        seen_files: set[tuple[Any, ...]] = set()
        for item in source_content.get("files", []):
            record = {
                "name": item.get("name"),
                "size": item.get("size"),
                "content_type": item.get("content_type"),
            }
            key = (record["name"], record["size"], record["content_type"])
            if key in seen_files:
                continue
            seen_files.add(key)
            files.append(record)
        return {
            "workspace": cls._stable_workspace_context(prompt_context),
            "source_revision": source_artifact.get("revision"),
            "source_brief": source_content.get("brief"),
            "source_files": files,
            "project_memory": prompt_context.get("project_memory", ""),
            "user_instruction": prompt_context.get("user_instruction", ""),
            "execution_directive": prompt_context.get("execution_directive", {}),
        }

    def _load_cached_result(self, path: Path, cache_key: str) -> dict[str, Any] | None:
        if not self.analysis_cache_enabled:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if payload.get("cache_key") != cache_key:
            return None
        result = payload.get("result")
        return result if isinstance(result, dict) else None

    @staticmethod
    def _write_cached_result(path: Path, cache_key: str, result: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cache_key": cache_key, "result": result}
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _parse_json_object(content: Any) -> dict[str, Any]:
        if isinstance(content, dict):
            return content
        if isinstance(content, list):
            text_parts = [
                str(part.get("text"))
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "text"
                and part.get("text") is not None
            ]
            content = "\n".join(text_parts)
        if not isinstance(content, str):
            raise TypeError(
                f"Expected model content to be text or object, got {type(content).__name__}"
            )

        text = content.strip().lstrip("\ufeff")
        if not text:
            raise ValueError("Model returned empty content")

        candidates = [text]
        if text.startswith("```"):
            first_newline = text.find("\n")
            closing_fence = text.rfind("```")
            if first_newline >= 0:
                if closing_fence > first_newline:
                    candidates.insert(0, text[first_newline + 1 : closing_fence].strip())
                else:
                    candidates.insert(0, text[first_newline + 1 :].strip())

        decoder = json.JSONDecoder()
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except ValueError as exc:
                last_error = exc
                start = candidate.find("{")
                if start < 0:
                    continue
                try:
                    value, _ = decoder.raw_decode(candidate[start:])
                except ValueError as raw_exc:
                    last_error = raw_exc
                    continue
            if not isinstance(value, dict):
                raise TypeError(
                    f"Expected top-level JSON object, got {type(value).__name__}"
                )
            return value

        raise ValueError(f"Could not parse model content as JSON object: {last_error}")

    @staticmethod
    def _response_preview(value: Any, limit: int = 6000) -> str:
        try:
            if isinstance(value, str):
                text = value
            else:
                text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = repr(value)
        if len(text) <= limit:
            return text
        head = text[: limit // 2]
        tail = text[-(limit // 2) :]
        return f"{head} ... <truncated preview> ... {tail}"

    @staticmethod
    def _task_with_directive(base_task: str, prompt_context: dict[str, Any]) -> str:
        directive = prompt_context.get("execution_directive") or {}
        addendum = str(directive.get("prompt_addendum") or "").strip()
        criteria = directive.get("acceptance_criteria") or []
        params = directive.get("parameter_overrides") or {}
        task = base_task
        if addendum:
            task += "\n\nART DIRECTOR EXECUTION ADDENDUM: " + addendum
        if params:
            task += "\nExecution parameter intent: " + json.dumps(params, ensure_ascii=False)
        if criteria:
            task += "\nAcceptance criteria: " + json.dumps(criteria, ensure_ascii=False)
        return task

    def _build_payload(
        self,
        *,
        task: str,
        user_content: str | list[dict[str, Any]],
        stage: str,
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt_for_stage(stage, task)},
                {"role": "user", "content": user_content},
            ],
        }

    def _request_stage_json(
        self,
        *,
        stage: str,
        prompt_context: dict[str, Any],
        source_media: list[dict[str, Any]],
    ) -> dict[str, Any]:
        task = self._task_with_directive(TASK_PROMPTS[stage], prompt_context)
        return self._request_json(
            task=task,
            user_content=self._build_user_content(
                stage=stage,
                prompt_context=prompt_context,
                source_media=source_media,
            ),
            stage=stage,
        )

    def _request_json(
        self,
        *,
        task: str,
        user_content: str | list[dict[str, Any]],
        stage: str,
    ) -> dict[str, Any]:
        payload = self._build_payload(task=task, user_content=user_content, stage=stage)
        response_data, response_text = self._post_json(payload)
        try:
            choice = response_data["choices"][0]
            content = choice["message"]["content"]
            return self._parse_json_object(content)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            finish_reason = None
            try:
                finish_reason = response_data.get("choices", [{}])[0].get(
                    "finish_reason"
                )
            except Exception:
                pass
            preview = self._response_preview(response_data if response_data else response_text)
            hint = (
                " The model stopped because the output-length limit was reached; reduce requested output or raise the model output limit."
                if finish_reason == "length"
                else ""
            )
            raise RuntimeError(
                "Model response could not be parsed as the expected JSON object"
                f" (finish_reason={finish_reason!r}).{hint} Response preview: {preview}"
            ) from exc

    def _post_json(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        timeout = httpx.Timeout(self.read_timeout_seconds, connect=self.connect_timeout_seconds)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except httpx.ReadTimeout as exc:
            raise RuntimeError(
                "Model API read timed out after "
                f"{self.read_timeout_seconds:.0f}s. "
                "The upstream model may still have received the request. "
                "Please wait and retry later; segmented cache / resume will reuse completed segments."
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                "Model API request timed out. "
                f"connect_timeout={self.connect_timeout_seconds:.0f}s, read_timeout={self.read_timeout_seconds:.0f}s"
            ) from exc
        if response.is_error:
            raise RuntimeError(self._format_http_error(response))
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Model API returned invalid JSON envelope: {self._response_preview(response.text)}"
            ) from exc
        return data, response.text

    def _build_user_content(
        self,
        *,
        stage: str,
        prompt_context: dict[str, Any],
        source_media: list[dict[str, Any]],
    ) -> str | list[dict[str, Any]]:
        context_text = json.dumps(prompt_context, ensure_ascii=False)
        if stage != "analysis" or not source_media:
            return context_text

        parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    context_text
                    + "\n\n以下按时间顺序附上源视频的视觉证据帧。"
                    "每张图前的标签给出视频名、时间戳和抽帧类型。"
                    "请只根据可见画面和上面的已提供文字信息作事实判断；"
                    "帧之间未观察到的事件必须标注为不确定。"
                ),
            }
        ]
        attached = 0
        for media in source_media:
            media_name = str(media.get("name") or "source-video")
            selected_frames = [
                frame
                for frame in media.get("frames", [])
                if frame.get("selected_for_model") and frame.get("local_path")
            ]
            if not selected_frames:
                continue
            parts.append(
                {
                    "type": "text",
                    "text": (
                        f"\n视频：{media_name}；"
                        f"时长：{media.get('duration_seconds')} 秒；"
                        f"本次发送 {len(selected_frames)} 张视觉证据帧。"
                    ),
                }
            )
            for frame in selected_frames:
                path = Path(str(frame["local_path"]))
                if not path.is_file():
                    continue
                timestamp = float(frame.get("timestamp_seconds") or 0.0)
                kind = str(frame.get("kind") or "sample")
                parts.append(
                    {"type": "text", "text": f"[{media_name} | t={timestamp:.3f}s | {kind}]"}
                )
                parts.append(
                    {"type": "image_url", "image_url": {"url": self._image_data_uri(path)}}
                )
                attached += 1

        if attached == 0:
            return context_text
        return parts

    def _build_segment_user_content(
        self,
        prompt_context: dict[str, Any],
        media: dict[str, Any],
        segment: dict[str, Any],
    ) -> list[dict[str, Any]]:
        segment_context = {
            "workspace": prompt_context.get("workspace"),
            "brief": prompt_context.get("brief"),
            "source_media_technical_summary": [
                {
                    "name": media.get("name"),
                    "duration_seconds": media.get("duration_seconds"),
                    "size_bytes": media.get("size_bytes"),
                    "format": media.get("format"),
                    "video_stream": media.get("video_stream"),
                    "audio_stream": media.get("audio_stream"),
                    "sampling": media.get("sampling"),
                }
            ],
            "segment": {
                "segment_index": segment.get("segment_index"),
                "start_seconds": segment.get("start_seconds"),
                "end_seconds": segment.get("end_seconds"),
                "duration_seconds": segment.get("duration_seconds"),
                "frame_count": segment.get("frame_count"),
            },
        }
        parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    json.dumps(segment_context, ensure_ascii=False)
                    + "\n\n以下图片全部来自同一视频时间片段。"
                    "请只总结该片段内能看到的事实，并在 evidence_timeline 中使用该片段内的时间戳。"
                ),
            }
        ]
        media_name = str(media.get("name") or "source-video")
        for frame in segment.get("frames", []):
            path = Path(str(frame.get("local_path") or ""))
            if not path.is_file():
                continue
            timestamp = float(frame.get("timestamp_seconds") or 0.0)
            kind = str(frame.get("kind") or "sample")
            parts.append({"type": "text", "text": f"[{media_name} | t={timestamp:.3f}s | {kind}]"})
            parts.append(
                {"type": "image_url", "image_url": {"url": self._image_data_uri(path)}}
            )
        return parts

    @staticmethod
    def _image_data_uri(path: Path) -> str:
        suffix = path.suffix.lower().lstrip(".")
        mime_suffix = "jpeg" if suffix in {"jpg", "jpeg"} else suffix or "jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/{mime_suffix};base64,{encoded}"

    @staticmethod
    def _format_http_error(response: httpx.Response) -> str:
        try:
            detail: Any = response.json()
            if isinstance(detail, (dict, list)):
                detail_text = json.dumps(detail, ensure_ascii=False)
            else:
                detail_text = str(detail)
        except ValueError:
            detail_text = response.text
        detail_text = detail_text.strip()[:4000]
        request_id = response.headers.get("x-request-id") or response.headers.get(
            "request-id"
        )
        suffix = f"; request_id={request_id}" if request_id else ""
        return f"Model API HTTP {response.status_code}: {detail_text or response.reason_phrase}{suffix}"

    @classmethod
    def _sanitize_media_for_storage(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._sanitize_media_for_storage(item)
                for key, item in value.items()
                if key not in {"path", "local_path", "sha256", "source_sha256", "remote_url"}
            }
        if isinstance(value, list):
            return [cls._sanitize_media_for_storage(item) for item in value]
        return value

    @classmethod
    def _prepare_prompt_context(cls, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        """Strip runtime/heavy evidence that downstream agents do not need.

        In particular, the screenplay should consume the synthesized analysis, not
        dozens of raw segment analyses or repeated source-video metadata.
        """
        cleaned = cls._sanitize_for_external(context)
        if not isinstance(cleaned, dict):
            return {}
        artifacts = []
        for artifact in cleaned.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            kind = artifact.get("kind")
            if stage != "analysis" and kind == "source":
                continue
            item = dict(artifact)
            content = item.get("content")
            if isinstance(content, dict) and kind == "analysis":
                compact = dict(content)
                compact.pop("segment_analyses", None)
                compact.pop("source_media", None)
                compact.pop("analysis_plan", None)
                compact.pop("source_media_warning", None)
                item["content"] = compact
            artifacts.append(item)
        cleaned["artifacts"] = artifacts
        return cleaned

    @classmethod
    def _sanitize_for_external(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._sanitize_for_external(item)
                for key, item in value.items()
                if key not in {"path", "local_path", "sha256", "source_sha256", "remote_url", "source_media"}
            }
        if isinstance(value, list):
            return [cls._sanitize_for_external(item) for item in value]
        return value
