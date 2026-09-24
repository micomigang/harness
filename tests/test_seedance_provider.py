from pathlib import Path

import httpx

from app.providers.seedance import SeedanceProvider


def test_preview_creates_polls_and_downloads(tmp_path: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "POST":
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


def test_preview_honors_director_video_options(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(request.read().decode())
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
    assert "Natural French acting" in posted[0]
    assert "--resolution 1080p" in posted[0]
    assert "--generate_audio false" in posted[0]
