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
    assert '/static/app.js?v=20260923-v11' in html
    assert '/static/style.css?v=20260923-v11' in html


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
