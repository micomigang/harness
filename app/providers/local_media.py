from __future__ import annotations

import json
import subprocess
from collections import Counter
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
                "note": "当前沿用 Seedance 镜头原生对白与环境音；已生成全片 BGM 方案，但未配置独立音乐生成 API，因此不会声称已渲染独立 BGM。",
            }
        if stage == "compose":
            return self._compose(context)
        if stage == "delivery_qa":
            return self._delivery_qa(context)
        raise NotImplementedError(f"Local media does not handle stage: {stage}")

    def _probe_json(self, path: Path) -> dict[str, Any]:
        ffprobe = Path(self.ffmpeg_path).with_name("ffprobe.exe")
        if not ffprobe.is_file():
            ffprobe = Path(self.ffmpeg_path).with_name("ffprobe")
        command = [
            str(ffprobe),
            "-v", "error",
            "-show_entries",
            "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,sample_rate,channels",
            "-of", "json",
            str(path),
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
            return {"status": "fail", "returncode": None, "error": str(exc), "format": {}, "streams": []}
        if result.returncode != 0:
            return {
                "status": "fail",
                "returncode": int(result.returncode),
                "error": (result.stderr or result.stdout)[-1200:],
                "format": {},
                "streams": [],
            }
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return {"status": "fail", "returncode": 0, "error": f"invalid_json: {exc}", "format": {}, "streams": []}
        return {
            "status": "pass",
            "returncode": 0,
            "format": payload.get("format") if isinstance(payload.get("format"), dict) else {},
            "streams": payload.get("streams") if isinstance(payload.get("streams"), list) else [],
        }

    @staticmethod
    def _stream_summary(probe: dict[str, Any], codec_type: str) -> dict[str, Any] | None:
        for stream in probe.get("streams", []) if isinstance(probe.get("streams"), list) else []:
            if not isinstance(stream, dict) or stream.get("codec_type") != codec_type:
                continue
            summary = {
                key: stream.get(key)
                for key in ("index", "codec_type", "codec_name", "width", "height", "sample_rate", "channels")
                if stream.get(key) is not None
            }
            if "sample_rate" in summary:
                try:
                    summary["sample_rate"] = int(summary["sample_rate"])
                except (TypeError, ValueError):
                    pass
            if "channels" in summary:
                try:
                    summary["channels"] = int(summary["channels"])
                except (TypeError, ValueError):
                    pass
            return summary
        return None

    @staticmethod
    def _resolve_audio_profile(
        input_audio_probe: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Derive and lock the compose audio profile from the current batch.

        Explicit compose overrides win. Otherwise the profile is selected from the
        unique majority of successfully probed input audio streams. A tie is not
        guessed: the compose stage fails before FFmpeg so the user/director can
        choose an explicit profile. This keeps the lock project/revision-specific
        instead of hard-coding a sample rate into Harness.
        """
        observed: list[tuple[int, int]] = []
        for row in input_audio_probe:
            try:
                sample_rate = int(row.get("sample_rate_hz") or 0)
                channels = int(row.get("channels") or 0)
            except (TypeError, ValueError):
                continue
            if sample_rate > 0 and channels in {1, 2}:
                observed.append((sample_rate, channels))

        explicit_rate = int(params.get("audio_sample_rate_hz") or 0)
        explicit_channels = int(params.get("audio_channels") or 0)
        counts = Counter(observed)
        distribution = [
            {"sample_rate_hz": rate, "channels": channels, "count": count}
            for (rate, channels), count in sorted(counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
        ]

        if explicit_rate or explicit_channels:
            if explicit_rate <= 0:
                if not counts:
                    raise LocalMediaError(
                        "Compose audio profile cannot infer sample rate: no input audio stream is probeable; "
                        "set audio_sample_rate_hz explicitly."
                    )
                explicit_rate = counts.most_common(1)[0][0][0]
            if explicit_channels not in {1, 2}:
                if not counts:
                    raise LocalMediaError(
                        "Compose audio profile cannot infer channel count: no input audio stream is probeable; "
                        "set audio_channels explicitly to 1 or 2."
                    )
                explicit_channels = counts.most_common(1)[0][0][1]
            return {
                "sample_rate_hz": explicit_rate,
                "channels": explicit_channels,
                "channel_layout": "mono" if explicit_channels == 1 else "stereo",
                "source": "explicit_compose_override",
                "observed_audio_streams": len(observed),
                "winning_count": counts.get((explicit_rate, explicit_channels), 0),
                "distribution": distribution,
                "locked": True,
            }

        if not counts:
            raise LocalMediaError(
                "Compose cannot dynamically lock an audio profile because no input shot exposes a probeable audio stream. "
                "Provide an explicit compose audio_sample_rate_hz/audio_channels override or restore source audio."
            )

        ranked = counts.most_common()
        winning_count = ranked[0][1]
        winners = [profile for profile, count in ranked if count == winning_count]
        if len(winners) != 1:
            choices = ", ".join(f"{rate} Hz/{channels} ch" for rate, channels in winners)
            raise LocalMediaError(
                "Compose found no unique majority audio profile (tie: " + choices + "). "
                "Fail-stop instead of guessing; set an explicit compose audio_sample_rate_hz/audio_channels override."
            )
        sample_rate, channels = winners[0]
        return {
            "sample_rate_hz": sample_rate,
            "channels": channels,
            "channel_layout": "mono" if channels == 1 else "stereo",
            "source": "batch_majority",
            "observed_audio_streams": len(observed),
            "winning_count": winning_count,
            "distribution": distribution,
            "locked": True,
        }

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
        raw_items = batch.get("items", []) if isinstance(batch.get("items"), list) else []
        successful_items = [
            item for item in raw_items
            if isinstance(item, dict) and item.get("status") == "succeeded" and item.get("local_path")
        ]
        successful_items.sort(key=lambda item: int(item.get("shot_index") or 0))

        storyboard = next(
            (
                artifact.get("content", {})
                for artifact in context.get("artifacts", [])
                if artifact.get("kind") == "storyboard"
            ),
            {},
        )
        storyboard_shots = storyboard.get("shots", []) if isinstance(storyboard.get("shots"), list) else []
        expected_indices = [
            int(shot.get("index") or position)
            for position, shot in enumerate(storyboard_shots, start=1)
            if isinstance(shot, dict)
        ]
        expected_duration_seconds = round(sum(
            float(shot.get("duration_seconds") or 0.0)
            for shot in storyboard_shots
            if isinstance(shot, dict)
        ), 3)
        actual_indices = [int(item.get("shot_index") or 0) for item in successful_items]
        if expected_indices and actual_indices != expected_indices:
            missing = [index for index in expected_indices if index not in set(actual_indices)]
            raise LocalMediaError(
                "Cannot compose an incomplete batch; missing or out-of-order shots: "
                + ", ".join(map(str, missing or actual_indices))
            )

        paths = [Path(str(item["local_path"])) for item in successful_items]
        source_files_present = bool(paths) and all(path.is_file() and path.stat().st_size > 0 for path in paths)
        if not source_files_present:
            raise LocalMediaError("No complete local Seedance shot files available for compose")

        input_audio_probe: list[dict[str, Any]] = []
        input_shot_rows: list[dict[str, Any]] = []
        expected_duration_by_index = {
            int(shot.get("index") or position): round(float(shot.get("duration_seconds") or 0.0), 3)
            for position, shot in enumerate(storyboard_shots, start=1)
            if isinstance(shot, dict)
        }
        for item, path in zip(successful_items, paths):
            probe = self._probe_json(path)
            audio = self._stream_summary(probe, "audio")
            shot_index = int(item.get("shot_index") or 0)
            try:
                actual_input_duration = round(float((probe.get("format") or {}).get("duration") or 0.0), 3)
            except (TypeError, ValueError):
                actual_input_duration = 0.0
            expected_input_duration = expected_duration_by_index.get(shot_index, 0.0)
            duration_delta = round(actual_input_duration - expected_input_duration, 3) if expected_input_duration else None
            input_audio_probe.append({
                "shot_index": shot_index,
                "status": probe.get("status"),
                "codec": (audio or {}).get("codec_name"),
                "sample_rate_hz": (audio or {}).get("sample_rate"),
                "channels": (audio or {}).get("channels"),
                "actual_duration_seconds": actual_input_duration,
            })
            input_shot_rows.append({
                "shot_index": shot_index,
                "source_url": str(item.get("url") or ""),
                "source_path": str(path),
                "source_path_present": path.is_file(),
                "file_name": path.name,
                "file_size_bytes": path.stat().st_size if path.is_file() else 0,
                "actual_duration_seconds": actual_input_duration,
                "expected_duration_seconds": expected_input_duration,
                "duration_delta_seconds": duration_delta,
                "reused_from": str(item.get("reused_from") or ""),
            })
        audio_profile_lock = self._resolve_audio_profile(input_audio_probe, params)
        target_sample_rate_hz = int(audio_profile_lock["sample_rate_hz"])
        target_channels = int(audio_profile_lock["channels"])
        target_channel_layout = str(audio_profile_lock["channel_layout"])
        resampled_shots = [
            row["shot_index"] for row in input_audio_probe
            if row.get("sample_rate_hz") not in (None, target_sample_rate_hz)
            or row.get("channels") not in (None, target_channels)
        ]

        target_dir = self.output_dir / workspace_id / "compose"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "final.mp4"

        # Do not feed mixed-rate MP4s directly to the concat demuxer. The concat
        # demuxer assumes matching stream parameters/time bases; changing only
        # the *output* sample rate happens too late and can preserve a bad audio
        # timestamp boundary (for example a 44.1 kHz shot between 32 kHz shots),
        # which presents as stretched audio and shifts every later segment.
        #
        # Instead, decode every shot independently, reset video PTS, resample
        # every audio stream *before* concatenation, rebuild audio PTS from the
        # resampled sample count, and then concatenate the normalized streams.
        # The concat filter pads a shorter audio leg with silence to the matching
        # video segment boundary, so one odd source cannot move later dialogue.
        command = [self.ffmpeg_path, "-y"]
        for path in paths:
            command.extend(["-i", str(path)])

        filter_parts: list[str] = []
        concat_inputs: list[str] = []
        for input_index, (row, path) in enumerate(zip(input_audio_probe, paths)):
            filter_parts.append(
                f"[{input_index}:v:0]settb=AVTB,setpts=PTS-STARTPTS[v{input_index}]"
            )
            if row.get("codec"):
                filter_parts.append(
                    f"[{input_index}:a:0]aresample={target_sample_rate_hz}:first_pts=0,"
                    f"aformat=sample_rates={target_sample_rate_hz}:channel_layouts={target_channel_layout},"
                    f"asetpts=N/SR/TB[a{input_index}]"
                )
            else:
                # A silent/missing-audio source must not collapse the timeline.
                # Generate silence for exactly that shot's probed media duration.
                duration = next(
                    (float(item.get("actual_duration_seconds") or 0.0) for item in input_shot_rows
                     if int(item.get("shot_index") or 0) == int(row.get("shot_index") or 0)),
                    0.0,
                )
                filter_parts.append(
                    f"anullsrc=r={target_sample_rate_hz}:cl={target_channel_layout},"
                    f"atrim=duration={max(duration, 0.001):.3f},asetpts=N/SR/TB[a{input_index}]"
                )
            concat_inputs.extend([f"[v{input_index}]", f"[a{input_index}]"])

        filter_parts.append(
            "".join(concat_inputs)
            + f"concat=n={len(paths)}:v=1:a=1[vout][aout]"
        )
        filter_complex = ";".join(filter_parts)
        command.extend([
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", str(video_crf),
            "-c:a", "aac",
            "-b:a", f"{audio_bitrate_kbps}k",
            "-ar", str(target_sample_rate_hz),
            "-ac", str(target_channels),
            "-movflags", "+faststart",
            str(target),
        ])
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

        output_probe = self._probe_json(target)
        video_stream = self._stream_summary(output_probe, "video")
        audio_stream = self._stream_summary(output_probe, "audio")
        try:
            duration_seconds = float((output_probe.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            duration_seconds = 0.0
        duration_seconds = round(duration_seconds, 3)
        duration_delta_seconds = round(duration_seconds - expected_duration_seconds, 3) if expected_duration_seconds else None
        # Concat/re-encode duration can drift slightly at every file boundary because
        # video frames and AAC packets do not land on arbitrary millisecond edges.
        # Use a dynamic tolerance tied to the current storyboard size rather than a
        # hard-coded episode duration. 100 ms per input shot (minimum 1 s) is strict
        # enough to catch real timeline loss while tolerating normal packet-boundary
        # accumulation in multi-shot masters.
        duration_tolerance_seconds = round(max(1.0, 0.1 * len(paths)), 3)

        width = int((video_stream or {}).get("width") or 0)
        height = int((video_stream or {}).get("height") or 0)
        target_ratio = str(context.get("workspace", {}).get("settings", {}).get("aspect_ratio", "") or "")
        aspect_ratio_ok = True
        if target_ratio == "9:16":
            aspect_ratio_ok = width > 0 and height > 0 and abs((width / height) - (9 / 16)) < 0.035
        elif target_ratio == "16:9":
            aspect_ratio_ok = width > 0 and height > 0 and abs((width / height) - (16 / 9)) < 0.035

        requested_resolution = next(
            (str(item.get("resolution") or "").strip() for item in successful_items if str(item.get("resolution") or "").strip()),
            "",
        )
        requested_ratio = next(
            (str(item.get("ratio") or "").strip() for item in successful_items if str(item.get("ratio") or "").strip()),
            target_ratio,
        )

        input_probe_ok = all(row.get("status") == "pass" for row in input_audio_probe)
        duration_ok = duration_seconds > 0 and (
            not expected_duration_seconds or abs(duration_seconds - expected_duration_seconds) <= duration_tolerance_seconds
        )
        checks = {
            "source_files_present": source_files_present,
            "shot_order_matches_storyboard": (not expected_indices) or actual_indices == expected_indices,
            "input_audio_probe": input_probe_ok,
            "ffmpeg_returncode_zero": result.returncode == 0,
            "output_file_present": target.is_file() and target.stat().st_size > 0,
            "ffprobe_returncode_zero": output_probe.get("status") == "pass" and output_probe.get("returncode") == 0,
            "video_stream_h264": bool(video_stream) and str(video_stream.get("codec_name") or "").casefold() == "h264",
            "aspect_ratio": aspect_ratio_ok,
            "audio_stream_aac": bool(audio_stream) and str(audio_stream.get("codec_name") or "").casefold() == "aac",
            "audio_sample_rate_matches_lock": bool(audio_stream) and int(audio_stream.get("sample_rate") or 0) == target_sample_rate_hz,
            "audio_channels_match_lock": bool(audio_stream) and int(audio_stream.get("channels") or 0) == target_channels,
            "duration_within_tolerance": duration_ok,
        }
        checks["input_shots_schema"] = (
            len(input_shot_rows) == len(paths)
            and all(
                int(row.get("shot_index") or 0) > 0
                and bool(row.get("source_url") or row.get("source_path"))
                and float(row.get("actual_duration_seconds") or 0.0) > 0
                for row in input_shot_rows
            )
        )
        compose_validation = {
            "status": "pass" if all(checks.values()) else "fail",
            "checks": checks,
            "expected_shot_indices": expected_indices or actual_indices,
            "actual_shot_indices": actual_indices,
            "expected_shot_count": len(expected_indices or actual_indices),
            "actual_input_shots": len(paths),
            "expected_duration_seconds": expected_duration_seconds,
            "actual_duration_seconds": duration_seconds,
            "duration_delta_seconds": duration_delta_seconds,
            "duration_tolerance_seconds": duration_tolerance_seconds,
            "shot_duration_comparison": [
                {
                    "shot_index": row["shot_index"],
                    "expected_duration_seconds": row["expected_duration_seconds"],
                    "actual_duration_seconds": row["actual_duration_seconds"],
                    "duration_delta_seconds": row["duration_delta_seconds"],
                }
                for row in input_shot_rows
            ],
            "requested_resolution": requested_resolution,
            "requested_ratio": requested_ratio,
            "audio_profile_lock": audio_profile_lock,
            "video_stream": video_stream,
            "audio_stream": audio_stream,
            "structural_authority": "harness+ffmpeg+ffprobe",
        }

        relative = target.relative_to(self.output_dir).as_posix()
        # Backward-compatible alias for earlier UI/audit consumers. `input_shots` is
        # the canonical per-shot provenance contract; `input_sources` mirrors the
        # same rows and must never be interpreted as a count.
        input_sources = [dict(row) for row in input_shot_rows]
        music_artifact = next(
            (artifact for artifact in context.get("artifacts", []) if artifact.get("kind") == "music"),
            {},
        )
        music_content = music_artifact.get("content", {}) if isinstance(music_artifact, dict) else {}
        sample_rate_khz = target_sample_rate_hz / 1000.0
        sample_rate_label = f"{sample_rate_khz:g} kHz"
        channel_label = "mono" if target_channels == 1 else "stereo"
        return {
            "url": f"/media/{relative}",
            "local_path": str(target),
            "local_path_present": True,
            "output_file_size_bytes": target.stat().st_size,
            "input_shots": input_shot_rows,
            "input_shot_count": len(input_shot_rows),
            "input_shot_indices": actual_indices,
            "expected_shot_indices": expected_indices or actual_indices,
            "input_sources": input_sources,
            "requested_resolution": requested_resolution,
            "requested_ratio": requested_ratio,
            "audio_profile_lock": audio_profile_lock,
            "music_mode": music_content.get("mode") or "unknown",
            "status": "succeeded",
            "duration_seconds": duration_seconds,
            "expected_duration_seconds": expected_duration_seconds,
            "duration_delta_seconds": duration_delta_seconds,
            "encoding": {
                "video": f"H.264 / CRF {video_crf}",
                "audio": f"AAC / {audio_bitrate_kbps}k / {sample_rate_label} / {channel_label}",
                "video_codec": "h264",
                "video_crf": video_crf,
                "audio_codec": "aac",
                "audio_bitrate_kbps": audio_bitrate_kbps,
                "audio_sample_rate_hz": target_sample_rate_hz,
                "audio_channels": target_channels,
                "faststart": True,
            },
            "ffmpeg": {
                "status": "pass",
                "returncode": int(result.returncode),
                "audio_resample_enforced": True,
                "audio_resample_before_concat": True,
                "concat_strategy": "filter_complex_per_input_pts_normalized",
                "audio_pts_strategy": "resample_then_asetpts_from_sample_count",
                "target_sample_rate_hz": target_sample_rate_hz,
                "target_channels": target_channels,
            },
            "probe": {
                "status": output_probe.get("status"),
                "returncode": output_probe.get("returncode"),
                "format": output_probe.get("format"),
                "video_stream": video_stream,
                "audio_stream": audio_stream,
            },
            "audio_normalization": {
                "target_sample_rate_hz": target_sample_rate_hz,
                "target_channels": target_channels,
                "target_channel_layout": target_channel_layout,
                "profile_lock": audio_profile_lock,
                "input_audio": input_audio_probe,
                "resampled_shots": resampled_shots,
                "enforced_by_ffmpeg": True,
                "before_concat": True,
                "audio_pts_reset_per_shot": True,
                "concat_strategy": "filter_complex_per_input_pts_normalized",
            },
            "compose_validation": compose_validation,
            "note": f"本地 FFmpeg 已按 storyboard 顺序拼接全部成功镜头；当前批次动态锁定音频 profile 为 {target_sample_rate_hz} Hz / {channel_label}（{audio_profile_lock['source']}），每镜音频先归一化到该 profile 并重建 PTS，再通过 concat filter 合并，避免混合采样率边界造成音频拉伸/后续错位；最终母版由 ffprobe 验证。",
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
        compose_input_shots = compose.get("input_shots") if isinstance(compose.get("input_shots"), list) else []
        compose_input_count = int(compose.get("input_shot_count") or len(compose_input_shots) or 0)
        add(
            "shot_count",
            bool(successful) and compose_input_count == len(successful),
            f"compose={compose_input_count}, succeeded={len(successful)}",
        )
        add(
            "input_shots_schema",
            bool(compose_input_shots)
            and all(
                isinstance(row, dict)
                and int(row.get("shot_index") or 0) > 0
                and bool(row.get("source_url") or row.get("source_path"))
                and float(row.get("actual_duration_seconds") or 0.0) > 0
                for row in compose_input_shots
            ),
            f"rows={len(compose_input_shots)}",
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
