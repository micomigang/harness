from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_upload_uses_native_label_file_picker():
    html = (_root() / "static" / "index.html").read_text(encoding="utf-8")
    js = (_root() / "static" / "app.js").read_text(encoding="utf-8")
    assert 'for="sourceFileInput"' in html
    assert 'id="sourceFileInput"' in html
    assert '$("#sourceFileInput").click()' not in js
    assert 'uploadSourceFile(file)' in js


def test_frontend_assets_are_versioned_to_avoid_mixed_cache():
    html = (_root() / "static" / "index.html").read_text(encoding="utf-8")
    assert '/static/app.js?v=20260928-v23-dynamic-audio-lock' in html
    assert '/static/style.css?v=20260928-v23-dynamic-audio-lock' in html


def test_workspace_bootstrap_is_not_blocked_by_health_or_meta_failure():
    js = (_root() / "static" / "app.js").read_text(encoding="utf-8")
    assert 'refreshWorkspaceCatalog({autoSelect: false, silent: true})' in js
    assert 'Promise.all([workspacePromise, metaPromise, healthPromise, seriesPromise])' in js
    assert 'workspaceLoadError' in js
    assert '#refreshWorkspacesBtn' in js


def test_chat_first_flow_has_inline_composer_and_regeneration_controls():
    html = (_root() / "static" / "index.html").read_text(encoding="utf-8")
    js = (_root() / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="flowComposer"' in html
    assert 'id="stageGuidanceInput"' in html
    assert 'id="regenerateStageBtn"' in html
    assert 'id="flowAdvanceBtn"' in html
    assert '/stages/${stage}/regenerate' in js
    assert 'data-feedback-stage' in js
    assert 'Shift+Enter' in html


def test_flow_chat_is_pinned_into_main_canvas_and_compacts_job_updates():
    html = (_root() / "static" / "index.html").read_text(encoding="utf-8")
    js = (_root() / "static" / "app.js").read_text(encoding="utf-8")
    css = (_root() / "static" / "style.css").read_text(encoding="utf-8")
    assert 'class="flow-context-details"' in html
    assert '直接在这里和总管对话' in html
    assert 'canvas.classList.toggle("flow-active", flowActive)' in js
    assert 'function compactActivityFeed(feed)' in js
    assert '.canvas.flow-active .artifact-grid.stream-mode' in css
    assert '.canvas.flow-active .flow-composer' in css
    assert 'overflow-y: auto' in css


def test_regenerate_button_stays_bound_to_latest_ready_completed_stage():
    js = (_root() / "static" / "app.js").read_text(encoding="utf-8")
    assert "function latestReviewableStage()" in js
    assert 'if (reviewable) return reviewable;' in js
    assert 'regenButton.dataset.stage = artifact?.status === "ready" ? stage : "";' in js
    assert 'const boundStage = $("#regenerateStageBtn")?.dataset?.stage || "";' in js
    assert 'state.chatStageOverride = job.stage;' in js


def test_advancing_explicitly_rebinds_director_chat_to_next_stage():
    js = (_root() / "static" / "app.js").read_text(encoding="utf-8")
    assert "async function chatStageGuidance(messageOverride = null, stageOverride = null)" in js
    assert "const stage = stageOverride || currentGuidanceStage();" in js
    assert "const result = await chatStageGuidance(typed, stage);" in js
    assert 'state.chatStageOverride = action.stage || "";' in js


def test_visual_asset_workbench_uses_long_canvas_and_shows_director_review_state():
    js = (_root() / "static" / "app.js").read_text(encoding="utf-8")
    css = (_root() / "static" / "style.css").read_text(encoding="utf-8")
    assert 'asset-workbench-card' in js
    assert '总管已通过' in js
    assert '总管待修' in js
    assert '.artifact-card.asset-workbench-card .artifact-content' in css
    assert 'max-height: none' in css
    assert '.artifact-card.asset-workbench-card .candidate-editor textarea' in css
    assert 'min-height: 118px' in css


def test_music_and_compose_use_dedicated_full_width_workbenches():
    js = (_root() / "static" / "app.js").read_text(encoding="utf-8")
    css = (_root() / "static" / "style.css").read_text(encoding="utf-8")
    assert "function renderMusicPlan(content)" in js
    assert "function renderMusicRender(content)" in js
    assert "function renderCompose(content)" in js
    assert 'a.kind === "music_plan"' in js
    assert 'a.kind === "compose"' in js
    assert ".artifact-card.music-plan-workbench-card" in css
    assert ".artifact-card.compose-workbench-card" in css
    assert "grid-column: 1 / -1" in css


def test_music_can_be_explicitly_skipped_without_provider_call():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="skipMusicBtn"' in html
    assert '/music/skip' in js
    assert 'skipMusicAndContinue' in js
    assert 'skipped_no_bgm' in js


def test_music_stages_are_directly_navigable_and_skip_is_visible_in_music_tab():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert '["music_plan", "全片配乐方案"]' in js
    assert '["music", "音乐"]' in js
    assert '["compose", "最终合成"]' in js
    assert 'data-stage-nav' in js
    assert 'function renderMissingStageWorkbench(stage, artifacts)' in js
    assert 'data-skip-music-inline' in js
    assert '跳过音乐，直接进入合成' in js
