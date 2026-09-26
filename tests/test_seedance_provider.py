import json
from pathlib import Path

import httpx
import pytest

from app.providers.seedance import SeedanceError, SeedanceProvider


def test_preview_creates_polls_and_downloads(tmp_path: Path):
    calls = []
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "POST":
            posted.append(json.loads(request.read().decode()))
            return httpx.Response(200, json={"id": "task-123"})
        if str(request.url).endswith("/contents/generations/tasks/task-123"):
            return httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "content": {"video_url": "https://files.example/clip.mp4"},
                },
            )
        if str(request.url) == "https://files.example/clip.mp4":
            return httpx.Response(200, content=b"fake-mp4")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SeedanceProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="doubao-seedance-2-5-260628",
        output_dir=tmp_path,
        client=client,
        poll_interval_seconds=0,
        sleeper=lambda _: None,
    )
    result = provider.generate(
        "preview",
        {
            "workspace": {"id": "workspace-1"},
            "artifacts": [
                {
                    "kind": "storyboard",
                    "content": {
                        "shots": [
                            {
                                "index": 1,
                                "duration_seconds": 8,
                                "visual_prompt": "A woman speaks to camera",
                                "dialogue_prompt": "Bonjour, bienvenue à Paris.",
                                "audio_prompt": "quiet room tone",
                            }
                        ]
                    },
                }
            ],
        },
    )

    assert result["status"] == "succeeded"
    assert result["model"] == "doubao-seedance-2-5-260628"
    assert result["url"] == "/media/workspace-1/preview/shot-001.mp4"
    assert (tmp_path / "workspace-1" / "preview" / "shot-001.mp4").read_bytes() == b"fake-mp4"
    assert len(calls) == 3
    assert posted[0]["duration"] == 8
    assert posted[0]["resolution"] == "720p"
    assert posted[0]["ratio"] == "9:16"
    assert posted[0]["generate_audio"] is True


def test_preview_honors_director_video_options_as_api_fields(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.read().decode()))
            return httpx.Response(200, json={"id": "task-x"})
        if str(request.url).endswith("/contents/generations/tasks/task-x"):
            return httpx.Response(200, json={"status": "succeeded", "content": {"video_url": "https://files.example/x.mp4"}})
        if str(request.url) == "https://files.example/x.mp4":
            return httpx.Response(200, content=b"x")
        return httpx.Response(404)

    provider = SeedanceProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedance",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval_seconds=0,
        sleeper=lambda _: None,
    )
    provider.generate(
        "preview",
        {
            "workspace": {"id": "w"},
            "execution_directive": {
                "prompt_addendum": "Natural French acting, restrained gestures.",
                "parameter_overrides": {"resolution": "1080p", "ratio": "9:16", "generate_audio": False},
            },
            "artifacts": [{"kind": "storyboard", "content": {"shots": [{"index": 1, "duration_seconds": 6, "visual_prompt": "Interior"}]}}],
        },
    )
    body = posted[0]
    assert body["resolution"] == "1080p"
    assert body["ratio"] == "9:16"
    assert body["duration"] == 6
    assert body["generate_audio"] is False
    assert "Natural French acting" in body["content"][0]["text"]
    assert "--resolution" not in body["content"][0]["text"]


def test_preview_uses_shot_bound_reference_images(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.read().decode()))
            return httpx.Response(200, json={"id": "task-r"})
        if str(request.url).endswith("/task-r"):
            return httpx.Response(200, json={"status": "succeeded", "content": {"video_url": "https://files.example/r.mp4"}})
        if str(request.url) == "https://files.example/r.mp4":
            return httpx.Response(200, content=b"r")
        return httpx.Response(404)

    provider = SeedanceProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedance",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval_seconds=0,
        sleeper=lambda _: None,
    )
    result = provider.generate(
        "preview",
        {
            "workspace": {"id": "w"},
            "artifacts": [
                {
                    "kind": "reference_images",
                    "content": {
                        "items": [
                            {"canonical_key": "char_unused", "remote_url": "https://ref/unused.png"},
                            {"canonical_key": "combo__hero__room", "source_kind": "combination", "remote_url": "https://ref/combo.png", "candidate_id": "c1"},
                            {"canonical_key": "char_hero", "source_kind": "character", "remote_url": "https://ref/hero.png", "candidate_id": "c2"},
                            {"canonical_key": "scene_room", "source_kind": "scene", "remote_url": "https://ref/room.png", "candidate_id": "c3"},
                        ]
                    },
                },
                {
                    "kind": "storyboard",
                    "content": {
                        "shots": [
                            {
                                "index": 1,
                                "duration_seconds": 5,
                                "visual_prompt": "Interior",
                                "asset_bindings": {
                                    "reference_images": [
                                        {"canonical_key": "char_hero", "candidate_id": "c2", "source_kind": "character"},
                                        {"canonical_key": "combo__hero__room", "candidate_id": "c1", "source_kind": "combination"},
                                        {"canonical_key": "scene_room", "candidate_id": "c3", "source_kind": "scene"},
                                    ]
                                },
                            }
                        ]
                    },
                },
            ],
        },
    )
    refs = [part["image_url"]["url"] for part in posted[0]["content"] if part["type"] == "image_url"]
    assert refs == ["https://ref/combo.png", "https://ref/hero.png", "https://ref/room.png"]
    assert "https://ref/unused.png" not in refs
    assert result["reference_keys"] == ["combo__hero__room", "char_hero", "scene_room"]


def test_dialogue_prompt_preserves_multiple_speakers(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.read().decode()))
            return httpx.Response(200, json={"id": "task-d"})
        if str(request.url).endswith("/task-d"):
            return httpx.Response(200, json={"status": "succeeded", "content": {"video_url": "https://files.example/d.mp4"}})
        if str(request.url) == "https://files.example/d.mp4":
            return httpx.Response(200, content=b"d")
        return httpx.Response(404)

    provider = SeedanceProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedance",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval_seconds=0,
        sleeper=lambda _: None,
    )
    provider.generate(
        "preview",
        {
            "workspace": {"id": "w"},
            "artifacts": [
                {"kind": "storyboard", "content": {"shots": [{"index": 1, "duration_seconds": 8, "visual_prompt": "Foyer"}]}},
                {"kind": "dialogue_plan", "content": {"items": [{"shot_index": 1, "language": "fr", "dialogue_lines": [
                    {"speaker_id": "char_son", "text": "Tu es là... déjà."},
                    {"speaker_id": "char_grandmere", "text": "J’arrive au mauvais moment ?"},
                ]}]}}
            ],
        },
    )
    prompt = posted[0]["content"][0]["text"]
    assert "char_son" in prompt and "Tu es là... déjà." in prompt
    assert "char_grandmere" in prompt and "J’arrive au mauvais moment ?" in prompt
    assert "do not merge speakers" in prompt


def test_fallback_error_reports_both_models(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.read().decode())
            model = body["model"]
            return httpx.Response(404, json={"error": {"code": "ModelNotOpen", "message": f"{model} not activated"}})
        return httpx.Response(404)

    provider = SeedanceProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="doubao-seedance-2-5-260628",
        fallback_model="doubao-seedance-2-0-260128",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval_seconds=0,
        sleeper=lambda _: None,
    )
    with pytest.raises(SeedanceError) as excinfo:
        provider.generate(
            "preview",
            {"workspace": {"id": "w"}, "artifacts": [{"kind": "storyboard", "content": {"shots": [{"index": 1, "duration_seconds": 8, "visual_prompt": "Interior"}]}}]},
        )
    message = str(excinfo.value)
    assert "doubao-seedance-2-5-260628" in message
    assert "doubao-seedance-2-0-260128" in message
    assert "ModelNotOpen" in message


def test_preview_prefers_local_reference_as_base64(tmp_path: Path):
    posted = []
    ref = tmp_path / "locked-reference.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\nlocal-reference-bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.read().decode()))
            return httpx.Response(200, json={"id": "task-local"})
        if str(request.url).endswith("/task-local"):
            return httpx.Response(200, json={"status": "succeeded", "content": {"video_url": "https://files.example/local.mp4"}})
        if str(request.url) == "https://files.example/local.mp4":
            return httpx.Response(200, content=b"local-video")
        return httpx.Response(404)

    provider = SeedanceProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="doubao-seedance-2-5-260628",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval_seconds=0,
        sleeper=lambda _: None,
    )
    provider.generate(
        "preview",
        {
            "workspace": {"id": "w"},
            "artifacts": [
                {
                    "kind": "reference_images",
                    "content": {"items": [{
                        "canonical_key": "char_hero",
                        "candidate_id": "c1",
                        "remote_url": "https://expired.example/reference.png",
                        "local_path": str(ref),
                        "source_kind": "character",
                    }]},
                },
                {
                    "kind": "storyboard",
                    "content": {"shots": [{
                        "index": 1,
                        "duration_seconds": 5,
                        "visual_prompt": "Interior",
                        "asset_bindings": {"reference_images": [{
                            "canonical_key": "char_hero",
                            "candidate_id": "c1",
                            "source_kind": "character",
                        }]},
                    }]},
                },
            ],
        },
    )
    refs = [part["image_url"]["url"] for part in posted[0]["content"] if part["type"] == "image_url"]
    assert len(refs) == 1
    assert refs[0].startswith("data:image/png;base64,")
    assert "expired.example" not in refs[0]
