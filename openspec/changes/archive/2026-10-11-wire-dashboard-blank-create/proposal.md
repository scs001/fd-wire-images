# Proposal: wire-dashboard-blank-create — 空白看板新建（免费档第一块板）

## Why

E2E 走查前置核实发现真缺口：产物只有「会话草稿转正」一条出生路径，而草稿
只能由 AI 构建产生、AI 构建要求 cell——**免费档客户造不出任何看板产物**。
渲染/编辑/契约绑定/刷新/定时/导出/分享七轮能力对免费档的第一块板全部
够不着。补「空白新建」出生路径，免费档自助 BI 环才真正闭合。

## What Changes

- **空白建板端点**：`POST /api/v1/artifacts`（console 身份）body
  `{kind:"dashboard", title}` → 创建 draft 产物 v1，spec = 最小空板
  `{title, spec:{title, panels:[]}}`；kind 白名单 v0 仅 dashboard（其他
  kind 仍走 AI 草稿）；title 必填非空（422）；跨客户语义不变（产物域内）。
- **控制台入口**：产物页「新建看板」按钮 → 标题输入 → 创建即跳编辑器
  （编辑器已有加面板/绑定/试取全链）。
- 既有 from-draft 路径与状态机零改动。

## Impact

- **specs**：artifact-lifecycle MODIFIED「产物持久化」（补第二条出生路径
  场景，既有场景原样携带）。
- **api**：artifacts/routes.py + service（薄：构造最小 spec 走既有建档）。
- **web**：ArtifactsPage 按钮 + api client + i18n。
- **兼容**：纯新增出生路径。

## Non-Goals

- 其他 kind 的空白创建（composite_indicator 等结构强，仍 AI 产）。
- 建板模板库/复制既有板（后续）。
