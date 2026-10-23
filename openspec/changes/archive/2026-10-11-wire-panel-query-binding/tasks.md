# Tasks: wire-panel-query-binding

## 1. api 试取端点

- [x] 1.1 artifacts/routes.py `POST /api/v1/artifacts/query-preview`：入参
  PanelQuery 同形 → parse_panel_query（None→422）→ refresh 同路径
  （_query_dataset + _build_snapshot，datasets 库会话 + 引擎工厂依赖）→
  {snapshot, query}；跨客户 404；失败 400 带原因（引擎拒绝/列不存在）
- [x] 1.2 测试：合法试取（api 档零引擎）、非法 422、跨客户 404、列不存在
  400、cell 档引擎路径（mock 工厂）

## 2. web 绑定表单

- [x] 2.1 DashboardEditor「绑定查询」段：数据集选择/列勾选/过滤行/排序/
  limit 表单；试取按钮（queryPreview）→ 本地快照预览；解绑；回填已有
  query；kpi 约定提示；api client queryPreview + i18n 双语
- [x] 2.2 测试：绑定表单交互、试取写快照、解绑清 query、回填、保存带
  query、422/400 错误如实呈现

## 3. 收口

- [x] 3.1 api pytest 全量 + web vitest 全量 + tsc 全绿
- [x] 3.2 生产发布与实弹验证（试取端点 401 门/绑定表单 bundle 实证）
