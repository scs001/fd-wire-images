## Why

持有 `wire-admin` 角色的登录会话从裸 `#/console` 进入时被无条件改写至 `#/admin`，而 `#/admin` 壳的所有出口均指向门户首页 —— 管理员身份因此完全不可达控制台（看板、产物、我的数据、实体、用量、团队、市场），且门户文档页的「前往控制台 →」（`href="#/console"`）在跳转后顶部导航整体消失。同时内部后台目录治理页的指标列表接口全量返回、无分页阈值（生产实测 verified 3,421 条 ≈ 1.0 MB、registered 54,132 条 ≈ 16 MB），呈现层一次性渲染全量卡片，已认证态下滑出现内容缺失。

## What Changes

- 角色分流改为「可回退」而非「单向改写」：管理员落后台后仍保有进入控制台的显式出口，角色分流不再抹掉管理员作为客户的控制台可达性。
- 内部后台壳新增进入控制台的导航出口（与既有「返回门户」并列）。
- 文档页链向控制台的入口对管理员会话保持可达（导航呈现不因落后台而消失）。
- 目录治理列表接口新增分页（limit/offset），与公开目录列表的既有分页契约对齐；呈现层改为分页浏览，不再一次渲染全量条目。
- **BREAKING**：`GET /internal/admin/catalog/pending` 响应体由裸数组语义的全量列表改为分页信封（items + total + 分页游标），调用方需适配。

## Capabilities

### New Capabilities

- `catalog-governance`：内部后台目录治理面的行为——指标镜像列表的只读浏览与分页、审核意见 append-only 留档；镜像仅同步管道可写，状态修改在权威库侧完成。

### Modified Capabilities

- `console-auth`：修改「登录落地 SHALL 按角色分流」——分流后管理员 SHALL 仍可达控制台，且门户文档页链向控制台的入口 SHALL 不因此失去导航呈现。

## Impact

- 前端：`web/src/console/ConsoleView.tsx`（角色分流）、`web/src/admin/AdminLayout.tsx`（后台壳出口）、`web/src/views/DocsView.tsx`（快速开始区块控制台入口）、`web/src/admin/CatalogGovernancePage.tsx`（分页呈现）、`web/src/admin/api.ts`（分页请求契约）。
- 后端：`api/fd_wire_api/admin/routes.py`（`catalog_pending` 分页与响应信封）、`api/fd_wire_api/catalog/service.py`（可复用既有分页查询契约）。
- 契约：`GET /internal/admin/catalog/pending` 响应形状变更（BREAKING），需同步 `admin/api.ts` 类型与既有测试。
