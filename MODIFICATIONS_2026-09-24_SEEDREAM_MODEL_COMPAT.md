# 2026-09-24 · Seedream 当前模型参数兼容修复

## 问题

当前图片模型返回 HTTP 400：

`InvalidParameter: sequential_image_generation is not supported by the current model`

Harness 原先在每个 Seedream `/images/generations` 请求里固定发送：

`"sequential_image_generation": "disabled"`

即使设为 disabled，不支持该字段的模型仍会直接拒绝请求。

## 修复

- 从 Seedream 图片请求 payload 中移除 `sequential_image_generation`。
- Harness 的批量角色/场景/道具抽图本身就是逐资产逐请求调度，不依赖模型侧 sequential generation。
- 文生图、候选图、基于候选的图生图调整均使用同一兼容请求路径。
- 增加测试，确保该参数不会重新进入请求 payload。

