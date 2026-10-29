# wire-domain-actions — Design

## D1 数据模型：两张新表，双方言 JSON 列

- `domain_action_types`：`customer_id` / `entity_id` / `name` / `risk`
  (`write|destructive`) / `form_schema` (JSON：`fields[]`，形状复用 form-apps 字段子集)
  / `effect` (JSON：`{kind: insert|update, dataset_id, value_mapping{column →
  {field|literal}}, status_column?}`) / `enabled` / `created_by` / 时间戳。
  唯一约束 `(customer_id, entity_id, name)`；声明与变更 append-only 留痕表
  （沿 asset-meta events 范式，变更事件进同一张事件表）。
- `domain_action_instances`：`type_id` / `proposer_sub` / `proposer_channel`
  (`console|mcp`) / `args_digest`（脱敏参数摘要，沿 ai/actions.py 纪律）/
  `status` (`pending|awaiting_confirmation|executed|rejected|failed`) /
  `confirmed_by` / `correction_id`（成功时回填）/ `error` / 时间戳。append-only。
- 双方言：JSON 列用 `JSON_DUAL_DIALECT`（datasets 模型既有惯例）；时间戳
  naive-UTC（entities 模型同口径）。

## D2 与实体层的关系：挂载而非侵入

动作类型挂 `entity_id`（FK 语义手动维护，沿 entity_relationships 惯例——service
显式级联，不依赖 DB CASCADE）。实体删除 → 动作类型批量置 `enabled=false`（保留
历史实例可查）；实体层 spec 的 CRUD 语义不变（本 change 不改 entity-layer）。

## D3 效果执行：翻译为既有修正通道，零新写路径

执行器把 effect 翻译为 `corrections` 既有调用（insert → `api_store` corrections
insert；update → 主键等值定位 update）——**零新写路径、零第二存储**（D5 统一代码路
纪律，沿 pipelines/service.py 的「无第二条物化路径」先例）。通道归属
`actor="domain-action:{type_id}:{instance_id}"`，ADR-0006 通道归属语义自然涵盖：
动作产生的行是业务事实，不是人工修正，但存储/审计/回滚全走同一条 overlay 机制。
执行体直调 service 层（同自理动作纪律——不经 HTTP、不经 MCP）。

## D4 动作闸对接：动态源注册，闸语义不动

`ai/actions.py` 的 `REGISTRY` 扩为双源解析：自理冻结名单原样 + `domain.{type_id}`
动态项（查 `domain_action_types` enabled 行，包装成 `ActionDef`：risk 取声明、
impact 按参数生成摘要、preflight=归属+启用+效果列校验、execute=效果执行器）。
闸的 propose→confirm→execute→audit 管线、四级确认档位、异步审批、审计表
**全部复用，零改动**；`ACTION_TO_TOOL_NAME` 只覆盖自理动作，域动作不进该映射
（域动作走 `wire_action_*` 工具，不冒充自理工具）。自理注册表注释「十一条」
顺手修正为实际条数。

## D5 MCP 三工具（wire-ops-mcp）

- `wire_action_list`：本企业启用动作类型 + 表单 schema（供 agent 构造提议）。
- `wire_action_propose`：`action_type_id + {field: value}` → 校验字段 schema →
  创建实例（pending）→ 走闸 propose（返回确认卡影响面摘要）→ 立即回执，不阻塞。
  可选 `source_ref` 形参**原样透传**落台账（不解析——壹座身份行
  `fleet=`/`post=`/`title=`/`target=` 格式演进不影响本接口；paas 评审 R1 更正后
  turn= 由 `add-fleet-identity-turn` 上产，字段位预留即可）。即时性为对偶义务
  （阻塞会被壹座归因 stalled）。
- `wire_action_status`：按实例 id 查状态/结果/修正 id。
- 进站 Bearer 原样转发（既有双轨鉴权）；viewer 拒 propose（沿 console auth viewer
  只读门语义在 MCP 侧复刻）；跨企业 404。`tool-surface.json` 增列三工具 +
  `tool_risk` 标签（list/status=`read`、propose=`write`——只复用既有值，
  与壹座 `wire-tool-risk` 快照的 risk→tier 映射逐字节对齐；新值会打破对齐）。
  **部署后须通知壹座重烘焙 registry 快照**（AuthGate 匿名 401 → 发布门 live
  probe 探不到新工具），重烘焙完成前 fleet-packs 模板不引用 `wire_action_*`。
- 镜像禁令（paas 评审 R2 共同护栏）：域动作提议/实例不推送、不镜像至壹座
  拍板面（decision lane）；壹座观测走回合 trace 自然呈现，柏讯侧零对接代码。

## D6 确认链路与控制台

- 确认卡复用副驾 `ActionCard` 组件（影响面摘要 + 确认/拒绝 + 异步审批位）。
- 控制台入口放**实体页**「动作」节（声明/启停/台账），不新增侧栏页签——
  控制台 11 页签已是四组满编，动作是实体的附属能力，心智上挂在对象上。
- pending 实例的可见性：实体页动作节 + 工作台聚合端点增计数段（沿 workbench
  六段惯例扩一段，向后兼容——新段可选字段）。如实现中发现工作台契约冻结太紧，
  降级为仅实体页可见（tasks 里做取舍记录），不为此改 personal-workbench spec。

## D7 安全与边界

- 效果引用校验：dataset_id 须为实体绑定数据集（v0 限定——动作只写实体自己的集，
  跨集效果留 v1）；列须在保存时刻列快照；`update` 须主键定位；value_mapping 的
  value 只允许 `{field: <表单字段>}` 或字面量（禁引用其他数据集）。
- 提议时重校验列快照漂移（422 如实，沿 form 提交同口径）。
- 公开面零暴露：全部端点 console 身份门 / MCP Bearer 门，无匿名通道。
- 不做：delete 效果（destructive 语义留给未来，v0 只有 insert/update）、
  多步/跨集事务效果、定时触发（alert→action 联动是远期独立 change）、
  写回客户源（红线）。

## D8 计量口径

动作执行 = overlay 写 = 不产生行数读取计量（修正通道既有口径，不新增计量分支）。
提议过程中的读取（如 list/状态查询）沿既有通道各自的计量语义（MCP 读按 wire 入口
口径、console 读不计量），本 change 不发明新口径。

## D9 风险面

- 注册表动态源引入后，「未注册拒绝」的测试面必须覆盖：停用/他企/伪造 type_id
  三种绕过尝试（tasks 验收项）。
- MCP propose 与副驾 propose 汇入同一实例表，避免两条审计线漂移。
- 实体删除级联停用的窗口内，已 pending 实例的确认仍须拒绝执行（preflight 查
  type 启用态——停用后 pending 不可再确认）。
