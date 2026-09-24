from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    stages: tuple[str, ...]
    purpose: str
    owns: tuple[str, ...]
    must_not: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    provider: str
    debug_checks: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        return {key: list(item) if isinstance(item, tuple) else item for key, item in value.items()}


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        "art_director",
        "艺术总监 / 制片调度",
        (),
        "维护依赖图、审批门、成本边界与返工范围，只做路由和验收，不代替下游创作。",
        ("阶段顺序", "审批状态", "级联失效", "下一步动作"),
        ("撰写剧本", "设计资产", "改写分镜", "直接生成媒体"),
        ("全部产物状态", "人工审批", "工作区约束"),
        ("任务分配", "阻塞原因", "审批请求"),
        "Orchestrator",
        ("next_actions 只有一个安全动作", "修改上游后下游变 stale", "未审批阶段不可越过"),
    ),
    AgentSpec(
        "source_analyst",
        "素材分析师",
        ("source", "analysis"),
        "保管上传素材并提取可验证的技术信息、剧情证据、节奏和不确定项。",
        ("素材清单", "技术摘要", "证据时间线", "未知项"),
        ("本地化改写", "补写画面事实", "决定角色造型", "生成镜头"),
        ("源视频", "制作 brief", "本地 FFprobe/1fps 基础抽帧 + 场景切换帧"),
        ("source_summary", "evidence_timeline", "constraints", "questions"),
        "Local FFmpeg + Kimi",
        ("时长/画幅/音轨可追溯", "未把未观察内容写成事实", "仅发送选中的 Base64 视觉帧，不发送本地路径"),
    ),
    AgentSpec(
        "screenwriter",
        "编剧 / 本地化编剧",
        ("script",),
        "把已确认的素材证据按用户要求做目标市场本地化，输出可直接进入角色/场景/分镜生产的完整剧本，而不是剧情摘要。",
        ("剧情结构", "目标市场本地化", "逐场动作与台词", "文化映射", "连续性规则", "叙事钩子"),
        ("设计角色外观", "设计场景美术", "拆镜头", "生成图片或视频"),
        ("素材分析", "目标语言", "市场/时代/时长约束"),
        ("title", "language", "target_market", "localization_map", "beats", "scenes", "continuity_rules", "adaptation_notes"),
        "Kimi",
        ("每场来自素材或明确标注改编", "目标市场的人名/地点/制度/称谓/货币/语用一致", "台词不是摘要", "不混入其他项目设定"),
    ),
    AgentSpec(
        "asset_resolver",
        "资产解析 / 系列连续性管理员",
        ("asset_manifest",),
        "把已批准剧本中的人物、地点和关键道具转换成稳定资产清单，并优先检索当前系列历史资产；无合适资产时明确标记 CREATE，由后续资产 Agent 自行生成。",
        ("稳定 manifest ID", "系列资产匹配", "REUSE/VARIANT/CREATE 决策", "连续性锁", "跨集资产引用"),
        ("改写剧情", "凭空把不同角色合并", "为不存在的资产伪造 library_asset_id", "直接生成图片或视频"),
        ("已确认剧本", "script.asset_requirements", "系列/主题资产库", "项目记忆"),
        ("items[].manifest_id", "items[].asset_type", "items[].canonical_key", "items[].decision", "items[].library_asset_id", "items[].continuity_lock"),
        "Kimi",
        ("每个 recurring asset 有稳定 ID", "已有系列资产优先复用", "缺失资产明确 CREATE 而不是阻塞", "VARIANT 明确保留项与变化项"),
    ),
    AgentSpec(
        "character_designer",
        "角色设计师",
        ("characters",),
        "建立可跨镜复用的角色圣经，包括外观、服装、表演和声音身份。",
        ("角色 ID", "外观", "服装", "表演锚点", "声音锚点"),
        ("编写新剧情", "设计场景或道具", "决定摄影", "渲染图片"),
        ("已确认剧本", "连续性规则"),
        ("items[].id", "items[].appearance", "items[].wardrobe", "items[].performance", "items[].voice", "items[].source_visual_traits", "items[].source_continuity_lock", "items[].continuity_lock", "items[].localized_visual_design", "items[].generation_prompt_en"),
        "Kimi",
        ("角色 ID 唯一", "每个主要角色有固定描述", "没有把场景描述塞进角色字段"),
    ),
    AgentSpec(
        "scene_designer",
        "场景 / 视觉背景设计师",
        ("scenes",),
        "定义镜头所需的地点、视觉背景、时间、天气、灯光和可复用空间锚点。",
        ("场景 ID", "空间布局", "时代地域", "光线天气", "视觉背景"),
        ("创作 BGM", "设计角色或道具", "写对白", "决定分镜"),
        ("已确认剧本", "时代与地域约束"),
        ("items[].id", "items[].location", "items[].layout", "items[].lighting", "items[].continuity", "items[].source_visual_traits", "items[].source_continuity_lock", "items[].continuity_lock", "items[].localized_visual_design", "items[].generation_prompt_en"),
        "Kimi",
        ("背景指视觉环境而非音乐", "所有地点属于当前项目时空", "场景可被分镜 ID 引用"),
    ),
    AgentSpec(
        "prop_designer",
        "道具设计师",
        ("props",),
        "仅定义推动剧情或需要跨镜一致的实体道具及其状态变化。",
        ("道具 ID", "材质尺寸", "初始状态", "跨镜状态", "使用场次"),
        ("填充无关装饰", "设计角色或场景", "编写动作结果", "渲染图片"),
        ("已确认剧本", "场次列表"),
        ("items[].id", "items[].description", "items[].state", "items[].used_in", "items[].source_visual_traits", "items[].source_continuity_lock", "items[].continuity_lock", "items[].localized_visual_design", "items[].generation_prompt_en"),
        "Kimi",
        ("每件道具有剧情用途", "状态变化有来源", "没有重复场景布景"),
    ),
    AgentSpec(
        "visual_asset_renderer",
        "视觉资产渲染师",
        ("reference_images",),
        "把已批准的角色、场景和道具规格渲染成参考图，不改写规格。",
        ("参考图任务", "模型参数", "本地/远端媒体引用"),
        ("修改剧本", "重新定义资产", "生成分镜视频", "自行增加人物"),
        ("角色圣经", "场景圣经", "道具圣经", "画风约束"),
        ("items[].source_kind", "items[].source_id", "items[].url", "items[].status"),
        "Seedream",
        ("每张图能回指 source_id", "生成数受上限约束", "结果已落盘或明确失败"),
    ),
    AgentSpec(
        "storyboard_director",
        "分镜师",
        ("storyboard",),
        "把剧本拆成可生成镜头，负责镜头边界、时长、机位、构图、调度和资产绑定。",
        ("镜头编号", "镜头时长", "摄影与调度", "资产 ID 绑定", "视觉提示词"),
        ("改写核心剧情", "设计独立音效", "创作 BGM", "生成视频"),
        ("剧本", "角色/场景/道具", "参考图", "目标分镜数"),
        ("shots[].index", "shots[].duration_seconds", "shots[].asset_bindings", "shots[].visual_prompt", "shots[].story_beat"),
        "Kimi",
        ("镜头编号连续", "资产引用存在", "总时长与剧本相符", "每镜只有一个明确叙事目的"),
    ),
    AgentSpec(
        "dialogue_voice_planner",
        "对白与口型规划师",
        ("dialogue_plan",),
        "逐镜确定说话人、精确台词、语言、时间窗、字幕和口型要求。",
        ("逐镜对白", "说话人", "语言", "时间窗", "字幕", "口型约束"),
        ("添加环境音", "创作 BGM", "修改镜头构图", "合成语音"),
        ("剧本台词", "分镜时长", "角色声音锚点", "目标语言"),
        ("items[].shot_index", "items[].speaker_id", "items[].text", "items[].language", "items[].timing", "items[].subtitle"),
        "Kimi; optional TTS later",
        ("台词时长不超过镜头窗口", "说话人引用存在", "法语拼写和字幕一致", "无对白镜头明确标 silent"),
    ),
    AgentSpec(
        "sound_designer",
        "逐镜音效师",
        ("sound_plan",),
        "逐镜规划环境底噪、动作 Foley、转场和负向音频约束。",
        ("环境音", "动作音效", "响度/入点", "负向音频约束"),
        ("写对白", "创作全片 BGM", "修改画面", "决定剧情"),
        ("分镜动作", "场景环境", "对白时间窗"),
        ("items[].shot_index", "items[].ambience", "items[].foley", "items[].cues", "items[].negative_audio"),
        "Kimi; rendered by Seedance native audio",
        ("每项回指 shot_index", "不重复对白", "对白时有 ducking 提示", "静音要求可执行"),
    ),
    AgentSpec(
        "continuity_qa",
        "连续性与生产 QA",
        ("review",),
        "只读检查剧情、资产、时空、对白、音效、时长、成本和可生成性并给出阻断等级。",
        ("检查项", "证据路径", "pass/warn/fail", "返工责任人"),
        ("直接修改任何上游产物", "生成媒体", "替用户批准", "隐藏失败"),
        ("剧本", "全部资产", "分镜", "对白计划", "音效计划"),
        ("checks[].name", "checks[].status", "checks[].evidence", "checks[].owner", "blocking_failures"),
        "Kimi",
        ("每个 fail 有责任 Agent", "引用具体镜头或资产", "存在阻断失败时不可预览"),
    ),
    AgentSpec(
        "shot_video_renderer",
        "镜头视频生成师",
        ("preview", "batch_video"),
        "将已批准的视觉、对白和音效计划组装成 Seedance 请求，先单镜后批量。",
        ("模型请求", "任务轮询", "失败即停", "视频落盘"),
        ("改写剧本", "改变资产设定", "新增对白或音效", "跳过预览审批"),
        ("已批准分镜", "对白计划", "音效计划", "参考图"),
        ("shot_index", "provider_job_id", "model", "url", "status"),
        "Seedance",
        ("预览只提交一镜", "批量上限生效", "首个失败后停止", "请求同时包含视觉/对白/音效"),
    ),
    AgentSpec(
        "music_director",
        "全片配乐总监",
        ("music_plan", "music"),
        "在锁定镜头后制定全片 BGM cue sheet、情绪弧线和对白避让策略，并选择渲染方式。",
        ("BGM 风格", "段落与时码", "情绪弧线", "ducking", "音乐渲染策略"),
        ("逐镜 Foley", "写对白", "改变镜头", "承担最终混音"),
        ("批量镜头", "剧本情绪节拍", "对白时间窗"),
        ("cues", "global_style", "ducking", "render_mode", "status"),
        "Kimi + Seedance native audio / optional music API",
        ("cue 覆盖全片且不越界", "对白处有避让", "独立音乐 API 缺失时清楚标注策略"),
    ),
    AgentSpec(
        "editor",
        "剪辑与合成师",
        ("compose",),
        "按已批准顺序拼接镜头、混音、统一编码并输出母版。",
        ("镜头顺序", "拼接", "混音", "编码参数", "最终文件"),
        ("改写故事", "重新生成镜头", "改变角色资产", "发布到外部平台"),
        ("成功镜头", "配乐策略", "导出规格"),
        ("url", "local_path", "input_shots", "encoding", "status"),
        "Local FFmpeg",
        ("全部预期镜头均存在", "顺序正确", "FFmpeg 返回码为 0", "成片可被 ffprobe 读取"),
    ),
    AgentSpec(
        "delivery_qa",
        "交付质检员",
        ("delivery_qa",),
        "只读验证母版文件、视频流、音轨、时长、画幅、镜头数量和可播放性。",
        ("交付检查报告", "阻断项", "文件元数据"),
        ("修剪或覆盖母版", "发布", "修改剧情", "替用户验收内容审美"),
        ("最终母版", "工作区画幅", "批量镜头清单"),
        ("checks", "status", "probe", "blocking_failures"),
        "Local FFprobe",
        ("文件存在且非空", "有视频流", "画幅符合目标", "音轨状态清楚", "时长合理"),
    ),
)


STAGE_GUIDANCE_HINTS: dict[str, str] = {
    "analysis": "例如：重点分析人物关系与阶级冲突；忽略播放器UI；某段需要更密集取证；希望30秒一段、每段10张图。",
    "script": "例如：完整法国本地化，不只是翻译；法国人名/地点/社会关系/称谓/欧元/生活习惯都要自然，保留原剧情冲突和反转；对白用法国本土口语。",
    "asset_manifest": "例如：优先继承同系列上一集已批准的人物、场景和道具；没有合适资产就标记 CREATE；同一人物换服装应做 VARIANT，不要新建身份。",
    "characters": "例如：优先复用‘法国现代豪宅家庭’人物库中的既有人物；只有不匹配时才新建设定。也可指定角色年龄、职业气质、服装叙事功能和表演风格。",
    "scenes": "例如：优先复用已挂载主题场景库；把豪宅改为巴黎近郊现代别墅；保留阶级反差；室内偏法式现代，不要中式装潢。",
    "props": "例如：优先复用主题道具库里的旅行袋/茶具；神秘大袋可本地化但必须保留反差和悬念功能。",
    "reference_images": "例如：写实法国电视剧质感；角色参考图优先；最多生成4张；不要文字/水印。",
    "storyboard": "例如：前30秒节奏更快；多用反应特写；控制在15个镜头；竖屏构图给字幕留安全区。",
    "dialogue_plan": "例如：全法语；tu/vous关系必须一致；保持台词短促可口型；不要逐字翻译中文成语。",
    "sound_plan": "例如：豪宅环境安静克制；冲突时只加轻微Foley，不要夸张综艺音效。",
    "review": "例如：重点检查法国本地化是否穿帮、称谓与tu/vous是否一致、资产跨镜是否漂移。",
    "preview": "例如：先预览冲突最强的一镜；720p；必须生成法语对白音轨。",
    "batch_video": "例如：先只生成前4镜控制成本；遇失败立即停；保持9:16和720p。",
    "music_plan": "例如：法国家庭轻喜剧质感，前半克制、反转前逐步抬升，不盖对白。",
    "music": "例如：沿用镜头原生声音；若没有独立音乐API则不要伪造已生成BGM。",
    "compose": "例如：最终成片偏高质量，CRF 18；音频192kbps；保留快启。",
    "delivery_qa": "例如：重点检查9:16、法语音轨、字幕安全区、镜头数量与最终时长。",
}


AGENTS_BY_ID = {agent.id: agent for agent in AGENT_SPECS}
STAGE_AGENTS = {
    stage: agent.id
    for agent in AGENT_SPECS
    for stage in agent.stages
}


def agent_for_stage(stage: str) -> AgentSpec:
    agent_id = STAGE_AGENTS.get(stage)
    if not agent_id:
        raise KeyError(f"No agent owns stage: {stage}")
    return AGENTS_BY_ID[agent_id]


def system_prompt_for_stage(stage: str, task: str) -> str:
    agent = agent_for_stage(stage)
    return "\n".join(
        [
            f"You are the {agent.name} in a gated short-video production workflow.",
            f"Purpose: {agent.purpose}",
            "You exclusively own: " + "; ".join(agent.owns) + ".",
            "You must not: " + "; ".join(agent.must_not) + ".",
            "Allowed inputs: " + "; ".join(agent.inputs) + ".",
            "Required output fields: " + "; ".join(agent.outputs) + ".",
            task,
            "Return exactly one valid JSON object as raw JSON text. Do not use Markdown code fences such as ```json. Keep IDs stable and report uncertainty rather than inventing evidence.",
        ]
    )
