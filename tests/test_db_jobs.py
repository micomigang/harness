from pathlib import Path

from app.db import Database


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "harness.db")


def test_acquire_job_creates_first_job(tmp_path: Path):
    db = _db(tmp_path)
    ws = db.create_workspace("demo", "brief", {})
    acquired = db.acquire_job(ws["id"], "analysis")
    assert acquired["created"] is True
    assert acquired["reason"] == "created"
    assert acquired["job"]["stage"] == "analysis"
    assert acquired["job"]["status"] == "queued"


def test_acquire_job_reuses_same_stage_job(tmp_path: Path):
    db = _db(tmp_path)
    ws = db.create_workspace("demo", "brief", {})
    first = db.acquire_job(ws["id"], "analysis")
    second = db.acquire_job(ws["id"], "analysis")
    assert first["created"] is True
    assert second["created"] is False
    assert second["reason"] == "same_stage_active"
    assert second["job"]["id"] == first["job"]["id"]


def test_acquire_job_blocks_other_stage_while_workspace_busy(tmp_path: Path):
    db = _db(tmp_path)
    ws = db.create_workspace("demo", "brief", {})
    first = db.acquire_job(ws["id"], "analysis")
    blocked = db.acquire_job(ws["id"], "script")
    assert first["created"] is True
    assert blocked["created"] is False
    assert blocked["reason"] == "workspace_busy"
    assert blocked["job"]["id"] == first["job"]["id"]


def test_recover_interrupted_jobs_releases_safety_valve(tmp_path: Path):
    db = _db(tmp_path)
    ws = db.create_workspace("demo", "brief", {})
    job = db.acquire_job(ws["id"], "analysis")["job"]
    db.update_job(job["id"], status="running", progress=42, message="working")
    assert db.list_active_jobs(ws["id"])
    recovered = db.recover_interrupted_jobs()
    assert recovered == 1
    restored = db.get_job(job["id"])
    assert restored["status"] == "failed"
    assert "重启" in restored["message"]
    assert db.list_active_jobs(ws["id"]) == []


def test_delete_workspace_removes_related_database_state(tmp_path: Path):
    db = _db(tmp_path)
    ws = db.create_workspace("demo", "brief", {})
    db.upsert_artifact(ws["id"], "source", "source", {"files": []}, "local")
    db.set_approval(ws["id"], "script_approved", "pending")
    job = db.create_job(ws["id"], "analysis")
    db.update_job(job["id"], status="failed", progress=100, message="x")
    assert db.delete_workspace(ws["id"]) is True
    assert db.get_workspace(ws["id"]) is None
    assert db.list_artifacts(ws["id"]) == []
    assert db.list_jobs(ws["id"]) == []
    assert db.list_approvals(ws["id"]) == []


def test_clear_terminal_jobs_preserves_active_job(tmp_path: Path):
    db = _db(tmp_path)
    ws = db.create_workspace("demo", "brief", {})
    failed = db.create_job(ws["id"], "analysis")
    db.update_job(failed["id"], status="failed", progress=100, message="failed")
    active = db.create_job(ws["id"], "script")
    removed = db.clear_terminal_jobs(ws["id"])
    assert removed == 1
    jobs = db.list_jobs(ws["id"])
    assert [job["id"] for job in jobs] == [active["id"]]


def test_job_request_snapshot_and_guidance_memory_persist(tmp_path: Path):
    db = _db(tmp_path)
    ws = db.create_workspace("demo", "brief", {"target_market": "France"})
    db.set_workspace_memory(ws["id"], "Use natural French localization")
    guidance = db.upsert_stage_guidance(
        ws["id"],
        "script",
        user_instruction="Localize fully for France",
        director_plan={"prompt_addendum": "Use idiomatic French"},
        plan_input_hash="hash-1",
    )
    job = db.create_job(
        ws["id"], "script", request={"user_instruction": "Localize fully for France"}
    )

    assert db.get_workspace_memory(ws["id"]) == "Use natural French localization"
    assert guidance["director_plan"]["prompt_addendum"] == "Use idiomatic French"
    assert db.get_stage_guidance(ws["id"], "script")["plan_input_hash"] == "hash-1"
    assert job["request"]["user_instruction"] == "Localize fully for France"


def test_existing_jobs_schema_is_migrated_with_request_json(tmp_path: Path):
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL,
            message TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    with db.connect() as migrated:
        columns = [row["name"] for row in migrated.execute("PRAGMA table_info(jobs)").fetchall()]
    assert "request_json" in columns


def test_thematic_asset_library_can_be_shared_across_workspaces(tmp_path: Path):
    db = _db(tmp_path)
    ws1 = db.create_workspace("episode 1", "brief", {})
    ws2 = db.create_workspace("episode 2", "brief", {})
    lib = db.create_asset_library(
        "法国现代豪宅人物库", "France contemporary bourgeois family", "shared cast", "character"
    )
    item = db.add_asset_item(
        lib["id"], "character", "Mamie Rose", "elder matriarch", {"continuity_lock": "same face and coat"}
    )
    db.link_workspace_asset_library(ws1["id"], lib["id"])
    db.link_workspace_asset_library(ws2["id"], lib["id"])

    assert db.list_workspace_asset_libraries(ws1["id"])[0]["scope"] == "character"
    assert db.workspace_asset_context(ws2["id"])[0]["items"][0]["id"] == item["id"]

    # Deleting one project must not delete a shared global library.
    assert db.delete_workspace(ws1["id"])
    assert db.get_asset_library(lib["id"])
    assert db.list_asset_items(lib["id"])[0]["name"] == "Mamie Rose"


def test_job_progress_is_emitted_into_linear_activity_feed(tmp_path: Path):
    db = _db(tmp_path)
    ws = db.create_workspace("demo", "brief", {})
    job = db.create_job(ws["id"], "analysis")
    db.update_job(job["id"], status="running", progress=48, message="Kimi 分段分析 6/18")
    feed = db.list_activity_feed(ws["id"])
    updates = [item for item in feed if item.get("kind") == "event" and item.get("type") == "job.updated"]
    assert updates
    assert updates[-1]["payload"]["progress"] == 48
    assert "6/18" in updates[-1]["payload"]["message"]


def test_stage_review_persists_by_artifact_revision(tmp_path: Path):
    db = _db(tmp_path)
    ws = db.create_workspace("demo", "brief", {})
    review = db.set_stage_review(ws["id"], "script", 2, {"recommended_action": "regenerate_current"})
    assert review["artifact_revision"] == 2
    assert db.get_stage_review(ws["id"], "script")["review"]["recommended_action"] == "regenerate_current"
