from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from .base import WorkflowProvider
from app.guidance import requested_preview_shot_index, sanitize_parameter_overrides
from app.image_provenance import contains_person, image_format, sha256_file
from app.reference_plan import source_ids_from_combination_key
from app.tos_archive import TosArchive


class SeedanceError(RuntimeError):
    pass


class SeedanceAPIError(SeedanceError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        error_code: str = "",
        model: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.model = model


class SeedanceProvider(WorkflowProvider):
    """Fire Ark Seedance async video adapter.

    The adapter creates an Ark video task, polls it to a terminal state, and
    downloads successful results because provider URLs may expire.
    """

    name = "seedance"
    _FALLBACK_MODEL_ERROR_CODES = {
        "ModelNotOpen",
        "ModelNotFound",
        "InvalidModel",
        "ModelDisabled",
    }

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        fallback_model: str = "",
        resolution: str = "720p",
        ratio: str = "9:16",
        generate_audio: bool = True,
        poll_interval_seconds: float = 10,
        poll_timeout_seconds: int = 900,
        batch_max_shots: int = 8,
        batch_concurrency: int = 1,
        output_dir: Path,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        account_id: str = "",
        original_archive: TosArchive | None = None,
        preflight_enabled: bool = False,
        ffmpeg_path: str = "ffmpeg",
    ):
        if not api_key or not model:
            raise ValueError("VIDEO_API_KEY and VIDEO_MODEL are required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fallback_model = fallback_model
        self.resolution = resolution
        self.ratio = ratio
        self.generate_audio = generate_audio
        self.poll_interval_seconds = max(0, poll_interval_seconds)
        self.poll_timeout_seconds = max(30, poll_timeout_seconds)
        # ``batch_max_shots`` is retained for constructor/API compatibility, but
        # is no longer a silent global cap. A full batch should cover the whole
        # storyboard unless the Director explicitly supplies a per-run
        # ``batch_max_shots`` override (for example, "先生成前 4 镜").
        self.batch_max_shots = max(1, batch_max_shots)
        self.batch_concurrency = max(1, min(8, int(batch_concurrency)))
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._sleep = sleeper
        self.account_id = account_id.strip()
        self.original_archive = original_archive
        self.preflight_enabled = preflight_enabled
        ffmpeg = Path(str(ffmpeg_path or "ffmpeg"))
        sibling_name = "ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe"
        sibling = ffmpeg.with_name(sibling_name)
        self.ffprobe_path = str(sibling) if sibling.is_file() else "ffprobe"

    def generate(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        shots = self._storyboard_shots(context)
        dialogue_by_shot = self._plan_by_shot(context, "dialogue_plan")
        sound_by_shot = self._plan_by_shot(context, "sound_plan")
        workspace_id = str(context["workspace"]["id"])
        directive = context.get("execution_directive") or {}
        params = sanitize_parameter_overrides(stage, directive.get("parameter_overrides"))
        options = {
            "resolution": params.get("resolution", self.resolution),
            "ratio": params.get("ratio", self.ratio),
            "generate_audio": params.get("generate_audio", self.generate_audio),
            "prompt_addendum": str(directive.get("prompt_addendum") or "").strip(),
        }
        max_reference_images = 30 if "seedance-2-5" in self.model.lower() else 9
        if stage == "preview":
            if not shots:
                raise SeedanceError("Storyboard has no shots")
            try:
                shot_index = requested_preview_shot_index(
                    context.get("user_instruction", ""), params
                )
            except (TypeError, ValueError) as exc:
                raise SeedanceError(str(exc)) from exc
            selected = [shot for shot in shots if int(shot.get("index", 0)) == shot_index]
            if len(selected) != 1:
                raise SeedanceError(f"Storyboard does not contain exactly one shot_index={shot_index}")
            shot = selected[0]
            reference_urls, reference_keys = self._shot_reference_urls(
                context, shot, max_images=max_reference_images
            )
            resolver_audit = self._reference_resolver_audit(shot, reference_keys, reference_urls)
            reference_urls = self._preflight_reference_urls(
                context, reference_keys, reference_urls
            )
            provenance_audit = self._preflight_audit_report(
                context, reference_keys, reference_urls
            )
            preflight_checks = {
                "status": "pass" if provenance_audit.get("status") == "pass" else provenance_audit.get("status", "unknown"),
                "reference_resolver": resolver_audit,
                "trusted_provenance_tos": provenance_audit,
                "technical_params": {
                    "status": "pass",
                    "resolution": str(options["resolution"]),
                    "ratio": str(options["ratio"]),
                    "generate_audio": bool(options["generate_audio"]),
                },
            }
            return self._generate_one(
                workspace_id,
                shot,
                "preview",
                reference_urls,
                reference_keys,
                dialogue_by_shot.get(shot_index, {}),
                sound_by_shot.get(shot_index, {}),
                options,
                context=context,
                preflight_checks=preflight_checks,
            )
        if stage == "batch_video":
            if not shots:
                raise SeedanceError("Storyboard has no shots")
            if self._approval_status(context, "preview_approved") != "approved":
                raise SeedanceError(
                    "批量视频需要 preview_approved=approved；当前单镜预览尚未获得人工批准，"
                    "已停止提交，不会创建收费 Seedance 任务"
                )

            # Only an explicit Director/user override may intentionally limit a
            # batch. The old VIDEO_BATCH_MAX_SHOTS setting used to silently turn
            # a 14-shot episode into an 8-shot artifact; that is no longer
            # allowed. Full batch means full storyboard coverage by default.
            explicit_limit = params.get("batch_max_shots")
            has_batch_history = any(
                artifact.get("kind") == "batch_video"
                and any(
                    isinstance(item, dict)
                    and str(item.get("status") or "").lower() == "succeeded"
                    for item in ((artifact.get("content") or {}).get("items") or [])
                )
                for artifact in (context.get("artifacts", []) or [])
                if isinstance(artifact, dict)
            )
            # A resume run always targets full storyboard coverage. This keeps a
            # Director-generated numeric override from accidentally interpreting
            # "补齐 6 镜" as "只看前 6 镜" and skipping Shots 09-14.
            selected = list(shots) if has_batch_history else (
                shots[: int(explicit_limit)] if explicit_limit is not None else list(shots)
            )
            selected_indices = [int(shot.get("index", 0)) for shot in selected]

            reusable = self._reusable_batch_items(context, selected_indices)
            items_by_index: dict[int, dict[str, Any]] = dict(reusable)
            prepared: list[
                tuple[dict[str, Any], list[str], list[str], dict[str, Any]]
            ] = []
            # Check every still-missing shot before creating any billable video
            # task. If any reference fails, the whole new submission stops while
            # already-successful preview/batch clips remain reusable.
            for shot in selected:
                shot_index = int(shot.get("index", 0))
                if shot_index in items_by_index:
                    continue
                reference_urls, reference_keys = self._shot_reference_urls(
                    context, shot, max_images=max_reference_images
                )
                resolver_audit = self._reference_resolver_audit(
                    shot, reference_keys, reference_urls
                )
                reference_urls = self._preflight_reference_urls(
                    context, reference_keys, reference_urls
                )
                provenance_audit = self._preflight_audit_report(
                    context, reference_keys, reference_urls
                )
                prepared.append((shot, reference_urls, reference_keys, {
                    "status": "pass" if provenance_audit.get("status") == "pass" else provenance_audit.get("status", "unknown"),
                    "reference_resolver": resolver_audit,
                    "trusted_provenance_tos": provenance_audit,
                    "technical_params": {
                        "status": "pass",
                        "resolution": str(options["resolution"]),
                        "ratio": str(options["ratio"]),
                        "generate_audio": bool(options["generate_audio"]),
                    },
                }))

            submitted = 0
            failures = 0
            # Ark's CreateContentsGenerationsTasks API creates one video task per
            # request. Speed up a production batch by running independent shot
            # tasks in bounded parallel waves. If one task in a wave fails, no
            # *next* wave is submitted; already-submitted tasks in the current
            # wave are allowed to finish so their paid results are not discarded.
            concurrency = min(self.batch_concurrency, max(1, len(prepared)))
            for start in range(0, len(prepared), concurrency):
                wave = prepared[start:start + concurrency]
                wave_results: dict[int, dict[str, Any]] = {}
                if concurrency == 1:
                    iterable = []
                    for shot, reference_urls, reference_keys, checks in wave:
                        shot_index = int(shot.get("index", 1))
                        try:
                            submitted += 1
                            wave_results[shot_index] = self._generate_one(
                                workspace_id,
                                shot,
                                "batch",
                                reference_urls,
                                reference_keys,
                                dialogue_by_shot.get(shot_index, {}),
                                sound_by_shot.get(shot_index, {}),
                                options,
                                context=context,
                                preflight_checks=checks,
                            )
                        except SeedanceError as exc:
                            failures += 1
                            wave_results[shot_index] = {
                                "shot_index": shot_index,
                                "status": "failed",
                                "error": str(exc),
                            }
                            break
                    items_by_index.update(wave_results)
                    if failures:
                        break
                    continue

                with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="seedance") as executor:
                    future_to_index = {}
                    for shot, reference_urls, reference_keys, checks in wave:
                        shot_index = int(shot.get("index", 1))
                        submitted += 1
                        future = executor.submit(
                            self._generate_one,
                            workspace_id,
                            shot,
                            "batch",
                            reference_urls,
                            reference_keys,
                            dialogue_by_shot.get(shot_index, {}),
                            sound_by_shot.get(shot_index, {}),
                            options,
                            context=context,
                            preflight_checks=checks,
                        )
                        future_to_index[future] = shot_index
                    for future in as_completed(future_to_index):
                        shot_index = future_to_index[future]
                        try:
                            wave_results[shot_index] = future.result()
                        except SeedanceError as exc:
                            failures += 1
                            wave_results[shot_index] = {
                                "shot_index": shot_index,
                                "status": "failed",
                                "error": str(exc),
                            }
                        except Exception as exc:  # fail closed for unexpected provider/runtime errors
                            failures += 1
                            wave_results[shot_index] = {
                                "shot_index": shot_index,
                                "status": "failed",
                                "error": f"Unexpected Seedance batch error: {exc}",
                            }
                items_by_index.update(wave_results)
                if failures:
                    break

            items = [items_by_index[index] for index in selected_indices if index in items_by_index]
            completed = sum(item.get("status") == "succeeded" for item in items)
            coverage_complete = (
                len(selected) == len(shots)
                and set(selected_indices) == set(int(shot.get("index", 0)) for shot in shots)
                and completed == len(shots)
                and not failures
            )
            status = "succeeded" if coverage_complete else ("partial_failed" if failures else "partial")
            return {
                "items": items,
                "requested": len(selected),
                "completed": completed,
                "storyboard_shots": len(shots),
                "submitted_new_tasks": submitted,
                "reused_existing": sum(bool(item.get("reused_from")) for item in items),
                "batch_concurrency": concurrency if prepared else 0,
                "execution_mode": "parallel_waves" if concurrency > 1 else "sequential",
                "fail_stop_scope": "wave" if concurrency > 1 else "shot",
                "coverage_complete": coverage_complete,
                "status": status,
                "note": (
                    "Seedance 每个创建请求仍对应一个视频任务；Harness 采用有界并发波次。"
                    "任一波次出现失败后不再提交下一波；当前波次已提交任务会完成。"
                    if concurrency > 1 else
                    "Seedance 按镜头顺序执行；首个失败镜头后停止继续提交。"
                ),
            }
        raise NotImplementedError(f"Seedance does not handle stage: {stage}")

    @staticmethod
    def _storyboard_shots(context: dict[str, Any]) -> list[dict[str, Any]]:
        for artifact in context.get("artifacts", []):
            if artifact.get("kind") == "storyboard":
                return list(artifact.get("content", {}).get("shots", []))
        return []

    @staticmethod
    def _approval_status(context: dict[str, Any], gate: str) -> str:
        for approval in context.get("approvals", []) or []:
            if str(approval.get("gate") or "") == gate:
                return str(approval.get("status") or "").lower()
        return ""

    @staticmethod
    def _reusable_video_item(
        content: dict[str, Any], *, source: str, revision: int | None = None
    ) -> dict[str, Any] | None:
        if not isinstance(content, dict):
            return None
        try:
            shot_index = int(content.get("shot_index"))
        except (TypeError, ValueError):
            return None
        if shot_index < 1 or str(content.get("status") or "").lower() != "succeeded":
            return None
        if not (content.get("url") or content.get("remote_url") or content.get("local_path")):
            return None
        allowed = {
            "shot_index", "provider_job_id", "model", "url", "remote_url", "local_path",
            "duration_seconds", "resolution", "ratio", "generate_audio",
            "reference_image_count", "reference_keys", "request_payload_digest",
            "assembly_log", "preflight_checks", "provider_generate_audio_echo",
            "audio_probe", "status", "warning",
        }
        item = {key: value for key, value in content.items() if key in allowed}
        item["shot_index"] = shot_index
        item["status"] = "succeeded"
        item["reused_from"] = source
        if revision is not None:
            item["reused_from_revision"] = int(revision)
        return item

    def _reusable_batch_items(
        self, context: dict[str, Any], selected_indices: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Reuse already-paid successful clips when a batch is resumed.

        Priority is the existing batch artifact, then the explicitly approved
        preview. This makes a partial 1-8 batch resumable as 9-14 without paying
        for shots 1-8 again, while still preserving the preview gate.
        """
        wanted = set(selected_indices)
        result: dict[int, dict[str, Any]] = {}
        for artifact in context.get("artifacts", []) or []:
            if artifact.get("kind") != "batch_video":
                continue
            content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
            for raw in content.get("items", []) or []:
                item = self._reusable_video_item(
                    raw if isinstance(raw, dict) else {},
                    source="batch_video",
                    revision=int(artifact.get("revision") or 0),
                )
                if item and item["shot_index"] in wanted:
                    result[item["shot_index"]] = item

        if self._approval_status(context, "preview_approved") == "approved":
            for artifact in context.get("artifacts", []) or []:
                if artifact.get("kind") != "preview":
                    continue
                item = self._reusable_video_item(
                    artifact.get("content") if isinstance(artifact.get("content"), dict) else {},
                    source="approved_preview",
                    revision=int(artifact.get("revision") or 0),
                )
                # An explicitly approved preview is the production authority for
                # that shot and must replace any accidental pre-approval batch
                # duplicate from an older run.
                if item and item["shot_index"] in wanted:
                    result[item["shot_index"]] = item
                break
        return result

    @staticmethod
    def _reference_input_value(item: dict[str, Any]) -> str:
        """Resolve transport bytes; strict human provenance is checked separately."""
        # Ark can trust an unmodified Seedream 5.0 output from the same account.
        # A locally re-encoded copy loses that provenance, so use the original
        # signed output URL while it remains valid. Strict preflight rejects a
        # local fallback for person-containing images before task creation.
        remote = str(item.get("remote_url") or "").strip()
        if "seedream-5-0" in str(item.get("model") or "").lower() and SeedanceProvider._valid_ark_output_url(remote):
            return remote
        local = Path(str(item.get("local_path") or ""))
        if local.is_file():
            size = local.stat().st_size
            if size > 30 * 1024 * 1024:
                raise SeedanceError(
                    f"Reference image is too large for Seedance inline input: {local.name} "
                    f"({size / 1024 / 1024:.1f} MB; max 30 MB per image)"
                )
            mime = (mimetypes.guess_type(local.name)[0] or "image/png").lower()
            if mime not in {
                "image/jpeg", "image/png", "image/webp", "image/bmp",
                "image/tiff", "image/gif", "image/heic", "image/heif",
            }:
                mime = "image/png"
            encoded = base64.b64encode(local.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{encoded}"
        for key in ("remote_url", "url"):
            value = str(item.get(key) or "").strip()
            if value.startswith("https://") or value.startswith("http://"):
                return value
        return ""

    @staticmethod
    def _valid_ark_output_url(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".volces.com"):
            return False
        params = parse_qs(parsed.query)
        try:
            signed_at = datetime.strptime(params["X-Tos-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            expiry = signed_at + timedelta(seconds=int(params["X-Tos-Expires"][0]))
        except (KeyError, IndexError, ValueError, OverflowError):
            return False
        return datetime.now(timezone.utc) + timedelta(minutes=5) < expiry

    @staticmethod
    def _isolated_upstream_stage(item: dict[str, Any], key: str) -> str:
        """Return the production-truth stage for an isolated reference item.

        ``reference_images`` stores isolated mirrors of character/scene/prop
        production truths as well as independently generated relationship
        combinations.  Isolated mirrors must always hydrate from the currently
        selected upstream candidate; a selected ``reference_images`` proxy may
        be a provenance-less binding record and must never become portrait
        authority.
        """
        if SeedanceProvider._is_combination_reference(item, key):
            return ""
        source_kind = str(item.get("source_kind") or "").strip().lower()
        if source_kind in {"character", "characters"} or key.startswith("char_"):
            return "characters"
        if source_kind in {"scene", "scenes"} or key.startswith("scene_"):
            return "scenes"
        if source_kind in {"prop", "props"} or key.startswith("prop_"):
            return "props"
        return ""

    @classmethod
    def _reference_items(cls, context: dict[str, Any]) -> list[dict[str, Any]]:
        selected_reference_by_key: dict[str, dict[str, Any]] = {}
        selected_upstream_by_stage_key: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in context.get("asset_candidates", []) or []:
            if not isinstance(candidate, dict) or not candidate.get("selected"):
                continue
            key = str(candidate.get("canonical_key") or "").strip()
            stage = str(candidate.get("stage") or "").strip()
            if not key:
                continue
            if stage == "reference_images":
                selected_reference_by_key[key] = candidate
            elif stage in {"characters", "scenes", "props"}:
                selected_upstream_by_stage_key[(stage, key)] = candidate

        hydrate_fields = ("model", "remote_url", "url", "local_path", "provenance")
        for artifact in context.get("artifacts", []):
            if artifact.get("kind") == "reference_images":
                items = []
                for item in artifact.get("content", {}).get("items", []):
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("canonical_key") or item.get("reference_key") or "").strip()
                    upstream_stage = cls._isolated_upstream_stage(item, key)
                    selected = (
                        selected_upstream_by_stage_key.get((upstream_stage, key))
                        if upstream_stage
                        else selected_reference_by_key.get(key)
                    )
                    if selected:
                        # Isolated character/scene/prop references are mirrors of
                        # the upstream production truth, so hydrate transport and
                        # provenance from that selected candidate. Relationship
                        # combinations remain governed by their selected
                        # reference_images candidate. This prevents an old
                        # provenance-less isolated proxy from overwriting a newly
                        # trusted character portrait before Seedance preflight.
                        item = {
                            **item,
                            **{field: selected.get(field) for field in hydrate_fields},
                            "candidate_id": selected["id"],
                        }
                    items.append(item)
                return items
        return []

    @classmethod
    def _reference_urls(cls, context: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for item in cls._reference_items(context):
            url = cls._reference_input_value(item)
            if url and url not in urls:
                urls.append(url)
        return urls[:4]

    @staticmethod
    def _combination_source_ids(*records: dict[str, Any] | None) -> list[str]:
        """Return canonical source assets for a relationship combination.

        Prefer the explicit source_id/source_ids stored on the current
        reference-image production truth.  Canonical combo keys are accepted as
        a backwards-compatible structural fallback; parsing the key never grants
        provenance or trust to the combination render itself.
        """
        for record in records:
            if not isinstance(record, dict):
                continue
            for field in ("source_id", "source_ids"):
                value = record.get(field)
                if isinstance(value, list):
                    result = [str(item).strip() for item in value if str(item).strip()]
                    if result:
                        return result
        for record in records:
            if not isinstance(record, dict):
                continue
            key = str(record.get("canonical_key") or record.get("reference_key") or record.get("id") or "").strip()
            parsed = source_ids_from_combination_key(key)
            if parsed:
                return parsed
        return []

    @staticmethod
    def _is_combination_reference(record: dict[str, Any] | None, key: str = "") -> bool:
        if isinstance(record, dict):
            if str(record.get("source_kind") or "").lower() == "combination":
                return True
            record_key = str(record.get("canonical_key") or record.get("reference_key") or record.get("id") or "")
            if record_key.startswith("combo__"):
                return True
        return str(key or "").startswith("combo__")

    @staticmethod
    def _reference_priority(key: str, item: dict[str, Any], *, nonhuman_combo: bool = False) -> int:
        """Order identity authorities before contextual relationship renders."""
        source_kind = str(item.get("source_kind") or "").lower()
        if key.startswith("char_") or source_kind in {"character", "characters"}:
            return 0
        if key.startswith(("scene_", "prop_")) or source_kind in {"scene", "scenes", "prop", "props"}:
            return 1
        if nonhuman_combo:
            return 2
        return 3

    @classmethod
    def _shot_reference_urls(
        cls, context: dict[str, Any], shot: dict[str, Any], *, max_images: int = 9
    ) -> tuple[list[str], list[str]]:
        """Resolve the real Seedance inputs for exactly one shot.

        A relationship combination that contains a character is *never* sent to
        Seedance as an identity authority.  Instead it is expanded to the
        currently selected isolated production truths named by source_id/source_ids
        (or, for older structural records, by its canonical combo key).  Pure
        scene/prop combinations may still be submitted as contextual references.

        The reference limit is enforced after expansion and de-duplication so a
        compact storyboard binding can never silently become an oversized paid
        request.
        """
        items = cls._reference_items(context)
        bindings = (shot.get("asset_bindings") or {}).get("reference_images") or []
        if not items:
            if bindings:
                raise SeedanceError("镜头已绑定参考图，但 reference_images 产物缺失")
            return [], []
        if not isinstance(bindings, list) or not bindings:
            # Do not attach arbitrary global references to an unbound shot.  This
            # also guarantees that every submitted image has a canonical key for
            # preflight instead of bypassing validation through an empty key list.
            return [], []

        by_candidate: dict[str, dict[str, Any]] = {}
        by_key: dict[str, dict[str, Any]] = {}
        by_signature: dict[tuple[str, ...], dict[str, Any]] = {}
        for item in items:
            candidate_id = str(item.get("candidate_id") or "").strip()
            canonical_key = str(item.get("canonical_key") or item.get("reference_key") or item.get("id") or "").strip()
            if candidate_id:
                by_candidate[candidate_id] = item
            if canonical_key:
                by_key[canonical_key] = item
            source_ids = cls._combination_source_ids(item)
            if cls._is_combination_reference(item, canonical_key) and source_ids:
                signature = tuple(sorted(source_ids))
                if signature:
                    by_signature[signature] = item

        resolved: list[tuple[int, int, str, str]] = []
        unresolved: list[str] = []
        seen_keys: set[str] = set()
        sequence = 0

        def add_item(key: str, item: dict[str, Any], *, nonhuman_combo: bool = False) -> None:
            nonlocal sequence
            canonical_key = str(item.get("canonical_key") or item.get("reference_key") or item.get("id") or key or "").strip()
            canonical_key = canonical_key or str(key or "").strip() or "direct_reference"
            if canonical_key in seen_keys:
                return
            url = cls._reference_input_value(item)
            if not url:
                unresolved.append(canonical_key)
                return
            seen_keys.add(canonical_key)
            resolved.append((cls._reference_priority(canonical_key, item, nonhuman_combo=nonhuman_combo), sequence, canonical_key, url))
            sequence += 1

        def resolve_binding(binding: Any) -> tuple[dict[str, Any] | None, str, str]:
            matched: dict[str, Any] | None = None
            display_key = ""
            candidate_id = ""
            if isinstance(binding, str):
                display_key = binding.strip()
                matched = by_key.get(display_key)
            elif isinstance(binding, dict):
                candidate_id = str(binding.get("candidate_id") or "").strip()
                display_key = str(binding.get("canonical_key") or binding.get("reference_key") or binding.get("id") or "").strip()
                if candidate_id:
                    matched = by_candidate.get(candidate_id)
                if matched is None and display_key:
                    matched = by_key.get(display_key)
                source_ids = cls._combination_source_ids(binding)
                if matched is None and source_ids:
                    matched = by_signature.get(tuple(sorted(source_ids)))
            return matched, display_key, candidate_id

        for binding in bindings:
            matched, display_key, candidate_id = resolve_binding(binding)
            binding_record = binding if isinstance(binding, dict) else None
            combination_record = matched if cls._is_combination_reference(matched, display_key) else binding_record
            is_combination = cls._is_combination_reference(combination_record, display_key)
            source_ids = cls._combination_source_ids(matched, binding_record) if is_combination else []

            if is_combination and (any(source_id.startswith("char_") for source_id in source_ids) or (not source_ids and contains_person(matched or binding_record or {"canonical_key": display_key}))):
                combo_key = str((matched or {}).get("canonical_key") or display_key or candidate_id or "human_combination")
                if not source_ids:
                    unresolved.append(f"{combo_key} 缺少 source_id/source_ids，无法安全展开人物组合")
                    continue
                for source_id in source_ids:
                    isolated = by_key.get(source_id)
                    if isolated is None:
                        unresolved.append(f"{combo_key} -> {source_id} 缺少当前 isolated production truth")
                        continue
                    if cls._is_combination_reference(isolated, source_id):
                        unresolved.append(f"{combo_key} -> {source_id} 不是 isolated production truth")
                        continue
                    add_item(source_id, isolated)
                # Deliberately never add the human-containing combination render.
                continue

            if matched is not None:
                key = str(matched.get("canonical_key") or matched.get("reference_key") or matched.get("id") or display_key or candidate_id or "reference")
                add_item(key, matched, nonhuman_combo=is_combination)
                continue

            if isinstance(binding, dict):
                direct_url = cls._reference_input_value(binding)
                if direct_url:
                    key = display_key or candidate_id or "direct_reference"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        resolved.append((cls._reference_priority(key, binding, nonhuman_combo=is_combination), sequence, key, direct_url))
                        sequence += 1
                    continue
            unresolved.append(display_key or candidate_id or "unknown_reference")

        if unresolved:
            raise SeedanceError("镜头参考图不可用：" + "；".join(dict.fromkeys(unresolved)))

        # Identity/reference authority first, pure contextual combinations last.
        # De-duplicate by transport URL as well as canonical key so the provider
        # sees the true number of images that will be billed/validated.
        urls: list[str] = []
        keys: list[str] = []
        seen_urls: set[str] = set()
        for _, _, key, url in sorted(resolved, key=lambda value: (value[0], value[1])):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            keys.append(key)
            urls.append(url)

        if len(urls) > max_images:
            raise SeedanceError(
                f"人物组合展开并去重后共有 {len(urls)} 张真实参考图，当前视频模型最多支持 {max_images} 张；"
                "请减少镜头绑定，不会截断后继续提交"
            )
        return urls, keys

    @classmethod
    def _reference_resolver_audit(
        cls, shot: dict[str, Any], keys: list[str], urls: list[str]
    ) -> dict[str, Any]:
        bindings = (shot.get("asset_bindings") or {}).get("reference_images") or []
        if not isinstance(bindings, list):
            bindings = []
        human_combinations: list[str] = []
        for binding in bindings:
            record = binding if isinstance(binding, dict) else {"canonical_key": str(binding)}
            key = str(
                record.get("canonical_key")
                or record.get("reference_key")
                or record.get("id")
                or ""
            ).strip()
            if not cls._is_combination_reference(record, key):
                continue
            source_ids = cls._combination_source_ids(record)
            if any(source_id.startswith("char_") for source_id in source_ids):
                human_combinations.append(key or "+".join(source_ids))
        return {
            "status": "pass",
            "requested_binding_count": len(bindings),
            "resolved_reference_count": len(urls),
            "resolved_reference_keys": list(keys),
            "human_combinations_expanded": list(dict.fromkeys(human_combinations)),
        }

    def _preflight_reference_urls(
        self, context: dict[str, Any], keys: list[str], urls: list[str]
    ) -> list[str]:
        checked, _ = self._preflight_reference_urls_with_report(context, keys, urls)
        return checked

    def _preflight_reference_urls_with_report(
        self, context: dict[str, Any], keys: list[str], urls: list[str]
    ) -> tuple[list[str], dict[str, Any]]:
        if not self.preflight_enabled:
            return urls, {
                "status": "skipped",
                "reason": "preflight_disabled",
                "reference_count": len(urls),
                "items": [],
            }
        if len(keys) != len(urls):
            raise SeedanceError(
                "镜头参考图预检内部状态不一致：reference_keys 与 reference_urls 数量不同"
            )
        by_key = {
            str(item.get("canonical_key") or item.get("reference_key") or ""): item
            for item in self._reference_items(context)
        }
        checked: list[str] = []
        failures: list[str] = []
        checks: list[dict[str, Any]] = []
        client, owns_client = self._get_client()
        try:
            for key, supplied_url in zip(keys, urls):
                item = by_key.get(key)
                if not item:
                    failures.append(f"镜头参考图 {key} 缺少可核验的来源记录")
                    continue
                try:
                    checked_url = self._preflight_one_reference(client, key, item, supplied_url)
                    checked.append(checked_url)
                    checks.append(
                        self._reference_preflight_audit_item(key, item, checked_url)
                    )
                except SeedanceError as exc:
                    failures.append(str(exc))
        finally:
            if owns_client:
                client.close()
        if failures:
            raise SeedanceError("镜头参考图预检未通过：" + "；".join(failures))
        return checked, {
            "status": "pass",
            "reference_count": len(checked),
            "trusted_person_reference_count": sum(
                1 for item in checks if item.get("contains_person")
            ),
            "items": checks,
        }

    def _preflight_audit_report(
        self, context: dict[str, Any], keys: list[str], checked_urls: list[str]
    ) -> dict[str, Any]:
        if not self.preflight_enabled:
            return {
                "status": "skipped",
                "reason": "preflight_disabled",
                "reference_count": len(checked_urls),
                "items": [],
            }
        by_key = {
            str(item.get("canonical_key") or item.get("reference_key") or ""): item
            for item in self._reference_items(context)
        }
        items: list[dict[str, Any]] = []
        for key, checked_url in zip(keys, checked_urls):
            item = by_key.get(key)
            if item:
                items.append(self._reference_preflight_audit_item(key, item, checked_url))
        return {
            "status": "pass",
            "reference_count": len(checked_urls),
            "trusted_person_reference_count": sum(
                1 for item in items if item.get("contains_person")
            ),
            "items": items,
        }

    def _reference_preflight_audit_item(
        self, key: str, item: dict[str, Any], checked_url: str
    ) -> dict[str, Any]:
        provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
        person = bool(contains_person(item))
        transport = "unknown"
        if checked_url.startswith("asset://"):
            transport = "ark_preset"
        elif checked_url.startswith("data:image/"):
            transport = "inline_original_or_nonperson"
        elif checked_url.startswith("https://"):
            if checked_url == str(item.get("remote_url") or "") and self._valid_ark_output_url(checked_url):
                transport = "ark_original_url"
            elif provenance.get("tos_uri"):
                transport = "tos_signed_url"
            else:
                transport = "https_url"
        return {
            "canonical_key": key,
            "contains_person": person,
            "trusted_portrait": True if person else None,
            "transport": transport,
            "provider": str(provenance.get("provider") or ""),
            "generation_mode": str(provenance.get("generation_mode") or ""),
            "tos_archived": bool(provenance.get("tos_uri")),
        }

    def _preflight_one_reference(
        self, client: httpx.Client, key: str, item: dict[str, Any], supplied_url: str
    ) -> str:
        url = supplied_url
        if self._is_combination_reference(item, key):
            source_ids = self._combination_source_ids(item)
            if any(source_id.startswith("char_") for source_id in source_ids) or contains_person(item):
                raise SeedanceError(
                    f"镜头参考图 {key} 是含人物 relationship combination，禁止直接提交给 Seedance；"
                    "必须展开为当前 isolated production truths"
                )
        if contains_person(item):
            provenance = item.get("provenance") or {}
            origin = str(provenance.get("origin") or "")
            if origin == "ark_preset" and str(provenance.get("asset_id") or "").startswith("asset://"):
                url = str(provenance["asset_id"])
            elif origin == "authorized_person" and provenance.get("authorization_id"):
                if not url.startswith("https://"):
                    raise SeedanceError(f"镜头参考图 {key} 的授权人像缺少可访问 URL")
            else:
                self._validate_original_person(key, item, provenance)
                remote = str(item.get("remote_url") or "")
                tos_uri = str(provenance.get("tos_uri") or "")
                if self._valid_ark_output_url(remote):
                    url = remote
                elif tos_uri and self.original_archive and self.original_archive.enabled:
                    try:
                        url = self.original_archive.signed_get_url(tos_uri)
                    except Exception as exc:
                        raise SeedanceError(f"镜头参考图 {key} 的 TOS 原始文件无法签名：{exc}") from exc
                else:
                    raise SeedanceError(
                        f"镜头参考图 {key} 的原始 URL 已失效，且没有可用的同账号 TOS 原始文件；"
                        "已停止提交，不会回退到本地 Base64"
                    )
        if url.startswith("https://"):
            try:
                with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
                    response.raise_for_status()
            except httpx.HTTPError as exc:
                raise SeedanceError(f"镜头参考图 {key} 的提交 URL 不可访问：{exc}") from exc
        elif url.startswith("data:image/"):
            local = Path(str(item.get("local_path") or ""))
            if not local.is_file():
                raise SeedanceError(f"镜头参考图 {key} 的本地文件缺失")
        elif not url.startswith("asset://"):
            raise SeedanceError(f"镜头参考图 {key} 没有可提交的图片地址")
        return url

    def _validate_original_person(
        self, key: str, item: dict[str, Any], provenance: dict[str, Any]
    ) -> None:
        if not self.account_id:
            raise SeedanceError(f"镜头参考图 {key} 无法核验同账号来源：请配置 ARK_ACCOUNT_ID")
        if provenance.get("provider") != "volcengine-ark" or provenance.get("account_id") != self.account_id:
            raise SeedanceError(f"镜头参考图 {key} 缺少同账号方舟原始产物记录")
        model = str(provenance.get("model") or item.get("model") or "").lower()
        if provenance.get("generation_mode") != "text_to_image" or not re.search(r"seedream-5-0-(?:pro|lite)", model):
            raise SeedanceError(f"镜头参考图 {key} 不是受信的 Seedream 5.0 pro/lite 文生图原始产物")
        try:
            generated = datetime.fromisoformat(str(provenance["generated_at"]).replace("Z", "+00:00"))
            if generated.tzinfo is None:
                raise ValueError("timezone missing")
        except (KeyError, ValueError, TypeError) as exc:
            raise SeedanceError(f"镜头参考图 {key} 缺少有效生成时间") from exc
        now = datetime.now(timezone.utc)
        if generated.astimezone(timezone.utc) < datetime(2026, 4, 16, tzinfo=timezone.utc) or not (generated <= now < generated + timedelta(days=30)):
            raise SeedanceError(f"镜头参考图 {key} 已超出方舟人像原始产物的 30 天受信期")
        local = Path(str(item.get("local_path") or ""))
        if not local.is_file() or not provenance.get("format_verified") or not provenance.get("original_bytes"):
            raise SeedanceError(f"镜头参考图 {key} 缺少可核验的未改动原始文件")
        with local.open("rb") as original:
            detected_format = image_format(original.read(16))
        if sha256_file(local) != provenance.get("sha256") or detected_format != provenance.get("media_format"):
            raise SeedanceError(f"镜头参考图 {key} 的原始文件格式或 SHA-256 校验失败")
        if str(provenance.get("original_url") or "") != str(item.get("remote_url") or ""):
            raise SeedanceError(f"镜头参考图 {key} 的原始 URL 与来源记录不一致")

    @staticmethod
    def _plan_by_shot(
        context: dict[str, Any], artifact_kind: str
    ) -> dict[int, dict[str, Any]]:
        for artifact in context.get("artifacts", []):
            if artifact.get("kind") != artifact_kind:
                continue
            return {
                int(item["shot_index"]): item
                for item in artifact.get("content", {}).get("items", [])
                if item.get("shot_index") is not None
            }
        return {}

    @staticmethod
    def _artifact_revisions(context: dict[str, Any]) -> dict[str, int]:
        tracked = {
            "storyboard",
            "dialogue_plan",
            "sound_plan",
            "reference_images",
            "characters",
            "scenes",
            "props",
            "asset_manifest",
        }
        result: dict[str, int] = {}
        for artifact in context.get("artifacts", []) or []:
            kind = str(artifact.get("kind") or "")
            if kind not in tracked:
                continue
            try:
                result[kind] = int(artifact.get("revision") or 0)
            except (TypeError, ValueError):
                result[kind] = 0
        return result

    @classmethod
    def _assembly_log(
        cls,
        context: dict[str, Any],
        shot: dict[str, Any],
        dialogue_plan: dict[str, Any],
        sound_plan: dict[str, Any],
    ) -> dict[str, Any]:
        lines = cls._dialogue_lines(dialogue_plan)
        lip_sync_targets: list[str] = []
        for line in lines:
            value = line.get("lip_sync_target") or line.get("speaker_id")
            if value:
                lip_sync_targets.append(str(value))
        plan_targets = dialogue_plan.get("lip_sync_targets") or dialogue_plan.get("lip_sync_target")
        if isinstance(plan_targets, list):
            lip_sync_targets.extend(str(value) for value in plan_targets if str(value).strip())
        elif plan_targets:
            lip_sync_targets.append(str(plan_targets))
        sound_fields = [
            key
            for key in ("ambience", "foley", "cues", "ducking", "negative_audio")
            if sound_plan.get(key) not in (None, "", [], {})
        ]
        ducking_value = sound_plan.get("ducking")
        ducking_active: bool | None = None
        if isinstance(ducking_value, dict):
            if "active" in ducking_value:
                ducking_active = bool(ducking_value.get("active"))
            elif "enabled" in ducking_value:
                ducking_active = bool(ducking_value.get("enabled"))
        elif isinstance(ducking_value, bool):
            ducking_active = ducking_value
        negative_audio_value = sound_plan.get("negative_audio")
        if isinstance(negative_audio_value, list):
            negative_audio = [str(value) for value in negative_audio_value if str(value).strip()]
        elif negative_audio_value in (None, ""):
            negative_audio = []
        else:
            negative_audio = [str(negative_audio_value)]
        return {
            "status": "pass",
            "upstream_revisions": cls._artifact_revisions(context),
            "storyboard": {
                "shot_index": int(shot.get("index") or 0),
                "visual_prompt_hydrated": bool(str(shot.get("visual_prompt") or "").strip()),
                "camera_hydrated": bool(shot.get("camera")),
                "blocking_hydrated": bool(shot.get("blocking")),
                "asset_bindings_hydrated": bool(shot.get("asset_bindings")),
            },
            "dialogue_plan": {
                "hydrated": bool(dialogue_plan),
                "language": str(dialogue_plan.get("language") or ""),
                "line_count": len(lines),
                "timing": dialogue_plan.get("timing") or dialogue_plan.get("timing_window") or "",
                "lip_sync_targets": list(dict.fromkeys(lip_sync_targets)),
            },
            "sound_plan": {
                "hydrated": bool(sound_plan),
                "fields_present": sound_fields,
                "ducking_active": ducking_active,
                "negative_audio": negative_audio,
                "negative_audio_count": len(negative_audio),
            },
        }

    @staticmethod
    def _request_payload_digest(
        *,
        model: str,
        prompt: str,
        reference_keys: list[str],
        duration: int,
        resolution: str,
        ratio: str,
        generate_audio: bool,
        upstream_revisions: dict[str, int],
        shot_index: int,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "shot_index": shot_index,
            "prompt": prompt,
            "reference_keys": reference_keys,
            "duration": duration,
            "resolution": resolution,
            "ratio": ratio,
            "generate_audio": generate_audio,
            "upstream_revisions": upstream_revisions,
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return {
            "sha256": hashlib.sha256(canonical).hexdigest(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_chars": len(prompt),
            "shot_index": shot_index,
            "model": model,
            "reference_keys": list(reference_keys),
            "upstream_revisions": dict(upstream_revisions),
            "technical_params": {
                "duration_seconds": duration,
                "resolution": resolution,
                "ratio": ratio,
                "generate_audio": generate_audio,
            },
        }

    @staticmethod
    def _provider_generate_audio_echo(task: dict[str, Any]) -> bool | None:
        containers = [task]
        content = task.get("content")
        if isinstance(content, dict):
            containers.append(content)
        for container in containers:
            value = container.get("generate_audio")
            if isinstance(value, bool):
                return value
        return None

    def _audio_probe(self, local_path: Path | None) -> dict[str, Any]:
        if local_path is None or not local_path.is_file():
            return {
                "status": "unavailable",
                "has_audio": None,
                "reason": "local_video_unavailable",
            }
        try:
            header = local_path.read_bytes()[:16]
        except OSError as exc:
            return {
                "status": "unavailable",
                "has_audio": None,
                "reason": f"local_video_unreadable: {exc}",
            }
        # Unit-test fixtures and failed downloads can be named .mp4 without being
        # an ISO BMFF/MP4 file. Avoid spawning ffprobe for obviously invalid data.
        if len(header) < 12 or header[4:8] != b"ftyp":
            return {
                "status": "unavailable",
                "has_audio": None,
                "reason": "not_probeable_mp4",
            }
        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index,codec_name,channels,sample_rate",
            "-of",
            "json",
            str(local_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            return {
                "status": "unavailable",
                "has_audio": None,
                "reason": f"ffprobe_unavailable: {exc}",
            }
        if completed.returncode != 0:
            return {
                "status": "unavailable",
                "has_audio": None,
                "reason": "ffprobe_failed",
                "detail": (completed.stderr or "")[:300],
            }
        try:
            body = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            return {
                "status": "unavailable",
                "has_audio": None,
                "reason": f"ffprobe_invalid_json: {exc}",
            }
        streams = body.get("streams") if isinstance(body, dict) else []
        if not isinstance(streams, list):
            streams = []
        first = streams[0] if streams and isinstance(streams[0], dict) else {}
        return {
            "status": "pass" if streams else "warn",
            "has_audio": bool(streams),
            "stream_count": len(streams),
            "codec": str(first.get("codec_name") or ""),
            "channels": first.get("channels"),
            "sample_rate": first.get("sample_rate"),
        }

    def _generate_one(
        self,
        workspace_id: str,
        shot: dict[str, Any],
        group: str,
        reference_urls: list[str],
        reference_keys: list[str],
        dialogue_plan: dict[str, Any],
        sound_plan: dict[str, Any],
        options: dict[str, Any] | None = None,
        *,
        context: dict[str, Any] | None = None,
        preflight_checks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        shot_index = int(shot.get("index", 1))
        duration = min(30, max(4, int(shot.get("duration_seconds", 10))))
        prompt = self._prompt(shot, duration, dialogue_plan, sound_plan, options)
        options = options or {}
        resolution = str(options.get("resolution") or self.resolution)
        ratio = str(options.get("ratio") or self.ratio)
        generate_audio = bool(options.get("generate_audio", self.generate_audio))
        context = context or {}
        assembly_log = self._assembly_log(context, shot, dialogue_plan, sound_plan)
        client, owns_client = self._get_client()
        try:
            model = self.model
            primary_error: SeedanceAPIError | None = None
            try:
                task_id = self._create_task(
                    client,
                    model,
                    prompt,
                    reference_urls,
                    duration=duration,
                    resolution=resolution,
                    ratio=ratio,
                    generate_audio=generate_audio,
                )
            except SeedanceAPIError as exc:
                primary_error = exc
                if exc.error_code.startswith("InputImageSensitiveContentDetected"):
                    affected = [
                        reference_keys[int(index) - 1]
                        for index in re.findall(r"content\[(\d+)\]", str(exc))
                        if 1 <= int(index) <= len(reference_keys)
                    ]
                    names = ", ".join(dict.fromkeys(affected)) or "未知参考图"
                    raise SeedanceError(
                        f"Seedance 拒绝了含人物图像的参考素材：{names}。"
                        "请使用火山方舟支持的可信原始模型产物、预置虚拟人像或已授权真人素材。"
                        f"原始错误：{exc}"
                    ) from exc
                if not self._should_try_fallback(exc, duration):
                    raise
                model = self.fallback_model
                try:
                    task_id = self._create_task(
                        client,
                        model,
                        prompt,
                        reference_urls,
                        duration=duration,
                        resolution=resolution,
                        ratio=ratio,
                        generate_audio=generate_audio,
                    )
                except SeedanceAPIError as fallback_error:
                    raise SeedanceError(
                        "Seedance primary and fallback models both failed. "
                        f"Primary [{self.model}]: {primary_error}. "
                        f"Fallback [{self.fallback_model}]: {fallback_error}. "
                        "If error code is ModelNotOpen, activate that exact model for the "
                        "same Ark account/API key or change VIDEO_MODEL/VIDEO_FALLBACK_MODEL."
                    ) from fallback_error
            task = self._wait_for_task(client, task_id)
            remote_url = self._video_url(task)
            if not remote_url:
                raise SeedanceError("Seedance task succeeded without a video URL")
            local_url, local_path, warning = self._download(
                client, remote_url, workspace_id, group, shot_index
            )
            provider_audio_echo = self._provider_generate_audio_echo(task)
            audio_probe = self._audio_probe(local_path)
            request_payload_digest = self._request_payload_digest(
                model=model,
                prompt=prompt,
                reference_keys=reference_keys,
                duration=duration,
                resolution=resolution,
                ratio=ratio,
                generate_audio=generate_audio,
                upstream_revisions=assembly_log.get("upstream_revisions") or {},
                shot_index=shot_index,
            )
            result = {
                "shot_index": shot_index,
                "provider_job_id": task_id,
                "model": model,
                "url": local_url or remote_url,
                "remote_url": remote_url,
                "local_path": str(local_path) if local_path else None,
                "duration_seconds": duration,
                "resolution": resolution,
                "ratio": ratio,
                "generate_audio": generate_audio,
                "reference_image_count": len(reference_urls),
                "reference_keys": reference_keys,
                "request_payload_digest": request_payload_digest,
                "assembly_log": assembly_log,
                "preflight_checks": preflight_checks or {
                    "status": "skipped",
                    "reason": "audit_not_supplied",
                },
                "provider_generate_audio_echo": provider_audio_echo,
                "audio_probe": audio_probe,
                "status": "succeeded",
            }
            if warning:
                result["warning"] = warning
            return result
        finally:
            if owns_client:
                client.close()

    def _should_try_fallback(self, exc: SeedanceAPIError, duration: int) -> bool:
        if not self.fallback_model or self.fallback_model == self.model:
            return False
        if "seedance-2-0" in self.fallback_model.lower() and duration > 15:
            return False
        if exc.error_code in self._FALLBACK_MODEL_ERROR_CODES:
            return True
        message = str(exc).lower()
        return exc.status_code == 404 and "model" in message

    @staticmethod
    def _dialogue_lines(plan: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("lines", "dialogue_lines"):
            value = plan.get(key)
            if isinstance(value, list):
                result: list[dict[str, Any]] = []
                for line in value:
                    if isinstance(line, dict):
                        result.append(line)
                    elif isinstance(line, str) and line.strip():
                        result.append({"text": line})
                if result:
                    return result
        value = plan.get("dialogue")
        if isinstance(value, list):
            result = []
            for line in value:
                if isinstance(line, dict):
                    result.append(line)
                elif isinstance(line, str) and line.strip():
                    result.append({"text": line})
            if result:
                return result
        # Keep field precedence explicit across legacy/new dialogue-plan schemas.
        text = plan.get("dialogue_text")
        if text in (None, ""):
            text = plan.get("text")
        if text in (None, "") and isinstance(plan.get("dialogue"), str):
            text = plan.get("dialogue")
        if text not in (None, ""):
            return [
                {
                    "speaker_id": plan.get("speaker_id"),
                    "text": text,
                    "timing": plan.get("timing"),
                    "delivery_note": plan.get("delivery_note") or plan.get("delivery"),
                }
            ]
        return []

    @staticmethod
    def _compact_value(value: Any) -> str:
        if value in (None, "", [], {}):
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            return str(value)

    def _prompt(
        self,
        shot: dict[str, Any],
        duration: int,
        dialogue_plan: dict[str, Any] | None = None,
        sound_plan: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> str:
        options = options or {}
        parts = [str(shot.get("visual_prompt", "")).strip()]
        dialogue_plan = dialogue_plan or {}
        sound_plan = sound_plan or {}
        lines = self._dialogue_lines(dialogue_plan)
        if lines:
            rendered_lines = []
            default_speaker = str(dialogue_plan.get("speaker_id") or "on-screen speaker")
            language = str(dialogue_plan.get("language") or "target language")
            for line in lines:
                text = str(
                    line.get("dialogue_text") or line.get("text") or line.get("dialogue") or ""
                ).strip()
                if not text:
                    continue
                speaker = str(line.get("speaker_id") or default_speaker)
                timing = self._compact_value(line.get("timing") or dialogue_plan.get("timing")) or "within the shot"
                rendered_lines.append(f"{speaker} @ {timing}: {text}")
            if rendered_lines:
                parts.append(
                    "Spoken dialogue. Preserve every quoted line exactly; do not merge speakers, "
                    "rewrite words, or invent speech. Language="
                    + language
                    + ". Synchronize the visible speaker's lips to each line:\n"
                    + "\n".join(rendered_lines)
                )
        else:
            parts.append("No spoken dialogue; do not invent speech or mouth movements.")
        audio = self._sound_text(sound_plan) or str(shot.get("audio_prompt", "")).strip()
        if audio:
            parts.append("Environment and Foley only: " + audio)
        parts.append("Keep character identity, wardrobe and scene continuity stable.")
        prompt_addendum = str(options.get("prompt_addendum") or "").strip()
        if prompt_addendum:
            parts.append("Art director requirements: " + prompt_addendum)
        # Ark's current video-generation API accepts resolution / ratio / duration /
        # generate_audio as request-body fields. Do not encode them as pseudo prompt
        # flags because that makes provider behavior ambiguous and hard to audit.
        return "\n".join(part for part in parts if part)

    @classmethod
    def _sound_text(cls, plan: dict[str, Any]) -> str:
        fields = []
        for key in ("ambience", "foley", "cues", "ducking", "negative_audio"):
            value = plan.get(key)
            if value not in (None, "", []):
                fields.append(f"{key}={cls._compact_value(value)}")
        return "; ".join(fields)

    def _get_client(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.Client(timeout=120, follow_redirects=True), True

    def _create_task(
        self,
        client: httpx.Client,
        model: str,
        prompt: str,
        reference_urls: list[str],
        *,
        duration: int,
        resolution: str,
        ratio: str,
        generate_audio: bool,
    ) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": url},
                "role": "reference_image",
            }
            for url in reference_urls
        )
        inline_bytes = sum(
            len(part.get("image_url", {}).get("url", ""))
            for part in content
            if part.get("type") == "image_url"
            and str(part.get("image_url", {}).get("url", "")).startswith("data:image/")
        )
        # Keep comfortably below Ark's 64 MB request-body ceiling after JSON
        # overhead. Typical workbench images are far smaller than this.
        if inline_bytes > 56 * 1024 * 1024:
            raise SeedanceError(
                "Seedance reference payload is too large after base64 encoding "
                f"({inline_bytes / 1024 / 1024:.1f} MB). Reduce the number or size "
                "of bound reference images for this shot."
            )
        response = client.post(
            f"{self.base_url}/contents/generations/tasks",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "content": content,
                "resolution": resolution,
                "ratio": ratio,
                "duration": duration,
                "generate_audio": generate_audio,
            },
        )
        if response.is_error:
            detail = response.text[:800]
            error_code = ""
            try:
                body = response.json()
                error = body.get("error") if isinstance(body, dict) else None
                if isinstance(error, dict):
                    error_code = str(error.get("code") or "")
            except Exception:
                pass
            raise SeedanceAPIError(
                f"Seedance task creation failed ({response.status_code})"
                + (f" [{error_code}]" if error_code else "")
                + f" for model {model}: {detail}",
                response.status_code,
                error_code=error_code,
                model=model,
            )
        task_id = response.json().get("id")
        if not task_id:
            raise SeedanceAPIError(
                f"Seedance response did not contain a task id for model {model}",
                model=model,
            )
        return str(task_id)

    def _wait_for_task(self, client: httpx.Client, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        headers = {"Authorization": f"Bearer {self.api_key}"}
        while time.monotonic() < deadline:
            response = client.get(
                f"{self.base_url}/contents/generations/tasks/{task_id}",
                headers=headers,
            )
            if response.is_error:
                raise SeedanceAPIError(
                    f"Seedance task query failed ({response.status_code}): "
                    f"{response.text[:800]}",
                    response.status_code,
                )
            task = response.json()
            status = str(task.get("status", "")).lower()
            if status == "succeeded":
                return task
            if status in {"failed", "cancelled", "canceled", "expired"}:
                error = task.get("error") or task.get("message") or status
                raise SeedanceError(f"Seedance task {task_id} ended as {status}: {error}")
            self._sleep(self.poll_interval_seconds)
        raise SeedanceError(
            f"Seedance task {task_id} exceeded {self.poll_timeout_seconds}s timeout"
        )

    @staticmethod
    def _video_url(task: dict[str, Any]) -> str:
        content = task.get("content") or {}
        if isinstance(content, dict):
            return str(content.get("video_url") or content.get("url") or "")
        return str(task.get("video_url") or "")

    def _download(
        self,
        client: httpx.Client,
        remote_url: str,
        workspace_id: str,
        group: str,
        shot_index: int,
    ) -> tuple[str | None, Path | None, str | None]:
        target_dir = self.output_dir / workspace_id / group
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"shot-{shot_index:03d}.mp4"
        try:
            response = client.get(remote_url)
            response.raise_for_status()
            target.write_bytes(response.content)
        except Exception as exc:
            return None, None, f"视频已生成，但本地下载失败：{exc}"
        relative = target.relative_to(self.output_dir).as_posix()
        return f"/media/{relative}", target, None
