from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any


class SourceMediaError(RuntimeError):
    pass


class SourceMediaProcessor:
    """Probe uploaded videos and build adaptive visual evidence for source analysis.

    The processor keeps a dense time-based baseline (1 fps by default), then adds
    frames at detected scene cuts. All extracted frames are retained locally; a
    bounded subset is marked for multimodal model input so long or highly edited
    videos cannot create unbounded API requests.
    """

    VIDEO_EXTENSIONS = {
        ".mp4",
        ".mov",
        ".mkv",
        ".webm",
        ".avi",
        ".m4v",
        ".ts",
    }
    _PTS_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        output_dir: Path,
        base_fps: float = 1.0,
        scene_threshold: float = 0.12,
        max_base_frames: int = 1200,
        max_scene_frames: int = 300,
        max_model_frames: int = 480,
        max_model_image_bytes: int = 48_000_000,
        frame_width: int = 768,
        media_cache_enabled: bool = True,
    ):
        self.ffmpeg_path = ffmpeg_path
        ffmpeg = Path(ffmpeg_path)
        sibling_name = "ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe"
        sibling = ffmpeg.with_name(sibling_name)
        self.ffprobe_path = str(sibling) if sibling.is_file() else "ffprobe"
        self.output_dir = output_dir
        self.base_fps = max(0.1, min(float(base_fps), 10.0))
        self.scene_threshold = max(0.05, min(float(scene_threshold), 0.95))
        self.max_base_frames = max(1, int(max_base_frames))
        self.max_scene_frames = max(0, int(max_scene_frames))
        self.max_model_frames = max(1, int(max_model_frames))
        self.max_model_image_bytes = max(1_000_000, int(max_model_image_bytes))
        self.frame_width = max(320, min(int(frame_width), 1920))
        self.media_cache_enabled = bool(media_cache_enabled)

    def process(
        self, workspace_id: str, files: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        for file_info in files:
            source = Path(str(file_info.get("path", "")))
            content_type = str(file_info.get("content_type", ""))
            if not source.is_file():
                continue
            if not content_type.startswith("video/") and source.suffix.lower() not in self.VIDEO_EXTENSIONS:
                continue
            source_sha256 = str(file_info.get("sha256") or "")
            # Existing workspaces may contain the same upload multiple times from
            # older builds. De-duplicate here too, so a legacy workspace cannot
            # multiply segment requests even before its source artifact is edited.
            identity = source_sha256 or str(source.resolve()).lower()
            if identity in seen_sources:
                continue
            seen_sources.add(identity)
            results.append(
                self._process_video(
                    workspace_id,
                    source,
                    source_sha256=source_sha256,
                )
            )
        return results

    def _process_video(
        self,
        workspace_id: str,
        source: Path,
        *,
        source_sha256: str = "",
    ) -> dict[str, Any]:
        target_dir = self.output_dir / workspace_id / "source_frames" / source.stem
        target_dir.mkdir(parents=True, exist_ok=True)
        cache_identity = self._media_cache_identity(source, source_sha256)
        if self.media_cache_enabled:
            cached = self._load_media_cache(target_dir, cache_identity)
            if cached is not None:
                cached["path"] = str(source)
                cached["source_sha256"] = source_sha256
                cached["media_cache"] = {"status": "hit"}
                return cached

        probe = self._probe(source)
        duration = self._duration(probe)
        self._clear_generated_previews(target_dir)

        effective_fps = self._effective_fps(
            duration=duration,
            requested_fps=self.base_fps,
            max_frames=self.max_base_frames,
        )
        base_frames = self._extract_base_frames(
            source=source,
            target_dir=target_dir,
            duration=duration,
            fps=effective_fps,
        )
        scene_frames = self._extract_scene_frames(source=source, target_dir=target_dir)
        frames = self._merge_frames(base_frames, scene_frames)
        model_frame_indexes = self._select_model_frame_indexes(frames)
        for index, frame in enumerate(frames, 1):
            frame["index"] = index
            frame["selected_for_model"] = index - 1 in model_frame_indexes

        streams = probe.get("streams", [])
        audio_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"),
            None,
        )
        audio_preview = self._extract_audio(source, target_dir) if audio_stream else None
        selected_frames = [frame for frame in frames if frame["selected_for_model"]]
        selected_bytes = sum(self._frame_size(frame) for frame in selected_frames)
        result = {
            "name": source.name,
            "path": str(source),
            "source_sha256": source_sha256,
            "duration_seconds": round(duration, 3),
            "size_bytes": int(probe.get("format", {}).get("size") or source.stat().st_size),
            "format": probe.get("format", {}).get("format_name"),
            "video_stream": next(
                (stream for stream in streams if stream.get("codec_type") == "video"),
                None,
            ),
            "audio_stream": audio_stream,
            "audio_preview": audio_preview,
            "sampling": {
                "requested_base_fps": round(self.base_fps, 4),
                "effective_base_fps": round(effective_fps, 4),
                "scene_threshold": round(self.scene_threshold, 4),
                "base_frames": len(base_frames),
                "scene_frames": len(scene_frames),
                "total_frames": len(frames),
                "model_frames": len(selected_frames),
                "model_image_bytes": selected_bytes,
                "max_model_frames": self.max_model_frames,
                "max_model_image_bytes": self.max_model_image_bytes,
            },
            "frames": frames,
            "media_cache": {"status": "miss"},
        }
        if self.media_cache_enabled:
            self._write_media_cache(target_dir, cache_identity, result)
        return result

    def _extract_base_frames(
        self,
        *,
        source: Path,
        target_dir: Path,
        duration: float,
        fps: float,
    ) -> list[dict[str, Any]]:
        pattern = target_dir / "base-%05d.jpg"
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "info",
            "-y",
            "-i",
            str(source),
            "-an",
            "-vf",
            f"fps={fps:.8f},showinfo,scale={self.frame_width}:-2",
            "-q:v",
            "3",
            "-frames:v",
            str(self.max_base_frames),
            str(pattern),
        ]
        result = self._run_ffmpeg(command, timeout=self._video_timeout(duration))
        files = sorted(target_dir.glob("base-*.jpg"))
        if result.returncode != 0 or not files:
            raise SourceMediaError(
                f"FFmpeg base-frame extraction failed for {source.name}: {(result.stderr or "")[-1200:]}"
            )
        timestamps = self._parse_pts_times(result.stderr)
        if len(timestamps) < len(files):
            timestamps.extend(index / fps for index in range(len(timestamps), len(files)))
        return [
            self._frame_record(
                target=file,
                timestamp=min(max(timestamps[index], 0.0), duration),
                kind="base",
            )
            for index, file in enumerate(files)
        ]

    def _extract_scene_frames(
        self, *, source: Path, target_dir: Path
    ) -> list[dict[str, Any]]:
        if self.max_scene_frames <= 0:
            return []
        pattern = target_dir / "scene-%05d.jpg"
        select_filter = f"select=gt(scene\\,{self.scene_threshold:.6f})"
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "info",
            "-y",
            "-i",
            str(source),
            "-an",
            "-vf",
            f"{select_filter},showinfo,scale={self.frame_width}:-2",
            "-fps_mode",
            "vfr",
            "-q:v",
            "3",
            "-frames:v",
            str(self.max_scene_frames),
            str(pattern),
        ]
        result = self._run_ffmpeg(command, timeout=600)
        files = sorted(target_dir.glob("scene-*.jpg"))
        if result.returncode != 0:
            # Scene detection is an enhancement. Dense base sampling still gives a
            # usable analysis path, so do not fail the whole stage here.
            return []
        timestamps = self._parse_pts_times(result.stderr)
        return [
            self._frame_record(
                target=file,
                timestamp=timestamps[index] if index < len(timestamps) else 0.0,
                kind="scene",
            )
            for index, file in enumerate(files)
        ]

    def _frame_record(self, *, target: Path, timestamp: float, kind: str) -> dict[str, Any]:
        relative = target.relative_to(self.output_dir).as_posix()
        return {
            "timestamp_seconds": round(float(timestamp), 3),
            "kind": kind,
            "url": f"/media/{relative}",
            "local_path": str(target),
            "size_bytes": target.stat().st_size,
        }

    @staticmethod
    def _merge_frames(
        base_frames: list[dict[str, Any]], scene_frames: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        ordered = sorted(
            [*base_frames, *scene_frames],
            key=lambda frame: (float(frame["timestamp_seconds"]), frame["kind"] != "scene"),
        )
        merged: list[dict[str, Any]] = []
        for frame in ordered:
            if merged:
                previous = merged[-1]
                delta = abs(
                    float(frame["timestamp_seconds"])
                    - float(previous["timestamp_seconds"])
                )
                # Only collapse practically identical timestamps of the same kind.
                # A scene-cut frame close to a 1-fps baseline frame is intentionally
                # kept because it may capture the reaction/new shot the baseline misses.
                if delta < 0.04 and frame["kind"] == previous["kind"]:
                    continue
            merged.append(frame)
        return merged

    def _select_model_frame_indexes(self, frames: list[dict[str, Any]]) -> set[int]:
        if not frames:
            return set()
        all_indexes = list(range(len(frames)))
        if self._fits_model_budget(frames, all_indexes):
            return set(all_indexes)

        scene_indexes = [
            index for index, frame in enumerate(frames) if frame.get("kind") == "scene"
        ]
        base_indexes = [
            index for index, frame in enumerate(frames) if frame.get("kind") != "scene"
        ]

        selected: list[int] = []
        for index in self._evenly_spaced(scene_indexes, min(len(scene_indexes), self.max_model_frames)):
            if self._can_add_frame(frames, selected, index):
                selected.append(index)

        remaining_slots = self.max_model_frames - len(selected)
        for index in self._evenly_spaced(base_indexes, remaining_slots):
            if self._can_add_frame(frames, selected, index):
                selected.append(index)

        # Preserve timeline edges when budget allows; these often contain setup/hook.
        for index in (0, len(frames) - 1):
            if index not in selected and self._can_add_frame(frames, selected, index):
                selected.append(index)

        return set(selected)

    def _fits_model_budget(self, frames: list[dict[str, Any]], indexes: list[int]) -> bool:
        return (
            len(indexes) <= self.max_model_frames
            and sum(self._frame_size(frames[index]) for index in indexes)
            <= self.max_model_image_bytes
        )

    def _can_add_frame(
        self, frames: list[dict[str, Any]], selected: list[int], index: int
    ) -> bool:
        if index in selected or len(selected) >= self.max_model_frames:
            return False
        current_bytes = sum(self._frame_size(frames[item]) for item in selected)
        return current_bytes + self._frame_size(frames[index]) <= self.max_model_image_bytes

    @staticmethod
    def _frame_size(frame: dict[str, Any]) -> int:
        try:
            return int(frame.get("size_bytes") or Path(str(frame["local_path"])).stat().st_size)
        except (KeyError, OSError, TypeError, ValueError):
            return 0

    @staticmethod
    def _evenly_spaced(indexes: list[int], count: int) -> list[int]:
        if count <= 0 or not indexes:
            return []
        if count >= len(indexes):
            return list(indexes)
        if count == 1:
            return [indexes[len(indexes) // 2]]
        positions = [round(i * (len(indexes) - 1) / (count - 1)) for i in range(count)]
        result: list[int] = []
        seen: set[int] = set()
        for position in positions:
            index = indexes[position]
            if index not in seen:
                result.append(index)
                seen.add(index)
        return result

    def build_analysis_segments(
        self,
        media: dict[str, Any],
        *,
        segment_seconds: float,
        max_frames_per_segment: int,
    ) -> list[dict[str, Any]]:
        """Split selected evidence frames into bounded time segments for LLM analysis."""
        # Segment mode deliberately starts from all locally extracted evidence.
        # The old selected_for_model flag was a global single-request budget and
        # would create blind spots in long videos after segmentation.
        frames = list(media.get("frames", []))
        if not frames:
            return []
        duration = float(media.get("duration_seconds") or 0.0)
        segment_seconds = max(5.0, float(segment_seconds))
        max_frames_per_segment = max(1, int(max_frames_per_segment))
        segment_count = max(1, int((max(duration, 0.001) + segment_seconds - 1e-9) // segment_seconds))
        segments: list[dict[str, Any]] = []
        for index in range(segment_count):
            start = round(index * segment_seconds, 3)
            end = round(min(duration, (index + 1) * segment_seconds), 3)
            bucket = [
                frame
                for frame in frames
                if (float(frame.get("timestamp_seconds") or 0.0) >= start)
                and (float(frame.get("timestamp_seconds") or 0.0) < end or (index == segment_count - 1 and float(frame.get("timestamp_seconds") or 0.0) <= end))
            ]
            if not bucket:
                continue
            chosen = self._select_frames_for_segment(bucket, max_frames_per_segment)
            segments.append(
                {
                    "segment_index": index,
                    "start_seconds": start,
                    "end_seconds": end,
                    "duration_seconds": round(max(0.0, end - start), 3),
                    "frame_count": len(chosen),
                    "frames": chosen,
                }
            )
        return segments

    @staticmethod
    def _select_frames_for_segment(
        frames: list[dict[str, Any]], max_frames_per_segment: int
    ) -> list[dict[str, Any]]:
        if len(frames) <= max_frames_per_segment:
            return list(frames)

        scene_indexes = [
            index for index, frame in enumerate(frames) if frame.get("kind") == "scene"
        ]
        base_indexes = [
            index for index, frame in enumerate(frames) if frame.get("kind") != "scene"
        ]

        selected: list[int] = []
        scene_budget = min(len(scene_indexes), max_frames_per_segment // 2)
        for index in SourceMediaProcessor._evenly_spaced(scene_indexes, scene_budget):
            if index not in selected:
                selected.append(index)

        remaining = max_frames_per_segment - len(selected)
        base_with_edges: list[int] = []
        for index in base_indexes:
            if index not in base_with_edges:
                base_with_edges.append(index)
        for edge in (0, len(frames) - 1):
            if edge not in base_with_edges:
                base_with_edges.append(edge)
        for index in SourceMediaProcessor._evenly_spaced(base_with_edges, remaining):
            if index not in selected:
                selected.append(index)
            if len(selected) >= max_frames_per_segment:
                break

        ordered = sorted(selected, key=lambda item: float(frames[item].get("timestamp_seconds") or 0.0))
        return [frames[index] for index in ordered]

    def _media_cache_identity(self, source: Path, source_sha256: str) -> dict[str, Any]:
        stat = source.stat()
        source_identity: dict[str, Any] = {
            "name": source.name,
            "size_bytes": stat.st_size,
        }
        if source_sha256:
            source_identity["sha256"] = source_sha256
        else:
            source_identity["mtime_ns"] = stat.st_mtime_ns
        return {
            "version": 2,
            "source": source_identity,
            "sampling": {
                "base_fps": round(self.base_fps, 8),
                "scene_threshold": round(self.scene_threshold, 8),
                "max_base_frames": self.max_base_frames,
                "max_scene_frames": self.max_scene_frames,
                "max_model_frames": self.max_model_frames,
                "max_model_image_bytes": self.max_model_image_bytes,
                "frame_width": self.frame_width,
            },
        }

    def _load_media_cache(
        self, target_dir: Path, identity: dict[str, Any]
    ) -> dict[str, Any] | None:
        manifest_path = target_dir / "media-manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if payload.get("identity") != identity:
            return None
        cached = payload.get("media")
        if not isinstance(cached, dict):
            return None

        frames: list[dict[str, Any]] = []
        for item in cached.get("frames", []):
            if not isinstance(item, dict) or not item.get("file_name"):
                return None
            frame_path = target_dir / str(item["file_name"])
            if not frame_path.is_file():
                return None
            record = {
                "timestamp_seconds": item.get("timestamp_seconds", 0.0),
                "kind": item.get("kind", "base"),
                "url": f"/media/{frame_path.relative_to(self.output_dir).as_posix()}",
                "local_path": str(frame_path),
                "size_bytes": frame_path.stat().st_size,
                "index": item.get("index"),
                "selected_for_model": bool(item.get("selected_for_model")),
            }
            frames.append(record)

        audio_preview = None
        audio_info = cached.get("audio_preview")
        if isinstance(audio_info, dict) and audio_info.get("file_name"):
            audio_path = target_dir / str(audio_info["file_name"])
            if not audio_path.is_file():
                return None
            audio_preview = {
                "url": f"/media/{audio_path.relative_to(self.output_dir).as_posix()}",
                "local_path": str(audio_path),
                "size_bytes": audio_path.stat().st_size,
            }

        return {
            "name": cached.get("name"),
            "path": str(Path(str(cached.get("source_path") or ""))) if cached.get("source_path") else "",
            "source_sha256": str(cached.get("source_sha256") or ""),
            "duration_seconds": cached.get("duration_seconds"),
            "size_bytes": cached.get("size_bytes"),
            "format": cached.get("format"),
            "video_stream": cached.get("video_stream"),
            "audio_stream": cached.get("audio_stream"),
            "audio_preview": audio_preview,
            "sampling": cached.get("sampling") or {},
            "frames": frames,
        }

    def _write_media_cache(
        self,
        target_dir: Path,
        identity: dict[str, Any],
        media: dict[str, Any],
    ) -> None:
        manifest_path = target_dir / "media-manifest.json"
        payload = {
            "identity": identity,
            "media": {
                "name": media.get("name"),
                "source_path": media.get("path"),
                "source_sha256": media.get("source_sha256"),
                "duration_seconds": media.get("duration_seconds"),
                "size_bytes": media.get("size_bytes"),
                "format": media.get("format"),
                "video_stream": media.get("video_stream"),
                "audio_stream": media.get("audio_stream"),
                "sampling": media.get("sampling"),
                "audio_preview": (
                    {"file_name": Path(str(media["audio_preview"]["local_path"])).name}
                    if isinstance(media.get("audio_preview"), dict)
                    and media["audio_preview"].get("local_path")
                    else None
                ),
                "frames": [
                    {
                        "file_name": Path(str(frame.get("local_path") or "")).name,
                        "timestamp_seconds": frame.get("timestamp_seconds"),
                        "kind": frame.get("kind"),
                        "index": frame.get("index"),
                        "selected_for_model": bool(frame.get("selected_for_model")),
                    }
                    for frame in media.get("frames", [])
                    if frame.get("local_path")
                ],
            },
        }
        self._atomic_write_json(manifest_path, payload)

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _extract_audio(self, source: Path, target_dir: Path) -> dict[str, Any]:
        target = target_dir / "source-audio.mp3"
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(target),
        ]
        result = self._run_ffmpeg(command, timeout=600)
        if result.returncode != 0 or not target.is_file():
            raise SourceMediaError(
                f"FFmpeg audio extraction failed for {source.name}: {(result.stderr or "")[-800:]}"
            )
        relative = target.relative_to(self.output_dir).as_posix()
        return {
            "url": f"/media/{relative}",
            "local_path": str(target),
            "size_bytes": target.stat().st_size,
        }

    def _probe(self, source: Path) -> dict[str, Any]:
        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(source),
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=120, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceMediaError(f"FFprobe could not run: {exc}") from exc
        if result.returncode != 0:
            raise SourceMediaError(
                f"FFprobe failed for {source.name}: {(result.stderr or "")[-800:]}"
            )
        try:
            import json

            return json.loads(result.stdout)
        except ValueError as exc:
            raise SourceMediaError(f"FFprobe returned invalid JSON for {source.name}") from exc

    def _run_ffmpeg(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceMediaError(f"FFmpeg could not run: {exc}") from exc

    @staticmethod
    def _clear_generated_previews(target_dir: Path) -> None:
        for pattern in ("frame-*.jpg", "base-*.jpg", "scene-*.jpg", "source-audio.mp3"):
            for path in target_dir.glob(pattern):
                try:
                    path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _duration(probe: dict[str, Any]) -> float:
        try:
            duration = float(probe.get("format", {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            raise SourceMediaError("Video duration is missing or invalid")
        return duration

    @staticmethod
    def _effective_fps(duration: float, requested_fps: float, max_frames: int) -> float:
        if duration <= 0 or requested_fps <= 0 or max_frames <= 0:
            return 0.0
        requested_count = duration * requested_fps
        if requested_count <= max_frames:
            return requested_fps
        return max(0.1, max_frames / duration)

    @classmethod
    def _parse_pts_times(cls, stderr: str | None) -> list[float]:
        text = stderr or ""
        return [float(match) for match in cls._PTS_TIME_RE.findall(text)]

    @staticmethod
    def _video_timeout(duration: float) -> int:
        # Dense sampling requires decoding the entire source. Allow enough room for
        # slower Windows laptops while still preventing a hung process forever.
        return max(300, min(1800, int(duration * 3 + 120)))
