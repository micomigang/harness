# 2026-09-21 素材分析修复说明

本包已完成“上传视频后 Kimi 400”与“固定少量抽帧但模型看不到画面”两类问题的修复。

## 已修改文件

- `app/providers/openai_compat.py`
  - 删除显式 `temperature=0.3`，避免 Kimi K2.6 固定采样参数冲突导致 HTTP 400。
  - 素材分析阶段将选中的 JPG 以 Base64 `image_url` 多模态输入真实发送给 Kimi。
  - 每张视觉证据帧都带视频名、时间戳和 `base/scene` 类型标签。
  - 上游非 2xx 响应保留 response body，任务列表可直接看到真实 API 错误。
  - 后续文本阶段不会重复把大批 `source_media` 帧元数据发送给模型。
- `app/providers/source_media.py`
  - 固定 6 帧改为默认 `1 fps` 基础采样。
  - 增加 FFmpeg scene score 镜头切换补帧，默认阈值 `0.12`。
  - 基础抽帧改为单次 FFmpeg 解码，不再为每一帧启动一个 FFmpeg 进程。
  - 通过帧数和图片字节预算控制发送给模型的规模；短视频默认会发送全部候选帧。
  - 重跑分析时清理旧版本 `frame-*.jpg` 和新版本的 base/scene 帧。
- `app/config.py`
  - 新增自适应抽帧配置项。
- `app/providers/registry.py`
  - 将新配置注入 `SourceMediaProcessor`。
- `app/agents.py`
  - 素材分析师合同更新为 1 fps + scene cut 多模态证据。
- `static/app.js`
  - 分析结果帧很多时，浏览器最多均匀预览 80 张，避免页面一次渲染数百张图片。
- `tests/test_source_media.py`
  - 新增 FPS 自适应、scene 帧合并与模型帧预算测试。
- `tests/test_openai_compat.py`
  - 新增 K2.6 payload 不发送 temperature、多模态图片输入、上游错误正文保留测试。
- `README.md`
- `docs/MISSING_APIS.md`
- `docs/AGENT_ARCHITECTURE_AND_DEBUG.md`
  - 同步新行为和配置说明。

## 默认配置

```text
SOURCE_BASE_FPS=1.0
SOURCE_SCENE_THRESHOLD=0.12
SOURCE_MAX_BASE_FRAMES=1200
SOURCE_MAX_SCENE_FRAMES=300
SOURCE_MAX_MODEL_FRAMES=480
SOURCE_MAX_MODEL_IMAGE_BYTES=48000000
SOURCE_FRAME_WIDTH=768
```

`SOURCE_MAX_MODEL_IMAGE_BYTES` 是 Base64 编码前的图片总字节预算。48 MB 原图预算经 Base64 膨胀后仍给 Kimi 100 MB 请求体限制留有明显余量。

## 用本次 EP002 实测

对上传的 `EP002_全家把我当乡下老太，都吓傻了_screen_record.mp4`：

- 时长：约 `258.866 s`
- 基础帧：`259`
- scene cut 补帧：`34`
- 合计视觉证据：`293`
- 发送给模型：`293`
- 图片原始总量：约 `8.84 MB`
- 本地 FFmpeg 两轮处理：约 `13 s`（当前测试容器）

因此这条约 4 分 19 秒的视频现在基本实现“每秒至少一帧 + 镜头切换额外补帧”，而不是原来的固定 6 帧。

## 验证

```text
16 passed
```

已通过项目全部 pytest；并用用户上传的 EP002 做了真实 FFmpeg 抽帧烟雾测试。

## 仍未接入

`source-audio.mp3` 仍只是本地提取，尚未自动走 ASR。因此 Kimi 当前获得的是视觉帧 + brief + 技术摘要，而不是逐句对白转写。要得到精确对白和台词时间戳，仍需继续接 ASR。


## 追加修复：分段并行素材分析（同日）

为避免长视频在单次多模态请求中触发 Kimi `262144` token 上下文上限，本次又追加了“分段并行分析 + 汇总”的流程：

- `app/providers/openai_compat.py`
  - 素材分析阶段改为：
    1. 本地抽帧；
    2. 按时间段切分（默认 `30s` 一段）；
    3. 每段最多发送 `12` 张图；
    4. 多个段可并行请求 Kimi；
    5. 最后再用一次纯文本 Kimi 汇总所有分段结果为全局 `analysis` JSON。
  - 这样既保留 1fps 本地证据密度，也避免把几百张图一次性塞进单个 API 请求。
- `app/providers/source_media.py`
  - 新增 `build_analysis_segments()`，把已选中的视觉证据帧按时段拆分，并在每段内再次限流选择。
- `app/config.py`
  - 新增：
    - `SOURCE_SEGMENT_SECONDS=30`
    - `SOURCE_SEGMENT_MAX_FRAMES=12`
    - `SOURCE_SEGMENT_PARALLELISM=4`
- `app/providers/registry.py`
  - 把上述分段参数注入 `OpenAICompatibleProvider`。
- `tests/test_source_media.py`
  - 新增 30 秒分段与每段帧数上限测试。
- `tests/test_openai_compat.py`
  - 调整 Provider 测试以覆盖新的 payload 构造接口。

### 新的素材分析链路

```text
上传 MP4
  ↓
FFprobe + 1fps 基础抽帧 + scene cut 补帧
  ↓
选中候选视觉证据帧
  ↓
每 30 秒切成一个 segment
  ↓
每段最多 12 张图，并行请求 Kimi
  ↓
得到 segment_analyses[]
  ↓
纯文本汇总请求
  ↓
最终 analysis JSON
```


## 追加修复：分段缓存 / 断点续跑

在 30 秒并行分析基础上，进一步加入两级持久缓存：

1. **媒体预处理缓存**
   - 默认 `SOURCE_MEDIA_CACHE_ENABLED=true`。
   - 缓存键由上传视频 SHA256（无 SHA 时回退到文件大小/mtime）和抽帧配置组成。
   - 命中后直接复用 `base-*.jpg`、`scene-*.jpg` 与 `source-audio.mp3`，不再重复跑 FFmpeg。
   - 缓存清单位于对应 `source_frames/<video>/media-manifest.json`。

2. **Kimi 分段分析缓存**
   - 默认 `SOURCE_ANALYSIS_CACHE_ENABLED=true`。
   - 每个 30 秒 segment 成功后立即原子写入独立 JSON 缓存。
   - 缓存键覆盖：Kimi 模型、分析 prompt、source revision/brief、源视频 SHA256、抽帧参数、segment 时间范围及代表帧时间戳。
   - 某一段失败不会丢掉其他成功段；下一次点击“生成素材分析”只调用失败/缺失段。
   - 如果全部 segment 已成功但最终 synthesis 失败，下一次只重新做 synthesis。
   - 最终 synthesis 自身也缓存；完全相同输入再次执行可 0 次 Kimi API 调用。
   - 缓存目录：`data/outputs/<workspace_id>/analysis_cache/`。

3. **强制刷新**
   - 仅强制重新调用 Kimi：`SOURCE_ANALYSIS_CACHE_ENABLED=false`
   - 连 FFmpeg 帧/音轨也重新生成：再设 `SOURCE_MEDIA_CACHE_ENABLED=false`

### 本次 EP002 本地烟雾验证

上传视频约 258.866 秒：

- 本地视觉证据：293 帧
- 30 秒逻辑分段：9 段
- 每段实际代表帧：11–12 张
- 首次 FFmpeg 处理：约 16.6 秒（当前测试容器）
- 第二次媒体缓存命中：约 0.02 秒

完整 pytest：`25 passed`。


## 追加修复：超时与重复触发安全阀（同日）

为解决 `The read operation timed out` 与用户重复点击造成的并发/重复问询，本次进一步追加：

- `app/providers/openai_compat.py`
  - OpenAI-compatible / Kimi 请求的读取超时默认提升到 `900s`。
  - 新增环境变量：
    - `OPENAI_COMPAT_CONNECT_TIMEOUT_SECONDS`（默认 `30`）
    - `OPENAI_COMPAT_READ_TIMEOUT_SECONDS`（默认 `900`）
  - 当上游读超时时，返回更明确的错误信息，提示“上游模型可能已经收到了请求”，并建议等待后结合缓存续跑。
- `app/db.py`
  - 新增 `acquire_job()` 与活动任务查询逻辑。
  - 同一工作区只允许一个 `queued/running` 任务占用执行槽位。
  - 如果重复触发同一阶段，则直接复用已存在任务；如果工作区已有别的运行中任务，则阻止新任务创建。
- `app/main.py`
  - `/run/{stage}` 接口现在使用后端安全阀，避免重复创建任务。
- `static/app.js`
  - 前端检测到工作区已有活动任务时，会禁用“推进下一步”按钮。
  - 如果用户仍尝试推进，则前端会直接复用并轮询现有任务，而不是再次触发新任务。
- `tests/test_db_jobs.py`
  - 新增对任务去重/工作区互斥的测试。

### 新增的安全阀行为

```text
同一工作区已有 analysis 任务 running
  ↓
再次点“生成素材分析”
  ↓
不再新建任务
  ↓
直接复用现有 job 并继续轮询
```

```text
同一工作区 analysis 正在 running
  ↓
又试图触发 script
  ↓
后端拒绝新建 script job
  ↓
避免并发串线 / 重复扣费 / 重复问询
```


## 追加修复：实时任务进度、工作区清理、重复素材去重、编剧输出（同日）

- `static/app.js`
  - job 启动后立即显示，不再等第一次手动刷新。
  - 运行中 job 每 1.5 秒自动轮询并实时刷新进度/消息；结束后自动刷新工作区产物与下一步。
  - 新增“清理任务记录”“清空工作区”。
  - 剧本增加专用渲染：展示 beats、场景动作、逐句对白、场尾钩子与完整 JSON。
- `static/index.html` / `static/style.css`
  - 增加清理任务、清空工作区按钮和剧本显示样式。
- `app/db.py`
  - 新增工作区级删除、终态任务清理、服务重启后的僵尸任务恢复。
- `app/main.py`
  - 新增 `DELETE /api/workspaces/{id}` 与 `DELETE /api/workspaces/{id}/jobs`。
  - 上传视频按 SHA256 去重，同文件不会反复追加到 source artifact。
- `app/providers/source_media.py`
  - 对旧工作区里历史重复 source 条目再次做运行时去重，确保同一视频只分析一次。
- `app/providers/openai_compat.py`
  - 编剧输入剔除 raw `segment_analyses/source_media/analysis_plan`，只使用全局素材分析结论。
  - 编剧 Prompt 改为生产级剧本 schema，强制详细场景、动作、对白与场尾钩子。
  - 分段分析 Prompt 增加字段条数限制，减少单段数千 token 的冗长输出和长响应风险。
- `app/orchestrator.py`
  - 素材分析会回写分段进度；退回审批后可真正重新生成对应阶段。

已针对任务安全阀、工作区删除、僵尸任务恢复、旧 source 去重、上下文压缩和退回重生成增加回归测试。

## v4：总管需求控制层、项目记忆与逐阶段用户指导

本次把 Harness 从“固定 Prompt 的流水线”升级为“每个阶段都可由用户指导、由总管模型解释并下发执行指令”的控制架构。

### 1. 每个可执行阶段都有用户要求入口

右侧新增“总管沟通 / 本步要求”。当下一步是 `analysis/script/characters/.../delivery_qa` 时，UI 会显示该阶段对应的输入框和示例提示。用户可以：

- 保存本步骤要求；
- 先点击“让总管解析”预览 Kimi 对要求的理解；
- 不预览，直接“按要求推进下一步”，后台仍会先经过总管解析再执行下游阶段。

阶段要求写入 SQLite，重启 Harness 后仍保留。

### 2. 新增总管（Art Director）规划调用

在真正执行每个阶段前，Kimi 先作为总管读取：

- 当前阶段和负责 Agent 的职责边界；
- 工作区 brief / 目标语言 / 目标市场；
- 项目长期记忆；
- 用户对本步骤的自然语言要求；
- 上游产物的压缩上下文；
- 当前阶段允许修改的参数白名单。

总管输出结构化 `execution_directive`：

- `interpretation`
- `prompt_addendum`
- `parameter_overrides`
- `acceptance_criteria`
- `memory_candidates`
- `warnings`
- `questions`
- `requires_user_input`

该指令会作为下一 Agent / Provider 的执行上下文，并写入最终 artifact 的 `_execution` 字段，便于追溯“为什么这一版这样生成”。

### 3. 参数修改采用安全白名单

总管不能任意拼 API 参数，只允许在已实现且验证过的范围内调整：

- `analysis`: `segment_seconds`, `segment_max_frames`, `segment_parallelism`
- `storyboard`: `storyboard_count`
- `reference_images`: `max_assets`
- `preview/batch_video`: `resolution`, `ratio`, `generate_audio`, `batch_max_shots`
- `compose`: `video_crf`, `audio_bitrate_kbps`

未知参数（例如随意修改 `temperature`）会被丢弃，避免再次引入模型兼容性问题。

### 4. 项目记忆与沟通历史

新增持久化：

- `workspace_memory`: 跨阶段长期要求，例如“法国市场、保留原剧情功能、不要逐字翻译”；
- `stage_guidance`: 每个阶段自己的用户要求和最近一次总管计划；
- `guidance_messages`: 用户与总管的阶段沟通历史；
- `jobs.request_json`: 任务启动时的要求快照，避免运行期间 UI 改动改变已提交任务语义。

### 5. 法国本地化剧本 Prompt 重构

`script` 不再只要求“目标语言改写”，而是明确要求“目标市场本地化”：

- 人名、地点、制度、家庭/社会礼仪、称谓、阶层符号、货币、饮食、交通、职场语境和习语可按目标市场适配；
- 法国项目默认要求自然法国法语、稳定的 `tu/vous` 关系、法国可接受的人名/地点/制度与欧元语境；
- 保留源片剧情顺序、人物功能、核心冲突、反转和 cliffhanger，除非用户明确要求结构改编；
- 新增 `target_market`, `localization_strategy`, `localization_map`, `cultural_adaptations`, `open_questions`；
- `scenes` 必须是实际可生产剧本，包含动作和可说出口的结构化台词，而不是五条剧情概要。

### 6. 下游媒体 Provider 也接受总管指导

- Seedream：总管的 `prompt_addendum` 会追加到视觉资产 Prompt；可限制参考图数量。
- Seedance：总管要求会进入视频 Prompt；可在白名单内调整分辨率、画幅、是否生成音频、批量镜头上限。
- FFmpeg compose：可安全调整 CRF 与音频码率。
- Analysis：用户对分析重点与分段参数的要求会同时影响分段 Prompt、汇总 Prompt 和分析缓存键。

### 7. UI 改进

右侧现在同时展示：

- 当前下一节点与负责 Agent；
- 本步用户要求；
- 总管理解、追加 Prompt、参数覆盖和验收标准；
- 总管建议写入长期记忆的候选项；
- 项目长期记忆编辑器；
- 最近的用户 / 总管沟通记录。

剧本页面新增目标市场、本地化策略、本地化映射和每场的文化适配展示。

### 验证

```text
43 passed
Node app.js syntax check passed
```


## 2026-09-22 v5：区域清理、总管强绑定对话、生成后复盘

- `app/db.py`
  - 新增按 stage 批量删除 artifact、按 stage 清理已结束 job、仅失效总管 plan 但保留用户阶段要求。
- `app/orchestrator.py`
  - 新增依赖图驱动的 `reset_stage()`；上游与 source 永远不被下游区域清理误删。
  - 新增 `chat_stage()`：每条用户消息均由总管回应并形成新的 `effective_instruction`。
  - 新增 `validate_bound_plan()`：执行阶段必须使用当前上下文对应的总管计划 hash。
  - 每个阶段生成后自动调用总管进行 post-generation review，并把回复写入沟通历史。
- `app/providers/openai_compat.py`
  - 总管 Prompt 升级为多轮对话式规划：读取既有有效要求、对话历史和真实产物上下文，返回 `reply + effective_instruction + execution_directive`。
  - 新增生成后复盘调用，比较实际产物与用户要求并给出下一步建议。
- `app/main.py`
  - 新增 `POST /api/workspaces/{id}/guidance/{stage}/chat`。
  - 新增 `DELETE /api/workspaces/{id}/stages/{stage}`。
  - `POST /run/{stage}` 强制校验 `director_plan_hash`，防止对话与真实执行脱节。
  - 清理 analysis 时只清 Kimi 分析缓存，不清 FFmpeg 媒体缓存。
- `static/index.html` / `static/app.js` / `static/style.css`
  - “清空工作区”改成“清理当前区域”；每个产物卡片可单独清理。
  - 新增素材、素材分析独立 Tab。
  - 总管输入框改为消息式对话；显示当前已绑定要求；推进任务前强制总管确认。
  - 生成完成后显示总管“生成复盘”消息。
- `tests/`
  - 新增区域清理边界、分支依赖保留、对话绑定 hash 与上游 revision 失效测试。

验证：`48 passed`，Python compileall 与 Node `app.js` syntax check 均通过。

## 2026-09-22：源视频上传与工作区删除修复（v6）

- 源视频上传改为“临时文件写入 → 完整接收后原子替换”，上传中断不会破坏已有同名源视频。
- 上传期间如果工作区已有 `queued/running` 任务，后端返回 409，避免执行中的分析读取到被替换的视频。
- 前端上传改为 `XMLHttpRequest`，显示实时百分比和 MB 进度，不再只显示短暂 toast。
- `/source-media` 挂载本地 `data/uploads`，素材卡片可直接播放上传后的原视频；旧工作区即使 DB 内没有 `url` 字段也会按文件名生成预览地址。
- 重复上传相同 SHA256 继续去重；历史重复记录会被压缩。
- UI 新增“删除当前工作区”，调用已有 DELETE API，删除数据库状态以及该工作区的 `data/uploads/<id>` 与 `data/outputs/<id>`。
- 运行中任务或正在上传的工作区禁止删除，避免数据竞争。

## 2026-09-22 v7：启动工作区列表与源视频文件选择器可靠性

本次针对两个前端启动级问题修复：进入 Harness 后左侧工作区列表为空，以及“上传源视频”按钮无法唤起系统文件选择器。

- `static/app.js`
  - 启动时不再使用会“一项失败全部失败”的 `Promise.all([health, meta, workspaces])` 逻辑。
  - 工作区目录现在独立优先加载；即使 `/api/health` 或 `/api/meta` 暂时失败，已有 Workspace 仍会先显示在左侧。
  - 增加工作区读取错误提示和“刷新”按钮；启动瞬间遇到 Uvicorn 暂未完全就绪时会自动重试一次。
  - 所有顶层 UI 事件改为安全绑定，一个旧缓存 DOM 节点缺失不再导致整个 `app.js` 提前中止。
  - 上传文件选择不再依赖 JavaScript `input.click()`。
- `static/index.html`
  - “上传源视频”改为原生 `<label for="sourceFileInput">` 触发文件输入，直接依赖浏览器原生文件选择行为。
  - 增加工作区“刷新”按钮。
  - JS/CSS URL 增加 v7 版本参数，避免浏览器同时使用旧 HTML 与新 JS（或反之）。
- `static/style.css`
  - 增加原生文件输入的无障碍隐藏样式、文件选择触发器状态和工作区加载错误样式。
- `app/main.py`
  - `/` 与 `/static/*` 返回 `no-store/no-cache`，本地频繁覆盖版本后不再容易出现前端文件混版。
- `tests/test_frontend_bootstrap.py`
  - 增加文件选择器、静态资源版本和独立 Workspace bootstrap 回归测试。

验证：`51 passed`，`app.js` 通过 Node syntax check。

## 2026-09-22 v8：主题资产库、线性制作对话流、总管复盘闸门

- `app/providers/openai_compat.py`
  - 素材分析候选人物不再视为固定 cast；编剧 Prompt 拆分源片事实与本地化创作决策。
  - 剧本新增 `source_fact_register`, `adaptation_decisions`, `episode_sections`, `asset_requirements`，并强化疑似集数边界处理与自然法语对白要求。
  - 总管技术参数 override 仅在用户显式要求时允许执行；新增资源包/资产归档规划 Prompt。
- `app/guidance.py`
  - 增加显式技术参数授权检测；“更详细”等语义要求只改 Prompt，不自动改分段/并发等成本参数。
- `app/db.py`
  - 新增 `stage_reviews`, `asset_libraries`, `asset_items`, `workspace_asset_libraries`。
  - Job 状态/进度变化写入 activity event，支持线性制作流。
- `app/orchestrator.py`
  - 运行上下文注入共享资产库；资产库变化会令旧总管 plan 失效。
  - 生成后复盘按 Artifact revision 持久化，并影响下一步建议；人工明确审批仍可覆盖总管建议。
- `app/main.py`
  - 新增主题资产库 CRUD、挂载/卸载、资源包上传/Kimi 路由、当前 Artifact 回存资产库 API。
  - 新增 Workspace SSE 活动流；审批增加显式 `force` 人工覆盖。
- `app/providers/seedream.py`
  - 角色/场景/道具可读取 `library_asset_id` 对应的共享资产描述与 continuity metadata，作为生成参考图的稳定 Prompt anchor。
- `static/index.html`, `static/app.js`, `static/style.css`
  - 新增“制作对话流”和“主题资产库”工作台。
  - 用户/总管消息、任务进度、产物更新、复盘、审批以线性时间流展示并通过 SSE 自动更新。
  - 可创建角色/场景/道具等主题库，上传单资源或 ZIP 资源包，交由 Kimi 归档，或把当前项目资产保存到共享库。
  - 总管复盘不通过时默认显示重生成建议，用户仍可选择“仍然确认”覆盖。
- `tests/`
  - 增加技术参数显式授权、共享资产跨 Workspace、活动流进度、stage review、复盘阻塞与人工覆盖等回归测试。

验证：`58 passed`，Python compileall 与 Node `app.js` syntax check 均通过。

## v9：Series Bible + Asset Manifest + 缺失资产自动生成（2026-09-22）

- 新增 `production_series`：Workspace 可属于一个系列并设置 `episode_key`。
- 系列自动创建人物/场景/道具三类长期资产库，后续 Episode 选择同系列即可继承。
- 新增 `asset_manifest` 工作流节点，位于“确认剧本”之后、“角色/场景/道具”之前。
- Kimi 对每个剧本资产做 `REUSE / VARIANT / CREATE` 决策：
  - 有同系列合适资产：REUSE；
  - 同一身份但状态变化：VARIANT；
  - 没有资产：CREATE，并由下游 Agent 自动生成，不要求用户先上传。
- 增加后端安全校验：模型不能伪造 `library_asset_id`；不存在的引用自动降级为 CREATE。
- 角色/场景/道具输出被 Asset Manifest 强制绑定，禁止自行增加未解析的 recurring asset。
- `assets_approved` 后自动把本集批准资产写入 Series Bible，参考图也回写为视觉锚点。
- Variant 使用 `parent_item_id + variant_key` 存储，不覆盖 canonical base。
- Seedream 对已有且带参考图的 REUSE 资产直接复用参考图；CREATE/VARIANT 才生成新参考图。
- UI 新建 Workspace 可选择 Series / Episode；已有 Workspace 可在主题资产库页挂载 Series。

## v10 · 对话式反馈重生成与统一制作流（2026-09-23）

- 把总管输入框从右侧独立控制台移动到中央“制作对话流”底部，形成类似聊天产品的单一时间线：用户消息 → Kimi 总管 → Agent 任务进度 → 产物更新 → 总管复盘。
- 新增 `POST /api/workspaces/{workspace_id}/stages/{stage}/regenerate`：用户可针对任何已生成节点直接给修改意见并重生成。
- 反馈重生成会保留项目长期记忆、该节点历史对话和旧 revision 快照；当前节点标记 stale，下游依赖自动失效，再由总管把旧要求与新反馈合并成新的 effective instruction。
- 新增 `artifact_revisions` 快照表，重生成前的旧版本不会随着新 revision 覆盖而完全丢失。
- 每个产物卡片及制作流中的产物/复盘事件均可“一键进入对话流调整”。
- 中央输入区新增：`发送`、`反馈并重生成当前节点`、`推进下一步`，支持 Enter 发送 / Shift+Enter 换行。
- Kimi 总管在 `regenerate_current` 模式下必须明确说明“理解了什么 / 保留什么 / 修改什么 / 哪些下游会失效”，并把历史有效要求与本轮反馈整合为替代性的执行指令，而不是简单追加文本。
- UI 在等待 Kimi 回复时显示用户消息和总管 typing indicator；SSE 继续实时呈现任务进度和工作流事件。

## v11 · 对话输入真正固定到主制作界面（2026-09-23）

- `制作对话流` 现在采用聊天式主界面布局：中央消息区独立滚动，底部总管输入框始终可见，不再被长任务时间线推到页面底部。
- `当前绑定要求` 与 `总管执行方案` 收进可展开区，默认不给聊天输入让位。
- 同一 job 的中间进度事件在主聊天流中自动折叠为最新一条，避免 15% / 18% / 24% / 30% / 94% 等大量技术卡片淹没用户与总管对话；完整执行记录仍保留在右侧日志。
- 进入 `制作对话流` 时 Canvas 会切换为 `flow-active` 布局，消息区滚动、输入区固定、右侧 Orchestrator 仅作为状态/记忆/审批辅助栏。

## v12 · Asset Manifest 再生成完整性修复（2026-09-23）

针对 asset_manifest 多次重生成时出现“补新增资产却把上一版已正确资产删除”的问题，本版增加程序级资产契约与合并机制：

- `script.asset_requirements` 现在是 Asset Manifest 的确定性基线契约；模型漏项时 Harness 自动补回为 CREATE（或在系列库存在完全 canonical_key 匹配时 REUSE）。
- asset_manifest 重生成采用单调合并：`剧本基线 < 上一版有效 manifest < 本轮模型输出`，本轮只增加/修改少量资产时不会删除上一版仍有效的道具、场景或角色。
- 只允许 `character / scene / prop` 三类资产进入下游；`costume` 不再作为独立资产类型，服装属于 Character metadata；`set/location` 统一映射为 `scene`。
- 每个 Asset Manifest 新增 `manifest_validation`：记录脚本基线数量、已覆盖数量、缺失 canonical keys、额外资产数量、类型计数与验证状态。
- 总管生成复盘会读取 `manifest_validation`，不得在 `missing_required_keys=[]` 时错误声称剧本基线资产缺失；额外资产仍可按明确绑定要求检查。
- Asset Manifest Prompt 强制重生成返回完整 manifest 而不是 patch，并要求先自检 `script.asset_requirements` 覆盖。

这意味着类似“上一版已有旅行袋和茶杯，本轮只补吊灯和床品”的修改，不会再因为 Kimi 重写整个 JSON 而把旅行袋/茶杯删除。
