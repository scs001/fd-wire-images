# Proposal: wire-transform-pipeline-view — 变换管线视图 v0

## Why

数据面的派生能力已齐（derive/join/级联刷新/定时重派生/调度器），但血缘只存在于
引擎 lineage 端点（单数据集、引擎调用、wdk 凭据）——控制台里客户看不到「我的
数据从哪来、派生了什么、谁在定时跑」。方向 C/B 的交接点：管线的派生产物
（derived dataset）正是看板查询契约（同批 wire-dashboard-query-contract）
的聚合数据源。需要一张**全企业数据集派生图**让管线可见。

## What Changes

- **管线图端点**：`GET /api/v1/pipeline/graph`（console 会话身份，
  `require_console_customer_id`）——纯本地元数据组装，**零引擎调用**：
  - nodes：本企业全部数据集（dataset_id/key/name/source_kind/storage_tier/
    row_count/updated_at/schedule_cron 有无）；
  - edges：父→子（来自本地血缘登记 `source_kind='derived'` +
    `source_ref=父 key`，与 direct_derived_children 同源），带 op
    （v0 统一 derived——derive/join 区分未入本地登记）；
  - 深度标注（拓扑分层用）：源=0，派生按链深；环防护（异常数据态跳过）；
  - 节点上限 500，超出如实返回 `truncated: true`。
- **管线视图（web）**：数据页新增「管线」子视图：
  - 分层布局：按拓扑深度分列（源数据集在最左，派生逐级向右）；
  - 节点卡：名称/行数/更新时间/tier 徽标（免费档|cell）/调度徽标（有定时
    登记时）/kind 标注（derive|join|upload|indicator）；
  - SVG 连线父→子；点击节点跳该数据集详情；
  - 空态如实（「尚无派生数据集——从数据集详情发起派生或指标联合」）；
  - 小屏退化：按深度分组的卡片列表（v0 不做响应式画布）。
- **只读边界（v0 如实）**：管线视图不提供刷新/派生按钮——数据面刷新端点
  是 wdk 凭据轨道，console 会话不可直接调用；操作入口仍在数据集详情页。

## Impact

- **specs**：新能力 `transform-pipeline`（2 要求：图端点 + 管线视图）。
- **api**：新模块 `api/fd_wire_api/pipeline/`（routes.py 图端点 + graph.py
  组装逻辑）；app 挂载。
- **web**：数据页 tab（`console/DataPage` 挂点按现状）+ `console/pipeline/`
  （PipelineView.tsx 分层布局 + 节点卡 + SVG 边）+ api client +
  i18n 三语。
- **兼容**：纯新增端点与视图，无既有行为改动。
- **风险**：大数据集量下的图规模（500 节点上限 + truncated 如实标注）；
  环/孤儿血缘异常态（跳过并计入 anomalies 计数，不崩图）。

## Non-Goals

- 响应式拖拽画布/自动布局算法（v0 分列足够表达链）。
- 管线节点上的操作按钮（刷新/派生，见只读边界）。
- 跨企业图、产物（artifact）入图、指标（indicator）外部节点。
