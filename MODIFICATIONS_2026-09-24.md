# v12 Asset Manifest 增量更新 — 2026-09-24

这是覆盖式增量包，只包含本轮修改文件，不是完整项目。

## 使用方式

将压缩包中的 `oiioii-harness` 文件夹直接拖到你现有项目所在目录，选择“覆盖/替换同名文件”。

建议覆盖前备份现有项目。

## 本轮修改

- `app/orchestrator.py`
  - Asset Manifest 以 `script.asset_requirements` 为 canonical baseline。
  - previous manifest + 本轮模型结果做 reconcile/merge。
  - 支持 legacy key 与 canonical key 的语义迁移，避免同一资产重复保留。
  - `costume/wardrobe/outfit/clothing` 归并为 character；`set/location` 归并为 scene。
  - 修复 legacy boolean `continuity_lock` 被错误转换成 `['True']` / `['False']`。
  - 保留真正的额外资产，并生成/补充 manifest validation 信息。

- `app/providers/openai_compat.py`
  - Asset Manifest / 总管复盘上下文不再只截取前 10 项。
  - 同步完整 screenplay asset requirements，避免后段 props 在 review 中被误判丢失。

- `tests/test_series_assets.py`
  - 增加/更新 screenplay baseline、monotonic merge、legacy semantic migration、continuity lock 等回归测试。

- `tests/test_openai_compat.py`
  - 增加/更新完整 manifest review context 的回归测试。

## 当前 EP02 回归目标

基于当前项目数据应自然收敛为约：

- 5 character
- 5 scene
- 4 prop
- 共 14 项

该数量不是代码硬编码，而是由 screenplay baseline、上一版有效资产和当前素材/模型结果合并得到。
关键旅行袋需要保持 continuity lock。

## 注意

本包是针对你上传的 v12 基线生成的覆盖包。若你在这几个文件上又做了新的本地修改，请先自行 diff/备份再覆盖。
