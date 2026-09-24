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
