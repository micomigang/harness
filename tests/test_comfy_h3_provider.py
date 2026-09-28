import json
from pathlib import Path

import httpx
import pytest

from app.providers.comfy_h3 import ComfyH3Provider
from app.providers.seedance import SeedanceError


def _models(root: Path):
    required = {
        "diffusion_models": ComfyH3Provider.MODEL_FILE,
        "text_encoders": ComfyH3Provider.TEXT_ENCODER,
        "vae": ComfyH3Provider.VIDEO_VAE,
        "vae_audio": ComfyH3Provider.AUDIO_VAE,
        "loras": ComfyH3Provider.TURBO_LORA,
    }
    for folder, filename in required.items():
        target = root / ("vae" if folder == "vae_audio" else folder) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test model marker")


def test_h3_preview_uploads_bound_local_refs_and_saves_contract(tmp_path: Path, monkeypatch):
    models = tmp_path / "models"
    _models(models)
    image = tmp_path / "character.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0test-original")
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "harness-upload.jpg"})
        if request.url.path == "/prompt":
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"prompt_id": "h3-job-1", "node_errors": {}})
        if request.url.path == "/history/h3-job-1":
            return httpx.Response(200, json={"h3-job-1": {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {"15": {"videos": [{"filename": "clip.mp4", "subfolder": "harness", "type": "output"}]}},
            }})
        if request.url.path == "/view":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypisom" + b"video")
        return httpx.Response(404)

    provider = ComfyH3Provider(
        base_url="http://127.0.0.1:8188", models_dir=models,
        output_dir=tmp_path / "outputs", client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
    )
    monkeypatch.setattr(provider, "_audio_probe", lambda path: {"status": "pass", "has_audio": True})
    context = {
        "workspace": {"id": "ws"},
        "user_instruction": "请生成第 3 镜预览",
        "artifacts": [
            {"kind": "storyboard", "revision": 5, "content": {"shots": [{
                "index": 3, "duration_seconds": 8, "visual_prompt": "French salon",
                "asset_bindings": {"reference_images": [{"canonical_key": "char_hero"}]},
            }]}},
            {"kind": "reference_images", "revision": 8, "content": {"items": [{
                "canonical_key": "char_hero", "source_kind": "character",
                "local_path": str(image), "url": "/media/ws/ref.jpg",
            }]}},
        ],
    }
    result = provider.generate("preview", context)
    assert result["shot_index"] == 3
    assert result["provider_job_id"] == "h3-job-1"
    assert result["model"] == provider.model
    assert result["url"] == "/media/ws/preview/shot-003.mp4"
    assert result["status"] == "succeeded"
    assert Path(result["local_path"]).is_file()
    assert result["reference_keys"] == ["char_hero"]
    workflow = posted[0]["prompt"]
    assert workflow["6"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert workflow["6"]["inputs"]["ref_images.ref_image_0"] == ["20", 0]
    assert "<Picture 1> = char_hero" in workflow["6"]["inputs"]["prompt"]
    assert workflow["15"]["class_type"] == "SaveVideo"


def test_h3_fails_before_submission_on_missing_reference(tmp_path: Path):
    models = tmp_path / "models"
    _models(models)
    provider = ComfyH3Provider(
        base_url="http://127.0.0.1:8188", models_dir=models,
        output_dir=tmp_path / "outputs",
    )
    context = {"artifacts": [{"kind": "reference_images", "content": {"items": [{
        "canonical_key": "char_hero", "local_path": str(tmp_path / "missing.jpg"),
    }]}}]}
    with pytest.raises(SeedanceError, match="char_hero"):
        provider._preflight_reference_urls(context, ["char_hero"], ["old-url"])


def test_h3_batch_requires_h3_preview(tmp_path: Path):
    models = tmp_path / "models"
    _models(models)
    provider = ComfyH3Provider(
        base_url="http://127.0.0.1:8188", models_dir=models,
        output_dir=tmp_path / "outputs",
    )
    context = {
        "workspace": {"id": "ws"},
        "approvals": [{"gate": "preview_approved", "status": "approved"}],
        "artifacts": [
            {"kind": "storyboard", "content": {"shots": [{"index": 1}]}},
            {"kind": "preview", "content": {"status": "succeeded", "model": "doubao-seedance-2-0-mini-260615"}},
        ],
    }
    with pytest.raises(SeedanceError, match="H3.*单镜预览"):
        provider.generate("batch_video", context)
