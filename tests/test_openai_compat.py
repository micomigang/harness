import base64
from pathlib import Path

import httpx
import pytest

from app.providers.openai_compat import OpenAICompatibleProvider
from app.providers.source_media import SourceMediaProcessor


class StubMediaProcessor:
    def __init__(self, output_dir: Path | None = None):
        self.output_dir = output_dir or Path("data") / "outputs"

    def build_analysis_segments(self, media, *, segment_seconds, max_frames_per_segment):
        return [
            {
                "segment_index": 0,
                "start_seconds": 0.0,
                "end_seconds": 30.0,
                "duration_seconds": 30.0,
                "frame_count": 1,
                "frames": [media["frames"][0]],
            }
        ]


def _provider(tmp_path: Path | None = None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key="test-key",
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k2.6",
        media_processor=StubMediaProcessor(tmp_path),
        segment_seconds=30,
        segment_max_frames=12,
        segment_parallelism=2,
    )


def _context() -> dict:
    return {
        "workspace": {
            "id": "workspace-1",
            "title": "Episode",
            "brief": "Analyze this source faithfully",
            "settings": {"aspect_ratio": "9:16"},
        },
        "artifacts": [
            {
                "kind": "source",
                "revision": 2,
                "content": {
                    "brief": "Analyze this source faithfully",
                    "files": [
                        {
                            "name": "source.mp4",
                            "size": 123456,
                            "content_type": "video/mp4",
                        }
                    ],
                },
            }
        ],
    }


def test_external_context_sanitizer_removes_local_paths_and_hashes():
    cleaned = OpenAICompatibleProvider._sanitize_for_external(
        {
            "path": "F:/private/source.mp4",
            "items": [
                {
                    "name": "source.mp4",
                    "local_path": "F:/private/frame.jpg",
                    "sha256": "secret-ish-fingerprint",
                    "description": "keep me",
                }
            ],
        }
    )
    assert "path" not in cleaned
    assert cleaned["items"] == [{"name": "source.mp4", "description": "keep me"}]


def test_kimi_payload_does_not_send_incompatible_temperature():
    payload = _provider()._build_payload(
        task="Return script JSON",
        user_content='{"brief":"test"}',
        stage="script",
    )
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert payload["response_format"] == {"type": "json_object"}


def test_analysis_payload_attaches_only_selected_frames(tmp_path: Path):
    selected = tmp_path / "selected.jpg"
    selected.write_bytes(b"jpeg-bytes")
    skipped = tmp_path / "skipped.jpg"
    skipped.write_bytes(b"other-bytes")

    content = _provider(tmp_path)._build_user_content(
        stage="analysis",
        prompt_context={"brief": "analyze"},
        source_media=[
            {
                "name": "source.mp4",
                "duration_seconds": 2.0,
                "frames": [
                    {
                        "timestamp_seconds": 0.0,
                        "kind": "base",
                        "local_path": str(selected),
                        "selected_for_model": True,
                    },
                    {
                        "timestamp_seconds": 1.0,
                        "kind": "base",
                        "local_path": str(skipped),
                        "selected_for_model": False,
                    },
                ],
            }
        ],
    )
    image_parts = [part for part in content if part["type"] == "image_url"]
    assert len(image_parts) == 1
    encoded = base64.b64encode(b"jpeg-bytes").decode("ascii")
    assert image_parts[0]["image_url"]["url"] == f"data:image/jpeg;base64,{encoded}"
    assert any("t=0.000s" in part.get("text", "") for part in content)


def test_segment_user_content_attaches_only_segment_frames(tmp_path: Path):
    selected = tmp_path / "selected.jpg"
    selected.write_bytes(b"jpeg-bytes")
    media = {
        "name": "source.mp4",
        "duration_seconds": 60.0,
        "frames": [
            {
                "timestamp_seconds": 10.0,
                "kind": "scene",
                "local_path": str(selected),
                "selected_for_model": True,
            }
        ],
    }
    provider = _provider(tmp_path)
    segment = provider._build_media_segments(media)[0]
    content = provider._build_segment_user_content({"brief": "analyze"}, media, segment)
    image_parts = [part for part in content if part["type"] == "image_url"]
    assert len(image_parts) == 1
    assert any("segment" in part.get("text", "") or "10.000s" in part.get("text", "") for part in content)


def test_segment_cache_reuses_completed_result(tmp_path: Path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg-bytes")
    media = {
        "name": "source.mp4",
        "source_sha256": "abc123",
        "size_bytes": 123456,
        "duration_seconds": 30.0,
        "sampling": {"effective_base_fps": 1.0},
        "frames": [
            {
                "timestamp_seconds": 10.0,
                "kind": "base",
                "size_bytes": frame.stat().st_size,
                "local_path": str(frame),
                "selected_for_model": True,
            }
        ],
    }
    segment = {
        "segment_index": 0,
        "start_seconds": 0.0,
        "end_seconds": 30.0,
        "duration_seconds": 30.0,
        "frame_count": 1,
        "frames": media["frames"],
    }
    provider = _provider(tmp_path)
    calls = {"count": 0}

    def fake_analyze(prompt_context, media_value, segment_value):
        calls["count"] += 1
        return {
            "summary": "cached",
            "media_name": media_value["name"],
            "segment_index": segment_value["segment_index"],
            "segment_start_seconds": 0.0,
            "segment_end_seconds": 30.0,
        }

    provider._analyze_segment = fake_analyze
    first = provider._analyze_segment_cached(_context(), media, segment)
    second = provider._analyze_segment_cached(_context(), media, segment)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["result"] == second["result"]
    assert calls["count"] == 1


def test_segmented_analysis_resumes_only_failed_segments_and_caches_synthesis(tmp_path: Path):
    processor = SourceMediaProcessor(ffmpeg_path="ffmpeg", output_dir=tmp_path)
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k2.6",
        media_processor=processor,
        segment_seconds=30,
        segment_max_frames=12,
        segment_parallelism=2,
    )
    frames = [
        {
            "timestamp_seconds": float(i),
            "kind": "base",
            "size_bytes": 1000,
            "selected_for_model": True,
        }
        for i in range(60)
    ]
    media = {
        "name": "source.mp4",
        "source_sha256": "source-hash",
        "size_bytes": 123456,
        "duration_seconds": 60.0,
        "sampling": {"effective_base_fps": 1.0},
        "frames": frames,
    }
    attempts = {0: 0, 1: 0}
    synthesis_calls = {"count": 0}

    def flaky_analyze(prompt_context, media_value, segment_value):
        index = int(segment_value["segment_index"])
        attempts[index] += 1
        if index == 1 and attempts[index] == 1:
            raise RuntimeError("temporary upstream error")
        return {
            "media_name": media_value["name"],
            "segment_index": index,
            "segment_start_seconds": segment_value["start_seconds"],
            "segment_end_seconds": segment_value["end_seconds"],
            "summary": f"segment-{index}",
        }

    def fake_request_json(*, task, user_content, stage):
        synthesis_calls["count"] += 1
        return {"logline": "global", "source_summary": "ok"}

    provider._analyze_segment = flaky_analyze
    provider._request_json = fake_request_json

    with pytest.raises(RuntimeError, match="Successful segments were cached"):
        provider._generate_segmented_analysis(
            prompt_context=_context(), source_media=[media]
        )

    assert attempts == {0: 1, 1: 1}

    result = provider._generate_segmented_analysis(
        prompt_context=_context(), source_media=[media]
    )
    assert attempts == {0: 1, 1: 2}
    assert result["analysis_plan"]["cache_hits"] == 1
    assert result["analysis_plan"]["cache_misses"] == 1
    assert result["analysis_plan"]["resumed_from_cache"] is True
    assert synthesis_calls["count"] == 1

    result_again = provider._generate_segmented_analysis(
        prompt_context=_context(), source_media=[media]
    )
    assert attempts == {0: 1, 1: 2}
    assert result_again["analysis_plan"]["cache_hits"] == 2
    assert result_again["analysis_plan"]["synthesis_cache_hit"] is True
    assert synthesis_calls["count"] == 1


def test_http_error_preserves_upstream_body():
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://api.moonshot.cn/v1/chat/completions"),
        json={"error": {"message": "invalid temperature"}},
    )
    message = OpenAICompatibleProvider._format_http_error(response)
    assert "400" in message
    assert "invalid temperature" in message


def test_parse_json_object_accepts_markdown_fenced_json():
    content = """```json
{"logline": "ok", "evidence_timeline": []}
```"""
    parsed = OpenAICompatibleProvider._parse_json_object(content)
    assert parsed == {"logline": "ok", "evidence_timeline": []}


def test_parse_json_object_accepts_json_with_trailing_text():
    content = '{"logline": "ok"}\nDone.'
    parsed = OpenAICompatibleProvider._parse_json_object(content)
    assert parsed == {"logline": "ok"}


def test_parse_json_object_rejects_incomplete_json():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider._parse_json_object('```json\n{"logline": "unfinished"')


def test_prepare_script_context_drops_raw_segment_evidence_and_source_artifact():
    context = {
        "workspace": {"id": "w1", "settings": {"target_language": "French"}},
        "artifacts": [
            {"kind": "source", "content": {"files": [{"name": "x.mp4"}]}},
            {
                "kind": "analysis",
                "content": {
                    "logline": "keep",
                    "source_summary": "keep summary",
                    "segment_analyses": [{"summary": "huge raw segment"}],
                    "source_media": [{"frames": [1, 2, 3]}],
                    "analysis_plan": {"segment_count": 99},
                },
            },
        ],
    }
    cleaned = OpenAICompatibleProvider._prepare_prompt_context("script", context)
    assert [item["kind"] for item in cleaned["artifacts"]] == ["analysis"]
    analysis = cleaned["artifacts"][0]["content"]
    assert analysis["logline"] == "keep"
    assert "segment_analyses" not in analysis
    assert "source_media" not in analysis
    assert "analysis_plan" not in analysis


def test_stable_analysis_context_deduplicates_repeated_source_entries():
    context = _context()
    source_files = context["artifacts"][0]["content"]["files"]
    source_files.append(dict(source_files[0]))
    stable = OpenAICompatibleProvider._stable_analysis_context(context)
    assert len(stable["source_files"]) == 1


def test_director_plan_sanitizes_unsupported_parameters():
    provider = _provider()

    def fake_post(payload):
        return (
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"interpretation":"France","prompt_addendum":"Use French localization","parameter_overrides":{"storyboard_count":15,"temperature":0.2},"acceptance_criteria":[],"memory_candidates":[],"warnings":[],"questions":[],"requires_user_input":false}'
                        }
                    }
                ]
            },
            "",
        )

    provider._post_json = fake_post
    plan = provider.plan_stage(
        "storyboard",
        {
            "workspace": {"id": "w", "settings": {"target_market": "France"}},
            "artifacts": [],
            "project_memory": "French market",
            "user_instruction": "Use 15 shots",
        },
    )
    assert plan["parameter_overrides"] == {"storyboard_count": 15}
    assert plan["requires_user_input"] is False


def test_script_task_contains_localization_contract():
    from app.providers.openai_compat import TASK_PROMPTS

    prompt = TASK_PROMPTS["script"]
    assert "cultural adaptation" in prompt
    assert "tu/vous" in prompt
    assert "localization_map" in prompt


def test_director_asset_manifest_preview_keeps_complete_compact_manifest():
    items = [
        {
            "manifest_id": f"ASSET_{i:03d}",
            "asset_type": "prop" if i > 10 else "scene",
            "canonical_key": f"KEY_{i:03d}",
            "script_identity": f"Asset {i}",
            "generation_requirements": ["large field intentionally omitted from director preview"],
        }
        for i in range(1, 15)
    ]
    preview = OpenAICompatibleProvider._director_artifact_preview(
        {
            "kind": "asset_manifest",
            "revision": 2,
            "status": "ready",
            "content": {
                "items": items,
                "manifest_validation": {"item_count": 14, "missing_required_keys": [], "status": "pass"},
            },
        }
    )
    assert len(preview["content"]["items"]) == 14
    assert preview["content"]["items"][-1]["canonical_key"] == "KEY_014"
    assert "generation_requirements" not in preview["content"]["items"][-1]
    assert preview["content"]["manifest_validation"]["item_count"] == 14


def test_director_asset_manifest_upstream_keeps_full_script_contract_and_current_manifest():
    requirements = [
        {
            "canonical_id": f"REQ_{i:03d}",
            "type": "prop",
            "canonical_name": f"Requirement {i}",
            "continuity_lock": f"lock-{i}",
        }
        for i in range(1, 12)
    ]
    manifest_items = [
        {
            "manifest_id": f"ASSET_{i:03d}",
            "asset_type": "prop",
            "canonical_key": f"KEY_{i:03d}",
            "script_identity": f"Asset {i}",
        }
        for i in range(1, 15)
    ]
    context = {
        "workspace": {"id": "w1"},
        "artifacts": [
            {
                "kind": "script",
                "revision": 1,
                "status": "ready",
                "content": {"asset_requirements": requirements, "continuity_rules": ["keep identities"]},
            },
            {
                "kind": "asset_manifest",
                "revision": 2,
                "status": "stale",
                "content": {
                    "items": manifest_items,
                    "manifest_validation": {"item_count": 14, "status": "pass"},
                },
            },
        ],
    }
    upstream = OpenAICompatibleProvider._director_upstream_context("asset_manifest", context)
    by_kind = {item["kind"]: item for item in upstream}
    assert len(by_kind["script"]["content"]["asset_requirements"]) == 11
    assert by_kind["script"]["content"]["continuity_rules"] == ["keep identities"]
    assert len(by_kind["asset_manifest"]["content"]["items"]) == 14
    assert by_kind["asset_manifest"]["content"]["items"][-1]["canonical_key"] == "KEY_014"
    assert by_kind["asset_manifest"]["content"]["manifest_validation"]["item_count"] == 14
