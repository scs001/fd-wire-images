# wire-entity-write-surface

## Why

Wave C（`wire-entity-v2`，2026-10-30 已归档上产）给了对象面「读」的腿，但「写」的腿
仍只有 owner 控制台手工声明一条路。本体建设舰队（blueprint `ontology-builder`）的
deployer 岗现状只能 escalate 提案＋owner 手工落库，手工腿是内容面的吞吐瓶颈——
「本体建设 v1」验收判据第 2 条（提案→确认→落库→图谱可见全链自动）无法成立。
红线「写操作永不进工具面直调」不拦这条路：`wire-domain-actions` 已有
propose→confirm→execute→audit 全通道，缺的只是「声明写入（实体/关系元数据）」
没有对应的可执行动作类型。

## What Changes

- **内置声明动作类型**：平台向每个客户提供两个内置域动作类型——`entity-declare`
  （业务名/数据集/主键列/显示列/注记）与 `entity-relation-declare`（源实体列/
  kind/目标实体主键列/m:n 时中间集两列）。代码内置定义（非 DB 声明行）：不可停用、
  不可删除、不可修改，内置名保留（客户声明同名动作类型 409），风险档 `write`。
- **效果通道扩 metadata 写**：效果 spec 枚举增第三类 `metadata`（仅内置类型可用，
  客户自声明仍限 insert|update）：执行器直调实体/关系 CRUD service 的**同一校验器
  同一写路径**；实体/关系是客户域元数据、不是数据行修正，**不进修正层**
  （ADR-0006 修正层语义不变；「永不写回客户源」红线不变；声明执行零计量不变）。
- **提议严校验、执行复校**：propose 时按声明校验器全量校验（主键列 ⊆ 列快照、
  关系目标 ∈ 主键集合、唯一性/隔离），非法草案不进 pending（fail-fast）；确认执行
  时复校（列快照漂移、端点实体失存、撞名），如实失败、零半写，实例台账记原因。
- **工具面零变化**：agent 侧复用 `wire_action_list / propose / status`（内置类型经
  动态源对 list 立即可见）；wire-ops-mcp 无新工具——registry 快照无需壹座重烘焙，
  舰队 allowTools 无需增项（本 change 上线与 pack 发布解耦）。
- **控制台**：动作管理面显示内置类型（「内置」徽标、无编辑/启停入口）；在途确认
  复用既有确认卡（影响面摘要＝将要声明的实体/关系）；台账视图不变。
- **不做**：m:n 物化放开（另 change `wire-entity-mn-materialize`）、共享源对象、
  真实客户开通、声明校验器语义变化、编辑/删除实体经提案（v0 只 create）。

## Capabilities

### New Capabilities

（无——声明通道归 `entity-layer`，动作机制扩展归 `wire-domain-actions`）

### Modified Capabilities

- `wire-domain-actions`：①「动作类型声明」扩——平台内置两个声明动作类型（代码定义、
  不可停用/删除/修改、名保留、字段 schema 与实体层校验器同源），效果枚举增
  `metadata`（仅内置）；②「效果执行落修正层」扩——metadata 效果为修正层外第三
  通道，直调实体 CRUD 同路径，不进 overlay、不落修正记录；确认档位、异步审批、
  审计台账、MCP 提议通道语义零改动。
- `entity-layer`：新增「经提案的声明写入通道」——实体与关系可经确认的内置声明动作
  落库，与控制台直declared 同校验器同效果同档位规则；级联删除与既有 CRUD 纪律不变。

## Impact

- **api**：`domain_actions/` 模块（内置类型合成解析＋metadata 效果执行器调用
  entities service）；`domain_action_instances` 加结果引用列（迁移 0045，双方言）；
  `entities/` CRUD 对外语义零变化（执行器是其新调用方）。
- **wire-ops-mcp**：零改动（`wire_action_list` 动态源自动覆盖内置类型）。
- **web**：DomainActionsPanel 内置徽标＋影响面摘要文案＋「内置类型无操作列」；
  i18n zh/en；文档「动作」章节增「声明提案」双语小节（同批上线红线）。
- **舰队侧（非代码）**：`ontology-builder` 蓝图 deployer 岗只依赖既有
  `wire_action_*` 在册工具，无重烘焙前置。
- **不改**：动作闸确认档位/审计/单一执行路径、修正层存储与回滚、计量轨道、
  免费档声明能力、实体 v2 读面（对象浏览/MCP 对象工具/AI 注入）。
