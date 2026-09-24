# 爆款复制 Harness

这是一个部署在 F 盘的本地 AI 短片复制工作流。它复现目标工作区的核心行为：17 个结构化阶段、15 个有明确边界的 Agent、二次确认、级联修改、角色/场景/道具参考图、单镜预览、批量视频、最终成片和交付质检。

它不是 OiiOii 私有后端的复制品，也不会读取或复用 OiiOii 的登录凭据、积分或私有 RPC。默认 `mock` provider 能在没有 API Key 的情况下完整演示工作流。

## 快速启动（Windows PowerShell）

```powershell
Set-Location F:\oiioii-harness
.\run.ps1
```

打开 `http://127.0.0.1:8788`。

也可以手动启动：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8788
```

## 演示路径

1. 新建工作区。
2. 可通过 `POST /api/workspaces/{id}/uploads` 上传原型视频；然后点击“生成素材分析”和“生成剧本”。
3. 在右侧通过 `script_approved` 二次确认。
4. 依次生成角色、场景背景、道具和 Seedream 参考图，确认 `assets_approved` 后生成分镜。
5. 分别生成对白/口型计划与逐镜音效计划；连续性 QA 完成且无阻断项后，确认 `storyboard_approved`。
6. 生成 Seedance 单镜预览；确认 `preview_approved` 后才批量生成视频。
7. 生成全片配乐方案，由本地 FFmpeg 拼接成片，再由 FFprobe 执行交付质检。

所有产物写入 SQLite。修改上游产物时调用：

```http
PUT /api/workspaces/{workspace_id}/artifacts/{kind}
Content-Type: application/json

{"content": {"...": "..."}, "cascade": true}
```

下游产物会变为 `stale`，对应审批重置为 `pending`。

## 已接入：Kimi + Seedream + Seedance + FFmpeg

复制 `.env.example` 为 `.env`，在启动前把值注入环境：

```powershell
$env:HARNESS_PROVIDER = 'kimi-seedance'
$env:OPENAI_COMPAT_API_KEY = '<your key>'
$env:OPENAI_COMPAT_BASE_URL = 'https://api.moonshot.cn/v1'
$env:OPENAI_COMPAT_MODEL = 'kimi-k2.6'
$env:OPENAI_COMPAT_CONNECT_TIMEOUT_SECONDS = '30'
$env:OPENAI_COMPAT_READ_TIMEOUT_SECONDS = '900'
$env:SOURCE_BASE_FPS = '1.0'
$env:SOURCE_SCENE_THRESHOLD = '0.12'
$env:SOURCE_MAX_BASE_FRAMES = '1200'
$env:SOURCE_MAX_SCENE_FRAMES = '300'
$env:SOURCE_MAX_MODEL_FRAMES = '480'
$env:SOURCE_MAX_MODEL_IMAGE_BYTES = '48000000'
$env:SOURCE_FRAME_WIDTH = '768'
$env:SOURCE_SEGMENT_SECONDS = '30'
$env:SOURCE_SEGMENT_MAX_FRAMES = '12'
$env:SOURCE_SEGMENT_PARALLELISM = '4'
$env:SOURCE_MEDIA_CACHE_ENABLED = 'true'
$env:SOURCE_ANALYSIS_CACHE_ENABLED = 'true'
$env:IMAGE_API_KEY = '<your ARK_API_KEY>'
$env:IMAGE_API_BASE_URL = 'https://ark.cn-beijing.volces.com/api/v3'
$env:IMAGE_MODEL = 'doubao-seedream-5-0-pro-260628'
$env:VIDEO_API_KEY = '<your ARK_API_KEY>'
$env:VIDEO_API_BASE_URL = 'https://ark.cn-beijing.volces.com/api/v3'
$env:VIDEO_MODEL = 'doubao-seedance-2-5-260628'
$env:VIDEO_FALLBACK_MODEL = 'doubao-seedance-2-0-260128'
$env:FFMPEG_PATH = 'F:\oiioii-harness\tools\ffmpeg-release\<build>\bin\ffmpeg.exe'
.\run.ps1
```

Kimi 负责分析、剧本、三类资产、分镜、对白/口型、逐镜音效、连续性 QA 和全片配乐方案；Seedream 5.0 Pro 生成角色、场景和道具参考图；Seedance 2.5 负责法语对白、音画同出的单镜预览与批量镜头；本地 FFmpeg 统一编码并拼接最终成片，FFprobe 做交付质检。所有远端媒体会立即下载到 `data/outputs/`，再通过 `/media/` 预览。

素材分析现在使用“本地高密度取证 + 30 秒分段并行分析”：默认先按 `SOURCE_BASE_FPS=1.0` 每秒至少抽取 1 张基础帧，再用 FFmpeg scene score 补充镜头切换帧；随后按 `SOURCE_SEGMENT_SECONDS=30` 切成逻辑时间段，每段最多选择 `SOURCE_SEGMENT_MAX_FRAMES=12` 张代表帧，以 Base64 `image_url` 发送给 Kimi K2.6。默认最多 `SOURCE_SEGMENT_PARALLELISM=4` 个片段并行，所有片段完成后再用一次纯文本请求汇总为全局素材分析，因此不会再把数百张图片塞进一个请求。

素材处理与 Kimi 分段结果都支持持久缓存。`SOURCE_MEDIA_CACHE_ENABLED=true` 时，视频 SHA256 与抽帧配置不变会直接复用已生成的 FFmpeg 帧和音轨；`SOURCE_ANALYSIS_CACHE_ENABLED=true` 时，每个成功的 30 秒片段会立即单独落盘。若某个并行片段失败，其他成功片段仍保留，重新点击“生成素材分析”只会请求失败/缺失的片段；如果片段全部完成而最终汇总失败，下次只重跑汇总。相同输入再次分析时，分段和最终汇总都可直接命中缓存。若要强制重新调用 Kimi，可临时设置 `SOURCE_ANALYSIS_CACHE_ENABLED=false`；若要连 FFmpeg 抽帧也强制重做，再同时设置 `SOURCE_MEDIA_CACHE_ENABLED=false`。

Kimi K2.6 请求不再手动传 `temperature` / `top_p` 等固定采样参数，避免模型参数不兼容导致 HTTP 400；上游 API 返回的错误正文也会保留在任务错误信息里，便于直接定位。

Kimi 请求现在默认使用 `OPENAI_COMPAT_READ_TIMEOUT_SECONDS=900` 的读取超时，以减少长视频分段分析时的 `The read operation timed out`；若网络较慢或模型响应较久，可以继续调大。

另外，Harness 现在对“推进下一步 / 运行阶段”增加了工作区级安全阀：同一工作区如果已有 `queued` / `running` 任务，则不会再次创建新任务。重复点击同一阶段时会直接复用现有任务；若别的阶段任务仍在运行，则新阶段会被阻止，避免重复问询、重复扣费和并发串线。

当前音乐阶段会产出完整 BGM 方案，但渲染默认沿用 Seedance 的镜头原生音轨，并明确标记“未生成独立 BGM”。法语 TTS 是可选增强项，需要火山语音服务的 `TTS_APP_ID`、`TTS_ACCESS_TOKEN` 和 `TTS_VOICE_TYPE`；方舟 ARK Key 不能替代这三项。

为控制成本，批量生成会在首个失败镜头后停止，且默认最多提交 8 个镜头。测试使用 HTTP mock，不会产生火山方舟费用。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\doctor.py
```

## 文档

- [工作流分析](docs/WORKFLOW_ANALYSIS.md)
- [Agent 职责、交接与明确调试路径](docs/AGENT_ARCHITECTURE_AND_DEBUG.md)
- [缺失 API 清单](docs/MISSING_APIS.md)

## 目录

```text
app/
  agents.py               15 个 Agent 的机器可读职责合同与阶段归属
  main.py                 FastAPI 与 HTTP API
  orchestrator.py         阶段、依赖、审批、级联失效
  db.py                   SQLite 状态与事件日志
  providers/              Kimi、Seedream、Seedance、FFmpeg 与 mock 适配器
static/                   本地工作区界面
tests/                    状态流与级联修改测试
docs/                     观察结论与 API 缺口
scripts/doctor.py         环境诊断
```


## 任务实时刷新、工作区清理与剧本输出

- 任务启动后，前端会立即把 `queued/running` job 插入任务列表，并约每 `1.5s` 自动轮询一次；不再需要手工刷新页面才能看到进度变化。
- 素材分析阶段会把“读取视频 / 分段分析 x/y / 全局汇总 / 保存产物”等进度写回 job，界面可实时显示百分比和当前步骤。
- 同一工作区仍保留全局安全阀：已有任务运行时不会重复创建新任务。
- 工具栏保留“清理任务记录”，并把“清空工作区”改成“清理当前区域”：可从剧本、素材分析、角色、分镜等任一节点开始清理该节点及其依赖下游，同时严格保留上游节点与源素材。产物卡片右上角也提供“清理此节点”。
- 服务重启时会自动把上一次遗留的 `queued/running` 任务标记为“中断”，避免安全阀被僵尸任务永久占用。
- 上传同一个视频会按 SHA256 去重；旧工作区中历史重复 source 条目也会在媒体处理层去重，避免同一 259 秒视频被重复切成 36 段并重复请求 Kimi。
- 编剧 Prompt 现在要求输出生产级剧本：`beats` 只是结构梗概，`scenes` 必须包含 `scene_id / heading / source_time_range / objective / summary / action / dialogue / ending_hook`。下游发送给编剧的上下文会剔除 `segment_analyses/source_media/analysis_plan` 等内部大字段，避免重复证据污染剧本。
- 剧本页会显示场景动作、对白和场尾钩子，并可展开完整 JSON；不再只显示五条场景摘要。
- 对剧本/资产/分镜/预览点击“退回”后，“下一步”会变成“重新生成该阶段”，而不是再次只要求确认。

## 总管需求控制层（v4）

Harness 现在允许在**每一个可执行节点之前**输入自然语言要求。右侧“总管沟通 / 本步要求”会跟随下一阶段自动切换，例如素材分析、剧本、角色、场景、参考图、分镜、对白、音效、预览、批量视频、配乐、合成与交付 QA。

执行链路变为：

```text
用户本步要求 + 项目长期记忆 + 工作区设置 + 上游产物
                    ↓
            Kimi 艺术总监 / Orchestrator
                    ↓
    prompt_addendum + acceptance_criteria
        + 安全白名单 parameter_overrides
                    ↓
             当前阶段 Agent / Provider
                    ↓
          artifact._execution 留痕
```

可以先点击“让总管解析”查看总管对要求的理解，也可以直接执行；直接执行时后台仍会先做同样的总管规划。如果输入、项目记忆、工作区设置和上游 revision 都没有变化，会复用已保存的总管计划，避免无意义的重复规划调用。

项目记忆保存在 SQLite 中，适合写跨阶段长期规则，例如：

```text
目标市场：法国。
剧情结构、人物功能、核心冲突和反转必须保留。
所有对白使用自然法国法语，不做中文逐字翻译。
tu/vous 关系保持一致。
不添加原片没有证据支持的超自然设定。
视觉风格为现代法国电视剧写实质感。
```

阶段级要求只影响该节点，例如剧本阶段可输入“完成法国本地化，法国人名/地点/制度/欧元/家庭称谓都要自然，但保留原剧情钩子”；分镜阶段可输入“控制 15 镜，冲突段增加反应特写”；批量视频阶段可输入“先生成 4 镜，720p，9:16，保留法语音轨”。

总管参数修改采用严格白名单，不会把任意自然语言直接拼成上游 API 参数。当前支持分析分段参数、分镜数量意图、参考图数量、Seedance 分辨率/画幅/音频/批量上限，以及 FFmpeg CRF/音频码率等已实现参数。


## v5：区域清理 + 强绑定总管对话 + 生成后复盘

### 区域清理不再等于删除整个项目

主界面的“清空工作区”已改成“清理当前区域”，每个非 `source` 产物卡片也有“清理此节点”。清理规则按真实依赖图计算：

- 清理 `script`：删除剧本和所有依赖剧本的下游产物，但保留 `source` 与 `analysis`；
- 清理 `analysis`：删除素材分析及其所有下游产物，但保留原始 `source` 上传素材；
- 清理 `characters`：删除角色及依赖角色的 `reference_images/storyboard/...`，但保留同级的 `scenes/props`；
- 审批会按受影响节点自动回到 `pending`；
- 受影响阶段的旧总管执行计划会失效，但用户已经保存的阶段要求和项目记忆会保留；
- 清理 `analysis` 时会删除 Kimi 分析缓存以确保真正重新分析，但保留 FFmpeg 抽帧/音频缓存，避免重复做本地媒体处理。

### 用户对话强制绑定到下一次执行

生成节点不再允许绕过总管直接启动。点击“与总管确认并生成…”时，Harness 会先强制完成一次总管确认：

```text
用户最新消息 + 当前有效要求 + 项目记忆 + 最近对话 + 当前/上游产物
                              ↓
                         Kimi 总管回应
                              ↓
          consolidated effective_instruction + execution_directive
                              ↓
                    plan_input_hash 绑定本次任务
                              ↓
                         下游 Agent / Provider
```

HTTP `run/{stage}` 也会验证绑定的 `director_plan_hash`。上游 revision、项目记忆或阶段要求变化后，旧计划会自动失效，必须重新和总管确认，避免 UI 中的对话与真正执行的 Prompt 脱节。

### 总管现在是持续对话，而不是一次性 Prompt 编译器

右侧输入框改成真正的“给总管发消息”：每一条用户消息都会调用总管模型并获得回复。总管会结合：

- 用户本轮要求；
- 该阶段之前的有效要求；
- 最近沟通历史；
- 当前已生成产物与上游产物；
- 工作区长期记忆；
- 下一节点 Agent 的职责与参数白名单。

总管输出除了 `prompt_addendum/parameter_overrides/acceptance_criteria`，还会维护一个去冲突后的 `effective_instruction`。当用户说“不要之前那个方案，改成……”，总管应修改有效要求，而不是机械把互相冲突的提示词继续叠加。

### 每次生成后 Kimi 自动复盘

每个阶段产物保存后，Kimi 总管会再读取“实际生成结果 + 本次用户要求 + 执行指令 + 项目记忆”，生成一条生产复盘消息，说明：

- 哪些要求已经满足；
- 有哪些偏差/不确定性；
- 是否建议批准、重生成、调整下一阶段或等待用户决定；
- 下一阶段值得重点关注什么。

复盘失败不会把已经成功生成的产物判为失败；它只会记录为总管复盘警告。所有复盘和用户对话继续写入 SQLite `guidance_messages`，重启后仍可查看。

### UI

- 新增 `素材` 与 `素材分析` 独立 Tab；
- 非源素材产物卡片增加“清理此节点”；
- “清理当前区域”只对明确节点执行，不再一键删整个项目；
- 右侧显示“当前已绑定要求”；聊天输入框发送后清空，完整对话保留在“沟通记录”；
- 运行按钮显示“与总管确认并生成…”，明确表明总管确认是任务启动的一部分；
- 生成完成后，“沟通记录”会自动出现“生成复盘”。

### 验证

```text
48 passed
Python compileall passed
Node app.js syntax check passed
```

### 源视频上传与工作区删除

v6 起，源视频上传会显示实时百分比/传输大小，并先写入临时文件，完整上传后才替换正式文件。上传后的源视频可以直接在“素材”页播放预览。若工作区已有运行中任务，上传会被阻止以避免素材版本在任务执行中发生变化。

顶部“删除当前工作区”会永久删除该工作区的数据库状态、上传文件、抽帧/分析缓存和生成产物；运行中任务或上传中的工作区不能删除。

### 前端启动与上传排查（v7）

v7 开始，左侧工作区列表会独立于 Provider 健康检查加载；即使 Kimi/Seedream 健康状态暂时读取失败，也不会把已有工作区隐藏。左侧“工作区”标题旁提供“刷新”按钮。

“上传源视频”使用浏览器原生 file input label，不再依赖 `input.click()`。如果刚覆盖源码，请重启 Harness；v7 的首页和静态 JS/CSS 默认禁止缓存，并带版本参数，通常不再需要反复手动清浏览器缓存。

## v8：总管复盘闸门、主题资产库与线性制作对话流

### 剧本与总管复盘

编剧 Prompt 进一步把“源片事实”和“本地化创作决策”分离：

- `source_fact_register` 记录 confirmed / inferred / unknown，避免把素材分析中的候选人物关系直接升级为事实；
- `adaptation_decisions` 单独记录法国化过程中新增的人名、机构、阶层符号等创作决策与理由；
- `episode_sections` 显式处理疑似集数/段落边界，禁止把一分钟以上的续集内容塞进一个笼统的 `HOOK` 场；
- 每场戏增加 `production_scope`, `source_fact_basis`, `adaptation_decisions`；
- `asset_requirements` 为后续角色、场景、道具提供稳定 canonical ID 与连续性锁定要求；
- 对白要求短、自然、可说出口，降低“中文伦理剧直译成法语”的书面化和说教感。

每个阶段生成后，总管复盘会持久化到 `stage_reviews`。当复盘建议 `regenerate_current` 或 `wait_for_user` 时，UI 默认不再直接建议批准，而是建议重生成或继续对话。用户仍然可以明确选择“仍然确认”覆盖总管建议，保留人工最终决定权。

### 跨项目主题资产库

新增全局主题资产库，独立于单个 Workspace，可跨项目复用。资产库可以按主题与类型建立，例如：

- `France / Famille bourgeoise contemporaine / Characters`
- `France / Hôtel particulier / Scenes`
- `France / Accessoires familiaux / Props`

支持 `character / scene / prop / reference / mixed` 范围。Workspace 只“挂载”所需资产库，删除 Workspace 不会删除共享资产。

用户可以：

- 手动新建不同主题的角色/场景/道具资料库；
- 上传单个图片、文档或 ZIP 资源包；
- 在上传时用自然语言告诉 Kimi“这批资源属于哪个主题、应该放在哪个库”；
- 让 Kimi 对图片资源进行视觉理解并写入描述、标签和 continuity notes；
- 把当前项目生成的 `characters/scenes/props/reference_images` 回存到共享资产库；
- 在另一个 Workspace 挂载相同资产库，让下游 Agent 输出 `library_asset_id/reuse_decision/continuity_lock` 并优先复用已有设计。

当前 Seedream 对共享资产的复用以“结构化资产描述 + continuity metadata + prompt anchor”为主。上传图片会被保存并由 Kimi 理解，但是否能把某张本地图片作为云端 Seedream/Seedance 的原生 image-conditioning 输入，仍取决于对应 Provider 是否支持可访问的参考图 URL/上传接口；因此目前不能承诺像素级或人脸级完全一致，只能保证工作流层面的资产 ID、描述与连续性约束稳定。

### 线性制作对话流

中央画布新增“制作对话流”，默认把以下内容按时间顺序串成一条制作时间线：

- 用户发给总管的要求；
- Kimi 总管回复与执行计划；
- Agent / Provider 启动；
- Job 实时进度；
- Artifact 更新；
- 总管生成后复盘；
- 用户审批/退回；
- 资产库挂载、导入等事件。

后端提供 Workspace SSE 流，前端通过 `EventSource` 自动刷新，因此不再需要在右侧对话、中央 Agent 进度和任务列表之间来回手动刷新。发送总管消息或启动生成节点时，中央区域会自动切换到“制作对话流”。

### 技术参数权限

总管可以建议更细分段、更高并发等技术调整，但只有用户本轮消息明确提出具体技术参数时才允许实际写入 `parameter_overrides`。例如“分析详细一点”不能把 30 秒分段偷偷改成 15 秒；“改成 15 秒一段、每段 8 帧、并行 3 路”才会生效。每次 override 只作用于当前任务，不再污染 Provider 的默认配置。

### 验证

```text
58 passed
Python compileall passed
Node app.js syntax check passed
```

## v9：Series Bible、Asset Manifest 与跨集自动复用

v9 将资产库主路径从“用户上传/手工归档”改为“本集生产并批准 → 自动沉淀 → 下一集自动检索与复用”。用户上传仍保留，但只作为补充来源。

### Series / Episode

新建工作区时可以选择所属系列（Series）与集数标识。一个系列会自动维护三类长期资产库：

- Characters：系列固定人物与身份/服装变体；
- Scenes：系列固定地点与时间/灯光变体；
- Props：系列关键道具与状态变体。

旧工作区也可以在“主题资产库”页绑定到一个 Series。绑定后，Series 的自动资产库会挂载到当前 Workspace，并进入总管和下游 Agent 上下文。

### Asset Manifest

剧本确认后新增 `asset_manifest` 阶段：

```text
approved script
  ↓
asset_manifest
  ↓
REUSE / VARIANT / CREATE
  ↓
characters / scenes / props
```

每个 recurring character、location、story-critical prop 都会被转换为稳定 `manifest_id` / `canonical_key`。总管会优先检索同系列已批准资产：

- `REUSE`：直接复用 canonical asset；
- `VARIANT`：保留 canonical identity，只改变服装、时间、道具开合等有界状态；
- `CREATE`：没有合适历史资产时，后续 Agent 自动创建新规格，不要求用户先上传。

Harness 会验证 `library_asset_id` 必须真实存在。模型如果引用了不存在的资产 ID，会自动降级为 `CREATE`，避免伪造复用关系。

### 自动沉淀

当 `assets_approved` 被人工确认后，当前集已经批准的角色、场景、道具与参考图会自动写回 Series Bible：

- 新身份 → 新 canonical asset；
- 已有身份继续使用 → 更新 usage history；
- 同一身份的新服装/时间/状态 → 建立 variant，不覆盖 canonical base；
- 参考图会成为该 canonical/variant 的视觉锚点。

因此 EP02、EP03 新建 Workspace 时只要选择同一个 Series，就能读取 EP01 已批准资源。缺少的资产继续由角色/场景/道具 Agent 和 Seedream 自动生成。

### 推荐连续剧流程

```text
EP01 script
  ↓
asset_manifest
  ↓
CREATE missing assets
  ↓
reference images
  ↓
assets_approved
  ↓
automatically publish to Series Bible
  ↓
EP02 selects same Series
  ↓
asset_manifest retrieves EP01 assets
  ↓
REUSE / VARIANT / CREATE only what is needed
```
