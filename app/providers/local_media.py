from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .base import WorkflowProvider
from app.guidance import sanitize_parameter_overrides


class LocalMediaError(RuntimeError):
    pass


class LocalMediaProvider(WorkflowProvider):
    """Use Seedance native audio and concatenate successful shots with FFmpeg."""

    name = "local-ffmpeg"

    def __init__(self, *, ffmpeg_path: str, output_dir: Path):
        self.ffmpeg_path = ffmpeg_path
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        if stage == "music":
            plan = next(
                (
                    artifact.get("content", {})
                    for artifact in context.get("artifacts", [])
                    if artifact.get("kind") == "music_plan"
                ),
                {},
            )
            return {
                "mode": "seedance_native_audio_without_separate_bgm",
                "status": "ready",
                "plan": plan,
                "note": "当前沿用 Seedance 2.5 镜头原生对白与环境音；已生成全片 BGM 方案，但未配置独立音乐生成 API，因此不会声称已渲染独立 BGM。",
            }
        if stage == "compose":
            return self._compose(context)
        if stage == "delivery_qa":
            return self._delivery_qa(context)
        raise NotImplementedError(f"Local media does not handle stage: {stage}")

    def _compose(self, context: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(context["workspace"]["id"])
        directive = context.get("execution_directive") or {}
        params = sanitize_parameter_overrides("compose", directive.get("parameter_overrides"))
        video_crf = int(params.get("video_crf", 20))
        audio_bitrate_kbps = int(params.get("audio_bitrate_kbps", 192))
        batch = next(
            (
                artifact.get("content", {})
                for artifact in context.get("artifacts", [])
                if artifact.get("kind") == "batch_video"
            ),
            {},
        )
        paths = [
            Path(item["local_path"])
            for item in batch.get("items", [])
            if item.get("status") == "succeeded" and item.get("local_path")
        ]
        if not paths or any(not path.is_file() for path in paths):
            raise LocalMediaError("No complete local Seedance shot files available for compose")

        target_dir = self.output_dir / workspace_id / "compose"
        target_dir.mkdir(parents=True, exist_ok=True)
        concat_file = target_dir / "concat.txt"
        concat_file.write_text(
            "".join(f"file '{self._concat_escape(path.resolve())}'\n" for path in paths),
            encoding="utf-8",
        )
        target = target_dir / "final.mp4"
        command = [
            self.ffmpeg_path,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            str(video_crf),
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_bitrate_kbps}k",
            "-movflags",
            "+faststart",
            str(target),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LocalMediaError(f"FFmpeg could not run: {exc}") from exc
        if result.returncode != 0 or not target.is_file():
            detail = (result.stderr or result.stdout)[-1600:]
            raise LocalMediaError(f"FFmpeg compose failed: {detail}")
        relative = target.relative_to(self.output_dir).as_posix()
        return {
            "url": f"/media/{relative}",
            "local_path": str(target),
            "input_shots": len(paths),
            "status": "succeeded",
            "encoding": {
                "video": f"H.264 / CRF {video_crf}",
                "audio": f"AAC / {audio_bitrate_kbps}k",
                "faststart": True,
            },
            "note": "本地 FFmpeg 已统一编码并拼接所有成功镜头。",
        }

    def _delivery_qa(self, context: dict[str, Any]) -> dict[str, Any]:
        compose = next(
            (
                artifact.get("content", {})
                for artifact in context.get("artifacts", [])
                if artifact.get("kind") == "compose"
            ),
            {},
        )
        batch = next(
            (
                artifact.get("content", {})
                for artifact in context.get("artifacts", [])
                if artifact.get("kind") == "batch_video"
            ),
            {},
        )
        target = Path(str(compose.get("local_path", "")))
        if not target.is_file() or target.stat().st_size <= 0:
            raise LocalMediaError("Final master is missing or empty")

        ffprobe = Path(self.ffmpeg_path).with_name("ffprobe.exe")
        if not ffprobe.is_file():
            ffprobe = Path(self.ffmpeg_path).with_name("ffprobe")
        command = [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height",
            "-of",
            "json",
            str(target),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LocalMediaError(f"FFprobe could not run: {exc}") from exc
        if result.returncode != 0:
            raise LocalMediaError(f"FFprobe failed: {(result.stderr or result.stdout)[-1200:]}")
        try:
            probe = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LocalMediaError("FFprobe returned invalid JSON") from exc

        streams = probe.get("streams", [])
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        expected_items = list(batch.get("items", []))
        successful = [item for item in expected_items if item.get("status") == "succeeded"]
        checks: list[dict[str, Any]] = []
        blocking: list[str] = []

        def add(name: str, ok: bool, evidence: str) -> None:
            status = "pass" if ok else "fail"
            checks.append({"name": name, "status": status, "evidence": evidence})
            if not ok:
                blocking.append(name)

        add("master_file", True, f"{target} ({target.stat().st_size} bytes)")
        add("video_stream", bool(video), str(video or "missing"))
        add("audio_stream", bool(audio), str(audio or "missing"))
        add(
            "shot_count",
            bool(successful) and int(compose.get("input_shots", -1)) == len(successful),
            f"compose={compose.get('input_shots')}, succeeded={len(successful)}",
        )
        duration = float(probe.get("format", {}).get("duration") or 0)
        add("duration", duration > 0, f"{duration:.3f}s")

        target_ratio = str(context.get("workspace", {}).get("settings", {}).get("aspect_ratio", ""))
        ratio_ok = True
        ratio_evidence = "target not specified"
        if video and target_ratio == "9:16":
            width = int(video.get("width") or 0)
            height = int(video.get("height") or 0)
            ratio_ok = width > 0 and height > 0 and abs((width / height) - (9 / 16)) < 0.035
            ratio_evidence = f"{width}x{height}, target=9:16"
        add("aspect_ratio", ratio_ok, ratio_evidence)

        return {
            "status": "pass" if not blocking else "fail",
            "checks": checks,
            "blocking_failures": blocking,
            "probe": probe,
            "local_path": str(target),
        }

    @staticmethod
    def _concat_escape(path: Path) -> str:
        return path.as_posix().replace("'", "'\\''")
