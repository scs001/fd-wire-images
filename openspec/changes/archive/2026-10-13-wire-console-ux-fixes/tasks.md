## 1. 白屏根治：/assets 契约回嵌套 + 前端容错

- [x] 1.1 api `list_assets`（fd_wire_api/assets/routes.py）条目元数据从平铺改为嵌套 `asset:{domain,tags,description,cadence_hint,owner_note,sensitivity,cadence_effective}`（六字段快照+cadence_effective 一并包入；摘要字段 id/name/source_kind/row_count/storage_tier/created_at/updated_by/updated_at 留平铺）。验证：test_assets_api.py 全部断言改嵌套后全绿，含「目录条目含嵌套 asset 段」「未补全条目 asset 内为默认值」。
- [x] 1.2 web `DatasetCard`/`DatasetDetail` 对 `item.asset` 缺失容错：`item.asset?.sensitivity` 等安全读法，asset 段缺席时资产元数据行整体不渲染（列表不断、徽章行降级）。验证：web 测试补「asset 缺失不渲染元数据行且列表正常」一例。
- [x] 1.3 契约注释同步：web api.ts 资产目录契约注释与 `AssetCatalogItem` 保持嵌套口径（本次不动），api 侧 routes docstring 补一行响应形状说明。验证：两侧注释/类型/假后端三处形状一致人工核对。

## 2. 错误边界：全局 + 控制台内容区

- [x] 2.1 新增 `components/ErrorBoundary.tsx`（class 组件，getDerivedStateFromError + componentDidCatch console 留痕 + 重试按钮重挂载 children），双语错误卡文案入 i18n。验证：组件测试——正常渲染直通、子异常呈现错误卡、点重试重建子树。
- [x] 2.2 App 全局包裹一层 ErrorBoundary（renderRoute 外围）；ConsoleView 在 ConsoleLayout children 外围再包一层（内容区层）。验证：web 测试——控制台子页抛异常时侧栏/顶栏仍在且该页签呈现错误卡（局部降级），门户顶层异常呈现全页错误卡（全局兜底）。

## 3. 控制台去页脚

- [x] 3.1 App.tsx 页脚分发简化：`#/console/*` 与 demandNew 不再渲染 ConsoleFooter（门户全量、admin 无页脚维持）；`ConsoleFooter.tsx` 及其测试退役删除；SiteFooter 不动。验证：路由测试——console/demandNew 无 footer、四门户页 footer 在、admin 无 footer；i18n 孤键清理。
- [x] 3.2 portal-shell spec delta 对照验收：控制台无页脚 + 备案口径仍在门户页两场景。验证：与 3.1 测试同批覆盖。

## 4. 内部后台修复：档位指派 + 本地数据并闸

- [x] 4.1 CustomersPage 档位下拉改为「活跃档 ∪ 该客户当前档」，禁用档置灰（disabled）标注「（已禁用）」；当前档为禁用旧档时仍可见并选中。验证：web 测试——禁用档置灰不可选、存量禁用档客户的当前档可见、切换到活跃档成功提交。
- [x] 4.2 新建客户表单取消写死默认档 starter：空默认 + 未选档时提交按钮禁用。验证：web 测试——默认不可提交、选活跃档后可提交、提交 payload 带所选档。
- [x] 4.3 api `internal_overview`（datasets/connector_routes.py）鉴权从 `require_internal` 换 `require_admin_access`（admin/auth.py 双闸），路由归属与 OpenAPI tag 同步整理。验证：api 测试改双闸矩阵——双全放行、无 Bearer 401、无角色 403、令牌缺失 401/403、viewer 身份 403；X-Internal-Token 不再被接受。
- [x] 4.4 deploy/README 内部面段补一行：`/internal/datasets/overview` 已并入 admin 双闸（X-Internal-Token 失效）。验证：文档核对（无 env/部署变更）。

## 5. 分流落地 + 后台独立壳

- [x] 5.1 ConsoleView 会话建立（ready）裸 #/console 且 `hasAdminRole` 时改跳 `#/admin`：判定先于既有偏好落地序（pinned/landing_tab），程序跳转 suppress 不回写 landing_tab；显式子路径直达不劫持。验证：web 测试——管理员裸登录落 admin（且 landing_tab 偏好不被覆盖）、普通客户落地序不变、管理员直达 #/console/usage 停留原页。
- [x] 5.2 App 壳对 route=admin 不渲染站点 header（门户顶栏/页签/语言切换），footer 维持无；AdminLayout 顶部新增极简顶条（品牌字 + 返回门户链接 + 身份 + 登出），无会话（角色门禁拦截）时顶条仍渲染品牌与返回链接。验证：web 测试——#/admin 无门户顶栏、顶条含返回门户、门禁拦截卡场景顶条仍在。
- [x] 5.3 i18n zh/en 补齐后台顶条与错误边界文案键。验证：i18n 键完整性测试（zh/en 键集一致）。

## 6. 回归、一致性门与发布

- [x] 6.1 api 全量 pytest + web 全量 vitest + tsc 0 错误；两侧 /assets 形状对拍核对（假后端与 api 测试同形状）。验证：CI 本地全绿。
- [x] 6.2 生产冒烟五件：真客户账号「我的数据」列表正常（有数据集客户）；admin 改档成功且禁用档置灰；admin 本地数据页 200 出数；管理员裸登录落 #/admin；普通账号登录仍落 #/console 且无后台入口、控制台无页脚。验证：实弹截图/请求留痕入 reports/。
- [x] 6.3 api+web 双镜像同批滚动（Jenkins→TCR→ArgoCD 常规通道），回滚预案=回滚双镜像。验证：ArgoCD 同步健康 + 冒烟过。
