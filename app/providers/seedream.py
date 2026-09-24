from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx

from .base import WorkflowProvider
from app.guidance import sanitize_parameter_overrides


class SeedreamError(RuntimeError):
    pass


class SeedreamProvider(WorkflowProvider):
    """Volcengine Ark Seedream reference-image adapter."""

    name = "seedream"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        size: str = "2K",
        max_assets: int = 6,
        output_dir: Path,
        client: httpx.Client | None = None,
    ):
        if not api_key or not model:
            raise ValueError("IMAGE_API_KEY and IMAGE_MODEL are required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.size = size
        self.max_assets = max(1, max_assets)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client = client

    def generate(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        if stage != "reference_images":
            raise NotImplementedError(f"Seedream does not handle stage: {stage}")
        workspace_id = str(context["workspace"]["id"])
        directive = context.get("execution_directive") or {}
        params = sanitize_parameter_overrides(stage, directive.get("parameter_overrides"))
        max_assets = int(params.get("max_assets", self.max_assets))
        prompt_addendum = str(directive.get("prompt_addendum") or "").strip()
        library_lookup: dict[str, dict[str, Any]] = {}
        for library in context.get("asset_library_context", []) or []:
            for library_item in library.get("items", []) or []:
                if isinstance(library_item, dict) and library_item.get("id"):
                    library_lookup[str(library_item["id"])] = library_item
        sources = self._asset_sources(context)
        anchored_sources: list[tuple[str, dict[str, Any]]] = []
        for source_kind, item in sources:
            enriched = dict(item)
            library_id = str(item.get("library_asset_id") or "")
            if library_id and library_id in library_lookup:
                enriched["_library_asset"] = library_lookup[library_id]
            anchored_sources.append((source_kind, enriched))
        sources = anchored_sources[: max_assets]
        if not sources:
            raise SeedreamError("No character, scene or prop assets available")
        client, owns_client = self._get_client()
        items: list[dict[str, Any]] = []
        try:
            for source_kind, item in sources:
                library_asset = item.get("_library_asset") if isinstance(item.get("_library_asset"), dict) else {}
                decision = str(item.get("reuse_decision") or "").upper()
                ref = (library_asset.get("content") or {}).get("reference_image") if library_asset else {}
                if decision == "REUSE" and library_asset and (library_asset.get("preview_url") or (ref or {}).get("url")):
                    items.append({
                        "source_kind": source_kind,
                        "source_id": str(item.get("id") or item.get("name") or source_kind),
                        "name": item.get("name") or item.get("id") or source_kind,
                        "library_asset_id": library_asset.get("id"),
                        "canonical_key": item.get("canonical_key"),
                        "model": "series-library-reuse",
                        "remote_url": (ref or {}).get("remote_url") or "",
                        "url": library_asset.get("preview_url") or (ref or {}).get("url") or "",
                        "local_path": (ref or {}).get("local_path") or "",
                        "status": "reused",
                    })
                    continue
                items.append(self._generate_one(client, workspace_id, source_kind, item, prompt_addendum))
        finally:
            if owns_client:
                client.close()
        return {
            "items": items,
            "requested": len(sources),
            "completed": len(items),
            "model": self.model,
            "status": "succeeded",
            "note": "参考图已下载到本地；remote_url 仅用于随后提交 Seedance。",
        }

    @staticmethod
    def _asset_sources(context: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        result: list[tuple[str, dict[str, Any]]] = []
        for artifact in context.get("artifacts", []):
            kind = str(artifact.get("kind", ""))
            if kind not in {"characters", "scenes", "props"}:
                continue
            for item in artifact.get("content", {}).get("items", []):
                result.append((kind, item))
        return result

    def _generate_one(
        self,
        client: httpx.Client,
        workspace_id: str,
        source_kind: str,
        item: dict[str, Any],
        prompt_addendum: str = "",
    ) -> dict[str, Any]:
        prompt = self._prompt(source_kind, item)
        if prompt_addendum:
            prompt += "\nArt director requirements: " + prompt_addendum
        response = client.post(
            f"{self.base_url}/images/generations",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "prompt": prompt,
                "size": self.size,
                "sequential_image_generation": "disabled",
                "stream": False,
                "response_format": "url",
                "watermark": False,
            },
        )
        if response.is_error:
            raise SeedreamError(
                f"Seedream generation failed ({response.status_code}): {response.text[:800]}"
            )
        data = response.json().get("data") or []
        remote_url = str(data[0].get("url", "")) if data else ""
        if not remote_url:
            raise SeedreamError("Seedream response did not contain an image URL")

        source_id = str(item.get("id") or item.get("name") or source_kind)
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", source_id).strip("-") or "asset"
        target_dir = self.output_dir / workspace_id / "reference_images"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{source_kind}-{safe_id}.png"
        download = client.get(remote_url)
        download.raise_for_status()
        target.write_bytes(download.content)
        relative = target.relative_to(self.output_dir).as_posix()
        return {
            "source_kind": source_kind,
            "source_id": source_id,
            "name": item.get("name") or source_id,
            "library_asset_id": item.get("library_asset_id"),
            "canonical_key": item.get("canonical_key"),
            "reuse_decision": item.get("reuse_decision"),
            "model": self.model,
            "remote_url": remote_url,
            "url": f"/media/{relative}",
            "local_path": str(target),
            "status": "succeeded",
        }

    @staticmethod
    def _prompt(source_kind: str, item: dict[str, Any]) -> str:
        label = {
            "characters": "cinematic character reference portrait and turnaround",
            "scenes": "cinematic environment reference image",
            "props": "cinematic production prop reference image",
        }[source_kind]
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        library = item.get("_library_asset") if isinstance(item.get("_library_asset"), dict) else {}
        library_anchor = ""
        if library:
            library_anchor = (
                " Reuse anchor from thematic asset library: "
                + str(library.get("name") or "")
                + ". "
                + str(library.get("description") or "")
                + " Continuity metadata: "
                + str(library.get("content") or "")
                + ". Keep this identity/design stable; do not reinterpret it unless the art director explicitly requests a change."
            )
        return (
            f"{label}. Subject: {name}. Exact design: {description}. "
            + library_anchor
            + " Photorealistic cinematic screen drama, production-ready continuity "
            "reference, neutral clean composition, no captions, no text, no watermark."
        )

    def _get_client(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.Client(timeout=180, follow_redirects=True), True
