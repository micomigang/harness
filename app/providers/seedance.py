from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .base import WorkflowProvider
from app.guidance import sanitize_parameter_overrides


class SeedanceError(RuntimeError):
    pass


class SeedanceAPIError(SeedanceError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SeedanceProvider(WorkflowProvider):
    """Fire Ark Seedance async video adapter.

    The adapter creates an Ark video task, polls it to a terminal state, and
    downloads successful results because provider URLs may expire.
    """

    name = "seedance"

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
        reference_urls = self._reference_urls(context)
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
            return self._generate_one(
                workspace_id,
                shot,
                "preview",
                reference_urls,
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
                    items.append(
                        self._generate_one(
                            workspace_id,
                            shot,
                            "batch",
                            reference_urls,
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
    def _reference_urls(context: dict[str, Any]) -> list[str]:
        for artifact in context.get("artifacts", []):
            if artifact.get("kind") != "reference_images":
                continue
            return [
                str(item["remote_url"])
                for item in artifact.get("content", {}).get("items", [])
                if item.get("remote_url")
            ][:4]
        return []

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
        dialogue_plan: dict[str, Any],
        sound_plan: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        shot_index = int(shot.get("index", 1))
        duration = min(30, max(4, int(shot.get("duration_seconds", 10))))
        prompt = self._prompt(shot, duration, dialogue_plan, sound_plan, options)
        client, owns_client = self._get_client()
        try:
            model = self.model
            try:
                task_id = self._create_task(client, model, prompt, reference_urls)
            except SeedanceAPIError as exc:
                if not self.fallback_model or exc.status_code not in {400, 403, 404}:
                    raise
                model = self.fallback_model
                task_id = self._create_task(client, model, prompt, reference_urls)
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
                "status": "succeeded",
            }
            if warning:
                result["warning"] = warning
            return result
        finally:
            if owns_client:
                client.close()

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
        dialogue = str(dialogue_plan.get("text") or shot.get("dialogue_prompt", "")).strip()
        if dialogue:
            speaker = str(dialogue_plan.get("speaker_id") or "on-screen speaker")
            language = str(dialogue_plan.get("language") or "target language")
            timing = str(dialogue_plan.get("timing") or "within the shot")
            parts.append(
                "Spoken dialogue: speaker="
                + speaker
                + "; language="
                + language
                + "; timing="
                + timing
                + "; preserve these exact words and synchronize visible lips: "
                + dialogue
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
        ratio = str(options.get("ratio") or self.ratio)
        resolution = str(options.get("resolution") or self.resolution)
        generate_audio = bool(options.get("generate_audio", self.generate_audio))
        parts.append(
            f"--ratio {ratio} --duration {duration} "
            f"--resolution {resolution} "
            f"--generate_audio {str(generate_audio).lower()}"
        )
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _sound_text(plan: dict[str, Any]) -> str:
        fields = []
        for key in ("ambience", "foley", "cues", "ducking", "negative_audio"):
            value = plan.get(key)
            if value not in (None, "", []):
                fields.append(f"{key}={value}")
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
        response = client.post(
            f"{self.base_url}/contents/generations/tasks",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "content": content,
            },
        )
        if response.is_error:
            detail = response.text[:800]
            raise SeedanceAPIError(
                f"Seedance task creation failed ({response.status_code}): {detail}",
                response.status_code,
            )
        task_id = response.json().get("id")
        if not task_id:
            raise SeedanceAPIError("Seedance response did not contain a task id")
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
