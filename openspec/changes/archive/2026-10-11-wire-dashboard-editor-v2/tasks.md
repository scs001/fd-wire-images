# Tasks — wire-dashboard-editor-v2

- [x] 1.1 spec.ts 纯函数：convertPanelType(panel, target)（snapshot 原样、kpi↔图表类带 needConfirm 标记）与 duplicatePanel(panel)（新 id+「副本」标题+深拷贝 snapshot）；单测
- [x] 1.2 DashboardEditor：面板操作组收纳（上移/下移/span/复制/转换菜单/删除）；类型转换含 kpi 确认；source 文本编辑（提示=刷新依赖标注）；config 表单按 type 展开（kpi unit/subtitle、line/bar xName/yName）；未保存变更计数
- [x] 1.3 i18n ZH/EN（console.dashboard.editor.* 扩展）；组件测试：转换保数据/复制新 id/kpi 确认流/config 表单分发/未保存计数
- [x] 1.4 全量 web 测试绿+tsc 干净；既有编辑器测试零回归
