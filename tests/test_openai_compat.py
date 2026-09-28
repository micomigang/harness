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


def test_director_storyboard_preview_keeps_complete_compact_shot_sequence():
    shots = [
        {
            "index": i,
            "duration_seconds": 8,
            "story_beat": f"beat-{i}",
            "asset_bindings": {
                "characters": ["char_a"],
                "scenes": ["scene_a"],
                "props": [],
                "reference_images": [
                    {
                        "canonical_key": "combo__char_a__scene_a",
                        "candidate_id": f"candidate-{i}",
                        "url": "https://example.invalid/very/long/url.jpg",
                        "source_kind": "combination",
                    }
                ],
            },
            "visual_prompt": "x" * 1200,
            "blocking": "keep axis",
            "status": "planned",
        }
        for i in range(1, 15)
    ]
    preview = OpenAICompatibleProvider._director_artifact_preview(
        {
            "kind": "storyboard",
            "revision": 1,
            "status": "ready",
            "content": {
                "shots": shots,
                "estimated_seconds": 112,
                "storyboard_validation": {
                    "status": "pass",
                    "expected_shots": 14,
                    "actual_shots": 14,
                },
            },
        }
    )
    compact = preview["content"]["shots"]
    assert len(compact) == 14
    assert compact[-1]["index"] == 14
    assert compact[-1]["asset_bindings"]["reference_images"][0]["url"] == "https://example.invalid/very/long/url.jpg"
    assert len(compact[-1]["visual_prompt"]) <= 901
    assert preview["content"]["storyboard_validation"]["actual_shots"] == 14


def test_director_upstream_keeps_full_storyboard_and_reference_contracts():
    storyboard_shots = [
        {
            "index": i,
            "duration_seconds": 6,
            "story_beat": f"beat-{i}",
            "asset_bindings": {"scenes": ["scene_a"], "reference_images": []},
            "visual_prompt": f"prompt-{i}",
        }
        for i in range(1, 15)
    ]
    reference_items = [
        {
            "canonical_key": f"ref_{i}",
            "source_kind": "scene",
            "source_id": f"scene_{i}",
            "candidate_id": f"cand_{i}",
            "url": f"/media/{i}.jpg",
        }
        for i in range(1, 22)
    ]
    context = {
        "workspace": {"id": "w1"},
        "artifacts": [
            {"kind": "reference_images", "revision": 8, "status": "ready", "content": {"items": reference_items, "reference_validation": {"status": "pass"}}},
            {"kind": "storyboard", "revision": 1, "status": "ready", "content": {"shots": storyboard_shots, "storyboard_validation": {"status": "pass"}}},
        ],
    }
    upstream = OpenAICompatibleProvider._director_upstream_context("dialogue_plan", context)
    by_kind = {item["kind"]: item for item in upstream}
    assert len(by_kind["reference_images"]["content"]["items"]) == 21
    assert len(by_kind["storyboard"]["content"]["shots"]) == 14
    assert by_kind["storyboard"]["content"]["shots"][-1]["index"] == 14


def test_dialogue_plan_preview_keeps_complete_item_sequence():
    items = [
        {
            "shot_index": index,
            "speaker_id": None if index in {2, 9, 14} else "char_a",
            "dialogue_text": "" if index in {2, 9, 14} else f"line {index}",
            "language": "fr",
            "timing": {"start_seconds": 0.5, "end_seconds": 1.5},
            "subtitle": "" if index in {2, 9, 14} else f"line {index}",
            "lip_sync_target": index not in {2, 9, 14},
            "status": "silent" if index in {2, 9, 14} else "dialogue",
        }
        for index in range(1, 15)
    ]
    preview = OpenAICompatibleProvider._director_artifact_preview(
        {
            "kind": "dialogue_plan",
            "revision": 1,
            "status": "ready",
            "provider": "kimi",
            "content": {"items": items, "dialogue_validation": {"status": "pass"}},
        }
    )
    assert len(preview["content"]["items"]) == 14
    assert preview["content"]["items"][-1]["shot_index"] == 14
    assert preview["content"]["dialogue_validation"]["status"] == "pass"


def test_sound_plan_preview_keeps_complete_item_sequence():
    items = [
        {
            "shot_index": index,
            "ambience": f"ambience-{index}",
            "foley": [f"foley-{index}"],
            "cues": [],
            "ducking": {"enabled": index not in {2, 9, 14}},
            "negative_audio": ["no music"],
            "status": "planned",
        }
        for index in range(1, 15)
    ]
    preview = OpenAICompatibleProvider._director_artifact_preview(
        {
            "kind": "sound_plan",
            "revision": 2,
            "status": "ready",
            "provider": "kimi",
            "content": {"items": items, "sound_validation": {"status": "pass"}},
        }
    )
    assert len(preview["content"]["items"]) == 14
    assert preview["content"]["items"][-1]["shot_index"] == 14
    assert preview["content"]["sound_validation"]["status"] == "pass"


def test_review_preview_keeps_complete_checks_and_blockers():
    checks = [
        {
            "name": f"check-{index}",
            "status": "fail" if index in {1, 2} else ("warn" if index in {3, 4} else "pass"),
            "evidence": f"evidence-{index}",
            "owner": "storyboard_director" if index <= 2 else "continuity_qa",
            "remediation": f"fix-{index}",
        }
        for index in range(1, 13)
    ]
    blockers = [
        {"name": "blocker-1", "owner": "storyboard_director", "remediation": "fix binding"},
        {"name": "blocker-2", "owner": "storyboard_director", "remediation": "fix prop binding"},
    ]
    preview = OpenAICompatibleProvider._director_artifact_preview(
        {
            "kind": "review",
            "revision": 1,
            "status": "ready",
            "provider": "kimi",
            "content": {"checks": checks, "blocking_failures": blockers},
        }
    )
    assert len(preview["content"]["checks"]) == 12
    assert preview["content"]["checks"][-1]["name"] == "check-12"
    assert len(preview["content"]["blocking_failures"]) == 2


def test_review_stage_upstream_keeps_full_sound_and_review_contracts():
    sound_items = [
        {"shot_index": index, "ambience": f"amb-{index}", "foley": [], "cues": [], "ducking": {}, "negative_audio": [], "status": "planned"}
        for index in range(1, 15)
    ]
    checks = [{"name": f"check-{i}", "status": "pass", "evidence": f"e-{i}", "owner": "qa"} for i in range(1, 13)]
    context = {
        "workspace": {"id": "w1"},
        "artifacts": [
            {"kind": "sound_plan", "revision": 2, "status": "ready", "content": {"items": sound_items, "sound_validation": {"status": "pass"}}},
            {"kind": "review", "revision": 1, "status": "ready", "content": {"checks": checks, "blocking_failures": []}},
        ],
    }
    upstream = OpenAICompatibleProvider._director_upstream_context("preview", context)
    by_kind = {item["kind"]: item for item in upstream}
    assert len(by_kind["sound_plan"]["content"]["items"]) == 14
    assert len(by_kind["review"]["content"]["checks"]) == 12


def test_preview_review_context_includes_tracking_fields():
    content = {
        "shot_index": 1,
        "provider_job_id": "cgt-123",
        "model": "doubao-seedance-2-5-260628",
        "url": "/media/shot-001.mp4",
        "status": "succeeded",
    }
    preview = OpenAICompatibleProvider._director_artifact_preview(
        {"kind": "preview", "status": "ready", "revision": 1, "content": content}
    )
    assert all(preview["content"][key] == value for key, value in content.items())


def test_batch_video_preview_keeps_complete_compact_shot_sequence():
    items = []
    for index in range(1, 15):
        items.append({
            "shot_index": index,
            "provider_job_id": f"task-{index}",
            "model": "doubao-seedance-2-0-260128",
            "status": "succeeded",
            "duration_seconds": 10,
            "resolution": "720p",
            "ratio": "9:16",
            "generate_audio": True,
            "reference_keys": ["char_a", "scene_a"],
            "request_payload_digest": {
                "shot_index": index,
                "upstream_revisions": {"storyboard": 6, "sound_plan": 3},
                "technical_params": {"resolution": "720p", "ratio": "9:16", "generate_audio": True},
            },
            "assembly_log": {
                "status": "pass",
                "sound_plan": {"hydrated": True, "ducking_active": False if index in {9, 14} else True},
            },
            "preflight_checks": {"status": "pass"},
            "audio_probe": {"status": "pass", "has_audio": True, "codec": "aac"},
        })
    preview = OpenAICompatibleProvider._director_artifact_preview({
        "kind": "batch_video",
        "revision": 2,
        "status": "ready",
        "provider": "kimi-seedance",
        "content": {
            "items": items,
            "requested": 14,
            "completed": 14,
            "storyboard_shots": 14,
            "coverage_complete": True,
            "submitted_new_tasks": 6,
            "reused_existing": 8,
            "batch_concurrency": 3,
            "execution_mode": "parallel_waves",
            "fail_stop_scope": "wave",
        },
    })
    assert len(preview["content"]["items"]) == 14
    assert preview["content"]["items"][-1]["shot_index"] == 14
    assert preview["content"]["items"][-1]["assembly_log"]["sound_plan"]["ducking_active"] is False
    assert preview["content"]["coverage_complete"] is True
    assert preview["content"]["submitted_new_tasks"] == 6


def test_music_plan_context_keeps_dynamic_shot_rows_but_drops_heavy_visual_assets():
    context = {
        "workspace": {"id": "w", "title": "Episode", "settings": {"target_market": "France"}},
        "project_memory": "keep continuity",
        "user_instruction": "plan music only",
        "execution_directive": {"interpretation": "locked upstream"},
        "artifacts": [
            {"kind": "characters", "revision": 4, "status": "ready", "content": {"items": [{"canonical_key": "char_a", "huge": "x" * 10000}]}},
            {"kind": "script", "revision": 1, "status": "ready", "content": {"title": "EP", "language": "fr", "target_market": "France", "beats": ["a", "b"]}},
            {"kind": "storyboard", "revision": 6, "status": "ready", "content": {"shots": [
                {"index": 1, "duration_seconds": 5, "story_beat": "a", "visual_prompt": "x" * 5000},
                {"index": 2, "duration_seconds": 7, "story_beat": "b", "visual_prompt": "y" * 5000},
            ], "estimated_seconds": 12}},
            {"kind": "dialogue_plan", "revision": 2, "status": "ready", "content": {"items": [
                {"shot_index": 1, "text": "Bonjour", "timing": {"relative_start": 0.5, "relative_end": 2.0}, "status": "dialogue"},
                {"shot_index": 2, "status": "silent", "no_dialogue": True},
            ]}},
            {"kind": "sound_plan", "revision": 3, "status": "ready", "content": {"items": [
                {"shot_index": 1, "ambience": "room", "foley": [], "cues": [], "ducking": {"active": True}, "negative_audio": [], "status": "dialogue"},
                {"shot_index": 2, "ambience": "room", "foley": [], "cues": [], "ducking": {"active": False}, "negative_audio": ["no bgm"], "status": "silent"},
            ]}},
            {"kind": "batch_video", "revision": 2, "status": "ready", "content": {"items": [
                {"shot_index": 1, "duration_seconds": 5, "status": "succeeded", "url": "https://example/1.mp4", "assembly_log": {"huge": "z" * 10000}},
                {"shot_index": 2, "duration_seconds": 7, "status": "succeeded", "url": "https://example/2.mp4"},
            ], "requested": 2, "completed": 2, "coverage_complete": True}},
        ],
    }
    compact = OpenAICompatibleProvider._prepare_prompt_context("music_plan", context)
    kinds = [item["kind"] for item in compact["artifacts"]]
    assert kinds == ["script", "storyboard", "dialogue_plan", "sound_plan", "batch_video"]
    storyboard = next(item for item in compact["artifacts"] if item["kind"] == "storyboard")
    assert len(storyboard["content"]["shots"]) == 2
    assert "visual_prompt" not in storyboard["content"]["shots"][0]
    batch = next(item for item in compact["artifacts"] if item["kind"] == "batch_video")
    assert batch["content"]["items"] == [
        {"shot_index": 1, "duration_seconds": 5, "status": "succeeded"},
        {"shot_index": 2, "duration_seconds": 7, "status": "succeeded"},
    ]


def test_compose_director_preview_exposes_audit_not_private_local_path():
    preview = OpenAICompatibleProvider._director_artifact_preview({
        "kind": "compose",
        "status": "ready",
        "revision": 2,
        "provider": "local-ffmpeg",
        "content": {
            "url": "/media/w/compose/final.mp4",
            "local_path": "F:/private/final.mp4",
            "local_path_present": True,
            "output_file_size_bytes": 1234,
            "input_shots": [
                {"shot_index": i, "source_url": f"/media/w/batch/shot-{i:03d}.mp4", "source_path": f"F:/private/shot-{i:03d}.mp4", "source_path_present": True, "file_name": f"shot-{i:03d}.mp4", "file_size_bytes": 1000, "actual_duration_seconds": 10.001, "expected_duration_seconds": 10.0, "duration_delta_seconds": 0.001}
                for i in range(1, 15)
            ],
            "input_shot_count": 14,
            "input_shot_indices": list(range(1, 15)),
            "expected_shot_indices": list(range(1, 15)),
            "input_sources": [
                {"shot_index": i, "source_url": f"/media/w/batch/shot-{i:03d}.mp4", "source_path": f"F:/private/shot-{i:03d}.mp4", "source_path_present": True, "file_name": f"shot-{i:03d}.mp4", "file_size_bytes": 1000, "actual_duration_seconds": 10.001}
                for i in range(1, 15)
            ],
            "requested_resolution": "720p",
            "requested_ratio": "9:16",
            "duration_seconds": 158.0,
            "expected_duration_seconds": 158.0,
            "encoding": {"video": "H.264 / CRF 20", "audio": "AAC / 192k / 32 kHz / stereo"},
            "ffmpeg": {"returncode": 0},
            "probe": {"returncode": 0, "audio_stream": {"codec_name": "aac", "sample_rate": 32000, "channels": 2}},
            "audio_normalization": {"target_sample_rate_hz": 32000, "target_channels": 2, "resampled_shots": [10]},
            "compose_validation": {"status": "pass", "checks": {"ffmpeg_returncode_zero": True}},
            "status": "succeeded",
        },
    })
    content = preview["content"]
    assert content["input_shot_count"] == 14
    assert len(content["input_shots"]) == 14
    assert content["input_shots"][-1]["shot_index"] == 14
    assert content["input_shots"][-1]["actual_duration_seconds"] == 10.001
    assert all("source_path" not in item for item in content["input_shots"])
    assert content["input_shot_indices"] == list(range(1, 15))
    assert content["expected_shot_indices"] == list(range(1, 15))
    assert len(content["input_sources"]) == 14
    assert all("local_path" not in item and "source_path" not in item for item in content["input_sources"])
    assert content["requested_resolution"] == "720p"
    assert content["requested_ratio"] == "9:16"
    assert content["compose_validation"]["status"] == "pass"
    assert content["local_path_present"] is True
    assert "local_path" not in content
