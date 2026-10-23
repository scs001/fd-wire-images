# wire-dashboard-renderer

## Why

产物转正（wire-artifacts-persistence）后，dashboard 产物已有持久化与版本，但
**没有渲染器**——仪表盘 spec 仍是自由 JSON，客户在控制台看不到图。这是
ROADMAP 方向 B（应用平台）当头炮的第二步：AI 生成 → **客户看得见** → 客户能改。
本 change 定义 dashboard spec **v1 契约**、实现快照渲染器与基础编辑器。

## What Changes

- **dashboard spec v1 契约**（前端 TypeScript 类型 + 轻量校验函数）：
  `{ title, panels: [...] }`；panel = `{ id, type: "kpi"|"line"|"bar"|"table",
  title, span(1|2|3 栅格), source(来源标注: 指标 code/数据集名，展示用),
  snapshot: { columns: string[], rows: (string|number|null)[][] },
  config(各 type 的少量选项: 单位/y 轴名等) }`。
  **快照数据内嵌**：AI 生成仪表盘草稿时把已取数结果嵌进 snapshot（数据值没有
  REST 通道——指标序列在 MCP 面，实时取数=后续物化执行 change 的事）。
- **渲染器**：echarts（已在依赖）渲染 line/bar；kpi 大数卡；table 简表；
  栅格布局；空 snapshot 的 panel 如实显示「无数据」。非法 panel 跳过并标注，
  不整板失败。
- **编辑器 v1**：面板增/删/改标题/左右移序/调 span；kpi 值与副标题可编辑；
  JSON 视图可查看（只读）；保存走既有 PUT（新版本）。不做图表类型切换与
  数据绑定编辑（v2）。
- **产物页集成**：dashboard kind 的产物卡片加「查看看板」，抽屉/全屏打开
  渲染器；编辑模式内嵌。
- **兼容**：自由 JSON 的历史 spec（无 panels 结构）显示「旧版自由格式」+
  JSON 查看，不崩不拒。

## Capabilities

### New Capabilities

- `dashboard-renderer`: 仪表盘 spec v1 契约、快照渲染（kpi/line/bar/table+
  栅格）、基础编辑器（面板增删改排）、产物页入口、自由 JSON 旧 spec 兼容。

### Modified Capabilities

- `artifact-lifecycle`: 产物页对 dashboard kind 提供看板视图入口（渲染/编辑
  复用既有保存与版本链）。

## Impact

- **fd-wire/web**：新组件（DashboardRenderer/DashboardEditor/spec 校验）、
  ArtifactsPage 集成、i18n ZH/EN、组件测试。
- **不碰**：api（零后端改动——spec 校验在前端，保存走既有端点）、AI 生成链路
  的工具 schema（AI 侧仍自由 JSON，前端宽容渲染；生成侧引导遵循 v1 契约
  =后续 AI prompt change）。
