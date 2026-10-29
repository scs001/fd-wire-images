# wire-domain-actions

## Why

柏讯动作闸现注册的 13 条动作全部是**平台自理动作**（建草稿/发布/订阅/修数），客户无法定义属于
自己业务的动作（批准订单、标记批次不合格、指派工单、状态流转）——分析只能「看」，不能「办」。
这是 BI 工具与运营平台的分水岭（ROADMAP-v2 Wave D，2026-10-29 用户拍板立项）。地基全部现成：
动作闸（四级风险+确认档位+异步审批+审计台账）、修正层（overlay 写通道，ADR-0006 业务写并入
修正层、以通道归属区分语义）、实体层（动作类型的挂载点）。

## What Changes

- 新增**动作类型**一等客户域实体：客户在本企业实体上声明动作——名称、风险档、表单字段
  schema、效果 spec、启停；声明与变更留痕。
- 效果 v0 限定两种：对绑定数据集 **insert 落一行** 或 **update 状态列**（须实体主键定位）；
  执行翻译为既有修正通道调用，通道归属 `domain-action:*`（业务写语义，沿 ADR-0006）。
- 执行全链走动作闸：preflight → 确认档位（复用四级风险）→ 执行 → 审计台账；
  **写操作永不进任何工具面直调**（结构性保证，沿 wire-action-gate 既有红线）。
- agent 通道：wire-ops-mcp 新增 `wire_action_list` / `wire_action_propose` / `wire_action_status`
  三个客户面工具（verb_noun，与 business-mcp search/read 形状纪律一致）；集群岗位可提议，
  owner 在柏讯控制台确认。
- 控制台面：实体页新增「动作」管理节（声明/启停/实例台账）+ 动作确认卡复用副驾动作卡；
  文档页新增「动作」双语章节（同批上线红线）。
- 动作闸注册表从单一冻结名单扩为**双源**：平台自理冻结名单 + 客户域动作类型动态注册；
  未注册/未启用/他企动作类型一律按「动作未注册」拒绝（闸既有语义不变）。
- **壹座侧消费不在本 change 实施**：跨线需求已单独成文（`docs/paas-domain-action-requirements.md`），
  由用户推进 paas 仓。

## Capabilities

### New Capabilities

- `wire-domain-actions`: 客户域动作类型的声明、效果执行（落修正层）、确认档位、审计台账、
  MCP 提议通道与控制台管理面。

### Modified Capabilities

- `wire-action-gate`: 注册表扩为双源（自理冻结名单 + 客户域动作类型动态注册）；闸的
  风险分级、确认档位、异步审批、审计与「未注册拒绝」语义全部不变。

## Impact

- **api**：新 Alembic 迁移（`domain_action_types` / `domain_action_instances` 两表）；新模块
  `fd_wire_api/domain_actions/`（声明 service + 效果执行器 + 路由）；动作闸注册表接动态源。
- **wire-ops-mcp**：工具面 +3（`wire_action_*`），`tool-surface.json` 同批更新，走网关注册纪律。
- **web**：实体页动作管理节 + 实例台账 + 确认卡复用 + i18n zh/en + 文档页新章节。
- **跨线（不在本仓）**：壹座/萬星侧按需求文档消费；舰队岗位身份注入（R1-a/R1-b）是其前置。
- **不改**：corrections 存储与回滚语义、cell 引擎、计量轨道（动作执行不产生读取计量）、
  自理 13 条动作语义、「永不写回客户源」红线。
