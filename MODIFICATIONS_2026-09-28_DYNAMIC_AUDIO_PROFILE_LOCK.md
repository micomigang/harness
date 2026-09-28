# 2026-09-28 Dynamic compose audio-profile lock

## Purpose

Remove the EP02-specific fixed 32 kHz assumption from final compose while preserving the per-shot pre-concat resampling / PTS rebuild fix.

## Runtime behavior

Final compose now probes the current locked batch-video inputs first, then derives one canonical audio profile for the current compose run.

Priority:

1. An explicit compose override (`audio_sample_rate_hz` / `audio_channels`) if the user deliberately supplied one.
2. Otherwise the unique majority `(sample_rate_hz, channels)` profile among probeable input shots.
3. If there is no probeable audio, or there is a tie with no unique majority, compose fail-stops instead of guessing.

The selected profile is written into `audio_profile_lock` and reused throughout that compose execution.

Example for the current EP02 batch:

- 13 shots: 32000 Hz / 2 ch
- 1 shot: 44100 Hz / 2 ch
- lock: 32000 Hz / 2 ch, source=`batch_majority`
- only the outlier is normalized to the lock before concat

A future project whose locked shots are mainly 48000 Hz will lock to 48000 Hz automatically.

## Audio sync behavior

Each shot is still processed independently before concat:

- decode input
- resample to the dynamically locked sample rate/channel layout
- rebuild audio PTS from sample count
- reset video PTS
- concat normalized A/V streams
- encode final H.264 + AAC master
- ffprobe final output

This prevents one odd source clock from stretching a shot and shifting all later audio.

## Audit

The compose artifact now records:

- `audio_profile_lock.sample_rate_hz`
- `audio_profile_lock.channels`
- `audio_profile_lock.source`
- `audio_profile_lock.winning_count`
- `audio_profile_lock.observed_audio_streams`
- `audio_profile_lock.distribution`
- generic validation checks:
  - `audio_sample_rate_matches_lock`
  - `audio_channels_match_lock`

The UI shows the derived lock source and how many shots supported it.

## Optional explicit override

Compose accepts:

- `audio_sample_rate_hz` (8000-192000)
- `audio_channels` (1 or 2)

These remain user-controlled technical overrides and are not silently invented by the Director.

## Dynamic behavior

No runtime rule is hard-coded to 14 shots, 158 seconds, or 32000 Hz.

## Validation

- `pytest -q` -> 171 passed
- `node --check static/app.js` -> passed
