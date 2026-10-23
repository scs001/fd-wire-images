# wire-dashboard-editor-v2

## Why

编辑器 v1 只能增删面板/改标题/调跨度——离「客户自定义看板」还差最常用的三件：
**图表类型切换**（line↔bar↔table 互转保数据）、**面板复制**、**source 与
config 字段编辑**（单位/轴名/副标题/来源标注）。本 change 补齐，编辑器达到
日常可用线。

## What Changes

- **类型切换**：面板操作加「转为折线/柱状/表格」——snapshot 原样保留（三类型
  共用同一 snapshot 形状，切换零数据损失）；kpi↔其他类型转换给出确认提示
  （kpi 只读首值，转出/转入有信息取舍，确认后执行）。
- **面板复制**：一键复制面板（新 id、标题加「副本」后缀、snapshot 深拷贝）。
- **source/config 编辑**：所有面板可编辑 source（文本，提示填指标 code/数据集
  名——刷新依赖此标注）；config 表单按 type 展开（kpi：unit/subtitle；
  line/bar：xName/yName）。
- **编辑器 UX**：面板卡片操作按钮组（上移/下移/span/复制/转换/删除）统一收纳；
  未保存变更计数展示（「N 处未保存」）。

## Capabilities

### Modified Capabilities

- `dashboard-renderer`: 编辑器支持类型互转（snapshot 保留）、面板复制、
  source/config 编辑与未保存计数。

## Impact

- **fd-wire/web**：DashboardEditor 扩展+spec.ts 辅助（convertPanelType/
  duplicatePanel 纯函数）、i18n、测试。零 api 改动（保存仍走 PUT）。
