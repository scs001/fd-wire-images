# Proposal: wire-dashboard-query-contract — 看板查询契约（确定性刷新 v2）

## Why

看板刷新今天是纯 AI 轮（wire-dashboard-refresh）：LLM 读面板 source 自选工具取数。
三个问题：

1. **不确定**：同一看板两次刷新可能取不同口径的数据（source 只是展示字符串）；
2. **贵且慢**：每面板取数都要走一轮 LLM（网关调用 + cell 工具循环）；
3. **免费档不可用**：AI 轮要求 cell 端点，免费档客户（api 层数据集）完全无法刷新。

路线图（ROADMAP 方向 B「看板查询契约」半边）：把面板来源从「展示标注」升格为
**结构化查询引用**（与数据面 query 端点同形同界），刷新走确定性执行——零 LLM、
免费档本地执行、可审计可复算。

## What Changes

- **面板契约 v1.1**：面板新增可选 `query` 字段
  `{dataset_id, filters?, sort?, limit?, columns?}`（与
  `POST /api/v1/datasets/{id}/query` 入参同形：filters 算子
  eq/in/gt/gte/lt/lte/contains/startswith、sort dir asc/desc、limit 1..1000、
  columns 投影即快照列序）。聚合口径 v0 不入契约——需要聚合的面板引用派生
  数据集（变换管线产出），契约面保持最小。
- **确定性刷新路径**：`POST /api/v1/artifacts/{id}/refresh` 决策——spec 中
  ≥1 面板带合法 query → 契约路径（逐面板执行查询、零 LLM）；0 个 → 既有
  AI 轮路径原样保留（旧看板零漂移）。
  - 契约路径执行：免费档（api tier）本地 `api_store.execute_query`（零引擎
    调用、零计量）；cell 档经引擎 query 工具（沿用 EngineClientFactory）。
  - 面板级失败隔离：某面板查询失败（数据集不存在/列名非法/引擎拒绝）→
    该面板保留原快照并在响应记录 error，其余面板照常刷新；**全部**契约面板
    失败 → 502 失败零半写（不落版本）。
  - 无 query 面板 → 保留原快照，响应记 `kept`。
  - 响应扩展：`mode: "contract"|"ai"` + `panels: [{panel_id, status, error?}]`。
- **AI 生成侧**：`dashboard_spec_create` 工具指令升级——面板数据取自数据集
  查询工具时必须同时落 `query` 引用（把所用 dataset_id 与查询形参写进面板）；
  kpi 面板模式：sort 最近一行 + limit 1 + columns 投影到值列。
- **前端**：spec.ts 宽容解析/归一 query（非法整体丢弃、面板不受影响）；
  编辑器保留 query（增删/复制/类型互转不抹掉）并显示「已绑定查询」徽标；
  刷新结果按面板呈现（成功 N / 保留 M / 失败 K 及原因）。

## Impact

- **specs**：dashboard-renderer（契约 + AI 生成侧）、dashboard-refresh（确定性
  路径 + 免费档边界修订）。
- **api**：新模块 `artifacts/query_contract.py`（解析 + 确定性执行）、
  `artifacts/routes.py`（refresh 端点决策分支）、`artifacts/schemas.py`
  （ArtifactRefreshOut 扩展）、`ai/tools.py`（工具指令）。
- **web**：`console/dashboard/spec.ts`（query 归一）、`DashboardPane.tsx`
  （刷新结果按面板）、`DashboardEditor.tsx`（徽标 + 保留）。
- **兼容**：旧看板（无 query）走 AI 路径行为零漂移；响应字段只增不改；
  免费档从「刷新必 403」变为「契约看板可刷新」（档位边界修订，spec 同步）。
- **风险**：AI 落 query 的质量（dataset_id 写错 → 面板级失败如实呈现，不静默）；
  快照类型归一（引擎返回 bool/None → str|null 降格，不丢数据）。

## Non-Goals

- aggregate 模式入契约（v0 用派生数据集承载聚合）。
- query 的编辑器表单化编辑（v0 只读徽标 + 透传保留）。
- 定时刷新的契约感知（refresh-scheduler 已按 artifact 调度，路径自动受益）。
