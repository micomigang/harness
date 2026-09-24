const state = {
  meta: null,
  workspaces: [],
  detail: null,
  selectedId: null,
  tab: "flow",
  upload: null,
  workspaceLoadError: "",
  assetCatalog: [],
  assetItems: {},
  selectedAssetLibraryId: "",
  seriesCatalog: [],
  activityStream: null,
  streamRefreshBusy: false,
  chatStageOverride: "",
  pendingChat: null,
  candidateBases: {},
  batchSelections: {},
};

let jobWatchTimer = null;
let jobWatchBusy = false;

const $ = (selector) => document.querySelector(selector);
const createDialog = $("#createDialog");

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    ...options,
    headers,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || body.message || `HTTP ${response.status}`);
  return body;
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2200);
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    const capabilityValues = Object.values(health.capabilities || {});
    const readyCount = capabilityValues.filter(Boolean).length;
    const badge = $("#providerBadge");
    if (badge) badge.textContent = `provider: ${health.provider} · ${readyCount}/${capabilityValues.length} ready`;
  } catch (error) {
    const badge = $("#providerBadge");
    if (badge) badge.textContent = "provider: 状态读取失败";
    console.error("health bootstrap failed", error);
  }
}

async function ensureMeta() {
  if (state.meta) return state.meta;
  state.meta = await api("/api/meta");
  renderWorkspaceList();
  return state.meta;
}

async function refreshWorkspaceCatalog({autoSelect = false, silent = false} = {}) {
  try {
    const workspaces = await api("/api/workspaces");
    state.workspaces = Array.isArray(workspaces) ? workspaces : [];
    state.workspaceLoadError = "";
    renderWorkspaceList();

    const selectedExists = Boolean(state.selectedId && state.workspaces.some(item => item.id === state.selectedId));
    if (!state.workspaces.length) {
      state.selectedId = null;
      state.detail = null;
      const view = $("#workspaceView");
      const empty = $("#emptyState");
      const deleteButton = $("#deleteWorkspaceBtn");
      if (view) view.classList.add("hidden");
      if (empty) empty.classList.remove("hidden");
      if (deleteButton) deleteButton.disabled = true;
      return [];
    }

    if (autoSelect || !selectedExists) {
      await selectWorkspace(selectedExists ? state.selectedId : state.workspaces[0].id);
    }
    if (!silent) toast(`已刷新工作区：${state.workspaces.length} 个`);
    return state.workspaces;
  } catch (error) {
    state.workspaceLoadError = error.message || String(error);
    renderWorkspaceList();
    if (!silent) toast(`工作区读取失败：${state.workspaceLoadError}`);
    throw error;
  }
}

async function init() {
  // Workspace discovery is intentionally independent from health/meta. A broken
  // provider or meta endpoint must never hide existing projects from the sidebar.
  const workspacePromise = refreshWorkspaceCatalog({autoSelect: false, silent: true}).catch(() => []);
  const metaPromise = ensureMeta().catch(error => {
    console.error("meta bootstrap failed", error);
    return null;
  });
  const healthPromise = loadHealth();
  const seriesPromise = refreshSeriesCatalog(true).catch(() => []);

  await Promise.all([workspacePromise, metaPromise, healthPromise, seriesPromise]);
  renderWorkspaceList();

  if (state.workspaces.length) {
    try {
      await selectWorkspace(state.workspaces[0].id);
    } catch (error) {
      console.error("initial workspace selection failed", error);
      toast(`工作区已找到，但详情加载失败：${error.message}`);
    }
  }
}

function renderWorkspaceList() {
  const root = $("#workspaceList");
  if (!root) return;
  const listHtml = state.workspaces.map(item => `
    <button class="workspace-item ${item.id === state.selectedId ? "active" : ""}" data-id="${item.id}">
      <strong>${escapeHtml(item.title)}</strong>
      <span>${escapeHtml(state.meta?.labels?.[item.stage] || item.stage || "unknown")} · ${escapeHtml(item.status || "")}</span>
    </button>
  `).join("");
  const errorHtml = state.workspaceLoadError
    ? `<div class="workspace-load-error">工作区读取失败：${escapeHtml(state.workspaceLoadError)}<br>点击上方“刷新”重试。</div>`
    : "";
  root.innerHTML = `${errorHtml}${listHtml || (!state.workspaceLoadError ? `<div class="sidebar-note">尚无工作区。</div>` : "")}`;
  root.querySelectorAll(".workspace-item").forEach(el => {
    el.addEventListener("click", () => selectWorkspace(el.dataset.id).catch(error => toast(error.message)));
  });
}

async function selectWorkspace(id) {
  await ensureMeta();
  state.selectedId = id;
  state.detail = await api(`/api/workspaces/${id}`);
  $("#emptyState").classList.add("hidden");
  $("#workspaceView").classList.remove("hidden");
  $("#deleteWorkspaceBtn").disabled = false;
  renderWorkspaceList();
  renderDetail();
  startWorkspaceStream(id);
}

function renderDetail() {
  const { workspace, artifacts, approvals, jobs, events, next_actions } = state.detail;
  $("#workspaceTitle").textContent = workspace.title;
  $("#workspaceBrief").textContent = workspace.brief;
  $("#artifactCount").textContent = artifacts.length;
  $("#jobCount").textContent = jobs.length;
  $("#approvedCount").textContent = approvals.filter(x => x.status === "approved").length;
  renderStageRail(artifacts);
  renderTabs();
  renderArtifacts(artifacts);
  renderNextAction(next_actions);
  renderGuidancePanel(next_actions);
  renderApprovals(approvals);
  renderJobs(jobs);
  renderEvents(events);
  const composer = $("#flowComposer");
  const canvas = document.querySelector(".canvas");
  const layout = document.querySelector(".canvas-layout");
  const flowActive = state.tab === "flow";
  if (composer) composer.classList.toggle("hidden", !flowActive);
  if (canvas) canvas.classList.toggle("flow-active", flowActive);
  if (layout) layout.classList.toggle("flow-layout", flowActive);
  syncJobWatcher();
}

function renderStageRail(artifacts) {
  const byKind = Object.fromEntries(artifacts.map(x => [x.kind, x]));
  const agents = Object.fromEntries((state.meta.agents || []).map(x => [x.id, x]));
  $("#stageRail").innerHTML = state.meta.stages.map(stage => {
    const artifact = byKind[stage];
    const status = artifact?.status || "missing";
    const agent = agents[state.meta.stage_agents?.[stage]];
    return `<div class="stage-step ${status}" title="${escapeHtml(agent?.name || "未分配")} · ${status}">${escapeHtml(state.meta.labels[stage])}</div>`;
  }).join("");
}

function renderTabs() {
  const tabs = [
    ["flow", "制作对话流"], ["overview", "总览"], ["asset_library", "主题资产库"], ["agents", "Agent 职责"], ["source", "素材"], ["analysis", "素材分析"], ["script", "剧本"], ["asset_manifest", "资产解析"], ["characters", "角色"],
    ["scenes", "场景背景"], ["props", "道具"], ["reference_images", "参考图"], ["storyboard", "分镜"],
    ["plans", "声音/QA"], ["media", "视频/交付"]
  ];
  $("#stageTabs").innerHTML = tabs.map(([id, label]) =>
    `<button class="tab ${state.tab === id ? "active" : ""}" data-tab="${id}">${label}</button>`
  ).join("");
  $("#stageTabs").querySelectorAll(".tab").forEach(el => {
    el.addEventListener("click", async () => {
      state.tab = el.dataset.tab;
      if (state.tab === "asset_library") await refreshAssetCatalog(true);
      renderDetail();
    });
  });
}

function renderArtifacts(artifacts) {
  if (state.tab === "flow") {
    $("#artifactGrid")?.classList.add("stream-mode");
    $("#artifactGrid")?.classList.remove("asset-mode");
    renderActivityFeed();
    return;
  }
  if (state.tab === "asset_library") {
    $("#artifactGrid")?.classList.add("asset-mode");
    $("#artifactGrid")?.classList.remove("stream-mode");
    renderAssetLibrary();
    return;
  }
  $("#artifactGrid")?.classList.remove("stream-mode", "asset-mode");
  if (state.tab === "agents") {
    renderAgentContracts();
    return;
  }
  const planKinds = new Set(["dialogue_plan", "sound_plan", "review", "music_plan"]);
  const mediaKinds = new Set(["preview", "batch_video", "music", "compose", "delivery_qa"]);
  let visible = artifacts;
  if (state.tab !== "overview") {
    if (state.tab === "media") visible = artifacts.filter(a => mediaKinds.has(a.kind));
    else if (state.tab === "plans") visible = artifacts.filter(a => planKinds.has(a.kind));
    else visible = artifacts.filter(a => a.kind === state.tab);
  }
  const root = $("#artifactGrid");
  if (!visible.length) {
    root.innerHTML = `<div class="empty-canvas">该阶段尚无产物。使用“推进下一步”运行可用阶段。</div>`;
    return;
  }
  root.innerHTML = visible.map(artifactCard).join("");
  bindStageResetButtons();
  bindArtifactFeedbackButtons();
  bindAssetCandidateUi();
}

function renderAgentContracts() {
  const root = $("#artifactGrid");
  root.innerHTML = (state.meta.agents || []).map(agent => `
    <article class="artifact-card agent-contract">
      <div class="artifact-card-header">
        <div><strong>${escapeHtml(agent.name)}</strong><div class="artifact-meta">${escapeHtml(agent.provider)}</div></div>
        <span class="pill ready">${escapeHtml((agent.stages || []).map(x => state.meta.labels[x] || x).join(" / ") || "调度")}</span>
      </div>
      <p>${escapeHtml(agent.purpose)}</p>
      <div class="contract-row"><b>负责</b><span>${escapeHtml((agent.owns || []).join("；"))}</span></div>
      <div class="contract-row"><b>禁止越界</b><span>${escapeHtml((agent.must_not || []).join("；"))}</span></div>
      <div class="contract-row"><b>调试断言</b><span>${escapeHtml((agent.debug_checks || []).join("；"))}</span></div>
    </article>
  `).join("");
}

function artifactCard(a) {
  const agentId = a.content?._agent?.id || state.meta.stage_agents?.[a.kind];
  const agent = (state.meta.agents || []).find(item => item.id === agentId);
  const reviewRow = state.detail?.stage_reviews?.[a.kind];
  const currentReview = reviewRow && Number(reviewRow.artifact_revision || 0) === Number(a.revision || 0) ? (reviewRow.review || {}) : null;
  const reviewAction = String(currentReview?.recommended_action || "");
  const reviewBadge = reviewAction === "proceed"
    ? `<span class="pill ready review-status-pill" title="总管复盘已通过。右侧二次确认如仍显示 pending，表示等待人工确认，不代表总管拒绝。">总管已通过</span>`
    : (["regenerate_current", "wait_for_user"].includes(reviewAction)
      ? `<span class="pill stale review-status-pill" title="总管复盘认为当前 revision 仍需处理。">总管待修</span>`
      : "");
  const visualStageClass = ["characters", "scenes", "props"].includes(a.kind) ? " asset-workbench-card" : "";
  return `
    <article class="artifact-card ${a.status}${visualStageClass}">
      <div class="artifact-card-header">
        <div><strong>${escapeHtml(a.name)}</strong><div class="artifact-meta">${escapeHtml(agent?.name || "未分配 Agent")} · rev ${a.revision} · ${escapeHtml(a.provider)}</div></div>
        <div class="artifact-header-actions">
          <span class="pill ${a.status}">${escapeHtml(a.status)}</span>
          ${reviewBadge}
          ${a.kind !== "source" ? `<button class="mini-button" data-feedback-stage="${escapeHtml(a.kind)}">在对话流中调整</button><button class="mini-button reject stage-reset-button" data-reset-stage="${escapeHtml(a.kind)}">清理此节点</button>` : ""}
        </div>
      </div>
      <div class="artifact-content">${renderExecutionInfo(a.content?._execution)}${renderStageReview(a)}${renderContent(a.content, a.kind)}</div>
    </article>
  `;
}

function renderStageReview(artifact) {
  const row = state.detail?.stage_reviews?.[artifact.kind];
  if (!row || Number(row.artifact_revision || 0) !== Number(artifact.revision || 0)) return "";
  const review = row.review || {};
  const action = review.recommended_action || "";
  const bad = ["regenerate_current", "wait_for_user"].includes(action);
  return `<div class="director-review ${bad ? "blocking" : "ok"}">
    <b>总管生成复盘 · ${escapeHtml(action || "reviewed")}</b>
    ${review.assessment ? `<div>${escapeHtml(review.assessment)}</div>` : ""}
    ${Array.isArray(review.deviations) && review.deviations.length ? `<div><strong>偏差：</strong>${escapeHtml(review.deviations.join("；"))}</div>` : ""}
    ${Array.isArray(review.suggested_adjustments) && review.suggested_adjustments.length ? `<div><strong>建议：</strong>${escapeHtml(review.suggested_adjustments.join("；"))}</div>` : ""}
  </div>`;
}

function activityEventText(item) {
  const type = item.type || "event";
  const p = item.payload || {};
  if (type === "job.updated") return `${state.meta.labels[p.stage] || p.stage || "任务"} · ${p.status || ""} · ${Number(p.progress || 0)}% · ${p.message || ""}`;
  if (type === "artifact.updated") return `${state.meta.labels[p.kind] || p.kind || "产物"} 已更新 · rev ${p.revision || ""} · ${p.status || ""}`;
  if (type === "approval.changed") return `审批 ${p.gate || ""} → ${p.status || ""}`;
  if (type === "director.review") return `${state.meta.labels[p.stage] || p.stage || "阶段"} 总管复盘 → ${p.recommended_action || "reviewed"}${p.assessment ? ` · ${p.assessment}` : ""}`;
  if (type.startsWith("asset_library.")) return `资产库 · ${type.split(".").slice(1).join(".")} · ${p.item_count ?? ""}`;
  if (type === "workspace.created") return `工作区已创建：${p.title || ""}`;
  if (type === "memory.updated") return "项目长期记忆已更新";
  if (type === "stage.regeneration_requested") return `${state.meta.labels[p.stage] || p.stage} 收到修改意见 · 将基于 rev ${p.previous_revision || "?"} 重新生成，并使下游失效`;
  if (type === "asset_candidate.generated") return `${state.meta.labels[p.stage] || p.stage} · ${p.canonical_key || "资产"} 新增 ${p.count || 1} 张视觉候选${p.has_feedback ? " · 已应用局部调整要求" : ""}`;
  if (type === "asset_candidate.batch_generated") return `${state.meta.labels[p.stage] || p.stage} · 已为 ${p.asset_count || 0} 项资产批量生成视觉候选（每项 ${p.count_per_asset || 1} 张）${p.has_feedback ? " · 已应用统一要求" : ""}`;
  if (type === "asset_candidate.selected") return `${state.meta.labels[p.stage] || p.stage} · ${p.canonical_key || "资产"} 已选定视觉候选，并使相关下游结果失效`;
  if (type === "asset_candidate.deleted") return `${state.meta.labels[p.stage] || p.stage} · ${p.canonical_key || "资产"} 删除了一张视觉候选`;
  return `${type} · ${JSON.stringify(p)}`;
}

function activityArtifactPreview(kind) {
  const artifact = (state.detail?.artifacts || []).find(item => item.kind === kind);
  if (!artifact) return "";
  const c = artifact.content || {};
  let text = c.title || c.logline || c.source_summary || c.summary || c.note || "";
  if (text && typeof text === "object") {
    text = text.text || text.summary || text.note || text.description || JSON.stringify(text);
  }
  if (!text && Array.isArray(c.items)) text = `${c.items.length} 项结构化资产`;
  if (!text && Array.isArray(c.shots)) text = `${c.shots.length} 个镜头`;
  text = String(text || "").replace(/\s+/g, " ").trim();
  if (text.length > 220) text = text.slice(0, 220) + "…";
  return text;
}

function compactActivityFeed(feed) {
  const latestJobIndex = new Map();
  feed.forEach((item, index) => {
    if (item?.type === "job.updated") {
      const key = item.payload?.job_id || `${item.payload?.stage || "job"}`;
      latestJobIndex.set(key, index);
    }
  });
  return feed.filter((item, index) => {
    if (item?.type !== "job.updated") return true;
    const key = item.payload?.job_id || `${item.payload?.stage || "job"}`;
    return latestJobIndex.get(key) === index;
  });
}

function renderActivityFeed() {
  const root = $("#artifactGrid");
  const rawFeed = state.detail?.activity_feed || [];
  const feed = compactActivityFeed(rawFeed);
  if (!feed.length) {
    root.innerHTML = `<div class="empty-canvas">还没有制作对话。直接在下方输入框告诉总管你的要求，然后开始推进流程。</div>`;
    return;
  }
  root.innerHTML = `<div class="production-stream">${feed.map(item => {
    if (item.kind === "message") {
      const role = item.role || "system";
      const stage = item.stage ? (state.meta.labels[item.stage] || item.stage) : "";
      const badge = item.metadata?.kind === "post_generation_review" ? "生成复盘" : (item.metadata?.kind === "asset_ingest" ? "资产归档" : stage);
      return `<div class="stream-row message ${escapeHtml(role)}"><div class="stream-avatar">${role === "user" ? "你" : role === "director" ? "✦" : "•"}</div><div class="stream-bubble"><div class="stream-meta">${escapeHtml(role === "user" ? "用户" : role === "director" ? "艺术总监 / Kimi" : "Harness")} · ${escapeHtml(badge)}</div><div>${escapeHtml(item.content || "")}</div></div></div>`;
    }
    if (["director.chat", "memory.updated"].includes(item.type)) return "";
    const isJob = item.type === "job.updated";
    const p = item.payload || {};
    const stage = p.stage || p.kind || "";
    const preview = item.type === "artifact.updated" ? activityArtifactPreview(p.kind) : "";
    const canAdjust = ["artifact.updated", "director.review"].includes(item.type) && stage && stage !== "source";
    const eventLabel = isJob ? `${state.meta.labels[p.stage] || p.stage || "任务"} · 当前进度` : "Harness / Agent";
    return `<div class="stream-row event ${isJob ? "job-live-card" : ""}"><div class="stream-avatar">${isJob ? "↻" : "✓"}</div><div class="stream-bubble"><div class="stream-meta">${escapeHtml(eventLabel)}</div><div>${escapeHtml(activityEventText(item))}</div>${preview ? `<div class="stream-artifact-preview">${escapeHtml(preview)}</div>` : ""}${isJob ? `<div class="progress stream-progress"><span style="width:${Number(p.progress || 0)}%"></span></div>` : ""}${canAdjust ? `<div class="stream-inline-actions"><button class="mini-button" data-feedback-stage="${escapeHtml(stage)}">给这个节点提修改意见</button></div>` : ""}</div></div>`;
  }).join("")}${state.pendingChat ? `<div class="stream-row message user pending"><div class="stream-avatar">你</div><div class="stream-bubble"><div class="stream-meta">用户 · ${escapeHtml(state.meta.labels[state.pendingChat.stage] || state.pendingChat.stage || "")}</div><div>${escapeHtml(state.pendingChat.message || "")}</div></div></div><div class="stream-row message director pending"><div class="stream-avatar">✦</div><div class="stream-bubble"><div class="stream-meta">艺术总监 / Kimi</div><div class="typing-indicator"><span></span><span></span><span></span></div></div></div>` : ""}</div>`;
  bindArtifactFeedbackButtons();
  requestAnimationFrame(() => { root.scrollTop = root.scrollHeight; });
}

function renderExecutionInfo(execution) {
  if (!execution || typeof execution !== "object") return "";
  const instruction = execution.user_instruction || "";
  const interpretation = execution.director_interpretation || "";
  const params = execution.parameter_overrides || {};
  if (!instruction && !interpretation && !Object.keys(params).length) return "";
  return `<div class="execution-note">
    <b>总管执行上下文</b>
    ${instruction ? `<div><strong>用户要求：</strong>${escapeHtml(instruction)}</div>` : ""}
    ${interpretation ? `<div><strong>总管理解：</strong>${escapeHtml(interpretation)}</div>` : ""}
    ${Object.keys(params).length ? `<div><strong>参数：</strong><code>${escapeHtml(JSON.stringify(params))}</code></div>` : ""}
  </div>`;
}

function renderContent(content, kind) {
  if (kind === "source") return renderSource(content);
  if (kind === "script") return renderScript(content);
  if (kind === "asset_manifest") return renderAssetManifest(content);
  if (["characters", "scenes", "props"].includes(kind)) return renderAssetDesignWorkbench(content, kind);
  if (kind === "analysis" && Array.isArray(content?.source_media)) {
    const allFrames = content.source_media.flatMap(media => (media.frames || []).map(frame => ({...frame, media_name: media.name})));
    const previewLimit = 80;
    const previewFrames = allFrames.length <= previewLimit
      ? allFrames
      : Array.from({length: previewLimit}, (_, index) => allFrames[Math.round(index * (allFrames.length - 1) / (previewLimit - 1))]);
    const previews = previewFrames.map(frame => `
      <div class="artifact-item">
        <b>${escapeHtml(frame.media_name || "源视频")} · ${escapeHtml(frame.timestamp_seconds)}s</b>
        <div class="artifact-meta">${escapeHtml(frame.kind || "sample")}${frame.selected_for_model ? " · 已发送 Kimi" : ""}</div>
        ${renderMedia(frame.url)}
      </div>
    `).join("");
    const audio = content.source_media.map(media => renderMedia(media.audio_preview?.url)).join("");
    const summary = {
      ...content,
      source_media: content.source_media.map(media => ({
        ...media,
        frames: `[${(media.frames || []).length} frames; previewing ${Math.min((media.frames || []).length, previewLimit)} at most]`,
      })),
    };
    const previewNote = allFrames.length > previewFrames.length
      ? `<div class="artifact-meta">共 ${allFrames.length} 帧，界面均匀预览 ${previewFrames.length} 帧；完整帧文件仍保存在 data/outputs。</div>`
      : "";
    return `${audio}${previewNote}${previews ? `<div class="artifact-items source-frame-grid">${previews}</div>` : ""}<pre>${escapeHtml(JSON.stringify(summary, null, 2))}</pre>`;
  }
  const items = content?.items || content?.shots || content?.scenes || content?.checks;
  if (Array.isArray(items)) {
    return `<div class="artifact-items">${items.slice(0, 18).map((item, idx) => `
      <div class="artifact-item">
        <b>${escapeHtml(item.name || item.slug || item.scene || item.id || item.index || idx + 1)}</b>
        ${escapeHtml(item.description || item.summary || item.visual_prompt || item.message || item.dialogue || item.status || "")}
        ${renderMedia(item.url)}
      </div>
    `).join("")}</div>`;
  }
  if (["preview", "compose"].includes(kind) && content?.url) {
    return `${renderMedia(content.url)}<pre>${escapeHtml(JSON.stringify(content, null, 2))}</pre>`;
  }
  return escapeHtml(JSON.stringify(content, null, 2));
}

function renderAssetManifest(content) {
  const items = Array.isArray(content?.items) ? content.items : [];
  const counts = `REUSE ${Number(content?.reuse_count || 0)} · VARIANT ${Number(content?.variant_count || 0)} · CREATE ${Number(content?.create_count || 0)}`;
  const validation = content?.manifest_validation || {};
  const validationText = validation?.status ? ` · validation ${validation.status}` : "";
  return `<div class="manifest-summary"><strong>Asset Manifest</strong><span class="artifact-meta">${escapeHtml(counts + validationText)} · 没有匹配资产时 Harness 会自动 CREATE，不要求先上传</span></div><div class="artifact-items">${items.map(item => {
    const sourceKeys = Array.isArray(item.source_requirement_keys) ? item.source_requirement_keys : [];
    return `<div class="artifact-item"><div class="asset-card-actions"><b>${escapeHtml(item.manifest_id || item.canonical_key || "asset")}</b><span class="asset-manifest-decision">${escapeHtml(item.decision || "CREATE")}</span></div><div>${escapeHtml(item.script_identity || item.canonical_key || "")}</div><div class="artifact-meta">${escapeHtml(item.asset_type || "")} · ${escapeHtml(item.canonical_key || "")}${item.library_asset_id ? ` · library: ${escapeHtml(item.library_asset_id)}` : " · 将自动生成"}</div>${sourceKeys.length ? `<div class="artifact-meta">script binding: ${escapeHtml(sourceKeys.join(" · "))}</div>` : ""}<div class="artifact-meta">${escapeHtml(item.reason || "")}</div>${Array.isArray(item.variant_requirements) && item.variant_requirements.length ? `<div class="artifact-meta">Variant: ${escapeHtml(item.variant_requirements.join("；"))}</div>` : ""}</div>`;
  }).join("")}</div>`;
}

function assetStageLabel(kind) {
  return {characters: "角色", scenes: "场景", props: "道具"}[kind] || kind;
}

function assetValue(value) {
  if (value === null || value === undefined || value === "") return "";
  if (Array.isArray(value)) return value.map(x => typeof x === "object" ? JSON.stringify(x) : String(x)).join("；");
  if (typeof value === "object") return Object.entries(value).map(([k,v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join("；");
  return String(value);
}

function assetSpecRows(item, kind) {
  const rows = [];
  const add = (label, value) => { const text = assetValue(value); if (text) rows.push([label, text]); };
  if (kind === "characters") {
    add("外观", item.appearance || item.description);
    add("服装", item.wardrobe || item.costume);
    add("表演", item.performance);
    add("声音", item.voice);
    add("成片连续性锁", item.production_continuity_lock || item.continuity_lock);
    add("扩展连续性备注", item.continuity);
  } else if (kind === "scenes") {
    add("地点", item.location || item.description);
    add("布局", item.layout || item.key_set_elements);
    add("灯光", item.lighting);
    add("时间/天气", item.time_weather);
    add("成片连续性锁", item.production_continuity_lock || item.continuity_lock);
    add("扩展连续性备注", item.continuity);
  } else {
    add("描述", item.description || item.physical_description);
    add("材质/尺度", item.material_scale || item.material);
    add("状态", item.state);
    add("使用场次", item.used_in || item.used_in_scenes);
    add("成片连续性锁", item.production_continuity_lock || item.continuity_lock);
    add("扩展连续性备注", item.continuity);
  }
  add("源素材连续性（仅归档）", item.source_continuity_lock);
  add("源视觉锚点", item.source_visual_traits);
  add("法国化/目标市场生产设计", item.localized_visual_design);
  add("视觉本地化说明", item.visual_localization_notes);
  add("图片/视频 Prompt (EN)", item.generation_prompt_en);
  return rows;
}

function currentArtifactForStage(kind) {
  return (state.detail?.artifacts || []).find(a => a.kind === kind) || null;
}

function currentCandidatesForAsset(kind, canonicalKey) {
  const artifact = currentArtifactForStage(kind);
  const revision = Number(artifact?.revision || 0);
  return (state.detail?.asset_candidates || [])
    .filter(c => c.stage === kind && c.canonical_key === canonicalKey && Number(c.artifact_revision || 0) === revision)
    .sort((a,b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
}

function batchSelectionKey(kind, canonicalKey) {
  return `${kind}:${canonicalKey}`;
}

function isBatchSelected(kind, canonicalKey) {
  return Boolean(state.batchSelections[batchSelectionKey(kind, canonicalKey)]);
}

function setBatchSelected(kind, canonicalKey, selected) {
  const key = batchSelectionKey(kind, canonicalKey);
  if (selected) state.batchSelections[key] = true;
  else delete state.batchSelections[key];
}

function selectedBatchKeys(kind) {
  const prefix = `${kind}:`;
  return Object.keys(state.batchSelections)
    .filter(key => key.startsWith(prefix) && state.batchSelections[key])
    .map(key => key.slice(prefix.length));
}

function renderAssetCandidate(candidate, baseCandidateId) {
  const isSelected = Boolean(candidate.selected);
  const isBase = candidate.id === baseCandidateId;
  const feedback = String(candidate.feedback || "").trim();
  return `<div class="visual-candidate ${isSelected ? "selected" : ""} ${isBase ? "edit-base" : ""}" data-candidate-card="${escapeHtml(candidate.id)}">
    <a class="visual-candidate-image" href="${escapeHtml(candidate.url || "#")}" target="_blank" rel="noopener">${renderMedia(candidate.url)}</a>
    <div class="visual-candidate-meta"><span>${escapeHtml(candidate.model || "Seedream")}</span>${isSelected ? `<span class="pill ready">采用中</span>` : ""}${isBase ? `<span class="pill">调整基准</span>` : ""}</div>
    ${feedback ? `<div class="artifact-meta candidate-feedback-note">本次要求：${escapeHtml(feedback)}</div>` : ""}
    <div class="visual-candidate-actions">
      ${isSelected ? "" : `<button class="mini-button ok" data-select-candidate="${escapeHtml(candidate.id)}">设为采用并锁定</button>`}
      <button class="mini-button" data-base-candidate="${escapeHtml(candidate.id)}">基于这张调整</button>
      ${isSelected ? "" : `<button class="mini-button reject" data-delete-candidate="${escapeHtml(candidate.id)}">删除</button>`}
    </div>
  </div>`;
}

function renderAssetDesignWorkbench(content, kind) {
  const items = Array.isArray(content?.items) ? content.items : [];
  const label = assetStageLabel(kind);
  const artifact = currentArtifactForStage(kind);
  const revision = Number(artifact?.revision || 0);
  const withCandidate = items.filter(item => currentCandidatesForAsset(kind, String(item.canonical_key || item.id || "")).length).length;
  const selectedCount = items.filter(item => currentCandidatesForAsset(kind, String(item.canonical_key || item.id || "")).some(c => c.selected)).length;
  const cards = items.map((item, idx) => {
    const canonicalKey = String(item.canonical_key || item.id || `asset-${idx+1}`);
    const candidates = currentCandidatesForAsset(kind, canonicalKey);
    const selected = candidates.find(c => c.selected);
    const rememberedBase = state.candidateBases[`${kind}:${canonicalKey}`] || "";
    const latest = candidates[candidates.length - 1];
    const baseCandidateId = rememberedBase || selected?.id || latest?.id || "";
    const rows = assetSpecRows(item, kind);
    const candidateHtml = candidates.length
      ? candidates.map(c => renderAssetCandidate(c, baseCandidateId)).join("")
      : `<div class="candidate-empty">还没有视觉候选。先“抽 1 张”，满意后点“设为采用”；不满意就写局部要求再抽。</div>`;
    const batchSelected = isBatchSelected(kind, canonicalKey);
    return `<section class="asset-design-card ${batchSelected ? "batch-selected" : ""}" data-asset-stage="${escapeHtml(kind)}" data-canonical-key="${escapeHtml(canonicalKey)}">
      <div class="asset-design-header">
        <div class="asset-design-title-wrap"><label class="batch-select-box"><input type="checkbox" data-batch-select ${batchSelected ? "checked" : ""}><span>加入批量</span></label><div><b>${escapeHtml(item.name || item.id || canonicalKey)}</b><div class="artifact-meta">${escapeHtml(canonicalKey)} · ${escapeHtml(item.reuse_decision || item.decision || "CREATE")}</div></div></div>
        ${selected ? `<span class="pill ready">已锁定视觉</span>` : `<span class="pill">待选视觉</span>`}
      </div>
      <div class="visual-localization-banner"><b>视觉本地化</b><span>目标市场：${escapeHtml(item?.visual_localization?.target_market || state.detail?.workspace?.settings?.target_market || "未设置")}</span><span>Provider Prompt：English</span><span>${item.localized_visual_design && item.generation_prompt_en ? "显式本地化设计已就绪" : "当前旧产物缺少显式本地化字段；抽图时仍会由 Harness Prompt Compiler 强制做目标市场适配，建议本轮重新生成该设计节点后再正式锁图"}</span></div>
      <div class="asset-spec-grid">${rows.map(([k,v]) => `<div class="asset-spec-row"><strong>${escapeHtml(k)}</strong><span>${escapeHtml(v)}</span></div>`).join("")}</div>
      <div class="candidate-gallery">${candidateHtml}</div>
      <div class="candidate-editor">
        <textarea rows="2" data-candidate-feedback placeholder="只改这个${escapeHtml(label)}。例如：${kind === "characters" ? "脸更接近原片、年龄感更明显；保留人物身份和酒红色识别色，但服装改成可信的法国乡村绗缝外套，不要中国式大红花棉袄" : kind === "scenes" ? "门厅更窄、吊灯更大、保留现有布局和法式豪宅质感" : "旅行袋更旧、更大；保持绿灰帆布和束带结构不变"}"></textarea>
        <div class="candidate-editor-actions"><span class="artifact-meta">${baseCandidateId ? "默认基于当前采用/最近候选做图生图调整" : "当前为文生图首抽"}</span><button class="mini-button" data-generate-candidate="1">抽 1 张</button><button class="mini-button" data-generate-candidate="4">抽 4 张</button></div>
      </div>
    </section>`;
  }).join("");
  const batchSelectedKeys = selectedBatchKeys(kind);
  return `<div class="asset-visual-workbench">
    <div class="visual-workbench-toolbar"><div><strong>${escapeHtml(label)}视觉候选工作台</strong><div class="artifact-meta">rev ${revision} · ${items.length} 项 · ${withCandidate} 项已有候选 · ${selectedCount} 项已采用。视觉抽卡只改图片，不重跑 Kimi ${escapeHtml(label)}圣经。</div></div><button class="mini-button" data-generate-missing-assets>为全部未出图资产各抽 1 张</button></div>
    <div class="visual-workbench-note">源片视觉事实 ≠ 成片视觉锁。Harness 会先按目标市场做视觉本地化，再调用图片 Provider；Provider 视觉 Prompt 默认使用英文，法语对白/可见文字仍保持目标语言。候选图不会自动进入下游；满意后点“设为采用并锁定”，后续「参考图」优先使用该图。</div>
    <div class="batch-workbench-panel" data-batch-workbench="${escapeHtml(kind)}">
      <div class="batch-workbench-header"><div><strong>批量要求与抽图</strong><div class="artifact-meta">对勾选的${escapeHtml(label)}一次性应用同一条要求，例如“全部进一步法国化、保持各自身份与既有分析”。模型仍会结合每个资产自己的 Bible / 本地化分析分别出图。</div></div><div class="batch-workbench-toolbar"><button class="mini-button" data-batch-select-all>全选</button><button class="mini-button" data-batch-clear>清空勾选</button><span class="pill amber" data-batch-count>${batchSelectedKeys.length} 项已勾选</span></div></div>
      <textarea data-batch-feedback placeholder="给一批${escapeHtml(label)}同样的要求。例如：全部进一步法国化，保留各自年龄、阶层、叙事功能与主要识别色；服装更贴合可信法国环境，不要保留中国特定造型元素。"></textarea>
      <div class="batch-workbench-actions"><span class="artifact-meta">橘色按钮 = 对当前勾选项批量抽图。若某项已有采用图，会默认基于该图继续调整；没有则按当前设计首抽。</span><button class="mini-button batch" data-generate-batch="1">对勾选项各抽 1 张</button><button class="mini-button batch" data-generate-batch="4">对勾选项各抽 4 张</button></div>
    </div>
    <div class="asset-design-list">${cards}</div>
    <details class="raw-json"><summary>查看 ${escapeHtml(label)} JSON</summary><pre>${escapeHtml(JSON.stringify(content, null, 2))}</pre></details>
  </div>`;
}

async function generateCandidateForCard(card, count = 1) {
  const stage = card.dataset.assetStage;
  const canonicalKey = card.dataset.canonicalKey;
  const feedback = card.querySelector("[data-candidate-feedback]")?.value?.trim() || "";
  const key = `${stage}:${canonicalKey}`;
  const candidates = currentCandidatesForAsset(stage, canonicalKey);
  const selected = candidates.find(c => c.selected);
  const latest = candidates[candidates.length - 1];
  const baseCandidateId = state.candidateBases[key] || selected?.id || latest?.id || "";
  const buttons = card.querySelectorAll("[data-generate-candidate]");
  buttons.forEach(btn => btn.disabled = true);
  try {
    toast(`${assetStageLabel(stage)} ${canonicalKey} 正在抽图…`);
    const result = await api(`/api/workspaces/${state.selectedId}/asset-candidates`, {
      method: "POST",
      body: JSON.stringify({stage, canonical_key: canonicalKey, feedback, base_candidate_id: baseCandidateId, count}),
    });
    const created = result.items || [];
    if (created.length) state.candidateBases[key] = created[created.length - 1].id;
    await refreshCurrent();
    state.tab = stage;
    renderDetail();
    toast(`已生成 ${created.length} 张候选`);
  } finally {
    buttons.forEach(btn => btn.disabled = false);
  }
}

async function generateBatchCandidates(kind, canonicalKeys, feedback, count = 1, button = null) {
  if (!canonicalKeys.length) { toast("请先勾选至少一个资产"); return; }
  if (button) button.disabled = true;
  try {
    toast(`正在为 ${canonicalKeys.length} 个${assetStageLabel(kind)}批量抽图…`);
    const result = await api(`/api/workspaces/${state.selectedId}/asset-candidates/batch`, {
      method: "POST",
      body: JSON.stringify({stage: kind, canonical_keys: canonicalKeys, feedback, count}),
    });
    const itemsByKey = result.items_by_key || {};
    Object.entries(itemsByKey).forEach(([canonicalKey, created]) => {
      if (Array.isArray(created) && created.length) state.candidateBases[`${kind}:${canonicalKey}`] = created[created.length - 1].id;
    });
    await refreshCurrent();
    state.tab = kind;
    renderDetail();
    toast(`已为 ${result.asset_count || canonicalKeys.length} 项各生成 ${result.count_per_asset || count} 张候选`);
  } finally {
    if (button) button.disabled = false;
  }
}

function bindAssetCandidateUi() {
  document.querySelectorAll("[data-generate-candidate]").forEach(button => button.addEventListener("click", async () => {
    const card = button.closest("[data-asset-stage]");
    if (!card) return;
    try { await generateCandidateForCard(card, Number(button.dataset.generateCandidate || 1)); }
    catch (error) { toast(error.message); }
  }));
  document.querySelectorAll("[data-base-candidate]").forEach(button => button.addEventListener("click", () => {
    const card = button.closest("[data-asset-stage]"); if (!card) return;
    const key = `${card.dataset.assetStage}:${card.dataset.canonicalKey}`;
    state.candidateBases[key] = button.dataset.baseCandidate || "";
    renderDetail();
    toast("下一次只调整这个候选图");
  }));
  document.querySelectorAll("[data-select-candidate]").forEach(button => button.addEventListener("click", async () => {
    try {
      await api(`/api/workspaces/${state.selectedId}/asset-candidates/${button.dataset.selectCandidate}/select`, {method:"POST"});
      await refreshCurrent(); renderDetail(); toast("已设为下游采用图");
    } catch (error) { toast(error.message); }
  }));
  document.querySelectorAll("[data-delete-candidate]").forEach(button => button.addEventListener("click", async () => {
    if (!window.confirm("删除这张候选图吗？")) return;
    try {
      await api(`/api/workspaces/${state.selectedId}/asset-candidates/${button.dataset.deleteCandidate}`, {method:"DELETE"});
      await refreshCurrent(); renderDetail(); toast("候选已删除");
    } catch (error) { toast(error.message); }
  }));
  document.querySelectorAll("[data-batch-select]").forEach(box => box.addEventListener("change", event => {
    const card = box.closest("[data-asset-stage]");
    if (!card) return;
    setBatchSelected(card.dataset.assetStage, card.dataset.canonicalKey, event.target.checked);
    renderDetail();
  }));
  document.querySelectorAll("[data-batch-select-all]").forEach(button => button.addEventListener("click", () => {
    const workbench = button.closest("[data-batch-workbench]");
    const kind = workbench?.dataset.batchWorkbench || "";
    document.querySelectorAll(`[data-asset-stage="${kind}"]`).forEach(card => setBatchSelected(kind, card.dataset.canonicalKey, true));
    renderDetail();
  }));
  document.querySelectorAll("[data-batch-clear]").forEach(button => button.addEventListener("click", () => {
    const workbench = button.closest("[data-batch-workbench]");
    const kind = workbench?.dataset.batchWorkbench || "";
    selectedBatchKeys(kind).forEach(key => setBatchSelected(kind, key, false));
    renderDetail();
  }));
  document.querySelectorAll("[data-generate-batch]").forEach(button => button.addEventListener("click", async () => {
    const workbench = button.closest("[data-batch-workbench]");
    const kind = workbench?.dataset.batchWorkbench || "";
    const canonicalKeys = selectedBatchKeys(kind);
    const feedback = workbench?.querySelector("[data-batch-feedback]")?.value?.trim() || "";
    const count = Number(button.dataset.generateBatch || 1);
    if (!canonicalKeys.length) { toast("请先勾选至少一个资产"); return; }
    if (!window.confirm(`将为 ${canonicalKeys.length} 个${assetStageLabel(kind)}各生成 ${count} 张候选图。继续吗？`)) return;
    try { await generateBatchCandidates(kind, canonicalKeys, feedback, count, button); }
    catch (error) { toast(error.message); }
  }));
  document.querySelectorAll("[data-generate-missing-assets]").forEach(button => button.addEventListener("click", async () => {
    const workbench = button.closest(".asset-visual-workbench");
    const cards = Array.from(workbench?.querySelectorAll("[data-asset-stage]") || []).filter(card => currentCandidatesForAsset(card.dataset.assetStage, card.dataset.canonicalKey).length === 0);
    if (!cards.length) { toast("当前所有资产都已有候选图"); return; }
    if (!window.confirm(`将调用 Seedream ${cards.length} 次，为 ${cards.length} 个未出图资产各生成 1 张候选。继续吗？`)) return;
    button.disabled = true;
    try {
      for (let i=0; i<cards.length; i++) {
        toast(`批量抽图 ${i+1}/${cards.length}`);
        await generateCandidateForCard(cards[i], 1);
      }
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  }));
}

function sourceMediaUrl(file) {
  if (file?.url) return String(file.url);
  if (!state.selectedId || !file?.name) return "";
  return `/source-media/${encodeURIComponent(state.selectedId)}/${encodeURIComponent(file.name)}`;
}

function renderSource(content) {
  const files = Array.isArray(content?.files) ? content.files : [];
  const fileHtml = files.map(file => {
    const sizeMb = file?.size ? `${(Number(file.size) / 1024 / 1024).toFixed(1)} MB` : "";
    const url = sourceMediaUrl(file);
    return `<div class="source-file-card">
      <div class="source-file-heading"><b>${escapeHtml(file?.name || "源视频")}</b><span class="artifact-meta">${escapeHtml(sizeMb)}</span></div>
      ${url ? renderMedia(url) : `<div class="artifact-meta">源文件已记录，但当前没有可预览 URL。</div>`}
    </div>`;
  }).join("");
  const brief = content?.brief ? `<div class="execution-note"><b>素材要求</b><div>${escapeHtml(content.brief)}</div></div>` : "";
  return `${brief}${fileHtml || `<div class="event">尚未上传源视频。</div>`}<details class="raw-json"><summary>查看素材 JSON</summary><pre>${escapeHtml(JSON.stringify(content, null, 2))}</pre></details>`;
}

function renderScript(content) {
  const beats = Array.isArray(content?.beats) ? content.beats : [];
  const scenes = Array.isArray(content?.scenes) ? content.scenes : [];
  const localizationMap = Array.isArray(content?.localization_map) ? content.localization_map : [];
  const episodeSections = Array.isArray(content?.episode_sections) ? content.episode_sections : [];
  const assetRequirements = Array.isArray(content?.asset_requirements) ? content.asset_requirements : [];
  const localizationHtml = localizationMap.length ? `<div class="localization-map">${localizationMap.slice(0, 16).map(item => `
    <div class="localization-row"><b>${escapeHtml(item.source_element || "源元素")} → ${escapeHtml(item.localized_element || "本地化元素")}</b><div>${escapeHtml(item.rationale || "")}</div></div>
  `).join("")}</div>` : "";
  const episodeHtml = episodeSections.length ? `<div class="episode-sections">${episodeSections.map((section, idx) => `<div class="artifact-item"><b>Section ${idx + 1} · ${escapeHtml(section.label || section.name || section.scope || "")}</b><div>${escapeHtml(section.source_time_range || section.time_range || "")}</div><div>${escapeHtml(section.notes || section.summary || "")}</div></div>`).join("")}</div>` : "";
  const assetRequirementHtml = assetRequirements.length ? `<details class="raw-json"><summary>下游稳定资产需求 · ${assetRequirements.length} 项</summary><pre>${escapeHtml(JSON.stringify(assetRequirements, null, 2))}</pre></details>` : "";
  const beatHtml = beats.length ? `
    <div class="artifact-items">
      ${beats.map((beat, idx) => {
        const text = typeof beat === "string" ? beat : (beat.summary || beat.description || beat.beat || JSON.stringify(beat));
        return `<div class="artifact-item"><b>Beat ${idx + 1}</b><div>${escapeHtml(text)}</div></div>`;
      }).join("")}
    </div>` : "";
  const sceneHtml = scenes.length ? scenes.map((scene, idx) => {
    if (typeof scene === "string") {
      return `<div class="artifact-item"><b>Scene ${idx + 1}</b><div>${escapeHtml(scene)}</div></div>`;
    }
    const heading = scene.heading || scene.name || scene.scene || scene.slug || `Scene ${idx + 1}`;
    const range = scene.source_time_range || scene.time_range || scene.timing || "";
    const objective = scene.objective || "";
    const productionScope = scene.production_scope || "";
    const sourceBasis = scene.source_fact_basis || "";
    const decisions = scene.adaptation_decisions || [];
    const summary = scene.summary || scene.description || "";
    const action = scene.action || scene.blocking || "";
    const hook = scene.ending_hook || scene.hook || "";
    const cultural = scene.cultural_adaptations || scene.localization_notes || [];
    const culturalText = Array.isArray(cultural) ? cultural.join("；") : String(cultural || "");
    const dialogue = Array.isArray(scene.dialogue) ? scene.dialogue : [];
    const dialogueHtml = dialogue.length ? `<div class="script-dialogue">${dialogue.map(line => {
      if (typeof line === "string") return `<div class="dialogue-line">${escapeHtml(line)}</div>`;
      const speaker = line.speaker || line.character || line.speaker_id || "";
      const text = line.text || line.dialogue || "";
      const intent = line.intent || line.performance || "";
      return `<div class="dialogue-line"><b>${escapeHtml(speaker)}</b>${speaker ? "：" : ""}${escapeHtml(text)}${intent ? `<span class="artifact-meta"> · ${escapeHtml(intent)}</span>` : ""}</div>`;
    }).join("")}</div>` : `<div class="artifact-meta">该场暂无结构化对白；若源片有对白，建议重新生成剧本。</div>`;
    return `<div class="artifact-item script-scene">
      <b>${escapeHtml(scene.scene_id || scene.id || idx + 1)} · ${escapeHtml(heading)}</b>
      ${range ? `<div class="artifact-meta">源片：${escapeHtml(range)}${productionScope ? ` · ${escapeHtml(productionScope)}` : ""}</div>` : ""}
      ${objective ? `<div><strong>目标：</strong>${escapeHtml(objective)}</div>` : ""}
      ${sourceBasis ? `<div><strong>事实依据：</strong>${escapeHtml(typeof sourceBasis === "string" ? sourceBasis : JSON.stringify(sourceBasis))}</div>` : ""}
      ${Array.isArray(decisions) && decisions.length ? `<div><strong>改编决策：</strong>${escapeHtml(decisions.map(x => typeof x === "string" ? x : JSON.stringify(x)).join("；"))}</div>` : ""}
      ${summary ? `<div><strong>剧情：</strong>${escapeHtml(summary)}</div>` : ""}
      ${action ? `<div><strong>动作：</strong>${escapeHtml(action)}</div>` : ""}
      ${dialogueHtml}
      ${culturalText ? `<div><strong>本地化处理：</strong>${escapeHtml(culturalText)}</div>` : ""}
      ${hook ? `<div><strong>场尾钩子：</strong>${escapeHtml(hook)}</div>` : ""}
    </div>`;
  }).join("") : `<div class="event">剧本缺少 scenes；建议重新生成。</div>`;
  return `
    <div class="artifact-meta">${escapeHtml(content?.title || "未命名剧本")} · ${escapeHtml(content?.language || "未标注语言")} · ${escapeHtml(content?.target_market || "未标注市场")}</div>
    ${content?.localization_strategy ? `<div class="execution-note"><b>本地化策略</b><div>${escapeHtml(content.localization_strategy)}</div></div>` : ""}
    ${localizationHtml}
    ${episodeHtml}
    ${beatHtml}
    <div class="artifact-items">${sceneHtml}</div>
    ${assetRequirementHtml}
    <details class="raw-json"><summary>查看完整剧本 JSON</summary><pre>${escapeHtml(JSON.stringify(content, null, 2))}</pre></details>`;
}

function renderMedia(value) {
  const url = String(value || "");
  if (!(url.startsWith("/media/") || url.startsWith("/source-media/") || url.startsWith("/asset-media/") || url.startsWith("https://") || url.startsWith("http://"))) return "";
  if (/\.(png|jpe?g|webp)(\?|$)/i.test(url)) {
    return `<img class="media-preview media-image" loading="lazy" src="${escapeHtml(url)}" alt="生成参考图">`;
  }
  if (/\.(mp3|wav|ogg|m4a)(\?|$)/i.test(url)) {
    return `<audio class="media-preview" controls preload="metadata" src="${escapeHtml(url)}"></audio>`;
  }
  return `<video class="media-preview" controls preload="metadata" src="${escapeHtml(url)}"></video>`;
}

function activeJob() {
  return (state.detail?.jobs || []).find(job => ["queued", "running"].includes(job.status)) || null;
}

function renderNextAction(actions) {
  const action = actions[0];
  const running = activeJob();
  if (running) {
    $("#nextAction").innerHTML = `<strong>当前有任务进行中</strong><br>${escapeHtml(state.meta.labels[running.stage] || running.stage)} · ${escapeHtml(running.status)}<div class="artifact-meta">${escapeHtml(running.message || "")}</div>`;
    $("#advanceBtn").disabled = true;
    $("#advanceBtn").textContent = "任务进行中";
    return;
  }
  $("#nextAction").innerHTML = action
    ? `<strong>建议下一步</strong><br>${escapeHtml(action.label)}`
    : `<strong>流程已完成</strong><br>可以检查产物或修改上游内容。`;
  $("#advanceBtn").disabled = !action;
  $("#advanceBtn").textContent = action
    ? (action.type === "run" ? `与总管确认并${action.label}` : action.label)
    : "已完成";
}

function latestReviewableStage() {
  const artifacts = new Map((state.detail?.artifacts || []).map(item => [item.kind, item]));
  const jobs = state.detail?.jobs || [];
  for (const job of jobs) {
    const stage = String(job?.stage || "");
    if (!stage || stage === "source" || job?.status !== "succeeded") continue;
    const artifact = artifacts.get(stage);
    if (artifact?.status === "ready") return stage;
  }
  return null;
}

function currentGuidanceStage(actions = state.detail?.next_actions || []) {
  const running = activeJob();
  if (running) return running.stage;
  if (state.chatStageOverride && (state.meta?.stages || []).includes(state.chatStageOverride) && state.chatStageOverride !== "source") return state.chatStageOverride;

  // After a node finishes, keep the composer attached to that ready revision so
  // "反馈并重生成当前节点" still targets the artifact the user is looking at.
  // Advancing to the next node explicitly rebinds the composer in advance().
  const reviewable = latestReviewableStage();
  if (reviewable) return reviewable;

  const action = (actions || [])[0];
  if (!action) return null;
  if (action.type === "run" || action.type === "chat") return action.stage || null;
  if (action.type === "approve") {
    const pair = Object.entries(state.meta?.gates || {}).find(([, gate]) => gate === action.gate);
    return pair?.[0] || null;
  }
  return null;
}

function renderGuidancePanel(actions) {
  const stage = currentGuidanceStage(actions);
  const input = $("#stageGuidanceInput");
  const memoryInput = $("#projectMemoryInput");
  const chatButton = $("#saveGuidanceBtn");
  const regenButton = $("#regenerateStageBtn");
  const flowAdvance = $("#flowAdvanceBtn");
  const running = activeJob();

  if (document.activeElement !== memoryInput) memoryInput.value = state.detail?.project_memory || "";
  if (!stage) {
    $("#guidanceStageLabel").textContent = "当前没有需要总管协同的节点";
    $("#effectiveGuidanceView").innerHTML = "";
    input.disabled = true;
    input.value = "";
    chatButton.disabled = true;
    if (regenButton) {
      regenButton.disabled = true;
      regenButton.dataset.stage = "";
    }
    if (flowAdvance) flowAdvance.disabled = true;
    $("#directorPlanView").innerHTML = "";
  } else {
    const agentId = state.meta.stage_agents?.[stage];
    const agent = (state.meta.agents || []).find(item => item.id === agentId);
    const action = (actions || [])[0];
    const artifact = (state.detail?.artifacts || []).find(item => item.kind === stage);
    const mode = artifact?.status === "ready"
      ? "当前评审节点"
      : (action?.type === "approve" ? "正在评审当前产物" : (action?.type === "chat" ? "总管要求先沟通" : "下一执行节点"));
    $("#guidanceStageLabel").textContent = `${mode}：${state.meta.labels[stage] || stage} · ${agent?.name || "Agent"}`;
    input.disabled = Boolean(running);
    chatButton.disabled = Boolean(running);
    if (regenButton) {
      regenButton.disabled = Boolean(running) || !artifact || artifact.status !== "ready";
      regenButton.dataset.stage = artifact?.status === "ready" ? stage : "";
      regenButton.textContent = artifact ? `反馈并重生成${state.meta.labels[stage] || stage}` : "反馈并重生成当前节点";
      regenButton.title = artifact?.status === "ready" ? `当前绑定：${state.meta.labels[stage] || stage}` : "当前没有 ready 产物可重生成";
    }
    if (flowAdvance) {
      flowAdvance.disabled = Boolean(running) || !(state.detail?.next_actions || []).length;
      flowAdvance.textContent = (state.detail?.next_actions || [])[0]?.label || "推进下一步";
    }
    const modeBadge = $("#flowComposerMode");
    if (modeBadge) modeBadge.textContent = artifact?.status === "ready" ? "评审 / 可重生成" : "下一步要求";
    const record = state.detail?.guidance?.[stage] || {};
    const effective = String(record.user_instruction || "").trim();
    $("#effectiveGuidanceView").innerHTML = effective
      ? `<b>当前已绑定要求</b><div>${escapeHtml(effective)}</div>`
      : `<span class="artifact-meta">当前节点还没有绑定要求；你可以直接发消息，或推进时让总管按项目记忆自动确认。</span>`;
    input.placeholder = state.meta.guidance_hints?.[stage] || "告诉总管你希望当前节点怎么调整。";
    renderDirectorPlan(record.director_plan || {});
  }
  renderGuidanceHistory();
}

function renderDirectorPlan(plan) {
  const root = $("#directorPlanView");
  if (!plan || !Object.keys(plan).length) {
    root.innerHTML = `<div class="artifact-meta">尚未形成执行方案。你可以直接给总管发消息；推进生成节点时 Harness 也会强制先与总管确认。</div>`;
    return;
  }
  const criteria = Array.isArray(plan.acceptance_criteria) ? plan.acceptance_criteria : [];
  const questions = Array.isArray(plan.questions) ? plan.questions : [];
  const warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
  const memories = Array.isArray(plan.memory_candidates) ? plan.memory_candidates : [];
  root.innerHTML = `<div class="director-plan-card">
    <b>总管理解</b><div>${escapeHtml(plan.interpretation || "按默认规则执行")}</div>
    ${plan.prompt_addendum ? `<div><b>追加提示词</b><div>${escapeHtml(plan.prompt_addendum)}</div></div>` : ""}
    ${Object.keys(plan.parameter_overrides || {}).length ? `<div><b>参数调整</b><code>${escapeHtml(JSON.stringify(plan.parameter_overrides, null, 2))}</code></div>` : ""}
    ${criteria.length ? `<div><b>验收标准</b><div>${criteria.map(x => `• ${escapeHtml(x)}`).join("<br>")}</div></div>` : ""}
    ${memories.length ? `<div><b>建议写入项目记忆</b><div>${memories.map(x => `• ${escapeHtml(typeof x === "string" ? x : (x.text || JSON.stringify(x)))}`).join("<br>")}</div></div>` : ""}
    ${warnings.length ? `<div><b>提醒</b><div>${warnings.map(x => `• ${escapeHtml(x)}`).join("<br>")}</div></div>` : ""}
    ${questions.length ? `<div><b>仍需确认${plan.requires_user_input ? "（执行前必须回答）" : ""}</b><div>${questions.map(x => `• ${escapeHtml(x)}`).join("<br>")}</div></div>` : ""}
  </div>`;
}

function renderGuidanceHistory() {
  const root = $("#guidanceHistory");
  if (!root) return;
  const messages = state.detail?.guidance_messages || [];
  root.innerHTML = messages.slice(-18).map(item => {
    const kind = item.metadata?.kind === "post_generation_review" ? " · 生成复盘" : (item.metadata?.kind === "stage_reset" ? " · 区域清理" : "");
    return `<div class="guidance-message ${escapeHtml(item.role)}">
      <div class="role">${escapeHtml(item.role)} · ${escapeHtml(state.meta.labels[item.stage] || item.stage)}${escapeHtml(kind)}</div>
      <div>${escapeHtml(item.content)}</div>
    </div>`;
  }).join("") || `<div class="artifact-meta">还没有总管沟通记录。</div>`;
}

async function saveProjectMemory(silent = false) {
  if (!state.selectedId) return;
  const memory = $("#projectMemoryInput").value.trim();
  await api(`/api/workspaces/${state.selectedId}/memory`, {
    method: "PUT",
    body: JSON.stringify({memory}),
  });
  if (state.detail) state.detail.project_memory = memory;
  if (!silent) toast("项目记忆已保存");
}

async function chatStageGuidance(messageOverride = null, stageOverride = null) {
  const stage = stageOverride || currentGuidanceStage();
  if (!stage || !state.selectedId) throw new Error("当前没有可沟通的模型节点");
  const input = $("#stageGuidanceInput");
  const message = messageOverride !== null ? String(messageOverride) : input.value.trim();
  try {
    await saveProjectMemory(true);
    state.pendingChat = {stage, message: message || "请总管结合当前上下文确认下一步"};
    state.tab = "flow";
    renderDetail();
    toast("总管正在结合当前产物和上下文回应…");
    const result = await api(`/api/workspaces/${state.selectedId}/guidance/${stage}/chat`, {
      method: "POST",
      body: JSON.stringify({message}),
    });
    input.value = "";
    state.pendingChat = null;
    state.tab = "flow";
    await refreshCurrent();
    if (result.reply) toast(result.reply.slice(0, 120));
    return result;
  } catch (error) {
    toast(error.message);
    throw error;
  }
}

async function ensureBoundDirectorPlan(stage) {
  const typed = $("#stageGuidanceInput").value.trim();
  const message = typed || "请结合最新生成结果、项目记忆和已有沟通，确认本节点下一次执行方案；如无阻塞问题，请直接给出可执行指令。";
  // Bind the director chat to the stage being advanced, not whichever completed
  // artifact currently owns the review composer.
  const result = await chatStageGuidance(message, stage);
  const plan = result.plan || {};
  if (plan.requires_user_input) {
    const questions = Array.isArray(plan.questions) ? plan.questions.join("；") : "总管需要更多信息";
    throw new Error(`总管需要你先补充：${questions}`);
  }
  return result;
}

function renderApprovals(approvals) {
  const known = Object.values(state.meta.gates);
  const byGate = Object.fromEntries(approvals.map(x => [x.gate, x]));
  const stageByGate = Object.fromEntries(Object.entries(state.meta.gates || {}).map(([stage, gate]) => [gate, stage]));
  const artifactsByKind = Object.fromEntries((state.detail?.artifacts || []).map(item => [item.kind, item]));
  $("#approvalList").innerHTML = known.map(gate => {
    const current = byGate[gate]?.status || "pending";
    const stage = stageByGate[gate];
    const artifact = artifactsByKind[stage];
    const reviewRow = state.detail?.stage_reviews?.[stage];
    const review = reviewRow && artifact && Number(reviewRow.artifact_revision || 0) === Number(artifact.revision || 0) ? (reviewRow.review || {}) : {};
    const blocked = ["regenerate_current", "wait_for_user"].includes(review.recommended_action);
    return `<div class="approval ${blocked ? "blocked" : ""}">
      <div class="approval-row"><strong>${escapeHtml(gate)}</strong><span class="pill ${current === "approved" ? "ready" : ""}">${current}</span></div>
      ${blocked ? `<div class="artifact-meta">总管复盘建议：${escapeHtml(review.recommended_action)}。先调整/重生成，再确认更稳。</div>` : ""}
      <div class="approval-actions">
        ${blocked ? `<button class="mini-button warn" data-force-approve="${gate}">仍然确认</button>` : `<button class="mini-button ok" data-approve="${gate}">确认</button>`}
        <button class="mini-button reject" data-reject="${gate}">退回</button>
      </div>
    </div>`;
  }).join("");
  document.querySelectorAll("[data-approve]").forEach(el => el.addEventListener("click", () => setApproval(el.dataset.approve, "approved")));
  document.querySelectorAll("[data-force-approve]").forEach(el => el.addEventListener("click", () => {
    if (window.confirm("总管建议先调整或重生成。你仍要以人工决定覆盖该建议并确认当前产物吗？")) setApproval(el.dataset.forceApprove, "approved", true);
  }));
  document.querySelectorAll("[data-reject]").forEach(el => el.addEventListener("click", () => setApproval(el.dataset.reject, "rejected")));
}

function renderJobs(jobs) {
  $("#jobList").innerHTML = jobs.slice(0, 8).map(job => `
    <div class="job">
      <div class="job-row"><strong>${escapeHtml(state.meta.labels[job.stage] || job.stage)}</strong><span>${escapeHtml(job.status)}${["queued", "running"].includes(job.status) ? ` · ${Number(job.progress || 0)}%` : ""}</span></div>
      <div class="progress"><span style="width:${job.progress}%"></span></div>
      <div class="artifact-meta">${escapeHtml(job.message)}</div>
    </div>
  `).join("") || `<div class="event">当前无任务</div>`;
}

function renderEvents(events) {
  $("#eventList").innerHTML = events.slice(0, 12).map(event => `
    <div class="event"><strong>${escapeHtml(event.type)}</strong><br>${escapeHtml(JSON.stringify(event.payload))}</div>
  `).join("");
}

function upsertJobState(job) {
  if (!state.detail || !job) return;
  const jobs = [...(state.detail.jobs || [])];
  const index = jobs.findIndex(item => item.id === job.id);
  if (index >= 0) jobs[index] = {...jobs[index], ...job};
  else jobs.unshift(job);
  state.detail.jobs = jobs;
  renderJobs(jobs);
  renderNextAction(state.detail.next_actions || []);
}

function stopJobWatcher() {
  if (jobWatchTimer) clearInterval(jobWatchTimer);
  jobWatchTimer = null;
}

function syncJobWatcher() {
  const running = activeJob();
  if (!running) {
    stopJobWatcher();
    return;
  }
  if (jobWatchTimer) return;
  jobWatchTimer = setInterval(refreshActiveJob, 1500);
  refreshActiveJob();
}

async function refreshActiveJob() {
  if (jobWatchBusy || !state.selectedId) return;
  const current = activeJob();
  if (!current) {
    stopJobWatcher();
    return;
  }
  jobWatchBusy = true;
  try {
    const job = await api(`/api/jobs/${current.id}`);
    upsertJobState(job);
    if (["succeeded", "failed"].includes(job.status)) {
      stopJobWatcher();
      if (job.status === "failed") toast(job.message);
      if (job.status === "succeeded" && job.stage && job.stage !== "source") {
        // Stay on the revision that just finished so the yellow regenerate button
        // remains bound to it.  The purple advance action explicitly rebinds to
        // the next stage when the user chooses to move on.
        state.chatStageOverride = job.stage;
      }
      await refreshCurrent();
    }
  } catch (error) {
    console.warn("job poll failed", error);
  } finally {
    jobWatchBusy = false;
  }
}

async function regenerateCurrentStageFromFeedback() {
  const boundStage = $("#regenerateStageBtn")?.dataset?.stage || "";
  const stage = boundStage || currentGuidanceStage();
  if (!stage || !state.selectedId) throw new Error("当前没有可重生成的节点");
  const input = $("#stageGuidanceInput");
  const feedback = input?.value.trim() || "";
  if (!feedback) {
    input?.focus();
    throw new Error("请先在对话框里说明哪里不满意、要保留什么、希望怎么改");
  }
  const artifact = (state.detail?.artifacts || []).find(item => item.kind === stage);
  if (!artifact || artifact.status !== "ready") throw new Error("当前节点还没有可反馈重生成的 ready 产物");
  const downstream = stageResetImpact(stage).filter(item => item !== stage);
  if (downstream.length) {
    const names = downstream.map(item => state.meta.labels[item] || item).join("、");
    if (!window.confirm(`重生成${state.meta.labels[stage] || stage}会使下游失效：${names}。\n项目记忆、对话历史和当前 revision 快照会保留。继续吗？`)) return;
  }
  await saveProjectMemory(true);
  input.disabled = true;
  state.pendingChat = {stage, message: feedback};
  state.tab = "flow";
  renderDetail();
  try {
    const job = await api(`/api/workspaces/${state.selectedId}/stages/${stage}/regenerate`, {
      method: "POST",
      body: JSON.stringify({feedback}),
    });
    input.value = "";
    state.pendingChat = null;
    state.chatStageOverride = stage;
    state.tab = "flow";
    upsertJobState(job);
    await refreshCurrent();
    toast(job.director_reply ? job.director_reply.slice(0, 140) : `已按反馈重新生成${state.meta.labels[stage] || stage}`);
    syncJobWatcher();
  } catch (error) {
    state.pendingChat = null;
    renderDetail();
    throw error;
  } finally {
    input.disabled = false;
  }
}

async function advance() {
  const action = state.detail.next_actions[0];
  if (!action) return;
  const running = activeJob();
  try {
    if (running) {
      toast(`已有进行中任务：${state.meta.labels[running.stage] || running.stage}`);
      syncJobWatcher();
      return;
    }
    if (action.type === "chat") {
      const input = $("#stageGuidanceInput");
      input.value = input.value || "请根据总管复盘指出的阻塞问题，告诉我本节点应该怎么调整，然后绑定到下一次重生成。";
      input.focus();
      toast("总管认为当前产物需要先沟通调整");
      return;
    }
    if (action.type === "approve") {
      const typed = $("#stageGuidanceInput").value.trim();
      if (typed) {
        await chatStageGuidance(typed);
        $("#stageGuidanceInput").value = "";
        toast("总管已回应你的调整意见；请根据回应选择确认、退回或清理节点重做");
        return;
      }
      await setApproval(action.gate, "approved");
      return;
    }
    // The composer normally remains attached to the most recently completed
    // artifact for review/regeneration.  Advancing is an explicit stage switch.
    state.chatStageOverride = action.stage || "";
    const director = await ensureBoundDirectorPlan(action.stage);
    const effective = director.effective_instruction || "";
    const job = await api(`/api/workspaces/${state.selectedId}/run/${action.stage}`, {
      method: "POST",
      body: JSON.stringify({
        user_instruction: effective,
        director_plan_hash: director.input_hash,
      }),
    });
    upsertJobState(job);
    state.chatStageOverride = action.stage || "";
    state.tab = "flow";
    renderDetail();
    if (job.reused) toast(job.message || `已复用任务：${job.id}`);
    else toast(`总管已确认，开始：${action.label}`);
    syncJobWatcher();
  } catch (error) { toast(error.message); }
}

async function setApproval(gate, status, force = false) {
  await api(`/api/workspaces/${state.selectedId}/approvals/${gate}`, {
    method: "PUT", body: JSON.stringify({ status, force, note: status === "approved" ? (force ? "用户明确覆盖总管复盘并确认" : "用户在本地 harness 确认") : "用户退回修改" })
  });
  toast(`${gate}: ${status}`);
  if (status === "approved") state.chatStageOverride = "";
  await refreshCurrent();
}

async function refreshCurrent() {
  state.detail = await api(`/api/workspaces/${state.selectedId}`);
  state.workspaces = await api("/api/workspaces");
  renderWorkspaceList();
  renderDetail();
}

async function clearJobHistory() {
  if (!state.selectedId) return;
  try {
    const result = await api(`/api/workspaces/${state.selectedId}/jobs`, { method: "DELETE" });
    toast(`已清理 ${result.removed || 0} 条已结束任务`);
    await refreshCurrent();
  } catch (error) { toast(error.message); }
}

function stageResetImpact(stage) {
  return state.meta?.reset_impacts?.[stage] || [stage];
}

async function clearStageRegion(stage) {
  if (!state.selectedId || !stage || stage === "source") return;
  const affected = stageResetImpact(stage);
  const labels = affected.map(item => state.meta.labels[item] || item);
  const label = state.meta.labels[stage] || stage;
  if (!window.confirm(`确定清理“${label}”区域？

会删除：${labels.join("、")}
会保留所有上游节点和源素材。
该操作用于从此节点重新生成。`)) return;
  try {
    const result = await api(`/api/workspaces/${state.selectedId}/stages/${stage}`, {method: "DELETE"});
    toast(`已清理：${(result.affected || []).map(item => state.meta.labels[item] || item).join("、")}`);
    await refreshCurrent();
  } catch (error) { toast(error.message); }
}

function bindArtifactFeedbackButtons() {
  document.querySelectorAll("[data-feedback-stage]").forEach(el => {
    if (el.dataset.feedbackBound === "1") return;
    el.dataset.feedbackBound = "1";
    el.addEventListener("click", event => {
      event.stopPropagation();
      state.chatStageOverride = el.dataset.feedbackStage || "";
      state.tab = "flow";
      renderDetail();
      requestAnimationFrame(() => {
        const input = $("#stageGuidanceInput");
        if (input) {
          input.placeholder = `告诉总管你对${state.meta.labels[state.chatStageOverride] || state.chatStageOverride}哪里不满意、哪些要保留、哪些要修改…`;
          input.focus();
        }
      });
    });
  });
}

function bindStageResetButtons() {
  document.querySelectorAll("[data-reset-stage]").forEach(el => {
    el.addEventListener("click", event => {
      event.stopPropagation();
      clearStageRegion(el.dataset.resetStage);
    });
  });
}

async function clearCurrentStage() {
  const directStages = new Set(state.meta?.stages || []);
  if (directStages.has(state.tab) && state.tab !== "source") {
    await clearStageRegion(state.tab);
    return;
  }
  toast("请切换到具体节点页，或直接点击产物卡片右上角的“清理此节点”");
}

async function createWorkspace(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const payload = Object.fromEntries(form.entries());
  payload.storyboard_count = Number(payload.storyboard_count);
  if (payload.series_id === "__new__") payload.series_id = "";
  else payload.series_name = "";
  try {
    const detail = await api("/api/workspaces", { method: "POST", body: JSON.stringify(payload) });
    createDialog.close();
    state.workspaces = await api("/api/workspaces");
    await refreshSeriesCatalog(true);
    state.selectedId = detail.workspace.id;
    state.detail = detail;
    renderWorkspaceList();
    $("#emptyState").classList.add("hidden");
    $("#workspaceView").classList.remove("hidden");
    renderDetail();
    toast("工作区已创建");
  } catch (error) { toast(error.message); }
}

function setUploadUi(upload) {
  state.upload = upload;
  const button = $("#uploadSourceBtn");
  const status = $("#uploadStatus");
  const active = Boolean(upload && upload.status === "uploading");
  if (button) {
    button.setAttribute("aria-disabled", active ? "true" : "false");
    button.textContent = active ? "上传中…" : "上传源视频";
  }
  if (!status) return;
  if (!upload) {
    status.textContent = "";
    return;
  }
  if (active) {
    const percent = Number(upload.percent || 0);
    const loadedMb = (Number(upload.loaded || 0) / 1024 / 1024).toFixed(1);
    const totalMb = upload.total ? (Number(upload.total) / 1024 / 1024).toFixed(1) : "?";
    status.textContent = `${percent}% · ${loadedMb}/${totalMb} MB`;
  } else {
    status.textContent = upload.message || "";
  }
}

function uploadFileWithProgress(workspaceId, file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    form.append("file", file);
    xhr.open("POST", `/api/workspaces/${workspaceId}/uploads`);
    xhr.responseType = "json";
    xhr.timeout = 0;
    xhr.upload.onprogress = event => {
      if (!event.lengthComputable) return;
      const percent = Math.max(0, Math.min(100, Math.round(event.loaded * 100 / event.total)));
      setUploadUi({status: "uploading", percent, loaded: event.loaded, total: event.total, workspaceId});
    };
    xhr.onerror = () => reject(new Error("上传连接中断，请确认 Harness 仍在运行后重试"));
    xhr.onabort = () => reject(new Error("上传已取消"));
    xhr.onload = () => {
      const body = xhr.response || (() => { try { return JSON.parse(xhr.responseText || "{}"); } catch { return {}; } })();
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(body?.detail || body?.message || `上传失败 HTTP ${xhr.status}`));
    };
    xhr.send(form);
  });
}

async function uploadSourceFile(file) {
  if (!state.selectedId || !file) return;
  if (state.upload?.status === "uploading") {
    toast("当前已有源视频正在上传");
    return;
  }
  const workspaceId = state.selectedId;
  try {
    setUploadUi({status: "uploading", percent: 0, loaded: 0, total: file.size, workspaceId});
    toast(`正在上传：${file.name}`);
    const result = await uploadFileWithProgress(workspaceId, file);
    setUploadUi({status: "done", message: result.deduplicated ? "已去重" : "上传完成", workspaceId});
    if (state.selectedId === workspaceId) {
      state.tab = "source";
      await refreshCurrent();
    }
    toast(result.deduplicated ? "检测到相同视频，已去重，不会重复分析" : "源视频上传完成，可在“素材”页直接播放预览");
  } catch (error) {
    setUploadUi({status: "error", message: error.message || "上传失败", workspaceId});
    toast(error.message);
  }
}

async function deleteCurrentWorkspace() {
  if (!state.selectedId || !state.detail?.workspace) return;
  const workspaceId = state.selectedId;
  const title = state.detail.workspace.title || workspaceId;
  if (state.upload?.status === "uploading" && state.upload.workspaceId === workspaceId) {
    toast("当前工作区仍在上传源视频，请等待上传结束后再删除");
    return;
  }
  const confirmed = window.confirm(`确定永久删除工作区“${title}”吗？\n\n会删除该工作区的数据库状态、上传源视频、抽帧、缓存和所有生成产物。此操作不可撤销。`);
  if (!confirmed) return;
  try {
    await api(`/api/workspaces/${workspaceId}`, {method: "DELETE"});
    stopJobWatcher();
    stopWorkspaceStream();
    state.workspaces = await api("/api/workspaces");
    state.selectedId = null;
    state.detail = null;
    state.tab = "flow";
    $("#deleteWorkspaceBtn").disabled = true;
    renderWorkspaceList();
    if (state.workspaces.length) {
      await selectWorkspace(state.workspaces[0].id);
    } else {
      $("#workspaceView").classList.add("hidden");
      $("#emptyState").classList.remove("hidden");
    }
    toast(`已删除工作区：${title}`);
  } catch (error) { toast(error.message); }
}


async function refreshSeriesCatalog(silent = false) {
  try {
    state.seriesCatalog = await api("/api/series");
    populateSeriesOptions();
    if (!silent) toast(`系列：${state.seriesCatalog.length} 个`);
    return state.seriesCatalog;
  } catch (error) { if (!silent) toast(error.message); return []; }
}

function populateSeriesOptions() {
  const select = $("#workspaceSeriesSelect");
  if (!select) return;
  const current = select.value;
  select.innerHTML = `<option value="">独立项目（不继承系列资产）</option><option value="__new__">＋ 新建系列</option>${state.seriesCatalog.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${Number(item.asset_count || 0)} assets</option>`).join("")}`;
  if ([...select.options].some(o => o.value === current)) select.value = current;
  const label = $("#newSeriesNameLabel");
  if (label) label.classList.toggle("hidden", select.value !== "__new__");
}

async function assignWorkspaceSeries(seriesId) {
  if (!state.selectedId || !seriesId) return;
  const episodeKey = window.prompt("本工作区的集数/项目标识（例如 EP02）", state.detail?.workspace?.settings?.episode_key || "") ?? "";
  await api(`/api/workspaces/${state.selectedId}/series`, {method:"PUT", body:JSON.stringify({series_id:seriesId, episode_key:episodeKey})});
  await refreshCurrent(); await refreshAssetCatalog(true); await refreshSeriesCatalog(true);
  state.tab="asset_library"; renderDetail(); toast("已挂载系列 Bible 与自动资产库");
}

async function createAndAssignSeries() {
  const name = window.prompt("新系列名称，例如：Mamie 归家");
  if (!name?.trim()) return;
  const series = await api("/api/series", {method:"POST", body:JSON.stringify({name:name.trim(), theme:name.trim(), description:"由 Harness 自动维护的跨集资产与连续性 Bible"})});
  await refreshSeriesCatalog(true);
  await assignWorkspaceSeries(series.id);
}

async function refreshAssetCatalog(silent = false) {
  try {
    state.assetCatalog = await api("/api/asset-libraries");
    if (!state.selectedAssetLibraryId && state.assetCatalog.length) state.selectedAssetLibraryId = state.assetCatalog[0].id;
    if (!silent) toast(`资产库：${state.assetCatalog.length} 个主题`);
  } catch (error) {
    if (!silent) toast(error.message);
  }
}

function linkedLibraryIds() {
  return new Set((state.detail?.asset_libraries || []).map(item => item.id));
}

function renderAssetLibrary() {
  const root = $("#artifactGrid");
  const linked = linkedLibraryIds();
  const contextById = Object.fromEntries((state.detail?.asset_library_context || []).map(item => [item.id, item]));
  const options = [`<option value="">让总管根据要求选择/新建主题库</option>`, ...state.assetCatalog.map(lib => `<option value="${escapeHtml(lib.id)}" ${lib.id === state.selectedAssetLibraryId ? "selected" : ""}>${escapeHtml(lib.name)} · ${escapeHtml(lib.theme)}</option>`)].join("");
  const currentSeries = state.detail?.series;
  root.innerHTML = `<div class="asset-library-workbench">
    <div class="series-bible-box"><div><strong>Series Bible</strong><div class="artifact-meta">${currentSeries ? `当前系列：${escapeHtml(currentSeries.name)}。本集批准的人物/场景/道具会自动沉淀；下一集选择同一系列后优先 REUSE / VARIANT。` : "当前是独立项目。绑定系列后，历史批准资产会自动参与检索；缺失资产将自动 CREATE。"}</div></div><div class="asset-card-actions">${currentSeries ? "" : `<select id="seriesAttachSelect"><option value="">选择已有系列</option>${state.seriesCatalog.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("")}</select><button id="attachSeriesBtn" class="mini-button">挂载系列</button><button id="createSeriesBtn" class="mini-button ok">新建系列</button>`}</div></div>
    <div class="asset-toolbar"><button id="createAssetLibraryBtn" class="button primary">新建主题资料库</button><span class="artifact-meta">主路径：本集生产 → assets_approved → 自动沉淀到系列库。手工上传只是补充能力。</span></div>
    <div class="asset-ingest-box"><h3>上传资源或资源包</h3><select id="assetIngestLibrary">${options}</select><select id="assetIngestType"><option value="">自动识别类型</option><option value="character">人物</option><option value="scene">场景</option><option value="prop">道具</option><option value="reference">参考图</option><option value="document">文档</option><option value="audio">音频</option><option value="video">视频</option></select><textarea id="assetIngestInstruction" rows="3" placeholder="例如：把这批法国豪宅人物、室内场景和神秘旅行袋归档到‘法国现代豪宅家庭’主题资料库；人物优先归类为 character。"></textarea><input id="assetFilesInput" type="file" multiple><button id="assetIngestBtn" class="button secondary">上传并让总管归档</button></div>
    <div class="asset-library-grid">${state.assetCatalog.map(lib => {
      const attached = linked.has(lib.id); const ctx = contextById[lib.id];
      const items = ctx?.items || [];
      return `<article class="asset-library-card ${attached ? "attached" : ""}" data-library="${escapeHtml(lib.id)}"><div class="artifact-card-header"><div><strong>${escapeHtml(lib.name)}</strong><div class="artifact-meta">${escapeHtml(lib.scope || "mixed")} · 主题：${escapeHtml(lib.theme)} · ${Number(lib.item_count || 0)} 项</div></div><span class="pill ${attached ? "ready" : ""}">${attached ? "已挂载" : "全局库"}</span></div><p>${escapeHtml(lib.description || "")}</p><div class="asset-card-actions">${lib.auto_managed ? `<span class="pill ready">Series 自动管理</span>` : `<button class="mini-button" data-toggle-library="${escapeHtml(lib.id)}">${attached ? "从项目卸载" : "挂载到项目"}</button>`}${attached && !lib.auto_managed ? `<button class="mini-button" data-import-kind="characters" data-library-id="${escapeHtml(lib.id)}">保存当前角色</button><button class="mini-button" data-import-kind="scenes" data-library-id="${escapeHtml(lib.id)}">保存当前场景</button><button class="mini-button" data-import-kind="props" data-library-id="${escapeHtml(lib.id)}">保存当前道具</button>` : ""}</div>${items.length ? `<div class="asset-items-mini">${items.slice(0,18).map(i => `<div class="asset-mini"><b>${escapeHtml(i.name)}</b><span>${escapeHtml(i.asset_type)}${i.variant_key ? ` · variant:${escapeHtml(i.variant_key)}` : ""}</span>${renderMedia(i.preview_url)}${lib.auto_managed ? "" : `<button class="mini-button reject" data-delete-asset-item="${escapeHtml(i.id)}" data-library-id="${escapeHtml(lib.id)}">删除</button>`}</div>`).join("")}</div>` : ""}${lib.auto_managed ? "" : `<div class="asset-danger-zone"><button class="mini-button reject" data-delete-library="${escapeHtml(lib.id)}">删除资料库</button></div>`}</article>`;
    }).join("") || `<div class="empty-canvas">还没有主题资产库。先新建一个，例如“法国现代豪宅家庭”。</div>`}</div></div>`;
  bindAssetLibraryUi();
}

function bindAssetLibraryUi() {
  const create = $("#createAssetLibraryBtn"); if (create) create.addEventListener("click", () => $("#assetLibraryDialog")?.showModal());
  const attachSeries = $("#attachSeriesBtn"); if (attachSeries) attachSeries.addEventListener("click", async () => { const id=$("#seriesAttachSelect")?.value; if(!id){toast("请选择系列");return;} try{await assignWorkspaceSeries(id);}catch(error){toast(error.message);} });
  const createSeries = $("#createSeriesBtn"); if (createSeries) createSeries.addEventListener("click", () => createAndAssignSeries().catch(error => toast(error.message)));
  const select = $("#assetIngestLibrary"); if (select) select.addEventListener("change", () => { state.selectedAssetLibraryId = select.value; });
  document.querySelectorAll("[data-toggle-library]").forEach(el => el.addEventListener("click", async () => {
    const id = el.dataset.toggleLibrary; const attached = linkedLibraryIds().has(id);
    await api(`/api/workspaces/${state.selectedId}/asset-libraries/${id}`, {method: attached ? "DELETE" : "POST"});
    await refreshCurrent(); await refreshAssetCatalog(true); state.tab = "asset_library"; renderDetail();
  }));
  document.querySelectorAll("[data-import-kind]").forEach(el => el.addEventListener("click", async () => {
    try { const result = await api(`/api/workspaces/${state.selectedId}/asset-libraries/${el.dataset.libraryId}/import/${el.dataset.importKind}`, {method:"POST"}); toast(`已存入资产库：${result.items?.length || 0} 项`); await refreshCurrent(); await refreshAssetCatalog(true); state.tab="asset_library"; renderDetail(); } catch(error){ toast(error.message); }
  }));
  document.querySelectorAll("[data-delete-asset-item]").forEach(el => el.addEventListener("click", async () => {
    if (!window.confirm("删除这个共享资产吗？挂载该资料库的其他项目也会失去它。")) return;
    try { await api(`/api/asset-libraries/${el.dataset.libraryId}/items/${el.dataset.deleteAssetItem}`, {method:"DELETE"}); await refreshAssetCatalog(true); await refreshCurrent(); state.tab="asset_library"; renderDetail(); } catch(error){ toast(error.message); }
  }));
  document.querySelectorAll("[data-delete-library]").forEach(el => el.addEventListener("click", async () => {
    if (!window.confirm("永久删除这个主题资料库及其中所有共享资产吗？此操作会影响所有挂载它的项目。")) return;
    try { await api(`/api/asset-libraries/${el.dataset.deleteLibrary}`, {method:"DELETE"}); state.selectedAssetLibraryId=""; await refreshAssetCatalog(true); await refreshCurrent(); state.tab="asset_library"; renderDetail(); } catch(error){ toast(error.message); }
  }));
  const ingest = $("#assetIngestBtn"); if (ingest) ingest.addEventListener("click", ingestAssets);
}

async function ingestAssets() {
  const files = Array.from($("#assetFilesInput")?.files || []); if (!files.length) { toast("请选择资源文件或 ZIP 资源包"); return; }
  const form = new FormData(); files.forEach(file => form.append("files", file)); form.append("instruction", $("#assetIngestInstruction")?.value || ""); form.append("library_id", $("#assetIngestLibrary")?.value || ""); form.append("asset_type", $("#assetIngestType")?.value || "");
  try { toast("总管正在归档资源…"); const response = await fetch(`/api/workspaces/${state.selectedId}/asset-libraries/ingest`, {method:"POST", body:form}); const body = await response.json().catch(()=>({})); if(!response.ok) throw new Error(body.detail || `HTTP ${response.status}`); toast(body.routing?.reply || `已归档 ${body.items?.length || 0} 项资源`); state.selectedAssetLibraryId = body.library?.id || state.selectedAssetLibraryId; await refreshAssetCatalog(true); await refreshCurrent(); state.tab="asset_library"; renderDetail(); } catch(error){ toast(error.message); }
}

async function createAssetLibrary(event) {
  event.preventDefault(); const form = new FormData(event.target); const payload = Object.fromEntries(form.entries());
  try { const lib = await api("/api/asset-libraries", {method:"POST", body:JSON.stringify(payload)}); $("#assetLibraryDialog")?.close(); state.selectedAssetLibraryId=lib.id; await api(`/api/workspaces/${state.selectedId}/asset-libraries/${lib.id}`, {method:"POST"}); await refreshAssetCatalog(true); await refreshCurrent(); state.tab="asset_library"; renderDetail(); toast(`已创建并挂载：${lib.name}`); } catch(error){ toast(error.message); }
}

function stopWorkspaceStream() { if (state.activityStream) { state.activityStream.close(); state.activityStream = null; } }
function startWorkspaceStream(workspaceId) {
  stopWorkspaceStream(); if (!workspaceId || typeof EventSource === "undefined") return;
  const stream = new EventSource(`/api/workspaces/${workspaceId}/stream`); state.activityStream = stream;
  stream.onmessage = async () => { if (state.streamRefreshBusy || state.selectedId !== workspaceId) return; state.streamRefreshBusy=true; try { state.detail = await api(`/api/workspaces/${workspaceId}`); renderDetail(); } catch(error){ console.warn("stream refresh failed",error); } finally { state.streamRefreshBusy=false; } };
  stream.addEventListener("deleted", () => { stopWorkspaceStream(); refreshWorkspaceCatalog({autoSelect:true,silent:true}).catch(()=>{}); });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}

function on(selector, eventName, handler) {
  const element = $(selector);
  if (!element) {
    console.warn(`UI element missing during bind: ${selector}`);
    return;
  }
  element.addEventListener(eventName, handler);
}

function bindUi() {
  on("#newWorkspaceBtn", "click", () => { refreshSeriesCatalog(true).finally(() => createDialog?.showModal()); });
  on("#deleteWorkspaceBtn", "click", deleteCurrentWorkspace);
  on("#emptyCreateBtn", "click", () => { refreshSeriesCatalog(true).finally(() => createDialog?.showModal()); });
  on("#closeDialogBtn", "click", () => createDialog?.close());
  on("#cancelDialogBtn", "click", () => createDialog?.close());
  on("#createForm", "submit", createWorkspace);
  on("#advanceBtn", "click", advance);
  on("#clearJobsBtn", "click", clearJobHistory);
  on("#clearStageBtn", "click", clearCurrentStage);
  on("#saveGuidanceBtn", "click", () => chatStageGuidance().catch(error => toast(error.message)));
  on("#regenerateStageBtn", "click", () => regenerateCurrentStageFromFeedback().catch(error => toast(error.message)));
  on("#flowAdvanceBtn", "click", advance);
  on("#saveMemoryBtn", "click", () => saveProjectMemory(false).catch(error => toast(error.message)));
  on("#stageGuidanceInput", "keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      chatStageGuidance().catch(error => toast(error.message));
    }
  });
  on("#refreshWorkspacesBtn", "click", () => refreshWorkspaceCatalog({autoSelect: !state.selectedId, silent: false}).catch(() => {}));
  on("#assetLibraryForm", "submit", createAssetLibrary);
  on("#workspaceSeriesSelect", "change", populateSeriesOptions);
  on("#closeAssetLibraryDialogBtn", "click", () => $("#assetLibraryDialog")?.close());

  // File picking is opened by the native <label for=sourceFileInput> in HTML.
  // This deliberately avoids programmatic input.click(), which can be blocked by
  // browser security policies or break when HTML/JS versions are temporarily mixed.
  on("#sourceFileInput", "change", event => {
    const file = event.target.files?.[0];
    if (!file) return;
    uploadSourceFile(file).finally(() => { event.target.value = ""; });
  });
}

bindUi();
init().catch(error => {
  console.error("Harness bootstrap failed", error);
  toast(`初始化失败：${error.message}`);
  // A transient startup race (e.g. uvicorn still warming up) gets one automatic retry.
  setTimeout(() => refreshWorkspaceCatalog({autoSelect: true, silent: true}).catch(() => {}), 1500);
});
