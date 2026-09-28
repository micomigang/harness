from pathlib import Path

from app.db import Database
from app.agents import AGENT_SPECS, STAGE_AGENTS
from app.orchestrator import STAGES, OrchestrationError, Orchestrator
from app.providers.mock import MockProvider


class MockRegistry:
    def workflow(self):
        return MockProvider()


def build(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    orchestrator = Orchestrator(db, MockRegistry())
    workspace = db.create_workspace(
        "Episode 01", "A modern French family drama", {"aspect_ratio": "9:16"}
    )
    orchestrator.bootstrap_workspace(workspace["id"])
    return db, orchestrator, workspace["id"]


def test_full_mock_pipeline(tmp_path: Path):
    db, orchestrator, workspace_id = build(tmp_path)
    orchestrator.run(workspace_id, "analysis")
    orchestrator.run(workspace_id, "script")
    db.set_approval(workspace_id, "script_approved", "approved")
    orchestrator.run(workspace_id, "asset_manifest")
    for stage in ["characters", "scenes", "props", "reference_images"]:
        orchestrator.run(workspace_id, stage)
    db.set_approval(workspace_id, "assets_approved", "approved")
    orchestrator.run(workspace_id, "storyboard")
    orchestrator.run(workspace_id, "dialogue_plan")
    orchestrator.run(workspace_id, "sound_plan")
    orchestrator.run(workspace_id, "review")
    db.set_approval(workspace_id, "storyboard_approved", "approved")
    orchestrator.run(workspace_id, "preview")
    db.set_approval(workspace_id, "preview_approved", "approved")
    for stage in ["batch_video", "music_plan", "music", "compose", "delivery_qa"]:
        orchestrator.run(workspace_id, stage)

    artifacts = {item["kind"]: item for item in db.list_artifacts(workspace_id)}
    assert artifacts["delivery_qa"]["content"]["status"] == "pass"
    assert len(artifacts["storyboard"]["content"]["shots"]) == 8
    assert artifacts["storyboard"]["content"]["_agent"]["id"] == "storyboard_director"


def test_revision_cascade_marks_downstream_stale(tmp_path: Path):
    db, orchestrator, workspace_id = build(tmp_path)
    orchestrator.run(workspace_id, "analysis")
    script = orchestrator.run(workspace_id, "script")
    db.set_approval(workspace_id, "script_approved", "approved")
    orchestrator.run(workspace_id, "asset_manifest")
    for stage in ["characters", "scenes", "props", "reference_images"]:
        orchestrator.run(workspace_id, stage)
    db.set_approval(workspace_id, "assets_approved", "approved")
    orchestrator.run(workspace_id, "storyboard")

    result = orchestrator.revise(workspace_id, "script", script["content"], cascade=True)
    assert set(result["invalidated"]) == {
        "asset_manifest",
        "characters",
        "scenes",
        "props",
        "reference_images",
        "storyboard",
    }
    approvals = {x["gate"]: x["status"] for x in db.list_approvals(workspace_id)}
    assert approvals["script_approved"] == "pending"


def test_every_stage_has_exactly_one_bounded_agent_contract():
    claimed = [stage for agent in AGENT_SPECS for stage in agent.stages]
    assert set(claimed) == set(STAGES)
    assert len(claimed) == len(set(claimed))
    assert set(STAGE_AGENTS) == set(STAGES)
    for agent in AGENT_SPECS:
        assert agent.purpose
        assert agent.owns
        assert agent.must_not
        assert agent.debug_checks


def test_preview_is_blocked_when_continuity_qa_has_failures(tmp_path: Path):
    db, orchestrator, workspace_id = build(tmp_path)
    orchestrator.run(workspace_id, "analysis")
    orchestrator.run(workspace_id, "script")
    db.set_approval(workspace_id, "script_approved", "approved")
    orchestrator.run(workspace_id, "asset_manifest")
    for stage in ["characters", "scenes", "props", "reference_images"]:
        orchestrator.run(workspace_id, stage)
    db.set_approval(workspace_id, "assets_approved", "approved")
    for stage in ["storyboard", "dialogue_plan", "sound_plan", "review"]:
        orchestrator.run(workspace_id, stage)
    review = db.get_artifact(workspace_id, "review")
    assert review
    failed = dict(review["content"])
    failed["blocking_failures"] = ["shot_3_missing_speaker"]
    orchestrator.revise(workspace_id, "review", failed, cascade=False)
    db.set_approval(workspace_id, "storyboard_approved", "approved")
    try:
        orchestrator.run(workspace_id, "preview")
    except OrchestrationError as exc:
        assert "blocking failures" in str(exc)
    else:
        raise AssertionError("Preview must not run when QA has blocking failures")


def test_reset_script_removes_script_and_downstream_but_preserves_analysis_and_source(tmp_path: Path):
    db, orchestrator, workspace_id = build(tmp_path)
    orchestrator.run(workspace_id, "analysis")
    orchestrator.run(workspace_id, "script")
    db.set_approval(workspace_id, "script_approved", "approved")
    orchestrator.run(workspace_id, "asset_manifest")
    for stage in ["characters", "scenes", "props", "reference_images"]:
        orchestrator.run(workspace_id, stage)

    result = orchestrator.reset_stage(workspace_id, "script")
    artifacts = {item["kind"] for item in db.list_artifacts(workspace_id)}
    assert "source" in artifacts
    assert "analysis" in artifacts
    assert "script" not in artifacts
    assert "asset_manifest" not in artifacts
    assert "characters" not in artifacts
    assert "scenes" not in artifacts
    assert "props" not in artifacts
    assert "reference_images" not in artifacts
    assert result["affected"][0] == "script"
    approvals = {x["gate"]: x["status"] for x in db.list_approvals(workspace_id)}
    assert approvals["script_approved"] == "pending"


def test_reset_analysis_preserves_source_and_clears_all_generated_descendants(tmp_path: Path):
    db, orchestrator, workspace_id = build(tmp_path)
    orchestrator.run(workspace_id, "analysis")
    orchestrator.run(workspace_id, "script")

    result = orchestrator.reset_stage(workspace_id, "analysis")
    artifacts = {item["kind"] for item in db.list_artifacts(workspace_id)}
    assert artifacts == {"source"}
    assert result["affected"][0] == "analysis"
    assert "script" in result["affected"]


def test_reset_character_branch_keeps_sibling_scene_and_prop_artifacts(tmp_path: Path):
    db, orchestrator, workspace_id = build(tmp_path)
    orchestrator.run(workspace_id, "analysis")
    orchestrator.run(workspace_id, "script")
    db.set_approval(workspace_id, "script_approved", "approved")
    orchestrator.run(workspace_id, "asset_manifest")
    for stage in ["characters", "scenes", "props", "reference_images"]:
        orchestrator.run(workspace_id, stage)

    orchestrator.reset_stage(workspace_id, "characters")
    artifacts = {item["kind"] for item in db.list_artifacts(workspace_id)}
    assert "characters" not in artifacts
    assert "reference_images" not in artifacts
    assert "scenes" in artifacts
    assert "props" in artifacts
    assert "script" in artifacts


def test_director_regenerate_review_blocks_script_approval_next_action(tmp_path: Path):
    db, orchestrator, workspace_id = build(tmp_path)
    orchestrator.run(workspace_id, "analysis")
    script = orchestrator.run(workspace_id, "script")
    db.set_stage_review(
        workspace_id,
        "script",
        script["revision"],
        {"recommended_action": "regenerate_current", "assessment": "unsupported facts"},
    )
    action = orchestrator.next_actions(workspace_id)[0]
    assert action["type"] == "run"
    assert action["stage"] == "script"
    assert action["review_blocked"] is True


def test_human_approval_can_override_director_regenerate_review(tmp_path: Path):
    db, orchestrator, workspace_id = build(tmp_path)
    orchestrator.run(workspace_id, "analysis")
    script = orchestrator.run(workspace_id, "script")
    db.set_stage_review(
        workspace_id,
        "script",
        script["revision"],
        {"recommended_action": "regenerate_current", "assessment": "needs revision"},
    )
    db.set_approval(workspace_id, "script_approved", "approved", "human override")
    action = orchestrator.next_actions(workspace_id)[0]
    assert not (action.get("stage") == "script" and action.get("review_blocked"))


def test_storyboard_normalization_validates_full_sequence_and_reconciles_duration():
    shots = []
    for index in range(1, 15):
        shots.append(
            {
                "index": f"{index:02d}",
                "duration_seconds": 8 if index < 14 else 10,
                "story_beat": f"beat {index}",
                "visual_prompt": f"visual {index}",
                "asset_bindings": {
                    "characters": ["char_a"],
                    "scenes": ["scene_a"],
                    "props": [],
                    "reference_images": [
                        {
                            "canonical_key": "combo__char_a__scene_a",
                            "candidate_id": "candidate-a",
                            "url": "/media/a.jpg",
                            "source_kind": "combination",
                        }
                    ],
                },
            }
        )
    normalized = Orchestrator._normalize_storyboard(
        {"shots": shots, "estimated_seconds": 999},
        workspace={"settings": {"storyboard_count": 14}},
        execution_directive={"parameter_overrides": {}},
        user_instruction="严格拆分为 14 个连续镜头",
    )
    validation = normalized["storyboard_validation"]
    assert validation["status"] == "pass"
    assert validation["expected_shots"] == 14
    assert validation["actual_shots"] == 14
    assert validation["actual_indices"] == list(range(1, 15))
    assert normalized["estimated_seconds"] == 114
    assert normalized["estimated_seconds_model"] == 999


def test_storyboard_normalization_reports_real_missing_tail_shots():
    shots = [
        {
            "index": index,
            "duration_seconds": 8,
            "story_beat": f"beat {index}",
            "visual_prompt": f"visual {index}",
            "asset_bindings": {
                "scenes": ["scene_a"],
                "reference_images": [{"canonical_key": "scene_a", "candidate_id": "c", "url": "/a.jpg"}],
            },
        }
        for index in range(1, 11)
    ]
    normalized = Orchestrator._normalize_storyboard(
        {"shots": shots, "estimated_seconds": 80},
        workspace={"settings": {"storyboard_count": 14}},
        execution_directive={},
        user_instruction="14 shots",
    )
    validation = normalized["storyboard_validation"]
    assert validation["status"] == "fail"
    assert validation["actual_shots"] == 10
    assert validation["missing_indices"] == [11, 12, 13, 14]


def test_dialogue_plan_normalization_validates_complete_storyboard_mapping():
    storyboard = {
        "shots": [
            {"index": index, "duration_seconds": 10}
            for index in range(1, 15)
        ]
    }
    silent = {2, 9, 14}
    items = []
    for index in range(1, 15):
        if index in silent:
            items.append({
                "shot_index": index,
                "status": "silent",
                "speaker_id": None,
                "text": "",
                "language": "fr",
                "timing": {"start_seconds": 0, "end_seconds": 0},
                "subtitle": "",
                "lip_sync_target": False,
            })
        else:
            items.append({
                "shot_index": index,
                "status": "dialogue",
                "speaker_id": "char_a",
                "text": f"Bonjour {index}",
                "language": "fr",
                "timing": {"start_seconds": 1, "end_seconds": 3},
                "subtitle": f"Bonjour {index}",
                "lip_sync_target": True,
            })
    normalized = Orchestrator._normalize_dialogue_plan(
        {"items": items}, storyboard_content=storyboard
    )
    validation = normalized["dialogue_validation"]
    assert validation["status"] == "pass"
    assert validation["expected_items"] == 14
    assert validation["actual_items"] == 14
    assert validation["missing_indices"] == []
    assert validation["dialogue_items"] == 11
    assert validation["silent_items"] == 3
    assert normalized["items"][0]["dialogue_text"] == "Bonjour 1"


def test_dialogue_review_guard_suppresses_ten_item_preview_false_negative():
    artifact = {
        "content": {
            "dialogue_validation": {
                "status": "pass",
                "expected_items": 14,
                "actual_items": 14,
            }
        }
    }
    review = {
        "recommended_action": "regenerate_current",
        "assessment": "only 10 items",
        "deviations": ["产物仅包含 10 个 items，缺失 shot_index 11–14"],
        "suggested_adjustments": ["补全 11–14"],
    }
    guarded = Orchestrator._guard_dialogue_plan_review(review, artifact)
    assert guarded["recommended_action"] == "proceed"
    assert guarded["deviations"] == []
    assert guarded["harness_review_guard"]["suppressed_structural_deviations"]



def test_sound_plan_validation_uses_complete_storyboard_sequence():
    storyboard = {"shots": [{"index": i, "duration_seconds": 8} for i in range(1, 15)]}
    dialogue = {"items": [
        {"shot_index": i, "status": "silent" if i in {2, 9, 14} else "dialogue"}
        for i in range(1, 15)
    ]}
    items = [
        {
            "shot_index": i,
            "ambience": "room tone",
            "foley": ["cloth"],
            "cues": [] if i in {2, 9, 14} else ["subtle cue"],
            "ducking": "none" if i in {2, 9, 14} else "duck under dialogue",
            "negative_audio": ["no BGM", "no spoken words"],
            "status": "planned",
        }
        for i in range(1, 15)
    ]
    normalized = Orchestrator._normalize_sound_plan(
        {"items": items}, storyboard_content=storyboard, dialogue_content=dialogue
    )
    validation = normalized["sound_validation"]
    assert validation["status"] == "pass"
    assert validation["expected_items"] == 14
    assert validation["actual_items"] == 14
    assert validation["missing_indices"] == []
    assert validation["dialogue_shots"] == [1,3,4,5,6,7,8,10,11,12,13]
    assert validation["silent_shots"] == [2,9,14]


def test_sound_review_guard_suppresses_ten_item_preview_false_negative():
    artifact = {"content": {"sound_validation": {"status": "pass", "expected_items": 14, "actual_items": 14}}}
    review = {
        "recommended_action": "regenerate_current",
        "assessment": "only 10 items",
        "deviations": ["产物仅包含 10 个 items，缺失 shot_index 11–14"],
        "suggested_adjustments": ["补全 11–14"],
    }
    guarded = Orchestrator._guard_sound_plan_review(review, artifact)
    assert guarded["recommended_action"] == "proceed"
    assert guarded["deviations"] == []
    assert guarded["harness_review_guard"]["suppressed_structural_deviations"]


def test_preview_contract_fails_before_saving_when_tracking_field_is_missing():
    content = {
        "shot_index": 1,
        "provider_job_id": "cgt-123",
        "model": "doubao-seedance-2-5-260628",
        "url": "/media/shot-001.mp4",
        "status": "succeeded",
    }
    Orchestrator._validate_preview_contract(content)
    del content["model"]
    try:
        Orchestrator._validate_preview_contract(content)
    except OrchestrationError as exc:
        assert "model" in str(exc)
    else:
        raise AssertionError("missing preview model must stop execution")


def test_preview_contract_rejects_wrong_requested_shot():
    content = {
        "shot_index": 1, "provider_job_id": "cgt-123", "model": "seedance-mini",
        "url": "/media/shot-001.mp4", "status": "succeeded",
    }
    try:
        Orchestrator._validate_preview_contract(content, expected_shot_index=3)
    except OrchestrationError as exc:
        assert "要求 shot_index=3" in str(exc)
    else:
        raise AssertionError("wrong preview shot must not be persisted")


def test_preview_review_guard_removes_false_metadata_blocker():
    artifact = {"content": {
        "shot_index": 1, "provider_job_id": "cgt-123", "model": "seedance",
        "url": "/media/shot-001.mp4", "status": "succeeded",
    }}
    review = {
        "deviations": ["Artifact content missing shot_index, provider_job_id, and model"],
        "suggested_adjustments": ["Enrich preview metadata", "Review visual continuity"],
        "recommended_action": "wait_for_user",
    }
    guarded = Orchestrator._guard_preview_review(review, artifact)
    assert guarded["deviations"] == []
    assert guarded["suggested_adjustments"] == ["Review visual continuity"]
    assert guarded["recommended_action"] == "wait_for_user"
    assert guarded["harness_review_guard"]["preview_contract_status"] == "pass"


def test_storyboard_expected_count_recognizes_compact_chinese_jing_and_beats_workspace_default():
    expected = Orchestrator._storyboard_expected_count(
        {"settings": {"storyboard_count": 8}},
        {"parameter_overrides": {}},
        "严格保留当前 storyboard 的 14 镜结构，总时长不变",
        previous_shot_count=14,
    )
    assert expected == 14


def test_storyboard_expected_count_preserves_previous_board_before_workspace_default():
    expected = Orchestrator._storyboard_expected_count(
        {"settings": {"storyboard_count": 8}},
        {"parameter_overrides": {}},
        "仅修复 asset_bindings，不改写任何镜头内容",
        previous_shot_count=14,
    )
    assert expected == 14


def test_storyboard_normalization_hydrates_reference_metadata_from_production_truth():
    shots = [{
        "index": 1,
        "duration_seconds": 10,
        "story_beat": "beat",
        "visual_prompt": "visual",
        "asset_bindings": {
            "characters": ["char_son"],
            "scenes": ["scene_foyer"],
            "props": [],
            "reference_images": [{"canonical_key": "char_son"}],
        },
    }]
    reference_content = {
        "items": [{
            "canonical_key": "char_son",
            "source_kind": "character",
            "candidate_id": "candidate-son",
            "url": "/media/son.png",
            "status": "selected_candidate",
        }]
    }
    normalized = Orchestrator._normalize_storyboard(
        {"shots": shots, "estimated_seconds": 10},
        workspace={"settings": {"storyboard_count": 8}},
        execution_directive={"parameter_overrides": {}},
        user_instruction="1 镜",
        reference_content=reference_content,
        previous_shot_count=1,
    )
    ref = normalized["shots"][0]["asset_bindings"]["reference_images"][0]
    assert ref["candidate_id"] == "candidate-son"
    assert ref["url"] == "/media/son.png"
    assert ref["source_kind"] == "character"
    assert normalized["storyboard_validation"]["reference_binding_gaps"] == []
    assert normalized["storyboard_validation"]["reference_metadata_hydrated"] == 1
    assert normalized["storyboard_validation"]["status"] == "pass"


def test_storyboard_review_guard_does_not_require_downstream_qa_closure():
    artifact = {
        "content": {
            "storyboard_validation": {"status": "pass"},
        }
    }
    review = {
        "recommended_action": "regenerate_current",
        "assessment": "missing downstream proof",
        "deviations": ["未提供 continuity_qa 复验结果以证明 blocking failures 已关闭"],
        "suggested_adjustments": ["重新运行 continuity_qa"],
    }
    guarded = Orchestrator._guard_storyboard_review(review, artifact)
    assert guarded["recommended_action"] == "proceed"
    assert guarded["deviations"] == []
    assert guarded["harness_review_guard"]["suppressed_structural_deviations"]


def test_batch_video_validation_and_review_guard_suppress_ten_item_preview_false_negative():
    artifact = {
        "kind": "batch_video",
        "revision": 2,
        "content": {
            "items": [
                {"shot_index": index, "status": "succeeded", "provider_job_id": f"task-{index}", "url": f"/shot-{index}.mp4"}
                for index in range(1, 15)
            ],
            "requested": 14,
            "completed": 14,
            "storyboard_shots": 14,
            "coverage_complete": True,
        },
    }
    storyboard = {"content": {"shots": [{"index": index} for index in range(1, 15)]}}
    sound = {"content": {"items": [
        {"shot_index": index, "ducking": {"active": False if index in {9, 14} else True}, "negative_audio": ["no music"]}
        for index in range(1, 15)
    ]}}
    validation = Orchestrator._batch_video_validation(artifact, storyboard, sound)
    assert validation["status"] == "pass"
    assert validation["actual_items"] == 14
    assert validation["missing_shots"] == []
    assert validation["sound_source_checks"]["14"]["ducking_active"] is False

    reviewed_artifact = {**artifact, "content": {**artifact["content"], "batch_validation": validation}}
    review = {
        "assessment": "incomplete",
        "recommended_action": "regenerate_current",
        "deviations": [
            "Shots 11–14 在 generated_artifact.content.items 中完全缺失，artifact 却声明 completed=14",
            "Shot 09 的 compact assembly_log 未显式暴露 ducking.active=false",
            "Shot 14 缺失导致无法验证其 negative_audio 禁令是否落实",
        ],
        "suggested_adjustments": ["补提交缺失的 Shots 11–14"],
    }
    guarded = Orchestrator._guard_batch_video_review(review, reviewed_artifact)
    assert guarded["recommended_action"] == "proceed"
    assert guarded["assessment"] == "pass"
    assert guarded["deviations"] == []
    assert guarded["harness_review_guard"]["actual_items"] == 14


def test_batch_video_validation_and_review_guard_use_dynamic_storyboard_count():
    """Batch coverage authority is the current storyboard, never a hard-coded 14."""
    shot_count = 6
    artifact = {
        "kind": "batch_video",
        "revision": 3,
        "content": {
            "items": [
                {"shot_index": index, "status": "succeeded", "provider_job_id": f"task-{index}", "url": f"/shot-{index}.mp4"}
                for index in range(1, shot_count + 1)
            ],
            "requested": shot_count,
            "completed": shot_count,
            "storyboard_shots": shot_count,
            "coverage_complete": True,
        },
    }
    storyboard = {"content": {"shots": [{"index": index} for index in range(1, shot_count + 1)]}}
    sound = {"content": {"items": [
        {"shot_index": index, "ducking": {"active": index != 6}, "negative_audio": ["no music"]}
        for index in range(1, shot_count + 1)
    ]}}
    validation = Orchestrator._batch_video_validation(artifact, storyboard, sound)
    assert validation["status"] == "pass"
    assert validation["expected_items"] == shot_count
    assert validation["expected_shot_indices"] == list(range(1, shot_count + 1))

    reviewed_artifact = {**artifact, "content": {**artifact["content"], "batch_validation": validation}}
    review = {
        "assessment": "incomplete",
        "recommended_action": "regenerate_current",
        "deviations": [
            "Shots 5–6 在 generated_artifact.content.items 中缺失，artifact 却声明 completed=6",
            "Shot 6 的 compact assembly_log 未显式暴露 ducking.active=false",
            "Shot 6 缺失导致无法验证其 negative_audio 禁令是否落实",
        ],
        "suggested_adjustments": ["补提交缺失的 Shots 5–6"],
    }
    guarded = Orchestrator._guard_batch_video_review(review, reviewed_artifact)
    assert guarded["recommended_action"] == "proceed"
    assert guarded["assessment"] == "pass"
    assert guarded["deviations"] == []
    assert guarded["harness_review_guard"]["expected_items"] == shot_count


def test_music_plan_validation_uses_current_storyboard_shot_count_not_fixed_count():
    content = {
        "global_style": "French chamber score",
        "cues": [
            {"cue_id": "A", "start_shot": 1, "end_shot": 2, "start_time": 0, "end_time": 10, "mood": "warm", "instrumentation": "piano", "intensity": "low", "dialogue_avoidance": {"active": True}},
            {"cue_id": "B", "start_shot": 3, "end_shot": 3, "start_time": 10, "end_time": 15, "mood": "silence", "instrumentation": "silence", "intensity": 0, "dialogue_avoidance": {"active": False}},
        ],
        "ducking": "-8dB",
        "render_mode": "assembly-external",
        "status": "ready",
    }
    storyboard = {"shots": [
        {"index": 1, "duration_seconds": 5},
        {"index": 2, "duration_seconds": 5},
        {"index": 3, "duration_seconds": 5},
    ]}
    normalized = Orchestrator._normalize_music_plan(content, storyboard_content=storyboard, dialogue_content={"items": []})
    validation = normalized["music_plan_validation"]
    assert validation["status"] == "pass"
    assert validation["expected_shot_count"] == 3
    assert validation["covered_shots"] == [1, 2, 3]
    assert validation["total_duration_seconds"] == 15.0


def test_skip_music_creates_auditable_ready_placeholder_and_unlocks_compose(tmp_path: Path):
    db, orchestrator, workspace_id = build(tmp_path)
    orchestrator.run(workspace_id, "analysis")
    orchestrator.run(workspace_id, "script")
    db.set_approval(workspace_id, "script_approved", "approved")
    orchestrator.run(workspace_id, "asset_manifest")
    for stage in ["characters", "scenes", "props", "reference_images"]:
        orchestrator.run(workspace_id, stage)
    db.set_approval(workspace_id, "assets_approved", "approved")
    for stage in ["storyboard", "dialogue_plan", "sound_plan", "review"]:
        orchestrator.run(workspace_id, stage)
    db.set_approval(workspace_id, "storyboard_approved", "approved")
    orchestrator.run(workspace_id, "preview")
    db.set_approval(workspace_id, "preview_approved", "approved")
    orchestrator.run(workspace_id, "batch_video")

    result = orchestrator.skip_music(workspace_id)
    music = db.get_artifact(workspace_id, "music")
    assert result["media_generated"] is False
    assert result["next_stage"] == "compose"
    assert music and music["status"] == "ready"
    assert music["content"]["mode"] == "skipped_no_bgm"
    assert music["content"]["skipped"] is True
    assert music["content"]["_execution"]["music_api_called"] is False

    action = orchestrator.next_actions(workspace_id)[0]
    assert action["type"] == "run"
    assert action["stage"] == "compose"


def test_skipped_music_bypasses_missing_music_plan_but_not_incomplete_batch(tmp_path: Path):
    db, orchestrator, workspace_id = build(tmp_path)
    orchestrator.run(workspace_id, "analysis")
    orchestrator.run(workspace_id, "script")
    db.set_approval(workspace_id, "script_approved", "approved")
    orchestrator.run(workspace_id, "asset_manifest")
    for stage in ["characters", "scenes", "props", "reference_images"]:
        orchestrator.run(workspace_id, stage)
    db.set_approval(workspace_id, "assets_approved", "approved")
    for stage in ["storyboard", "dialogue_plan", "sound_plan", "review"]:
        orchestrator.run(workspace_id, stage)
    db.set_approval(workspace_id, "storyboard_approved", "approved")
    orchestrator.run(workspace_id, "preview")
    db.set_approval(workspace_id, "preview_approved", "approved")
    orchestrator.run(workspace_id, "batch_video")

    # There is deliberately no music_plan artifact here. Explicit skip still
    # opens compose because the user deferred the optional music branch.
    orchestrator.skip_music(workspace_id)
    assert db.get_artifact(workspace_id, "music_plan") is None
    assert orchestrator.next_actions(workspace_id)[0]["stage"] == "compose"

    # If the complete batch becomes stale, a new skip is refused.
    batch = db.get_artifact(workspace_id, "batch_video")
    db.mark_artifacts_stale(workspace_id, ["batch_video"])
    try:
        orchestrator.skip_music(workspace_id)
    except OrchestrationError as exc:
        assert "批量视频尚未完整 ready" in str(exc)
    else:
        raise AssertionError("skip music must not bypass incomplete/stale batch video")


def test_compose_review_guard_suppresses_false_missing_audit_fields():
    artifact = {
        "content": {
            "compose_validation": {
                "status": "pass",
                "structural_authority": "harness+ffmpeg+ffprobe",
                "checks": {"ffmpeg_returncode_zero": True, "audio_sample_rate_32000": True},
            }
        }
    }
    review = {
        "assessment": "incomplete",
        "recommended_action": "regenerate_current",
        "reply": "missing audit fields",
        "deviations": [
            "缺少 local_path / input_shots / encoding，无法验证 FFmpeg 返回码与 ffprobe streams",
            "未显式记录 Shot 10 重采样为 32 kHz stereo",
            "input_shot_indices 仅展示 1-10，与实际镜头数不匹配",
            "视频分辨率为 720x1280，非 1080x1920，不符合 9:16 要求",
            "duration_within_tolerance 未通过",
        ],
        "suggested_adjustments": ["补齐 ffprobe 与重采样审计字段"],
    }
    guarded = Orchestrator._guard_compose_review(review, artifact)
    assert guarded["recommended_action"] == "proceed"
    assert guarded["assessment"] == "pass"
    assert guarded["deviations"] == []
    assert guarded["harness_review_guard"]["compose_validation_status"] == "pass"


def test_compose_review_guard_handles_object_deviation_payloads():
    artifact = {
        "content": {
            "compose_validation": {
                "status": "pass",
                "structural_authority": "harness+ffmpeg+ffprobe",
                "checks": {"input_shots_schema": True, "ffmpeg_returncode_zero": True},
            }
        }
    }
    review = {
        "assessment": "partial_failure",
        "recommended_action": "regenerate_current",
        "deviations": {
            "issue": "input_shots 缺少逐镜结构，无法验证 duration_seconds"
        },
        "suggested_adjustments": {
            "message": "补齐 input_shots"
        },
    }
    guarded = Orchestrator._guard_compose_review(review, artifact)
    assert guarded["recommended_action"] == "proceed"
    assert guarded["assessment"] == "pass"
    assert guarded["deviations"] == []
