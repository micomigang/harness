from pathlib import Path

from app.providers.source_media import SourceMediaProcessor


def _processor(tmp_path: Path, **kwargs) -> SourceMediaProcessor:
    return SourceMediaProcessor(
        ffmpeg_path="ffmpeg",
        output_dir=tmp_path,
        **kwargs,
    )


def test_effective_fps_keeps_one_fps_for_short_video():
    assert SourceMediaProcessor._effective_fps(258.9, 1.0, 1200) == 1.0


def test_effective_fps_adapts_for_long_video():
    assert SourceMediaProcessor._effective_fps(2400.0, 1.0, 1200) == 0.5


def test_parse_showinfo_timestamps():
    stderr = "pts: 0 pts_time:0.000 pos:1\npts: 90 pts_time:1.500 pos:2\n"
    assert SourceMediaProcessor._parse_pts_times(stderr) == [0.0, 1.5]


def test_parse_showinfo_timestamps_accepts_none():
    assert SourceMediaProcessor._parse_pts_times(None) == []


def test_merge_keeps_scene_cut_close_to_baseline():
    merged = SourceMediaProcessor._merge_frames(
        [{"timestamp_seconds": 10.0, "kind": "base"}],
        [{"timestamp_seconds": 10.05, "kind": "scene"}],
    )
    assert [frame["kind"] for frame in merged] == ["base", "scene"]


def test_model_selection_keeps_all_frames_when_under_budget(tmp_path: Path):
    processor = _processor(
        tmp_path,
        max_model_frames=10,
        max_model_image_bytes=1_000_000,
    )
    frames = [
        {
            "timestamp_seconds": float(index),
            "kind": "base",
            "size_bytes": 1000,
            "local_path": str(tmp_path / f"{index}.jpg"),
        }
        for index in range(5)
    ]
    assert processor._select_model_frame_indexes(frames) == {0, 1, 2, 3, 4}


def test_model_selection_prioritizes_scene_frames_and_caps_count(tmp_path: Path):
    processor = _processor(
        tmp_path,
        max_model_frames=4,
        max_model_image_bytes=1_000_000,
    )
    frames = [
        {"timestamp_seconds": 0.0, "kind": "base", "size_bytes": 1000},
        {"timestamp_seconds": 1.0, "kind": "scene", "size_bytes": 1000},
        {"timestamp_seconds": 2.0, "kind": "base", "size_bytes": 1000},
        {"timestamp_seconds": 3.0, "kind": "scene", "size_bytes": 1000},
        {"timestamp_seconds": 4.0, "kind": "base", "size_bytes": 1000},
        {"timestamp_seconds": 5.0, "kind": "base", "size_bytes": 1000},
    ]
    selected = processor._select_model_frame_indexes(frames)
    assert len(selected) == 4
    assert {1, 3}.issubset(selected)


def test_build_analysis_segments_splits_by_30_seconds_and_caps_frames(tmp_path: Path):
    processor = _processor(tmp_path)
    frames = []
    for i in range(60):
        frames.append(
            {
                "timestamp_seconds": float(i),
                "kind": "scene" if i in {5, 15, 35, 45} else "base",
                # Intentionally false: segmented mode must use all extracted frames,
                # not the legacy global single-request selection flag.
                "selected_for_model": False,
                "local_path": str(tmp_path / f"{i}.jpg"),
            }
        )
    media = {"duration_seconds": 60.0, "frames": frames}
    segments = processor.build_analysis_segments(
        media,
        segment_seconds=30,
        max_frames_per_segment=12,
    )
    assert len(segments) == 2
    assert segments[0]["start_seconds"] == 0.0
    assert segments[0]["end_seconds"] == 30.0
    assert segments[1]["start_seconds"] == 30.0
    assert segments[1]["end_seconds"] == 60.0
    assert all(segment["frame_count"] <= 12 for segment in segments)
    assert any(frame["kind"] == "scene" for frame in segments[0]["frames"])


def test_media_manifest_cache_round_trip(tmp_path: Path):
    processor = _processor(tmp_path)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video-placeholder")
    target_dir = tmp_path / "workspace" / "source_frames" / "source"
    target_dir.mkdir(parents=True)
    frame = target_dir / "base-00001.jpg"
    frame.write_bytes(b"jpeg")
    audio = target_dir / "source-audio.mp3"
    audio.write_bytes(b"audio")

    identity = processor._media_cache_identity(source, "sha-123")
    media = {
        "name": source.name,
        "path": str(source),
        "source_sha256": "sha-123",
        "duration_seconds": 30.0,
        "size_bytes": source.stat().st_size,
        "format": "mp4",
        "video_stream": {"codec_type": "video", "codec_name": "h264"},
        "audio_stream": {"codec_type": "audio", "codec_name": "aac"},
        "audio_preview": {"local_path": str(audio)},
        "sampling": {"effective_base_fps": 1.0},
        "frames": [
            {
                "timestamp_seconds": 0.0,
                "kind": "base",
                "local_path": str(frame),
                "index": 1,
                "selected_for_model": True,
            }
        ],
    }
    processor._write_media_cache(target_dir, identity, media)
    restored = processor._load_media_cache(target_dir, identity)

    assert restored is not None
    assert restored["duration_seconds"] == 30.0
    assert restored["frames"][0]["local_path"] == str(frame)
    assert restored["audio_preview"]["local_path"] == str(audio)


def test_process_deduplicates_legacy_duplicate_source_entries(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    processor = _processor(tmp_path)
    calls = []

    def fake_process(workspace_id, source_path, *, source_sha256=""):
        calls.append((workspace_id, source_path, source_sha256))
        return {"name": source_path.name, "source_sha256": source_sha256}

    processor._process_video = fake_process
    files = [
        {"path": str(source), "content_type": "video/mp4", "sha256": "same-hash"},
        {"path": str(source), "content_type": "video/mp4", "sha256": "same-hash"},
        {"path": str(source), "content_type": "video/mp4", "sha256": "same-hash"},
    ]
    result = processor.process("workspace", files)
    assert len(result) == 1
    assert len(calls) == 1
