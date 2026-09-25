# 2026-09-25 · Reference Images binding + UI fix

本补丁修复 reference_images 阶段的两个核心问题：

1. **总管把 `asset_library_context` 为空误判为没有已采用候选**
   - 已采用的角色/场景/道具视觉实际保存在 `asset_candidates`，不是主题资产库。
   - Director planner/reviewer 现在会收到当前 revision 的 selected asset candidates。
   - guidance fingerprint 也纳入 selected candidate 状态，选择图片后不会继续复用旧的“没有候选”计划。

2. **Reference provider 只做 isolated asset，没有真正生成 Phase B combinations**
   - 上游已采用候选直接进入 reference artifact，不重新生成。
   - source_kind 统一为 singular：`character | scene | prop | combination`。
   - 从当前绑定要求中提取显式 canonical-key 组合（同一行出现 >=2 个 char_/scene_/prop_ key）。
   - Seedream 5.0 Pro 使用多参考图 `image: string[]` 生成组合图。
   - 输出 `selection_coverage`、`reference_plan`、`missing_isolated`、`missing_combinations`，总管与 UI 可确定性检查完整度。

## Reference UI

`参考图`现在与角色/场景/道具采用同一套缩略图工作台：

- 阶段标题下方先显示所有 reference 缩略图；
- 点击缩略图打开单项详情 Modal；
- 可直接确认采用；
- 可对单张 reference 写局部意见并“抽 1 张 / 抽 4 张”；
- 支持批量调整；
- 顶部显示上游已采用数量、isolated 完成度、combination 完成度和缺失组合数。

Reference artifact 首次生成后会把每一项同步到 `asset_candidates(stage=reference_images)`：
- `upstream_adopted/reused` isolated reference 自动视为已采用；
- 新生成的 isolated/combination 是待确认候选；
- 用户选择新候选后会按原有机制使 storyboard 等下游失效。

## Seedream 5.0 Pro

5.0 Pro 不发送 `sequential_image_generation`。组合图使用官方支持的多图生图 `image: string[]`，最多传 10 张参考图。
