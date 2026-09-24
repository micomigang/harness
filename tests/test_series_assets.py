from pathlib import Path

from app.db import Database
from app.orchestrator import Orchestrator
from app.providers.mock import MockProvider


class MockRegistry:
    def workflow(self):
        return MockProvider()


def test_series_creates_auto_managed_character_scene_prop_libraries(tmp_path: Path):
    db = Database(tmp_path / "series.db")
    series = db.create_series("Mamie 归家", "France contemporary family")
    ws = db.create_workspace(
        "Mamie 归家 · EP01",
        "brief",
        {"series_id": series["id"], "episode_key": "EP01"},
    )
    libraries = db.attach_series_libraries(ws["id"], series["id"])
    assert {item["scope"] for item in libraries} == {"character", "scene", "prop"}
    assert all(item["auto_managed"] == 1 for item in libraries)
    assert len(db.list_workspace_asset_libraries(ws["id"])) == 3


def test_asset_manifest_is_next_after_script_approval(tmp_path: Path):
    db = Database(tmp_path / "manifest.db")
    orch = Orchestrator(db, MockRegistry())
    ws = db.create_workspace("EP01", "brief", {})
    orch.bootstrap_workspace(ws["id"])
    orch.run(ws["id"], "analysis")
    orch.run(ws["id"], "script")
    db.set_approval(ws["id"], "script_approved", "approved")
    action = orch.next_actions(ws["id"])[0]
    assert action["type"] == "run"
    assert action["stage"] == "asset_manifest"


def test_approved_episode_assets_are_published_and_reused_by_next_episode(tmp_path: Path):
    db = Database(tmp_path / "publish.db")
    orch = Orchestrator(db, MockRegistry())
    series = db.create_series("Mamie 归家", "France contemporary family")
    ws1 = db.create_workspace(
        "Mamie 归家 · EP01",
        "brief",
        {"series_id": series["id"], "episode_key": "EP01"},
    )
    db.attach_series_libraries(ws1["id"], series["id"])
    orch.bootstrap_workspace(ws1["id"])
    db.upsert_artifact(
        ws1["id"],
        "characters",
        "角色",
        {
            "items": [
                {
                    "id": "char_mamie_ep1",
                    "name": "Mamie",
                    "canonical_key": "CHAR_MAMIE",
                    "reuse_decision": "CREATE",
                    "appearance": "elderly woman, stable face",
                    "continuity": "same face across episodes",
                    "continuity_lock": ["face", "age"],
                }
            ]
        },
        "mock",
    )
    result = orch.publish_series_assets(ws1["id"])
    assert result["published"] == 1

    ws2 = db.create_workspace(
        "Mamie 归家 · EP02",
        "brief2",
        {"series_id": series["id"], "episode_key": "EP02"},
    )
    db.attach_series_libraries(ws2["id"], series["id"])
    context = db.workspace_asset_context(ws2["id"])
    character_items = [
        item
        for library in context
        if library["scope"] == "character"
        for item in library["items"]
    ]
    assert any(item["canonical_key"] == "CHAR_MAMIE" for item in character_items)
    assert any("same face" in str(item["content"]) for item in character_items)


def test_variant_does_not_overwrite_series_canonical_asset(tmp_path: Path):
    db = Database(tmp_path / "variant.db")
    orch = Orchestrator(db, MockRegistry())
    series = db.create_series("Mamie 归家", "France family")
    ws1 = db.create_workspace("EP01", "brief", {"series_id": series["id"], "episode_key": "EP01"})
    libs = db.attach_series_libraries(ws1["id"], series["id"])
    char_lib = next(item for item in libs if item["scope"] == "character")
    base = db.add_asset_item(
        char_lib["id"], "character", "Mamie", "canonical identity",
        {"appearance": "base face"}, canonical_key="CHAR_MAMIE", retrieval_text="Mamie base face"
    )
    db.upsert_artifact(
        ws1["id"], "characters", "角色",
        {"items": [{
            "id": "char_mamie_dinner", "name": "Mamie dinner",
            "canonical_key": "CHAR_MAMIE", "library_asset_id": base["id"],
            "reuse_decision": "VARIANT", "variant_key": "EP01_DINNER",
            "appearance": "same face", "wardrobe": "formal dinner outfit",
        }]}, "mock"
    )
    result = orch.publish_series_assets(ws1["id"])
    assert result["variants"] == 1
    canonical = db.find_asset_by_canonical_key(char_lib["id"], "CHAR_MAMIE")
    variant = db.find_asset_variant(char_lib["id"], "CHAR_MAMIE", "EP01_DINNER")
    assert canonical["id"] == base["id"]
    assert canonical["content"]["appearance"] == "base face"
    assert variant["parent_item_id"] == base["id"]
    assert variant["content"]["wardrobe"] == "formal dinner outfit"


def test_invalid_manifest_library_reference_is_downgraded_to_create():
    content = {
        "items": [
            {
                "manifest_id": "CHAR_001",
                "asset_type": "character",
                "canonical_key": "CHAR_MAMIE",
                "decision": "REUSE",
                "library_asset_id": "invented-id",
            }
        ]
    }
    normalized = Orchestrator._normalize_asset_manifest(content, [])
    item = normalized["items"][0]
    assert item["decision"] == "CREATE"
    assert item["library_asset_id"] is None
    assert normalized["create_count"] == 1


def test_asset_agent_output_is_bound_back_to_manifest_decision():
    artifacts = [
        {
            "kind": "asset_manifest",
            "content": {
                "items": [
                    {
                        "manifest_id": "CHAR_001",
                        "asset_type": "character",
                        "canonical_key": "CHAR_MAMIE",
                        "decision": "REUSE",
                        "library_asset_id": "asset-1",
                        "continuity_lock": ["face", "age"],
                    }
                ]
            },
        }
    ]
    content = {
        "items": [
            {
                "manifest_id": "CHAR_001",
                "canonical_key": "CHAR_MAMIE",
                "id": "char-mamie-episode",
                "name": "Mamie",
                "reuse_decision": "CREATE",
                "library_asset_id": "hallucinated",
            }
        ]
    }
    enforced = Orchestrator._enforce_asset_manifest("characters", content, artifacts)
    item = enforced["items"][0]
    assert item["reuse_decision"] == "REUSE"
    assert item["library_asset_id"] == "asset-1"
    assert item["continuity_lock"] == ["face", "age"]
    assert enforced["manifest_enforced"] is True


def test_asset_manifest_reconciliation_restores_missing_script_requirements():
    script = {
        "asset_requirements": [
            {"canonical_id": "CHAR_MAMIE", "type": "character", "canonical_name": "Mamie", "source_function": "protagonist", "continuity_lock": "same identity"},
            {"canonical_id": "LOC_HALL", "type": "location", "canonical_name": "Hall", "source_function": "class contrast"},
            {"canonical_id": "PROP_BAG", "type": "prop", "canonical_name": "Travel bag", "source_function": "cliffhanger", "continuity_lock": "same bag across episodes"},
        ]
    }
    # Model accidentally drops the prop.
    content = {
        "items": [
            {"manifest_id": "CHAR_001", "asset_type": "character", "canonical_key": "CHAR_MAMIE", "decision": "CREATE"},
            {"manifest_id": "LOC_001", "asset_type": "scene", "canonical_key": "LOC_HALL", "decision": "CREATE"},
        ]
    }
    normalized = Orchestrator._normalize_asset_manifest(content, [], script_content=script)
    keys = {item["canonical_key"] for item in normalized["items"]}
    assert keys == {"char_mamie", "scene_hall", "prop_bag"}
    bag = next(item for item in normalized["items"] if item["canonical_key"] == "prop_bag")
    assert bag["source_requirement_keys"] == ["PROP_BAG"]
    assert bag["decision"] == "CREATE"
    assert bag["continuity_lock"] == ["same bag across episodes"]
    assert normalized["manifest_validation"]["status"] == "pass"
    assert normalized["manifest_validation"]["missing_required_keys"] == []


def test_asset_manifest_regeneration_is_monotonic_and_preserves_prior_extra_props():
    script = {
        "asset_requirements": [
            {"canonical_id": "CHAR_MAMIE", "type": "character", "canonical_name": "Mamie"},
            {"canonical_id": "PROP_BAG", "type": "prop", "canonical_name": "Travel bag"},
        ]
    }
    previous = {
        "items": [
            {"manifest_id": "CHAR_MAMIE", "asset_type": "character", "canonical_key": "CHAR_MAMIE", "decision": "CREATE"},
            {"manifest_id": "PROP_BAG", "asset_type": "prop", "canonical_key": "PROP_BAG", "decision": "CREATE", "continuity_lock": ["identity"]},
            {"manifest_id": "PROP_TEACUP", "asset_type": "prop", "canonical_key": "PROP_TEACUP", "decision": "CREATE"},
        ]
    }
    # A bad regeneration returns only the character plus one newly requested prop.
    regenerated = {
        "items": [
            {"manifest_id": "CHAR_MAMIE", "asset_type": "character", "canonical_key": "CHAR_MAMIE", "decision": "CREATE"},
            {"manifest_id": "PROP_CHANDELIER", "asset_type": "prop", "canonical_key": "PROP_CHANDELIER", "decision": "CREATE"},
        ]
    }
    normalized = Orchestrator._normalize_asset_manifest(
        regenerated, [], script_content=script, previous_manifest=previous
    )
    keys = {item["canonical_key"] for item in normalized["items"]}
    assert {"char_mamie", "prop_bag", "prop_teacup", "prop_chandelier"}.issubset(keys)
    assert normalized["manifest_validation"]["previous_revision_items_preserved"] is True
    assert normalized["manifest_validation"]["status"] == "pass"


def test_asset_manifest_drops_standalone_costume_type_but_keeps_script_character():
    script = {
        "asset_requirements": [
            {"canonical_id": "CHAR_MAMIE", "type": "character", "canonical_name": "Mamie", "continuity_lock": "red coat"},
        ]
    }
    content = {
        "items": [
            {"manifest_id": "COSTUME_001", "asset_type": "costume", "canonical_key": "MAMIE_RED_COAT", "decision": "CREATE"}
        ]
    }
    normalized = Orchestrator._normalize_asset_manifest(content, [], script_content=script)
    assert [item["canonical_key"] for item in normalized["items"]] == ["char_mamie"]
    assert normalized["items"][0]["source_requirement_keys"] == ["CHAR_MAMIE"]
    assert normalized["items"][0]["asset_type"] == "character"


def test_asset_manifest_reconciles_legacy_ep02_aliases_without_duplicate_assets():
    script = {
        "asset_requirements": [
            {"canonical_id": "CHAR_GRANDMERE", "type": "character", "canonical_name": "La dame âgée en manteau rouge", "continuity_lock": "red coat identity"},
            {"canonical_id": "CHAR_SON", "type": "character", "canonical_name": "L'homme en costume", "continuity_lock": "dark suit"},
            {"canonical_id": "CHAR_GREENELDER", "type": "character", "canonical_name": "La dame en robe verte", "continuity_lock": "green dress"},
            {"canonical_id": "CHAR_YOUNGWOMAN", "type": "character", "canonical_name": "La jeune femme en beige", "continuity_lock": "beige wardrobe"},
            {"canonical_id": "CHAR_BOY", "type": "character", "canonical_name": "Le garçonnet en costume", "continuity_lock": "black suit"},
            {"canonical_id": "LOC_FOYER", "type": "location", "canonical_name": "Vestibule / Hall d'entrée", "continuity_lock": "marble stair"},
            {"canonical_id": "LOC_SALON", "type": "location", "canonical_name": "Salon principal", "continuity_lock": "salon layout"},
            {"canonical_id": "LOC_COULOIR_ETAGE", "type": "location", "canonical_name": "Couloir de l'étage", "continuity_lock": "long perspective"},
            {"canonical_id": "LOC_SERVANT_ROOM", "type": "location", "canonical_name": "Chambre de service", "continuity_lock": "grey bedding room"},
            {"canonical_id": "PROP_BAG", "type": "prop", "canonical_name": "Grand sac de voyage en toile", "continuity_lock": "same green/grey travel bag across the episode"},
            {"canonical_id": "PROP_TEACUP", "type": "prop", "canonical_name": "Tasse et soucoupe", "continuity_lock": "same cup and saucer"},
        ]
    }
    previous = {
        "items": [
            {"asset_type": "costume", "canonical_key": "protagonist_red_quilted_coat", "script_identity": "La dame âgée en manteau rouge", "continuity_lock": True},
            {"asset_type": "costume", "canonical_key": "son_dark_suit", "script_identity": "L’homme en costume", "continuity_lock": False},
            {"asset_type": "costume", "canonical_key": "green_elder_green_dress", "script_identity": "La dame en robe verte", "continuity_lock": False},
            {"asset_type": "costume", "canonical_key": "young_woman_beige_dress", "script_identity": "La jeune femme en beige", "continuity_lock": False},
            {"asset_type": "costume", "canonical_key": "grandson_black_suit", "script_identity": "Le garçonnet en costume", "continuity_lock": True},
            {"asset_type": "set", "canonical_key": "set_vestibule_bourgeois", "script_identity": "Vestibule / Hall d’entrée", "continuity_lock": False},
            {"asset_type": "set", "canonical_key": "set_ground_floor_corridor", "script_identity": "Couloir du rez-de-chaussée", "continuity_lock": False},
            {"asset_type": "set", "canonical_key": "set_salon_principal", "script_identity": "Salon principal", "continuity_lock": False},
            {"asset_type": "set", "canonical_key": "set_upstairs_corridor", "script_identity": "Couloir de l’étage", "continuity_lock": False},
            {"asset_type": "set", "canonical_key": "set_servant_room", "script_identity": "Chambre de service", "continuity_lock": False},
            {"asset_type": "prop", "canonical_key": "prop_mysterious_travel_bag", "script_identity": "Grand sac de voyage en toile", "continuity_lock": True},
            {"asset_type": "prop", "canonical_key": "prop_teacup_saucer", "script_identity": "Tasse et soucoupe", "continuity_lock": False},
            {"asset_type": "prop", "canonical_key": "prop_crystal_chandelier", "script_identity": "Lustre en cristal", "continuity_lock": False},
            {"asset_type": "prop", "canonical_key": "prop_servant_bedding_grey", "script_identity": "Literie grise de la chambre de service", "continuity_lock": False},
        ]
    }

    normalized = Orchestrator._normalize_asset_manifest(
        {"items": []}, [], script_content=script, previous_manifest=previous
    )

    assert len(normalized["items"]) == 14
    assert normalized["manifest_validation"]["type_counts"] == {
        "character": 5,
        "scene": 5,
        "prop": 4,
    }
    keys = {item["canonical_key"] for item in normalized["items"]}
    assert {
        "char_grandmere", "char_son", "char_greenelder", "char_youngwoman", "char_boy",
        "scene_foyer", "scene_salon", "scene_couloir_etage", "scene_servant_room",
        "prop_mysterious_travel_bag", "prop_teacup_saucer",
        "scene_ground_floor_corridor", "prop_crystal_chandelier", "prop_servant_bedding_grey",
    } == keys
    assert not any(item["asset_type"] == "costume" for item in normalized["items"])
    bag = next(item for item in normalized["items"] if item["canonical_key"] == "prop_mysterious_travel_bag")
    assert bag["source_requirement_keys"] == ["PROP_BAG"]
    assert bag["continuity_lock"] == ["same green/grey travel bag across the episode"]
    chandelier = next(item for item in normalized["items"] if item["canonical_key"] == "prop_crystal_chandelier")
    assert chandelier["continuity_lock"] == []
    validation = normalized["manifest_validation"]
    assert validation["semantic_alias_match_count"] == 9
    assert validation["required_baseline_coverage"]["missing_source_requirement_keys"] == []
    assert validation["duplicate_canonical_semantic_check"]["status"] == "pass"
    assert validation["illegal_asset_type_check"]["status"] == "pass"
    assert validation["continuity_lock_check"]["encoding"] == "array_of_rules"
    assert validation["asset_type_counts"] == {"character": 5, "scene": 5, "prop": 4}
    assert validation["status"] == "pass"


def test_asset_manifest_duplicate_script_requirement_is_one_canonical_asset():
    script = {
        "asset_requirements": [
            {
                "canonical_id": "PROP_BAG",
                "type": "prop",
                "canonical_name": "Grand sac de voyage en toile",
                "continuity_lock": "same bag",
                "used_in": ["S01"],
            },
            {
                "canonical_id": "PROP_BAG",
                "type": "prop",
                "canonical_name": "Grand sac de voyage en toile",
                "continuity_lock": "green/grey canvas",
                "used_in": ["T04"],
            },
        ]
    }
    normalized = Orchestrator._normalize_asset_manifest(
        {"items": []}, [], script_content=script
    )
    assert len(normalized["items"]) == 1
    item = normalized["items"][0]
    assert item["canonical_key"] == "prop_bag"
    assert item["source_requirement_keys"] == ["PROP_BAG"]
    assert item["continuity_lock"] == ["same bag", "green/grey canvas"]
    validation = normalized["manifest_validation"]
    assert validation["required_count"] == 1
    assert validation["duplicate_script_requirement_keys"] == ["PROP_BAG"]
    assert validation["duplicate_canonical_keys"] == []
    assert validation["continuity_lock_violations"] == []
    assert validation["count_policy"] == "evidence_derived_not_quota"
    assert validation["status"] == "pass"



def test_asset_manifest_keeps_script_requirement_binding_separate_from_manifest_identity():
    script = {
        "asset_requirements": [
            {"canonical_id": "CHAR_GRANDMERE", "type": "character", "canonical_name": "La dame âgée en manteau rouge", "continuity_lock": "same identity"},
            {"canonical_id": "LOC_FOYER", "type": "location", "canonical_name": "Vestibule / Hall d'entrée"},
            {"canonical_id": "PROP_BAG", "type": "prop", "canonical_name": "Grand sac de voyage en toile", "continuity_lock": "same bag"},
        ]
    }
    generated = {
        "items": [
            {"manifest_id": "mamie-ep02-character-char_grandmere", "asset_type": "character", "canonical_key": "char_grandmere", "script_identity": "La dame âgée en manteau rouge", "appearance": "manteau rouge", "costume": "manteau rouge", "continuity_lock": ["same identity"], "decision": "CREATE"},
            {"manifest_id": "mamie-ep02-scene-scene_foyer", "asset_type": "scene", "canonical_key": "scene_foyer", "script_identity": "Vestibule / Hall d'entrée", "key_set_elements": ["escalier de marbre"], "decision": "CREATE"},
            {"manifest_id": "mamie-ep02-prop-prop_mysterious_travel_bag", "asset_type": "prop", "canonical_key": "prop_mysterious_travel_bag", "script_identity": "Grand sac de voyage en toile", "physical_description": "toile verte/grise", "used_in_scenes": ["S01"], "continuity_lock": ["same bag"], "decision": "CREATE"},
        ]
    }
    normalized = Orchestrator._normalize_asset_manifest(generated, [], script_content=script)
    by_key = {item["canonical_key"]: item for item in normalized["items"]}
    assert set(by_key) == {"char_grandmere", "scene_foyer", "prop_mysterious_travel_bag"}
    assert by_key["char_grandmere"]["source_requirement_keys"] == ["CHAR_GRANDMERE"]
    assert by_key["scene_foyer"]["source_requirement_keys"] == ["LOC_FOYER"]
    assert by_key["prop_mysterious_travel_bag"]["source_requirement_keys"] == ["PROP_BAG"]
    assert normalized["manifest_validation"]["missing_required_keys"] == []
    assert normalized["manifest_validation"]["status"] == "pass"


def test_asset_manifest_current_generated_extra_key_migrates_legacy_extra_alias():
    previous = {
        "items": [
            {"manifest_id": "mamie-ep02-set-ground-floor-corridor", "asset_type": "set", "canonical_key": "set_ground_floor_corridor", "script_identity": "Couloir du rez-de-chaussée", "generation_requirements": "couloir étroit", "decision": "CREATE"},
        ]
    }
    generated = {
        "items": [
            {"manifest_id": "mamie-ep02-scene-scene_couloir_rdc", "asset_type": "scene", "canonical_key": "scene_couloir_rdc", "script_identity": "Couloir du rez-de-chaussée", "key_set_elements": ["couloir étroit", "éclairage froid"], "decision": "CREATE"},
        ]
    }
    normalized = Orchestrator._normalize_asset_manifest(
        generated, [], script_content={"asset_requirements": []}, previous_manifest=previous
    )
    assert len(normalized["items"]) == 1
    item = normalized["items"][0]
    assert item["canonical_key"] == "scene_couloir_rdc"
    assert item["manifest_id"] == "mamie-ep02-scene-scene_couloir_rdc"
    assert item["key_set_elements"] == ["couloir étroit", "éclairage froid"]
    assert normalized["manifest_validation"]["duplicate_semantic_assets"] == []


def test_asset_manifest_typed_metadata_is_explicit_and_boolean_locks_are_normalized():
    script = {
        "asset_requirements": [
            {"canonical_id": "CHAR_A", "type": "character", "canonical_name": "A", "continuity_lock": "same face"},
            {"canonical_id": "LOC_A", "type": "location", "canonical_name": "Room A"},
            {"canonical_id": "PROP_A", "type": "prop", "canonical_name": "Bag A", "continuity_lock": "same bag"},
        ]
    }
    generated = {
        "items": [
            {"asset_type": "character", "canonical_key": "char_a", "script_identity": "A", "continuity_lock": True},
            {"asset_type": "scene", "canonical_key": "scene_a", "script_identity": "Room A", "continuity_lock": False},
            {"asset_type": "prop", "canonical_key": "prop_a", "script_identity": "Bag A", "continuity_lock": True},
        ]
    }
    normalized = Orchestrator._normalize_asset_manifest(generated, [], script_content=script)
    by_type = {item["asset_type"]: item for item in normalized["items"]}
    assert "appearance" in by_type["character"] and "costume" in by_type["character"]
    assert "key_set_elements" in by_type["scene"]
    assert "physical_description" in by_type["prop"] and "used_in_scenes" in by_type["prop"]
    assert by_type["character"]["continuity_lock"] == ["same face"]
    assert by_type["prop"]["continuity_lock"] == ["same bag"]
    assert normalized["manifest_validation"]["typed_metadata_check"]["status"] == "pass"
    assert normalized["manifest_validation"]["continuity_lock_check"]["status"] == "pass"

def test_asset_manifest_director_guard_neutralizes_historical_count_and_legacy_quota():
    artifacts = [
        {
            "kind": "script",
            "content": {
                "asset_requirements": [
                    {"canonical_id": "CHAR_GRANDMERE", "type": "character"},
                    {"canonical_id": "LOC_FOYER", "type": "location"},
                    {"canonical_id": "PROP_BAG", "type": "prop"},
                ]
            },
        }
    ]
    plan = {
        "reply": "需要严格补到 14 项后再通过。",
        "effective_instruction": (
            "保留现有 12 项资产（5 costume、5 set、2 prop）。\n"
            "新增水晶吊灯并保留旅行袋。\n"
            "最终 items[] 数量须严格等于 14。"
        ),
        "interpretation": "revision 2 必须严格等于 14 项，并保留 5 costume、5 set。",
        "prompt_addendum": (
            "保留 12 项旧资产。\n"
            "新增 prop_crystal_chandelier。\n"
            "总数必须等于 14 项。"
        ),
        "acceptance_criteria": [
            "items[] total must equal exactly 14",
            "prop_crystal_chandelier must be present",
        ],
        "warnings": [],
    }
    guarded = Orchestrator._guard_asset_manifest_directive(plan, artifacts)
    assert "最终 items[] 数量须严格等于 14" not in guarded["effective_instruction"]
    assert "5 costume" not in guarded["effective_instruction"]
    assert "新增水晶吊灯并保留旅行袋" in guarded["effective_instruction"]
    assert "prop_crystal_chandelier" in guarded["prompt_addendum"]
    assert "evidence_derived_not_quota" in guarded["prompt_addendum"]
    assert guarded["asset_manifest_contract"]["required_count"] == 3
    assert guarded["asset_manifest_contract"]["required_type_counts"] == {
        "character": 1,
        "scene": 1,
        "prop": 1,
    }
    assert guarded["asset_manifest_contract"]["continuity_lock_encoding"] == "array_of_rules"
    assert "source_requirement_keys" in guarded["effective_instruction"]
    assert any("source_requirement_keys" in criterion for criterion in guarded["acceptance_criteria"])
    assert all("14" not in criterion for criterion in guarded["acceptance_criteria"])


def test_asset_manifest_contract_declares_closed_typed_metadata_schema():
    artifacts = [
        {
            "kind": "script",
            "content": {
                "asset_requirements": [
                    {"canonical_id": "CHAR_A", "type": "character"},
                    {"canonical_id": "LOC_A", "type": "location"},
                    {"canonical_id": "PROP_A", "type": "prop"},
                ]
            },
        }
    ]
    contract = Orchestrator._asset_manifest_contract(artifacts)
    assert contract["typed_metadata_required_fields"] == {
        "character": ["appearance", "costume"],
        "scene": ["key_set_elements"],
        "prop": ["physical_description", "used_in_scenes"],
    }
    assert contract["typed_metadata_policy"].startswith("closed_required_set")


def test_asset_manifest_review_guard_suppresses_invented_schema_fields_when_validation_passes():
    artifact = {
        "content": {
            "manifest_validation": {
                "status": "pass",
                "typed_metadata_check": {
                    "status": "pass",
                    "required_fields": {
                        "character": ["appearance", "costume"],
                        "scene": ["key_set_elements"],
                        "prop": ["physical_description", "used_in_scenes"],
                    },
                },
            }
        }
    }
    review = {
        "reply": "typed metadata 缺失，需要重生成",
        "assessment": "schema 不合规",
        "deviations": [
            "character 缺 physical_tags/social_register；scene 缺 lighting_mood/spatial_function/architectural_style；prop 缺 material/dimensions_hint/narrative_function。",
            "manifest_validation 错误地把 typed_metadata_violations 判为空。",
        ],
        "suggested_adjustments": ["补齐上述字段"],
        "recommended_action": "regenerate_current",
    }
    directive = {
        "asset_manifest_contract": {
            "typed_metadata_required_fields": {
                "character": ["appearance", "costume"],
                "scene": ["key_set_elements"],
                "prop": ["physical_description", "used_in_scenes"],
            }
        }
    }
    guarded = Orchestrator._guard_asset_manifest_review(review, artifact, directive)
    assert guarded["deviations"] == []
    assert guarded["recommended_action"] == "proceed"
    assert guarded["harness_review_guard"]["suppressed_structural_deviations"]
    assert guarded["harness_review_guard"]["typed_metadata_required_fields"]["prop"] == [
        "physical_description",
        "used_in_scenes",
    ]


def test_asset_manifest_review_guard_keeps_semantic_content_deviation():
    artifact = {"content": {"manifest_validation": {"status": "pass"}}}
    review = {
        "deviations": ["用户明确要求保留水晶吊灯，但当前 items 中没有该道具。"],
        "recommended_action": "regenerate_current",
    }
    guarded = Orchestrator._guard_asset_manifest_review(review, artifact, {})
    assert guarded["deviations"] == review["deviations"]
    assert guarded["recommended_action"] == "regenerate_current"


def test_localized_character_keeps_source_and_production_continuity_separate():
    artifacts = [{
        "kind": "asset_manifest",
        "content": {"items": [{
            "manifest_id": "mamie-ep02-character-char_grandmere",
            "asset_type": "character",
            "canonical_key": "char_grandmere",
            "decision": "CREATE",
            "continuity_lock": ["Manteau rouge matelassé à motifs floraux", "same travel bag"],
        }]},
    }]
    content = {"items": [{
        "manifest_id": "mamie-ep02-character-char_grandmere",
        "canonical_key": "char_grandmere",
        "id": "char_grandmere",
        "name": "Grand-mère",
        "source_continuity_lock": [
            "Chinese red floral padded coat with mandarin collar and frog buttons",
        ],
        "continuity_lock": [
            "Deep burgundy quilted French countryside jacket with restrained near-solid textile treatment",
            "No mandarin collar or East Asian fastening details",
            "Same large green-grey travel bag",
        ],
        "continuity": [
            "Manteau rouge matelassé à motifs floraux",
            "Performance: slightly stooped to upright transition",
        ],
        "localized_visual_design": {
            "outerwear": "Deep burgundy quilted French countryside jacket",
        },
        "generation_prompt_en": "French rural elderly woman in a deep burgundy quilted countryside jacket.",
    }]}

    enforced = Orchestrator._enforce_asset_manifest(
        "characters", content, artifacts,
        workspace={"settings": {"target_market": "France", "target_language": "fr-FR"}},
    )
    item = enforced["items"][0]
    assert item["source_continuity_lock"] == [
        "Chinese red floral padded coat with mandarin collar and frog buttons"
    ]
    assert item["continuity_lock"] == [
        "Deep burgundy quilted French countryside jacket with restrained near-solid textile treatment",
        "No mandarin collar or East Asian fastening details",
        "Same large green-grey travel bag",
    ]
    assert item["production_continuity_lock"] == item["continuity_lock"]
    assert item["continuity"] == item["continuity_lock"]
    assert enforced["continuity_localization_validation"]["status"] == "pass"


def test_localized_asset_never_falls_back_to_source_lock_when_production_lock_missing():
    artifacts = [{
        "kind": "asset_manifest",
        "content": {"items": [{
            "manifest_id": "CHAR_001",
            "asset_type": "character",
            "canonical_key": "char_mamie",
            "decision": "CREATE",
            "continuity_lock": ["source floral coat"],
        }]},
    }]
    content = {"items": [{
        "manifest_id": "CHAR_001",
        "canonical_key": "char_mamie",
        "id": "char_mamie",
        "source_continuity_lock": ["source floral coat"],
        "localized_visual_design": {"wardrobe": "French countryside coat"},
        "generation_prompt_en": "French countryside elderly woman",
    }]}
    enforced = Orchestrator._enforce_asset_manifest("characters", content, artifacts)
    item = enforced["items"][0]
    assert item["source_continuity_lock"] == ["source floral coat"]
    assert item["continuity_lock"] == []
    assert item["production_continuity_lock"] == []
    assert enforced["continuity_localization_validation"]["status"] == "fail"
    assert enforced["continuity_localization_validation"]["issues"] == [{
        "canonical_key": "char_mamie",
        "issue": "missing_production_continuity_lock",
    }]
