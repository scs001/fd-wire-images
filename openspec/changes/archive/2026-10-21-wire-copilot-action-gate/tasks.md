## 1. 动作闸地基（后端）

- [x] 1.1 `api/fd_wire_api/ai/actions.py`：动作注册表（design D3 十一条冻结表：id/risk/tier/executor/preflight/impact），风险→档位映射常量；单测覆盖 tier 映射、未注册动作拒绝、impact 按参数生成
- [x] 1.2 `ai/models.py` + alembic 0039：`ai_action_proposals` / `ai_action_audit` 两表（append-only；args 只存摘要与 sha256）；单测验证建表、状态迁移与按 team/时间/action 检索
- [x] 1.3 提议端点四件（design D4）：`POST /actions/propose`、`POST /actions/{id}/approve`、`POST /actions/{id}/reject`、`GET /actions/{id}` + `GET /actions?conversation_id=`；单测覆盖 pending_confirm/pending_approval/executed/rejected/expired/failed 六态、他企提议 404、审批人角色（owner/admin 过、viewer 403 且状态不变）
- [x] 1.4 24h 过期扫描：扫 `pending_approval` 置 `expired` fail-closed，进程内定时器（对齐 refresh-scheduler 心跳），时钟可注入；单测验证过期不执行
- [x] 1.5 preflight 各分支（design D3 表）：cell 未开通拦构建类、档位拦订阅、归属拦他队对象、viewer 拦写；单测覆盖每条分支的如实文案（含修复指引）与不进确认档位
- [x] 1.6 执行器：十一条件执行体直调各切片 service（跨切片多库会话按 routes 注入纪律）；单测验证单一执行路径、执行结果/耗时落审计、审计与参数摘要不含凭据明文
- [x] 1.7 需求单合规门：`approve` 收 `compliance{source_url, public_claim}`，模型提议参数中的合规取值服务端一律丢弃；单测覆盖缺字段不执行、不代填不代勾

## 2. 副驾循环改造

- [x] 2.1 `ai/loop.py` → `run_copilot_loop`：工具面 = 内部读工具 + cell 工具（有引擎才注入）+ `propose_action`；同步语义不变（提议返回 pending 不等待确认）；既有 8 处循环测试改造后全绿
- [x] 2.2 内部读工具：dataset_list / dataset_schema / artifact_list / artifact_get / demands_list 直调 service（任意档位可用）；单测覆盖免费档可读自有数据、指标探索类仅在有引擎时进工具面
- [x] 2.3 `propose_action` 工具：schema 固定、执行=写 proposal 行并返回 `{proposal_id, state, impact}`；单测覆盖回执不冒充结果（pending ≠ 成功）
- [x] 2.4 system prompt 换副驾 persona（design D11：只读直通/写必提议/回执转述不加工/意图不清先追问）；实体图谱注入块零漂移验证
- [x] 2.5 `/api/v1/ai/answer` 保持行为不变（兼容子集），前端停用后回归测试确认端点仍可用

## 3. 前端面板

- [x] 3.1 `AssistantConversation`：问/做模式切换退役，单一输入与事务流；免费档徽章文案改为能力说明；组件测试覆盖免费档可读可提议、构建类提议被拦后的文案
- [x] 3.2 `ActionCard.tsx` 两面：pending（动作名/目标/影响面/确认取消；`demand.submit` 卡上合规字段留白必填）与 resolved（结果摘要/审计指向）；组件测试覆盖两面与历史回放（按 `GET /actions?conversation_id=` 状态分派）
- [x] 3.3 草稿预览内联：`wire.artifact.draft.create` 的 dashboard kind 在卡内用 `DashboardRenderer` 渲染 spec；form/record-view 呈现结构摘要（字段/键列）；组件测试验证预览不含占位符、确认前不落草稿
- [x] 3.4 `api.ts`：propose/approve/reject/list 客户端；复活 `fetchArtifacts` 并在面板聚合展示本会话草稿与预览入口；单测覆盖草稿列表与转正动作链

## 4. 页面刷新与收口

- [x] 4.1 执行成功事件：闸 approve 返回后广播 `wire:action-executed`（detail 带 resources/team）；`ArtifactsPage` / `DatasetsPage` 监听重取受影响列表，保留筛选与滚动；组件测试覆盖发布后列表当场更新
- [x] 4.2 草稿转正链路端到端：草稿提议 → 确认 → 转正 → 产物页可见（含事件刷新）；集成测试覆盖 promote 成功与失败两态
- [x] 4.3 i18n 双语补齐（动作卡/影响面/待审批/已过期/合规字段等键）；奇偶校验通过

## 5. 外部仓风险标注（单一真源）

- [x] 5.1 `finddata/wire-ops-mcp` / `wire-build-mcp`：`tool-surface.json` 增顶层 `tool_risk` 平行键（不动既有 tool_groups 形状），写类工具逐条标注；两仓 selfcheck 通过
- [x] 5.2 本仓对拍测试：注册表风险分级 ↔ `tool_risk` 不一致即失败，错误信息指明条目；把两份 surface 文件的拷贝纳入测试夹具并注明来源与同步方式

## 6. 验收与回滚

- [x] 6.1 全量测试绿：`api` 单测（含既有循环/浮层测试改造）、`web` vitest、typecheck、web build
- [x] 6.2 真机验收（wire-testers 三档账号）：免费档问+读+订阅提议可用、构建类提议被如实拦；basic/pro 档完整走「描述看板 → 预览确认 → 草稿 → 转正 → 发布 → 分享进审批 → owner 批准」全链；viewer 角色确认写动作被拒；审计台账可按团队检索且无凭据（2026-10-21 生产实证，见 `reports/acceptance-6.2.md`；抓到并修复两缺陷：转正未建档、分享令牌落审计。问答轨 00:22 实证 200；复测受 token 网关上游 403 阻塞——非本改动，端点如实 502）
- [x] 6.3 回滚演练：回滚 api/web 镜像后回到问/做两态现状，两表与新端点留存无消费者、无副作用（2026-10-21 生产实证，见 `reports/rollback-6.3.md`：旧 api 新端点 404、旧 web 恢复问答模式；两表行数与 alembic 0039 在回滚/前滚前后完全不变；已前滚恢复当前版）
