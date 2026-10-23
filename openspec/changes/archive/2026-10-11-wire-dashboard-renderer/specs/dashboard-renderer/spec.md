## Purpose

定义仪表盘产物的 v1 契约与控制台渲染/编辑：dashboard spec 从自由 JSON 升级为
可渲染结构（面板×快照数据），客户在产物页直接看见仪表盘并进行基础编辑（增删
面板/改标题/调布局），保存复用产物版本链——使 AI 生成的仪表盘从"JSON 文件"
变成"看得见的看板"，为方向 B（自定义看板与分享）铺第一块可视地基。

## ADDED Requirements

### Requirement: 仪表盘 spec v1 契约

dashboard 产物 spec 采用 v1 结构：标题 + 面板数组；每个面板含类型
（kpi/line/bar/table）、标题、栅格跨度、来源标注与**内嵌快照数据**
（columns+rows）；前端提供宽容校验——结构不合法的面板跳过并标注，不拒绝
整个看板；无 v1 结构的旧自由 JSON 显示为旧版格式只读视图。

#### Scenario: 合法看板渲染
- **WHEN** 打开含 kpi/line/bar/table 四类面板的 dashboard 产物
- **THEN** 四类面板按栅格布局渲染，数据来自面板快照

#### Scenario: 坏面板隔离
- **WHEN** 看板中某面板 snapshot 形状不合法
- **THEN** 该面板显示问题标注，其余面板正常渲染

### Requirement: 快照渲染

渲染以面板内嵌快照为数据源：kpi 呈现大数卡（值/单位/副标题），line/bar 以
echarts 渲染（x/y 轴依快照列推断），table 呈现列表（长表滚动）；空快照如实
显示无数据占位；渲染器标注快照口径（实时刷新属后续物化能力）。

#### Scenario: 空数据诚实呈现
- **WHEN** 面板快照为空
- **THEN** 显示无数据占位而非空白或报错

### Requirement: 基础编辑器

客户可对面板做增/删/改标题/移序/调跨度操作，kpi 面板可编辑值/单位/副标题；
编辑实时预览；保存走既有产物 PUT 端点产生新版本；未保存离开有提示。

#### Scenario: 编辑即版本
- **WHEN** 客户编辑面板标题并保存
- **THEN** 产生新版本号，重新打开看到新标题，历史版本仍可查

### Requirement: 产物页入口

产物页对 dashboard kind 提供「查看看板」入口（渲染态）并可切换编辑态；其他
kind 的产物页行为不变。

#### Scenario: 入口按 kind 分发
- **WHEN** 客户在产物页打开 dashboard 产物
- **THEN** 呈现看板视图入口；composite_indicator 等其他 kind 不出现该入口
