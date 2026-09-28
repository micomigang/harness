# 2026-09-28 Music UI visibility fix

- Adds dedicated secondary tabs for `music_plan`, `music`, `compose`, and `delivery_qa`.
- Makes the top production-stage rail clickable.
- Adds actionable empty-state workbenches when a stage has no artifact yet.
- Exposes `跳过音乐，直接进入合成` inside the Music tab itself instead of only inside the flow composer.
- Existing music-plan/music/compose artifacts continue to use their dedicated full-width workbenches.
- No provider, model, storyboard, batch-video, or asset data is changed.
