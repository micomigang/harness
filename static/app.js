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
  visualAssetDetail: null,
  candidateRequestStatus: {},
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
  bindVisualAssetOverviewUi();
  bindStoryboardUi();
  bindDialoguePlanUi();
  bindSoundPlanUi();
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
  const visualStageClass = ["characters", "scenes", "props", "reference_images"].includes(a.kind) ? " asset-workbench-card" : "";
  const storyboardStageClass = a.kind === "storyboard" ? " storyboard-workbench-card" : "";
  const dialogueStageClass = a.kind === "dialogue_plan" ? " dialogue-workbench-card" : "";
  const soundStageClass = a.kind === "sound_plan" ? " sound-workbench-card" : "";
  const reviewStageClass = a.kind === "review" ? " review-workbench-card" : "";
  return `
    <article class="artifact-card ${a.status}${visualStageClass}${storyboardStageClass}${dialogueStageClass}${soundStageClass}${reviewStageClass}">
      <div class="artifact-card-header">
        <div><strong>${escapeHtml(a.name)}</strong><div class="artifact-meta">${escapeHtml(agent?.name || "未分配 Agent")} · rev ${a.revision} · ${escapeHtml(a.provider)}</div></div>
        <div class="artifact-header-actions">
          <span class="pill ${a.status}">${escapeHtml(a.status)}</span>
          ${reviewBadge}
          ${a.kind !== "source" ? `<button class="mini-button" data-feedback-stage="${escapeHtml(a.kind)}">在对话流中调整</button><button class="mini-button reject stage-reset-button" data-reset-stage="${escapeHtml(a.kind)}">清理此节点</button>` : ""}
        </div>
      </div>
      <div class="artifact-content">${["characters", "scenes", "props", "reference_images"].includes(a.kind)
        ? `${renderVisualAssetOverview(a.content, a.kind)}${renderExecutionInfo(a.content?._execution)}${renderStageReview(a)}`
        : a.kind === "storyboard"
          ? `${renderStoryboard(a.content)}${renderExecutionInfo(a.content?._execution)}${renderStageReview(a)}`
          : a.kind === "dialogue_plan"
            ? `${renderDialoguePlan(a.content)}${renderExecutionInfo(a.content?._execution)}${renderStageReview(a)}`
            : a.kind === "sound_plan"
              ? `${renderSoundPlan(a.content)}${renderExecutionInfo(a.content?._execution)}${renderStageReview(a)}`
              : a.kind === "review"
                ? `${renderReview(a.content)}${renderReviewExecutionInfo(a.content?._execution)}${renderStageReview(a)}`
                : `${renderExecutionInfo(a.content?._execution)}${renderStageReview(a)}${renderContent(a.content, a.kind)}`}</div>
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

function storyboardArray(value) {
  if (Array.isArray(value)) return value;
  if (value === null || value === undefined || value === "") return [];
  return [value];
}

function storyboardRefLabel(ref) {
  if (ref && typeof ref === "object") {
    return String(ref.canonical_key || ref.reference_key || ref.source_id || ref.source_kind || "reference");
  }
  return String(ref || "reference");
}

function storyboardBindingGroup(label, values, extraClass = "") {
  const items = storyboardArray(values).filter(value => value !== null && value !== undefined && String(value) !== "");
  if (!items.length) return "";
  return `<div class="storyboard-binding-group ${extraClass}"><span class="storyboard-binding-label">${escapeHtml(label)}</span><div class="storyboard-binding-chips">${items.map(value => `<span>${escapeHtml(typeof value === "object" ? storyboardRefLabel(value) : String(value))}</span>`).join("")}</div></div>`;
}

function renderStoryboard(content) {
  const shots = Array.isArray(content?.shots) ? content.shots : [];
  const validation = content?.storyboard_validation || {};
  const validationStatus = String(validation.status || "");
  const expected = Number(validation.expected_shots || shots.length || 0);
  const duration = Number(content?.estimated_seconds ?? validation.duration_sum_seconds ?? 0);
  const missing = Array.isArray(validation.missing_indices) ? validation.missing_indices : [];
  const issues = [
    ...(Array.isArray(validation.missing_required_fields) ? validation.missing_required_fields : []),
    ...(Array.isArray(validation.reference_binding_gaps) ? validation.reference_binding_gaps : []),
  ];
  const summaryClass = validationStatus === "fail" ? "blocking" : "ok";
  const cards = shots.map((shot, position) => {
    const indexValue = shot?.index ?? position + 1;
    const numericIndex = Number.parseInt(String(indexValue).replace(/\D/g, ""), 10);
    const indexText = Number.isFinite(numericIndex) ? String(numericIndex).padStart(2, "0") : String(indexValue);
    const bindings = shot?.asset_bindings && typeof shot.asset_bindings === "object" ? shot.asset_bindings : {};
    const scenes = bindings.scenes ?? bindings.scene ?? [];
    const refs = storyboardArray(bindings.reference_images);
    const prompt = String(shot?.visual_prompt || "");
    const blocking = String(shot?.blocking || "");
    const camera = String(shot?.camera || "");
    const beat = String(shot?.story_beat || "");
    const durationSeconds = shot?.duration_seconds ?? "—";
    return `<article class="storyboard-shot-card">
      <div class="storyboard-shot-head">
        <div><span class="storyboard-shot-index">SHOT ${escapeHtml(indexText)}</span><span class="storyboard-shot-duration">${escapeHtml(durationSeconds)}s</span></div>
        <span class="pill ${shot?.status === "planned" ? "ready" : ""}">${escapeHtml(shot?.status || "planned")}</span>
      </div>
      <div class="storyboard-beat">${beat ? escapeHtml(beat) : '<span class="storyboard-missing">缺少 story_beat</span>'}</div>
      <div class="storyboard-bindings">
        ${storyboardBindingGroup("人物", bindings.characters)}
        ${storyboardBindingGroup("场景", scenes)}
        ${storyboardBindingGroup("道具", bindings.props)}
        ${storyboardBindingGroup(`参考图 ${refs.length}`, refs, "references")}
      </div>
      <details class="storyboard-shot-details">
        <summary>镜头细节 / Provider prompt</summary>
        ${camera ? `<div><strong>Camera</strong><p>${escapeHtml(camera)}</p></div>` : ""}
        ${blocking ? `<div><strong>Blocking</strong><p>${escapeHtml(blocking)}</p></div>` : ""}
        <div><strong>Visual prompt</strong><p>${prompt ? escapeHtml(prompt) : '<span class="storyboard-missing">缺少 visual_prompt</span>'}</p></div>
      </details>
    </article>`;
  }).join("");
  return `<div class="storyboard-workbench">
    <div class="storyboard-validation ${summaryClass}">
      <div><strong>分镜结构</strong><span>镜头 ${shots.length}/${expected || shots.length}</span><span>总时长 ${escapeHtml(duration)}s</span>${validationStatus ? `<span>Harness 校验：${escapeHtml(validationStatus)}</span>` : ""}${missing.length ? `<span>缺失编号：${escapeHtml(missing.join(", "))}</span>` : ""}${issues.length ? `<span>结构问题：${issues.length}</span>` : ""}</div>
      <button class="mini-button" data-revalidate-storyboard>重新校验当前分镜（不重生成）</button>
    </div>
    <div class="storyboard-grid">${cards || '<div class="empty-canvas">当前没有分镜镜头。</div>'}</div>
  </div>`;
}

async function revalidateStoryboard(button = null) {
  if (!state.selectedId) return;
  if (button) button.disabled = true;
  try {
    toast("正在重新校验当前分镜，不会重新生成镜头…");
    const result = await api(`/api/workspaces/${state.selectedId}/storyboard/revalidate`, {method: "POST"});
    await refreshCurrent();
    state.tab = "storyboard";
    renderDetail();
    const validation = result.storyboard_validation || {};
    toast(`分镜校验完成：${validation.actual_shots ?? "?"}/${validation.expected_shots ?? "?"} · ${validation.status || "unknown"}`);
  } finally {
    if (button) button.disabled = false;
  }
}

function bindStoryboardUi() {
  document.querySelectorAll("[data-revalidate-storyboard]").forEach(button => button.addEventListener("click", () => {
    revalidateStoryboard(button).catch(error => toast(error.message));
  }));
}

function dialogueArray(value) {
  if (Array.isArray(value)) return value;
  if (value === null || value === undefined || value === "") return [];
  return [value];
}

function dialogueTextValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  try { return JSON.stringify(value); } catch (_) { return String(value); }
}

function dialogueTimingLabel(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (typeof value === "object") {
    const start = value.start_seconds ?? value.start ?? value.local_start_seconds ?? value.shot_start_seconds;
    const end = value.end_seconds ?? value.end ?? value.local_end_seconds ?? value.shot_end_seconds;
    const globalStart = value.timeline_start_seconds ?? value.global_start_seconds ?? value.absolute_start_seconds;
    const globalEnd = value.timeline_end_seconds ?? value.global_end_seconds ?? value.absolute_end_seconds;
    const local = start !== undefined || end !== undefined ? `${start ?? "?"}s → ${end ?? "?"}s` : "";
    const global = globalStart !== undefined || globalEnd !== undefined ? `累计 ${globalStart ?? "?"}s → ${globalEnd ?? "?"}s` : "";
    if (local || global) return [local, global].filter(Boolean).join(" · ");
    return dialogueTextValue(value);
  }
  return String(value);
}

function dialogueLinesForItem(item) {
  const nested = Array.isArray(item?.lines) ? item.lines
    : Array.isArray(item?.dialogue_lines) ? item.dialogue_lines
      : Array.isArray(item?.dialogue) ? item.dialogue : [];
  if (nested.length) return nested.map(line => {
    if (typeof line === "string") return {text: line, speaker_id: item?.speaker_id || ""};
    return line && typeof line === "object" ? line : {text: String(line || "")};
  });
  const rawDialogue = typeof item?.dialogue === "string" ? item.dialogue : "";
  const text = item?.dialogue_text ?? item?.text ?? rawDialogue;
  if (text === null || text === undefined || String(text).trim() === "") return [];
  return [{
    speaker_id: item?.speaker_id,
    text,
    language: item?.language,
    delivery_note: item?.delivery_note ?? item?.delivery,
    timing: item?.timing,
    subtitle: item?.subtitle,
    lip_sync_target: item?.lip_sync_target ?? item?.lip_sync,
  }];
}

function dialogueLipLabel(value) {
  if (value === true) return "启用";
  if (value === false || value === null) return "关闭";
  if (value === undefined || value === "") return "—";
  if (typeof value === "object") {
    const target = value.target ?? value.enabled ?? value.mode ?? value.status;
    return target !== undefined ? dialogueTextValue(target) : dialogueTextValue(value);
  }
  return dialogueTextValue(value);
}

function renderDialoguePlan(content) {
  const items = Array.isArray(content?.items) ? content.items : [];
  const validation = content?.dialogue_validation || {};
  const expected = Number(validation.expected_items || items.length || 0);
  const actual = Number(validation.actual_items ?? items.length);
  const missing = Array.isArray(validation.missing_indices) ? validation.missing_indices : [];
  const structuralIssues = [
    ...(Array.isArray(validation.missing_required_fields) ? validation.missing_required_fields : []),
    ...(Array.isArray(validation.timing_issues) ? validation.timing_issues : []),
  ];
  let dialogueCount = Number(validation.dialogue_items ?? 0);
  let silentCount = Number(validation.silent_items ?? 0);
  if (!validation.dialogue_items && !validation.silent_items && items.length) {
    dialogueCount = items.filter(item => !["silent", "no_dialogue", "no-dialogue"].includes(String(item?.status || "").toLowerCase())).length;
    silentCount = items.length - dialogueCount;
  }
  const validationStatus = String(validation.status || "");
  const summaryClass = validationStatus === "fail" ? "blocking" : "ok";

  const cards = items.map((item, position) => {
    const rawIndex = item?.shot_index ?? position + 1;
    const numericIndex = Number.parseInt(String(rawIndex).replace(/\D/g, ""), 10);
    const indexText = Number.isFinite(numericIndex) ? String(numericIndex).padStart(2, "0") : String(rawIndex);
    const status = String(item?.status || "dialogue").toLowerCase();
    const silent = ["silent", "no_dialogue", "no-dialogue"].includes(status) || item?.no_dialogue === true;
    const lines = dialogueLinesForItem(item);
    const speaker = item?.speaker_id || (lines[0] && lines[0].speaker_id) || "";
    const language = item?.language || (lines[0] && lines[0].language) || "";
    const delivery = item?.delivery_note ?? item?.delivery ?? (lines[0] && (lines[0].delivery_note ?? lines[0].delivery)) ?? "";
    const timing = item?.timing ?? (lines[0] && lines[0].timing);
    const subtitle = item?.subtitle ?? (lines[0] && lines[0].subtitle) ?? "";
    const lipSync = item?.lip_sync_target ?? item?.lip_sync ?? (lines[0] && (lines[0].lip_sync_target ?? lines[0].lip_sync));
    const linesHtml = silent
      ? `<div class="dialogue-silent-panel"><strong>NO DIALOGUE</strong><span>纯视觉镜头 · Lip sync 关闭</span></div>`
      : `<div class="dialogue-lines">${lines.length ? lines.map((line, lineIndex) => {
          const lineSpeaker = line?.speaker_id || speaker || "speaker";
          const lineText = line?.dialogue_text ?? line?.text ?? line?.dialogue ?? "";
          const lineTiming = line?.timing;
          const lineDelivery = line?.delivery_note ?? line?.delivery ?? "";
          return `<div class="dialogue-line-card"><div class="dialogue-line-meta"><strong>${escapeHtml(lineSpeaker)}</strong>${lineTiming ? `<span>${escapeHtml(dialogueTimingLabel(lineTiming))}</span>` : ""}</div><div class="dialogue-line-text">${escapeHtml(dialogueTextValue(lineText))}</div>${lineDelivery ? `<div class="dialogue-delivery">${escapeHtml(dialogueTextValue(lineDelivery))}</div>` : ""}</div>`;
        }).join("") : `<div class="dialogue-missing">该镜标记为对白，但没有可显示的 dialogue_text / lines。</div>`}</div>`;
    return `<article class="dialogue-shot-card ${silent ? "silent" : "speaking"}">
      <div class="dialogue-shot-head">
        <div><span class="dialogue-shot-index">SHOT ${escapeHtml(indexText)}</span>${language ? `<span class="dialogue-language">${escapeHtml(language)}</span>` : ""}</div>
        <span class="pill ${silent ? "" : "ready"}">${escapeHtml(silent ? "NO DIALOGUE" : status || "dialogue")}</span>
      </div>
      ${linesHtml}
      <div class="dialogue-facts">
        <div><span>主 Speaker</span><b>${escapeHtml(speaker || (silent ? "—" : "未标注"))}</b></div>
        <div><span>Timing</span><b>${escapeHtml(dialogueTimingLabel(timing))}</b></div>
        <div><span>Lip Sync</span><b>${escapeHtml(dialogueLipLabel(lipSync))}</b></div>
      </div>
      ${delivery ? `<div class="dialogue-note"><span>语气 / Delivery</span><p>${escapeHtml(dialogueTextValue(delivery))}</p></div>` : ""}
      ${subtitle ? `<details class="dialogue-details"><summary>字幕与结构详情</summary><div><strong>Subtitle</strong><p>${escapeHtml(dialogueTextValue(subtitle))}</p></div><pre>${escapeHtml(JSON.stringify({timing: item?.timing, lip_sync_target: item?.lip_sync_target, lip_sync: item?.lip_sync}, null, 2))}</pre></details>` : ""}
    </article>`;
  }).join("");

  return `<div class="dialogue-workbench">
    <div class="dialogue-validation ${summaryClass}">
      <div><strong>对白与口型结构</strong><span>镜头覆盖 ${actual}/${expected || actual}</span><span>有对白 ${dialogueCount}</span><span>无对白 ${silentCount}</span>${validationStatus ? `<span>Harness 校验：${escapeHtml(validationStatus)}</span>` : ""}${missing.length ? `<span>缺失：${escapeHtml(missing.join(", "))}</span>` : ""}${structuralIssues.length ? `<span>结构问题：${structuralIssues.length}</span>` : ""}</div>
      <button class="mini-button" data-revalidate-dialogue>重新校验当前对白（不重生成）</button>
    </div>
    <div class="dialogue-grid">${cards || '<div class="empty-canvas">当前没有对白与口型条目。</div>'}</div>
  </div>`;
}

async function revalidateDialoguePlan(button = null) {
  if (!state.selectedId) return;
  if (button) button.disabled = true;
  try {
    toast("正在重新校验当前对白与口型，不会重新生成台词…");
    const result = await api(`/api/workspaces/${state.selectedId}/dialogue-plan/revalidate`, {method: "POST"});
    await refreshCurrent();
    state.tab = "dialogue_plan";
    renderDetail();
    const validation = result.dialogue_validation || {};
    toast(`对白校验完成：${validation.actual_items ?? "?"}/${validation.expected_items ?? "?"} · ${validation.status || "unknown"}`);
  } finally {
    if (button) button.disabled = false;
  }
}

function bindDialoguePlanUi() {
  document.querySelectorAll("[data-revalidate-dialogue]").forEach(button => button.addEventListener("click", () => {
    revalidateDialoguePlan(button).catch(error => toast(error.message));
  }));
}

function soundTextValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(soundTextValue).filter(x => x && x !== "—").join("；") || "—";
  if (typeof value === "object") {
    const preferred = value.text ?? value.description ?? value.note ?? value.label ?? value.name;
    if (preferred !== undefined) return soundTextValue(preferred);
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }
  return String(value);
}

function soundListHtml(value, emptyLabel = "无") {
  if (Array.isArray(value)) {
    if (!value.length) return `<span class="sound-empty">${escapeHtml(emptyLabel)}</span>`;
    return `<ul>${value.map(item => `<li>${escapeHtml(soundTextValue(item))}</li>`).join("")}</ul>`;
  }
  const text = soundTextValue(value);
  return text === "—" ? `<span class="sound-empty">${escapeHtml(emptyLabel)}</span>` : `<p>${escapeHtml(text)}</p>`;
}

function renderSoundPlan(content) {
  const items = Array.isArray(content?.items) ? content.items : [];
  const validation = content?.sound_validation || {};
  const expected = Number(validation.expected_items || items.length || 0);
  const actual = Number(validation.actual_items ?? items.length);
  const missing = Array.isArray(validation.missing_indices) ? validation.missing_indices : [];
  const issues = Array.isArray(validation.missing_required_fields) ? validation.missing_required_fields : [];
  const dialogueShots = Array.isArray(validation.dialogue_shots) ? validation.dialogue_shots : [];
  const silentShots = Array.isArray(validation.silent_shots) ? validation.silent_shots : [];
  const status = String(validation.status || "");
  const summaryClass = status === "fail" ? "blocking" : "ok";

  const cards = items.map((item, position) => {
    const rawIndex = item?.shot_index ?? position + 1;
    const numericIndex = Number.parseInt(String(rawIndex).replace(/\D/g, ""), 10);
    const indexText = Number.isFinite(numericIndex) ? String(numericIndex).padStart(2, "0") : String(rawIndex);
    const hasDialogue = dialogueShots.includes(numericIndex);
    const knownSilent = silentShots.includes(numericIndex);
    const ambience = item?.ambience;
    const foley = item?.foley ?? item?.foley_events;
    const cues = item?.cues ?? item?.sound_cues;
    const ducking = item?.ducking ?? item?.dialogue_ducking ?? item?.ducking_plan;
    const negative = item?.negative_audio ?? item?.negative_audio_constraints ?? item?.negative;
    const itemStatus = item?.status || "planned";
    return `<article class="sound-shot-card">
      <div class="sound-shot-head">
        <div><span class="sound-shot-index">SHOT ${escapeHtml(indexText)}</span>${hasDialogue ? `<span class="sound-dialogue-flag">有对白</span>` : (knownSilent ? `<span class="sound-silent-flag">无对白镜</span>` : "")}</div>
        <span class="pill ready">${escapeHtml(soundTextValue(itemStatus))}</span>
      </div>
      <div class="sound-primary-block"><span>环境 / Ambience</span>${soundListHtml(ambience, "未标注")}</div>
      <div class="sound-two-col">
        <div class="sound-block"><span>动作声 / Foley</span>${soundListHtml(foley, "无明确 Foley")}</div>
        <div class="sound-block"><span>提示音 / Cues</span>${soundListHtml(cues, "无额外 Cue")}</div>
      </div>
      <div class="sound-two-col">
        <div class="sound-block"><span>对白 Ducking</span>${soundListHtml(ducking, hasDialogue ? "未标注" : "无需对白压低")}</div>
        <div class="sound-block negative"><span>Negative Audio</span>${soundListHtml(negative, "未标注")}</div>
      </div>
      ${(item?.notes || item?.scene_acoustics) ? `<details class="sound-details"><summary>声学与结构详情</summary>${item?.scene_acoustics ? `<div><strong>Scene acoustics</strong><p>${escapeHtml(soundTextValue(item.scene_acoustics))}</p></div>` : ""}${item?.notes ? `<div><strong>Notes</strong><p>${escapeHtml(soundTextValue(item.notes))}</p></div>` : ""}</details>` : ""}
    </article>`;
  }).join("");

  return `<div class="sound-workbench">
    <div class="sound-validation ${summaryClass}">
      <div><strong>逐镜音效结构</strong><span>镜头覆盖 ${actual}/${expected || actual}</span><span>有对白镜 ${dialogueShots.length}</span><span>无对白镜 ${silentShots.length}</span>${status ? `<span>Harness 校验：${escapeHtml(status)}</span>` : ""}${missing.length ? `<span>缺失：${escapeHtml(missing.join(", "))}</span>` : ""}${issues.length ? `<span>结构问题：${issues.length}</span>` : ""}</div>
      <button class="mini-button" data-revalidate-sound>重新校验当前音效（不重生成）</button>
    </div>
    <div class="sound-grid">${cards || '<div class="empty-canvas">当前没有逐镜音效条目。</div>'}</div>
  </div>`;
}

async function revalidateSoundPlan(button = null) {
  if (!state.selectedId) return;
  if (button) button.disabled = true;
  try {
    toast("正在重新校验当前逐镜音效，不会重新生成声学设计…");
    const result = await api(`/api/workspaces/${state.selectedId}/sound-plan/revalidate`, {method: "POST"});
    await refreshCurrent();
    state.tab = "sound_plan";
    renderDetail();
    const validation = result.sound_validation || {};
    toast(`音效校验完成：${validation.actual_items ?? "?"}/${validation.expected_items ?? "?"} · ${validation.status || "unknown"}`);
  } finally {
    if (button) button.disabled = false;
  }
}

function bindSoundPlanUi() {
  document.querySelectorAll("[data-revalidate-sound]").forEach(button => button.addEventListener("click", () => {
    revalidateSoundPlan(button).catch(error => toast(error.message));
  }));
}


function reviewValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(reviewValue).filter(Boolean).join("；");
  if (typeof value === "object") {
    const preferred = value.text ?? value.message ?? value.description ?? value.evidence ?? value.note ?? value.name;
    if (preferred !== undefined) return reviewValue(preferred);
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }
  return String(value);
}

function renderReviewExecutionInfo(execution) {
  const body = renderExecutionInfo(execution);
  if (!body) return "";
  return `<details class="review-context-details"><summary>查看总管执行上下文</summary>${body}</details>`;
}

function renderReview(content) {
  const checks = Array.isArray(content?.checks) ? content.checks : [];
  const blockingFailures = Array.isArray(content?.blocking_failures) ? content.blocking_failures : [];
  const normalized = checks.map((check, index) => {
    const status = String(check?.status || "warn").toLowerCase();
    return {check: check || {}, status, index};
  });
  const order = {fail: 0, warn: 1, pass: 2};
  normalized.sort((a, b) => (order[a.status] ?? 1) - (order[b.status] ?? 1) || a.index - b.index);
  const failCount = normalized.filter(x => x.status === "fail").length;
  const warnCount = normalized.filter(x => x.status === "warn").length;
  const passCount = normalized.filter(x => x.status === "pass").length;
  const blocked = blockingFailures.length > 0 || failCount > 0;

  const blockerHtml = blockingFailures.length ? `<section class="review-blockers">
    <div class="review-section-title"><strong>阻断项</strong><span>${blockingFailures.length} 项 · 修复后重新运行一致性检查</span></div>
    <div class="review-blocker-list">${blockingFailures.map((failure, idx) => {
      const title = typeof failure === "object" && failure ? (failure.name || failure.title || failure.check || `阻断 ${idx + 1}`) : `阻断 ${idx + 1}`;
      const message = typeof failure === "object" && failure ? (failure.remediation || failure.message || failure.description || failure.evidence || "") : failure;
      const owner = typeof failure === "object" && failure ? (failure.owner || failure.agent || failure.stage || "") : "";
      return `<article class="review-blocker-card"><div><b>${escapeHtml(reviewValue(title))}</b>${owner ? `<span class="review-owner">责任：${escapeHtml(reviewValue(owner))}</span>` : ""}</div>${message ? `<p>${escapeHtml(reviewValue(message))}</p>` : ""}</article>`;
    }).join("")}</div>
  </section>` : "";

  const cards = normalized.map(({check, status}) => {
    const name = check.name || check.title || "未命名检查";
    const evidence = check.evidence ?? check.details ?? check.finding ?? "";
    const owner = check.owner ?? check.agent ?? check.stage ?? "";
    const remediation = check.remediation ?? check.suggested_adjustment ?? check.action ?? check.fix ?? "";
    return `<article class="review-check-card ${escapeHtml(status)}">
      <div class="review-check-head"><b>${escapeHtml(reviewValue(name))}</b><span class="review-status ${escapeHtml(status)}">${escapeHtml(status.toUpperCase())}</span></div>
      ${evidence ? `<div class="review-check-row"><span>证据</span><p>${escapeHtml(reviewValue(evidence))}</p></div>` : ""}
      ${owner ? `<div class="review-check-row compact"><span>责任</span><p>${escapeHtml(reviewValue(owner))}</p></div>` : ""}
      ${remediation ? `<div class="review-check-row"><span>修复</span><p>${escapeHtml(reviewValue(remediation))}</p></div>` : ""}
    </article>`;
  }).join("");

  return `<div class="review-workbench">
    <div class="review-summary ${blocked ? "blocking" : "ok"}">
      <div class="review-summary-main"><strong>一致性检查</strong><span class="review-summary-state">${blocked ? "存在阻断，暂不放行" : "无阻断，可进入下一阶段"}</span></div>
      <div class="review-summary-counts"><span class="fail">Fail ${failCount}</span><span class="warn">Warn ${warnCount}</span><span class="pass">Pass ${passCount}</span><span>Checks ${checks.length}</span></div>
    </div>
    ${blockerHtml}
    <section class="review-checks"><div class="review-section-title"><strong>全部检查</strong><span>失败与警告优先显示；通过项保留用于审计</span></div><div class="review-check-grid">${cards || '<div class="empty-canvas">当前没有 QA 检查项。</div>'}</div></section>
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
  return {characters: "角色", scenes: "场景", props: "道具", reference_images: "参考图"}[kind] || kind;
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
  } else if (kind === "reference_images") {
    add("类型", item.source_kind);
    add("绑定资产", item.source_ids || item.source_id);
    add("状态", item.status);
    add("模型", item.model);
    add("生成提示", item.prompt);
    add("上游候选 ID", item.candidate_id || item.input_candidate_ids);
    add("Library Asset", item.library_asset_id);
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

function candidateRequestKey(kind, canonicalKey) {
  return `${kind}:${canonicalKey}`;
}

function candidateStatusHtml(kind, canonicalKey) {
  const status = state.candidateRequestStatus[candidateRequestKey(kind, canonicalKey)];
  if (!status?.text) return `<div class="candidate-request-status" data-candidate-status hidden></div>`;
  return `<div class="candidate-request-status ${escapeHtml(status.type || "info")}" data-candidate-status>${escapeHtml(status.text)}</div>`;
}

function setCandidateRequestStatus(card, type, text) {
  const stage = card?.dataset?.assetStage || "";
  const canonicalKey = card?.dataset?.canonicalKey || "";
  if (!stage || !canonicalKey) return;
  const key = candidateRequestKey(stage, canonicalKey);
  if (text) state.candidateRequestStatus[key] = {type, text};
  else delete state.candidateRequestStatus[key];
  const el = card.querySelector("[data-candidate-status]");
  if (el) {
    el.hidden = !text;
    el.className = `candidate-request-status ${type || "info"}`;
    el.textContent = text || "";
  }
}

function renderAssetCandidate(candidate, baseCandidateId) {
  const isSelected = Boolean(candidate.selected);
  const isBase = candidate.id === baseCandidateId;
  const feedback = String(candidate.feedback || "").trim();
  return `<div class="visual-candidate ${isSelected ? "selected" : ""} ${isBase ? "edit-base" : ""}" data-candidate-card="${escapeHtml(candidate.id)}">
    <a class="visual-candidate-image" href="${escapeHtml(candidate.url || "#")}" target="_blank" rel="noopener">${renderMedia(candidate.url)}</a>
    <div class="visual-candidate-meta"><span>${escapeHtml(candidate.model || "Seedream")}</span>${isSelected ? `<span class="pill ready">当前采用</span>` : ""}${isBase ? `<span class="pill">调整基准</span>` : ""}</div>
    ${feedback ? `<div class="artifact-meta candidate-feedback-note">本次要求：${escapeHtml(feedback)}</div>` : ""}
    <div class="visual-candidate-actions">
      ${isSelected
        ? `<button class="mini-button" data-unselect-candidate="${escapeHtml(candidate.id)}">解除采用 / 解锁</button>`
        : `<button class="mini-button ok" data-select-candidate="${escapeHtml(candidate.id)}">设为采用并锁定</button>`}
      <button class="mini-button" data-base-candidate="${escapeHtml(candidate.id)}">基于这张调整</button>
      ${isSelected ? "" : `<button class="mini-button reject" data-delete-candidate="${escapeHtml(candidate.id)}">删除</button>`}
    </div>
  </div>`;
}

function preferredCandidateForAsset(kind, canonicalKey) {
  const candidates = currentCandidatesForAsset(kind, canonicalKey);
  if (!candidates.length) return null;
  const selected = candidates.find(candidate => candidate.selected) || null;
  const latest = candidates[candidates.length - 1] || null;
  // A selected candidate remains production truth, but it must never hide a newer
  // adjustment candidate.  Showing the newest pending candidate lets the user
  // actually see that generation changed something and switch adoption explicitly.
  if (latest && !latest.selected) return latest;
  return selected || latest;
}

function renderSingleAssetDetail(item, kind, idx = 0) {
  const label = assetStageLabel(kind);
  const canonicalKey = String(item.canonical_key || item.id || `asset-${idx+1}`);
  const candidates = currentCandidatesForAsset(kind, canonicalKey);
  const selected = candidates.find(c => c.selected);
  const rememberedBase = state.candidateBases[`${kind}:${canonicalKey}`] || "";
  const latest = candidates[candidates.length - 1];
  const baseCandidateId = rememberedBase || selected?.id || latest?.id || "";
  const rows = assetSpecRows(item, kind);
  const artifactPreviewUrl = kind === "reference_images" ? String(item.url || "") : "";
  const candidateHtml = candidates.length
    ? candidates.map(c => renderAssetCandidate(c, baseCandidateId)).join("")
    : artifactPreviewUrl
      ? `<div class="visual-candidate artifact-reference-fallback">
          <a class="visual-candidate-image" href="${escapeHtml(artifactPreviewUrl)}" target="_blank" rel="noopener">${renderMedia(artifactPreviewUrl)}</a>
          <div class="visual-candidate-meta"><span>${escapeHtml(item.model || "Reference artifact")}</span><span class="pill ready">已恢复参考</span></div>
          <div class="artifact-meta candidate-feedback-note">当前组合媒体已存在于 reference_images artifact，但本 revision 尚无独立候选行。可以直接检查；若要调整，下面“抽 1 张 / 抽 4 张”会以这张 artifact 图作为参考基准。</div>
        </div>`
      : `<div class="candidate-empty">还没有视觉候选。先“抽 1 张”，满意后点“设为采用并锁定”；不满意就写局部要求再抽。</div>`;
  return `<section class="asset-design-card detail-card" data-asset-stage="${escapeHtml(kind)}" data-canonical-key="${escapeHtml(canonicalKey)}">
    <div class="asset-design-header">
      <div><b>${escapeHtml(item.name || item.id || canonicalKey)}</b><div class="artifact-meta">${escapeHtml(canonicalKey)} · ${escapeHtml(kind === "reference_images" ? (item.source_kind || item.status || "reference") : (item.reuse_decision || item.decision || "CREATE"))}</div></div>
      ${selected ? `<span class="pill ready">已锁定视觉</span>` : `<span class="pill">待选视觉</span>`}
    </div>
    ${kind === "reference_images"
      ? `<div class="visual-localization-banner"><b>参考绑定</b><span>${escapeHtml(item.source_kind || "reference")}</span><span>${escapeHtml(assetValue(item.source_ids || item.source_id) || canonicalKey)}</span><span>调整只会生成新的参考候选，不会改写上游角色/场景/道具 Bible</span></div>`
      : `<div class="visual-localization-banner"><b>视觉本地化</b><span>目标市场：${escapeHtml(item?.visual_localization?.target_market || state.detail?.workspace?.settings?.target_market || "未设置")}</span><span>Provider Prompt：English</span><span>${item.localized_visual_design && item.generation_prompt_en ? "显式本地化设计已就绪" : "当前产物缺少显式本地化字段；抽图时仍会由 Harness Prompt Compiler 强制做目标市场适配"}</span></div>`}
    <div class="visual-detail-columns">
      <div class="visual-detail-specs"><div class="asset-spec-grid">${rows.map(([k,v]) => `<div class="asset-spec-row"><strong>${escapeHtml(k)}</strong><span>${escapeHtml(v)}</span></div>`).join("")}</div></div>
      <div class="visual-detail-candidates">
        <div class="candidate-gallery">${candidateHtml}</div>
        <div class="candidate-editor">
          <textarea rows="3" data-candidate-feedback placeholder="只改这个${escapeHtml(label)}。例如：${kind === "characters" ? "脸更接近原片、年龄感更明显；保留身份与识别色，只调整服装和气质" : kind === "scenes" ? "保持同一栋法国住宅 DNA；只调整这个空间的材质、光线或构图" : kind === "reference_images" ? "保持所有已绑定 canonical asset 的身份、脸、服装、场景结构和道具形态，只调整构图、相对位置、镜头感或光线" : "保持道具身份和关键结构；只调整材质、旧化程度或尺度"}"></textarea>
          ${candidateStatusHtml(kind, canonicalKey)}
          <div class="candidate-editor-actions"><span class="artifact-meta">${baseCandidateId ? "默认基于当前采用/最近候选继续调整；锁定只决定下游采用，不会阻止抽新候选" : "当前为文生图首抽"}</span><button class="mini-button" data-generate-candidate="1">抽 1 张</button><button class="mini-button" data-generate-candidate="4">抽 4 张</button></div>
        </div>
      </div>
    </div>
  </section>`;
}

function renderVisualAssetOverview(content, kind) {
  const items = Array.isArray(content?.items) ? content.items : [];
  const label = assetStageLabel(kind);
  const artifact = currentArtifactForStage(kind);
  const revision = Number(artifact?.revision || 0);
  const withCandidate = items.filter(item => {
    const key = String(item.canonical_key || item.id || "");
    return currentCandidatesForAsset(kind, key).length || (kind === "reference_images" && Boolean(item.url));
  }).length;
  const selectedCount = items.filter(item => {
    const key = String(item.canonical_key || item.id || "");
    return currentCandidatesForAsset(kind, key).some(c => c.selected)
      || (kind === "reference_images" && item.status === "selected_candidate" && Boolean(item.url));
  }).length;
  const tileRecords = items.map((item, idx) => {
    const canonicalKey = String(item.canonical_key || item.id || `asset-${idx+1}`);
    const candidate = preferredCandidateForAsset(kind, canonicalKey);
    const artifactPreviewUrl = kind === "reference_images" ? String(item.url || "") : "";
    const previewUrl = String(candidate?.url || artifactPreviewUrl || "");
    const recoveredArtifactOnly = !candidate && Boolean(artifactPreviewUrl);
    const batchSelected = isBatchSelected(kind, canonicalKey);
    const status = candidate?.selected ? "已采用" : candidate ? "候选" : recoveredArtifactOnly ? (item.status === "selected_candidate" ? "已恢复采用" : "已恢复参考") : "待出图";
    const sourceKind = String(item.source_kind || "");
    const sourceIds = Array.isArray(item.source_ids) ? item.source_ids : (Array.isArray(item.source_id) ? item.source_id : []);
    const isCombination = kind === "reference_images" && sourceKind === "combination";
    const title = isCombination ? "关系组合参考" : (item.name || item.id || canonicalKey);
    const subtitle = isCombination && sourceIds.length ? sourceIds.join(" + ") : canonicalKey;
    const html = `<div class="asset-overview-tile ${batchSelected ? "batch-selected" : ""} ${isCombination ? "reference-combination-tile" : ""}" data-asset-stage="${escapeHtml(kind)}" data-canonical-key="${escapeHtml(canonicalKey)}">
      <div class="asset-overview-image" data-open-visual-asset="${escapeHtml(canonicalKey)}">${previewUrl ? renderMedia(previewUrl) : `<div class="asset-overview-empty"><span>＋</span><small>暂无候选</small></div>`}</div>
      <div class="asset-overview-meta">
        <div><b>${escapeHtml(title)}</b><div class="artifact-meta ${isCombination ? "reference-combination-binding" : ""}">${escapeHtml(subtitle)}</div></div>
        <span class="pill ${(candidate?.selected || (recoveredArtifactOnly && item.status === "selected_candidate")) ? "ready" : ""}">${escapeHtml(status)}</span>
      </div>
      <div class="asset-overview-actions"><label class="batch-select-box"><input type="checkbox" data-batch-select ${batchSelected ? "checked" : ""}><span>加入批量</span></label><div class="asset-overview-action-buttons">${candidate ? (candidate.selected ? `<button class="mini-button ok" disabled>当前采用</button><button class="mini-button" data-quick-unselect-candidate="${escapeHtml(candidate.id)}">解锁</button>` : `<button class="mini-button ok" data-quick-select-candidate="${escapeHtml(candidate.id)}">${currentCandidatesForAsset(kind, canonicalKey).some(c => c.selected) ? "采用这个新候选" : "确认采用"}</button>`) : ""}<button class="mini-button" data-open-visual-asset="${escapeHtml(canonicalKey)}">查看 / 调整</button></div></div>
    </div>`;
    return {html, sourceKind, isCombination};
  });
  const tiles = tileRecords.map(record => record.html).join("");
  const batchSelectedKeys = selectedBatchKeys(kind);
  const pendingQuickCandidates = items.map((item, idx) => {
    const canonicalKey = String(item.canonical_key || item.id || `asset-${idx+1}`);
    return preferredCandidateForAsset(kind, canonicalKey);
  }).filter(candidate => candidate && !candidate.selected);
  const active = state.visualAssetDetail && state.visualAssetDetail.stage === kind ? state.visualAssetDetail.key : "";
  const activeIndex = items.findIndex((item, idx) => String(item.canonical_key || item.id || `asset-${idx+1}`) === active);
  const modal = activeIndex >= 0 ? `<div class="visual-detail-overlay" data-visual-detail-overlay>
    <div class="visual-detail-dialog" role="dialog" aria-modal="true">
      <div class="visual-detail-header"><div><strong>${escapeHtml(label)}详情与候选调整</strong><div class="artifact-meta">点击右上角关闭即可回到缩略图总览，不需要上下滚动寻找其他资产。</div></div><button class="mini-button" data-close-visual-asset>关闭</button></div>
      <div class="visual-detail-scroll">${renderSingleAssetDetail(items[activeIndex], kind, activeIndex)}</div>
    </div>
  </div>` : "";
  const coverage = kind === "reference_images" ? (content?.selection_coverage || {}) : {};
  const validation = kind === "reference_images" ? (content?.reference_validation || {}) : {};
  const missingCombinations = kind === "reference_images" && Array.isArray(content?.missing_combinations) ? content.missing_combinations : [];
  const validationStatus = String(validation?.status || "").toLowerCase();
  const referenceDiagnostics = kind === "reference_images" ? `<div class="reference-diagnostics ${(missingCombinations.length || validationStatus === "fail") ? "blocking" : "ok"}"><strong>参考图绑定状态</strong><span>上游已采用：${Number(coverage.upstream_selected_count || 0)}</span><span>孤立参考：${Number(coverage.isolated_completed || 0)}/${Number(coverage.isolated_expected || 0)}</span><span>组合参考：${Number(coverage.combination_completed || 0)}/${Number(coverage.combination_expected || 0)}</span>${validationStatus ? `<span>Harness 校验：${escapeHtml(validationStatus)}</span>` : ""}${missingCombinations.length ? `<span>缺失组合：${missingCombinations.length}</span>` : `<span>组合完整</span>`}<button class="mini-button" data-reconcile-reference-bindings>同步当前采用绑定（不重画）</button></div>` : "";
  const referenceGallery = kind === "reference_images" ? (() => {
    const isolated = tileRecords.filter(record => !record.isCombination).map(record => record.html).join("");
    const combinations = tileRecords.filter(record => record.isCombination).map(record => record.html).join("");
    const isolatedCount = tileRecords.filter(record => !record.isCombination).length;
    const comboCount = tileRecords.filter(record => record.isCombination).length;
    const isolatedOpen = Number(coverage.isolated_completed || 0) < Number(coverage.isolated_expected || 0);
    return `<div class="reference-phase-stack">
      <details class="reference-phase reference-phase-isolated" ${isolatedOpen ? "open" : ""}>
        <summary><span><strong>Phase A · 基础参考</strong><small>角色 / 场景 / 道具的已锁定 production truth</small></span><span class="pill ${Number(coverage.isolated_completed || 0) === Number(coverage.isolated_expected || 0) ? "ready" : ""}">${isolatedCount} 项</span></summary>
        <div class="asset-overview-gallery reference-phase-gallery">${isolated}</div>
      </details>
      <section class="reference-phase reference-phase-combinations">
        <div class="reference-phase-header"><span><strong>Phase B · 关系组合</strong><small>供 storyboard 直接判断人物、空间与道具关系；优先检查和确认这里。</small></span><span class="pill ${validationStatus === "pass" && comboCount === Number(coverage.combination_expected || 0) ? "ready" : ""}">${comboCount}/${Number(coverage.combination_expected || comboCount)}</span></div>
        <div class="asset-overview-gallery reference-phase-gallery">${combinations || `<div class="candidate-empty">当前没有关系组合参考。若已绑定组合要求，请先重新生成参考图。</div>`}</div>
      </section>
    </div>`;
  })() : `<div class="asset-overview-gallery">${tiles}</div>`;
  return `<div class="asset-visual-workbench compact-overview">
    <div class="visual-workbench-toolbar"><div><strong>${escapeHtml(label)}视觉结果</strong><div class="artifact-meta">rev ${revision} · ${items.length} 项 · ${withCandidate} 项已有候选 · ${selectedCount} 项已采用。点击图片进入单项详情和调整；也可以直接在卡片上确认采用。</div></div><div class="overview-toolbar-actions">${pendingQuickCandidates.length ? `<button class="mini-button ok" data-quick-select-all-current="${escapeHtml(kind)}">一键确认所有当前候选（${pendingQuickCandidates.length}）</button>` : (withCandidate && selectedCount === withCandidate ? `<span class="pill ready">当前候选均已采用</span>` : "")}</div></div>
    ${referenceDiagnostics}
    ${referenceGallery}
    <details class="batch-quick-panel" ${batchSelectedKeys.length ? "open" : ""} data-batch-workbench="${escapeHtml(kind)}">
      <summary><span>批量要求与抽图</span><span class="pill amber" data-batch-count>${batchSelectedKeys.length} 项已勾选</span></summary>
      <div class="batch-workbench-panel compact-batch">
        <div class="batch-workbench-header"><div class="artifact-meta">统一要求会应用到勾选项，但每个${escapeHtml(label)}仍结合自己的 Bible / 本地化分析分别生成。</div><div class="batch-workbench-toolbar"><button class="mini-button" data-batch-select-all>全选</button><button class="mini-button" data-batch-clear>清空勾选</button></div></div>
        <textarea data-batch-feedback placeholder="给勾选的${escapeHtml(label)}同样的要求。"></textarea>
        <div class="batch-workbench-actions"><span class="artifact-meta">橘色按钮仅重抽视觉候选，不重跑 Kimi 结构化设计。</span><button class="mini-button batch" data-generate-batch="1">对勾选项各抽 1 张</button><button class="mini-button batch" data-generate-batch="4">对勾选项各抽 4 张</button></div>
      </div>
    </details>
    ${modal}
  </div>`;
}

async function reconcileReferenceBindings(button = null) {
  if (!state.selectedId) return;
  if (!window.confirm("只同步参考图与当前角色/场景/道具已采用候选的绑定，不重新生成任何图片。继续吗？")) return;
  if (button) button.disabled = true;
  try {
    toast("正在同步参考图 candidate binding（不重画）…");
    const result = await api(`/api/workspaces/${state.selectedId}/reference-images/reconcile-bindings`, {method:"POST"});
    await refreshCurrent();
    state.tab = "reference_images";
    renderDetail();
    const validation = result.reference_validation || {};
    const changes = Array.isArray(result.binding_changes) ? result.binding_changes.length : 0;
    if (validation.status === "pass") toast(`绑定同步完成：${changes} 项更新，未重新生成图片`);
    else toast(`绑定同步完成，但仍有 ${Array.isArray(result.issues) ? result.issues.length : 0} 个冲突需要处理`);
  } finally {
    if (button && document.body.contains(button)) button.disabled = false;
  }
}

async function quickSelectVisualCandidate(candidateId, button = null) {
  if (!candidateId) return;
  if (button) button.disabled = true;
  try {
    await api(`/api/workspaces/${state.selectedId}/asset-candidates/${candidateId}/select`, {method:"POST"});
    await refreshCurrent();
    renderDetail();
    toast("已确认采用并锁定");
  } finally {
    if (button && document.body.contains(button)) button.disabled = false;
  }
}

async function unselectVisualCandidate(candidateId, button = null) {
  if (!candidateId) return;
  if (!window.confirm("解除当前采用锁定吗？图片不会删除；它会保留为普通候选，相关下游结果会等待重新确认。")) return;
  if (button) button.disabled = true;
  try {
    await api(`/api/workspaces/${state.selectedId}/asset-candidates/${candidateId}/unselect`, {method:"POST"});
    await refreshCurrent();
    renderDetail();
    toast("已解除采用锁定；图片仍保留为候选");
  } finally {
    if (button && document.body.contains(button)) button.disabled = false;
  }
}

async function quickSelectAllCurrentCandidates(kind, button = null) {
  const artifact = currentArtifactForStage(kind);
  const items = Array.isArray(artifact?.content?.items) ? artifact.content.items : [];
  const pending = items.map((item, idx) => {
    const canonicalKey = String(item.canonical_key || item.id || `asset-${idx+1}`);
    return preferredCandidateForAsset(kind, canonicalKey);
  }).filter(candidate => candidate && !candidate.selected);
  if (!pending.length) { toast("当前没有待确认候选"); return; }
  if (!window.confirm(`将把 ${pending.length} 张当前候选设为下游采用图并锁定。继续吗？`)) return;
  if (button) button.disabled = true;
  let success = 0;
  const failures = [];
  try {
    for (const candidate of pending) {
      try {
        await api(`/api/workspaces/${state.selectedId}/asset-candidates/${candidate.id}/select`, {method:"POST"});
        success += 1;
      } catch (error) {
        failures.push(error.message || String(error));
      }
    }
    await refreshCurrent();
    renderDetail();
    toast(failures.length ? `已确认 ${success}/${pending.length} 项；${failures.length} 项失败` : `已确认采用 ${success} 项当前候选`);
  } finally {
    if (button && document.body.contains(button)) button.disabled = false;
  }
}

function bindVisualAssetOverviewUi() {
  document.querySelectorAll("[data-quick-select-candidate]").forEach(button => button.addEventListener("click", async event => {
    event.preventDefault();
    event.stopPropagation();
    try { await quickSelectVisualCandidate(button.dataset.quickSelectCandidate || "", button); }
    catch (error) { toast(error.message); }
  }));
  document.querySelectorAll("[data-quick-unselect-candidate]").forEach(button => button.addEventListener("click", async event => {
    event.preventDefault();
    event.stopPropagation();
    try { await unselectVisualCandidate(button.dataset.quickUnselectCandidate || "", button); }
    catch (error) { toast(error.message); }
  }));
  document.querySelectorAll("[data-quick-select-all-current]").forEach(button => button.addEventListener("click", async event => {
    event.preventDefault();
    try { await quickSelectAllCurrentCandidates(button.dataset.quickSelectAllCurrent || "", button); }
    catch (error) { toast(error.message); }
  }));
  document.querySelectorAll("[data-open-visual-asset]").forEach(el => el.addEventListener("click", event => {
    event.preventDefault();
    const tile = el.closest("[data-asset-stage]");
    if (!tile) return;
    state.visualAssetDetail = {stage: tile.dataset.assetStage, key: tile.dataset.canonicalKey};
    renderDetail();
  }));
  document.querySelectorAll("[data-close-visual-asset]").forEach(el => el.addEventListener("click", () => {
    state.visualAssetDetail = null;
    renderDetail();
  }));
  document.querySelectorAll("[data-visual-detail-overlay]").forEach(overlay => overlay.addEventListener("click", event => {
    if (event.target !== overlay) return;
    state.visualAssetDetail = null;
    renderDetail();
  }));
}

function renderAssetDesignWorkbench(content, kind) {
  const items = Array.isArray(content?.items) ? content.items : [];
  const label = assetStageLabel(kind);
  const artifact = currentArtifactForStage(kind);
  const revision = Number(artifact?.revision || 0);
  const withCandidate = items.filter(item => {
    const key = String(item.canonical_key || item.id || "");
    return currentCandidatesForAsset(kind, key).length || (kind === "reference_images" && Boolean(item.url));
  }).length;
  const selectedCount = items.filter(item => {
    const key = String(item.canonical_key || item.id || "");
    return currentCandidatesForAsset(kind, key).some(c => c.selected)
      || (kind === "reference_images" && item.status === "selected_candidate" && Boolean(item.url));
  }).length;
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
  setCandidateRequestStatus(card, "loading", `正在生成 ${count} 张候选，请稍候…`);
  try {
    toast(`${assetStageLabel(stage)} ${canonicalKey} 正在抽 ${count} 张…`);
    const result = await api(`/api/workspaces/${state.selectedId}/asset-candidates`, {
      method: "POST",
      body: JSON.stringify({stage, canonical_key: canonicalKey, feedback, base_candidate_id: baseCandidateId, count}),
    });
    const created = result.items || [];
    if (created.length) state.candidateBases[key] = created[created.length - 1].id;
    const partial = result.status === "partial" || created.length < Number(result.requested_count || count);
    state.candidateRequestStatus[key] = partial
      ? {type:"warning", text:`已生成 ${created.length}/${result.requested_count || count} 张；后续请求失败：${result.error || "请检查 Provider"}`}
      : {type:"success", text:`已生成 ${created.length} 张候选。`};
    await refreshCurrent();
    state.tab = stage;
    renderDetail();
    const hadSelected = Boolean(selected);
    toast(partial
      ? `已生成 ${created.length}/${result.requested_count || count} 张候选`
      : hadSelected
        ? `已生成 ${created.length} 张新候选；原采用图仍锁定，下方/总览会优先显示最新候选供比较`
        : `已生成 ${created.length} 张候选`);
    return result;
  } catch (error) {
    state.candidateRequestStatus[key] = {type:"error", text:`生成失败：${error.message || error}`};
    // A previous request in a multi-image run may already have been saved. Refresh
    // even on error so successful candidates never remain invisible until reload.
    try {
      await refreshCurrent();
      state.tab = stage;
      renderDetail();
    } catch (refreshError) {
      console.warn("candidate refresh after error failed", refreshError);
      setCandidateRequestStatus(card, "error", `生成失败：${error.message || error}`);
    }
    throw error;
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
  document.querySelectorAll("[data-reconcile-reference-bindings]").forEach(button => button.addEventListener("click", () => {
    reconcileReferenceBindings(button).catch(error => toast(error.message));
  }));
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
  document.querySelectorAll("[data-unselect-candidate]").forEach(button => button.addEventListener("click", async () => {
    try { await unselectVisualCandidate(button.dataset.unselectCandidate || "", button); }
    catch (error) { toast(error.message); }
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

  // If the user has just typed a new requirement, bind that new turn first.
  if (typed) {
    const result = await chatStageGuidance(typed, stage);
    const plan = result.plan || {};
    if (plan.requires_user_input) {
      const questions = Array.isArray(plan.questions) ? plan.questions.join("；") : "总管需要更多信息";
      throw new Error(`总管需要你先补充：${questions}`);
    }
    return result;
  }

  // A plan already confirmed in Director Chat is an executable binding. Advancing
  // must reuse it instead of asking Kimi to confirm the same thing again. The
  // backend /run endpoint still validates the fingerprint, so an actually stale
  // plan remains safely blocked.
  const record = state.detail?.guidance?.[stage] || {};
  const cachedPlan = record.director_plan || {};
  const cachedHash = String(record.plan_input_hash || "").trim();
  const effective = String(record.user_instruction || "").trim();
  if (cachedHash && effective && Object.keys(cachedPlan).length) {
    if (cachedPlan.requires_user_input) {
      const questions = Array.isArray(cachedPlan.questions) ? cachedPlan.questions.join("；") : "总管需要更多信息";
      throw new Error(`总管需要你先补充：${questions}`);
    }
    return {
      plan: cachedPlan,
      effective_instruction: effective,
      input_hash: cachedHash,
      reused_bound_plan: true,
    };
  }

  // No executable binding exists yet: ask the director once, bind it, then run.
  const result = await chatStageGuidance(
    "请结合最新生成结果、项目记忆和已有沟通，确认本节点下一次执行方案；如无阻塞问题，请直接给出可执行指令。",
    stage,
  );
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
