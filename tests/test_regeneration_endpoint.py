from fastapi import BackgroundTasks

import app.main as main_module


class FakeDB:
    def __init__(self):
        self.request = None

    def get_workspace(self, workspace_id):
        return {"id": workspace_id} if workspace_id == "ws-1" else None

    def acquire_job(self, workspace_id, stage, request=None):
        self.request = request
        return {
            "job": {
                "id": "job-1",
                "workspace_id": workspace_id,
                "stage": stage,
                "status": "queued",
                "progress": 0,
                "message": "等待执行",
            },
            "created": True,
            "reason": "created",
        }


class FakeOrchestrator:
    def __init__(self):
        self.prepared = None

    def prepare_stage_regeneration(self, workspace_id, stage, feedback):
        self.prepared = (workspace_id, stage, feedback)
        return {
            "reply": "已理解并准备重新生成",
            "effective_instruction": "canonical regenerated instruction",
            "input_hash": "plan-hash",
            "previous_revision": 2,
            "downstream_cleared": ["characters"],
            "reset_gates": ["assets_approved"],
            "memory_preserved": True,
            "conversation_preserved": True,
        }

    def execute_job(self, job_id):
        return None


def test_regeneration_route_is_registered():
    paths = {getattr(route, "path", "") for route in main_module.app.routes}
    assert "/api/workspaces/{workspace_id}/stages/{stage}/regenerate" in paths


def test_regeneration_endpoint_prepares_feedback_and_enqueues_job(monkeypatch):
    fake_db = FakeDB()
    fake_orchestrator = FakeOrchestrator()
    monkeypatch.setattr(main_module, "db", fake_db)
    monkeypatch.setattr(main_module, "orchestrator", fake_orchestrator)
    background = BackgroundTasks()

    result = main_module.regenerate_stage(
        "ws-1",
        "asset_manifest",
        background,
        main_module.StageRegenerationRequest(feedback="保留正确资产并重新解析"),
    )

    assert fake_orchestrator.prepared == (
        "ws-1",
        "asset_manifest",
        "保留正确资产并重新解析",
    )
    assert fake_db.request["user_instruction"] == "canonical regenerated instruction"
    assert fake_db.request["director_plan_hash"] == "plan-hash"
    assert fake_db.request["interaction_mode"] == "regenerate_current"
    assert fake_db.request["previous_revision"] == 2
    assert result["status"] == "queued"
    assert result["director_reply"] == "已理解并准备重新生成"
    assert result["memory_preserved"] is True
    assert len(background.tasks) == 1
