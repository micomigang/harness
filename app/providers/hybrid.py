from __future__ import annotations

from typing import Any

from .base import WorkflowProvider


class RoutedProvider(WorkflowProvider):
    name = "kimi-seedance"

    def __init__(
        self,
        *,
        llm: WorkflowProvider,
        image: WorkflowProvider,
        video: WorkflowProvider,
        media: WorkflowProvider,
    ):
        self.llm = llm
        self.image = image
        self.video = video
        self.media = media

    def generate(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        if stage in {
            "analysis",
            "script",
            "asset_manifest",
            "characters",
            "scenes",
            "props",
            "storyboard",
            "dialogue_plan",
            "sound_plan",
            "review",
            "music_plan",
        }:
            return self.llm.generate(stage, context)
        if stage == "reference_images":
            return self.image.generate(stage, context)
        if stage in {"preview", "batch_video"}:
            return self.video.generate(stage, context)
        if stage in {"music", "compose", "delivery_qa"}:
            return self.media.generate(stage, context)
        raise NotImplementedError(f"No provider route for stage: {stage}")
