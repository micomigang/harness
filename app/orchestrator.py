from __future__ import annotations

import hashlib
import json
import re
import traceback
import unicodedata
from typing import Any, Callable

from .agents import STAGE_AGENTS, agent_for_stage
from .db import Database
from .providers import ProviderRegistry


STAGES = [
    "source",
    "analysis",
    "script",
    "asset_manifest",
    "characters",
    "scenes",
    "props",
    "reference_images",
    "storyboard",
    "dialogue_plan",
    "sound_plan",
    "review",
    "preview",
    "batch_video",
    "music_plan",
    "music",
    "compose",
    "delivery_qa",
]

STAGE_LABELS = {
    "source": "素材",
    "analysis": "素材分析",
    "script": "剧本",
    "asset_manifest": "资产解析",
    "characters": "角色",
    "scenes": "场景",
    "props": "道具",
    "reference_images": "参考图",
    "storyboard": "分镜",
    "dialogue_plan": "对白与口型",
    "sound_plan": "逐镜音效",
    "review": "一致性检查",
    "preview": "单镜预览",
    "batch_video": "批量视频",
    "music_plan": "全片配乐方案",
    "music": "音乐",
    "compose": "最终合成",
    "delivery_qa": "交付质检",
}

REQUIRES = {
    "analysis": ["source"],
    "script": ["analysis"],
    "asset_manifest": ["script"],
    "characters": ["script", "asset_manifest"],
    "scenes": ["script", "asset_manifest"],
    "props": ["script", "asset_manifest"],
    "reference_images": ["characters", "scenes", "props"],
    "storyboard": ["script", "characters", "scenes", "props", "reference_images"],
    "dialogue_plan": ["storyboard", "characters"],
    "sound_plan": ["storyboard", "scenes"],
    "review": ["storyboard", "dialogue_plan", "sound_plan"],
    "preview": ["review"],
    "batch_video": ["preview"],
    "music_plan": ["batch_video", "script", "dialogue_plan"],
    "music": ["batch_video", "music_plan"],
    "compose": ["batch_video", "music", "music_plan"],
    "delivery_qa": ["compose", "batch_video"],
}

GATES = {
    "script": "script_approved",
    "reference_images": "assets_approved",
    "review": "storyboard_approved",
    "preview": "preview_approved",
}

if set(STAGES) != set(STAGE_AGENTS):
    missing = sorted(set(STAGES) - set(STAGE_AGENTS))
    extra = sorted(set(STAGE_AGENTS) - set(STAGES))
    raise RuntimeError(f"Agent ownership mismatch; missing={missing}, extra={extra}")


class OrchestrationError(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, db: Database, providers: ProviderRegistry | None = None):
        self.db = db
        self.providers = providers or ProviderRegistry()

    def bootstrap_workspace(self, workspace_id: str) -> dict[str, Any]:
        workspace = self._workspace(workspace_id)
        agent = agent_for_stage("source")
        return self.db.upsert_artifact(
            workspace_id,
            "source",
            "原始素材与制作说明",
            {
                "brief": workspace["brief"],
                "files": [],
                "ingest_status": "ready",
                "note": "上传接口已保留；mock 演示直接使用 brief。",
                "_agent": {"id": agent.id, "name": agent.name},
            },
            "local",
        )

    def run(
        self,
        workspace_id: str,
        stage: str,
        progress_callback: Callable[[int, str], None] | None = None,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGES or stage == "source":
            raise OrchestrationError(f"Stage cannot be executed: {stage}")
        workspace = self._workspace(workspace_id)
        artifacts = self.db.list_artifacts(workspace_id)
        by_kind = {artifact["kind"]: artifact for artifact in artifacts}
        missing = [
            item
            for item in REQUIRES.get(stage, [])
            if item not in by_kind or by_kind[item]["status"] != "ready"
        ]
        if missing:
            raise OrchestrationError("Missing or stale prerequisites: " + ", ".join(missing))

        required_gate = self._required_gate(stage)
        if required_gate and not self._is_approved(workspace_id, required_gate):
            raise OrchestrationError(f"Approval required: {required_gate}")
        self._validate_stage(stage, by_kind)

        provider = self.providers.workflow()
        if progress_callback:
            progress_callback(18, "总管正在理解本步骤需求")
            # RoutedProvider exposes its Kimi provider as .llm. Attach a runtime
            # callback without putting a Python callable into model prompt context.
            target = getattr(provider, "llm", provider)
            try:
                setattr(target, "_progress_callback", progress_callback)
            except Exception:
                pass

        request = request or {}
        saved_guidance = self.db.get_stage_guidance(workspace_id, stage) or {}
        user_instruction = str(
            request.get("user_instruction")
            if request.get("user_instruction") is not None
            else saved_guidance.get("user_instruction") or ""
        ).strip()
        project_memory = self.db.get_workspace_memory(workspace_id)
        execution_directive = self._resolve_execution_directive(
            provider=provider,
            workspace=workspace,
            artifacts=artifacts,
            stage=stage,
            user_instruction=user_instruction,
            project_memory=project_memory,
            saved_guidance=saved_guidance,
        )
        if execution_directive.get("requires_user_input") and execution_directive.get("questions"):
            questions = "；".join(map(str, execution_directive.get("questions", [])[:5]))
            raise OrchestrationError(
                "总管需要你补充本步骤要求后再执行：" + questions
            )
        if progress_callback:
            summary = str(execution_directive.get("interpretation") or "").strip()
            progress_callback(
                24,
                "总管已生成执行指令" + (f"：{summary[:100]}" if summary else ""),
            )
        selected_candidates = self._selected_asset_candidates_for_current_artifacts(workspace_id, artifacts)
        runtime_context = {
            "workspace": workspace,
            "artifacts": artifacts,
            "approvals": self.db.list_approvals(workspace_id),
            "project_memory": project_memory,
            "user_instruction": user_instruction,
            "execution_directive": execution_directive,
            "asset_library_context": self.db.workspace_asset_context(workspace_id),
            "asset_candidates": selected_candidates,
            "series": self.db.get_series(str((workspace.get("settings") or {}).get("series_id") or "")) if (workspace.get("settings") or {}).get("series_id") else None,
        }
        content = provider.generate(stage, runtime_context)
        if stage == "asset_manifest":
            content = self._normalize_asset_manifest(
                content,
                runtime_context.get("asset_library_context", []),
                script_content=(by_kind.get("script") or {}).get("content", {}),
                previous_manifest=(by_kind.get("asset_manifest") or {}).get("content", {}),
            )
        elif stage in {"characters", "scenes", "props"}:
            content = self._enforce_asset_manifest(stage, content, artifacts, workspace=workspace)
        if progress_callback:
            progress_callback(94, "生成完成，正在保存产物")
        agent = agent_for_stage(stage)
        content["_agent"] = {"id": agent.id, "name": agent.name}
        content["_execution"] = {
            "user_instruction": user_instruction,
            "project_memory_applied": bool(project_memory.strip()),
            "director_interpretation": execution_directive.get("interpretation", ""),
            "prompt_addendum": execution_directive.get("prompt_addendum", ""),
            "parameter_overrides": execution_directive.get("parameter_overrides", {}),
            "acceptance_criteria": execution_directive.get("acceptance_criteria", []),
            "planner_model": execution_directive.get("planner_model", ""),
        }
        result = self.db.upsert_artifact(
            workspace_id,
            stage,
            STAGE_LABELS[stage],
            content,
            provider.name,
            upstream=REQUIRES.get(stage, []),
        )
        if stage == "reference_images":
            self._sync_reference_image_candidates(workspace_id, result)
        self.db.update_workspace(workspace_id, stage=stage, status="active")
        if progress_callback:
            progress_callback(96, "总管正在复盘本阶段生成结果")
        self._post_generation_review(
            provider=provider,
            workspace=workspace,
            stage=stage,
            artifact=result,
            user_instruction=user_instruction,
            project_memory=project_memory,
            execution_directive=execution_directive,
        )
        if progress_callback:
            progress_callback(99, "总管复盘完成，正在结束任务")
        return result

    def _sync_reference_image_candidates(self, workspace_id: str, artifact: dict[str, Any]) -> None:
        revision = int(artifact.get("revision") or 0)
        items = (artifact.get("content") or {}).get("items") or []
        existing = self.db.list_asset_candidates(
            workspace_id, stage="reference_images", artifact_revision=revision, limit=2000
        )
        existing_keys = {str(item.get("canonical_key") or "") for item in existing}
        for index, item in enumerate(items if isinstance(items, list) else []):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            canonical_key = str(
                item.get("canonical_key")
                or item.get("reference_key")
                or f"reference_{index + 1:03d}"
            )
            if canonical_key in existing_keys:
                continue
            auto_selected = str(item.get("status") or "").lower() in {
                "selected_candidate", "upstream_adopted", "reused", "selected", "locked"
            }
            self.db.add_asset_candidate(
                workspace_id,
                "reference_images",
                revision,
                canonical_key,
                str(item.get("source_id") or canonical_key),
                feedback="",
                prompt=str(item.get("prompt") or ""),
                model=str(item.get("model") or ""),
                remote_url=str(item.get("remote_url") or ""),
                url=str(item.get("url") or ""),
                local_path=str(item.get("local_path") or ""),
                selected=auto_selected,
            )

    def execute_job(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        try:
            agent = agent_for_stage(job["stage"])
            self.db.update_job(
                job_id,
                status="running",
                progress=15,
                message=f"正在调度：{agent.name}",
            )
            def report(progress: int, message: str) -> None:
                self.db.update_job(
                    job_id,
                    status="running",
                    progress=max(15, min(95, int(progress))),
                    message=message,
                )

            result = self.run(
                job["workspace_id"],
                job["stage"],
                progress_callback=report,
                request=job.get("request") or {},
            )
            self.db.update_job(
                job_id,
                status="succeeded",
                progress=100,
                message="完成",
                result_json=json.dumps(result, ensure_ascii=False),
            )
        except Exception as exc:
            trace = traceback.format_exc()
            print(trace, flush=True)
            self.db.update_job(
                job_id,
                status="failed",
                progress=100,
                message=str(exc),
                result_json=json.dumps(
                    {"error": str(exc), "traceback": trace},
                    ensure_ascii=False,
                ),
            )

    def _selected_asset_candidates_for_current_artifacts(
        self, workspace_id: str, artifacts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        revisions = {
            str(item.get("kind") or ""): int(item.get("revision") or 0)
            for item in artifacts
            if isinstance(item, dict)
        }
        return [
            candidate
            for candidate in self.db.list_asset_candidates(workspace_id, selected_only=True, limit=1000)
            if int(candidate.get("artifact_revision") or 0)
            == revisions.get(str(candidate.get("stage") or ""), -1)
        ]

    def plan_stage(
        self,
        workspace_id: str,
        stage: str,
        user_instruction: str | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        if stage not in STAGES or stage == "source":
            raise OrchestrationError(f"Stage cannot be planned: {stage}")
        workspace = self._workspace(workspace_id)
        artifacts = self.db.list_artifacts(workspace_id)
        saved = self.db.get_stage_guidance(workspace_id, stage) or {}
        instruction = str(
            user_instruction if user_instruction is not None else saved.get("user_instruction") or ""
        ).strip()
        memory = self.db.get_workspace_memory(workspace_id)
        provider = self.providers.workflow()
        selected_candidates = self._selected_asset_candidates_for_current_artifacts(workspace_id, artifacts)
        fingerprint = self._guidance_fingerprint(
            workspace, artifacts, stage, instruction, memory,
            self.db.workspace_asset_context(workspace_id),
            selected_candidates,
        )
        if (
            not force
            and saved.get("plan_input_hash") == fingerprint
            and isinstance(saved.get("director_plan"), dict)
            and saved.get("director_plan")
        ):
            cached_plan = dict(saved["director_plan"])
            if stage == "asset_manifest":
                cached_plan = self._guard_asset_manifest_directive(cached_plan, artifacts)
            return {
                "plan": cached_plan,
                "cached": True,
                "input_hash": fingerprint,
            }
        plan = self._call_director(
            provider=provider,
            workspace=workspace,
            artifacts=artifacts,
            stage=stage,
            user_instruction=instruction,
            project_memory=memory,
            existing_instruction=str(saved.get("user_instruction") or ""),
            conversation_history=self.db.list_guidance_messages(workspace_id, limit=20),
            asset_library_context=self.db.workspace_asset_context(workspace_id),
            asset_candidates=selected_candidates,
        )
        self.db.upsert_stage_guidance(
            workspace_id,
            stage,
            user_instruction=instruction,
            director_plan=plan,
            plan_input_hash=fingerprint,
        )
        if instruction:
            self.db.add_guidance_message(
                workspace_id, stage, "user", instruction, {"input_hash": fingerprint}
            )
        self.db.add_guidance_message(
            workspace_id,
            stage,
            "director",
            str(plan.get("interpretation") or plan.get("prompt_addendum") or "执行指令已生成"),
            {"plan": plan, "input_hash": fingerprint},
        )
        self.db.add_event(
            workspace_id,
            "director.plan",
            {
                "stage": stage,
                "has_user_instruction": bool(instruction),
                "parameter_overrides": plan.get("parameter_overrides", {}),
            },
        )
        return {"plan": plan, "cached": False, "input_hash": fingerprint}

    def save_stage_instruction(
        self, workspace_id: str, stage: str, user_instruction: str
    ) -> dict[str, Any]:
        if stage not in STAGES or stage == "source":
            raise OrchestrationError(f"Stage cannot accept guidance: {stage}")
        previous = self.db.get_stage_guidance(workspace_id, stage) or {}
        instruction = user_instruction.strip()
        # Keep an existing plan only when the instruction text is identical.
        same = instruction == str(previous.get("user_instruction") or "")
        return self.db.upsert_stage_guidance(
            workspace_id,
            stage,
            user_instruction=instruction,
            director_plan=previous.get("director_plan") if same else {},
            plan_input_hash=str(previous.get("plan_input_hash") or "") if same else "",
        )

    def _resolve_execution_directive(
        self,
        *,
        provider: Any,
        workspace: dict[str, Any],
        artifacts: list[dict[str, Any]],
        stage: str,
        user_instruction: str,
        project_memory: str,
        saved_guidance: dict[str, Any],
    ) -> dict[str, Any]:
        selected_candidates = self._selected_asset_candidates_for_current_artifacts(workspace["id"], artifacts)
        fingerprint = self._guidance_fingerprint(
            workspace, artifacts, stage, user_instruction, project_memory,
            self.db.workspace_asset_context(workspace["id"]),
            selected_candidates,
        )
        if (
            saved_guidance.get("plan_input_hash") == fingerprint
            and isinstance(saved_guidance.get("director_plan"), dict)
            and saved_guidance.get("director_plan")
        ):
            cached_plan = dict(saved_guidance["director_plan"])
            if stage == "asset_manifest":
                cached_plan = self._guard_asset_manifest_directive(cached_plan, artifacts)
            return cached_plan
        plan = self._call_director(
            provider=provider,
            workspace=workspace,
            artifacts=artifacts,
            stage=stage,
            user_instruction=user_instruction,
            project_memory=project_memory,
            existing_instruction=str(saved_guidance.get("user_instruction") or ""),
            conversation_history=self.db.list_guidance_messages(workspace["id"], limit=20),
            asset_library_context=self.db.workspace_asset_context(workspace["id"]),
            asset_candidates=selected_candidates,
        )
        self.db.upsert_stage_guidance(
            workspace["id"],
            stage,
            user_instruction=user_instruction,
            director_plan=plan,
            plan_input_hash=fingerprint,
        )
        if user_instruction:
            self.db.add_guidance_message(
                workspace["id"], stage, "user", user_instruction, {"input_hash": fingerprint}
            )
        self.db.add_guidance_message(
            workspace["id"],
            stage,
            "director",
            str(plan.get("interpretation") or plan.get("prompt_addendum") or "执行指令已生成"),
            {"plan": plan, "input_hash": fingerprint},
        )
        return plan

    @staticmethod
    def _asset_manifest_contract(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the harness-owned Asset Manifest contract from the approved script.

        Director prose and historical reviews are advisory.  The screenplay contract
        owns canonical identity/type; counts are diagnostics only and never output
        quotas.  Keep this deterministic so a stale director message cannot turn an
        old 12/14 observation into a binding regeneration requirement.
        """
        script_artifact = next(
            (item for item in artifacts if str(item.get("kind") or "") == "script"),
            {},
        )
        script_content = script_artifact.get("content", {}) if isinstance(script_artifact, dict) else {}
        requirements = script_content.get("asset_requirements", []) if isinstance(script_content, dict) else []
        aliases = {
            "character": "character", "person": "character", "role": "character",
            "costume": "character", "wardrobe": "character", "outfit": "character", "clothing": "character",
            "scene": "scene", "location": "scene", "set": "scene",
            "prop": "prop", "object": "prop", "item": "prop",
        }
        required_keys: list[str] = []
        type_counts = {"character": 0, "scene": 0, "prop": 0}
        seen: set[str] = set()
        for index, requirement in enumerate(requirements if isinstance(requirements, list) else []):
            if not isinstance(requirement, dict):
                continue
            key = str(
                requirement.get("canonical_key")
                or requirement.get("canonical_id")
                or f"SCRIPT_ASSET_{index + 1:03d}"
            ).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            required_keys.append(key)
            raw_type = str(requirement.get("asset_type") or requirement.get("type") or "").strip().lower()
            asset_type = aliases.get(raw_type, "")
            if not asset_type:
                upper_key = key.upper()
                if upper_key.startswith("CHAR_"):
                    asset_type = "character"
                elif upper_key.startswith(("LOC_", "SCENE_")):
                    asset_type = "scene"
                elif upper_key.startswith("PROP_"):
                    asset_type = "prop"
            if asset_type in type_counts:
                type_counts[asset_type] += 1
        return {
            "contract_source": "script.asset_requirements",
            "required_keys": required_keys,
            "required_source_requirement_keys": required_keys,
            "required_count": len(required_keys),
            "required_type_counts": type_counts,
            "canonical_asset_types": ["character", "scene", "prop"],
            "identity_policy": "script keys bind through source_requirement_keys; manifest canonical_key is a separate stable production identity",
            "continuity_lock_encoding": "array_of_rules",
            "typed_metadata_required_fields": {
                "character": ["appearance", "costume"],
                "scene": ["key_set_elements"],
                "prop": ["physical_description", "used_in_scenes"],
            },
            "typed_metadata_policy": "closed_required_set; other descriptive fields are optional unless the user explicitly binds them",
            "count_policy": "evidence_derived_not_quota",
        }

    @staticmethod
    def _guard_asset_manifest_directive(
        plan: dict[str, Any], artifacts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Neutralize stale count/taxonomy instructions for Asset Manifest runs.

        The director is still free to carry forward content-specific feedback (for
        example a chandelier that must remain present), but exact historical totals
        and costume/set quotas cannot become acceptance criteria.  This guard runs
        after model planning *and* on cached plans.
        """
        guarded = dict(plan or {})
        contract = Orchestrator._asset_manifest_contract(artifacts)
        marker = "[[HARNESS_ASSET_MANIFEST_CONTRACT]]"

        def strip_old_guard(value: Any) -> str:
            text = str(value or "").strip()
            if marker in text:
                text = text.split(marker, 1)[0].rstrip()
            return text

        quota_re = re.compile(
            r"(?:"
            r"(?:exactly|strictly|must\s+(?:equal|total)|fixed\s+count|total\s+(?:must|shall|=))"
            r"|(?:严格|必须|务必).{0,10}(?:等于|总数|数量|共)"
            r"|(?:共|总数|数量).{0,8}\d+\s*项"
            r"|\d+\s*项.{0,12}(?:资产|清单|items?\[?\]?|manifest|达标)"
            r"|(?:资产|清单|items?\[?\]?|manifest).{0,12}\d+\s*项"
            r"|\b\d+\s*/\s*\d+\b"
            r"|\b\d+\s+(?:costume|costumes|set|sets)\b"
            r")",
            re.IGNORECASE,
        )
        legacy_count_signal_re = re.compile(
            r"(?:\d+\s*(?:项|套|处).{0,10}(?:资产|服装|场景|道具|items?|manifest)|"
            r"(?:资产|服装|场景|道具|items?|manifest).{0,10}\d+\s*(?:项|套|处)|"
            r"\b\d+\s+(?:costume|costumes|set|sets|props?|characters?|scenes?)\b)",
            re.IGNORECASE,
        )

        def strip_quota_lines(value: Any) -> str:
            text = strip_old_guard(value)
            if not text:
                return ""
            kept: list[str] = []
            for line in text.splitlines():
                normalized = line.strip()
                if normalized and quota_re.search(normalized):
                    continue
                kept.append(line)
            return "\n".join(kept).strip()

        contract_note = (
            marker
            + "\nHarness-owned contract (overrides historical review/count wording):\n"
            + "- script.asset_requirements is the minimum SOURCE-REQUIREMENT baseline; each unique screenplay requirement key must be covered exactly once through items[].source_requirement_keys.\n"
            + "- items[].canonical_key is a separate stable production identity and MUST NOT be overwritten by screenplay IDs such as CHAR_*/LOC_*/PROP_* merely to prove coverage.\n"
            + "- Canonical asset_type values are character / scene / prop only. Costume/wardrobe is character metadata; set/location maps to scene.\n"
            + "- Previous manifests are evidence/reference for still-valid fields and extras, not a target count or taxonomy quota.\n"
            + "- Preserve genuinely supported extra reusable scenes/props, reconcile semantic aliases, and do not invent assets just to hit a number.\n"
            + "- Final item count is evidence-derived. No historical total (including 10/12/14 or a 5/5/4 shape) is a binding acceptance criterion.\n"
            + "- continuity_lock canonical encoding is an array of immutable rule strings; historical boolean true/false means locked/unlocked intent only and must never be used as the output data type.\n"
            + "- Typed metadata has a CLOSED required field set: character = appearance + costume; scene = key_set_elements; prop = physical_description + used_in_scenes. Other descriptive fields (for example physical_tags/social_register/lighting_mood/spatial_function/architectural_style/material/dimensions_hint/narrative_function) are optional unless the latest user instruction explicitly names them.\n"
            + "- Validation is structural: no missing source requirements, no duplicate canonical/semantic assets, canonical types only, valid library references, required continuity locks preserved, and the closed required typed-metadata fields are explicitly present with the expected container type.\n"
            + f"- Current screenplay baseline: {json.dumps(contract, ensure_ascii=False)}"
        )

        base_effective = strip_quota_lines(guarded.get("effective_instruction"))
        base_addendum = strip_quota_lines(guarded.get("prompt_addendum"))
        guarded["effective_instruction"] = (base_effective + "\n\n" + contract_note).strip()
        guarded["prompt_addendum"] = (base_addendum + "\n\n" + contract_note).strip()

        interpretation = strip_old_guard(guarded.get("interpretation"))
        if quota_re.search(interpretation):
            interpretation = (
                "按已批准 script.asset_requirements 的 canonical character/scene/prop 基线重新核对当前 Asset Manifest，"
                "保留仍有证据支持的历史有效项与本轮新增项；历史数量与 costume/set 分类仅作旧版本参考，不作为配额。"
            )
        guarded["interpretation"] = interpretation

        criteria = [
            "Every unique script.asset_requirements key is covered exactly once through source_requirement_keys; screenplay IDs are not required to equal manifest canonical_key.",
            "All output asset_type values use only character, scene, or prop; wardrobe is character metadata and set/location is scene.",
            "Manifest canonical_key values are stable normalized production identities, with no duplicate canonical or semantic assets after reconciliation.",
            "continuity_lock uses the canonical array-of-rules encoding; required lock intent and valid REUSE/VARIANT/CREATE library-reference semantics are preserved.",
            "Typed metadata uses the closed required set only: character appearance/costume, scene key_set_elements, prop physical_description/used_in_scenes; no extra schema field is required unless explicitly bound by the user.",
            "Evidence-supported additional reusable scenes/props may remain; final item count is evidence-derived and is not an exact-count quota.",
        ]
        guarded["acceptance_criteria"] = criteria
        warnings = list(guarded.get("warnings") or [])
        warning = "Harness 已将 Asset Manifest 的历史精确数量/legacy taxonomy 约束降级为非绑定参考。"
        if warning not in warnings:
            warnings.append(warning)
        guarded["warnings"] = warnings
        guarded["asset_manifest_contract"] = contract

        reply = strip_old_guard(guarded.get("reply"))
        if quota_re.search(reply) or legacy_count_signal_re.search(reply):
            reply = (
                "Asset Manifest 将以已批准 script.asset_requirements 的 canonical character/scene/prop 基线执行，"
                "继续保留仍有证据支持的历史有效项和本轮明确的内容要求；历史 10/12/14、5/5/4 或 costume/set 统计只作旧版本参考，不作为硬配额。"
            )
        guarded["reply"] = reply
        return guarded

    @staticmethod
    def _call_director(
        *,
        provider: Any,
        workspace: dict[str, Any],
        artifacts: list[dict[str, Any]],
        stage: str,
        user_instruction: str,
        project_memory: str,
        existing_instruction: str = "",
        conversation_history: list[dict[str, Any]] | None = None,
        asset_library_context: list[dict[str, Any]] | None = None,
        asset_candidates: list[dict[str, Any]] | None = None,
        interaction_mode: str = "conversation",
    ) -> dict[str, Any]:
        planner = getattr(provider, "llm", provider)
        plan_method = getattr(planner, "plan_stage", None)
        context = {
            "workspace": workspace,
            "artifacts": artifacts,
            "project_memory": project_memory,
            "user_instruction": user_instruction,
            "existing_instruction": existing_instruction,
            "conversation_history": conversation_history or [],
            "asset_library_context": asset_library_context or [],
            "asset_candidates": asset_candidates or [],
            "interaction_mode": interaction_mode,
        }
        planner_warning = ""
        if callable(plan_method):
            try:
                plan = plan_method(stage, context)
                if isinstance(plan, dict):
                    if stage == "asset_manifest":
                        plan = Orchestrator._guard_asset_manifest_directive(plan, artifacts)
                    return plan
            except Exception as exc:
                planner_warning = f"总管解析失败，已回退为直接使用用户要求：{exc}"
        # Deterministic fallback for mock/offline providers or planner parse failures.
        if interaction_mode == "regenerate_current" and existing_instruction.strip() and user_instruction.strip():
            effective = existing_instruction.strip() + "\n\n本轮修改意见：\n" + user_instruction.strip()
        else:
            effective = user_instruction.strip() or existing_instruction.strip()
        fallback = {
            "stage": stage,
            "reply": "收到。" + (f"我会把这条要求绑定到{STAGE_LABELS.get(stage, stage)}节点。" if effective else "我会按项目现有约束继续。"),
            "effective_instruction": effective,
            "interpretation": effective or "按项目默认要求执行下一步",
            "prompt_addendum": effective,
            "parameter_overrides": {},
            "acceptance_criteria": [],
            "memory_candidates": [],
            "warnings": [planner_warning] if planner_warning else [],
            "questions": [],
            "requires_user_input": False,
            "planner_model": "deterministic-fallback",
        }
        if stage == "asset_manifest":
            fallback = Orchestrator._guard_asset_manifest_directive(fallback, artifacts)
        return fallback

    @staticmethod
    def _guidance_fingerprint(
        workspace: dict[str, Any],
        artifacts: list[dict[str, Any]],
        stage: str,
        user_instruction: str,
        project_memory: str,
        asset_library_context: list[dict[str, Any]] | None = None,
        asset_candidates: list[dict[str, Any]] | None = None,
    ) -> str:
        payload = {
            "stage": stage,
            "instruction": user_instruction,
            "project_memory": project_memory,
            "workspace": {
                "title": workspace.get("title"),
                "brief": workspace.get("brief"),
                "settings": workspace.get("settings"),
            },
            "asset_libraries": [
                {"id": item.get("id"), "updated_at": item.get("updated_at"), "items": len(item.get("items", []))}
                for item in (asset_library_context or [])
            ],
            "selected_asset_candidates": [
                {
                    "id": item.get("id"),
                    "stage": item.get("stage"),
                    "artifact_revision": item.get("artifact_revision"),
                    "canonical_key": item.get("canonical_key"),
                    "updated_at": item.get("updated_at"),
                }
                for item in (asset_candidates or [])
            ],
            "upstream_revisions": [
                {
                    "kind": item.get("kind"),
                    "revision": item.get("revision"),
                    "status": item.get("status"),
                }
                for item in artifacts
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def chat_stage(
        self, workspace_id: str, stage: str, message: str, *, interaction_mode: str = "conversation"
    ) -> dict[str, Any]:
        """Have the director respond to a user turn and bind it to a stage plan."""
        if stage not in STAGES or stage == "source":
            raise OrchestrationError(f"Stage cannot accept director chat: {stage}")
        message = message.strip()
        if not message:
            message = "请结合当前生成结果、项目记忆和工作流状态确认本节点的执行方案；如无阻塞问题，请直接给出可执行指令。"
        workspace = self._workspace(workspace_id)
        artifacts = self.db.list_artifacts(workspace_id)
        saved = self.db.get_stage_guidance(workspace_id, stage) or {}
        memory = self.db.get_workspace_memory(workspace_id)
        history = self.db.list_guidance_messages(workspace_id, limit=24)
        provider = self.providers.workflow()
        selected_candidates = self._selected_asset_candidates_for_current_artifacts(workspace_id, artifacts)
        plan = self._call_director(
            provider=provider,
            workspace=workspace,
            artifacts=artifacts,
            stage=stage,
            user_instruction=message,
            project_memory=memory,
            existing_instruction=str(saved.get("user_instruction") or ""),
            conversation_history=history,
            asset_library_context=self.db.workspace_asset_context(workspace_id),
            asset_candidates=selected_candidates,
            interaction_mode=interaction_mode,
        )
        effective = str(
            plan.get("effective_instruction")
            or plan.get("prompt_addendum")
            or message
        ).strip()
        plan["effective_instruction"] = effective
        fingerprint = self._guidance_fingerprint(
            workspace, artifacts, stage, effective, memory,
            self.db.workspace_asset_context(workspace_id),
            selected_candidates,
        )
        self.db.upsert_stage_guidance(
            workspace_id,
            stage,
            user_instruction=effective,
            director_plan=plan,
            plan_input_hash=fingerprint,
        )
        self.db.add_guidance_message(
            workspace_id,
            stage,
            "user",
            message,
            {"kind": "revision_feedback" if interaction_mode == "regenerate_current" else "conversation", "input_hash": fingerprint, "interaction_mode": interaction_mode},
        )
        reply = str(
            plan.get("reply")
            or plan.get("interpretation")
            or plan.get("prompt_addendum")
            or "已将你的要求绑定到下一次执行。"
        )
        self.db.add_guidance_message(
            workspace_id,
            stage,
            "director",
            reply,
            {"kind": "revision_feedback" if interaction_mode == "regenerate_current" else "conversation", "plan": plan, "input_hash": fingerprint, "interaction_mode": interaction_mode},
        )
        self.db.add_event(
            workspace_id,
            "director.chat",
            {"stage": stage, "requires_user_input": bool(plan.get("requires_user_input")), "interaction_mode": interaction_mode},
        )
        return {
            "stage": stage,
            "reply": reply,
            "effective_instruction": effective,
            "plan": plan,
            "input_hash": fingerprint,
        }

    def prepare_stage_regeneration(
        self, workspace_id: str, stage: str, feedback: str
    ) -> dict[str, Any]:
        """Bind user feedback to a new revision while preserving memory and the old revision.

        The current artifact is marked stale (not deleted), downstream artifacts are
        invalidated, durable project/stage memory and conversation history are kept,
        and the director produces a fresh bound plan specifically for regeneration.
        """
        if stage == "source" or stage not in STAGES:
            raise OrchestrationError(f"Stage cannot be regenerated: {stage}")
        feedback = str(feedback or "").strip()
        if not feedback:
            raise OrchestrationError("请先告诉总管你对当前产物哪里不满意，以及希望怎么改")
        self._workspace(workspace_id)
        active = self.db.list_active_jobs(workspace_id)
        if active:
            job = active[0]
            raise OrchestrationError(
                f"{STAGE_LABELS.get(str(job.get('stage')), job.get('stage'))}仍在运行，请等待任务结束后再反馈重生成"
            )
        current = self.db.get_artifact(workspace_id, stage)
        if not current:
            raise OrchestrationError(f"当前还没有可重生成的{STAGE_LABELS.get(stage, stage)}产物")

        downstream = self.downstream_stages(stage, include_self=False)
        self.db.mark_artifacts_stale(workspace_id, [stage])
        removed = self.db.delete_artifacts(workspace_id, downstream)
        removed_candidates = self.db.delete_asset_candidates_for_stages(workspace_id, downstream)
        removed_jobs = self.db.clear_terminal_jobs_for_stages(workspace_id, downstream)
        affected = [stage, *downstream]
        self.db.clear_stage_guidance_plans(workspace_id, affected)
        self.db.clear_stage_reviews(workspace_id, affected)

        reset_gates: list[str] = []
        for gate_stage, gate_name in GATES.items():
            if gate_stage in affected:
                self.db.set_approval(
                    workspace_id,
                    gate_name,
                    "pending",
                    f"收到对{STAGE_LABELS[stage]}的修改意见，等待新 revision 后重新确认",
                )
                reset_gates.append(gate_name)

        self.db.add_event(
            workspace_id,
            "stage.regeneration_requested",
            {
                "stage": stage,
                "previous_revision": current.get("revision"),
                "downstream_cleared": downstream,
            },
        )
        chat = self.chat_stage(
            workspace_id, stage, feedback, interaction_mode="regenerate_current"
        )
        return {
            **chat,
            "previous_revision": current.get("revision"),
            "downstream_cleared": removed,
            "removed_jobs": removed_jobs,
            "removed_visual_candidates": len(removed_candidates),
            "reset_gates": reset_gates,
            "memory_preserved": True,
            "conversation_preserved": True,
        }

    def validate_bound_plan(
        self, workspace_id: str, stage: str, input_hash: str
    ) -> dict[str, Any]:
        workspace = self._workspace(workspace_id)
        artifacts = self.db.list_artifacts(workspace_id)
        saved = self.db.get_stage_guidance(workspace_id, stage) or {}
        memory = self.db.get_workspace_memory(workspace_id)
        effective = str(saved.get("user_instruction") or "")
        expected = self._guidance_fingerprint(
            workspace, artifacts, stage, effective, memory,
            self.db.workspace_asset_context(workspace_id)
        )
        valid = bool(
            input_hash
            and input_hash == expected
            and saved.get("plan_input_hash") == expected
            and isinstance(saved.get("director_plan"), dict)
            and saved.get("director_plan")
        )
        return {"valid": valid, "expected_hash": expected, "guidance": saved}

    def downstream_stages(self, stage: str, *, include_self: bool = False) -> list[str]:
        if stage not in STAGES:
            raise OrchestrationError(f"Unknown artifact kind: {stage}")
        downstream: set[str] = set()
        frontier = [stage]
        while frontier:
            current = frontier.pop()
            for candidate, requirements in REQUIRES.items():
                if current in requirements and candidate not in downstream:
                    downstream.add(candidate)
                    frontier.append(candidate)
        selected = set(downstream)
        if include_self:
            selected.add(stage)
        return [item for item in STAGES if item in selected]

    def reset_stage(self, workspace_id: str, stage: str) -> dict[str, Any]:
        """Clear one stage and its dependency descendants, never its upstream evidence."""
        if stage == "source":
            raise OrchestrationError("素材是工作区根节点，不能用区域清理删除；请重新上传素材或新建工作区")
        if stage not in STAGES:
            raise OrchestrationError(f"Unknown artifact kind: {stage}")
        self._workspace(workspace_id)
        affected = self.downstream_stages(stage, include_self=True)
        active = self.db.list_active_jobs(workspace_id)
        if active:
            job = active[0]
            raise OrchestrationError(
                f"{STAGE_LABELS.get(str(job.get('stage')), job.get('stage'))}仍在运行，不能清理该区域"
            )
        removed = self.db.delete_artifacts(workspace_id, affected)
        removed_candidates = self.db.delete_asset_candidates_for_stages(workspace_id, affected)
        removed_jobs = self.db.clear_terminal_jobs_for_stages(workspace_id, affected)
        self.db.clear_stage_guidance_plans(workspace_id, affected)
        self.db.clear_stage_reviews(workspace_id, affected)
        reset_gates: list[str] = []
        for gate_stage, gate_name in GATES.items():
            if gate_stage in affected:
                self.db.set_approval(
                    workspace_id,
                    gate_name,
                    "pending",
                    f"{STAGE_LABELS[stage]}区域已清理，需重新生成并确认",
                )
                reset_gates.append(gate_name)
        remaining = {
            artifact["kind"]: artifact
            for artifact in self.db.list_artifacts(workspace_id)
            if artifact.get("status") == "ready"
        }
        latest = "source"
        for candidate in STAGES:
            if candidate in remaining:
                latest = candidate
        self.db.update_workspace(workspace_id, stage=latest, status="active")
        self.db.add_guidance_message(
            workspace_id,
            stage,
            "system",
            f"已清理{STAGE_LABELS[stage]}及其下游区域：" + "、".join(STAGE_LABELS[item] for item in affected),
            {"kind": "stage_reset", "affected": affected},
        )
        return {
            "status": "cleared",
            "stage": stage,
            "affected": affected,
            "removed_artifacts": removed,
            "removed_jobs": removed_jobs,
            "removed_visual_candidates": len(removed_candidates),
            "reset_gates": reset_gates,
            "preserved_upstream": [item for item in STAGES if item not in affected],
        }

    @staticmethod
    def _guard_asset_manifest_review(
        review: dict[str, Any],
        artifact: dict[str, Any],
        execution_directive: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep LLM review subordinate to deterministic Asset Manifest validation.

        Kimi may still review semantic/content-specific requirements (for example a
        chandelier explicitly requested by the user), but it must not invent a
        second schema after the harness validator has passed.  Structural claims
        about keys, taxonomy, lock encoding, or typed metadata are owned by
        ``manifest_validation``.
        """
        guarded = dict(review or {})
        content = artifact.get("content") if isinstance(artifact, dict) else None
        validation = content.get("manifest_validation") if isinstance(content, dict) else None
        if not isinstance(validation, dict) or str(validation.get("status") or "").lower() != "pass":
            return guarded

        structural_markers = (
            "schema", "字段", "结构化元数据", "typed metadata", "typed_metadata",
            "canonical_key", "source_requirement", "continuity_lock",
            "manifest_validation", "asset_type", "taxonomy", "数据类型",
            "命名规范", "非法类型", "illegal_asset_type", "duplicate_canonical",
            "required_baseline_coverage", "physical_tags", "social_register",
            "lighting_mood", "spatial_function", "architectural_style",
            "dimensions_hint", "narrative_function",
        )

        deviations = guarded.get("deviations")
        if not isinstance(deviations, list):
            deviations = []
        kept: list[Any] = []
        suppressed: list[Any] = []
        for deviation in deviations:
            try:
                blob = json.dumps(deviation, ensure_ascii=False).casefold()
            except TypeError:
                blob = str(deviation).casefold()
            if any(marker.casefold() in blob for marker in structural_markers):
                suppressed.append(deviation)
            else:
                kept.append(deviation)
        guarded["deviations"] = kept

        # If deterministic validation passed, an LLM-only structural complaint may
        # not force a regeneration loop.  Semantic/content deviations remain free
        # to do so.
        if suppressed and not kept:
            guarded["recommended_action"] = "proceed"
            guarded["assessment"] = (
                "Harness deterministic Asset Manifest validation passed. "
                "The reviewer's schema-only deviations were suppressed because they "
                "contradict the harness-owned contract."
            )
            guarded["reply"] = (
                "Asset Manifest 已通过 Harness 的确定性结构校验；本轮总管提出的额外字段要求不属于绑定 schema，"
                "因此不会触发重复重生成。可以继续检查资产内容并进入下一阶段。"
            )
            guarded["suggested_adjustments"] = []

        guarded["harness_review_guard"] = {
            "manifest_validation_status": "pass",
            "structural_authority": "artifact.content.manifest_validation",
            "suppressed_structural_deviations": suppressed,
            "remaining_semantic_deviations": kept,
            "typed_metadata_required_fields": (execution_directive.get("asset_manifest_contract") or {}).get(
                "typed_metadata_required_fields",
                {
                    "character": ["appearance", "costume"],
                    "scene": ["key_set_elements"],
                    "prop": ["physical_description", "used_in_scenes"],
                },
            ),
        }
        return guarded

    @staticmethod
    def _guard_reference_images_review(
        review: dict[str, Any], artifact: dict[str, Any]
    ) -> dict[str, Any]:
        """Keep reference-image review subordinate to deterministic coverage validation.

        The reviewer receives a compact artifact preview and must not manufacture a
        missing-item loop when the provider's complete deterministic validation has
        already passed.  This is intentionally project-agnostic: no character names,
        episode counts or specific combinations are hard-coded here.
        """
        guarded = dict(review or {})
        content = artifact.get("content") if isinstance(artifact, dict) else None
        validation = content.get("reference_validation") if isinstance(content, dict) else None
        if not isinstance(validation, dict) or str(validation.get("status") or "").lower() != "pass":
            return guarded

        structural_markers = (
            "source_kind", "source_id", "source_ids", "selected_candidate", "upstream_adopted",
            "reference_validation", "selection_coverage", "isolated_completed", "isolated_expected",
            "combination_completed", "combination_expected", "missing_planned_combinations",
            "unexpected_combinations", "duplicate_reference_keys", "invalid_source_kinds",
            "items total", "total items", "item count", "items 数量", "schema", "结构性缺失",
        )
        deviations = guarded.get("deviations")
        if not isinstance(deviations, list):
            deviations = []
        kept: list[Any] = []
        suppressed: list[Any] = []
        for deviation in deviations:
            try:
                blob = json.dumps(deviation, ensure_ascii=False).casefold()
            except TypeError:
                blob = str(deviation).casefold()
            if any(marker.casefold() in blob for marker in structural_markers):
                suppressed.append(deviation)
            else:
                kept.append(deviation)
        guarded["deviations"] = kept

        if suppressed and not kept:
            guarded["recommended_action"] = "proceed"
            guarded["assessment"] = (
                "Harness deterministic reference-image validation passed; reviewer-only "
                "coverage/schema deviations were suppressed."
            )
            guarded["reply"] = (
                "参考图已通过 Harness 的确定性覆盖与绑定校验。总管复盘中与实际完整产物冲突的"
                "缺失项/数量/绑定结构判断已被抑制；可以继续进行视觉确认。"
            )
            guarded["suggested_adjustments"] = []

        guarded["harness_review_guard"] = {
            "reference_validation_status": "pass",
            "structural_authority": "artifact.content.reference_validation",
            "suppressed_structural_deviations": suppressed,
            "remaining_nonstructural_deviations": kept,
        }
        return guarded

    def _post_generation_review(
        self,
        *,
        provider: Any,
        workspace: dict[str, Any],
        stage: str,
        artifact: dict[str, Any],
        user_instruction: str,
        project_memory: str,
        execution_directive: dict[str, Any],
    ) -> None:
        planner = getattr(provider, "llm", provider)
        review_method = getattr(planner, "review_stage_result", None)
        recent = self.db.list_guidance_messages(workspace["id"], limit=18)
        context = {
            "workspace": workspace,
            "project_memory": project_memory,
            "user_instruction": user_instruction,
            "execution_directive": execution_directive,
            "artifact": artifact,
            "conversation_history": recent,
            "next_actions": self.next_actions(workspace["id"]),
            "asset_candidates": self._selected_asset_candidates_for_current_artifacts(
                workspace["id"], self.db.list_artifacts(workspace["id"])
            ),
        }
        review: dict[str, Any] = {}
        if callable(review_method):
            try:
                candidate = review_method(stage, context)
                if isinstance(candidate, dict):
                    review = candidate
            except Exception as exc:
                review = {"warning": str(exc)}
        if stage == "asset_manifest" and review:
            review = self._guard_asset_manifest_review(review, artifact, execution_directive)
        elif stage == "reference_images" and review:
            review = self._guard_reference_images_review(review, artifact)
        reply = str(review.get("reply") or "").strip()
        if not reply:
            next_actions = context["next_actions"]
            next_label = next_actions[0].get("label") if next_actions else "检查最终产物"
            reply = (
                f"{STAGE_LABELS[stage]}已生成。我已把本次用户要求和产物结果记录下来。"
                f"建议下一步：{next_label}。"
            )
            if review.get("warning"):
                reply += f" 总管复盘调用失败，但不影响已生成产物：{review['warning']}"
        self.db.set_stage_review(
            workspace["id"], stage, int(artifact.get("revision") or 0), review
        )
        self.db.add_guidance_message(
            workspace["id"],
            stage,
            "director",
            reply,
            {"kind": "post_generation_review", "review": review},
        )
        self.db.add_event(
            workspace["id"],
            "director.review",
            {"stage": stage, "artifact_revision": artifact.get("revision"), "recommended_action": review.get("recommended_action"), "assessment": review.get("assessment", "")},
        )

    @staticmethod
    def _normalize_asset_manifest(
        content: dict[str, Any],
        asset_library_context: list[dict[str, Any]],
        *,
        script_content: dict[str, Any] | None = None,
        previous_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reconcile model output into the stable production Asset Manifest schema.

        The approved screenplay owns *requirement coverage*, not the final manifest
        key namespace.  ``script.asset_requirements`` keys are therefore recorded in
        ``source_requirement_keys`` while ``canonical_key`` remains the stable
        character/scene/prop identity used by downstream production and series
        reuse.  This separation prevents screenplay IDs such as ``LOC_FOYER`` from
        overwriting a normalized manifest identity such as ``scene_foyer``.

        Previous and generated manifests may contain the legacy costume/set taxonomy
        or historical canonical keys.  Exact requirement bindings are preferred, then
        deterministic normalized semantic identity is used as a migration fallback.
        Regeneration remains monotonic for genuinely supported extra scenes/props,
        but semantic aliases are coalesced rather than duplicated.
        """
        result = dict(content or {})
        script_content = script_content if isinstance(script_content, dict) else {}
        previous_manifest = previous_manifest if isinstance(previous_manifest, dict) else {}

        library_items = [
            item
            for library in (asset_library_context or [])
            for item in (library.get("items") or [])
            if isinstance(item, dict)
        ]
        valid_ids = {str(item.get("id")) for item in library_items if item.get("id")}
        library_by_key: dict[str, dict[str, Any]] = {}
        for item in library_items:
            key = str(item.get("canonical_key") or "").strip()
            if key and key not in library_by_key:
                library_by_key[key] = item

        legacy_costume_types = {"costume", "wardrobe", "outfit", "clothing"}

        def normalize_type(value: Any, canonical_key: str = "") -> str:
            raw = str(value or "").strip().lower()
            aliases = {
                "character": "character", "person": "character", "role": "character",
                "costume": "character", "wardrobe": "character", "outfit": "character", "clothing": "character",
                "scene": "scene", "location": "scene", "set": "scene",
                "prop": "prop", "object": "prop", "item": "prop",
            }
            if raw in aliases:
                return aliases[raw]
            key = str(canonical_key or "").upper()
            if key.startswith("CHAR_"):
                return "character"
            if key.startswith("LOC_") or key.startswith("SCENE_") or key.startswith("SET_"):
                return "scene"
            if key.startswith("PROP_"):
                return "prop"
            return ""

        def normalize_identity(value: Any) -> str:
            text = unicodedata.normalize("NFKD", str(value or ""))
            text = "".join(ch for ch in text if not unicodedata.combining(ch))
            text = text.casefold().replace("’", "'").replace("‘", "'").replace("`", "'")
            text = re.sub(r"[^a-z0-9]+", " ", text)
            return " ".join(text.split())

        def slug(value: Any) -> str:
            text = unicodedata.normalize("NFKD", str(value or ""))
            text = "".join(ch for ch in text if not unicodedata.combining(ch))
            text = text.casefold().replace("’", "'").replace("‘", "'").replace("`", "'")
            text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
            return re.sub(r"_+", "_", text)

        def lock_list(value: Any, *, true_fallback: list[str] | None = None) -> list[str]:
            fallback = [str(x).strip() for x in (true_fallback or []) if str(x).strip()]
            if isinstance(value, bool):
                if not value:
                    return []
                return fallback or ["identity"]
            if isinstance(value, (list, tuple, set)):
                return [str(x).strip() for x in value if str(x).strip()]
            if value is None:
                return []
            text = str(value).strip()
            lowered = text.casefold()
            if lowered in {"false", "no", "off", "0", "none", "null", "[]"}:
                return []
            if lowered in {"true", "yes", "on", "1"}:
                return fallback or ["identity"]
            return [text] if text else []

        def merge_locks(*values: list[str]) -> list[str]:
            merged: list[str] = []
            for value in values:
                for lock in value:
                    lock = str(lock).strip()
                    if lock and lock not in merged:
                        merged.append(lock)
            if len(merged) > 1 and "identity" in merged:
                merged.remove("identity")
            return merged

        def list_value(value: Any) -> list[Any]:
            if value is None:
                return []
            return list(value) if isinstance(value, (list, tuple, set)) else [value]

        def source_key_list(value: Any) -> list[str]:
            output: list[str] = []
            for raw in list_value(value):
                key = str(raw or "").strip()
                if key and key not in output:
                    output.append(key)
            return output

        def item_key(item: dict[str, Any]) -> str:
            return str(item.get("canonical_key") or item.get("canonical_id") or item.get("manifest_id") or "").strip()

        def item_identity(item: dict[str, Any]) -> str:
            for field in ("script_identity", "canonical_name", "name"):
                normalized = normalize_identity(item.get(field))
                if normalized:
                    return normalized
            return ""

        def item_signature(item: dict[str, Any]) -> tuple[str, str]:
            asset_type = str(item.get("asset_type") or "")
            identity = item_identity(item)
            return asset_type, identity or normalize_identity(item_key(item))

        def is_native_manifest_key(key: Any, asset_type: str) -> bool:
            key_text = str(key or "").strip()
            if not key_text or key_text != key_text.casefold():
                return False
            prefixes = {"character": "char_", "scene": "scene_", "prop": "prop_"}
            prefix = prefixes.get(asset_type, "")
            return bool(prefix and key_text.startswith(prefix) and len(key_text) > len(prefix))

        def requirement_fallback_key(source_requirement_key: str, asset_type: str, identity: str = "") -> str:
            raw = str(source_requirement_key or "").strip()
            upper = raw.upper()
            suffix = ""
            if asset_type == "character" and upper.startswith("CHAR_"):
                suffix = raw[5:]
            elif asset_type == "scene" and upper.startswith("LOC_"):
                suffix = raw[4:]
            elif asset_type == "scene" and upper.startswith("SCENE_"):
                suffix = raw[6:]
            elif asset_type == "scene" and upper.startswith("SET_"):
                suffix = raw[4:]
            elif asset_type == "prop" and upper.startswith("PROP_"):
                suffix = raw[5:]
            suffix_slug = slug(suffix or identity or raw)
            prefix = {"character": "char", "scene": "scene", "prop": "prop"}.get(asset_type, "asset")
            return f"{prefix}_{suffix_slug or 'unnamed'}"

        def choose_manifest_key(
            *,
            asset_type: str,
            source_requirement_key: str,
            identity: str,
            generated_item: dict[str, Any] | None,
            previous_item: dict[str, Any] | None,
        ) -> str:
            # The current generator gets first chance to provide a normalized stable
            # manifest identity. Historical native keys are second; screenplay IDs
            # are only a deterministic fallback and are stored separately below.
            for candidate in (generated_item, previous_item):
                key = item_key(candidate or {})
                if is_native_manifest_key(key, asset_type):
                    return key
            return requirement_fallback_key(source_requirement_key, asset_type, identity)

        def choose_manifest_id(
            asset_type: str,
            canonical_key: str,
            *candidates: dict[str, Any] | None,
        ) -> str:
            ids = [
                str((candidate or {}).get("manifest_id") or "").strip()
                for candidate in candidates
                if isinstance(candidate, dict)
            ]
            ids = [value for value in ids if value]
            for value in ids:
                lowered = value.casefold()
                if canonical_key.casefold() in lowered and f"-{asset_type}-" in lowered:
                    return value
            # Preserve the workspace/episode prefix from legacy IDs when possible,
            # but rewrite stale costume/set/location identity into the canonical type.
            legacy_type = r"(?:character|scene|prop|costume|wardrobe|set|location)"
            for value in ids:
                match = re.match(rf"^(.*?)-{legacy_type}-.+$", value, flags=re.IGNORECASE)
                if match and match.group(1):
                    return f"{match.group(1)}-{asset_type}-{canonical_key}"
            return ids[0] if ids else f"{asset_type}-{canonical_key}"

        def text_value(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, (list, tuple, set)):
                return "; ".join(str(x).strip() for x in value if str(x).strip())
            if isinstance(value, dict):
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            return str(value).strip()

        def enrich_typed_metadata(item: dict[str, Any]) -> dict[str, Any]:
            item = dict(item)
            asset_type = str(item.get("asset_type") or "")
            generation_text = text_value(item.get("generation_requirements"))
            if asset_type == "character":
                appearance = text_value(item.get("appearance")) or text_value(item.get("description")) or generation_text
                costume = text_value(item.get("costume")) or text_value(item.get("wardrobe")) or generation_text
                item["appearance"] = appearance
                item["costume"] = costume
            elif asset_type == "scene":
                elements = list_value(
                    item.get("key_set_elements")
                    or item.get("set_elements")
                    or item.get("generation_requirements")
                )
                item["key_set_elements"] = [str(x).strip() for x in elements if str(x).strip()]
            elif asset_type == "prop":
                physical = (
                    text_value(item.get("physical_description"))
                    or text_value(item.get("description"))
                    or generation_text
                    or text_value(item.get("script_identity"))
                )
                item["physical_description"] = physical
                used = source_key_list(item.get("used_in_scenes") or item.get("used_in"))
                item["used_in_scenes"] = used
            return item

        def sanitize_item(raw: Any, index: int) -> dict[str, Any] | None:
            if not isinstance(raw, dict):
                return None
            item = dict(raw)
            key = item_key(item)
            raw_type = str(item.get("asset_type") or item.get("type") or "").strip().lower()
            asset_type = normalize_type(raw_type, key)
            if not asset_type:
                return None
            decision = str(item.get("decision") or "CREATE").upper()
            if decision not in {"REUSE", "VARIANT", "CREATE"}:
                decision = "CREATE"
            library_id = str(item.get("library_asset_id") or "").strip()
            if decision in {"REUSE", "VARIANT"} and library_id not in valid_ids:
                reason = str(item.get("reason") or "").strip()
                item["reason"] = (reason + " | referenced library asset not found; downgraded to CREATE").strip(" |")
                decision, library_id = "CREATE", ""
            if decision == "CREATE":
                library_id = ""
            item["decision"] = decision
            item["library_asset_id"] = library_id or None
            item["asset_type"] = asset_type
            item.setdefault("manifest_id", f"ASSET_{index+1:03d}")
            item.setdefault("canonical_key", key or item.get("manifest_id"))
            item["source_requirement_keys"] = source_key_list(
                item.get("source_requirement_keys") or item.get("source_requirement_key")
            )
            item["continuity_lock"] = lock_list(item.get("continuity_lock"))
            item["variant_requirements"] = list_value(item.get("variant_requirements"))
            item["generation_requirements"] = list_value(item.get("generation_requirements"))
            item["_legacy_costume"] = raw_type in legacy_costume_types
            return enrich_typed_metadata(item)

        generated = [
            item for i, raw in enumerate(result.get("items") or [])
            if (item := sanitize_item(raw, i)) is not None
        ]
        previous = [
            item for i, raw in enumerate(previous_manifest.get("items") or [])
            if (item := sanitize_item(raw, i)) is not None
        ]

        raw_requirements = [x for x in (script_content.get("asset_requirements") or []) if isinstance(x, dict)]
        requirements: list[dict[str, Any]] = []
        requirement_by_key: dict[str, dict[str, Any]] = {}
        duplicate_script_requirement_keys: list[str] = []
        for index, raw_requirement in enumerate(raw_requirements):
            requirement = dict(raw_requirement)
            source_requirement_key = str(
                requirement.get("canonical_key")
                or requirement.get("canonical_id")
                or f"SCRIPT_ASSET_{index + 1:03d}"
            ).strip()
            if not source_requirement_key:
                continue
            requirement["_source_requirement_key"] = source_requirement_key
            existing = requirement_by_key.get(source_requirement_key)
            if existing is None:
                requirement_by_key[source_requirement_key] = requirement
                requirements.append(requirement)
                continue
            if source_requirement_key not in duplicate_script_requirement_keys:
                duplicate_script_requirement_keys.append(source_requirement_key)
            existing["continuity_lock"] = merge_locks(
                lock_list(existing.get("continuity_lock")),
                lock_list(requirement.get("continuity_lock")),
            )
            merged_used_in: list[Any] = []
            for value in [*list_value(existing.get("used_in")), *list_value(requirement.get("used_in"))]:
                if value not in merged_used_in:
                    merged_used_in.append(value)
            if merged_used_in:
                existing["used_in"] = merged_used_in
            if not existing.get("preferred_library_asset_id") and requirement.get("preferred_library_asset_id"):
                existing["preferred_library_asset_id"] = requirement.get("preferred_library_asset_id")

        baseline_source_keys: list[str] = []
        baseline_required_locks: dict[str, list[str]] = {}
        reconciled: list[dict[str, Any]] = []
        consumed_previous: set[int] = set()
        consumed_generated: set[int] = set()
        semantic_alias_matches: list[dict[str, str]] = []

        def match_candidate(
            pool: list[dict[str, Any]],
            consumed: set[int],
            *,
            source_requirement_key: str,
            asset_type: str,
            identity: str,
        ) -> tuple[dict[str, Any] | None, str]:
            exact = [
                item for item in pool
                if id(item) not in consumed
                and str(item.get("asset_type") or "") == asset_type
                and (
                    source_requirement_key in source_key_list(item.get("source_requirement_keys"))
                    or item_key(item) == source_requirement_key
                )
            ]
            if exact:
                return exact[-1], "source_requirement_key"
            normalized_identity = normalize_identity(identity)
            if not normalized_identity:
                return None, ""
            semantic = [
                item for item in pool
                if id(item) not in consumed
                and str(item.get("asset_type") or "") == asset_type
                and item_identity(item) == normalized_identity
            ]
            if len(semantic) == 1:
                return semantic[0], "semantic_identity"
            return None, ""

        for index, req in enumerate(requirements):
            source_requirement_key = str(
                req.get("_source_requirement_key")
                or req.get("canonical_key")
                or req.get("canonical_id")
                or ""
            ).strip()
            if not source_requirement_key:
                source_requirement_key = f"SCRIPT_ASSET_{index+1:03d}"
            asset_type = normalize_type(req.get("asset_type") or req.get("type"), source_requirement_key)
            if not asset_type:
                continue
            baseline_source_keys.append(source_requirement_key)
            canonical_identity = str(req.get("canonical_name") or req.get("name") or source_requirement_key)
            required_locks = lock_list(req.get("continuity_lock"))
            baseline_required_locks[source_requirement_key] = required_locks
            fallback_key = requirement_fallback_key(source_requirement_key, asset_type, canonical_identity)
            seed = {
                "manifest_id": str(req.get("manifest_id") or f"{asset_type}-{fallback_key}"),
                "asset_type": asset_type,
                "script_identity": canonical_identity,
                "canonical_key": fallback_key,
                "source_requirement_keys": [source_requirement_key],
                "source_function": str(req.get("source_function") or req.get("function") or ""),
                "used_in": list(req.get("used_in") or []),
                "continuity_lock": required_locks,
                "decision": "CREATE",
                "library_asset_id": None,
                "confidence": 0.0,
                "reason": "required by approved screenplay asset_requirements",
                "variant_requirements": [],
                "generation_requirements": [],
            }

            prev_item, prev_match = match_candidate(
                previous, consumed_previous,
                source_requirement_key=source_requirement_key,
                asset_type=asset_type,
                identity=canonical_identity,
            )
            gen_item, gen_match = match_candidate(
                generated, consumed_generated,
                source_requirement_key=source_requirement_key,
                asset_type=asset_type,
                identity=canonical_identity,
            )
            if prev_item is not None:
                consumed_previous.add(id(prev_item))
            if gen_item is not None:
                consumed_generated.add(id(gen_item))

            canonical_key = choose_manifest_key(
                asset_type=asset_type,
                source_requirement_key=source_requirement_key,
                identity=canonical_identity,
                generated_item=gen_item,
                previous_item=prev_item,
            )
            preferred_id = str(req.get("preferred_library_asset_id") or "").strip()
            exact_library = library_by_key.get(canonical_key) or library_by_key.get(source_requirement_key)
            if preferred_id and preferred_id in valid_ids:
                seed.update({"decision": "REUSE", "library_asset_id": preferred_id, "confidence": 1.0, "reason": "screenplay preferred_library_asset_id exists"})
            elif exact_library and exact_library.get("id"):
                seed.update({"decision": "REUSE", "library_asset_id": str(exact_library["id"]), "confidence": 1.0, "reason": "exact canonical_key match in linked asset library"})

            merged = dict(seed)
            if prev_item is not None:
                merged.update(prev_item)
            if gen_item is not None:
                merged.update(gen_item)
            merged["canonical_key"] = canonical_key
            merged["manifest_id"] = choose_manifest_id(asset_type, canonical_key, gen_item, prev_item, seed)
            merged["asset_type"] = asset_type
            merged["script_identity"] = canonical_identity
            source_keys = [source_requirement_key]
            for candidate in (prev_item, gen_item):
                for value in source_key_list((candidate or {}).get("source_requirement_keys")):
                    if value not in source_keys:
                        source_keys.append(value)
            merged["source_requirement_keys"] = source_keys
            merged["continuity_lock"] = merge_locks(
                required_locks,
                lock_list((prev_item or {}).get("continuity_lock"), true_fallback=required_locks),
                lock_list((gen_item or {}).get("continuity_lock"), true_fallback=required_locks),
            )
            merged = sanitize_item(merged, len(reconciled)) or seed
            merged["canonical_key"] = canonical_key
            merged["manifest_id"] = choose_manifest_id(asset_type, canonical_key, gen_item, prev_item, merged)
            merged["source_requirement_keys"] = source_keys
            merged = enrich_typed_metadata(merged)
            merged.pop("_legacy_costume", None)
            reconciled.append(merged)

            for source, item, match_kind in (
                ("previous", prev_item, prev_match),
                ("generated", gen_item, gen_match),
            ):
                if item is not None and item_key(item) != canonical_key:
                    semantic_alias_matches.append({
                        "source": source,
                        "match_kind": match_kind,
                        "source_requirement_key": source_requirement_key,
                        "legacy_key": item_key(item),
                        "canonical_key": canonical_key,
                    })

        # Keep genuinely additive assets while coalescing previous/current semantic
        # aliases. Generated native keys win for extras so a current normalized
        # ``scene_couloir_rdc`` can migrate an older ``set_ground_floor_corridor``.
        output_key_index = {item_key(item): i for i, item in enumerate(reconciled) if item_key(item)}
        output_sig_index = {item_signature(item): i for i, item in enumerate(reconciled) if item_signature(item)[1]}
        baseline_length = len(reconciled)

        def normalize_extra_key(item: dict[str, Any]) -> str:
            key = item_key(item)
            asset_type = str(item.get("asset_type") or "")
            if is_native_manifest_key(key, asset_type):
                return key
            lowered = key.casefold()
            if asset_type == "scene":
                for prefix in ("set_", "loc_", "location_"):
                    if lowered.startswith(prefix):
                        suffix = slug(key[len(prefix):])
                        return f"scene_{suffix or 'unnamed'}"
            if asset_type == "character" and lowered.startswith("char_"):
                return lowered
            if asset_type == "prop" and lowered.startswith("prop_"):
                return lowered
            return key

        for source, pool, consumed in (
            ("previous", previous, consumed_previous),
            ("generated", generated, consumed_generated),
        ):
            for item in pool:
                if id(item) in consumed or item.get("_legacy_costume"):
                    continue
                clean = dict(item)
                clean.pop("_legacy_costume", None)
                key = normalize_extra_key(clean)
                if not key:
                    continue
                clean["canonical_key"] = key
                clean["source_requirement_keys"] = source_key_list(clean.get("source_requirement_keys"))
                clean["manifest_id"] = choose_manifest_id(str(clean.get("asset_type") or ""), key, clean)
                clean = enrich_typed_metadata(clean)
                signature = item_signature(clean)
                existing_index = output_key_index.get(key)
                if existing_index is None and signature[1]:
                    existing_index = output_sig_index.get(signature)
                if existing_index is not None:
                    if existing_index < baseline_length:
                        continue
                    if source == "generated":
                        existing = dict(reconciled[existing_index])
                        incoming_key = item_key(clean)
                        existing_key = item_key(existing)
                        updated = dict(existing)
                        updated.update(clean)
                        if is_native_manifest_key(incoming_key, str(clean.get("asset_type") or "")):
                            stable_key = incoming_key
                        else:
                            stable_key = existing_key
                        updated["canonical_key"] = stable_key
                        updated["manifest_id"] = choose_manifest_id(str(updated.get("asset_type") or ""), stable_key, clean, existing)
                        merged_source_keys = source_key_list(existing.get("source_requirement_keys"))
                        for value in source_key_list(clean.get("source_requirement_keys")):
                            if value not in merged_source_keys:
                                merged_source_keys.append(value)
                        updated["source_requirement_keys"] = merged_source_keys
                        updated = sanitize_item(updated, existing_index) or updated
                        updated["canonical_key"] = stable_key
                        updated["source_requirement_keys"] = merged_source_keys
                        updated = enrich_typed_metadata(updated)
                        updated.pop("_legacy_costume", None)
                        reconciled[existing_index] = updated
                        if existing_key != stable_key:
                            output_key_index.pop(existing_key, None)
                            output_key_index[stable_key] = existing_index
                        if signature[1]:
                            output_sig_index[signature] = existing_index
                    continue
                reconciled.append(clean)
                output_key_index[key] = len(reconciled) - 1
                if signature[1]:
                    output_sig_index[signature] = len(reconciled) - 1

        counts = {"REUSE": 0, "VARIANT": 0, "CREATE": 0}
        type_counts = {"character": 0, "scene": 0, "prop": 0}
        for item in reconciled:
            decision = str(item.get("decision") or "CREATE")
            counts[decision] = counts.get(decision, 0) + 1
            asset_type = str(item.get("asset_type") or "")
            if asset_type in type_counts:
                type_counts[asset_type] += 1

        output_keys = [item_key(item) for item in reconciled if item_key(item)]
        duplicate_canonical_keys = sorted({key for key in output_keys if output_keys.count(key) > 1})
        invalid_asset_types = sorted({
            str(item.get("asset_type") or "")
            for item in reconciled
            if str(item.get("asset_type") or "") not in {"character", "scene", "prop"}
        })

        coverage: dict[str, list[dict[str, Any]]] = {key: [] for key in baseline_source_keys}
        for item in reconciled:
            for source_key in source_key_list(item.get("source_requirement_keys")):
                if source_key in coverage:
                    coverage[source_key].append(item)
        missing_baseline = [key for key in baseline_source_keys if not coverage.get(key)]
        duplicate_requirement_coverage = sorted(
            key for key, items in coverage.items() if len(items) > 1
        )

        continuity_lock_violations: list[str] = []
        for source_key, required_locks in baseline_required_locks.items():
            if not required_locks:
                continue
            matching = coverage.get(source_key) or []
            if not matching:
                continue
            actual_locks = lock_list(matching[0].get("continuity_lock"))
            if any(lock not in actual_locks for lock in required_locks):
                continuity_lock_violations.append(source_key)

        signature_map: dict[tuple[str, str], list[str]] = {}
        for item in reconciled:
            signature = item_signature(item)
            if not signature[1]:
                continue
            signature_map.setdefault(signature, []).append(item_key(item))
        duplicate_semantic_assets = [
            {"asset_type": signature[0], "normalized_identity": signature[1], "canonical_keys": keys}
            for signature, keys in signature_map.items()
            if len(set(keys)) > 1
        ]

        typed_metadata_required_fields = {
            "character": ["appearance", "costume"],
            "scene": ["key_set_elements"],
            "prop": ["physical_description", "used_in_scenes"],
        }

        def metadata_field_valid(field: str, item: dict[str, Any]) -> bool:
            if field not in item:
                return False
            value = item.get(field)
            if field in {"key_set_elements", "used_in_scenes"}:
                return isinstance(value, list)
            return isinstance(value, str)

        typed_metadata_missing_fields: dict[str, list[str]] = {}
        typed_metadata_violations: list[str] = []
        for item in reconciled:
            key = item_key(item)
            asset_type = str(item.get("asset_type") or "")
            required_fields = typed_metadata_required_fields.get(asset_type, [])
            missing_fields = [field for field in required_fields if not metadata_field_valid(field, item)]
            if missing_fields:
                typed_metadata_violations.append(key)
                typed_metadata_missing_fields[key] = missing_fields

        validation_pass = not (
            missing_baseline
            or duplicate_requirement_coverage
            or duplicate_canonical_keys
            or duplicate_semantic_assets
            or invalid_asset_types
            or continuity_lock_violations
            or typed_metadata_violations
        )
        result["items"] = reconciled
        result["reuse_count"] = counts.get("REUSE", 0)
        result["variant_count"] = counts.get("VARIANT", 0)
        result["create_count"] = counts.get("CREATE", 0)
        result["manifest_validation"] = {
            "contract_source": "script.asset_requirements -> source_requirement_keys",
            "identity_policy": "manifest_canonical_key_separate_from_script_requirement_key",
            "count_policy": "evidence_derived_not_quota",
            "required_count": len(baseline_source_keys),
            "included_required_count": len(baseline_source_keys) - len(missing_baseline),
            "missing_required_keys": missing_baseline,
            "duplicate_script_requirement_keys": duplicate_script_requirement_keys,
            "duplicate_requirement_coverage": duplicate_requirement_coverage,
            "duplicate_canonical_keys": duplicate_canonical_keys,
            "duplicate_semantic_assets": duplicate_semantic_assets,
            "invalid_asset_types": invalid_asset_types,
            "continuity_lock_violations": continuity_lock_violations,
            "typed_metadata_violations": typed_metadata_violations,
            "required_baseline_coverage": {
                "required_source_requirement_keys": baseline_source_keys,
                "covered_source_requirement_keys": [key for key in baseline_source_keys if coverage.get(key)],
                "missing_source_requirement_keys": missing_baseline,
                "duplicate_coverage": duplicate_requirement_coverage,
            },
            "missing_items": missing_baseline,
            "duplicate_canonical_semantic_check": {
                "status": "pass" if not duplicate_canonical_keys and not duplicate_semantic_assets else "fail",
                "duplicate_canonical_keys": duplicate_canonical_keys,
                "duplicate_semantic_assets": duplicate_semantic_assets,
            },
            "illegal_asset_type_check": {
                "status": "pass" if not invalid_asset_types else "fail",
                "invalid_asset_types": invalid_asset_types,
            },
            "continuity_lock_check": {
                "status": "pass" if not continuity_lock_violations else "fail",
                "violations": continuity_lock_violations,
                "encoding": "array_of_rules",
            },
            "typed_metadata_check": {
                "status": "pass" if not typed_metadata_violations else "fail",
                "required_fields": typed_metadata_required_fields,
                "policy": "closed_required_set_explicit_types",
                "violations": typed_metadata_violations,
                "missing_fields_by_asset": typed_metadata_missing_fields,
            },
            "asset_type_counts": type_counts,
            "extra_count": max(0, len(reconciled) - len(baseline_source_keys)),
            "item_count": len(reconciled),
            "type_counts": type_counts,
            "status": "pass" if validation_pass else "fail",
            "previous_revision_items_preserved": bool(previous),
            "semantic_alias_match_count": len(semantic_alias_matches),
            "semantic_alias_matches": semantic_alias_matches,
        }
        return result

    @staticmethod
    def _enforce_asset_manifest(stage: str, content: dict[str, Any], artifacts: list[dict[str, Any]], *, workspace: dict[str, Any] | None = None) -> dict[str, Any]:
        type_map={"characters":"character","scenes":"scene","props":"prop"}
        wanted_type=type_map[stage]
        manifest_artifact=next((a for a in artifacts if a.get("kind")=="asset_manifest"), None)
        manifest_items=[
            item for item in ((manifest_artifact or {}).get("content", {}).get("items", []) or [])
            if isinstance(item,dict) and str(item.get("asset_type") or "").lower()==wanted_type
        ]
        result=dict(content or {})
        output=[dict(item) for item in (result.get("items") or []) if isinstance(item,dict)]
        by_manifest={str(item.get("manifest_id") or ""):item for item in output if item.get("manifest_id")}
        by_key={str(item.get("canonical_key") or ""):item for item in output if item.get("canonical_key")}
        enforced=[]
        missing=[]
        continuity_localization_issues: list[dict[str, Any]] = []
        for manifest in manifest_items:
            manifest_id=str(manifest.get("manifest_id") or "")
            canonical_key=str(manifest.get("canonical_key") or manifest_id)
            item=by_manifest.get(manifest_id) or by_key.get(canonical_key)
            if not item:
                missing.append(manifest_id or canonical_key)
                continue
            decision=str(manifest.get("decision") or "CREATE").upper()
            item["manifest_id"]=manifest_id
            item["canonical_key"]=canonical_key
            item["reuse_decision"]=decision
            item["library_asset_id"] = manifest.get("library_asset_id") if decision in {"REUSE","VARIANT"} else None
            # Source continuity and production continuity are intentionally separate.
            # The manifest lock is source/narrative evidence; once an asset designer has
            # produced an explicit localized design, NEVER copy that source lock back over
            # the designer's French/target-market production continuity.
            def rule_list(value: Any) -> list[str]:
                if isinstance(value, (list, tuple, set)):
                    raw_values = value
                elif value in (None, "", False):
                    raw_values = []
                else:
                    raw_values = [value]
                output_rules: list[str] = []
                for raw_value in raw_values:
                    rule = str(raw_value).strip()
                    if rule and rule not in output_rules:
                        output_rules.append(rule)
                return output_rules

            explicit_source_lock = rule_list(item.get("source_continuity_lock"))
            manifest_source_lock = rule_list(manifest.get("continuity_lock"))
            # Prefer an explicit source archive returned by the designer.  It can contain
            # source-culture facts that the higher-level manifest intentionally abstracts.
            source_lock = explicit_source_lock or manifest_source_lock

            production_lock = rule_list(item.get("production_continuity_lock"))
            if not production_lock:
                production_lock = rule_list(item.get("continuity_lock"))

            has_localized_design = bool(
                item.get("localized_visual_design")
                or item.get("generation_prompt_en")
                or item.get("visual_localization_notes")
            )
            # Backward compatibility for old/non-localized outputs only.  For an explicitly
            # localized asset, an absent production lock is safer left empty (and reviewable)
            # than silently contaminated by source-culture styling.
            if not production_lock and not has_localized_design:
                production_lock = list(manifest_source_lock)

            item["source_continuity_lock"] = source_lock
            item["continuity_lock"] = production_lock
            item["production_continuity_lock"] = list(production_lock)
            if has_localized_design:
                # continuity is a legacy/general-purpose alias used by some downstream
                # retrieval paths.  Once localization exists, keep it aligned to production
                # continuity so stale source styling cannot leak through a third field.
                item["continuity"] = list(production_lock)
            item["variant_key"] = item.get("variant_key") or (str(manifest.get("variant_key") or "") if decision=="VARIANT" else "")
            item["manifest_reason"] = manifest.get("reason") or ""

            settings = (workspace or {}).get("settings") or {}
            target_market = str(settings.get("target_market") or "").strip()
            target_language = str(settings.get("target_language") or "").strip()
            source_traits = item.get("source_visual_traits")
            if not isinstance(source_traits, list) or not source_traits:
                source_candidates: list[str] = []
                for field in (
                    ("appearance", "wardrobe") if stage == "characters"
                    else ("location", "layout", "lighting") if stage == "scenes"
                    else ("description", "material_scale", "state")
                ):
                    value = item.get(field)
                    if isinstance(value, str) and value.strip():
                        source_candidates.append(value.strip())
                source_traits = source_candidates[:4]
            item["source_visual_traits"] = source_traits
            item.setdefault("localized_visual_design", "")
            item.setdefault("visual_localization_notes", "")
            item.setdefault("generation_prompt_en", "")
            item["visual_localization"] = {
                "target_market": target_market,
                "target_language": target_language,
                "source_vs_production_policy": "source traits preserve narrative function; culturally specific styling is adapted to the target market before rendering",
                "provider_prompt_language": "en",
                "status": "localized_spec_ready" if item.get("localized_visual_design") and item.get("generation_prompt_en") else "provider_compiler_fallback",
            }
            if item.get("localized_visual_design") or item.get("generation_prompt_en"):
                if not item.get("continuity_lock"):
                    continuity_localization_issues.append({
                        "canonical_key": canonical_key,
                        "issue": "missing_production_continuity_lock",
                    })
                elif item.get("source_continuity_lock") and item.get("source_continuity_lock") == item.get("continuity_lock"):
                    continuity_localization_issues.append({
                        "canonical_key": canonical_key,
                        "issue": "source_and_production_continuity_are_identical",
                    })
            enforced.append(item)
        if missing:
            raise OrchestrationError(
                f"{STAGE_LABELS.get(stage, stage)} Agent 漏掉 Asset Manifest 项：" + ", ".join(missing)
            )
        result["items"]=enforced
        result["manifest_enforced"] = True
        result["continuity_localization_validation"] = {
            "status": "pass" if not continuity_localization_issues else "fail",
            "policy": "source_continuity_lock_is_archive; continuity_lock_is_target_market_production_authority; never auto-merge source into production",
            "issues": continuity_localization_issues,
        }
        return result

    def publish_series_assets(self, workspace_id: str) -> dict[str, Any]:
        """Publish approved episode assets into the series Bible for future episodes."""
        workspace = self._workspace(workspace_id)
        settings = workspace.get("settings") or {}
        series_id = str(settings.get("series_id") or "")
        if not series_id:
            return {"published": 0, "updated": 0, "variants": 0, "reason": "workspace_has_no_series"}
        series = self.db.get_series(series_id)
        if not series:
            return {"published": 0, "updated": 0, "variants": 0, "reason": "series_not_found"}

        libraries = self.db.attach_series_libraries(workspace_id, series_id)
        by_scope = {str(item.get("scope")): item for item in libraries}
        artifacts = {item["kind"]: item for item in self.db.list_artifacts(workspace_id)}
        kind_scope = {"characters": "character", "scenes": "scene", "props": "prop"}
        published = updated = variants = 0
        source_index: dict[tuple[str, str], dict[str, Any]] = {}

        def usage_entry(kind: str, revision: int, decision: str) -> dict[str, Any]:
            return {
                "workspace_id": workspace_id,
                "episode_key": settings.get("episode_key", ""),
                "artifact_kind": kind,
                "revision": revision,
                "decision": decision,
            }

        for kind, scope in kind_scope.items():
            artifact = artifacts.get(kind)
            library = by_scope.get(scope)
            if not artifact or not library:
                continue
            revision = int(artifact.get("revision") or 0)
            items = artifact.get("content", {}).get("items", [])
            if not isinstance(items, list):
                continue
            library_id = str(library["id"])
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                decision = str(item.get("reuse_decision") or "CREATE").upper()
                canonical_key = str(item.get("canonical_key") or item.get("id") or f"{scope.upper()}_{index+1:03d}")
                variant_key = str(item.get("variant_key") or "").strip()
                name = str(item.get("name") or item.get("id") or canonical_key)
                description = str(item.get("description") or item.get("appearance") or item.get("location") or item.get("continuity") or "")
                retrieval_text = " | ".join(part for part in [
                    name, canonical_key, str(item.get("source_function") or ""), description,
                    str(item.get("continuity") or ""), str(item.get("continuity_lock") or ""),
                ] if part)[:12000]
                usage = usage_entry(kind, revision, decision)

                requested_id = str(item.get("library_asset_id") or "")
                base = None
                if requested_id:
                    candidate = next((x for x in self.db.list_asset_items(library_id) if str(x.get("id")) == requested_id), None)
                    if candidate and not str(candidate.get("variant_key") or ""):
                        base = candidate
                if not base:
                    base = self.db.find_asset_by_canonical_key(library_id, canonical_key)

                if decision == "REUSE" and base:
                    content = dict(base.get("content") or {})
                    history = list(content.get("usage_history") or [])
                    if usage not in history:
                        history.append(usage)
                    content["usage_history"] = history[-100:]
                    target = self.db.update_asset_item(
                        str(base["id"]), content_json=content, retrieval_text=base.get("retrieval_text") or retrieval_text,
                        source_workspace_id=workspace_id, source_artifact_kind=kind, source_revision=revision, status="approved",
                    )
                    updated += 1

                elif decision == "VARIANT" and base:
                    if not variant_key:
                        episode = str(settings.get("episode_key") or "episode").replace(" ", "_")
                        variant_key = f"{episode}_{str(item.get('id') or index+1)}"
                    target = self.db.find_asset_variant(library_id, canonical_key, variant_key)
                    variant_content = dict(item)
                    variant_content.update({
                        "series_id": series_id, "series_name": series.get("name"), "canonical_key": canonical_key,
                        "parent_canonical_asset_id": base.get("id"), "usage_history": [usage],
                    })
                    if target:
                        old_history = list((target.get("content") or {}).get("usage_history") or [])
                        if usage not in old_history:
                            old_history.append(usage)
                        variant_content["usage_history"] = old_history[-100:]
                        target = self.db.update_asset_item(
                            str(target["id"]), name=name, description=description, content_json=variant_content,
                            canonical_key=canonical_key, parent_item_id=str(base["id"]), variant_key=variant_key,
                            retrieval_text=retrieval_text, source_workspace_id=workspace_id, source_artifact_kind=kind,
                            source_revision=revision, status="approved",
                        )
                        updated += 1
                    else:
                        target = self.db.add_asset_item(
                            library_id, scope, name, description=description, content=variant_content,
                            canonical_key=canonical_key, parent_item_id=str(base["id"]), variant_key=variant_key,
                            retrieval_text=retrieval_text, source_workspace_id=workspace_id, source_artifact_kind=kind,
                            source_revision=revision, status="approved",
                        )
                        variants += 1

                else:
                    # CREATE is the expected path when the series has no match. If a
                    # canonical asset with the same key already exists, preserve it
                    # and record usage instead of silently forking identity.
                    if base:
                        content = dict(base.get("content") or {})
                        history = list(content.get("usage_history") or [])
                        if usage not in history:
                            history.append(usage)
                        content["usage_history"] = history[-100:]
                        target = self.db.update_asset_item(
                            str(base["id"]), content_json=content, source_workspace_id=workspace_id,
                            source_artifact_kind=kind, source_revision=revision, status="approved",
                        )
                        updated += 1
                    else:
                        stored_content = dict(item)
                        stored_content.update({
                            "series_id": series_id, "series_name": series.get("name"), "canonical_key": canonical_key,
                            "usage_history": [usage],
                        })
                        target = self.db.add_asset_item(
                            library_id, scope, name, description=description, content=stored_content,
                            canonical_key=canonical_key, retrieval_text=retrieval_text, source_workspace_id=workspace_id,
                            source_artifact_kind=kind, source_revision=revision, status="approved",
                        )
                        published += 1

                if target:
                    source_index[(kind, str(item.get("id") or name))] = target

        refs = artifacts.get("reference_images")
        if refs:
            for ref in refs.get("content", {}).get("items", []) or []:
                if not isinstance(ref, dict):
                    continue
                target = source_index.get((str(ref.get("source_kind") or ""), str(ref.get("source_id") or "")))
                if not target:
                    continue
                content = dict(target.get("content") or {})
                content["reference_image"] = {
                    "url": ref.get("url"), "remote_url": ref.get("remote_url"), "local_path": ref.get("local_path"),
                    "source_workspace_id": workspace_id, "source_revision": refs.get("revision"),
                }
                self.db.update_asset_item(
                    str(target["id"]), content_json=content,
                    preview_url=str(ref.get("url") or target.get("preview_url") or ""),
                )

        self.db.add_event(workspace_id, "series.assets_published", {
            "series_id": series_id, "published": published, "updated": updated, "variants": variants,
        })
        self.db.add_guidance_message(
            workspace_id, "reference_images", "director",
            f"已把本集批准资产沉淀到系列《{series.get('name')}》：新增 canonical {published}，新增 variant {variants}，复用/更新 {updated}。下一集挂载同一系列后会先检索这些资产；找不到时再自动生成。",
            {"kind": "series_asset_publish", "series_id": series_id, "published": published, "updated": updated, "variants": variants},
        )
        return {
            "series_id": series_id, "series_name": series.get("name"),
            "published": published, "updated": updated, "variants": variants,
        }

    def revise(
        self, workspace_id: str, kind: str, content: dict[str, Any], cascade: bool
    ) -> dict[str, Any]:
        if kind not in STAGES:
            raise OrchestrationError(f"Unknown artifact kind: {kind}")
        agent = agent_for_stage(kind)
        content["_agent"] = {"id": agent.id, "name": agent.name}
        artifact = self.db.upsert_artifact(
            workspace_id,
            kind,
            STAGE_LABELS[kind],
            content,
            "manual",
            upstream=REQUIRES.get(kind, []),
        )
        invalidated = self.invalidate_downstream(workspace_id, kind) if cascade else []
        self.db.clear_stage_reviews(workspace_id, [kind, *invalidated])
        for gate_stage, gate_name in GATES.items():
            if gate_stage == kind or gate_stage in invalidated:
                self.db.set_approval(workspace_id, gate_name, "pending", "上游内容已修改")
        return {"artifact": artifact, "invalidated": invalidated}

    def invalidate_downstream(self, workspace_id: str, changed_kind: str) -> list[str]:
        downstream: set[str] = set()
        frontier = [changed_kind]
        while frontier:
            current = frontier.pop()
            for candidate, requirements in REQUIRES.items():
                if current in requirements and candidate not in downstream:
                    downstream.add(candidate)
                    frontier.append(candidate)
        ordered = [stage for stage in STAGES if stage in downstream]
        return self.db.mark_artifacts_stale(workspace_id, ordered)

    def next_actions(self, workspace_id: str) -> list[dict[str, Any]]:
        artifacts = {item["kind"]: item for item in self.db.list_artifacts(workspace_id)}
        approvals = {item["gate"]: item["status"] for item in self.db.list_approvals(workspace_id)}
        actions: list[dict[str, Any]] = []
        for stage in STAGES[1:]:
            artifact = artifacts.get(stage)
            if artifact and artifact["status"] == "ready":
                gate = GATES.get(stage)
                gate_status = approvals.get(gate, "pending") if gate else ""
                # A human can explicitly override a director review by approving
                # the gate with force=True. Human approval remains authoritative.
                if gate and gate_status == "approved":
                    continue
                review_row = self.db.get_stage_review(workspace_id, stage)
                review = (review_row or {}).get("review", {}) if review_row and int(review_row.get("artifact_revision") or 0) == int(artifact.get("revision") or 0) else {}
                recommended = str(review.get("recommended_action") or "")
                if recommended == "regenerate_current":
                    actions.append({
                        "type": "run", "stage": stage,
                        "label": f"按总管复盘重新生成{STAGE_LABELS[stage]}",
                        "review_blocked": True,
                        "review": review,
                    })
                    break
                if recommended == "wait_for_user":
                    actions.append({
                        "type": "chat", "stage": stage,
                        "label": f"与总管确认{STAGE_LABELS[stage]}问题",
                        "review_blocked": True,
                        "review": review,
                    })
                    break
                if gate:
                    if gate_status == "rejected":
                        actions.append(
                            {
                                "type": "run",
                                "stage": stage,
                                "label": f"重新生成{STAGE_LABELS[stage]}",
                            }
                        )
                        break
                    if gate_status != "approved":
                        actions.append({"type": "approve", "gate": gate, "label": f"确认{STAGE_LABELS[stage]}"})
                        break
                continue
            prerequisites = REQUIRES.get(stage, [])
            if all(artifacts.get(item, {}).get("status") == "ready" for item in prerequisites):
                required_gate = self._required_gate(stage)
                if required_gate and approvals.get(required_gate) != "approved":
                    actions.append({"type": "approve", "gate": required_gate, "label": f"确认 {required_gate}"})
                else:
                    actions.append({"type": "run", "stage": stage, "label": f"生成{STAGE_LABELS[stage]}"})
                break
        return actions

    @staticmethod
    def _required_gate(stage: str) -> str | None:
        if stage in {"asset_manifest", "characters", "scenes", "props", "reference_images"}:
            return "script_approved"
        if stage in {"storyboard", "dialogue_plan", "sound_plan", "review"}:
            return "assets_approved"
        if stage == "preview":
            return "storyboard_approved"
        if stage in {"batch_video", "music_plan", "music", "compose", "delivery_qa"}:
            return "preview_approved"
        return None

    @staticmethod
    def _validate_stage(stage: str, artifacts: dict[str, dict[str, Any]]) -> None:
        if stage != "preview":
            return
        review = artifacts.get("review", {}).get("content", {})
        blocking = review.get("blocking_failures", [])
        if blocking:
            raise OrchestrationError(
                "Continuity QA has blocking failures: " + ", ".join(map(str, blocking))
            )

    def _is_approved(self, workspace_id: str, gate: str) -> bool:
        return any(
            item["gate"] == gate and item["status"] == "approved"
            for item in self.db.list_approvals(workspace_id)
        )

    def _workspace(self, workspace_id: str) -> dict[str, Any]:
        workspace = self.db.get_workspace(workspace_id)
        if not workspace:
            raise OrchestrationError("Workspace not found")
        return workspace
