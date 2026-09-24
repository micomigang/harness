from __future__ import annotations

import re
from typing import Any


DIRECTOR_PARAMETER_RULES: dict[str, dict[str, str]] = {
    "analysis": {
        "segment_seconds": "integer 10-60; analysis segment duration in seconds",
        "segment_max_frames": "integer 4-16; maximum visual frames sent per segment",
        "segment_parallelism": "integer 1-6; parallel Kimi segment requests",
    },
    "storyboard": {
        "storyboard_count": "integer 1-100; desired shot count; downstream prompt override only",
    },
    "reference_images": {
        "max_assets": "integer 1-12; maximum Seedream reference images to generate",
    },
    "preview": {
        "resolution": "one of 480p, 720p, 1080p",
        "ratio": "one of 9:16, 16:9, 1:1",
        "generate_audio": "boolean",
    },
    "batch_video": {
        "resolution": "one of 480p, 720p, 1080p",
        "ratio": "one of 9:16, 16:9, 1:1",
        "generate_audio": "boolean",
        "batch_max_shots": "integer 1-20",
    },
    "compose": {
        "video_crf": "integer 16-28; lower means higher H.264 quality",
        "audio_bitrate_kbps": "integer 96-320",
    },
}


_PARAMETER_CUES: dict[str, dict[str, tuple[str, ...]]] = {
    "analysis": {
        "segment_seconds": ("分段", "一段", "每段", "segment", "秒"),
        "segment_max_frames": ("帧", "frame", "图片", "每段最多"),
        "segment_parallelism": ("并行", "parallel", "并发"),
    },
    "storyboard": {
        "storyboard_count": ("分镜", "镜头", "shot", "storyboard"),
    },
    "reference_images": {
        "max_assets": ("参考图", "资产图", "张", "image", "asset"),
    },
    "preview": {
        "resolution": ("480p", "720p", "1080p", "分辨率", "resolution"),
        "ratio": ("9:16", "16:9", "1:1", "画幅", "比例", "ratio"),
        "generate_audio": ("声音", "音频", "audio", "静音"),
    },
    "batch_video": {
        "resolution": ("480p", "720p", "1080p", "分辨率", "resolution"),
        "ratio": ("9:16", "16:9", "1:1", "画幅", "比例", "ratio"),
        "generate_audio": ("声音", "音频", "audio", "静音"),
        "batch_max_shots": ("前几镜", "几镜", "批量", "batch", "镜头"),
    },
    "compose": {
        "video_crf": ("crf", "压缩", "画质", "码率"),
        "audio_bitrate_kbps": ("音频码率", "kbps", "bitrate"),
    },
}


def allowed_parameter_description(stage: str) -> dict[str, str]:
    return dict(DIRECTOR_PARAMETER_RULES.get(stage, {}))


def _explicit_parameter_authorization(stage: str, key: str, user_instruction: str) -> bool:
    """Only allow technical overrides when the user explicitly asked for them.

    The director may *suggest* parameter changes conversationally, but it cannot
    silently turn a creative request such as "analyze carefully" into a more
    expensive technical configuration. This keeps harness defaults stable and
    makes cost/latency-affecting changes user-controlled.
    """
    text = str(user_instruction or "").strip().lower()
    if not text:
        return False
    cues = _PARAMETER_CUES.get(stage, {}).get(key, ())
    if not any(cue.lower() in text for cue in cues):
        return False

    # Numeric settings require the user to have supplied a number nearby/somewhere
    # in the turn. Boolean/enum settings can be explicit without a number.
    if key in {
        "segment_seconds",
        "segment_max_frames",
        "segment_parallelism",
        "storyboard_count",
        "max_assets",
        "batch_max_shots",
        "video_crf",
        "audio_bitrate_kbps",
    }:
        return bool(re.search(r"\d", text))
    return True


def sanitize_parameter_overrides(
    stage: str,
    value: Any,
    *,
    user_instruction: str | None = None,
    require_explicit_user_request: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = DIRECTOR_PARAMETER_RULES.get(stage, {})
    if not allowed:
        return {}
    result: dict[str, Any] = {}
    for key, raw in value.items():
        if key not in allowed:
            continue
        if require_explicit_user_request and not _explicit_parameter_authorization(
            stage, key, user_instruction or ""
        ):
            continue
        try:
            if stage == "analysis" and key == "segment_seconds":
                result[key] = max(10, min(60, int(raw)))
            elif stage == "analysis" and key == "segment_max_frames":
                result[key] = max(4, min(16, int(raw)))
            elif stage == "analysis" and key == "segment_parallelism":
                result[key] = max(1, min(6, int(raw)))
            elif stage == "storyboard" and key == "storyboard_count":
                result[key] = max(1, min(100, int(raw)))
            elif stage == "reference_images" and key == "max_assets":
                result[key] = max(1, min(12, int(raw)))
            elif stage in {"preview", "batch_video"} and key == "resolution":
                text = str(raw).lower()
                if text in {"480p", "720p", "1080p"}:
                    result[key] = text
            elif stage in {"preview", "batch_video"} and key == "ratio":
                text = str(raw)
                if text in {"9:16", "16:9", "1:1"}:
                    result[key] = text
            elif stage in {"preview", "batch_video"} and key == "generate_audio":
                if isinstance(raw, bool):
                    result[key] = raw
                elif str(raw).strip().lower() in {"1", "true", "yes", "on"}:
                    result[key] = True
                elif str(raw).strip().lower() in {"0", "false", "no", "off"}:
                    result[key] = False
            elif stage == "batch_video" and key == "batch_max_shots":
                result[key] = max(1, min(20, int(raw)))
            elif stage == "compose" and key == "video_crf":
                result[key] = max(16, min(28, int(raw)))
            elif stage == "compose" and key == "audio_bitrate_kbps":
                result[key] = max(96, min(320, int(raw)))
        except (TypeError, ValueError):
            continue
    return result
