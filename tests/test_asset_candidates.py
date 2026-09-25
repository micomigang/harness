import app.main as main_module


class FakeImageProvider:
    def __init__(self):
        self.calls = []

    def generate_candidate(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls)
        return {
            "prompt": f"prompt-{index}",
            "model": "seedream-test",
            "remote_url": f"https://example.test/{index}.png",
            "url": f"/media/ws/asset_candidates/{index}.png",
            "local_path": f"/tmp/{index}.png",
        }


class FakeWorkflow:
    def __init__(self, image):
        self.image = image


class FakeProviders:
    def __init__(self, image):
        self.image = image

    def workflow(self):
        return FakeWorkflow(self.image)


class FakeOrchestrator:
    def __init__(self, image):
        self.providers = FakeProviders(image)


class FakeDB:
    def __init__(self):
        self.created = []

    def get_workspace(self, workspace_id):
        return {"id": workspace_id} if workspace_id == "ws" else None

    def get_artifact(self, workspace_id, stage):
        return {
            "workspace_id": workspace_id,
            "kind": stage,
            "status": "ready",
            "revision": 3,
            "content": {"items": [{
                "id": "char_grandmere",
                "canonical_key": "char_grandmere",
                "name": "Grand-mère",
                "appearance": "elderly French woman",
                "wardrobe": "red quilted coat",
            }]},
        }

    def get_asset_candidate(self, candidate_id):
        return None

    def list_asset_candidates(self, *args, **kwargs):
        return []

    def add_asset_candidate(self, workspace_id, stage, artifact_revision, canonical_key, source_id, **values):
        item = {
            "id": f"cand-{len(self.created)+1}",
            "workspace_id": workspace_id,
            "stage": stage,
            "artifact_revision": artifact_revision,
            "canonical_key": canonical_key,
            "source_id": source_id,
            "selected": False,
            **values,
        }
        self.created.append(item)
        return item

    def add_event(self, *args, **kwargs):
        return None


def test_asset_candidate_route_is_registered():
    paths = {getattr(route, "path", "") for route in main_module.app.routes}
    assert "/api/workspaces/{workspace_id}/asset-candidates" in paths
    assert "/api/workspaces/{workspace_id}/asset-candidates/batch" in paths
    assert "/api/workspaces/{workspace_id}/asset-candidates/{candidate_id}/select" in paths


def test_generate_asset_candidate_is_scoped_to_one_asset(monkeypatch):
    fake_db = FakeDB()
    image = FakeImageProvider()
    monkeypatch.setattr(main_module, "db", fake_db)
    monkeypatch.setattr(main_module, "orchestrator", FakeOrchestrator(image))

    result = main_module.generate_asset_candidates(
        "ws",
        main_module.AssetCandidateGenerateRequest(
            stage="characters",
            canonical_key="char_grandmere",
            feedback="older face, keep coat",
            count=2,
        ),
    )

    assert len(result["items"]) == 2
    assert all(item["canonical_key"] == "char_grandmere" for item in result["items"])
    assert all(call["feedback"] == "older face, keep coat" for call in image.calls)
    assert all(call["item"]["canonical_key"] == "char_grandmere" for call in image.calls)


def test_candidate_route_passes_target_market_to_image_provider(monkeypatch):
    class LocalizedDB(FakeDB):
        def get_workspace(self, workspace_id):
            return {"id": workspace_id, "settings": {"target_market": "France", "target_language": "fr-FR"}} if workspace_id == "ws" else None

        def get_artifact(self, workspace_id, stage):
            if stage == "script":
                return {"kind": "script", "status": "ready", "revision": 1, "content": {
                    "target_market": "France",
                    "language": "fr-FR",
                    "localization_strategy": "French cultural adaptation",
                }}
            return super().get_artifact(workspace_id, stage)

    fake_db = LocalizedDB()
    image = FakeImageProvider()
    monkeypatch.setattr(main_module, "db", fake_db)
    monkeypatch.setattr(main_module, "orchestrator", FakeOrchestrator(image))

    main_module.generate_asset_candidates(
        "ws",
        main_module.AssetCandidateGenerateRequest(
            stage="characters", canonical_key="char_grandmere", count=1,
        ),
    )
    ctx = image.calls[0]["localization_context"]
    assert ctx["target_market"] == "France"
    assert ctx["target_language"] == "fr-FR"
    assert ctx["localization_strategy"] == "French cultural adaptation"


def test_generate_asset_candidates_batch_applies_same_feedback_to_multiple_assets(monkeypatch):
    class BatchDB(FakeDB):
        def get_artifact(self, workspace_id, stage):
            return {
                "workspace_id": workspace_id,
                "kind": stage,
                "status": "ready",
                "revision": 4,
                "content": {"items": [
                    {"id": "char_grandmere", "canonical_key": "char_grandmere", "name": "Grand-mère"},
                    {"id": "char_son", "canonical_key": "char_son", "name": "Le fils"},
                ]},
            }

    fake_db = BatchDB()
    image = FakeImageProvider()
    monkeypatch.setattr(main_module, "db", fake_db)
    monkeypatch.setattr(main_module, "orchestrator", FakeOrchestrator(image))

    result = main_module.generate_asset_candidates_batch(
        "ws",
        main_module.AssetCandidateBatchGenerateRequest(
            stage="characters",
            canonical_keys=["char_grandmere", "char_son"],
            feedback="all characters should look more French",
            count=1,
        ),
    )

    assert result["asset_count"] == 2
    assert set(result["items_by_key"].keys()) == {"char_grandmere", "char_son"}
    assert len(image.calls) == 2
    assert {call["item"]["canonical_key"] for call in image.calls} == {"char_grandmere", "char_son"}
    assert all(call["feedback"] == "all characters should look more French" for call in image.calls)


def test_single_candidate_endpoint_preserves_partial_success(monkeypatch):
    class FlakyImageProvider(FakeImageProvider):
        def generate_candidate(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) >= 2:
                raise RuntimeError("provider failed on second image")
            return {
                "prompt": "prompt-1",
                "model": "seedream-test",
                "remote_url": "https://example.test/1.png",
                "url": "/media/ws/asset_candidates/1.png",
                "local_path": "/tmp/1.png",
            }

    fake_db = FakeDB()
    image = FlakyImageProvider()
    monkeypatch.setattr(main_module, "db", fake_db)
    monkeypatch.setattr(main_module, "orchestrator", FakeOrchestrator(image))

    result = main_module.generate_asset_candidates(
        "ws",
        main_module.AssetCandidateGenerateRequest(
            stage="characters",
            canonical_key="char_grandmere",
            feedback="small edit",
            count=4,
        ),
    )

    assert result["status"] == "partial"
    assert result["requested_count"] == 4
    assert result["completed_count"] == 1
    assert len(result["items"]) == 1
    assert "second image" in result["error"]


def test_reference_image_candidate_uses_artifact_image_as_default_edit_base(monkeypatch):
    class ReferenceDB(FakeDB):
        def get_artifact(self, workspace_id, stage):
            if stage == "script":
                return {"kind": "script", "status": "ready", "revision": 1, "content": {}}
            assert stage == "reference_images"
            return {
                "workspace_id": workspace_id,
                "kind": stage,
                "status": "ready",
                "revision": 2,
                "content": {"items": [{
                    "id": "combo__char_grandmere__scene_foyer",
                    "canonical_key": "combo__char_grandmere__scene_foyer",
                    "name": "Grand-mère + foyer",
                    "source_kind": "combination",
                    "source_ids": ["char_grandmere", "scene_foyer"],
                    "prompt": "Preserve both canonical assets exactly.",
                    "remote_url": "https://example.test/reference.png",
                    "url": "/media/ws/reference_images/reference.png",
                    "local_path": "",
                }]},
            }

    fake_db = ReferenceDB()
    image = FakeImageProvider()
    monkeypatch.setattr(main_module, "db", fake_db)
    monkeypatch.setattr(main_module, "orchestrator", FakeOrchestrator(image))

    result = main_module.generate_asset_candidates(
        "ws",
        main_module.AssetCandidateGenerateRequest(
            stage="reference_images",
            canonical_key="combo__char_grandmere__scene_foyer",
            feedback="move the subjects slightly closer",
            count=1,
        ),
    )

    assert result["completed_count"] == 1
    assert image.calls[0]["source_kind"] == "reference_images"
    assert image.calls[0]["reference_url"] == "https://example.test/reference.png"
    assert image.calls[0]["feedback"] == "move the subjects slightly closer"
