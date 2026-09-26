from pathlib import Path

import httpx

from app.providers.seedream import SeedreamProvider
from app.reference_plan import extract_explicit_reference_combinations


def _selected(stage: str, key: str, cid: str) -> dict:
    return {
        "id": cid,
        "stage": stage,
        "canonical_key": key,
        "selected": True,
        "remote_url": f"https://files.example/{key}.png",
        "url": f"/media/{key}.png",
        "local_path": "",
        "model": "seedream",
    }


def _context(instruction: str) -> dict:
    return {
        "workspace": {"id": "w"},
        "user_instruction": instruction,
        "execution_directive": {"effective_instruction": instruction},
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
            _selected("characters", "char_a", "ca"),
            _selected("characters", "char_b", "cb"),
            _selected("scenes", "scene_a", "sa"),
            _selected("scenes", "scene_b", "sb"),
            _selected("props", "prop_a", "pa"),
            _selected("props", "prop_b", "pb"),
        ],
    }


def test_parser_accepts_canonical_combo_ids_without_overreading_rollups():
    text = """
5 character + 5 scene + 4 prop
- combo__char_a__scene_a__prop_a
- combo__char_b__scene_b
Characters: char_a, char_b; scenes: scene_a, scene_b
"""
    assert extract_explicit_reference_combinations(text) == [
        ["char_a", "scene_a", "prop_a"],
        ["char_b", "scene_b"],
    ]


def test_reference_regenerate_reuses_historical_combinations_without_render(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Seedream must not be called when exact historical combo URLs exist")

    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedream",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    instruction = """
- combo__char_a__scene_a__prop_a
- combo__char_b__scene_b
"""
    context = _context(instruction)
    context["reference_candidate_history"] = [
        {
            "id": "old-combo-1", "stage": "reference_images", "artifact_revision": 3,
            "canonical_key": "combo__char_a__scene_a__prop_a",
            "url": "/media/old-1.png", "remote_url": "https://files.example/old-1.png",
            "model": "seedream", "prompt": "old prompt 1", "selected": True,
        },
        {
            "id": "old-combo-2", "stage": "reference_images", "artifact_revision": 3,
            "canonical_key": "combo__char_b__scene_b",
            "url": "/media/old-2.png", "remote_url": "https://files.example/old-2.png",
            "model": "seedream", "prompt": "old prompt 2", "selected": False,
        },
    ]

    result = provider.generate("reference_images", context)
    combos = [x for x in result["items"] if x["source_kind"] == "combination"]
    assert len(combos) == 2
    assert [x["url"] for x in combos] == ["/media/old-1.png", "/media/old-2.png"]
    assert combos[0]["input_candidate_ids"] == ["ca", "sa", "pa"]
    assert combos[1]["input_candidate_ids"] == ["cb", "sb"]
    assert result["reference_validation"]["combination_expected"] == 2
    assert result["reference_validation"]["combination_completed"] == 2
    assert result["reference_validation"]["status"] == "pass"
    assert result["reference_plan"]["generation_calls_used"] == 0


def test_reference_regenerate_generates_only_missing_combination(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json
            posted.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"data": [{"url": "https://files.example/new-combo.png"}]})
        if str(request.url) == "https://files.example/new-combo.png":
            return httpx.Response(200, content=b"new-combo")
        return httpx.Response(404)

    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedream",
        output_dir=tmp_path,
        max_assets=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    instruction = """
- combo__char_a__scene_a__prop_a
- combo__char_b__scene_b
"""
    context = _context(instruction)
    context["reference_candidate_history"] = [
        {
            "id": "old-combo-1", "stage": "reference_images", "artifact_revision": 3,
            "canonical_key": "combo__char_a__scene_a__prop_a",
            "url": "/media/old-1.png", "remote_url": "https://files.example/old-1.png",
            "model": "seedream", "selected": False,
        },
    ]

    result = provider.generate("reference_images", context)
    combos = [x for x in result["items"] if x["source_kind"] == "combination"]
    assert len(combos) == 2
    assert combos[0]["url"] == "/media/old-1.png"
    assert combos[1]["url"].endswith("/combination-combo__char_b__scene_b.png")
    assert len(posted) == 1
    assert posted[0]["image"] == [
        "https://files.example/char_b.png",
        "https://files.example/scene_b.png",
    ]
    assert result["reference_validation"]["status"] == "pass"
    assert result["reference_plan"]["generation_calls_used"] == 1
