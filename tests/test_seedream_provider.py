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
    assert result["requested"] == 2
    assert result["completed"] == 1
    assert result["missing_isolated"] == ["b"]
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


def test_reference_stage_generates_explicit_multi_reference_combination(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json
            posted.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"data": [{"url": "https://files.example/combo.png"}]})
        if str(request.url) == "https://files.example/combo.png":
            return httpx.Response(200, content=b"combo")
        return httpx.Response(404)

    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="doubao-seedream-5-0-pro-260628",
        output_dir=tmp_path,
        max_assets=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate("reference_images", {
        "workspace": {"id": "w"},
        "user_instruction": "1. char_grandmere + scene_foyer + prop_mysterious_travel_bag",
        "execution_directive": {"prompt_addendum": "Preserve exact identities."},
        "artifacts": [
            {"kind": "characters", "content": {"items": [{"id": "char_grandmere", "canonical_key": "char_grandmere", "name": "Grandmere"}]}},
            {"kind": "scenes", "content": {"items": [{"id": "scene_foyer", "canonical_key": "scene_foyer", "name": "Foyer"}]}},
            {"kind": "props", "content": {"items": [{"id": "prop_mysterious_travel_bag", "canonical_key": "prop_mysterious_travel_bag", "name": "Bag"}]}},
        ],
        "asset_candidates": [
            {"id": "c1", "stage": "characters", "canonical_key": "char_grandmere", "selected": True, "remote_url": "https://files.example/grandmere.png", "url": "/media/grandmere.png"},
            {"id": "c2", "stage": "scenes", "canonical_key": "scene_foyer", "selected": True, "remote_url": "https://files.example/foyer.png", "url": "/media/foyer.png"},
            {"id": "c3", "stage": "props", "canonical_key": "prop_mysterious_travel_bag", "selected": True, "remote_url": "https://files.example/bag.png", "url": "/media/bag.png"},
        ],
    })

    assert result["selection_coverage"]["upstream_selected_count"] == 3
    assert result["selection_coverage"]["combination_completed"] == 1
    combo = next(item for item in result["items"] if item["source_kind"] == "combination")
    assert combo["source_id"] == ["char_grandmere", "scene_foyer", "prop_mysterious_travel_bag"]
    assert len(posted) == 1
    assert posted[0]["image"] == [
        "https://files.example/grandmere.png",
        "https://files.example/foyer.png",
        "https://files.example/bag.png",
    ]
    assert "sequential_image_generation" not in posted[0]


def test_reference_source_kinds_are_singular_for_upstream_adopted(tmp_path: Path):
    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedream",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("no generation expected")))),
    )
    result = provider.generate("reference_images", {
        "workspace": {"id": "w"},
        "artifacts": [
            {"kind": "characters", "content": {"items": [{"id": "char_a", "canonical_key": "char_a"}]}},
            {"kind": "scenes", "content": {"items": [{"id": "scene_a", "canonical_key": "scene_a"}]}},
            {"kind": "props", "content": {"items": [{"id": "prop_a", "canonical_key": "prop_a"}]}},
        ],
        "asset_candidates": [
            {"id": "1", "stage": "characters", "canonical_key": "char_a", "selected": True, "url": "/a.png"},
            {"id": "2", "stage": "scenes", "canonical_key": "scene_a", "selected": True, "url": "/b.png"},
            {"id": "3", "stage": "props", "canonical_key": "prop_a", "selected": True, "url": "/c.png"},
        ],
    })
    assert [item["source_kind"] for item in result["items"]] == ["character", "scene", "prop"]


def test_combination_parser_ignores_category_rollups_and_director_style_summaries():
    text = """
Characters: char_grandmere, char_son, char_boy
Scenes: scene_foyer, scene_salon
Props: prop_bag, prop_cup
1. char_grandmere + scene_foyer + prop_bag
2. char_son + scene_foyer
Example binding source_ids: [char_grandmere, scene_foyer, prop_bag]
"""
    assert SeedreamProvider._extract_reference_combinations(text) == [
        ["char_grandmere", "scene_foyer", "prop_bag"],
        ["char_son", "scene_foyer"],
    ]


def test_reference_validation_passes_for_selected_isolated_and_explicit_combinations(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json
            posted.append(json.loads(request.content.decode()))
            idx = len(posted)
            return httpx.Response(200, json={"data": [{"url": f"https://files.example/combo-{idx}.png"}]})
        if str(request.url).startswith("https://files.example/combo-"):
            return httpx.Response(200, content=b"combo")
        return httpx.Response(404)

    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedream",
        output_dir=tmp_path,
        max_assets=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate("reference_images", {
        "workspace": {"id": "w"},
        "user_instruction": "1. char_a + scene_a + prop_a\n2. char_b + scene_a",
        "execution_directive": {
            # Deliberately contains misleading multi-key prose. It must not create
            # extra combinations because combination intent is user-owned.
            "interpretation": "Characters char_a char_b and scenes scene_a scene_b and props prop_a prop_b",
            "prompt_addendum": "Preserve exact identities.",
        },
        "artifacts": [
            {"kind": "characters", "content": {"items": [
                {"id": "char_a", "canonical_key": "char_a"},
                {"id": "char_b", "canonical_key": "char_b"},
            ]}},
            {"kind": "scenes", "content": {"items": [
                {"id": "scene_a", "canonical_key": "scene_a"},
                {"id": "scene_b", "canonical_key": "scene_b"},
            ]}},
            {"kind": "props", "content": {"items": [
                {"id": "prop_a", "canonical_key": "prop_a"},
                {"id": "prop_b", "canonical_key": "prop_b"},
            ]}},
        ],
        "asset_candidates": [
            {"id": "c1", "stage": "characters", "canonical_key": "char_a", "selected": True, "remote_url": "https://files.example/char-a.png", "url": "/char-a.png"},
            {"id": "c2", "stage": "characters", "canonical_key": "char_b", "selected": True, "remote_url": "https://files.example/char-b.png", "url": "/char-b.png"},
            {"id": "s1", "stage": "scenes", "canonical_key": "scene_a", "selected": True, "remote_url": "https://files.example/scene-a.png", "url": "/scene-a.png"},
            {"id": "s2", "stage": "scenes", "canonical_key": "scene_b", "selected": True, "remote_url": "https://files.example/scene-b.png", "url": "/scene-b.png"},
            {"id": "p1", "stage": "props", "canonical_key": "prop_a", "selected": True, "remote_url": "https://files.example/prop-a.png", "url": "/prop-a.png"},
            {"id": "p2", "stage": "props", "canonical_key": "prop_b", "selected": True, "remote_url": "https://files.example/prop-b.png", "url": "/prop-b.png"},
        ],
    })

    assert result["selection_coverage"]["isolated_completed"] == 6
    assert result["selection_coverage"]["combination_completed"] == 2
    assert result["reference_plan"]["combinations"] == [
        ["char_a", "scene_a", "prop_a"],
        ["char_b", "scene_a"],
    ]
    assert result["reference_validation"]["status"] == "pass"
    assert result["reference_validation"]["unexpected_combinations"] == []
    assert result["completed"] == 8
    assert len(posted) == 2
