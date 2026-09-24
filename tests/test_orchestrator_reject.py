from pathlib import Path

from app.db import Database
from app.orchestrator import Orchestrator
from app.providers.mock import MockProvider


class MockRegistry:
    def workflow(self):
        return MockProvider()


def test_rejected_gate_offers_regeneration_instead_of_reapproval(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    orchestrator = Orchestrator(db, MockRegistry())
    workspace = db.create_workspace("Episode", "brief", {})
    orchestrator.bootstrap_workspace(workspace["id"])
    orchestrator.run(workspace["id"], "analysis")
    orchestrator.run(workspace["id"], "script")
    db.set_approval(workspace["id"], "script_approved", "rejected", "needs rewrite")
    action = orchestrator.next_actions(workspace["id"])[0]
    assert action["type"] == "run"
    assert action["stage"] == "script"
    assert "重新生成" in action["label"]
