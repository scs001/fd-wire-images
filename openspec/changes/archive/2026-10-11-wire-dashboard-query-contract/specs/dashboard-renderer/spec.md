# dashboard-renderer 变更（wire-dashboard-query-contract）

## ADDED Requirements

### Requirement: 面板查询契约

面板新增可选 `query` 引用（与数据面 query 端点入参同形同界）：
`{dataset_id, filters?, sort?, limit?, columns?}`——filters 算子集合
eq/in/gt/gte/lt/lte/contains/startswith、sort 方向 asc/desc、limit 1..1000、
columns 投影列序即快照列序。前端宽容归一：非法 query 整体丢弃（面板仍按
快照渲染，不受影响）；编辑器增删/移序/复制/类型互转一律保留 query 不抹掉，
面板详情区显示「已绑定查询」徽标（含 dataset_id）；渲染数据源仍是快照
（query 是刷新契约，不是渲染输入）。

#### Scenario: 契约面板渲染
- **WHEN** 打开带 query 引用与快照的面板
- **THEN** 渲染使用快照数据（契约不参与渲染），面板显示已绑定查询徽标

#### Scenario: 非法契约宽容降级
- **WHEN** 面板 query 形状不合法（如算子不在白名单、dataset_id 缺失）
- **THEN** 该 query 被丢弃，面板按快照正常渲染，看板不整板失败

#### Scenario: 编辑保留契约
- **WHEN** 客户对带 query 的面板改标题/调跨度/复制/类型互转后保存
- **THEN** 新版本面板的 query 原样保留

### Requirement: AI 生成侧契约产出

`dashboard_spec_create` 工具指令要求：面板数据取自数据集查询工具时，面板
必须同时携带 `query` 引用（所用 dataset_id 与查询形参如实落盘）；kpi 面板
取单值采用 rows 模式约定（sort 到最近一行 + limit 1 + columns 投影到值列）；
`source` 仍作展示标注保留。

#### Scenario: 生成即带契约
- **WHEN** AI 经数据集查询工具取数生成看板面板
- **THEN** 该面板 spec 同时含 query 引用与展示用 source

## MODIFIED Requirements

### Requirement: 仪表盘 spec v1 契约

dashboard 产物 spec 采用 v1 结构：标题 + 面板数组；每个面板含类型
（kpi/line/bar/table）、标题、栅格跨度、来源标注与**内嵌快照数据**
（columns+rows），可选携带**查询契约引用**（query，见「面板查询契约」）；
前端提供宽容校验——结构不合法的面板跳过并标注，不拒绝
整个看板；无 v1 结构的旧自由 JSON 显示为旧版格式只读视图。

#### Scenario: 合法看板渲染
- **WHEN** 打开含 kpi/line/bar/table 四类面板的 dashboard 产物
- **THEN** 四类面板按栅格布局渲染，数据来自面板快照

#### Scenario: 坏面板隔离
- **WHEN** 看板中某面板 snapshot 形状不合法
- **THEN** 该面板显示问题标注，其余面板正常渲染
