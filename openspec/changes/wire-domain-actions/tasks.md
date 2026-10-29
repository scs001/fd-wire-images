# wire-domain-actions — Tasks

## 1. 数据模型与迁移

- [x] 1.1 Alembic 迁移：`domain_action_types` / `domain_action_instances` /
  `domain_action_type_events` 三表（双方言 JSON、naive-UTC、唯一约束
  `(customer_id, entity_id, name)`）+ `ai_action_proposals.conversation_id`
  转可空（域动作无副驾会话——0043）——验证 = **alembic 真链 upgrade head
  实证（sqlite，0042→0043，2026-10-29）**：三表在位 + conversation_id 可空
  断言过；PG 侧 rollout 同跑；迁移链头断言五处同步 0043
- [x] 1.2 ORM 模型 `fd_wire_api/domain_actions/models.py`（类型/事件/实例三表
  挂 AiBase；实例状态机 pending→awaiting_confirmation→executed/rejected/
  failed/disabled；args_json 全量表单值=执行事实源，重启安全）

## 2. 声明 service 与校验

- [x] 2.1 声明 CRUD service：实体属主 + 绑定集一致 + **cell 档闸**（v0 边界，
  design D7）+ risk 枚举（publish/cost 422 带合法清单）+ 表单 schema 校验
  （{fields} 信封、select 字面量选项）+ 效果校验（映射列 ∈ 列快照、
  {field}/{literal} 恰一、update 须单列主键且主键列不进映射、主键字段必在
  表单）+ 重名 409——验证 = 校验矩阵 pytest 八例
- [x] 2.2 变更留痕（type_events append-only）+ 实体删除级联停用
  （`entities.delete_entity` 注入 ai 元数据会话）——验证 = 级联停用 pytest
  （在途实例同步 disabled）

## 3. 执行器与闸对接

- [x] 3.1 效果执行器：效果 → 引擎 `customer_dataset_write_batch`（**strict
  anchors**——业务写必须命中锚；update 锚=主键值，引擎锚按主键列匹配）；
  通道归属 `agent` + `actor="domain-action:{type}:{instance}"`（ADR-0006）；
  执行事实源 = 实例 args_json（进程内 args 重启丢失可恢复——approve 注入
  `_proposal_id`）；成败都回写实例——验证 = approve 执行/失败/停用三例
  pytest（write_batch 调用形状断言：ops/actor/channel）
- [x] 3.2 闸注册表双源：`resolve_action`（自理冻结名单优先 + `domain.{id}`
  动态源——未注册/停用/他企一律 ActionNotRegistered）；ActionDef 增
  on_proposed/on_rejected 生命周期钩子（自理动作恒 None）；propose 对域动作
  豁免会话要求（**恒不带 conversation_id**——不读 contextvar 防泄漏）；
  approve 注入 `_proposal_id`；reject 传会话束；自理注册表注释条数修正
  ——验证 = 停用类型 propose → 未注册；自理动作回归由既有 test_ai_actions
  全量背书（含 409/422/合规矩阵零回归）
- [x] 3.3 pending 实例在类型停用后的确认拒绝：停用即冻结在途实例（disabled）
  + 执行器启用态复检 backstop——验证 = 停用后 approve → ActionNotRegistered
  + 实例 disabled pytest

## 4. MCP 提议通道（wire-ops-mcp）

- [x] 4.1 `wire_action_list` / `wire_action_propose` / `wire_action_status`
  三工具：Bearer 转发、propose **即时回执不阻塞**（对偶义务措辞入工具
  docstring）、source_ref 原样透传（≤256，不解析——壹座身份行 key 名与
  turn= 位预留写进 docstring）——验证 = create_server 注册探针 19 工具；
  wire-ops 套件 32 全绿
- [x] 4.2 `tool-surface.json` 增列三工具（**tool_risk：propose=`write`、
  list/status 不入 risk 清单（read 语义）——只复用既有值，标签随 paas 评审
  回执知会壹座**）+ 描述更新 + api 测试 fixture 同步——验证 =
  `check_tool_surface.py` 过（rollout 时）、19 工具在位

## 5. 控制台（web）

- [x] 5.1 实体页「动作」节（DomainActionsPanel）：类型清单（风险徽标/启停）+
  声明构建器（字段行编辑/效果 kind+列映射行编辑）+ 实例台账（状态/结果/
  修正 id）+ 在途确认按钮（**复用闸既有 approve/reject 端点**——单一执行
  路径，无第二确认面；镜像禁令：不向壹座推送）——验证 = tsc 全绿 +
  既有 33 实体页测试零回归
- [x] 5.2 确认卡复用：实例确认走闸既有管线（AIActionAudit 同源审计，无双
  审计线）——验证 = 审批/拒绝 pytest（实例与闸状态同转）
- [x] 5.3 i18n zh/en 全量 + 文档页「实体」章节补域动作语句（唯一确认面/
  提议即时回执口径）——验证 = tsc 全绿 + DocsView 断言更新（v2 文案）

## 6. 测试与验证

- [x] 6.1 pytest：声明矩阵 + 闸管线（propose/approve/reject/失败/停用）+
  台账隔离 + 级联——验证 = 19 例全绿；全 api 套件 1524 全绿
- [x] 6.2 MCP 探针：19 工具注册（含三新工具）——验证 = create_server 探针
- [x] 6.3 公开面红线：域动作端点全部 console 身份门（401 兜底）+ wdk-
  数据面 key 不受理（业务写提议走客户身份）——验证 = 路由依赖签名
  （require_console_customer_id / require_action_editor，无匿名面）

## 7. 收口

- [x] 7.1 `openspec validate wire-domain-actions --strict` 绿；
  `docs/paas-domain-action-requirements.md` 状态已更新（评审回执 §五）
- [ ] 7.2 **部署三新工具后通知壹座重烘焙 registry 工具面快照**（AuthGate 401
  探不到——Mongo 直写配方）；重烘焙完成前 fleet-packs 模板不引用 `wire_action_*`
- [ ] 7.3 ROADMAP-v2 Wave D 勾选同步（本文件收口后）+ 归档（随生产 rollout）
