# 爆款复制 Harness：Agent 职责、交接与调试路径

更新日期：2026-09-20

## 1. 结论

当前 harness 已把 OiiOii 页面可见的创作链拆成 15 个职责单元。每个可执行阶段恰好只有一个主责 Agent；艺术总监只负责调度、审批和返工范围，不代替编剧、分镜、音效或渲染工作。

这次最重要的边界调整是：

- “场景背景”是视觉环境设计；“全片配乐”是音乐设计，二者分开。
- 分镜师只定义镜头、摄影、调度和资产绑定，不再同时写对白与音效。
- 对白/口型规划和逐镜环境音/Foley 是两份独立产物。
- 连续性 QA 只能报告 `pass / warn / fail`、证据和返工责任人，不能偷偷改稿。
- 视频生成师只能消费已审批的分镜、对白、音效和参考图，必须先做一镜预览。
- 配乐总监负责全片 BGM 方案；剪辑师负责拼接与混音；交付 QA 只读检查母版。

机器可读合同位于 `app/agents.py`，浏览器的“Agent 职责”页签和 `GET /api/meta` 使用同一份数据，避免文档与运行时分叉。

## 2. 对 OiiOii 工作流的只读复核

本次仅观察用户已登录的项目页，没有点击生成、批量任务、发布或任何可能消耗积分的操作。

页面直接可见的主要角色包括：艺术总监、编剧、角色设计师、场景设计师、道具设计师、分镜师和音效总监。执行记录还显示：

- 艺术总监负责阶段交接与最终合成。
- 分镜师内部先提取分镜设定，再修改逐镜提示词，最后调用视频生成助理。
- 单个分镜同时显示视频提示词、音频提示词和对白提示词。
- 音效总监在镜头视频确认后接手，生成背景音乐提示词，再进入音乐与最终合成。
- 角色、场景、道具有独立资产卡、预览、替换和资产库能力。
- 正式批量生成前会先生成一个镜头预览，让用户确认风格、身份一致性和成本。
- 上游修改会询问是否级联更新下游分镜图、视频等产物。

据此，本 harness 保留 OiiOii 的阶段和审批思想，同时把页面上容易混在一个卡片里的“分镜 / 对白 / 音效”拆成可单独定位故障的三份合同。

## 3. 执行图

```text
上传素材
  -> 素材分析师
  -> 编剧
  -> [审批：script_approved]
  -> 角色设计师 ─┐
     场景背景设计师 ├-> 视觉资产渲染师
     道具设计师 ───┘
  -> [审批：assets_approved]
  -> 分镜师
  -> 对白与口型规划师 ─┐
     逐镜音效师 ───────┤
  -> 连续性与生产 QA ──┘
  -> [审批：storyboard_approved]
  -> 镜头视频生成师：只生成 1 个预览镜头
  -> [审批：preview_approved]
  -> 镜头视频生成师：批量镜头，首错即停
  -> 全片配乐总监：BGM 方案 -> 当前渲染策略
  -> 剪辑与合成师
  -> 交付质检员
```

审批发生在 QA 之后。也就是说，用户确认的是“分镜 + 对白 + 音效 + QA 报告”的完整包，不是尚未检查的裸分镜。

## 4. Agent 职责矩阵

| Agent | 主责阶段 | 只负责 | 明确禁止 |
|---|---|---|---|
| 艺术总监 / 制片调度 | 无媒体阶段 | 依赖、审批、下一步、级联失效 | 写剧本、改分镜、直接生成媒体 |
| 素材分析师 | `source`, `analysis` | 素材清单、技术摘要、证据时间线、不确定项 | 本地化改写、臆造画面事实 |
| 编剧 / 本地化编剧 | `script` | 剧情、台词、场次、叙事钩子 | 外观设计、摄影拆镜、媒体生成 |
| 角色设计师 | `characters` | 外观、服装、表演、声音锚点 | 场景、道具、摄影、图片渲染 |
| 场景 / 视觉背景设计师 | `scenes` | 地点、空间、时代地域、天气灯光 | BGM、角色、道具、对白 |
| 道具设计师 | `props` | 剧情关键实体与状态连续性 | 无关装饰、场景或角色设计 |
| 视觉资产渲染师 | `reference_images` | 按已批准规格渲染参考图 | 重写规格、生成分镜视频 |
| 分镜师 | `storyboard` | 镜头边界、时长、机位、调度、资产绑定、视觉提示词 | 核心改稿、对白时间、Foley、BGM、视频生成 |
| 对白与口型规划师 | `dialogue_plan` | 说话人、精确台词、语言、时间窗、字幕、口型 | 环境音、BGM、画面构图、语音合成 |
| 逐镜音效师 | `sound_plan` | 环境音、Foley、入点、ducking、负向音频 | 对白、全片 BGM、改画面或剧情 |
| 连续性与生产 QA | `review` | 只读检查、证据、阻断等级、返工责任人 | 修改上游、生成媒体、替用户审批 |
| 镜头视频生成师 | `preview`, `batch_video` | 组装批准输入、提交/轮询/落盘、首错即停 | 改稿、改资产、添加台词、越过预览审批 |
| 全片配乐总监 | `music_plan`, `music` | BGM cue sheet、情绪弧线、ducking、渲染策略 | Foley、对白、镜头修改、最终混音 |
| 剪辑与合成师 | `compose` | 顺序、拼接、混音、统一编码、母版 | 改故事、重生成镜头、外部发布 |
| 交付质检员 | `delivery_qa` | 文件、流、时长、画幅、镜头数检查 | 覆盖母版、发布、审美代验收 |

完整的输入、输出字段、Provider 和调试断言以 `GET /api/meta` 返回的 `agents` 为准。

## 5. 阶段合同与验收点

| 阶段 | 必须输入 | 最小输出 | 单阶段通过条件 |
|---|---|---|---|
| `source` | brief、上传文件 | 文件名、大小、SHA256、本地路径 | 文件存在；哈希已保存；未把密钥写入产物 |
| `analysis` | source | 技术摘要、证据时间线、限制、问题 | 画面事实可追溯；不确定内容明确标注 |
| `script` | analysis | beats、scenes、dialogue、continuity_rules | 仅使用当前项目设定；语言和总时长匹配目标 |
| `characters` | 已批剧本 | 稳定角色 ID 与外观/服装/声音锚点 | 主角齐全；无场景/道具字段越界 |
| `scenes` | 已批剧本 | 稳定场景 ID、布局、灯光、时代地域 | 每个剧本地点可引用；背景含义仅指视觉环境 |
| `props` | 已批剧本 | 关键道具 ID、材质、状态、使用场次 | 每件道具有剧情用途；状态变化可追溯 |
| `reference_images` | 三类资产 | source_kind/source_id/url/status | 每张图回指一个已批准资产；结果已落盘 |
| `storyboard` | 全部资产和目标镜头数 | 连续 shot index、时长、资产绑定、视觉提示词 | 资产 ID 均存在；镜头数、总时长和叙事目的合理 |
| `dialogue_plan` | 分镜、角色、剧本 | 逐镜说话人、原文、语言、时间窗、字幕 | 与 shots 一一对齐；台词可在镜头内说完 |
| `sound_plan` | 分镜、场景 | 逐镜 ambience/Foley/cues/ducking | 与 shots 一一对齐；不含对白和 BGM 创作 |
| `review` | 分镜、对白、音效、资产 | checks、blocking_failures、owner | 每个 fail 有证据和责任 Agent；阻断项为零才能预览 |
| `preview` | 已批完整分镜包 | 仅一个视频任务与本地文件 | 只提交第一镜；身份、口型、声音和费用可人工验收 |
| `batch_video` | 已批 preview | 逐镜任务、状态、本地文件 | 不超过批量上限；首个失败后不继续扣费 |
| `music_plan` | 成功镜头、剧本、对白 | BGM cue sheet 与 ducking | cue 不越界；对白处有避让 |
| `music` | music_plan | 渲染模式和真实状态 | 没有音乐 API 时明确写“未渲染独立 BGM” |
| `compose` | 成功镜头、音乐策略 | final.mp4、编码信息、输入镜头数 | FFmpeg 返回 0；文件存在 |
| `delivery_qa` | final.mp4、批量清单、目标画幅 | probe、checks、blocking_failures | 有视频/音频流；时长 > 0；画幅和镜头数一致 |

## 6. 明确调试路径

### 路径 A：零计费验证全部编排

先临时使用 Mock，不调用 Kimi、Seedream 或 Seedance：

```powershell
Set-Location F:\oiioii-harness
$env:HARNESS_PROVIDER = 'mock'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8788
```

浏览器打开 `http://127.0.0.1:8788`，新建工作区后逐次点击“推进下一步”。在“Agent 职责”页签确认每个阶段显示唯一主责 Agent。

期望结果：

1. 17 个阶段都能完成。
2. 顺序中出现 4 次审批：剧本、资产、完整分镜包、单镜预览。
3. 最后产物是 `delivery_qa`，状态为 `pass`。
4. 修改 `script` 并令 `cascade=true` 后，下游产物全部变为 `stale`，审批回到 `pending`。

### 路径 B：先查配置，不生成媒体

保持 `.env` 中的真实 key，但只运行诊断和只读端点：

```powershell
Set-Location F:\oiioii-harness
.\.venv\Scripts\python.exe scripts\doctor.py
Invoke-RestMethod http://127.0.0.1:8788/api/health | ConvertTo-Json -Depth 6
Invoke-RestMethod http://127.0.0.1:8788/api/meta | ConvertTo-Json -Depth 8
```

`/api/health` 只返回能力布尔值，不返回 secret。`/api/meta` 应返回 17 个 stages、15 个 agents，以及每个 stage 对应的唯一 `stage_agents` 映射。

### 路径 C：真实 Provider 按成本从低到高

1. `source`：上传一个短样片，检查 `data/uploads/{workspace_id}`。
2. `analysis`：先由本地 FFmpeg 按默认 1 fps + scene cut 自适应抽帧，再按默认 30 秒切段、每段最多 12 张代表帧，最多 4 段并行发送给 Kimi；每个成功段立即缓存，失败后重跑只补缺失段，最后再做纯文本全局汇总。本地路径不会外发。
3. `script`：只调用 Kimi；人工审批 `script_approved`。
4. `characters / scenes / props`：只调用 Kimi；逐项检查 ID 和职责边界。
5. `reference_images`：首次产生 Seedream 费用；先把 `IMAGE_MAX_ASSETS` 调小进行烟雾测试，再审批 `assets_approved`。
6. `storyboard / dialogue_plan / sound_plan / review`：只调用 Kimi；确认 `blocking_failures=[]` 后审批 `storyboard_approved`。
7. `preview`：首次产生 Seedance 视频费用，只生成一个镜头。人工查看法语发音、口型、角色身份、衣着、画幅和费用，再审批 `preview_approved`。
8. `batch_video`：逐镜提交，首个失败即停。确认 `requested`、`completed` 和本地文件数一致。
9. `music_plan / music`：生成方案；当前没有独立音乐 API 时，`music.mode` 必须明确为 `seedance_native_audio_without_separate_bgm`。
10. `compose`：本地 FFmpeg 拼接；不产生模型费用。
11. `delivery_qa`：本地 FFprobe 检查；不产生模型费用。

### 路径 D：逐阶段 HTTP 调试

创建隔离工作区：

```powershell
$payload = @{
  title = 'Agent debug EP01'
  brief = '现代法国，9:16，法语对白；仅使用当前上传素材。'
  aspect_ratio = '9:16'
  target_language = 'French'
  storyboard_count = 6
} | ConvertTo-Json
$detail = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8788/api/workspaces -ContentType 'application/json' -Body $payload
$wid = $detail.workspace.id
```

启动一个阶段并轮询：

```powershell
$job = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8788/api/workspaces/$wid/run/analysis"
do {
  Start-Sleep -Seconds 2
  $state = Invoke-RestMethod "http://127.0.0.1:8788/api/jobs/$($job.id)"
  $state | Select-Object status, progress, message
} while ($state.status -in @('queued', 'running'))
```

查看当前产物、审批、任务和建议下一步：

```powershell
Invoke-RestMethod "http://127.0.0.1:8788/api/workspaces/$wid" | ConvertTo-Json -Depth 12
```

手动批准一个 gate：

```powershell
$approval = @{ status = 'approved'; note = '人工检查通过' } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:8788/api/workspaces/$wid/approvals/script_approved" -ContentType 'application/json' -Body $approval
```

四个合法 gate：`script_approved`、`assets_approved`、`storyboard_approved`、`preview_approved`。

## 7. 故障定位顺序

| 现象 | 首查位置 | 主责 Agent | 不应先改什么 |
|---|---|---|---|
| 分析声称看到了未上传内容 | `analysis.evidence_timeline`、`evidence_limitations` | 素材分析师 | 不要先改剧本 |
| 混入古代/其他项目设定 | `script.adaptation_notes` 与 brief | 编剧 | 不要靠负面视频提示词掩盖 |
| 人物跨镜变脸/换衣 | `characters`、参考图、shot asset_bindings | 角色设计师；必要时参考图渲染师 | 不要让分镜师重写人物外观 |
| 背景年代或地域错误 | `scenes` 与 `storyboard.asset_bindings.scene` | 场景背景设计师 | 不要改 BGM |
| 道具突然消失或状态错误 | `props.state/used_in` 与 shot bindings | 道具设计师 | 不要让音效师补救 |
| 镜头叙事断裂 | `storyboard.story_beat` 和相邻镜头 | 分镜师 | 不要让视频生成师改剧情 |
| 法语太长、嘴型跟不上 | `dialogue_plan.timing/text/lip_sync` | 对白与口型规划师 | 不要把台词塞进 sound_plan |
| 环境音包含人声或配乐 | `sound_plan` | 逐镜音效师 | 不要改 dialogue_plan |
| QA 报 fail 仍可预览 | `review.blocking_failures`、gate 状态 | 艺术总监 / 编排器 | 不要在 QA 内自动修复 |
| 单镜正常、批量首镜失败 | `batch_video.items[0].error`、模型 fallback | 镜头视频生成师 | 不要继续提交后续镜头 |
| 成片没有独立 BGM | `music.mode` | 全片配乐总监 | 不要声称 Seedance 原生环境音等于完整 BGM |
| 合成失败 | `compose` job message、FFmpeg stderr | 剪辑与合成师 | 不要回滚剧本 |
| 成片无音轨/画幅错误 | `delivery_qa.checks` 与 `probe` | 交付质检员 | 不要由 QA 覆盖母版 |

## 8. 当前 API 边界

当前核心链路已有：Kimi 多模态素材分析与文本规划、Seedream 参考图、Seedance 2.5/2.0 fallback 视频、FFmpeg/FFprobe 本地处理。素材分析默认 1 fps 基础抽帧并补充 scene cut 帧，再以 30 秒片段并行送入 Kimi；FFmpeg 预处理、片段结果与最终汇总均支持缓存/续跑。

尚未补齐但不阻塞结构调试的能力：

- 独立法语 TTS / 固定角色音色：需要火山语音服务的 `TTS_APP_ID`、`TTS_ACCESS_TOKEN`、`TTS_VOICE_TYPE`。方舟 ARK Key 不能替代。
- 源视频 ASR：需要单独 ASR 凭据；当前可依赖 brief 与技术摘要。
- 独立 BGM 生成：当前只有 BGM 方案和 Seedance 镜头原生音轨；如要完整复刻 OiiOii 的音乐阶段，需要接入 Seed Audio/Oii Music 或其他音乐 API。
- 发布到 Oii TV：属于第三方私有平台能力，不在本地 harness 范围。

Seedance 可以在单镜内生成法语对白并尝试同步嘴型，但跨镜固定音色和可复现发音仍建议后续接独立 TTS，再由合成阶段混音。

## 9. 回归验证

```powershell
Set-Location F:\oiioii-harness
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\doctor.py
```

自动测试至少覆盖：全 Mock 流程、级联失效、每阶段唯一 Agent 合同、QA 有阻断错误时禁止预览、Kimi 脱敏、Seedream 请求、Seedance 请求和本地素材处理。

## v4 Control Plane: User Guidance -> Art Director -> Stage Agent

Every runnable stage now has a control-plane pass before the execution provider. The Art Director receives persistent project memory, the current stage instruction, bounded upstream context and an explicit parameter whitelist. It returns a structured `execution_directive`; stage agents receive that directive in their context. Media providers consume only whitelisted overrides. The directive is persisted in `stage_guidance` and copied into each artifact as `_execution` for auditability.

The plan cache fingerprint includes workspace settings, brief, project memory, user instruction and all upstream artifact revisions. Editing any of these invalidates the saved director plan automatically.

## v5: Director conversation loop and stage-scoped reset

The Art Director is now a persistent control loop rather than a one-shot preprocessor:

```text
user turn -> director reply/effective_instruction -> bound plan hash -> stage execution
          -> actual artifact -> director post-generation review -> next user turn
```

A run request is accepted only when its `director_plan_hash` matches the current workspace memory, stage instruction and artifact revisions. Any upstream revision makes the prior plan stale.

Stage reset follows `REQUIRES` transitively. Resetting a stage deletes that artifact plus dependency descendants and resets affected gates, while preserving all upstream artifacts. `source` is protected from this reset operation. Resetting `analysis` also clears the Kimi analysis cache so a new analysis is actually requested, while preserving FFmpeg source-frame/audio cache.

## v8：总管—资产库—Agent 的连续制作闭环

现在每个 Workspace 除了项目记忆和阶段要求，还可以挂载跨项目共享的主题资产库。总管规划阶段会同时读取：用户最新消息、长期记忆、当前/上游 Artifact、已挂载资产库和最近沟通历史。角色、场景、道具 Agent 应优先决定 `reuse_decision`，并通过 `library_asset_id` 绑定已存在资产；只有确实需要新设计时才创建新的 canonical asset。

生成结果完成后，总管必须进行 post-generation review。Review 与具体 Artifact revision 绑定，避免旧复盘污染新版本。`regenerate_current` / `wait_for_user` 默认阻止 UI 把“确认”作为建议下一步，但人工可以明确覆盖。

调试时建议按以下顺序检查：

1. “制作对话流”里用户消息是否出现；
2. 总管回复是否形成新的有效要求与 plan hash；
3. Job progress 是否持续通过 SSE 更新；
4. Artifact 是否写入 `_execution` 与资产库引用；
5. post-generation review 是否与最新 revision 一致；
6. 若人物/场景/道具漂移，检查对应 item 是否有 `canonical_key`, `library_asset_id`, `continuity_lock`，以及 Workspace 是否真的挂载了目标主题库。

## v9：Series Bible 与 Asset Manifest

剧本不直接绑定最终视觉资产。确认剧本后先运行 `asset_manifest`，把自然语言实体转换为稳定生产 ID，并与系列历史资产匹配。

```text
script_approved
  ↓
asset_manifest
  ├─ REUSE   → 绑定 existing library_asset_id
  ├─ VARIANT → 绑定 canonical base + variant requirements
  └─ CREATE  → 下游 Agent 自动设计新资产
  ↓
characters / scenes / props
  ↓
reference_images
  ↓
assets_approved
  ↓
auto-publish to Series Bible
```

调试资产漂移时优先检查：

1. Workspace 是否绑定正确 `series_id`；
2. `asset_manifest.items[]` 的 `canonical_key/decision/library_asset_id`；
3. REUSE/VARIANT 的 `library_asset_id` 是否真实存在；
4. characters/scenes/props 是否被标记 `manifest_enforced=true`；
5. `assets_approved` 后是否出现 `series.assets_published` 事件；
6. 下一集的 `asset_library_context` 是否包含上一集的 canonical asset 与 reference image。
