# wire-workorder-ticket-conformance

## Why

drill 单 DE-20261011-99 的萬星生成回合实弹暴露：wire 出的生成工单缺 fleet 工单 schema 的必填字段（`ticket_id`/`source`/`created_at`/`evidence`/`lifecycle`），舰队验票器 `validate_ticket` 判红——agent 侧六环验证五环全绿（单元已建好、真取数命中），唯独 `ticket_valid` 红环来自工单自身，按「全绿才开 PR」纪律转 manual。平台自有出单工具对同内容会拒写（exit 2），而 wire 拉取器原样落盘不做校验=上游投递缺陷。修复后生成链可真实走通。

## What Changes

- `build_workorder` 补齐 fleet 工单 schema 必填七字段：`ticket_id`（=工单文件名主干，与落盘文件名一致）、`source="wire"`（与文件名 `{day}-wire-{code}` 段一致）、`created_at`（出单时刻 UTC ISO）、`evidence`（需求侧事实：demand_code/客户代号/source_urls/公开数据声明 + 生成单注记）、`lifecycle: []`；字段序对齐舰队 `new_ticket` canonical。
- 语义变化：工单 YAML 从「同日字节级确定」变为「每次出单自带上报时刻」（sha256 仍随每次出单留痕，append-only 时间线无跨次字节相等消费者；工单文件名与 ticket_id 保持同日稳定）。
- 测试更新：工单构造/导出既有断言对齐新字段；新增「构造产物通过舰队 `validate_ticket`」的契约测试（内联七必填断言，不引仓外依赖）。

## 红线与不动项

- 脱敏红线不变：evidence 只含需求单编码+客户代号（customer_id[:8]），contact_email/公司名绝不入单。
- 出单流程/端点/拉取器零改动；仅 YAML 内容补字段。

## Impact

- fd-wire/api：`demands/orchestration.py` build_workorder；api tests。
- 主 spec：demand-orchestration 工单契约要求补「舰队 schema 合规」场景。
