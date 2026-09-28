from pathlib import Path
from types import SimpleNamespace

from app.providers.local_media import LocalMediaProvider


def test_compose_records_ffmpeg_ffprobe_and_dynamically_locks_majority_audio(monkeypatch, tmp_path: Path):
    shot1 = tmp_path / "shot-001.mp4"
    shot2 = tmp_path / "shot-002.mp4"
    shot3 = tmp_path / "shot-003.mp4"
    for path in (shot1, shot2, shot3):
        path.write_bytes(b"shot")
    provider = LocalMediaProvider(ffmpeg_path=str(tmp_path / "ffmpeg"), output_dir=tmp_path / "outputs")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        exe = Path(str(command[0])).name.lower()
        target = Path(str(command[-1]))
        if "ffprobe" in exe:
            sample_rate = "44100" if target.name == "shot-002.mp4" else "32000"
            duration = "32.000" if target.name == "final.mp4" else "10.000"
            if target.name == "shot-003.mp4":
                duration = "12.000"
            payload = (
                '{"format":{"duration":"%s","size":"1000","format_name":"mov,mp4"},'
                '"streams":['
                '{"index":0,"codec_type":"video","codec_name":"h264","width":720,"height":1280},'
                '{"index":1,"codec_type":"audio","codec_name":"aac","sample_rate":"%s","channels":2}'
                ']}' % (duration, sample_rate)
            )
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"final-video")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.providers.local_media.subprocess.run", fake_run)
    context = {
        "workspace": {"id": "w1", "settings": {"aspect_ratio": "9:16"}},
        "execution_directive": {"parameter_overrides": {}},
        "artifacts": [
            {
                "kind": "storyboard",
                "content": {
                    "shots": [
                        {"index": 1, "duration_seconds": 10},
                        {"index": 2, "duration_seconds": 10},
                        {"index": 3, "duration_seconds": 12},
                    ]
                },
            },
            {
                "kind": "batch_video",
                "content": {
                    "items": [
                        {"shot_index": 1, "status": "succeeded", "local_path": str(shot1)},
                        {"shot_index": 2, "status": "succeeded", "local_path": str(shot2)},
                        {"shot_index": 3, "status": "succeeded", "local_path": str(shot3)},
                    ]
                },
            },
            {"kind": "music", "content": {"mode": "skipped_no_bgm"}},
        ],
    }

    result = provider.generate("compose", context)

    ffmpeg_command = next(cmd for cmd in commands if "ffprobe" not in Path(str(cmd[0])).name.lower())
    assert ffmpeg_command[ffmpeg_command.index("-ar") + 1] == "32000"
    assert ffmpeg_command[ffmpeg_command.index("-ac") + 1] == "2"
    filter_complex = ffmpeg_command[ffmpeg_command.index("-filter_complex") + 1]
    assert "[0:a:0]aresample=32000:first_pts=0" in filter_complex
    assert "[1:a:0]aresample=32000:first_pts=0" in filter_complex
    assert "[2:a:0]aresample=32000:first_pts=0" in filter_complex
    assert "concat=n=3:v=1:a=1[vout][aout]" in filter_complex
    assert result["status"] == "succeeded"
    assert result["duration_seconds"] == 32.0
    assert result["expected_duration_seconds"] == 32.0
    assert result["audio_profile_lock"]["source"] == "batch_majority"
    assert result["audio_profile_lock"]["sample_rate_hz"] == 32000
    assert result["audio_profile_lock"]["channels"] == 2
    assert result["audio_profile_lock"]["winning_count"] == 2
    assert result["audio_profile_lock"]["observed_audio_streams"] == 3
    assert result["audio_normalization"]["resampled_shots"] == [2]
    assert result["compose_validation"]["checks"]["audio_sample_rate_matches_lock"] is True
    assert result["compose_validation"]["checks"]["audio_channels_match_lock"] is True
    assert result["compose_validation"]["status"] == "pass"


def test_compose_uses_dynamic_48khz_profile_when_batch_majority_is_48khz(monkeypatch, tmp_path: Path):
    paths = [tmp_path / f"shot-{index:03d}.mp4" for index in range(1, 4)]
    for path in paths:
        path.write_bytes(b"shot")
    provider = LocalMediaProvider(ffmpeg_path=str(tmp_path / "ffmpeg"), output_dir=tmp_path / "outputs")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        exe = Path(str(command[0])).name.lower()
        target = Path(str(command[-1]))
        if "ffprobe" in exe:
            rate = "44100" if target.name == "shot-003.mp4" else "48000"
            if target.name == "final.mp4":
                rate = "48000"
            payload = (
                '{"format":{"duration":"30.000","size":"1000","format_name":"mov,mp4"},'
                '"streams":['
                '{"index":0,"codec_type":"video","codec_name":"h264","width":720,"height":1280},'
                '{"index":1,"codec_type":"audio","codec_name":"aac","sample_rate":"%s","channels":2}'
                ']}' % rate
            )
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"final-video")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.providers.local_media.subprocess.run", fake_run)
    context = {
        "workspace": {"id": "w48", "settings": {"aspect_ratio": "9:16"}},
        "execution_directive": {"parameter_overrides": {}},
        "artifacts": [
            {"kind": "storyboard", "content": {"shots": [{"index": i, "duration_seconds": 10} for i in range(1, 4)]}},
            {"kind": "batch_video", "content": {"items": [
                {"shot_index": i, "status": "succeeded", "local_path": str(path)} for i, path in enumerate(paths, 1)
            ]}},
        ],
    }
    result = provider.generate("compose", context)
    ffmpeg_command = next(cmd for cmd in commands if "ffprobe" not in Path(str(cmd[0])).name.lower())
    assert ffmpeg_command[ffmpeg_command.index("-ar") + 1] == "48000"
    assert result["audio_profile_lock"]["sample_rate_hz"] == 48000
    assert result["audio_normalization"]["resampled_shots"] == [3]
    assert "48 kHz" in result["encoding"]["audio"]


def test_compose_fails_stop_on_tied_audio_profiles_without_explicit_override(monkeypatch, tmp_path: Path):
    shot1 = tmp_path / "shot-001.mp4"
    shot2 = tmp_path / "shot-002.mp4"
    shot1.write_bytes(b"one")
    shot2.write_bytes(b"two")
    provider = LocalMediaProvider(ffmpeg_path=str(tmp_path / "ffmpeg"), output_dir=tmp_path / "outputs")

    def fake_run(command, **kwargs):
        target = Path(str(command[-1]))
        rate = "32000" if target.name == "shot-001.mp4" else "44100"
        payload = (
            '{"format":{"duration":"10.000","size":"1000","format_name":"mov,mp4"},'
            '"streams":['
            '{"index":0,"codec_type":"video","codec_name":"h264","width":720,"height":1280},'
            '{"index":1,"codec_type":"audio","codec_name":"aac","sample_rate":"%s","channels":2}'
            ']}' % rate
        )
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr("app.providers.local_media.subprocess.run", fake_run)
    context = {
        "workspace": {"id": "wtie", "settings": {"aspect_ratio": "9:16"}},
        "execution_directive": {"parameter_overrides": {}},
        "artifacts": [
            {"kind": "storyboard", "content": {"shots": [{"index": 1, "duration_seconds": 10}, {"index": 2, "duration_seconds": 10}]}},
            {"kind": "batch_video", "content": {"items": [
                {"shot_index": 1, "status": "succeeded", "local_path": str(shot1)},
                {"shot_index": 2, "status": "succeeded", "local_path": str(shot2)},
            ]}},
        ],
    }
    import pytest
    with pytest.raises(Exception, match="no unique majority audio profile"):
        provider.generate("compose", context)


def test_compose_duration_tolerance_scales_with_shot_count(monkeypatch, tmp_path: Path):
    shots = []
    items = []
    for index in range(1, 15):
        path = tmp_path / f"shot-{index:03d}.mp4"
        path.write_bytes(b"shot")
        shots.append({"index": index, "duration_seconds": 10})
        items.append({
            "shot_index": index,
            "status": "succeeded",
            "local_path": str(path),
            "url": f"/media/w/batch/shot-{index:03d}.mp4",
            "resolution": "720p",
            "ratio": "9:16",
        })

    provider = LocalMediaProvider(ffmpeg_path=str(tmp_path / "ffmpeg"), output_dir=tmp_path / "outputs")

    def fake_run(command, **kwargs):
        exe = Path(str(command[0])).name.lower()
        target = Path(str(command[-1]))
        if "ffprobe" in exe:
            duration = "141.251" if target.name == "final.mp4" else "10.000"
            payload = (
                '{"format":{"duration":"%s","size":"1000","format_name":"mov,mp4"},'
                '"streams":['
                '{"index":0,"codec_type":"video","codec_name":"h264","width":720,"height":1280},'
                '{"index":1,"codec_type":"audio","codec_name":"aac","sample_rate":"32000","channels":2}'
                ']}' % duration
            )
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"final-video")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.providers.local_media.subprocess.run", fake_run)
    context = {
        "workspace": {"id": "w14", "settings": {"aspect_ratio": "9:16"}},
        "execution_directive": {"parameter_overrides": {}},
        "artifacts": [
            {"kind": "storyboard", "content": {"shots": shots}},
            {"kind": "batch_video", "content": {"items": items}},
            {"kind": "music", "content": {"mode": "skipped_no_bgm"}},
        ],
    }

    result = provider.generate("compose", context)
    assert result["duration_delta_seconds"] == 1.251
    assert result["compose_validation"]["duration_tolerance_seconds"] == 1.4
    assert result["compose_validation"]["checks"]["duration_within_tolerance"] is True
    assert result["compose_validation"]["status"] == "pass"
    assert result["input_shot_count"] == 14
    assert result["input_shot_indices"] == list(range(1, 15))
    assert len(result["input_shots"]) == 14
    assert len(result["input_sources"]) == 14
    assert all(row["actual_duration_seconds"] == 10.0 for row in result["input_shots"])
    assert result["compose_validation"]["shot_duration_comparison"][-1]["shot_index"] == 14
    assert result["requested_resolution"] == "720p"
    assert result["requested_ratio"] == "9:16"
