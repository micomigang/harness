from __future__ import annotations

from app.config import settings

from .base import WorkflowProvider
from .hybrid import RoutedProvider
from .local_media import LocalMediaProvider
from .mock import MockProvider
from .openai_compat import OpenAICompatibleProvider
from .seedance import SeedanceProvider
from .seedream import SeedreamProvider
from .source_media import SourceMediaProcessor


class ProviderRegistry:
    def workflow(self) -> WorkflowProvider:
        if settings.provider == "mock":
            return MockProvider()
        if settings.provider in {"openai", "openai-compatible", "kimi"}:
            return OpenAICompatibleProvider(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                model=settings.openai_model,
                media_processor=SourceMediaProcessor(
                    ffmpeg_path=settings.ffmpeg_path,
                    output_dir=settings.data_dir / "outputs",
                    base_fps=settings.source_base_fps,
                    scene_threshold=settings.source_scene_threshold,
                    max_base_frames=settings.source_max_base_frames,
                    max_scene_frames=settings.source_max_scene_frames,
                    max_model_frames=settings.source_max_model_frames,
                    max_model_image_bytes=settings.source_max_model_image_bytes,
                    frame_width=settings.source_frame_width,
                    media_cache_enabled=settings.source_media_cache_enabled,
                ),
                segment_seconds=settings.source_segment_seconds,
                segment_max_frames=settings.source_segment_max_frames,
                segment_parallelism=settings.source_segment_parallelism,
                analysis_cache_enabled=settings.source_analysis_cache_enabled,
                connect_timeout_seconds=settings.openai_connect_timeout_seconds,
                read_timeout_seconds=settings.openai_read_timeout_seconds,
            )
        if settings.provider in {"kimi-seedance", "hybrid"}:
            return RoutedProvider(
                llm=OpenAICompatibleProvider(
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url,
                    model=settings.openai_model,
                    media_processor=SourceMediaProcessor(
                        ffmpeg_path=settings.ffmpeg_path,
                        output_dir=settings.data_dir / "outputs",
                        base_fps=settings.source_base_fps,
                        scene_threshold=settings.source_scene_threshold,
                        max_base_frames=settings.source_max_base_frames,
                        max_scene_frames=settings.source_max_scene_frames,
                        max_model_frames=settings.source_max_model_frames,
                        max_model_image_bytes=settings.source_max_model_image_bytes,
                        frame_width=settings.source_frame_width,
                        media_cache_enabled=settings.source_media_cache_enabled,
                    ),
                    segment_seconds=settings.source_segment_seconds,
                    segment_max_frames=settings.source_segment_max_frames,
                    segment_parallelism=settings.source_segment_parallelism,
                    analysis_cache_enabled=settings.source_analysis_cache_enabled,
                    connect_timeout_seconds=settings.openai_connect_timeout_seconds,
                    read_timeout_seconds=settings.openai_read_timeout_seconds,
                ),
                image=SeedreamProvider(
                    api_key=settings.image_api_key,
                    base_url=settings.image_base_url,
                    model=settings.image_model,
                    size=settings.image_size,
                    max_assets=settings.image_max_assets,
                    output_dir=settings.data_dir / "outputs",
                ),
                video=SeedanceProvider(
                    api_key=settings.video_api_key,
                    base_url=settings.video_base_url,
                    model=settings.video_model,
                    fallback_model=settings.video_fallback_model,
                    resolution=settings.video_resolution,
                    ratio=settings.video_ratio,
                    generate_audio=settings.video_generate_audio,
                    poll_interval_seconds=settings.video_poll_interval_seconds,
                    poll_timeout_seconds=settings.video_poll_timeout_seconds,
                    batch_max_shots=settings.video_batch_max_shots,
                    output_dir=settings.data_dir / "outputs",
                ),
                media=LocalMediaProvider(
                    ffmpeg_path=settings.ffmpeg_path,
                    output_dir=settings.data_dir / "outputs",
                ),
            )
        raise ValueError(f"Unsupported HARNESS_PROVIDER: {settings.provider}")
