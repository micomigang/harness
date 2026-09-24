from pathlib import Path

import httpx

from app.providers.seedream import SeedreamProvider


def test_reference_image_generates_and_downloads(tmp_path: Path):
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            payloads.append(request.read().decode())
            return httpx.Response(
                200, json={"data": [{"url": "https://files.example/reference.png"}]}
            )
        if str(request.url) == "https://files.example/reference.png":
            return httpx.Response(200, content=b"fake-png")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="doubao-seedream-5-0-pro-260628",
        output_dir=tmp_path,
        max_assets=1,
        client=client,
    )
    result = provider.generate(
        "reference_images",
        {
            "workspace": {"id": "workspace-1"},
            "artifacts": [
                {
                    "kind": "characters",
                    "content": {
                        "items": [
                            {
                                "id": "char_helene",
                                "name": "Hélène",
                                "description": "silver hair and a French country coat",
                            }
                        ]
                    },
                }
            ],
        },
    )

    item = result["items"][0]
    assert item["url"] == "/media/workspace-1/reference_images/characters-char_helene.png"
    assert Path(item["local_path"]).read_bytes() == b"fake-png"
    assert '"size":"2K"' in payloads[0]
    assert '"sequential_image_generation"' not in payloads[0]


def test_reference_image_honors_director_prompt_and_asset_cap(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(request.read().decode())
            return httpx.Response(200, json={"data": [{"url": "https://files.example/reference.png"}]})
        if str(request.url) == "https://files.example/reference.png":
            return httpx.Response(200, content=b"fake-png")
        return httpx.Response(404)

    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedream",
        output_dir=tmp_path,
        max_assets=6,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate(
        "reference_images",
        {
            "workspace": {"id": "w"},
            "execution_directive": {
                "prompt_addendum": "Contemporary Paris, understated French TV realism.",
                "parameter_overrides": {"max_assets": 1},
            },
            "artifacts": [
                {"kind": "characters", "content": {"items": [
                    {"id": "a", "name": "A", "description": "first"},
                    {"id": "b", "name": "B", "description": "second"},
                ]}},
            ],
        },
    )
    assert result["requested"] == 1
    assert "Contemporary Paris" in posted[0]


def test_targeted_candidate_uses_selected_image_as_edit_reference(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json
            posted.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"data": [{"url": "https://files.example/candidate.png"}]})
        if str(request.url) == "https://files.example/candidate.png":
            return httpx.Response(200, content=b"candidate")
        return httpx.Response(404)

    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedream",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate_candidate(
        workspace_id="w",
        source_kind="characters",
        item={
            "id": "char_grandmere",
            "canonical_key": "char_grandmere",
            "name": "Grand-mère",
            "appearance": "visage âgé, cheveux gris",
            "wardrobe": "manteau rouge matelassé",
            "continuity_lock": ["same face", "same red coat"],
        },
        feedback="Vieillir légèrement le visage, garder exactement le manteau rouge.",
        reference_url="https://files.example/base.png",
    )

    assert result["reference_used"] is True
    assert result["url"].startswith("/media/w/asset_candidates/characters-char_grandmere-")
    assert posted[0]["image"] == "https://files.example/base.png"
    assert "sequential_image_generation" not in posted[0]
    assert "Change only what the art director asks" in posted[0]["prompt"]
    assert "manteau rouge" in posted[0]["prompt"]


def test_reference_stage_prefers_user_selected_candidate_without_new_generation(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Seedream should not be called for a selected candidate")

    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedream",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate(
        "reference_images",
        {
            "workspace": {"id": "w"},
            "artifacts": [
                {"kind": "characters", "content": {"items": [
                    {"id": "char_grandmere", "canonical_key": "char_grandmere", "name": "Grand-mère"}
                ]}},
            ],
            "asset_candidates": [
                {
                    "id": "cand-1",
                    "stage": "characters",
                    "canonical_key": "char_grandmere",
                    "selected": True,
                    "url": "/media/w/asset_candidates/grandmere.png",
                    "remote_url": "https://files.example/grandmere.png",
                    "local_path": "/tmp/grandmere.png",
                    "model": "seedream",
                }
            ],
        },
    )
    item = result["items"][0]
    assert item["status"] == "selected_candidate"
    assert item["candidate_id"] == "cand-1"
    assert item["url"] == "/media/w/asset_candidates/grandmere.png"


def test_selected_candidates_are_not_dropped_by_generation_cap(tmp_path: Path):
    generated_posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            generated_posts.append(str(request.url))
            return httpx.Response(200, json={"data": [{"url": "https://files.example/new.png"}]})
        if str(request.url) == "https://files.example/new.png":
            return httpx.Response(200, content=b"new")
        return httpx.Response(404)

    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedream",
        output_dir=tmp_path,
        max_assets=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    artifacts = [{"kind": "characters", "content": {"items": [
        {"id": f"char_{i}", "canonical_key": f"char_{i}", "name": f"C{i}"} for i in range(3)
    ]}}]
    selected = [{
        "id": f"cand-{i}", "stage": "characters", "canonical_key": f"char_{i}", "selected": True,
        "url": f"/media/c{i}.png", "remote_url": f"https://files.example/c{i}.png", "local_path": "", "model": "seedream",
    } for i in range(3)]
    result = provider.generate("reference_images", {
        "workspace": {"id": "w"}, "artifacts": artifacts, "asset_candidates": selected,
    })
    assert result["completed"] == 3
    assert all(item["status"] == "selected_candidate" for item in result["items"])
    assert generated_posts == []


def test_visual_prompt_compiler_localizes_source_culture_for_france():
    prompt = SeedreamProvider._prompt(
        "characters",
        {
            "id": "char_grandmere",
            "canonical_key": "char_grandmere",
            "name": "Grand-mère",
            "appearance": "Femme âgée rurale",
            "wardrobe": "Manteau matelassé rouge à grands motifs floraux, bonnet bordeaux",
            "source_visual_traits": ["red floral padded rural coat", "burgundy knit cap", "modest rural silhouette"],
            "continuity_lock": ["keep the red floral padded coat", "same travel bag"],
            "localized_visual_design": {
                "wardrobe": "burgundy French countryside quilted field jacket, understated pattern, wool skirt, practical shoes",
            },
            "generation_prompt_en": "Elderly French countryside woman in a restrained burgundy quilted field jacket, practical rural clothing.",
        },
        localization_context={"target_market": "France", "target_language": "fr-FR"},
    )
    assert "Target market: France" in prompt
    assert "Provider-facing visual instructions are intentionally written in English" in prompt
    assert "French countryside quilted field jacket" in prompt
    assert "Avoid an oversized Chinese-style floral padded cotton jacket" in prompt
    assert "Source continuity constraints" in prompt
    assert "translate culturally" in prompt


def test_visual_prompt_compiler_localizes_scene_and_prop_for_france():
    scene_prompt = SeedreamProvider._prompt(
        "scenes",
        {"id": "scene_foyer", "name": "Foyer", "layout": "grand hall", "continuity_lock": []},
        localization_context={"target_market": "France"},
    )
    prop_prompt = SeedreamProvider._prompt(
        "props",
        {"id": "prop_teacup", "name": "Tea cup", "description": "tea service", "continuity_lock": []},
        localization_context={"target_market": "France"},
    )
    assert "Avoid Chinese/East-Asian mansion motifs" in scene_prompt
    assert "Use plausible French/European domestic object design" in prop_prompt
