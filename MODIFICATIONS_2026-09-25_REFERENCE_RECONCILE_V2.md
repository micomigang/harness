# 2026-09-25 · Reference Images reconcile v2

本补丁针对 reference_images rev2 的误判与组合计划污染做通用修复，不写死 EP02、人名、14/7/21 数量或任何具体 canonical_key。

## 根因

1. **总管复盘只看到了 items 的前 10 项**
   - `_director_artifact_preview()` 对普通 list 统一截断为 `[:10]`。
   - reference_images 即使实际 artifact 已包含更多 isolated / combination，Reviewer 也会把第 11 项以后误判为不存在。

2. **Combination 解析扫描了 Director 的整段复述文本**
   - 旧实现把 `user_instruction + effective_instruction + prompt_addendum + interpretation` 全部逐行扫描。
   - 只要一行出现多个 `char_/scene_/prop_` key 就被当成组合。
   - 因此“所有角色列表 / 所有场景列表 / 所有道具列表”会被错误生成成纯角色、纯场景、纯道具组合。

3. **上游采用状态命名不统一**
   - 上游已采用视觉进入 reference artifact 时使用 `upstream_adopted`，与工作台的 `selected_candidate` 语义不一致。

## 修复

### 组合计划

- Combination 优先从**用户绑定指令**读取；若当前重生成流程只保留了 Director 的 effective bound instruction，则仅从其中同样的显式 `+` 关系语法回退读取。不会扫描 interpretation / prompt_addendum 等自由复述文本。
- 只有用户明确使用 `+` / `＋` 连接 canonical keys 时才视为 relationship combination。
- 逗号列表、分类清单、解释性 prose 不再自动生成组合。
- 规则是通用语法，不绑定任何项目、人物或固定数量。

### Deterministic validation

reference artifact 新增 `reference_validation`：

- isolated expected / completed
- combination expected / completed
- planned combinations / actual combinations
- missing planned combinations
- unexpected combinations
- invalid source kinds
- duplicate reference keys
- status = pass / fail

Reviewer 的系统规则只认这一通用 deterministic contract，不写死本项目数量。

### Reviewer 完整上下文

- reference_images 的 `items` 不再截断前 10 项。
- 使用 compact reference item representation 把**完整 items 列表**交给 Reviewer。
- Reviewer 不再因为 preview truncation 虚构缺项。
- Orchestrator 增加通用 reference review guard：当 `reference_validation=pass` 时，纯 schema / coverage 幻觉不能触发 regenerate loop；视觉内容问题仍然允许 Reviewer 正常指出。

### 状态

- 上游工作台已采用的 isolated reference 写为 `status=selected_candidate`。
- 同时保留 `origin=upstream_selected_candidate` 表示来源。
- 同步逻辑兼容旧的 `upstream_adopted`，不会破坏已有数据库。

### UI

参考图工作台分为：

- Phase A · 基础参考：角色 / 场景 / 道具，全部完成时默认折叠。
- Phase B · 关系组合：始终重点展示，便于逐项确认组合一致性。

组合卡片不再用超长生成名称作为主标题，而显示“关系组合参考”，下方展示 canonical binding。

## 兼容性

- 不改数据库 schema。
- 不改 characters / scenes / props Bible。
- 不改已采用上游候选。
- 不改 Seedream 单项抽图 API。
- 旧 reference revision 可保留；重新生成新 revision 后自动以新 artifact 为下游依据。
