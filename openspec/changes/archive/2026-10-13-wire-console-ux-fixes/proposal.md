## Why

生产反馈五件问题集中暴露在门户/控制台/内部后台三面上：①控制台「我的数据」白屏——`GET /api/v1/assets` 后端平铺、前端按嵌套 `asset:{...}` 读，渲染 TypeError 且全应用零 ErrorBoundary，一炸全站卸载；②内部后台给客户指派档位必报错——新建表单默认档写死已禁用的旧档 starter，下拉又把三个禁用旧档无差别渲染，选中即 409「已禁用（不可指派）」；③内部后台「本地数据」页必报错——`/internal/datasets/overview` 要 `X-Internal-Token` 而 nginx 只注入 `X-Admin-Token`，无人能过鉴权；④控制台紧凑页脚悬在文档流尾部不贴底；⑤内部后台与门户共用同一壳层，不符合「同一登录入口、登录后按角色进不同界面」的管理后台惯例。趁问题刚暴露、客户面尚未放量，一次收敛。

## What Changes

- **白屏根治**：后端 `GET /api/v1/assets` 条目元数据改回嵌套 `asset:{...}`（回契约注释口径，api 测试同步改断言）；前端 `DatasetCard` 对 `item.asset` 缺失做容错缺省；web 假后端与 api 测试自此同形状对拍。
- **错误边界**：App 全局一层 + ConsoleView 内容区一层 ErrorBoundary（class 组件），子页异常只降级该页并给重试按钮，不再整站白屏。
- **控制台去页脚**：`#/console/*`（含需求单占位子页）不再渲染任何页脚；门户页全量页脚不变；admin 零页脚不变。`ConsoleFooter` 组件退役。
- **档位指派修复**：客户列表档位下拉选项集 = 活跃档 ∪ 该客户当前档，禁用档置灰标注「（已禁用）」（仍可见，符合 portal-pricing「内部档位列表仍可看到旧三档及其禁用标记」）；新建客户表单取消写死默认档，未选不能提交。
- **本地数据页鉴权并闸**：`/internal/datasets/overview` 鉴权从 `require_internal`（X-Internal-Token）换为 admin 双闸 `require_admin_access`（X-Admin-Token + Bearer 含 wire-admin 角色），与 `/internal/admin/*` 同闸；nginx 注入不变、前端零改动。
- **角色分流落地 + 后台独立壳**：登录会话建立后按角色落地——含 wire-admin 落 `#/admin`，否则维持 `#/console`；admin 路由摆脱门户壳（站点顶栏/门户导航不再包裹 `#/admin/*`），AdminLayout 自带极简顶栏（品牌字 + 返回门户 + 身份 + 登出），门户顶栏对管理员保留「内部后台」入口互跳。

## Capabilities

### New Capabilities

（无——五项修复均落在既有能力的 requirement 变更或实现细节内。）

### Modified Capabilities

- `portal-shell`: 「控制台 SHALL 展示紧凑页脚」改为「控制台 SHALL NOT 渲染页脚」（页脚三形态收敛为门户全量/工作台无页脚两形态）；新增「内部后台 SHALL 独立成壳」——admin 不被门户顶栏包裹，自带极简顶栏。
- `console-auth`: 新增「登录落地 SHALL 按角色分流」——wire-admin 登录后落内部后台，普通客户落控制台；「内部后台角色门禁（双闸）」的闸覆盖面扩至运营只读面 `/internal/datasets/overview`。

## Impact

- **web/**：`api/fd_wire_api/assets/routes.py`（嵌套回契约）、`datasets/connector_routes.py`（overview 并闸）、`App.tsx`（页脚分发简化 + admin 摘壳）、`views/ConsoleView.tsx`（ErrorBoundary + 分流）、`console/DatasetsPage.tsx`（asset 容错）、`admin/CustomersPage.tsx`（下拉过滤 + 默认值）、`admin/AdminLayout.tsx`（极简顶栏）、`components/ConsoleFooter.tsx` 退役、`i18n/zh.ts`+`en.ts`。
- **api 测试**：test_assets_api.py 断言改嵌套形状；overview 鉴权测试改双闸矩阵。
- **web 测试**：testing.ts 假后端不变（本就嵌套）；补 ErrorBoundary 降级、分流落地、下拉过滤三组用例。
- **部署**：无 env/secret 变更，nginx 不动；api+web 双镜像一次滚动。
- **不涉及**：根仓 `wire-asset-catalog` spec（响应形状是实现细节，spec 钉的行为不变）；`portal-pricing` spec（409 拒绝与旧档可见场景均保持）。
