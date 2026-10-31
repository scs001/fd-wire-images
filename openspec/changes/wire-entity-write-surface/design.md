# wire-entity-write-surface — Design

## D1 通道形状：复用 Wave D 域动作机制，内置类型代码化（三案对比）

- **A 独立声明提案端点**（新表+新路由+新工具 `wire_entity_propose`）：弃——制造
  第二套「提议→确认→执行→审计」机制，正面撞 `wire-action-gate`「单一执行路径与
  审计台账」要求；且 wire-ops 工具面 +1~3 条，触发壹座 registry 快照重烘焙前置，
  上线与 pack 发布耦合（Wave D 已付过这笔协调成本，不必再付）。
- **B 内置域动作类型**（本设计）：闸的确认档位、异步审批、台账、`wire_action_*`
  通道、DomainActionsPanel 全复用；工具面零变化、无重烘焙依赖。
- **C 动作闸自理注册表加条目**：弃——自理 13 条是平台自理动作（身份与会话语义不同：
  域动作恒无会话），实体声明是客户域数据操作，混入冻结名单会让两套身份语义串门。
- **B 内子裁决：DB 种子行 vs 代码内置合成定义**——取代码内置：种子行需逐客户 backfill
  （新客户必漏=静默能力缺口，沿「天花板类断言必须读单一真源」教训的反面）、可被客户
  停用/删除（破坏「声明通道恒可用」）、多一表写。合成定义在动态源解析**前**命中，
  内置名保留（客户声明同名 409，防冒充）。

## D2 内置类型字段 schema：text 承载引用字段（v0 select 约束的诚实适用）

`wire-domain-actions` 既有纪律：select 仅字面量选项、v0 不做动态选项源。实体/数据集
引用是动态值域，故：

- `entity-declare`：`business_name`(text,required)、`dataset_key`(text,required)、
  `pk_columns`(text,required,逗号分隔)、`display_column`(text,可选)、
  `column_notes`(text,可选,`列=注记;` 串)。
- `entity-relation-declare`：`source_entity`(text,required)、`source_column`
  (text,required)、`target_entity`(text,required)、`target_column`(text,required)、
  `kind`(select,字面量 `n:1|1:1|1:n|m:n`)、`junction_dataset`/`junction_left_column`/
  `junction_right_column`(text,kind=m:n 时必填——条件必填在 preflight 校验)。
- 提议 preflight **完整跑既有声明校验器**（按 `dataset_key`/实体名解析出对象再校验：
  主键 ⊆ 列快照、目标 ∈ 主键集、唯一性/互斥/隔离），非法草案不进 pending——这正是
  动态选项缺失的补偿：值域校验后置到闸、前置于 pending，agent 拿即时 422 自纠。
  执行时复校同校验器（漂移防御），两处调用同一函数（单一校验真源，零第二实现）。

## D3 metadata 效果执行器：同 service 路径，不进修正层

- effect spec `{kind:"metadata", op:"entity.create"|"relationship.create"}`；执行器
  解析表单→调 `entities` service 既有创建函数（同事务、同校验、同唯一性语义），
  沿自理动作「直调 service 不经 HTTP」纪律。
- **为什么不落修正层**：修正层是数据行 overlay 的存储/回滚机制（ADR-0006）；实体是
  元数据，强塞会产生假的「可回滚修正记录」且回滚语义错位（撤销声明=既有删除通道，
  级联删两端关系）。通道归属仍为 `domain-action:{type_id}:{instance_id}` 级别的
  台账区分，无需修正 actor 命名空间。
- `domain_action_instances` 加一列 `result_ref`（varchar(128) 可空，双方言，迁移
  0045）：metadata 成功回填实体 id 或关系 id；insert|update 仍回填 `correction_id`。
  台账展示按效果类型取其一。

## D4 提议即时性与对偶义务

沿用 `wire_action_propose` 即时返回 pending（阻塞=对偶义务违约，壹座归因 stalled）；
`source_ref` 原样透传（岗位身份行 `fleet=`/`post=`/`title=`/`target=`，`turn=` 字段位
预留，不解析）。舰队 deployer 岗的 cadence 提示词要求：当日提案数、被拒数（含原因类）
进报数槽，供看板计数。

## D5 web 面：内置类型的呈现纪律

- DomainActionsPanel 列表含内置两行：徽标「内置」、无启停/编辑/删除入口（操作列空）；
  影响面摘要（确认卡）＝将声明内容渲染（实体名+数据集+主键列 / 关系四元组+kind），
  参数摘要脱敏沿既有 `args_digest` 纪律。
- 实体页图谱上「经声明提案落库」的实体与控制台声明无差别（读面零漂移，spec 已锁）。
- i18n zh/en 同批；文档「动作」章节增「声明提案」小节——**同批上线红线**（实体层
  「文档与红线」纪律）。

## D6 免费档与计量

声明通道继承声明本身的档位规则：免费档可用（付费闸只在对象浏览）。执行零计量不变。
无新增配置项、无新增 flag（不需要 flag-off 等价性论证：通道是增量入口，既有路径逐字
不动即天然回退）。

## D7 可逆性与后挂账

- v0 只 create（`entity.update`/`entity.delete` op 挂账：加 op 枚举即可扩，不改通道）。
- 若后续共享源对象接入（跨线需求 `business-mcp-shared-objects-requirements.md`）需要
  批量声明，本通道 + 舰队可原生承载（批量=多条提议），不预留第二路径。
- 回退：合成定义从解析器摘除即全量回退（无 DB 状态残留；已落实体是正常声明数据，
  不随回退清除）。

## D8 实施期暴露的既有缺陷（随本 change 顺手修复）

apply 期间加路由级测试时撞出两处 HEAD 上的真缺陷——**域动作全部端点在生产不可达**：

1. `domain_actions/routes.py` 的 `APIRouter` **无 `/api/v1` 前缀**（entities 切片用
   `prefix="/api/v1/entities"`，本切片裸挂 `/domain-actions`）——web 面板（
   `DomainActionsPanel` 硬编 `/api/v1/domain-actions/instances`）与 wire-ops
   MCP（`wire_action_*` 调 `/api/v1/domain-actions/...`）全 404。Wave D 的验收做过
   工具面注册探针与 pod 内回环调用，没走网关真路径，故未暴露。修复：router 加
   `prefix="/api/v1"`（路径与 entities 切片同源纪律对齐）。
2. `require_action_editor` 惰性导入写成 `from ..console.models import Customer`
   ——`Customer` 在 `billing.models`（assets/routes.py 同源先例）→ 该依赖一碰即
   ImportError（propose/声明面全 500）。修复：改导入源（函数体未消费该名，仅
   文档/类型语义——顺手去未用导入）。

两条均属「写面可在生产被消费」的前置：本 change 的 METADATA 通道、列表注入、
propose 分流全走这组路由；不修则验收判据第 2 条（全链自动）在真环境不成立。
路由级测试（`test_entity_write_surface_routes.py`）锁定带前缀路径与 404/403 语义。
