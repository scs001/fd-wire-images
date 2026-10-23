## 1. 后端：目录治理列表分页

- [x] 1.1 为 `api/fd_wire_api/admin/routes.py` 的 `catalog_pending` 增加 `limit`/`offset` 查询参数，单页上限 100（与 `catalog/service.py` 公开目录对齐），验证：手工请求 `limit=5` 返回 5 条且 `total` 为匹配总数
- [x] 1.2 响应信封改为 `items` + `total`（`CatalogPendingOut` 调整），验证：`GET /internal/admin/catalog/pending?status=verified&limit=5` 返回 `{items:[...5 条], total: 3421}` 形状
- [x] 1.3 补后端测试：分页不重不漏（相邻页并集等于按总数枚举的前 N 条）、超限请求按上限返回、非法 status 仍 422，验证：`pytest api/tests/test_admin_api.py -k catalog` 全绿
- [x] 1.4 补后端测试：两种状态（verified / registered）均按页返回且带 `total`，验证：新增断言覆盖两种状态，`pytest` 通过

## 2. 前端：目录治理分页呈现

- [x] 2.1 更新 `web/src/admin/api.ts` 的 `CatalogPendingResponse` 类型与 `fetchCatalogPending`，携带分页参数，验证：`tsc --noEmit` 无类型错误
- [x] 2.2 `web/src/admin/CatalogGovernancePage.tsx` 改为分页呈现（上一页/下一页 + 共 N 页，复用 `DatasetsPage` 既有翻页风格），验证：已认证态首屏只渲染一页条目，翻页可取后续页
- [x] 2.3 补前端测试：`CatalogGovernancePage` 分页渲染与翻页、空态与错误态不受分页影响，验证：`vitest run` 相关用例全绿
- [x] 2.4 浏览器实测目录治理页两状态：下滑不再出现内容缺失，验证：verified 与 registered 两态各自翻页到末页，条目全程可见且无白屏

## 3. 前端：管理员控制台可达性

- [x] 3.1 `web/src/admin/AdminLayout.tsx` 顶栏新增指向 `#/console` 的出口（与既有「返回门户」并列），验证：后台任意页点击该出口进入控制台
- [x] 3.2 调整 `web/src/console/ConsoleView.tsx` 分流分支：保留管理员裸登录落后台，但确保控制台对管理员会话可达，验证：管理员点击出口后进入控制台，看板/产物/我的数据/实体/用量/团队/市场均可达
- [x] 3.3 核对 `web/src/views/DocsView.tsx` 快速开始区块「前往控制台」入口对管理员会话可达，验证：以管理员会话点击该入口到达控制台而非被改跳后台
- [x] 3.4 回归三个既有分流场景：管理员裸登录落后台、普通客户落地不变、显式子路径（#/console/usage）不被劫持，验证：三个场景逐条复验通过，无回归
- [x] 3.5 补前端测试覆盖新增场景（后台可进控制台、文档页入口可达、管理员落地后门户导航照常呈现），验证：`vitest run` 相关用例全绿

## 4. 收口

- [x] 4.1 跑双语键值一致性测试与全量前端测试，验证：`vitest run` 全绿
- [x] 4.2 跑后端测试套件，验证：`pytest` 无新增失败
- [x] 4.3 跑 `openspec validate wire-admin-console-routing --strict`，验证：输出 valid
