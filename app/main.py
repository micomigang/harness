from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agents import AGENT_SPECS, STAGE_AGENTS, STAGE_GUIDANCE_HINTS
from .config import settings
from .db import Database
from .orchestrator import GATES, STAGE_LABELS, STAGES, OrchestrationError, Orchestrator


app = FastAPI(title="爆款复制 Harness", version="0.4.0")
db = Database(settings.data_dir / "harness.db")
db.recover_interrupted_jobs()
orchestrator = Orchestrator(db)
static_dir = settings.root_dir / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount(
    "/media",
    StaticFiles(directory=settings.data_dir / "outputs"),
    name="media",
)
_source_upload_dir = settings.data_dir / "uploads"
_source_upload_dir.mkdir(parents=True, exist_ok=True)
app.mount(
    "/source-media",
    StaticFiles(directory=_source_upload_dir),
    name="source-media",
)
_asset_library_dir = settings.data_dir / "asset_library"
_asset_library_dir.mkdir(parents=True, exist_ok=True)
app.mount(
    "/asset-media",
    StaticFiles(directory=_asset_library_dir),
    name="asset-media",
)


@app.middleware("http")
async def frontend_no_cache(request, call_next):
    """Keep HTML/JS/CSS versions in sync during rapid local Harness upgrades."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


class WorkspaceCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(min_length=1, max_length=20000)
    aspect_ratio: str = "9:16"
    target_language: str = "French (France)"
    target_market: str = "France"
    project_memory: str = ""
    storyboard_count: int = Field(default=17, ge=1, le=100)
    series_id: str = ""
    series_name: str = ""
    episode_key: str = ""


class ApprovalUpdate(BaseModel):
    status: Literal["pending", "approved", "rejected"]
    note: str = ""
    force: bool = False


class ArtifactRevision(BaseModel):
    content: dict[str, Any]
    cascade: bool = True


class StageRunRequest(BaseModel):
    user_instruction: str = Field(default="", max_length=12000)
    director_plan_hash: str = Field(default="", max_length=128)


class DirectorChatRequest(BaseModel):
    message: str = Field(default="", max_length=12000)


class StageGuidanceUpdate(BaseModel):
    user_instruction: str = Field(default="", max_length=12000)


class StagePlanRequest(BaseModel):
    user_instruction: str | None = Field(default=None, max_length=12000)
    force: bool = False


class StageRegenerationRequest(BaseModel):
    feedback: str = Field(min_length=1, max_length=12000)


class AssetCandidateGenerateRequest(BaseModel):
    stage: Literal["characters", "scenes", "props", "reference_images"]
    canonical_key: str = Field(min_length=1, max_length=240)
    feedback: str = Field(default="", max_length=4000)
    base_candidate_id: str = Field(default="", max_length=128)
    count: int = Field(default=1, ge=1, le=4)


class AssetCandidateBatchGenerateRequest(BaseModel):
    stage: Literal["characters", "scenes", "props", "reference_images"]
    canonical_keys: list[str] = Field(min_length=1, max_length=200)
    feedback: str = Field(default="", max_length=4000)
    count: int = Field(default=1, ge=1, le=4)


class WorkspaceMemoryUpdate(BaseModel):
    memory: str = Field(default="", max_length=30000)


class AssetLibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    theme: str = Field(min_length=1, max_length=120)
    scope: Literal["mixed", "character", "scene", "prop", "reference"] = "mixed"
    description: str = Field(default="", max_length=4000)


class SeriesCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    theme: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4000)


class WorkspaceSeriesUpdate(BaseModel):
    series_id: str = Field(min_length=1, max_length=128)
    episode_key: str = Field(default="", max_length=120)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    capabilities = {
        "kimi_text": bool(settings.openai_api_key and settings.openai_model),
        "local_source_extract": Path(settings.ffmpeg_path).is_file(),
        "seedream_reference_images": bool(
            settings.image_api_key and settings.image_model
        ),
        "seedance_video": bool(settings.video_api_key and settings.video_model),
        "ffmpeg_compose": Path(settings.ffmpeg_path).is_file(),
        "french_tts": bool(
            settings.tts_app_id
            and settings.tts_access_token
            and settings.tts_voice_type
        ),
        "source_asr": bool(
            settings.asr_api_key
            or (settings.asr_app_id and settings.asr_access_token)
        ),
    }
    return {
        "status": "ok",
        "provider": settings.provider,
        "data_dir": str(settings.data_dir),
        "stages": STAGES,
        "capabilities": capabilities,
    }


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    return {
        "stages": STAGES,
        "labels": STAGE_LABELS,
        "gates": GATES,
        "stage_agents": STAGE_AGENTS,
        "agents": [agent.as_dict() for agent in AGENT_SPECS],
        "guidance_hints": STAGE_GUIDANCE_HINTS,
        "reset_impacts": {
            stage: orchestrator.downstream_stages(stage, include_self=True)
            for stage in STAGES
            if stage != "source"
        },
    }


@app.post("/api/workspaces")
def create_workspace(payload: WorkspaceCreate) -> dict[str, Any]:
    series_id = payload.series_id.strip()
    if not series_id and payload.series_name.strip():
        created_series = db.create_series(
            payload.series_name.strip(), payload.series_name.strip(),
            f"由工作区 {payload.title} 创建的系列资产与连续性 Bible",
        )
        series_id = str(created_series["id"])
    if series_id and not db.get_series(series_id):
        raise HTTPException(400, "Series not found")
    workspace = db.create_workspace(
        payload.title,
        payload.brief,
        {
            "aspect_ratio": payload.aspect_ratio,
            "target_language": payload.target_language,
            "target_market": payload.target_market,
            "storyboard_count": payload.storyboard_count,
            "series_id": series_id,
            "episode_key": payload.episode_key.strip(),
        },
    )
    if series_id:
        db.attach_series_libraries(workspace["id"], series_id)
    if payload.project_memory.strip():
        db.set_workspace_memory(workspace["id"], payload.project_memory.strip())
    orchestrator.bootstrap_workspace(workspace["id"])
    return workspace_detail(workspace["id"])


@app.get("/api/workspaces")
def list_workspaces() -> list[dict[str, Any]]:
    return db.list_workspaces()


@app.delete("/api/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    active = db.list_active_jobs(workspace_id)
    if active:
        current = active[0]
        raise HTTPException(
            409,
            f"工作区仍有进行中的任务：{current['stage']} ({current['status']})，请等待任务结束后再清空",
        )
    deleted = db.delete_workspace(workspace_id)
    if not deleted:
        raise HTTPException(404, "Workspace not found")
    for root_name in ("uploads", "outputs"):
        target = settings.data_dir / root_name / workspace_id
        shutil.rmtree(target, ignore_errors=True)
    return {"status": "deleted", "workspace_id": workspace_id}


@app.delete("/api/workspaces/{workspace_id}/jobs")
def clear_job_history(workspace_id: str) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    removed = db.clear_terminal_jobs(workspace_id)
    return {"status": "ok", "removed": removed}


@app.get("/api/workspaces/{workspace_id}")
def workspace_detail(workspace_id: str) -> dict[str, Any]:
    workspace = db.get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")
    series_id = str((workspace.get("settings") or {}).get("series_id") or "")
    return {
        "workspace": workspace,
        "series": db.get_series(series_id) if series_id else None,
        "artifacts": db.list_artifacts(workspace_id),
        "approvals": db.list_approvals(workspace_id),
        "jobs": db.list_jobs(workspace_id),
        "events": db.list_events(workspace_id),
        "project_memory": db.get_workspace_memory(workspace_id),
        "guidance": db.list_stage_guidance(workspace_id),
        "guidance_messages": db.list_guidance_messages(workspace_id, limit=60),
        "stage_reviews": db.list_stage_reviews(workspace_id),
        "asset_libraries": db.list_workspace_asset_libraries(workspace_id),
        "asset_library_context": db.workspace_asset_context(workspace_id),
        "asset_candidates": db.list_asset_candidates(workspace_id, limit=500),
        "activity_feed": db.list_activity_feed(workspace_id, limit=220),
        "next_actions": orchestrator.next_actions(workspace_id),
    }


@app.get("/api/workspaces/{workspace_id}/stream")
async def workspace_stream(workspace_id: str) -> StreamingResponse:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")

    async def events():
        last = ""
        while True:
            if not db.get_workspace(workspace_id):
                yield "event: deleted\ndata: {}\n\n"
                return
            token = db.workspace_activity_token(workspace_id)
            if token != last:
                last = token
                yield "data: " + json.dumps({"type": "refresh", "token": token}, ensure_ascii=False) + "\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.8)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/series")
def list_series() -> list[dict[str, Any]]:
    return db.list_series()


@app.post("/api/series")
def create_series(payload: SeriesCreate) -> dict[str, Any]:
    series = db.create_series(payload.name, payload.theme or payload.name, payload.description)
    libraries = db.ensure_series_libraries(str(series["id"]))
    return {**series, "libraries": libraries}


@app.put("/api/workspaces/{workspace_id}/series")
def set_workspace_series(workspace_id: str, payload: WorkspaceSeriesUpdate) -> dict[str, Any]:
    workspace = db.get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")
    if not db.get_series(payload.series_id):
        raise HTTPException(404, "Series not found")
    settings_json = dict(workspace.get("settings") or {})
    settings_json["series_id"] = payload.series_id
    settings_json["episode_key"] = payload.episode_key.strip()
    db.update_workspace(workspace_id, settings_json=json.dumps(settings_json, ensure_ascii=False))
    libraries = db.attach_series_libraries(workspace_id, payload.series_id)
    db.add_event(workspace_id, "series.attached", {"series_id": payload.series_id, "library_count": len(libraries)})
    return workspace_detail(workspace_id)


@app.get("/api/asset-libraries")
def list_asset_libraries() -> list[dict[str, Any]]:
    return db.list_asset_libraries()


@app.post("/api/asset-libraries")
def create_asset_library(payload: AssetLibraryCreate) -> dict[str, Any]:
    return db.create_asset_library(payload.name, payload.theme, payload.description, payload.scope)


@app.get("/api/asset-libraries/{library_id}/items")
def list_asset_library_items(library_id: str) -> list[dict[str, Any]]:
    if not db.get_asset_library(library_id):
        raise HTTPException(404, "Asset library not found")
    return db.list_asset_items(library_id)


@app.delete("/api/asset-libraries/{library_id}/items/{item_id}")
def delete_asset_library_item(library_id: str, item_id: str) -> dict[str, Any]:
    library = db.get_asset_library(library_id)
    if not library:
        raise HTTPException(404, "Asset library not found")
    candidate = next((item for item in db.list_asset_items(library_id) if str(item.get("id")) == item_id), None)
    if not candidate:
        raise HTTPException(404, "Asset item not found")
    item = db.delete_asset_item(item_id)
    file_path = str(item.get("file_path") or "")
    if file_path:
        Path(file_path).unlink(missing_ok=True)
    return {"status": "deleted", "item_id": item_id}


@app.delete("/api/asset-libraries/{library_id}")
def delete_asset_library(library_id: str) -> dict[str, Any]:
    library = db.get_asset_library(library_id)
    if not library:
        raise HTTPException(404, "Asset library not found")
    if bool(library.get("auto_managed")):
        raise HTTPException(409, "系列自动资产库由 Series Bible 管理，不能直接删除；如需停用请调整工作区所属系列")
    for item in db.list_asset_items(library_id):
        file_path = str(item.get("file_path") or "")
        if file_path:
            Path(file_path).unlink(missing_ok=True)
    db.delete_asset_library(library_id)
    shutil.rmtree(_asset_library_dir / library_id, ignore_errors=True)
    return {"status": "deleted", "library_id": library_id}


@app.post("/api/workspaces/{workspace_id}/asset-libraries/{library_id}")
def attach_asset_library(workspace_id: str, library_id: str) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    if not db.get_asset_library(library_id):
        raise HTTPException(404, "Asset library not found")
    db.link_workspace_asset_library(workspace_id, library_id)
    db.add_event(workspace_id, "asset_library.attached", {"library_id": library_id})
    return {"status": "attached", "library_id": library_id}


@app.delete("/api/workspaces/{workspace_id}/asset-libraries/{library_id}")
def detach_asset_library(workspace_id: str, library_id: str) -> dict[str, Any]:
    workspace = db.get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")
    library = db.get_asset_library(library_id)
    if not library:
        raise HTTPException(404, "Asset library not found")
    current_series = str((workspace.get("settings") or {}).get("series_id") or "")
    if bool(library.get("auto_managed")) and str(library.get("series_id") or "") == current_series:
        raise HTTPException(409, "当前系列的自动资产库必须保持挂载，以保证跨集连续性")
    db.unlink_workspace_asset_library(workspace_id, library_id)
    db.add_event(workspace_id, "asset_library.detached", {"library_id": library_id})
    return {"status": "detached", "library_id": library_id}


def _asset_type(value: str) -> str:
    allowed = {"character", "scene", "prop", "reference", "audio", "video", "document", "other"}
    text = str(value or "").strip().lower()
    return text if text in allowed else "other"


async def _save_asset_uploads(files: list[UploadFile]) -> tuple[Path, list[dict[str, Any]]]:
    temp_root = settings.data_dir / "asset_upload_tmp" / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=True)
    collected: list[dict[str, Any]] = []
    total = 0
    try:
        for upload in files:
            safe_name = Path(upload.filename or "asset.bin").name
            target = temp_root / (uuid.uuid4().hex + "-" + safe_name)
            with target.open("wb") as out:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > 2 * 1024 * 1024 * 1024:
                        raise HTTPException(413, "Asset upload exceeds 2 GiB limit")
                    out.write(chunk)
            await upload.close()
            if safe_name.lower().endswith(".zip"):
                with zipfile.ZipFile(target) as archive:
                    for info in archive.infolist()[:200]:
                        if info.is_dir() or info.file_size > 512 * 1024 * 1024:
                            continue
                        inner_name = Path(info.filename).name
                        if not inner_name or inner_name.startswith("."):
                            continue
                        extracted = temp_root / (uuid.uuid4().hex + "-" + inner_name)
                        with archive.open(info) as src, extracted.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        collected.append({"name": inner_name, "path": extracted, "size": info.file_size, "from_pack": safe_name})
                target.unlink(missing_ok=True)
            else:
                collected.append({"name": safe_name, "path": target, "size": target.stat().st_size, "from_pack": ""})
        if not collected:
            raise HTTPException(400, "No usable files found in upload")
        return temp_root, collected
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


@app.post("/api/workspaces/{workspace_id}/asset-libraries/ingest")
async def ingest_asset_library_files(
    workspace_id: str,
    files: list[UploadFile] = File(...),
    instruction: str = Form(""),
    library_id: str = Form(""),
    asset_type: str = Form(""),
) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    temp_root, collected = await _save_asset_uploads(files)
    try:
        libraries = db.list_asset_libraries()
        selected = db.get_asset_library(library_id) if library_id else None
        routing: dict[str, Any] = {}
        planner = getattr(orchestrator.providers.workflow(), "llm", orchestrator.providers.workflow())
        route_method = getattr(planner, "plan_asset_ingest", None)
        if callable(route_method):
            try:
                routing = route_method({
                    "user_instruction": instruction,
                    "selected_library_id": library_id,
                    "selected_asset_type": asset_type,
                    "available_libraries": [{k: lib.get(k) for k in ("id", "name", "theme", "scope", "description", "item_count")} for lib in libraries],
                    "files": [{"name": item["name"], "size": item["size"], "from_pack": item["from_pack"], "local_path": str(item["path"])} for item in collected],
                })
            except Exception as exc:
                routing = {"reply": f"总管资源归档解析失败，使用显式选择：{exc}"}
        if selected is None:
            routed_id = str(routing.get("library_id") or "")
            selected = db.get_asset_library(routed_id) if routed_id else None
        if selected is None and routing.get("create_library"):
            name = str(routing.get("new_library_name") or routing.get("new_library_theme") or "新主题资产库").strip()
            theme = str(routing.get("new_library_theme") or name).strip()
            suggested_scope = _asset_type(asset_type or routing.get("default_asset_type") or "other")
            scope = suggested_scope if suggested_scope in {"character", "scene", "prop", "reference"} else "mixed"
            selected = db.create_asset_library(name, theme, str(routing.get("new_library_description") or ""), scope)
        if selected is None:
            raise HTTPException(400, "请选择资产库，或在要求中明确告诉总管要归档到哪个主题资产库")

        library_dir = _asset_library_dir / str(selected["id"])
        library_dir.mkdir(parents=True, exist_ok=True)
        per_types = routing.get("per_file_types") if isinstance(routing.get("per_file_types"), dict) else {}
        per_metadata = routing.get("per_file_metadata") if isinstance(routing.get("per_file_metadata"), dict) else {}
        default_type = _asset_type(asset_type or routing.get("default_asset_type") or "other")
        items = []
        for item in collected:
            stored_name = uuid.uuid4().hex + "-" + Path(item["name"]).name
            destination = library_dir / stored_name
            shutil.move(str(item["path"]), destination)
            item_type = _asset_type(asset_type or per_types.get(item["name"]) or default_type)
            preview_url = f"/asset-media/{quote(str(selected['id']))}/{quote(stored_name)}"
            metadata = per_metadata.get(item["name"]) if isinstance(per_metadata.get(item["name"]), dict) else {}
            items.append(db.add_asset_item(
                str(selected["id"]), item_type, item["name"],
                description=str(metadata.get("description") or instruction.strip()),
                content={"tags": metadata.get("tags") or routing.get("tags", []), "continuity_notes": metadata.get("continuity_notes", ""), "routing_notes": routing.get("notes", ""), "source_pack": item["from_pack"]},
                file_path=str(destination), preview_url=preview_url,
            ))
        db.link_workspace_asset_library(workspace_id, str(selected["id"]))
        db.add_guidance_message(workspace_id, "source", "director", str(routing.get("reply") or f"已将 {len(items)} 个资源归档到主题资产库“{selected['name']}”。"), {"kind": "asset_ingest", "library_id": selected["id"], "item_count": len(items)})
        db.add_event(workspace_id, "asset_library.ingested", {"library_id": selected["id"], "item_count": len(items)})
        return {"library": selected, "items": items, "routing": routing}
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


@app.post("/api/workspaces/{workspace_id}/asset-libraries/{library_id}/import/{kind}")
def import_artifact_assets(workspace_id: str, library_id: str, kind: str) -> dict[str, Any]:
    if kind not in {"characters", "scenes", "props", "reference_images"}:
        raise HTTPException(400, "Only character/scene/prop/reference assets can be stored")
    artifact = db.get_artifact(workspace_id, kind)
    library = db.get_asset_library(library_id)
    if not artifact or not library:
        raise HTTPException(404, "Artifact or asset library not found")
    source_items = artifact.get("content", {}).get("items", [])
    if not isinstance(source_items, list):
        raise HTTPException(400, "Artifact has no reusable items")
    type_map = {"characters": "character", "scenes": "scene", "props": "prop", "reference_images": "reference"}
    added=[]
    for index, item in enumerate(source_items):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("id") or item.get("source_id") or f"{kind}-{index+1}")
        file_path = ""
        preview_url = str(item.get("url") or "")
        local_path = Path(str(item.get("local_path") or ""))
        if kind == "reference_images" and local_path.is_file():
            library_dir = _asset_library_dir / library_id
            library_dir.mkdir(parents=True, exist_ok=True)
            stored_name = uuid.uuid4().hex + local_path.suffix.lower()
            destination = library_dir / stored_name
            shutil.copy2(local_path, destination)
            file_path = str(destination)
            preview_url = f"/asset-media/{quote(library_id)}/{quote(stored_name)}"
        added.append(db.add_asset_item(library_id, type_map[kind], name, description=str(item.get("description") or item.get("continuity") or ""), content=item, file_path=file_path, preview_url=preview_url))
    db.link_workspace_asset_library(workspace_id, library_id)
    db.add_event(workspace_id, "asset_library.imported_artifact", {"library_id": library_id, "kind": kind, "item_count": len(added)})
    return {"library": library, "items": added}


def _current_stage_asset(workspace_id: str, stage: str, canonical_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if stage not in {"characters", "scenes", "props", "reference_images"}:
        raise HTTPException(400, "Visual candidate workbench only supports characters/scenes/props/reference_images")
    artifact = db.get_artifact(workspace_id, stage)
    if not artifact or artifact.get("status") != "ready":
        raise HTTPException(409, f"{STAGE_LABELS.get(stage, stage)} artifact is not ready")
    items = (artifact.get("content") or {}).get("items") or []
    item = next(
        (
            candidate for candidate in items
            if isinstance(candidate, dict)
            and str(candidate.get("canonical_key") or candidate.get("id") or "") == canonical_key
        ),
        None,
    )
    if not item:
        raise HTTPException(404, "Asset item not found in the current artifact revision")
    return artifact, item


def _current_stage_asset_candidates_base(workspace_id: str, stage: str, canonical_key: str, artifact: dict[str, Any], base_candidate_id: str = "") -> dict[str, Any] | None:
    base = None
    if base_candidate_id:
        base = db.get_asset_candidate(base_candidate_id)
        if not base or str(base.get("workspace_id") or "") != workspace_id:
            raise HTTPException(404, "Base candidate not found")
        if str(base.get("stage") or "") != stage or str(base.get("canonical_key") or "") != canonical_key:
            raise HTTPException(400, "Base candidate belongs to another asset")
    if base is None:
        selected = db.list_asset_candidates(
            workspace_id,
            stage=stage,
            canonical_key=canonical_key,
            artifact_revision=int(artifact.get("revision") or 0),
            selected_only=True,
            limit=1,
        )
        base = selected[0] if selected else None
    return base


def _reference_artifact_base(item: dict[str, Any]) -> dict[str, Any] | None:
    if not item.get("url"):
        return None
    return {
        "id": "",
        "remote_url": str(item.get("remote_url") or ""),
        "url": str(item.get("url") or ""),
        "local_path": str(item.get("local_path") or ""),
        "model": str(item.get("model") or ""),
    }


def _asset_candidate_localization_context(workspace_id: str) -> dict[str, Any]:
    workspace = db.get_workspace(workspace_id) or {}
    settings_json = workspace.get("settings") if isinstance(workspace.get("settings"), dict) else {}
    script_artifact = db.get_artifact(workspace_id, "script")
    script_content = (script_artifact or {}).get("content") if isinstance(script_artifact, dict) else {}
    if not isinstance(script_content, dict):
        script_content = {}
    return {
        "target_market": str(settings_json.get("target_market") or script_content.get("target_market") or "").strip(),
        "target_language": str(settings_json.get("target_language") or script_content.get("language") or "").strip(),
        "localization_strategy": str(script_content.get("localization_strategy") or "").strip(),
        "adaptation_notes": script_content.get("adaptation_notes") or [],
        "localization_map": script_content.get("localization_map") or [],
    }


def _generate_asset_candidate_images(
    workspace_id: str,
    stage: str,
    artifact: dict[str, Any],
    item: dict[str, Any],
    feedback: str,
    count: int,
    base: dict[str, Any] | None,
    localization_context: dict[str, Any],
) -> dict[str, Any]:
    """Generate up to ``count`` candidate images and preserve partial success.

    Image generation is intentionally one request per candidate.  A later request
    can fail after earlier candidates were already downloaded and persisted.  Do
    not throw those successful candidates away just because the final request
    failed; return a partial result so the UI can show them immediately.
    """
    workflow = orchestrator.providers.workflow()
    image_provider = getattr(workflow, "image", None)
    generate_method = getattr(image_provider, "generate_candidate", None)
    if not callable(generate_method):
        raise HTTPException(409, "当前 Provider 未配置可交互的 Seedream 图片生成能力")

    created: list[dict[str, Any]] = []
    error = ""
    for _ in range(count):
        try:
            generated = generate_method(
                workspace_id=workspace_id,
                source_kind=stage,
                item=item,
                feedback=feedback,
                reference_url=str((base or {}).get("remote_url") or ""),
                reference_local_path=str((base or {}).get("local_path") or ""),
                localization_context=localization_context,
            )
        except Exception as exc:
            error = str(exc)
            break
        created.append(
            db.add_asset_candidate(
                workspace_id,
                stage,
                int(artifact.get("revision") or 0),
                str(item.get("canonical_key") or item.get("id") or ""),
                str(item.get("id") or item.get("canonical_key") or ""),
                feedback=feedback.strip(),
                base_candidate_id=str((base or {}).get("id") or ""),
                prompt=str(generated.get("prompt") or ""),
                model=str(generated.get("model") or ""),
                remote_url=str(generated.get("remote_url") or ""),
                url=str(generated.get("url") or ""),
                local_path=str(generated.get("local_path") or ""),
            )
        )

    status = "succeeded" if len(created) == count else ("partial" if created else "failed")
    return {
        "items": created,
        "requested_count": int(count),
        "completed_count": len(created),
        "status": status,
        "error": error,
    }


@app.post("/api/workspaces/{workspace_id}/asset-candidates")
def generate_asset_candidates(
    workspace_id: str,
    payload: AssetCandidateGenerateRequest,
) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    artifact, item = _current_stage_asset(workspace_id, payload.stage, payload.canonical_key)
    base = _current_stage_asset_candidates_base(workspace_id, payload.stage, payload.canonical_key, artifact, payload.base_candidate_id)
    if base is None and payload.stage == "reference_images":
        base = _reference_artifact_base(item)
    localization_context = _asset_candidate_localization_context(workspace_id)
    result = _generate_asset_candidate_images(
        workspace_id,
        payload.stage,
        artifact,
        item,
        payload.feedback,
        payload.count,
        base,
        localization_context,
    )
    created = result["items"]
    if not created and result.get("error"):
        raise HTTPException(502, str(result["error"]))
    db.add_event(
        workspace_id,
        "asset_candidate.generated",
        {
            "stage": payload.stage,
            "canonical_key": payload.canonical_key,
            "artifact_revision": int(artifact.get("revision") or 0),
            "count": len(created),
            "requested_count": int(payload.count),
            "status": result.get("status"),
            "base_candidate_id": str((base or {}).get("id") or ""),
            "has_feedback": bool(payload.feedback.strip()),
            "error": str(result.get("error") or "")[:500],
        },
    )
    return {
        **result,
        "artifact_revision": int(artifact.get("revision") or 0),
    }


@app.post("/api/workspaces/{workspace_id}/asset-candidates/batch")
def generate_asset_candidates_batch(
    workspace_id: str,
    payload: AssetCandidateBatchGenerateRequest,
) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    cleaned_keys: list[str] = []
    seen: set[str] = set()
    for raw in payload.canonical_keys:
        key = str(raw or "").strip()
        if not key or key in seen:
            continue
        cleaned_keys.append(key)
        seen.add(key)
    if not cleaned_keys:
        raise HTTPException(400, "No asset keys selected")
    localization_context = _asset_candidate_localization_context(workspace_id)
    items_by_key: dict[str, list[dict[str, Any]]] = {}
    artifact_revision = 0
    for canonical_key in cleaned_keys:
        artifact, item = _current_stage_asset(workspace_id, payload.stage, canonical_key)
        artifact_revision = int(artifact.get("revision") or artifact_revision or 0)
        base = _current_stage_asset_candidates_base(workspace_id, payload.stage, canonical_key, artifact, "")
        if base is None and payload.stage == "reference_images":
            base = _reference_artifact_base(item)
        result = _generate_asset_candidate_images(
            workspace_id,
            payload.stage,
            artifact,
            item,
            payload.feedback,
            payload.count,
            base,
            localization_context,
        )
        items_by_key[canonical_key] = result["items"]
    db.add_event(
        workspace_id,
        "asset_candidate.batch_generated",
        {
            "stage": payload.stage,
            "canonical_keys": cleaned_keys,
            "artifact_revision": artifact_revision,
            "count_per_asset": int(payload.count),
            "asset_count": len(cleaned_keys),
            "has_feedback": bool(payload.feedback.strip()),
        },
    )
    return {
        "items_by_key": items_by_key,
        "artifact_revision": artifact_revision,
        "asset_count": len(cleaned_keys),
        "count_per_asset": int(payload.count),
    }


@app.post("/api/workspaces/{workspace_id}/asset-candidates/{candidate_id}/select")
def select_asset_candidate(workspace_id: str, candidate_id: str) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    candidate = db.get_asset_candidate(candidate_id)
    if not candidate or str(candidate.get("workspace_id") or "") != workspace_id:
        raise HTTPException(404, "Asset candidate not found")
    artifact, _ = _current_stage_asset(
        workspace_id,
        str(candidate.get("stage") or ""),
        str(candidate.get("canonical_key") or ""),
    )
    if int(candidate.get("artifact_revision") or 0) != int(artifact.get("revision") or 0):
        raise HTTPException(409, "该候选图属于旧版本资产，请在当前 revision 重新抽图")
    selected = db.select_asset_candidate(workspace_id, candidate_id)
    invalidated = orchestrator.invalidate_downstream(workspace_id, str(candidate.get("stage") or ""))
    if invalidated:
        db.clear_stage_reviews(workspace_id, invalidated)
        for gate_stage, gate_name in GATES.items():
            if gate_stage in invalidated:
                db.set_approval(workspace_id, gate_name, "pending", "视觉候选已重新选定")
    db.add_event(
        workspace_id,
        "asset_candidate.selected",
        {
            "stage": candidate.get("stage"),
            "canonical_key": candidate.get("canonical_key"),
            "candidate_id": candidate_id,
            "downstream_invalidated": invalidated,
        },
    )
    return {"candidate": selected, "downstream_invalidated": invalidated}


@app.delete("/api/workspaces/{workspace_id}/asset-candidates/{candidate_id}")
def delete_asset_candidate(workspace_id: str, candidate_id: str) -> dict[str, Any]:
    existing = db.get_asset_candidate(candidate_id)
    if not existing or str(existing.get("workspace_id") or "") != workspace_id:
        raise HTTPException(404, "Asset candidate not found")
    if existing.get("selected"):
        raise HTTPException(409, "这张候选图正在被下游采用；请先选择另一张再删除")
    candidate = db.delete_asset_candidate(workspace_id, candidate_id)
    if not candidate:
        raise HTTPException(404, "Asset candidate not found")
    local_path = Path(str(candidate.get("local_path") or ""))
    if local_path.is_file():
        local_path.unlink(missing_ok=True)
    db.add_event(
        workspace_id,
        "asset_candidate.deleted",
        {"stage": candidate.get("stage"), "canonical_key": candidate.get("canonical_key"), "candidate_id": candidate_id},
    )
    return {"status": "deleted", "candidate_id": candidate_id}


@app.post("/api/workspaces/{workspace_id}/run/{stage}")
def run_stage(
    workspace_id: str,
    stage: str,
    background_tasks: BackgroundTasks,
    payload: StageRunRequest | None = None,
) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    if stage not in STAGES or stage == "source":
        raise HTTPException(400, "Unknown or non-runnable stage")
    instruction = (payload.user_instruction if payload else "").strip()
    plan_hash = (payload.director_plan_hash if payload else "").strip()
    try:
        bound = orchestrator.validate_bound_plan(workspace_id, stage, plan_hash)
    except OrchestrationError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not bound["valid"]:
        raise HTTPException(
            409,
            "请先在右侧与总管确认本节点要求，再推进流程；当前总管计划不存在或已因上游变化失效",
        )
    effective_instruction = str(
        (bound.get("guidance") or {}).get("user_instruction") or instruction
    ).strip()
    acquired = db.acquire_job(
        workspace_id,
        stage,
        request={
            "user_instruction": effective_instruction,
            "director_plan_hash": plan_hash,
        },
    )
    job = acquired["job"]
    if acquired["created"]:
        background_tasks.add_task(orchestrator.execute_job, job["id"])
        return {**job, "reused": False, "reason": acquired["reason"]}

    if acquired["reason"] == "same_stage_active":
        message = f"已存在进行中的同阶段任务，复用任务 {job['id']}"
    else:
        message = (
            f"工作区已有进行中的任务：{job['stage']}（{job['id']}），"
            "为避免重复触发，本次未新建任务"
        )
    return {**job, "reused": True, "reason": acquired["reason"], "message": message}


@app.post("/api/workspaces/{workspace_id}/stages/{stage}/regenerate")
def regenerate_stage(
    workspace_id: str,
    stage: str,
    background_tasks: BackgroundTasks,
    payload: StageRegenerationRequest,
) -> dict[str, Any]:
    """Apply user feedback, invalidate downstream state, and enqueue a fresh revision.

    The front-end has used this endpoint since the chat-first regeneration flow was
    introduced.  Keep preparation and execution in one request so the user cannot
    end up with a stale current artifact but no replacement job.
    """
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    if stage not in STAGES or stage == "source":
        raise HTTPException(400, "Unknown or non-regeneratable stage")

    try:
        prepared = orchestrator.prepare_stage_regeneration(
            workspace_id, stage, payload.feedback
        )
    except OrchestrationError as exc:
        raise HTTPException(409, str(exc)) from exc

    acquired = db.acquire_job(
        workspace_id,
        stage,
        request={
            "user_instruction": str(prepared.get("effective_instruction") or ""),
            "director_plan_hash": str(prepared.get("input_hash") or ""),
            "interaction_mode": "regenerate_current",
            "previous_revision": prepared.get("previous_revision"),
        },
    )
    job = acquired["job"]
    if acquired["created"]:
        background_tasks.add_task(orchestrator.execute_job, job["id"])

    return {
        **job,
        "reused": not acquired["created"],
        "reason": acquired["reason"],
        "director_reply": str(prepared.get("reply") or ""),
        "previous_revision": prepared.get("previous_revision"),
        "downstream_cleared": prepared.get("downstream_cleared", []),
        "reset_gates": prepared.get("reset_gates", []),
        "memory_preserved": bool(prepared.get("memory_preserved")),
        "conversation_preserved": bool(prepared.get("conversation_preserved")),
    }


@app.post("/api/workspaces/{workspace_id}/guidance/{stage}/chat")
def chat_with_director(
    workspace_id: str, stage: str, payload: DirectorChatRequest
) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    if db.list_active_jobs(workspace_id):
        raise HTTPException(409, "工作区已有运行中任务，请等待结束后再调整总管要求")
    try:
        return orchestrator.chat_stage(workspace_id, stage, payload.message)
    except OrchestrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/workspaces/{workspace_id}/stages/{stage}")
def clear_stage_region(workspace_id: str, stage: str) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    try:
        result = orchestrator.reset_stage(workspace_id, stage)
    except OrchestrationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if stage == "analysis":
        # Regenerating analysis should call Kimi again, but keep expensive FFmpeg
        # source-frame extraction cache intact.
        shutil.rmtree(
            settings.data_dir / "outputs" / workspace_id / "analysis_cache",
            ignore_errors=True,
        )
        result["analysis_cache_cleared"] = True
    return result


@app.put("/api/workspaces/{workspace_id}/memory")
def update_workspace_memory(
    workspace_id: str, payload: WorkspaceMemoryUpdate
) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    memory = db.set_workspace_memory(workspace_id, payload.memory.strip())
    return {"status": "ok", "memory": memory}


@app.put("/api/workspaces/{workspace_id}/guidance/{stage}")
def update_stage_guidance(
    workspace_id: str, stage: str, payload: StageGuidanceUpdate
) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    try:
        return orchestrator.save_stage_instruction(
            workspace_id, stage, payload.user_instruction
        )
    except OrchestrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/workspaces/{workspace_id}/guidance/{stage}/plan")
def plan_stage_guidance(
    workspace_id: str, stage: str, payload: StagePlanRequest
) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    if db.list_active_jobs(workspace_id):
        raise HTTPException(409, "工作区已有运行中任务，请等待结束后再让总管重新解析要求")
    try:
        return orchestrator.plan_stage(
            workspace_id,
            stage,
            payload.user_instruction,
            force=payload.force,
        )
    except OrchestrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str) -> dict[str, Any]:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.put("/api/workspaces/{workspace_id}/approvals/{gate}")
def update_approval(
    workspace_id: str, gate: str, payload: ApprovalUpdate
) -> dict[str, Any]:
    if gate not in GATES.values():
        raise HTTPException(400, "Unknown approval gate")
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    if payload.status == "approved":
        gate_stage = next((stage for stage, gate_name in GATES.items() if gate_name == gate), None)
        if gate_stage:
            artifact = db.get_artifact(workspace_id, gate_stage)
            review_row = db.get_stage_review(workspace_id, gate_stage)
            if artifact and review_row and int(review_row.get("artifact_revision") or 0) == int(artifact.get("revision") or 0):
                recommended = str((review_row.get("review") or {}).get("recommended_action") or "")
                if recommended in {"regenerate_current", "wait_for_user"} and not payload.force:
                    raise HTTPException(409, f"总管复盘尚未通过：{recommended}。可先调整/重生成，或明确使用强制确认覆盖总管建议。")
    result = db.set_approval(workspace_id, gate, payload.status, payload.note)
    if gate == "assets_approved" and payload.status == "approved":
        published = orchestrator.publish_series_assets(workspace_id)
        result["series_publish"] = published
    return result


@app.put("/api/workspaces/{workspace_id}/artifacts/{kind}")
def revise_artifact(
    workspace_id: str, kind: str, payload: ArtifactRevision
) -> dict[str, Any]:
    try:
        return orchestrator.revise(workspace_id, kind, payload.content, payload.cascade)
    except OrchestrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/workspaces/{workspace_id}/uploads")
async def upload_source(
    workspace_id: str, file: UploadFile = File(...)
) -> dict[str, Any]:
    if not db.get_workspace(workspace_id):
        raise HTTPException(404, "Workspace not found")
    active = db.list_active_jobs(workspace_id)
    if active:
        current = active[0]
        raise HTTPException(
            409,
            f"工作区仍有进行中的任务：{current['stage']} ({current['status']})。请等待任务结束后再更换源视频。",
        )

    safe_name = Path(file.filename or "upload.bin").name
    upload_dir = settings.data_dir / "uploads" / workspace_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / safe_name
    temp_target = upload_dir / f".{uuid.uuid4().hex}.uploading"
    digest = hashlib.sha256()
    size = 0
    content_type = file.content_type
    try:
        with temp_target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 4 * 1024 * 1024 * 1024:
                    raise HTTPException(413, "File exceeds 4 GiB limit")
                digest.update(chunk)
                output.write(chunk)
    except Exception:
        temp_target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    source = db.get_artifact(workspace_id, "source")
    content = dict(source["content"]) if source else {"brief": "", "files": []}
    digest_hex = digest.hexdigest()
    media_url = f"/source-media/{quote(workspace_id)}/{quote(safe_name)}"
    new_file = {
        "name": safe_name,
        "size": size,
        "sha256": digest_hex,
        "path": str(target),
        "url": media_url,
        "content_type": content_type,
    }

    # Compact historical duplicates before deciding whether this upload changes
    # the logical source set. This prevents one video from being analyzed many
    # times because the upload button was clicked repeatedly in older builds.
    existing_files = list(content.get("files", []))
    compacted: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    duplicate_existing: dict[str, Any] | None = None
    for raw_item in existing_files:
        item = dict(raw_item)
        key = str(item.get("sha256") or item.get("path") or item.get("name") or "")
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        if item.get("name") and not item.get("url"):
            item["url"] = (
                f"/source-media/{quote(workspace_id)}/{quote(str(item['name']))}"
            )
        compacted.append(item)
        if item.get("sha256") == digest_hex:
            duplicate_existing = item

    if duplicate_existing is not None:
        # The newly received bytes are redundant. Delete the temporary upload and
        # keep the existing file/revision, while opportunistically compacting old
        # duplicate DB entries if necessary.
        temp_target.unlink(missing_ok=True)
        if len(compacted) != len(existing_files) or compacted != existing_files:
            content["files"] = compacted
            result = orchestrator.revise(workspace_id, "source", content, cascade=True)
            return {"file": duplicate_existing, "deduplicated": True, **result}
        return {
            "file": duplicate_existing,
            "deduplicated": True,
            "artifact": source,
            "invalidated": [],
        }

    # Download completes into a temporary file first, so an interrupted browser
    # upload cannot corrupt the currently active source video with the same name.
    temp_target.replace(target)

    # Same filename means replacement; different filename means another source.
    compacted = [item for item in compacted if item.get("name") != safe_name]
    compacted.append(new_file)
    content["files"] = compacted
    result = orchestrator.revise(workspace_id, "source", content, cascade=True)
    return {"file": new_file, "deduplicated": False, **result}


@app.post("/api/workspaces/{workspace_id}/demo/advance")
def demo_advance(workspace_id: str) -> dict[str, Any]:
    """Run the next deterministic action; useful for smoke tests and demos."""
    actions = orchestrator.next_actions(workspace_id)
    if not actions:
        return workspace_detail(workspace_id)
    action = actions[0]
    try:
        if action["type"] == "approve":
            db.set_approval(workspace_id, action["gate"], "approved", "Demo 自动确认")
        else:
            orchestrator.run(workspace_id, action["stage"])
    except OrchestrationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return workspace_detail(workspace_id)
