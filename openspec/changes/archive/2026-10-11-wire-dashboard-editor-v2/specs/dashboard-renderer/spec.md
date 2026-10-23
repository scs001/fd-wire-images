## MODIFIED Requirements

### Requirement: 基础编辑器

客户可对面板做增/删/改标题/移序/调跨度操作，kpi 面板可编辑值/单位/副标题；
编辑实时预览；保存走既有产物 PUT 端点产生新版本；未保存离开有提示。
v2 扩展：**图表类型互转**（line↔bar↔table 保 snapshot 零数据损失；kpi 与图表类
互转需确认——kpi 只取首值有信息取舍）、**面板复制**（新 id+副本标题+深拷贝）、
**source 与 config 编辑**（source 文本提示"填指标 code/数据集名，刷新依赖此标
注"；config 表单按 type 展开：kpi=单位/副标题，line/bar=x 轴名/y 轴名）、
未保存变更计数展示。

#### Scenario: 编辑即版本
- **WHEN** 客户编辑面板标题并保存
- **THEN** 产生新版本号，重新打开看到新标题，历史版本仍可查

#### Scenario: 类型互转保数据
- **WHEN** 客户把折线面板转为表格
- **THEN** snapshot 原样保留，表格呈现同一数据，保存后新版本生效

#### Scenario: 面板复制
- **WHEN** 客户复制某面板
- **THEN** 出现新面板（副本标题、独立 id、相同数据），可独立编辑
