## Why

柏讯已经有一套完整的市场内核（四档可见性、订阅/审批、复制、渲染、看板市场三页
签），但它**没有一张对外的橱窗**：市场列表与详情一律要求登录（
`require_console_customer_id`），匿名访客进不去；官网 `/indicators` 与 `/data`
只是构建期快照 JSON，不是可浏览的看板。

与此同时，`finddata/dashboards/` 下五套成品看板（宏观景气、六国对比、A 股截面、
区域格局、平安财务）与 `fd-industry-data` 的 108 个源单元（乘用车、钢铁、有色、
油价、半导体、物流、猪价、票房、医药、气象、航运…）全部**够不着柏讯**——数据、
图表、成品俱在，唯独没有上架通道。上一变更（`wire-chart-contract-v2`）已让契约
容得下它们（`raw_option` 直通，16/16 张成品图实测渲染通过），现在缺的是货架。

对外推广最需要的东西正是这个：**一个能指给客户看的、活的、有真数字的页面**。
它同时解决三件事——官网有内容、柏讯有入口、销售有话术。

## What Changes

- 新增**平台自营发布方**概念：平台自身作为一个特殊客户租户
  （`finddata-official`），其发布的看板是平台自有资产，与客户资产走同一套产物
  /版本/可见性/订阅机制，但**可被匿名只读浏览**。
- 新增**匿名只读橱窗端点**：严格的平台租户白名单——只返回平台自营发布方发布
  的 `market` 档看板，只含已发布版本的完整 spec（含快照，即图表本身要画的
  数据）。客户发布的任何产物**永不**出现在该端点；客户数据的 Market 面仍保持
  登录门禁不变。
- 新增**行业看板批量导入器**：把 `dashboards/*.spec.json`（完整 echarts option）
  转成 v2 的 `raw_option` 面板并上架为平台自营看板，一次命令完成；幂等（按
  slug upsert）、可重复执行、失败逐条报告不中断整批。
- 柏讯前端新增公开**橱窗页**（`#/showcase`）：匿名可浏览已上架看板列表与详情，
  复用既有 `DashboardRenderer`（只读、无工具栏——分享页红线同款）。
- 市场面新增「平台自营」标识：登录客户在市场里能看出哪些是平台发布的（这部分
  走既有登录市场，不是新面）。
- **红线改写（明确且有界）**：`artifact-marketplace` 的「市场面不向匿名开放」
  由「一律登录」改为「客户产物一律登录；平台自营产物可匿名只读」。核心意图
  不变——**客户私有数据绝不被动泄露**，只有平台自有内容对外。
- 刷新：平台自营看板可由平台侧触发刷新（沿用既有刷新链路），刷新结果自动反映
  到匿名橱窗（钉最新已发布版本）。

## Capabilities

### New Capabilities

- `showcase-market`：平台自营发布方与匿名只读橱窗——平台租户身份、匿名端点
  的白名单边界与返回契约、行业看板批量导入的可重复语义、橱窗页的只读红线。

### Modified Capabilities

- `artifact-marketplace`：市场准入由「一律登录」修订为「客户产物登录；平台自营
  产物可匿名只读」，并新增平台自营发布方的标识与判定口径。

## Impact

- 后端：`api/fd_wire_api/market/`（新增 showcase 浏览服务与匿名路由）、
  `api/fd_wire_api/billing/`（平台租户常量与判定）、`api/fd_wire_api/artifacts/`
  （导入器复用既有 create/update/publish/visibility 链路，不改其契约）。
- 前端：`web/src/views/ShowcaseView.tsx`（新）、`web/src/App.tsx`（路由
  `#/showcase`，匿名可达）、`web/src/console/MarketPage.tsx`（平台自营徽标）。
- 官网：`fd-official-web`（消费橱窗快照，静态生成页面）——属后续变更
  `official-web-showcase`，本变更只提供端点与稳定 JSON 形状。
- 导入脚本：`api/scripts/import_showcase_dashboards.py`（读
  `finddata/dashboards/*.spec.json`，转 `raw_option` 面板，幂等 upsert）。
- 数据：新增一行平台租户（迁移或启动期引导）；不新增表结构以外的状态。
- 依赖：无新增第三方库。
