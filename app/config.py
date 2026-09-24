from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    data_dir: Path
    provider: str
    host: str
    port: int
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    openai_connect_timeout_seconds: float
    openai_read_timeout_seconds: float
    source_base_fps: float
    source_scene_threshold: float
    source_max_base_frames: int
    source_max_scene_frames: int
    source_max_model_frames: int
    source_max_model_image_bytes: int
    source_frame_width: int
    source_segment_seconds: float
    source_segment_max_frames: int
    source_segment_parallelism: int
    source_media_cache_enabled: bool
    source_analysis_cache_enabled: bool
    image_api_key: str
    image_base_url: str
    image_model: str
    image_size: str
    image_max_assets: int
    video_api_key: str
    video_base_url: str
    video_model: str
    video_fallback_model: str
    video_resolution: str
    video_ratio: str
    video_generate_audio: bool
    video_poll_interval_seconds: float
    video_poll_timeout_seconds: int
    video_batch_max_shots: int
    ffmpeg_path: str
    tts_app_id: str
    tts_access_token: str
    tts_voice_type: str
    asr_app_id: str
    asr_access_token: str
    asr_api_key: str

    @classmethod
    def load(cls) -> "Settings":
        root = Path(__file__).resolve().parents[1]
        raw_data = os.getenv("HARNESS_DATA_DIR", "./data")
        data = Path(raw_data)
        if not data.is_absolute():
            data = root / data
        data.mkdir(parents=True, exist_ok=True)
        (data / "outputs").mkdir(parents=True, exist_ok=True)
        return cls(
            root_dir=root,
            data_dir=data,
            provider=os.getenv("HARNESS_PROVIDER", "mock").strip().lower(),
            host=os.getenv("HARNESS_HOST", "127.0.0.1"),
            port=int(os.getenv("HARNESS_PORT", "8788")),
            openai_api_key=os.getenv("OPENAI_COMPAT_API_KEY", ""),
            openai_base_url=os.getenv(
                "OPENAI_COMPAT_BASE_URL", "https://api.moonshot.cn/v1"
            ).rstrip("/"),
            openai_model=os.getenv("OPENAI_COMPAT_MODEL", ""),
            openai_connect_timeout_seconds=float(os.getenv("OPENAI_COMPAT_CONNECT_TIMEOUT_SECONDS", "30")),
            openai_read_timeout_seconds=float(os.getenv("OPENAI_COMPAT_READ_TIMEOUT_SECONDS", "900")),
            source_base_fps=float(os.getenv("SOURCE_BASE_FPS", "1.0")),
            source_scene_threshold=float(os.getenv("SOURCE_SCENE_THRESHOLD", "0.12")),
            source_max_base_frames=int(os.getenv("SOURCE_MAX_BASE_FRAMES", "1200")),
            source_max_scene_frames=int(os.getenv("SOURCE_MAX_SCENE_FRAMES", "300")),
            source_max_model_frames=int(os.getenv("SOURCE_MAX_MODEL_FRAMES", "480")),
            source_max_model_image_bytes=int(
                os.getenv("SOURCE_MAX_MODEL_IMAGE_BYTES", "48000000")
            ),
            source_frame_width=int(os.getenv("SOURCE_FRAME_WIDTH", "768")),
            source_segment_seconds=float(os.getenv("SOURCE_SEGMENT_SECONDS", "30")),
            source_segment_max_frames=int(os.getenv("SOURCE_SEGMENT_MAX_FRAMES", "12")),
            source_segment_parallelism=int(os.getenv("SOURCE_SEGMENT_PARALLELISM", "4")),
            source_media_cache_enabled=os.getenv("SOURCE_MEDIA_CACHE_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
            source_analysis_cache_enabled=os.getenv("SOURCE_ANALYSIS_CACHE_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
            image_api_key=os.getenv("IMAGE_API_KEY", ""),
            image_base_url=os.getenv(
                "IMAGE_API_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
            ).rstrip("/"),
            image_model=os.getenv(
                "IMAGE_MODEL", "doubao-seedream-5-0-pro-260628"
            ),
            image_size=os.getenv("IMAGE_SIZE", "2K"),
            image_max_assets=int(os.getenv("IMAGE_MAX_ASSETS", "6")),
            video_api_key=os.getenv("VIDEO_API_KEY", ""),
            video_base_url=os.getenv(
                "VIDEO_API_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
            ).rstrip("/"),
            video_model=os.getenv("VIDEO_MODEL", "doubao-seedance-2-5-260628"),
            video_fallback_model=os.getenv(
                "VIDEO_FALLBACK_MODEL", "doubao-seedance-2-0-260128"
            ),
            video_resolution=os.getenv("VIDEO_RESOLUTION", "720p"),
            video_ratio=os.getenv("VIDEO_RATIO", "9:16"),
            video_generate_audio=os.getenv("VIDEO_GENERATE_AUDIO", "true").lower()
            in {"1", "true", "yes", "on"},
            video_poll_interval_seconds=float(
                os.getenv("VIDEO_POLL_INTERVAL_SECONDS", "10")
            ),
            video_poll_timeout_seconds=int(
                os.getenv("VIDEO_POLL_TIMEOUT_SECONDS", "900")
            ),
            video_batch_max_shots=int(os.getenv("VIDEO_BATCH_MAX_SHOTS", "8")),
            ffmpeg_path=os.getenv("FFMPEG_PATH", "ffmpeg"),
            tts_app_id=os.getenv("TTS_APP_ID", ""),
            tts_access_token=os.getenv("TTS_ACCESS_TOKEN", ""),
            tts_voice_type=os.getenv("TTS_VOICE_TYPE", ""),
            asr_app_id=os.getenv("ASR_APP_ID", ""),
            asr_access_token=os.getenv("ASR_ACCESS_TOKEN", ""),
            asr_api_key=os.getenv("ASR_API_KEY", ""),
        )


settings = Settings.load()
