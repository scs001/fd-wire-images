## Why

控制台的 AI 助手浮层（`wire-assistant-panel`）上线后暴露出一个结构性矛盾：
**「问」不能做、「做」看不见**。

- 问答轨 `tools=[]` 是结构性保证（`ai/answer.py`），system prompt 第一句就是
  「你不能执行任何操作」——客户明明在看一个数据集，助手却只能指路不能带路。
- 构建轨会把五类产物 spec 写进 `ai_draft_artifacts`，但前端 `fetchArtifacts()`
  是死代码（`web/src/console/api.ts:298` 全仓无调用），唯一的转正入口
  `POST /internal/materialization/{id}/promote` 挂在 `X-Internal-Token` 闸上，
  客户面够不着——AI 说「草稿已生成」，客户找不到它。
- 循环是同步的（一次请求跑完最多 `AI_MAX_TURNS` 轮），模型发出的动作只能在
  服务端执行，没有「模型提议、人确认、系统执行」的通道。

壹座侧已按 `add-line-boards-and-action-gate`（paas 仓，2026-10-20 实施）
为谦面与萬星落了同构答案：**动作闸**（`propose_action` 提议通道 + 风险分级
注册表 + 三级确认 + 审计台账）+ 线看板 + 副驾。本 change 把同一模式落到柏讯，
并让柏讯的 AI 构建成为该模式的第三个（也是最完整的）实例——柏讯自己就是后端，
执行体可直调 service 层，不需要 MCP 桥。

## What Changes

- **动作闸（新能力 `wire-action-gate`）**：动作注册表按五级风险
  （read/write/destructive/cost/publish）声明 11 条 v1 动作；副驾循环新增
  `propose_action` 内置工具，立即返回 proposal id；闸按风险分级执行——
  读直通、写行内确认、破坏性/花钱二次确认+影响面、对外暴露（分享/市场）
  异步审批 24h fail-closed；每次执行落审计，回执不冒充结果。
- **单一副驾取代问/做二态**：浮层与「AI 构建」页合并为单一副驾——读工具
  直通（免费档可读自己的数据集/产物/需求单；指标探索仍需 cell，如实说明），
  写操作一律经闸。免费档能力净增（问答+读+提议），付费档构建类动作仍由
  档位 preflight 把关。
- **写工具结构性不进副驾**：循环的工具白名单里只有读工具与 `propose_action`；
  既有五类产物 spec 工具从「模型直调」改为经闸提议。执行只存在于闸的单一
  执行路径（fd-wire-api 内部直调各切片 service）。
- **草稿可见与转正（收口 G1）**：面板内动作卡渲染产物预览（复用
  `DashboardRenderer`，确认卡=预览卡二合一）；`wire.artifact.promote` 开
  客户面执行路径（复用 materialization service），草稿从「找不到」变成
  「看得到、点得动」。
- **多队审批语义**：审批人 = 活跃队 owner/admin，viewer 不能批（复用
  `VIEWER_DENIED_METHODS` 语义）；需求单合规门字段（公开源 URL+公开数据
  声明）在提议卡上留给客户显式填写，AI 绝不代填代勾。
- **动作执行后刷新当前页**：闸执行成功广播轻量事件，产物页/数据集页重取
  受影响数据。
- **风险声明单一真源**：`wire-ops-mcp` / `wire-build-mcp` 的
  `tool-surface.json` 增 `risk` 标注（write/destructive/cost/publish），
  本仓注册表从它生成；壹座侧注册表后续消费同一份（另一个 change）。

## Capabilities

### New Capabilities

- `wire-action-gate`: 动作注册表与风险分级、`propose_action` 提议通道、三级
  确认与影响面、异步审批与超时 fail-closed、单一执行路径与审计台账、
  回执不冒充结果、preflight（档位/引擎/归属/角色）、合规门不代勾。

### Modified Capabilities

- `assistant-panel`: 问/做二态合并为单一副驾；构建轨五类 spec 工具改经闸
  提议；动作卡两面（待确认/已执行）与产物预览内联渲染；草稿在面板内可见；
  动作执行成功刷新当前页；免费档能力边界重述（问答+读+提议可用，构建类
  动作仍需引擎）。
- `console-ai-entry`: 对话后端分轨要求重述——原「问答轨不执行工具」改为
  「读工具直通 + 写操作经闸」；`AI 构建`页保留整页形态与壹座深链不变。

## Impact

- `api/fd_wire_api/ai/` — 动作注册表（新 `actions.py`：注册表+propose/
  approve/reject/query 端点+执行器）、循环工具面改造（读工具+propose_action）、
  `answer.py` 问答轨退役并入单一循环（端点兼容期保留）
- `api/fd_wire_api/ai/models.py` + 迁移 — `ai_action_proposals` /
  `ai_action_audit` 两表（AI 库，append-only）
- `api/fd_wire_api/materialization/` — promote 增客户面执行路径（闸执行体
  内部调用；不新增绕闸 REST 端点，产物页手动转正走同一闸）
- `web/src/console/assistant/` — `AssistantConversation` 增动作卡与预览渲染、
  模式切换退役；`api.ts` 增提议端点客户端
- `web/src/console/ArtifactsPage.tsx` / `DatasetsPage.tsx` — 监听闸执行事件
  重取数据
- 外部仓（任务组内声明，不本仓实现）：`wire-ops-mcp` / `wire-build-mcp`
  `tool-surface.json` 增 `risk` 标注
- 不动：cell MCP 执行器与计量归属（AI_PLATFORM_CREDENTIAL 平台凭据轨道）、
  每客户每日会话限额、产物状态机（draft/published/archived）、分享令牌机制、
  壹座深链（spec 场景「不内嵌」保持）
