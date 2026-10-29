# wire-sensitivity-market-closure — Tasks

## 1. 发布闸与渲染复检闭包化

- [x] 1.1 `_sensitivity_of` 换用根仓 `effective_sensitivity` helper（消费同源
  helper；incomplete 按敏感 fail-closed，闸文案点名该绑定数据集；数据集已不存在
  /查询失败保守放行沿既有口径）——验证 = pytest：直接绑定敏感（既有用例零回归）
  + 敏感祖先链子集绑定 → market 422 + incomplete fail-closed 两例
- [x] 1.2 渲染复检同口径（Q5 渲染点走同一 helper）——验证 = 发布后祖先改敏感 →
  订阅者渲染 403；祖先改回常规 → 渲染恢复（读时计算无陈旧值，spec 场景）

## 2. 测试与收口

- [x] 2.1 既有 artifact-marketplace 测试全量回归（绑定重映射/订阅/快照零漂移）
  ——验证 = 全绿（27/27；全 api 套件 1490 全绿）
- [x] 2.2 `openspec validate wire-sensitivity-market-closure --strict` 绿；与根仓
  change 的上产顺序记录（helper 前置——两 change 同批实施，同仓同卷）；ROADMAP-v2
  Wave B 勾选同步
- [ ] 2.3 归档 + 生产 rollout 走快照仓通道（随根仓主件同批）
