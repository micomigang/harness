# 2026-09-24 · 角色 / 场景 / 道具视觉候选工作台

本增量基于当前 v12 + Asset Manifest rev5 修复链路制作。

## 新增能力

- `characters` / `scenes` / `props` 节点直接显示视觉候选工作台。
- 每个资产可单独调用 Seedream：抽 1 张或抽 4 张，不重跑 Kimi 文本节点。
- 每张候选图都可：
  - `设为采用`：成为该资产的下游正式视觉引用；
  - `基于这张调整`：下一次使用该图作为 Seedream 图生图 reference；
  - 删除未采用候选。
- 每个资产有独立反馈框，例如“脸更老一点，但保持红棉袄和同一人物身份”。反馈只作用于当前资产。
- 被选中的候选会进入 `reference_images`：Seedream 不会重复生成已经人工选定的资产。
- `IMAGE_MAX_ASSETS` 现在只限制「本轮还要新生成多少张」，不会截掉用户已经选中的候选图。
- 候选按 artifact revision 隔离，避免清理/重建节点后旧候选串到新版本。
- 选择新的候选会使已经存在的相关下游产物 stale，并重置对应审批，保证视觉变更不会静默污染后续结果。

## 数据层

新增 SQLite `asset_candidates` 表，记录：workspace、stage、artifact revision、canonical_key、feedback、base candidate、prompt、model、remote/local URL、selected 状态。

## Seedream

- 首抽：文生图。
- 已有采用图或指定“基于这张调整”时：优先使用本地候选图 Base64 做图生图；本地不可用时回退 remote URL。
- character prompt 使用 appearance / wardrobe / performance / continuity；
- scene prompt 使用 layout / lighting / time_weather / continuity；
- prop prompt 使用 physical design / material-scale / state / continuity。

## 验证

`python -m pytest -q`：89 passed。
`node --check static/app.js`：passed。
