from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    configured_ffmpeg = os.getenv("FFMPEG_PATH", "ffmpeg")
    resolved_ffmpeg = (
        configured_ffmpeg
        if Path(configured_ffmpeg).is_file()
        else shutil.which(configured_ffmpeg)
    )
    checks = {
        "python": sys.version.split()[0],
        "project_root": str(root),
        "ffmpeg": resolved_ffmpeg,
        "provider": os.getenv("HARNESS_PROVIDER", "mock"),
        "llm_key_present": bool(os.getenv("OPENAI_COMPAT_API_KEY")),
        "image_key_present": bool(os.getenv("IMAGE_API_KEY")),
        "video_key_present": bool(os.getenv("VIDEO_API_KEY")),
        "llm_model": os.getenv("OPENAI_COMPAT_MODEL"),
        "image_model": os.getenv("IMAGE_MODEL"),
        "video_model": os.getenv("VIDEO_MODEL"),
        "video_fallback_model": os.getenv("VIDEO_FALLBACK_MODEL"),
        "audio_key_present": bool(os.getenv("AUDIO_API_KEY")),
        "tts_app_id_present": bool(os.getenv("TTS_APP_ID")),
        "tts_access_token_present": bool(os.getenv("TTS_ACCESS_TOKEN")),
        "tts_voice_type_present": bool(os.getenv("TTS_VOICE_TYPE")),
        "asr_app_id_present": bool(os.getenv("ASR_APP_ID")),
        "asr_access_token_present": bool(os.getenv("ASR_ACCESS_TOKEN")),
        "asr_api_key_present": bool(os.getenv("ASR_API_KEY")),
    }
    checks["mock_ready"] = True
    checks["seedance_ready"] = bool(
        checks["video_key_present"] and checks["video_model"]
    )
    checks["seedream_ready"] = bool(
        checks["image_key_present"] and checks["image_model"]
    )
    checks["french_tts_ready"] = bool(
        checks["tts_app_id_present"]
        and checks["tts_access_token_present"]
        and checks["tts_voice_type_present"]
    )
    checks["source_asr_ready"] = bool(
        checks["asr_api_key_present"]
        or (checks["asr_app_id_present"] and checks["asr_access_token_present"])
    )
    checks["final_compose_ready"] = bool(checks["ffmpeg"])
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
