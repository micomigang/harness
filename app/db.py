from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    brief TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    content_json TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    upstream_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    gate TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(workspace_id, gate)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_memory (
                    workspace_id TEXT PRIMARY KEY,
                    memory_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );
                CREATE TABLE IF NOT EXISTS stage_guidance (
                    workspace_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    user_instruction TEXT NOT NULL,
                    director_plan_json TEXT NOT NULL,
                    plan_input_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, stage),
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );
                CREATE TABLE IF NOT EXISTS guidance_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );
                CREATE TABLE IF NOT EXISTS stage_reviews (
                    workspace_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    artifact_revision INTEGER NOT NULL,
                    review_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, stage)
                );
                CREATE TABLE IF NOT EXISTS production_series (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    theme TEXT NOT NULL,
                    description TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_libraries (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    theme TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'mixed',
                    description TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_items (
                    id TEXT PRIMARY KEY,
                    library_id TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    preview_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_asset_libraries (
                    workspace_id TEXT NOT NULL,
                    library_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, library_id)
                );
                """
            )
            job_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "request_json" not in job_columns:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}'"
                )
            asset_library_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(asset_libraries)").fetchall()
            }
            if "scope" not in asset_library_columns:
                conn.execute(
                    "ALTER TABLE asset_libraries ADD COLUMN scope TEXT NOT NULL DEFAULT 'mixed'"
                )
            asset_library_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(asset_libraries)").fetchall()
            }
            if "series_id" not in asset_library_columns:
                conn.execute("ALTER TABLE asset_libraries ADD COLUMN series_id TEXT NOT NULL DEFAULT ''")
            if "auto_managed" not in asset_library_columns:
                conn.execute("ALTER TABLE asset_libraries ADD COLUMN auto_managed INTEGER NOT NULL DEFAULT 0")
            asset_item_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(asset_items)").fetchall()
            }
            asset_item_additions = {
                "canonical_key": "TEXT NOT NULL DEFAULT ''",
                "parent_item_id": "TEXT NOT NULL DEFAULT ''",
                "variant_key": "TEXT NOT NULL DEFAULT ''",
                "retrieval_text": "TEXT NOT NULL DEFAULT ''",
                "source_workspace_id": "TEXT NOT NULL DEFAULT ''",
                "source_artifact_kind": "TEXT NOT NULL DEFAULT ''",
                "source_revision": "INTEGER NOT NULL DEFAULT 0",
                "status": "TEXT NOT NULL DEFAULT 'approved'",
            }
            for column, definition in asset_item_additions.items():
                if column not in asset_item_columns:
                    conn.execute(f"ALTER TABLE asset_items ADD COLUMN {column} {definition}")

    def create_workspace(
        self, title: str, brief: str, workspace_settings: dict[str, Any]
    ) -> dict[str, Any]:
        workspace_id = str(uuid.uuid4())
        now = utcnow()
        row = {
            "id": workspace_id,
            "title": title,
            "brief": brief,
            "stage": "source",
            "status": "draft",
            "settings": workspace_settings,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    title,
                    brief,
                    "source",
                    "draft",
                    json.dumps(workspace_settings, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        self.add_event(workspace_id, "workspace.created", {"title": title})
        return row

    def list_workspaces(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workspaces ORDER BY updated_at DESC"
            ).fetchall()
        return [self._workspace(row) for row in rows]

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        return self._workspace(row) if row else None

    def update_workspace(self, workspace_id: str, **values: Any) -> None:
        allowed = {"title", "brief", "stage", "status", "settings_json"}
        changes = {k: v for k, v in values.items() if k in allowed}
        changes["updated_at"] = utcnow()
        columns = ", ".join(f"{key} = ?" for key in changes)
        with self._lock, self.connect() as conn:
            conn.execute(
                f"UPDATE workspaces SET {columns} WHERE id = ?",
                (*changes.values(), workspace_id),
            )

    def upsert_artifact(
        self,
        workspace_id: str,
        kind: str,
        name: str,
        content: Any,
        provider: str,
        status: str = "ready",
        upstream: list[str] | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        with self._lock, self.connect() as conn:
            previous = conn.execute(
                "SELECT * FROM artifacts WHERE workspace_id = ? AND kind = ?",
                (workspace_id, kind),
            ).fetchone()
            artifact_id = previous["id"] if previous else str(uuid.uuid4())
            revision = int(previous["revision"]) + 1 if previous else 1
            conn.execute(
                """
                INSERT INTO artifacts
                (id, workspace_id, kind, name, status, revision, content_json,
                 provider, upstream_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name, status=excluded.status,
                  revision=excluded.revision, content_json=excluded.content_json,
                  provider=excluded.provider, upstream_json=excluded.upstream_json,
                  updated_at=excluded.updated_at
                """,
                (
                    artifact_id,
                    workspace_id,
                    kind,
                    name,
                    status,
                    revision,
                    json.dumps(content, ensure_ascii=False),
                    provider,
                    json.dumps(upstream or [], ensure_ascii=False),
                    previous["created_at"] if previous else now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        artifact = self._artifact(row)
        self.add_event(
            workspace_id,
            "artifact.updated",
            {"kind": kind, "revision": revision, "status": status},
        )
        return artifact

    def list_artifacts(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE workspace_id = ? ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
        return [self._artifact(row) for row in rows]

    def get_artifact(self, workspace_id: str, kind: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE workspace_id = ? AND kind = ?",
                (workspace_id, kind),
            ).fetchone()
        return self._artifact(row) if row else None

    def mark_artifacts_stale(self, workspace_id: str, kinds: list[str]) -> list[str]:
        if not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        now = utcnow()
        with self._lock, self.connect() as conn:
            conn.execute(
                f"UPDATE artifacts SET status='stale', updated_at=? "
                f"WHERE workspace_id=? AND kind IN ({placeholders})",
                (now, workspace_id, *kinds),
            )
            rows = conn.execute(
                f"SELECT kind FROM artifacts WHERE workspace_id=? "
                f"AND kind IN ({placeholders})",
                (workspace_id, *kinds),
            ).fetchall()
        changed = [row["kind"] for row in rows]
        if changed:
            self.add_event(workspace_id, "artifacts.invalidated", {"kinds": changed})
        return changed

    def delete_artifacts(self, workspace_id: str, kinds: list[str]) -> list[str]:
        """Delete selected stage artifacts while leaving upstream artifacts untouched."""
        if not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                f"SELECT kind FROM artifacts WHERE workspace_id=? AND kind IN ({placeholders})",
                (workspace_id, *kinds),
            ).fetchall()
            if rows:
                conn.execute(
                    f"DELETE FROM artifacts WHERE workspace_id=? AND kind IN ({placeholders})",
                    (workspace_id, *kinds),
                )
        removed = [str(row["kind"]) for row in rows]
        if removed:
            self.add_event(workspace_id, "artifacts.cleared", {"kinds": removed})
        return removed

    def clear_terminal_jobs_for_stages(self, workspace_id: str, stages: list[str]) -> int:
        if not stages:
            return 0
        placeholders = ",".join("?" for _ in stages)
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM jobs WHERE workspace_id=? AND stage IN ({placeholders}) "
                "AND status IN ('succeeded','failed')",
                (workspace_id, *stages),
            ).fetchall()
            if rows:
                conn.execute(
                    f"DELETE FROM jobs WHERE workspace_id=? AND stage IN ({placeholders}) "
                    "AND status IN ('succeeded','failed')",
                    (workspace_id, *stages),
                )
        return len(rows)

    def clear_stage_guidance_plans(self, workspace_id: str, stages: list[str]) -> int:
        """Invalidate director plans but preserve the user's durable stage instructions."""
        if not stages:
            return 0
        placeholders = ",".join("?" for _ in stages)
        now = utcnow()
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                f"SELECT stage FROM stage_guidance WHERE workspace_id=? AND stage IN ({placeholders})",
                (workspace_id, *stages),
            ).fetchall()
            if rows:
                conn.execute(
                    f"UPDATE stage_guidance SET director_plan_json='{{}}', plan_input_hash='', updated_at=? "
                    f"WHERE workspace_id=? AND stage IN ({placeholders})",
                    (now, workspace_id, *stages),
                )
        return len(rows)

    def set_approval(
        self, workspace_id: str, gate: str, status: str, note: str = ""
    ) -> dict[str, Any]:
        now = utcnow()
        approval_id = str(uuid.uuid4())
        with self._lock, self.connect() as conn:
            previous = conn.execute(
                "SELECT id, created_at FROM approvals WHERE workspace_id=? AND gate=?",
                (workspace_id, gate),
            ).fetchone()
            if previous:
                approval_id = previous["id"]
            conn.execute(
                """
                INSERT INTO approvals
                (id, workspace_id, gate, status, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, gate) DO UPDATE SET
                  status=excluded.status, note=excluded.note,
                  updated_at=excluded.updated_at
                """,
                (
                    approval_id,
                    workspace_id,
                    gate,
                    status,
                    note,
                    previous["created_at"] if previous else now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
        result = dict(row)
        self.add_event(workspace_id, "approval.changed", {"gate": gate, "status": status})
        return result

    def list_approvals(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE workspace_id=? ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_job(
        self,
        workspace_id: str,
        stage: str,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = utcnow()
        request_json = json.dumps(request or {}, ensure_ascii=False)
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs
                (id, workspace_id, stage, status, progress, message, result_json,
                 created_at, updated_at, request_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    workspace_id,
                    stage,
                    "queued",
                    0,
                    "等待执行",
                    "{}",
                    now,
                    now,
                    request_json,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row)  # type: ignore[return-value]

    def acquire_job(
        self,
        workspace_id: str,
        stage: str,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a queued job unless the workspace already has one in flight."""
        now = utcnow()
        request_json = json.dumps(request or {}, ensure_ascii=False)
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE workspace_id=? AND status IN ('queued','running') ORDER BY created_at DESC",
                (workspace_id,),
            ).fetchall()
            same_stage = next((row for row in rows if row["stage"] == stage), None)
            if same_stage:
                return {
                    "job": self._job(same_stage),
                    "created": False,
                    "reason": "same_stage_active",
                }
            if rows:
                return {
                    "job": self._job(rows[0]),
                    "created": False,
                    "reason": "workspace_busy",
                }
            job_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO jobs
                (id, workspace_id, stage, status, progress, message, result_json,
                 created_at, updated_at, request_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    workspace_id,
                    stage,
                    "queued",
                    0,
                    "等待执行",
                    "{}",
                    now,
                    now,
                    request_json,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return {"job": self._job(row), "created": True, "reason": "created"}  # type: ignore[arg-type]

    def update_job(self, job_id: str, **values: Any) -> None:
        allowed = {"status", "progress", "message", "result_json"}
        changes = {k: v for k, v in values.items() if k in allowed}
        changes["updated_at"] = utcnow()
        columns = ", ".join(f"{key}=?" for key in changes)
        workspace_id = ""
        stage = ""
        before: dict[str, Any] = {}
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row:
                before = dict(row)
                workspace_id = str(row["workspace_id"])
                stage = str(row["stage"])
            conn.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*changes.values(), job_id))
        if workspace_id and any(key in changes and before.get(key) != changes.get(key) for key in ("status", "progress", "message")):
            self.add_event(workspace_id, "job.updated", {
                "job_id": job_id, "stage": stage,
                "status": changes.get("status", before.get("status")),
                "progress": changes.get("progress", before.get("progress")),
                "message": changes.get("message", before.get("message")),
            })

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE workspace_id=? ORDER BY created_at DESC",
                (workspace_id,),
            ).fetchall()
        return [self._job(row) for row in rows]

    def list_active_jobs(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE workspace_id=? AND status IN ('queued','running') ORDER BY created_at DESC",
                (workspace_id,),
            ).fetchall()
        return [self._job(row) for row in rows]

    def recover_interrupted_jobs(self) -> int:
        """Mark jobs left queued/running by a previous server process as failed.

        BackgroundTasks cannot survive a process restart. Leaving those rows active
        would permanently trip the duplicate-task safety valve.
        """
        now = utcnow()
        message = "Harness 已重启；上一次未结束的任务已标记为中断，可重新运行"
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status IN ('queued','running')"
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE jobs SET status='failed', progress=100, message=?, updated_at=? "
                    "WHERE status IN ('queued','running')",
                    (message, now),
                )
        return len(rows)

    def delete_workspace(self, workspace_id: str) -> bool:
        """Delete one workspace and all of its persisted DB state."""
        with self._lock, self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)
            ).fetchone()
            if not exists:
                return False
            for table in ("workspace_asset_libraries", "stage_reviews", "guidance_messages", "stage_guidance", "workspace_memory", "events", "jobs", "approvals", "artifacts"):
                conn.execute(f"DELETE FROM {table} WHERE workspace_id=?", (workspace_id,))
            conn.execute("DELETE FROM workspaces WHERE id=?", (workspace_id,))
        return True

    def clear_terminal_jobs(self, workspace_id: str) -> int:
        """Remove completed/failed job history while preserving active jobs."""
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE workspace_id=? AND status IN ('succeeded','failed')",
                (workspace_id,),
            ).fetchall()
            if rows:
                conn.execute(
                    "DELETE FROM jobs WHERE workspace_id=? AND status IN ('succeeded','failed')",
                    (workspace_id,),
                )
        return len(rows)

    def get_workspace_memory(self, workspace_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT memory_text FROM workspace_memory WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        return str(row["memory_text"]) if row else ""

    def set_workspace_memory(self, workspace_id: str, memory_text: str) -> str:
        now = utcnow()
        with self._lock, self.connect() as conn:
            previous = conn.execute(
                "SELECT created_at FROM workspace_memory WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO workspace_memory
                (workspace_id, memory_text, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                  memory_text=excluded.memory_text, updated_at=excluded.updated_at
                """,
                (
                    workspace_id,
                    memory_text,
                    previous["created_at"] if previous else now,
                    now,
                ),
            )
        self.add_event(workspace_id, "memory.updated", {"characters": len(memory_text)})
        return memory_text

    def get_stage_guidance(self, workspace_id: str, stage: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM stage_guidance WHERE workspace_id=? AND stage=?",
                (workspace_id, stage),
            ).fetchone()
        return self._guidance(row) if row else None

    def list_stage_guidance(self, workspace_id: str) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM stage_guidance WHERE workspace_id=? ORDER BY updated_at DESC",
                (workspace_id,),
            ).fetchall()
        return {str(row["stage"]): self._guidance(row) for row in rows}

    def upsert_stage_guidance(
        self,
        workspace_id: str,
        stage: str,
        *,
        user_instruction: str,
        director_plan: dict[str, Any] | None = None,
        plan_input_hash: str = "",
    ) -> dict[str, Any]:
        now = utcnow()
        with self._lock, self.connect() as conn:
            previous = conn.execute(
                "SELECT created_at FROM stage_guidance WHERE workspace_id=? AND stage=?",
                (workspace_id, stage),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO stage_guidance
                (workspace_id, stage, user_instruction, director_plan_json,
                 plan_input_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, stage) DO UPDATE SET
                  user_instruction=excluded.user_instruction,
                  director_plan_json=excluded.director_plan_json,
                  plan_input_hash=excluded.plan_input_hash,
                  updated_at=excluded.updated_at
                """,
                (
                    workspace_id,
                    stage,
                    user_instruction,
                    json.dumps(director_plan or {}, ensure_ascii=False),
                    plan_input_hash,
                    previous["created_at"] if previous else now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM stage_guidance WHERE workspace_id=? AND stage=?",
                (workspace_id, stage),
            ).fetchone()
        return self._guidance(row)  # type: ignore[arg-type]

    def add_guidance_message(
        self,
        workspace_id: str,
        stage: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO guidance_messages
                (workspace_id, stage, role, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    stage,
                    role,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    utcnow(),
                ),
            )

    def list_guidance_messages(
        self, workspace_id: str, limit: int = 40
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM guidance_messages WHERE workspace_id=? ORDER BY id DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def set_stage_review(self, workspace_id: str, stage: str, artifact_revision: int, review: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self._lock, self.connect() as conn:
            previous = conn.execute("SELECT created_at FROM stage_reviews WHERE workspace_id=? AND stage=?", (workspace_id, stage)).fetchone()
            conn.execute("""INSERT INTO stage_reviews (workspace_id, stage, artifact_revision, review_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, stage) DO UPDATE SET artifact_revision=excluded.artifact_revision, review_json=excluded.review_json, updated_at=excluded.updated_at""",
                (workspace_id, stage, int(artifact_revision), json.dumps(review or {}, ensure_ascii=False), previous["created_at"] if previous else now, now))
            row = conn.execute("SELECT * FROM stage_reviews WHERE workspace_id=? AND stage=?", (workspace_id, stage)).fetchone()
        return self._stage_review(row)

    def get_stage_review(self, workspace_id: str, stage: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM stage_reviews WHERE workspace_id=? AND stage=?", (workspace_id, stage)).fetchone()
        return self._stage_review(row) if row else None

    def list_stage_reviews(self, workspace_id: str) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM stage_reviews WHERE workspace_id=? ORDER BY updated_at DESC", (workspace_id,)).fetchall()
        return {str(row["stage"]): self._stage_review(row) for row in rows}

    def clear_stage_reviews(self, workspace_id: str, stages: list[str]) -> None:
        if not stages: return
        placeholders = ",".join("?" for _ in stages)
        with self._lock, self.connect() as conn:
            conn.execute(f"DELETE FROM stage_reviews WHERE workspace_id=? AND stage IN ({placeholders})", (workspace_id, *stages))

    def create_series(self, name: str, theme: str = "", description: str = "") -> dict[str, Any]:
        series_id = str(uuid.uuid4()); now = utcnow()
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO production_series (id, name, theme, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (series_id, name.strip(), (theme or name).strip(), description.strip(), now, now),
            )
            row = conn.execute("SELECT * FROM production_series WHERE id=?", (series_id,)).fetchone()
        return dict(row)

    def list_series(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT s.*, COUNT(DISTINCT l.id) AS library_count, COUNT(DISTINCT i.id) AS asset_count "
                "FROM production_series s LEFT JOIN asset_libraries l ON l.series_id=s.id "
                "LEFT JOIN asset_items i ON i.library_id=l.id GROUP BY s.id ORDER BY s.updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_series(self, series_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM production_series WHERE id=?", (series_id,)).fetchone()
        return dict(row) if row else None

    def create_asset_library(
        self, name: str, theme: str, description: str = "", scope: str = "mixed",
        *, series_id: str = "", auto_managed: bool = False,
    ) -> dict[str, Any]:
        library_id = str(uuid.uuid4()); now = utcnow()
        scope = scope if scope in {"mixed", "character", "scene", "prop", "reference"} else "mixed"
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO asset_libraries (id, name, theme, scope, description, created_at, updated_at, series_id, auto_managed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (library_id, name.strip(), theme.strip(), scope, description.strip(), now, now, series_id, 1 if auto_managed else 0),
            )
            row = conn.execute("SELECT * FROM asset_libraries WHERE id=?", (library_id,)).fetchone()
        return dict(row)

    def ensure_series_libraries(self, series_id: str) -> list[dict[str, Any]]:
        series = self.get_series(series_id)
        if not series:
            return []
        specs = [
            ("character", "Characters", "系列固定人物、身份锚点、服装/状态变体与连续性规则"),
            ("scene", "Scenes", "系列固定地点、空间布局、时间/灯光变体与连续性规则"),
            ("prop", "Props", "系列关键道具、状态变体、剧情功能与连续性规则"),
        ]
        with self.connect() as conn:
            existing_rows = conn.execute(
                "SELECT * FROM asset_libraries WHERE series_id=?", (series_id,)
            ).fetchall()
        existing = {str(row["scope"]): dict(row) for row in existing_rows}
        result=[]
        for scope, suffix, description in specs:
            library = existing.get(scope)
            if not library:
                library = self.create_asset_library(
                    f"{series['name']} · {suffix}", str(series.get("theme") or series["name"]),
                    description, scope, series_id=series_id, auto_managed=True,
                )
            result.append(library)
        return result

    def attach_series_libraries(self, workspace_id: str, series_id: str) -> list[dict[str, Any]]:
        libraries = self.ensure_series_libraries(series_id)
        for library in libraries:
            self.link_workspace_asset_library(workspace_id, str(library["id"]))
        return libraries

    def list_asset_libraries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT l.*, COUNT(i.id) AS item_count FROM asset_libraries l LEFT JOIN asset_items i ON i.library_id=l.id GROUP BY l.id ORDER BY l.updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_asset_library(self, library_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM asset_libraries WHERE id=?", (library_id,)).fetchone()
        return dict(row) if row else None

    def list_series_asset_libraries(self, series_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT l.*, COUNT(i.id) AS item_count FROM asset_libraries l LEFT JOIN asset_items i ON i.library_id=l.id WHERE l.series_id=? GROUP BY l.id ORDER BY l.scope",
                (series_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_asset_library(self, library_id: str) -> bool:
        with self._lock, self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM asset_libraries WHERE id=?", (library_id,)).fetchone()
            if not exists: return False
            conn.execute("DELETE FROM workspace_asset_libraries WHERE library_id=?", (library_id,))
            conn.execute("DELETE FROM asset_items WHERE library_id=?", (library_id,))
            conn.execute("DELETE FROM asset_libraries WHERE id=?", (library_id,))
        return True

    def add_asset_item(
        self, library_id: str, asset_type: str, name: str, description: str = "",
        content: dict[str, Any] | None = None, file_path: str = "", preview_url: str = "",
        *, canonical_key: str = "", parent_item_id: str = "", variant_key: str = "",
        retrieval_text: str = "", source_workspace_id: str = "", source_artifact_kind: str = "",
        source_revision: int = 0, status: str = "approved",
    ) -> dict[str, Any]:
        item_id = str(uuid.uuid4()); now = utcnow()
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO asset_items (id, library_id, asset_type, name, description, content_json, file_path, preview_url, created_at, updated_at, canonical_key, parent_item_id, variant_key, retrieval_text, source_workspace_id, source_artifact_kind, source_revision, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, library_id, asset_type, name.strip(), description.strip(), json.dumps(content or {}, ensure_ascii=False), file_path, preview_url, now, now, canonical_key, parent_item_id, variant_key, retrieval_text, source_workspace_id, source_artifact_kind, int(source_revision or 0), status),
            )
            conn.execute("UPDATE asset_libraries SET updated_at=? WHERE id=?", (now, library_id))
            row = conn.execute("SELECT * FROM asset_items WHERE id=?", (item_id,)).fetchone()
        return self._asset_item(row)

    def update_asset_item(self, item_id: str, **values: Any) -> dict[str, Any] | None:
        allowed={"name","description","content_json","file_path","preview_url","canonical_key","parent_item_id","variant_key","retrieval_text","source_workspace_id","source_artifact_kind","source_revision","status"}
        changes={k:v for k,v in values.items() if k in allowed}
        if "content_json" in changes and not isinstance(changes["content_json"], str):
            changes["content_json"] = json.dumps(changes["content_json"] or {}, ensure_ascii=False)
        changes["updated_at"]=utcnow()
        with self._lock, self.connect() as conn:
            row=conn.execute("SELECT * FROM asset_items WHERE id=?",(item_id,)).fetchone()
            if not row: return None
            columns=", ".join(f"{key}=?" for key in changes)
            conn.execute(f"UPDATE asset_items SET {columns} WHERE id=?", (*changes.values(), item_id))
            conn.execute("UPDATE asset_libraries SET updated_at=? WHERE id=?", (changes["updated_at"], row["library_id"]))
            updated=conn.execute("SELECT * FROM asset_items WHERE id=?",(item_id,)).fetchone()
        return self._asset_item(updated)

    def find_asset_by_canonical_key(self, library_id: str, canonical_key: str) -> dict[str, Any] | None:
        if not canonical_key: return None
        with self.connect() as conn:
            row=conn.execute(
                "SELECT * FROM asset_items WHERE library_id=? AND canonical_key=? AND (variant_key='' OR variant_key IS NULL) ORDER BY updated_at DESC LIMIT 1",
                (library_id, canonical_key),
            ).fetchone()
        return self._asset_item(row) if row else None

    def find_asset_variant(self, library_id: str, canonical_key: str, variant_key: str) -> dict[str, Any] | None:
        if not canonical_key or not variant_key: return None
        with self.connect() as conn:
            row=conn.execute(
                "SELECT * FROM asset_items WHERE library_id=? AND canonical_key=? AND variant_key=? ORDER BY updated_at DESC LIMIT 1",
                (library_id, canonical_key, variant_key),
            ).fetchone()
        return self._asset_item(row) if row else None

    def list_asset_items(self, library_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM asset_items WHERE library_id=? ORDER BY updated_at DESC", (library_id,)).fetchall()
        return [self._asset_item(row) for row in rows]

    def delete_asset_item(self, item_id: str) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM asset_items WHERE id=?", (item_id,)).fetchone()
            if not row: return None
            item = self._asset_item(row)
            conn.execute("DELETE FROM asset_items WHERE id=?", (item_id,))
            conn.execute("UPDATE asset_libraries SET updated_at=? WHERE id=?", (utcnow(), row["library_id"]))
        return item

    def link_workspace_asset_library(self, workspace_id: str, library_id: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO workspace_asset_libraries (workspace_id, library_id, created_at) VALUES (?, ?, ?)", (workspace_id, library_id, utcnow()))

    def unlink_workspace_asset_library(self, workspace_id: str, library_id: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM workspace_asset_libraries WHERE workspace_id=? AND library_id=?", (workspace_id, library_id))

    def list_workspace_asset_libraries(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT l.*, COUNT(i.id) AS item_count FROM workspace_asset_libraries w JOIN asset_libraries l ON l.id=w.library_id LEFT JOIN asset_items i ON i.library_id=l.id WHERE w.workspace_id=? GROUP BY l.id ORDER BY w.created_at", (workspace_id,)).fetchall()
        return [dict(row) for row in rows]

    def workspace_asset_context(self, workspace_id: str, max_items_per_library: int = 80) -> list[dict[str, Any]]:
        result=[]
        for library in self.list_workspace_asset_libraries(workspace_id):
            items=self.list_asset_items(str(library["id"]))[:max_items_per_library]
            result.append({
                "id":library["id"],"name":library["name"],"theme":library["theme"],"scope":library.get("scope", "mixed"),
                "series_id":library.get("series_id", ""),"auto_managed":bool(library.get("auto_managed")),
                "description":library["description"],"updated_at":library["updated_at"],
                "items":[{
                    "id":i["id"],"asset_type":i["asset_type"],"name":i["name"],"description":i["description"],
                    "canonical_key":i.get("canonical_key", ""),"parent_item_id":i.get("parent_item_id", ""),
                    "variant_key":i.get("variant_key", ""),"retrieval_text":i.get("retrieval_text", ""),
                    "content":i["content"],"preview_url":i["preview_url"],"status":i.get("status", "approved"),
                } for i in items],
            })
        return result

    def list_activity_feed(self, workspace_id: str, limit: int = 180) -> list[dict[str, Any]]:
        feed=[]
        for item in self.list_guidance_messages(workspace_id, limit=limit):
            feed.append({"id":f"m-{item['id']}","kind":"message","stage":item.get("stage"),"role":item.get("role"),"content":item.get("content"),"metadata":item.get("metadata",{}),"created_at":item.get("created_at")})
        for item in reversed(self.list_events(workspace_id, limit=limit)):
            feed.append({"id":f"e-{item['id']}","kind":"event","type":item.get("type"),"payload":item.get("payload",{}),"created_at":item.get("created_at")})
        feed.sort(key=lambda x:(str(x.get("created_at") or ""),str(x.get("id"))))
        return feed[-limit:]

    def workspace_activity_token(self, workspace_id: str) -> str:
        with self.connect() as conn:
            rows=[conn.execute("SELECT MAX(id) AS value FROM events WHERE workspace_id=?",(workspace_id,)).fetchone(),conn.execute("SELECT MAX(id) AS value FROM guidance_messages WHERE workspace_id=?",(workspace_id,)).fetchone(),conn.execute("SELECT MAX(updated_at) AS value FROM jobs WHERE workspace_id=?",(workspace_id,)).fetchone(),conn.execute("SELECT MAX(updated_at) AS value FROM artifacts WHERE workspace_id=?",(workspace_id,)).fetchone()]
        return "|".join(str((row["value"] if row else "") or "") for row in rows)

    def add_event(self, workspace_id: str, event_type: str, payload: Any) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO events (workspace_id, type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (workspace_id, event_type, json.dumps(payload, ensure_ascii=False), utcnow()),
            )

    def list_events(self, workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE workspace_id=? ORDER BY id DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
        return [
            {
                **dict(row),
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json"))
        request_json = item.pop("request_json", "{}")
        try:
            item["request"] = json.loads(request_json or "{}")
        except (TypeError, ValueError):
            item["request"] = {}
        return item

    @staticmethod
    def _guidance(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["director_plan"] = json.loads(item.pop("director_plan_json"))
        except (TypeError, ValueError):
            item["director_plan"] = {}
        return item

    @staticmethod
    def _stage_review(row: sqlite3.Row) -> dict[str, Any]:
        item=dict(row)
        try: item["review"]=json.loads(item.pop("review_json"))
        except (TypeError,ValueError): item["review"]={}
        return item

    @staticmethod
    def _asset_item(row: sqlite3.Row) -> dict[str, Any]:
        item=dict(row)
        try: item["content"]=json.loads(item.pop("content_json"))
        except (TypeError,ValueError): item["content"]={}
        return item

    @staticmethod
    def _workspace(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["settings"] = json.loads(item.pop("settings_json"))
        return item

    @staticmethod
    def _artifact(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["content"] = json.loads(item.pop("content_json"))
        item["upstream"] = json.loads(item.pop("upstream_json"))
        return item

