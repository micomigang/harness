# Asset Manifest Review Guard Fix — 2026-09-24

本增量修复 rev4 暴露的“总管发明额外 schema 字段并反向否决已通过 manifest_validation 的产物”问题。

## 修改
- Asset Manifest contract 明确 typed metadata 为 closed required set：
  - character: appearance, costume
  - scene: key_set_elements
  - prop: physical_description, used_in_scenes
- physical_tags/social_register/lighting_mood/spatial_function/architectural_style/material/dimensions_hint/narrative_function 不再自动成为必填字段，除非最新用户指令明确绑定。
- manifest_validation.status=pass 时，Harness deterministic validation 为结构校验权威；总管不得凭空新增 schema 字段并触发 regenerate_current。
- 新增 review guard：仅抑制与已通过确定性校验冲突的 schema-only deviations；真实语义遗漏（例如用户明确要求的吊灯缺失）仍会保留并可触发重生成。
- typed_metadata_check 现在显式输出 required_fields、policy、missing_fields_by_asset。

## 测试
- 全量 pytest：83 passed
