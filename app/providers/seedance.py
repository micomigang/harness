from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from .base import WorkflowProvider
from app.guidance import requested_preview_shot_index, sanitize_parameter_overrides
from app.image_provenance import contains_person, image_format, sha256_file
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
        output_dir: Path,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        account_id: str = "",
        original_archive: TosArchive | None = None,
        preflight_enabled: bool = False,
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
        self.batch_max_shots = max(1, batch_max_shots)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._sleep = sleeper
        self.account_id = account_id.strip()
        self.original_archive = original_archive
        self.preflight_enabled = preflight_enabled

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
            reference_urls = self._preflight_reference_urls(context, reference_keys, reference_urls)
            return self._generate_one(
                workspace_id,
                shot,
                "preview",
                reference_urls,
                reference_keys,
                dialogue_by_shot.get(shot_index, {}),
                sound_by_shot.get(shot_index, {}),
                options,
            )
        if stage == "batch_video":
            if not shots:
                raise SeedanceError("Storyboard has no shots")
            items: list[dict[str, Any]] = []
            batch_max_shots = int(params.get("batch_max_shots", self.batch_max_shots))
            selected = shots[: batch_max_shots]
            prepared: list[tuple[dict[str, Any], list[str], list[str]]] = []
            # Check the complete batch before creating any billable video task.
            for shot in selected:
                reference_urls, reference_keys = self._shot_reference_urls(
                    context, shot, max_images=max_reference_images
                )
                reference_urls = self._preflight_reference_urls(
                    context, reference_keys, reference_urls
                )
                prepared.append((shot, reference_urls, reference_keys))
            for shot, reference_urls, reference_keys in prepared:
                try:
                    shot_index = int(shot.get("index", 1))
                    items.append(
                        self._generate_one(
                            workspace_id,
                            shot,
                            "batch",
                            reference_urls,
                            reference_keys,
                            dialogue_by_shot.get(shot_index, {}),
                            sound_by_shot.get(shot_index, {}),
                            options,
                        )
                    )
                except SeedanceError as exc:
                    items.append(
                        {
                            "shot_index": shot.get("index"),
                            "status": "failed",
                            "error": str(exc),
                        }
                    )
                    break
            completed = sum(item.get("status") == "succeeded" for item in items)
            return {
                "items": items,
                "requested": len(selected),
                "completed": completed,
                "status": "succeeded" if completed == len(selected) else "partial_failed",
                "note": "首个失败镜头后停止继续提交，避免意外消耗额度。",
            }
        raise NotImplementedError(f"Seedance does not handle stage: {stage}")

    @staticmethod
    def _storyboard_shots(context: dict[str, Any]) -> list[dict[str, Any]]:
        for artifact in context.get("artifacts", []):
            if artifact.get("kind") == "storyboard":
                return list(artifact.get("content", {}).get("shots", []))
        return []

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

    @classmethod
    def _reference_items(cls, context: dict[str, Any]) -> list[dict[str, Any]]:
        selected_by_key = {
            str(candidate.get("canonical_key") or "").strip(): candidate
            for candidate in context.get("asset_candidates", []) or []
            if isinstance(candidate, dict)
            and candidate.get("stage") == "reference_images"
            and candidate.get("selected")
            and str(candidate.get("canonical_key") or "").strip()
        }
        for artifact in context.get("artifacts", []):
            if artifact.get("kind") == "reference_images":
                items = []
                for item in artifact.get("content", {}).get("items", []):
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("canonical_key") or item.get("reference_key") or "").strip()
                    selected = selected_by_key.get(key)
                    if selected:
                        # Candidate selection is the current visual authority. The
                        # storyboard may still carry the previous candidate_id;
                        # retain its canonical binding but use the selected bytes.
                        item = {
                            **item,
                            **{field: selected.get(field) for field in (
                                "model", "remote_url", "url", "local_path", "provenance"
                            )},
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

    @classmethod
    def _shot_reference_urls(
        cls, context: dict[str, Any], shot: dict[str, Any], *, max_images: int = 9
    ) -> tuple[list[str], list[str]]:
        """Resolve provider-reachable references for exactly this shot.

        Storyboard bindings normally carry local /media URLs, which Ark cannot
        fetch. Resolve those bindings back to reference_images production-truth
        items and use their remote_url. Relationship combinations are preferred
        because one composition can preserve several bound assets at once.
        """
        items = cls._reference_items(context)
        bindings = (shot.get("asset_bindings") or {}).get("reference_images") or []
        if not items:
            if bindings:
                raise SeedanceError("镜头已绑定参考图，但 reference_images 产物缺失")
            return [], []

        by_candidate: dict[str, dict[str, Any]] = {}
        by_key: dict[str, dict[str, Any]] = {}
        by_signature: dict[tuple[str, ...], dict[str, Any]] = {}
        for item in items:
            candidate_id = str(item.get("candidate_id") or "").strip()
            canonical_key = str(item.get("canonical_key") or item.get("id") or "").strip()
            if candidate_id:
                by_candidate[candidate_id] = item
            if canonical_key:
                by_key[canonical_key] = item
            source_id = item.get("source_id") or item.get("source_ids")
            if isinstance(source_id, list):
                signature = tuple(sorted(str(value) for value in source_id if value))
                if signature:
                    by_signature[signature] = item

        if not isinstance(bindings, list) or not bindings:
            return cls._reference_urls(context), []
        if len(bindings) > max_images:
            raise SeedanceError(f"当前视频模型最多支持 {max_images} 张参考图")

        def priority(binding: Any) -> int:
            if isinstance(binding, dict):
                kind = str(binding.get("source_kind") or "").lower()
                key = str(binding.get("canonical_key") or binding.get("id") or "")
                if kind == "combination" or key.startswith("combo__"):
                    return 0
            elif isinstance(binding, str) and binding.startswith("combo__"):
                return 0
            return 1

        urls: list[str] = []
        keys: list[str] = []
        unresolved: list[str] = []
        for binding in sorted(bindings, key=priority):
            matched: dict[str, Any] | None = None
            display_key = ""
            if isinstance(binding, str):
                display_key = binding.strip()
                matched = by_key.get(display_key)
            elif isinstance(binding, dict):
                candidate_id = str(binding.get("candidate_id") or "").strip()
                display_key = str(
                    binding.get("canonical_key") or binding.get("id") or ""
                ).strip()
                if candidate_id:
                    matched = by_candidate.get(candidate_id)
                if matched is None and display_key:
                    matched = by_key.get(display_key)
                source_id = binding.get("source_id") or binding.get("source_ids")
                if matched is None and isinstance(source_id, list):
                    signature = tuple(sorted(str(value) for value in source_id if value))
                    matched = by_signature.get(signature)
                if matched is None:
                    direct_url = cls._reference_input_value(binding)
                    if direct_url and direct_url not in urls:
                        urls.append(direct_url)
                        keys.append(display_key or candidate_id or "direct_reference")
                    elif not direct_url:
                        unresolved.append(display_key or candidate_id or "unknown_reference")
                    continue
            if matched is None:
                unresolved.append(display_key or "unknown_reference")
                continue
            url = cls._reference_input_value(matched)
            if not url:
                unresolved.append(display_key or "unknown_reference")
                continue
            if url in urls:
                continue
            urls.append(url)
            keys.append(
                str(matched.get("canonical_key") or matched.get("id") or display_key or "reference")
            )
        if unresolved:
            raise SeedanceError("镜头参考图不可用：" + ", ".join(unresolved))
        return urls, keys

    def _preflight_reference_urls(
        self, context: dict[str, Any], keys: list[str], urls: list[str]
    ) -> list[str]:
        if not self.preflight_enabled:
            return urls
        by_key = {
            str(item.get("canonical_key") or item.get("reference_key") or ""): item
            for item in self._reference_items(context)
        }
        checked: list[str] = []
        failures: list[str] = []
        client, owns_client = self._get_client()
        try:
            for key, supplied_url in zip(keys, urls):
                item = by_key.get(key)
                if not item:
                    failures.append(f"镜头参考图 {key} 缺少可核验的来源记录")
                    continue
                try:
                    checked.append(self._preflight_one_reference(client, key, item, supplied_url))
                except SeedanceError as exc:
                    failures.append(str(exc))
        finally:
            if owns_client:
                client.close()
        if failures:
            raise SeedanceError("镜头参考图预检未通过：" + "；".join(failures))
        return checked

    def _preflight_one_reference(
        self, client: httpx.Client, key: str, item: dict[str, Any], supplied_url: str
    ) -> str:
        url = supplied_url
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
    ) -> dict[str, Any]:
        shot_index = int(shot.get("index", 1))
        duration = min(30, max(4, int(shot.get("duration_seconds", 10))))
        prompt = self._prompt(shot, duration, dialogue_plan, sound_plan, options)
        options = options or {}
        resolution = str(options.get("resolution") or self.resolution)
        ratio = str(options.get("ratio") or self.ratio)
        generate_audio = bool(options.get("generate_audio", self.generate_audio))
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
