# Tasks: wire-transform-pipeline-view

## 1. api 管线图端点

- [x] 1.1 `api/fd_wire_api/pipeline/`（routes + graph 组装）：查询本企业
  Dataset 全量 + materialization 定时登记（有→schedule 布尔/cron），edges
  由 source_kind='derived' 的 source_ref 解析父 key（环防护：成环边跳过计
  anomalies；父 key 无对应行→孤儿边跳过计 anomalies）；拓扑深度 BFS；节点
  500 上限 truncated；`require_console_customer_id` 身份
- [x] 1.2 app 挂载 + 测试：派生链成图（深度/边 op）、空图、企业隔离、
  环防护、孤儿边、truncated、401 未登录

## 2. web 管线视图

- [x] 2.1 `console/pipeline/PipelineView.tsx`：深度分列布局 + 节点卡
  （名称/行数/更新时间/tier 徽标/调度徽标/kind 标注）+ SVG 连线 + 节点
  点击跳数据集详情 + 空态 + 窄屏分组列表退化
- [x] 2.2 数据页挂「管线」子视图 tab；api client `getPipelineGraph`；
  i18n 三语文案
- [x] 2.3 测试：分层布局渲染、空态、节点跳转、徽标呈现、窄屏退化

## 3. 收口

- [x] 3.1 api pytest + web vitest + tsc 全绿
- [x] 3.2 生产发布与实弹验证（graph 端点 200 带真实节点、视图渲染）
