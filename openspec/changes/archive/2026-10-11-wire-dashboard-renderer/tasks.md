# Tasks — wire-dashboard-renderer

## 1. spec v1 契约与校验（web）

- [x] 1.1 `web/src/console/dashboard/spec.ts`：DashboardSpec/Panel/Snapshot 类型 + `parseDashboardSpec(raw): {ok, spec?, errors?}` 轻量校验（panels 数组、type 枚举、span 枚举、snapshot 形状宽容——列行数不齐截断标注不拒收）；单测覆盖合法/非法/旧自由 JSON 三态
- [x] 1.2 面板 id 生成器与稳定排序工具（编辑器增删移序用）

## 2. 渲染器（web）

- [x] 2.1 DashboardRenderer：3 列栅格；kpi 大数卡（值/单位/副标题）；line/bar 用 echarts（title 轴/legend 基本配置，dimensions 依 snapshot columns 推断 x/y）；table 简表（列头+行，超 20 行滚动）；空 snapshot 显「无数据」占位
- [x] 2.2 容错：非法 panel 单卡标注跳过不整板失败；旧自由 JSON 整体显「旧版自由格式」+ 只读 JSON 查看
- [x] 2.3 渲染器组件测试：三类面板渲染、空数据占位、坏 panel 隔离

## 3. 编辑器 v1（web）

- [x] 3.1 DashboardEditor：面板增（选 type 起始模板）/删/改标题/左移右移/span 切换；kpi 值+单位+副标题表单编辑；编辑态实时预览复用渲染器
- [x] 3.2 保存走既有 PUT（成功提示新版本号，沿用产物页交互）；未保存离开提示（沿用现有缓冲模式）
- [x] 3.3 编辑器组件测试：增删移改四操作 + 保存调用断言

## 4. 产物页集成（web）

- [x] 4.1 ArtifactsPage：dashboard kind 卡片「查看看板」入口（抽屉或路由内嵌），查看态渲染器+「编辑」切换编辑器；非 dashboard kind 行为零变化
- [x] 4.2 i18n ZH/EN 全部新键双语；快照口径说明（「快照数据，物化刷新后续开通」如实标注）

## 5. 验证

- [x] 5.1 E2E（组件级）：含三面板的 dashboard 产物 → 查看渲染三类图 → 编辑改标题保存 v2 → 重新查看新版本生效
- [x] 5.2 全量 web 测试绿 + tsc 干净；既有产物页测试零回归
