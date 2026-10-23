# Proposal: wire-panel-query-binding — 看板契约表单化绑定（自助 BI 环闭合）

## Why

查询契约（wire-dashboard-query-contract）让刷新确定性化，但契约只能由 AI 生成时
落盘——人类在编辑器里看到的只是只读徽标。要建「自己搭看板 → 绑数据 → 定时
确定性刷新」的自助闭环，客户必须能在编辑器里直接把面板绑到自己的数据集上，
并在保存前试取数据看到效果。

## What Changes

- **契约试取端点**：`POST /api/v1/artifacts/query-preview`（console 身份）——
  入参 = 面板 query 同形（dataset_id/filters/sort/limit/columns），复用契约
  刷新的同一执行路径（parse_panel_query → _query_dataset → _build_snapshot，
  datasets 库会话 + 引擎工厂），返回试取快照 `{snapshot:{columns,rows}}` +
  归一后 query——**所见即所刷**（试取与刷新走同一条代码路径，杜绝两口径）。
- **编辑器绑定表单**：面板详情区「绑定查询」段——数据集选择（本企业列表，
  带 tier/行数）、列勾选（=投影，按数据集列序）、过滤行（列+算子白名单+值）、
  排序（列+方向）、limit；「试取数据」按钮调试取端点把结果写进面板快照
  （本地预览即所得）；「解绑」清除 query；保存走既有版本链（query 随面板
  落盘）。kpi 面板给「sort 最近一期 + limit 1 + 投影到值列」的约定提示。
- 面板无绑定时该段显示「未绑定」（现状不回归）。

## Impact

- **specs**：dashboard-renderer ADDED「契约表单化绑定」；dashboard-refresh
  ADDED「契约试取端点」。
- **api**：artifacts/routes.py 新端点（复用 query_contract 模块，零新逻辑）。
- **web**：DashboardEditor 绑定表单段 + api client queryPreview + i18n。
- **兼容**：纯新增；既有徽标/解析/保留语义不动。

## Non-Goals

- aggregate/join 类高级查询入表单（契约 v0 rows 模式边界不变）。
- 跨面板联动/参数化（全局筛选器为后续看板 v3 事）。
- 试取结果自动保存（试取只进本地预览，保存仍走显式保存）。
