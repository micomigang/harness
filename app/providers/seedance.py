from __future__ import annotations

import base64
import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .base import WorkflowProvider
from app.guidance import sanitize_parameter_overrides


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
        if stage == "preview":
            if not shots:
                raise SeedanceError("Storyboard has no shots")
            shot = shots[0]
            shot_index = int(shot.get("index", 1))
            reference_urls, reference_keys = self._shot_reference_urls(context, shot)
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
            for shot in selected:
                try:
                    shot_index = int(shot.get("index", 1))
                    reference_urls, reference_keys = self._shot_reference_urls(context, shot)
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
        """Return an Ark-readable reference image value.

        Prefer the locally cached production-truth image and inline it as a data
        URI. Seedream provider URLs may be temporary or protected from server-side
        fetching, which makes them unsuitable as cross-provider references. Ark's
        video API accepts data:image/...;base64,... for image_url.url, so using
        the cached bytes avoids public-URL reachability and expiry problems.
        """
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

    @classmethod
    def _reference_items(cls, context: dict[str, Any]) -> list[dict[str, Any]]:
        for artifact in context.get("artifacts", []):
            if artifact.get("kind") == "reference_images":
                return [
                    item
                    for item in artifact.get("content", {}).get("items", [])
                    if isinstance(item, dict)
                ]
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
        cls, context: dict[str, Any], shot: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        """Resolve provider-reachable references for exactly this shot.

        Storyboard bindings normally carry local /media URLs, which Ark cannot
        fetch. Resolve those bindings back to reference_images production-truth
        items and use their remote_url. Relationship combinations are preferred
        because one composition can preserve several bound assets at once.
        """
        items = cls._reference_items(context)
        if not items:
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

        bindings = (shot.get("asset_bindings") or {}).get("reference_images") or []
        if not isinstance(bindings, list) or not bindings:
            return cls._reference_urls(context), []

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
                        if len(urls) >= 4:
                            break
                    continue
            if matched is None:
                continue
            url = cls._reference_input_value(matched)
            if not url or url in urls:
                continue
            urls.append(url)
            keys.append(
                str(matched.get("canonical_key") or matched.get("id") or display_key or "reference")
            )
            if len(urls) >= 4:
                break

        if not urls:
            return cls._reference_urls(context), []
        return urls, keys

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
