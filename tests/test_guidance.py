from pathlib import Path

from app.db import Database
from app.orchestrator import Orchestrator
from app.providers.mock import MockProvider
from app.guidance import sanitize_parameter_overrides


class PlanningProvider(MockProvider):
    def __init__(self):
        self.plan_calls = 0
        self.last_context = None

    def plan_stage(self, stage, context):
        self.plan_calls += 1
        return {
            "stage": stage,
            "interpretation": "French localization",
            "prompt_addendum": "Use idiomatic French and French cultural equivalents.",
            "parameter_overrides": {},
            "acceptance_criteria": ["French dialogue sounds native"],
            "memory_candidates": [],
            "warnings": [],
            "questions": [],
            "requires_user_input": False,
            "planner_model": "test-planner",
        }

    def generate(self, stage, context):
        self.last_context = context
        return super().generate(stage, context)


class PlanningRegistry:
    def __init__(self):
        self.provider = PlanningProvider()

    def workflow(self):
        return self.provider


def test_director_plan_is_cached_and_applied_to_stage_context(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    registry = PlanningRegistry()
    orchestrator = Orchestrator(db, registry)
    ws = db.create_workspace(
        "Episode",
        "family drama",
        {"target_language": "French (France)", "target_market": "France"},
    )
    orchestrator.bootstrap_workspace(ws["id"])
    db.set_workspace_memory(ws["id"], "Keep source plot, localize culture for France")

    preview = orchestrator.plan_stage(
        ws["id"], "analysis", "Focus on family hierarchy"
    )
    assert preview["cached"] is False
    assert registry.provider.plan_calls == 1

    # Running with the same inputs reuses the stored director plan instead of
    # spending a second planner call.
    result = orchestrator.run(
        ws["id"],
        "analysis",
        request={"user_instruction": "Focus on family hierarchy"},
    )
    assert registry.provider.plan_calls == 1
    assert registry.provider.last_context["project_memory"].startswith("Keep source")
    assert registry.provider.last_context["execution_directive"]["prompt_addendum"].startswith("Use idiomatic")
    assert result["content"]["_execution"]["user_instruction"] == "Focus on family hierarchy"


def test_parameter_override_safety_whitelist():
    assert sanitize_parameter_overrides(
        "analysis",
        {
            "segment_seconds": 5,
            "segment_max_frames": 999,
            "segment_parallelism": 20,
            "temperature": 0.1,
        },
    ) == {
        "segment_seconds": 10,
        "segment_max_frames": 16,
        "segment_parallelism": 6,
    }
    assert sanitize_parameter_overrides(
        "batch_video",
        {
            "resolution": "4K",
            "ratio": "9:16",
            "generate_audio": "true",
            "batch_max_shots": 99,
        },
    ) == {
        "ratio": "9:16",
        "generate_audio": True,
        "batch_max_shots": 20,
    }


def test_director_chat_binds_effective_instruction_and_records_reply(tmp_path: Path):
    db = Database(tmp_path / "chat.db")
    registry = PlanningRegistry()
    orchestrator = Orchestrator(db, registry)
    ws = db.create_workspace(
        "Episode",
        "family drama",
        {"target_language": "French (France)", "target_market": "France"},
    )
    orchestrator.bootstrap_workspace(ws["id"])

    result = orchestrator.chat_stage(
        ws["id"],
        "analysis",
        "重点看人物关系，并保留法国本地化需要的社会阶层信息",
    )
    assert result["stage"] == "analysis"
    assert result["input_hash"]
    guidance = db.get_stage_guidance(ws["id"], "analysis")
    assert guidance
    assert guidance["director_plan"]
    assert guidance["plan_input_hash"] == result["input_hash"]
    messages = db.list_guidance_messages(ws["id"])
    assert any(item["role"] == "user" for item in messages)
    assert any(item["role"] == "director" for item in messages)


def test_bound_plan_becomes_invalid_after_upstream_revision(tmp_path: Path):
    db = Database(tmp_path / "bound.db")
    registry = PlanningRegistry()
    orchestrator = Orchestrator(db, registry)
    ws = db.create_workspace("Episode", "family drama", {})
    orchestrator.bootstrap_workspace(ws["id"])
    chat = orchestrator.chat_stage(ws["id"], "analysis", "分析人物关系")
    assert orchestrator.validate_bound_plan(ws["id"], "analysis", chat["input_hash"])["valid"]

    source = db.get_artifact(ws["id"], "source")
    assert source
    orchestrator.revise(ws["id"], "source", source["content"], cascade=True)
    assert not orchestrator.validate_bound_plan(ws["id"], "analysis", chat["input_hash"])["valid"]


def test_technical_override_requires_explicit_user_request():
    proposed = {"segment_seconds": 15, "segment_max_frames": 8, "segment_parallelism": 3}
    assert sanitize_parameter_overrides(
        "analysis",
        proposed,
        user_instruction="请更仔细地分析人物关系和字幕证据",
        require_explicit_user_request=True,
    ) == {}
    assert sanitize_parameter_overrides(
        "analysis",
        proposed,
        user_instruction="改成15秒一段，每段最多8帧，并行3路",
        require_explicit_user_request=True,
    ) == proposed


def test_asset_library_context_invalidates_bound_plan(tmp_path: Path):
    db = Database(tmp_path / "assets-guidance.db")
    registry = PlanningRegistry()
    orchestrator = Orchestrator(db, registry)
    ws = db.create_workspace("Episode", "family drama", {})
    orchestrator.bootstrap_workspace(ws["id"])
    chat = orchestrator.chat_stage(ws["id"], "analysis", "分析人物关系")
    assert orchestrator.validate_bound_plan(ws["id"], "analysis", chat["input_hash"])["valid"]
    lib = db.create_asset_library("France Family", "France contemporary", scope="character")
    db.link_workspace_asset_library(ws["id"], lib["id"])
    assert not orchestrator.validate_bound_plan(ws["id"], "analysis", chat["input_hash"])["valid"]
