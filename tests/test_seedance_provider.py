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
    assert result["request_payload_digest"]["technical_params"]["generate_audio"] is True
    assert result["request_payload_digest"]["prompt_sha256"]
    assert result["assembly_log"]["storyboard"]["visual_prompt_hydrated"] is True
    assert result["preflight_checks"]["reference_resolver"]["status"] == "pass"
    assert result["preflight_checks"]["trusted_provenance_tos"]["status"] == "skipped"
    assert result["provider_generate_audio_echo"] is None
    assert result["audio_probe"]["status"] == "unavailable"


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
                            {"canonical_key": "combo__char_hero__scene_room", "source_kind": "combination", "source_ids": ["char_hero", "scene_room"], "remote_url": "https://ref/combo.png", "candidate_id": "c1"},
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
                                        {"canonical_key": "combo__char_hero__scene_room", "candidate_id": "c1", "source_kind": "combination", "source_ids": ["char_hero", "scene_room"]},
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
    assert refs == ["https://ref/hero.png", "https://ref/room.png"]
    assert "https://ref/combo.png" not in refs
    assert "https://ref/unused.png" not in refs
    assert result["reference_keys"] == ["char_hero", "scene_room"]


def test_human_combinations_expand_to_current_isolated_truths_and_keep_nonhuman_combo(tmp_path: Path):
    context = {
        "artifacts": [{"kind": "reference_images", "content": {"items": [
            {"canonical_key": "char_grandmere", "source_kind": "character", "candidate_id": "old-grandmere", "remote_url": "https://ref/old-grandmere.png"},
            {"canonical_key": "char_son", "source_kind": "character", "candidate_id": "son", "remote_url": "https://ref/son.png"},
            {"canonical_key": "scene_foyer", "source_kind": "scene", "candidate_id": "foyer", "remote_url": "https://ref/foyer.png"},
            {"canonical_key": "prop_mysterious_travel_bag", "source_kind": "prop", "candidate_id": "bag", "remote_url": "https://ref/bag.png"},
            {"canonical_key": "prop_crystal_chandelier", "source_kind": "prop", "candidate_id": "chandelier", "remote_url": "https://ref/chandelier.png"},
            {"canonical_key": "combo__char_grandmere__scene_foyer__prop_mysterious_travel_bag", "source_kind": "combination", "source_ids": ["char_grandmere", "scene_foyer", "prop_mysterious_travel_bag"], "candidate_id": "combo-grandmere", "remote_url": "https://ref/combo-grandmere.png"},
            {"canonical_key": "combo__char_son__scene_foyer", "source_kind": "combination", "source_ids": ["char_son", "scene_foyer"], "candidate_id": "combo-son", "remote_url": "https://ref/combo-son.png"},
            {"canonical_key": "combo__scene_foyer__prop_crystal_chandelier", "source_kind": "combination", "source_ids": ["scene_foyer", "prop_crystal_chandelier"], "candidate_id": "combo-context", "remote_url": "https://ref/combo-context.png"},
        ]}}],
        "asset_candidates": [
            {
                "id": "stale-reference-proxy", "stage": "reference_images", "selected": True,
                "canonical_key": "char_grandmere", "remote_url": "https://ref/stale-reference-proxy.png",
                "provenance": {},
            },
            {
                "id": "new-grandmere", "stage": "characters", "selected": True,
                "canonical_key": "char_grandmere", "remote_url": "https://ref/new-grandmere.png",
                "provenance": {"generation_mode": "text_to_image"},
            },
        ],
    }
    shot = {"asset_bindings": {"reference_images": [
        {"canonical_key": "combo__char_grandmere__scene_foyer__prop_mysterious_travel_bag", "candidate_id": "combo-grandmere", "source_kind": "combination"},
        {"canonical_key": "combo__char_son__scene_foyer", "candidate_id": "combo-son", "source_kind": "combination"},
        {"canonical_key": "prop_crystal_chandelier", "candidate_id": "chandelier", "source_kind": "prop"},
        {"canonical_key": "combo__scene_foyer__prop_crystal_chandelier", "candidate_id": "combo-context", "source_kind": "combination"},
        {"canonical_key": "char_son", "candidate_id": "son", "source_kind": "character"},
    ]}}

    urls, keys = SeedanceProvider._shot_reference_urls(context, shot, max_images=9)

    assert keys == [
        "char_grandmere",
        "char_son",
        "scene_foyer",
        "prop_mysterious_travel_bag",
        "prop_crystal_chandelier",
        "combo__scene_foyer__prop_crystal_chandelier",
    ]
    assert urls == [
        "https://ref/new-grandmere.png",
        "https://ref/son.png",
        "https://ref/foyer.png",
        "https://ref/bag.png",
        "https://ref/chandelier.png",
        "https://ref/combo-context.png",
    ]
    assert "https://ref/combo-grandmere.png" not in urls
    assert "https://ref/combo-son.png" not in urls
    assert "https://ref/stale-reference-proxy.png" not in urls


def test_isolated_character_preflight_hydrates_current_upstream_provenance(tmp_path: Path):
    from datetime import datetime, timezone
    import hashlib

    signed_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    original_url = (
        "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com/Image/2131396012/current.jpeg"
        f"?X-Tos-Date={signed_at}&X-Tos-Expires=86400"
    )
    raw = b"\xff\xd8\xff\xe0trusted-current-character"
    local = tmp_path / "current-character.jpg"
    local.write_bytes(raw)
    provenance = {
        "provider": "volcengine-ark",
        "model": "doubao-seedream-5-0-pro-260628",
        "generation_mode": "text_to_image",
        "account_id": "2131396012",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "original_url": original_url,
        "media_format": "jpeg",
        "format_verified": True,
        "original_bytes": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "contains_person": True,
    }
    key = "char_grandmere"
    context = {
        "artifacts": [{"kind": "reference_images", "content": {"items": [{
            "canonical_key": key,
            "source_kind": "character",
            "candidate_id": "reference-proxy",
            "model": "doubao-seedream-5-0-pro-260628",
            "remote_url": "https://ref.example/stale.jpeg",
            "provenance": {},
        }]}}],
        "asset_candidates": [
            {
                "id": "reference-proxy",
                "stage": "reference_images",
                "selected": True,
                "canonical_key": key,
                "model": "doubao-seedream-5-0-pro-260628",
                "remote_url": "https://ref.example/stale.jpeg",
                "provenance": {},
            },
            {
                "id": "trusted-character",
                "stage": "characters",
                "selected": True,
                "canonical_key": key,
                "model": provenance["model"],
                "remote_url": original_url,
                "url": "/media/current-character.jpg",
                "local_path": str(local),
                "provenance": provenance,
            },
        ],
    }
    shot = {"asset_bindings": {"reference_images": [{"canonical_key": key, "candidate_id": "reference-proxy"}]}}
    urls, keys = SeedanceProvider._shot_reference_urls(context, shot)
    assert keys == [key]
    assert urls == [original_url]

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        return httpx.Response(206)

    provider = SeedanceProvider(
        api_key="test",
        base_url="https://ark.example/api/v3",
        model="doubao-seedance-2-0-260128",
        output_dir=tmp_path,
        account_id="2131396012",
        preflight_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert provider._preflight_reference_urls(context, keys, urls) == [original_url]
    assert requests == [("GET", original_url)]


def test_reference_limit_is_checked_after_human_combination_expansion():
    source_ids = ["char_a", "scene_a", "prop_a"]
    extra_props = [f"prop_extra_{index}" for index in range(7)]
    items = [
        {"canonical_key": "char_a", "source_kind": "character", "remote_url": "https://ref/char-a.png"},
        {"canonical_key": "scene_a", "source_kind": "scene", "remote_url": "https://ref/scene-a.png"},
        {"canonical_key": "prop_a", "source_kind": "prop", "remote_url": "https://ref/prop-a.png"},
        {"canonical_key": "combo__char_a__scene_a__prop_a", "source_kind": "combination", "source_ids": source_ids, "remote_url": "https://ref/combo.png"},
    ]
    items.extend(
        {"canonical_key": key, "source_kind": "prop", "remote_url": f"https://ref/{key}.png"}
        for key in extra_props
    )
    context = {"artifacts": [{"kind": "reference_images", "content": {"items": items}}]}
    shot = {"asset_bindings": {"reference_images": [
        {"canonical_key": "combo__char_a__scene_a__prop_a", "source_kind": "combination", "source_ids": source_ids},
        *[{"canonical_key": key, "source_kind": "prop"} for key in extra_props],
    ]}}

    with pytest.raises(SeedanceError, match=r"展开并去重后共有 10 张.*最多支持 9 张"):
        SeedanceProvider._shot_reference_urls(context, shot, max_images=9)


def test_unbound_shot_does_not_attach_unchecked_global_reference_images():
    context = {"artifacts": [{"kind": "reference_images", "content": {"items": [
        {"canonical_key": "char_a", "source_kind": "character", "remote_url": "https://ref/char-a.png"},
    ]}}]}
    assert SeedanceProvider._shot_reference_urls(context, {"index": 1}) == ([], [])


def test_selected_reference_candidate_replaces_stale_artifact_binding(tmp_path: Path):
    from datetime import datetime, timezone

    signed_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    original = (
        "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com/new-original.jpeg"
        f"?X-Tos-Date={signed_at}&X-Tos-Expires=86400"
    )
    context = {
        "artifacts": [{"kind": "reference_images", "content": {"items": [
            {"canonical_key": "combo__scene_salon__prop_lamp",
             "candidate_id": "old-id", "model": "doubao-seedream-5-0-pro-260628",
             "remote_url": "https://old.example/expired.jpeg"},
        ]}}],
        "asset_candidates": [{
            "id": "new-id", "stage": "reference_images", "selected": True,
            "canonical_key": "combo__scene_salon__prop_lamp",
            "model": "doubao-seedream-5-0-pro-260628", "remote_url": original,
            "local_path": str(tmp_path / "new-original.jpg"), "url": "/media/new-original.jpg",
        }],
    }
    shot = {"asset_bindings": {"reference_images": [
        {"canonical_key": "combo__scene_salon__prop_lamp", "candidate_id": "old-id"},
    ]}}
    urls, keys = SeedanceProvider._shot_reference_urls(context, shot)
    assert urls == [original]
    assert keys == ["combo__scene_salon__prop_lamp"]


def test_preview_selects_requested_third_shot_and_all_six_references(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.read().decode()))
            return httpx.Response(200, json={"id": "task-3"})
        if str(request.url).endswith("/task-3"):
            return httpx.Response(200, json={"status": "succeeded", "content": {"video_url": "https://files.example/3.mp4"}})
        if str(request.url) == "https://files.example/3.mp4":
            return httpx.Response(200, content=b"third-shot")
        return httpx.Response(404)

    provider = SeedanceProvider(
        api_key="test-key", base_url="https://ark.example/api/v3",
        model="doubao-seedance-2-0-mini-260615", output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval_seconds=0, sleeper=lambda _: None,
    )
    keys = [f"ref-{index}" for index in range(6)]
    context = {
        "workspace": {"id": "w"},
        "user_instruction": "预览第 3 镜（shot_index 3）",
        "artifacts": [
            {"kind": "storyboard", "content": {"shots": [
                {"index": 1, "duration_seconds": 8, "visual_prompt": "Foyer"},
                {"index": 3, "duration_seconds": 12, "visual_prompt": "Salon conflict",
                 "asset_bindings": {"reference_images": [{"canonical_key": key} for key in keys]}},
            ]}},
            {"kind": "reference_images", "content": {"items": [
                {"canonical_key": key, "remote_url": f"https://ref.example/{key}.png"}
                for key in keys
            ]}},
            {"kind": "dialogue_plan", "content": {"items": [
                {"shot_index": 3, "language": "fr", "dialogue_text": "Sortez."}
            ]}},
        ],
    }
    result = provider.generate("preview", context)
    assert result["shot_index"] == 3
    assert result["url"] == "/media/w/preview/shot-003.mp4"
    assert result["reference_keys"] == keys
    assert len([part for part in posted[0]["content"] if part["type"] == "image_url"]) == 6
    assert "Salon conflict" in posted[0]["content"][0]["text"]
    assert "Sortez." in posted[0]["content"][0]["text"]
    assert "Foyer" not in posted[0]["content"][0]["text"]


def test_preview_missing_requested_shot_stops_before_api_call(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("provider should not be called")

    provider = SeedanceProvider(
        api_key="test-key", base_url="https://ark.example/api/v3", model="seedance",
        output_dir=tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SeedanceError, match="shot_index=3"):
        provider.generate("preview", {
            "workspace": {"id": "w"}, "user_instruction": "shot_index=3",
            "artifacts": [{"kind": "storyboard", "content": {"shots": [
                {"index": 1, "duration_seconds": 8, "visual_prompt": "Foyer"}
            ]}}],
        })


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


def test_recent_seedream_original_url_is_used_for_video_reference(tmp_path: Path):
    from datetime import datetime, timezone

    date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    remote = (
        "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com/original.jpeg"
        f"?X-Tos-Date={date}&X-Tos-Expires=86400"
    )
    local = tmp_path / "reencoded.png"
    local.write_bytes(b"\x89PNG\r\n\x1a\nlocal-copy")
    item = {
        "model": "doubao-seedream-5-0-pro-260628",
        "remote_url": remote,
        "local_path": str(local),
    }
    assert SeedanceProvider._reference_input_value(item) == remote
    item["remote_url"] = remote.replace("X-Tos-Expires=86400", "X-Tos-Expires=0")
    assert SeedanceProvider._reference_input_value(item).startswith("data:image/png;base64,")


def test_person_reference_preflight_requires_trusted_original_and_reachable_url(tmp_path: Path):
    from datetime import datetime, timezone
    import hashlib

    signed_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    original_url = (
        "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com/Image/2131396012/original.jpeg"
        f"?X-Tos-Date={signed_at}&X-Tos-Expires=86400"
    )
    raw = b"\xff\xd8\xff\xe0original"
    local = tmp_path / "original.jpg"
    local.write_bytes(raw)
    provenance = {
        "provider": "volcengine-ark", "model": "doubao-seedream-5-0-pro-260628",
        "generation_mode": "text_to_image", "account_id": "2131396012",
        "generated_at": datetime.now(timezone.utc).isoformat(), "original_url": original_url,
        "media_format": "jpeg", "format_verified": True, "original_bytes": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    key = "char_youngwoman"
    item = {"canonical_key": key, "source_kind": "character", "model": provenance["model"],
            "remote_url": original_url, "local_path": str(local), "provenance": provenance}
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        return httpx.Response(206)

    provider = SeedanceProvider(
        api_key="test", base_url="https://ark.example/api/v3", model="doubao-seedance-2-0-mini-260615",
        output_dir=tmp_path, account_id="2131396012", preflight_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    context = {"artifacts": [{"kind": "reference_images", "content": {"items": [item]}}]}
    assert provider._preflight_reference_urls(context, [key], [original_url]) == [original_url]
    assert requests == [("GET", original_url)]

    provenance["generation_mode"] = "image_to_image"
    with pytest.raises(SeedanceError, match=key):
        provider._preflight_reference_urls(context, [key], [original_url])
    assert len(requests) == 1

    provenance["generation_mode"] = "text_to_image"
    item["remote_url"] = original_url.replace("X-Tos-Expires=86400", "X-Tos-Expires=0")
    provenance["original_url"] = item["remote_url"]
    with pytest.raises(SeedanceError, match="不会回退到本地 Base64"):
        provider._preflight_reference_urls(context, [key], ["data:image/jpeg;base64,AA=="])
    assert len(requests) == 1


def test_preflight_rejects_direct_human_combination_even_with_provenance(tmp_path: Path):
    key = "combo__char_a__scene_a"
    item = {
        "canonical_key": key,
        "source_kind": "combination",
        "source_ids": ["char_a", "scene_a"],
        "remote_url": "https://ref/combo.png",
        "provenance": {"contains_person": True},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("human combination must be rejected before transport check")

    provider = SeedanceProvider(
        api_key="test", base_url="https://ark.example/api/v3", model="seedance",
        output_dir=tmp_path, preflight_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    context = {"artifacts": [{"kind": "reference_images", "content": {"items": [item]}}]}
    with pytest.raises(SeedanceError, match="含人物 relationship combination"):
        provider._preflight_reference_urls(context, [key], [item["remote_url"]])


def test_batch_preflights_all_shots_before_creating_any_video_task(tmp_path: Path, monkeypatch):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(request)
        return httpx.Response(200, json={"id": "unexpected-task"})

    provider = SeedanceProvider(
        api_key="test", base_url="https://ark.example/api/v3",
        model="doubao-seedance-2-0-mini-260615", output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(provider, "_shot_reference_urls", lambda context, shot, **kwargs: (["image"], [f"shot-{shot['index']}"]))

    def preflight(context, keys, urls):
        if keys == ["shot-2"]:
            raise SeedanceError("镜头参考图 shot-2 不合格")
        return urls

    monkeypatch.setattr(provider, "_preflight_reference_urls", preflight)
    context = {"workspace": {"id": "w"}, "approvals": [{"gate": "preview_approved", "status": "approved"}], "artifacts": [{
        "kind": "storyboard", "content": {"shots": [
            {"index": 1, "visual_prompt": "First"},
            {"index": 2, "visual_prompt": "Second"},
        ]},
    }]}
    with pytest.raises(SeedanceError, match="shot-2"):
        provider.generate("batch_video", context)
    assert posted == []


def test_batch_requires_explicit_preview_approval_before_any_provider_call(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200, json={"id": "unexpected"})

    provider = SeedanceProvider(
        api_key="test", base_url="https://ark.example/api/v3", model="seedance",
        output_dir=tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    context = {
        "workspace": {"id": "w"},
        "approvals": [{"gate": "preview_approved", "status": "pending"}],
        "artifacts": [{"kind": "storyboard", "content": {"shots": [
            {"index": 1, "visual_prompt": "First"},
            {"index": 2, "visual_prompt": "Second"},
        ]}}],
    }
    with pytest.raises(SeedanceError, match="preview_approved=approved"):
        provider.generate("batch_video", context)
    assert posted == []


def test_full_batch_reuses_approved_preview_and_covers_entire_storyboard(tmp_path: Path, monkeypatch):
    provider = SeedanceProvider(
        api_key="test", base_url="https://ark.example/api/v3", model="seedance",
        output_dir=tmp_path, batch_concurrency=3,
    )
    generated = []
    monkeypatch.setattr(provider, "_shot_reference_urls", lambda context, shot, **kwargs: ([], []))
    monkeypatch.setattr(provider, "_preflight_reference_urls", lambda context, keys, urls: urls)

    def fake_generate_one(workspace_id, shot, group, reference_urls, reference_keys, dialogue, sound, options, **kwargs):
        index = int(shot["index"])
        generated.append(index)
        return {
            "shot_index": index,
            "provider_job_id": f"task-{index}",
            "model": "seedance",
            "url": f"/media/w/batch/shot-{index:03d}.mp4",
            "status": "succeeded",
        }

    monkeypatch.setattr(provider, "_generate_one", fake_generate_one)
    shots = [{"index": index, "visual_prompt": f"Shot {index}"} for index in range(1, 15)]
    context = {
        "workspace": {"id": "w"},
        "approvals": [{"gate": "preview_approved", "status": "approved"}],
        "artifacts": [
            {"kind": "storyboard", "content": {"shots": shots}},
            {"kind": "preview", "revision": 2, "content": {
                "shot_index": 1, "provider_job_id": "preview-1", "model": "seedance",
                "url": "/media/w/preview/shot-001.mp4", "status": "succeeded",
            }},
        ],
    }
    result = provider.generate("batch_video", context)
    assert result["status"] == "succeeded"
    assert result["coverage_complete"] is True
    assert result["requested"] == 14
    assert result["completed"] == 14
    assert result["submitted_new_tasks"] == 13
    assert result["reused_existing"] == 1
    assert result["batch_concurrency"] == 3
    assert sorted(generated) == list(range(2, 15))
    assert result["items"][0]["shot_index"] == 1
    assert result["items"][0]["reused_from"] == "approved_preview"


def test_batch_resume_reuses_existing_successes_and_only_generates_missing_shots(tmp_path: Path, monkeypatch):
    provider = SeedanceProvider(
        api_key="test", base_url="https://ark.example/api/v3", model="seedance",
        output_dir=tmp_path, batch_concurrency=3,
    )
    generated = []
    monkeypatch.setattr(provider, "_shot_reference_urls", lambda context, shot, **kwargs: ([], []))
    monkeypatch.setattr(provider, "_preflight_reference_urls", lambda context, keys, urls: urls)

    def fake_generate_one(workspace_id, shot, group, reference_urls, reference_keys, dialogue, sound, options, **kwargs):
        index = int(shot["index"])
        generated.append(index)
        return {
            "shot_index": index,
            "provider_job_id": f"task-{index}",
            "model": "seedance",
            "url": f"/media/w/batch/shot-{index:03d}.mp4",
            "status": "succeeded",
        }

    monkeypatch.setattr(provider, "_generate_one", fake_generate_one)
    shots = [{"index": index, "visual_prompt": f"Shot {index}"} for index in range(1, 15)]
    old_items = [
        {"shot_index": index, "provider_job_id": f"old-{index}", "model": "seedance",
         "url": f"/media/w/batch/shot-{index:03d}.mp4", "status": "succeeded"}
        for index in range(1, 9)
    ]
    context = {
        "workspace": {"id": "w"},
        "approvals": [{"gate": "preview_approved", "status": "approved"}],
        "artifacts": [
            {"kind": "storyboard", "content": {"shots": shots}},
            {"kind": "preview", "revision": 2, "content": {
                "shot_index": 1, "provider_job_id": "approved-preview-1", "model": "seedance",
                "url": "/media/w/preview/shot-001.mp4", "status": "succeeded",
            }},
            {"kind": "batch_video", "revision": 1, "status": "ready", "content": {"items": old_items}},
        ],
    }
    result = provider.generate("batch_video", context)
    assert result["status"] == "succeeded"
    assert result["completed"] == 14
    assert result["submitted_new_tasks"] == 6
    assert result["reused_existing"] == 8
    assert sorted(generated) == list(range(9, 15))
    first = next(item for item in result["items"] if item["shot_index"] == 1)
    assert first["provider_job_id"] == "approved-preview-1"
    assert first["reused_from"] == "approved_preview"


def test_assembly_log_explicitly_records_ducking_and_negative_audio():
    log = SeedanceProvider._assembly_log(
        {"artifacts": []},
        {"index": 14, "visual_prompt": "Cliffhanger", "camera": {}, "blocking": {}, "asset_bindings": {}},
        {"language": "fr", "lines": [], "timing": {"relative_start": 0.0, "relative_end": 12.0}},
        {
            "ambience": "near silence",
            "foley": ["hand enters bag"],
            "ducking": {"active": False, "note": "silent shot"},
            "negative_audio": ["no suspense sting", "no heartbeat"],
        },
    )
    assert log["dialogue_plan"]["line_count"] == 0
    assert log["sound_plan"]["ducking_active"] is False
    assert log["sound_plan"]["negative_audio_count"] == 2
    assert log["sound_plan"]["negative_audio"] == ["no suspense sting", "no heartbeat"]
