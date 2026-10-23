## Context

三面已在产：门户（#/）、控制台（#/console/*）、内部后台（#/admin/*），单 SPA hash 路由、无 router 依赖。五件问题的代码现场（2026-10-13 诊断）：

- `GET /api/v1/assets`（api `assets/routes.py` list_assets）平铺返回元数据；web 侧 `AssetCatalogItem` 类型、契约注释（api.ts）、假后端（testing.ts）三处按嵌套 `asset:{...}`，真前端渲染 `item.asset.sensitivity` 即 TypeError；全应用零 ErrorBoundary → 整树卸载白屏。api 测试（test_assets_api.py）按平铺断言，两侧测试各自全绿——契约错位被测试镜像掩盖。
- admin 客户页：新建表单 `useState("starter")` 默认档（starter 是禁用旧档）；`GET /internal/admin/plans` 有意返回全部 6 档（含禁用），前端下拉无差别渲染，选中禁用档后端 `_assignable_plan_or_error` 409。
- `/internal/datasets/overview` 挂 datasets 面的 `require_internal`（要 `X-Internal-Token`）；nginx `/internal/` 反代只注入 `X-Admin-Token`；唯一调用方是 admin UI（admin/api.ts 注释早已预期「并入双闸通道」）。
- App 壳 `min-h-screen` 非 flex column，ConsoleFooter 在文档流尾部。
- admin 角色门禁双层在位（导航显隐 + AdminView 拦截），但 admin 由门户壳包裹，登录落地一律 `#/console`。

约束：`portal-pricing` spec 的「旧档不可指派」场景同时钉死 409 拒绝与「内部档位列表仍可看到旧三档及其禁用标记」——修复不得把禁用档从后台藏掉。根仓 `wire-asset-catalog` spec 只钉浏览/筛选/隔离行为，响应形状是实现细节，本 change 不跨仓改 spec。

## Goals / Non-Goals

**Goals:**
- 白屏根因消除（契约对齐）+ 同类故障爆炸半径收敛（错误边界）
- 内部后台恢复可用（档位指派、本地数据两页）
- 工作台界面页脚纪律与后台形态对齐管理后台惯例（单入口、双界面）

**Non-Goals:**
- admin 独立域名/独立部署（仍是同 SPA 内 #/admin 分壳）
- Logto 侧角色配置审计与发放流程梳理（现闸已双层在位，未发现角色误发）
- 根仓 wire-asset-catalog spec 的任何修改
- ErrorBoundary 的上报/监控接线（本期只做降级 UI）

## Decisions

### D1 契约错位：后端回嵌套（回契约），前端不做形状适配
`list_assets` 条目把六字段快照包进 `asset` 键（`{**摘要字段, asset: {...}}`），api 测试断言同步改。理由：三份前端工件（类型/假后端/契约注释）与文档化契约都是嵌套，平铺是后端单侧异端；改后端 diff 最小且让两侧测试首次真正对拍同形状。备选「前端改平铺读法」要动三处前端工件且背离契约注释，放弃。前端另加 `item.asset` 缺失容错（字段缺省不渲染徽章行），防单条坏数据再杀整页。

### D2 错误边界两层：App 全局 + ConsoleView 内容区
全局层兜门户/分享/登录面的渲染异常（降级为整页错误卡）；内容区层包住 ConsoleLayout 的 children，单页异常只降级该页签，侧栏顶栏仍在。统一 class 组件 `ErrorBoundary`（getDerivedStateFromError + 重试按钮重挂载），放 `components/ErrorBoundary.tsx`。理由：控制台是最高风险面（页签最多、数据面最杂），内容区层把爆炸半径从「整站」压到「单页」。

### D3 控制台页脚：直接移除（非贴底改造）
`#/console/*` 与 demandNew 不再渲染任何页脚，`ConsoleFooter` 组件与 App 分发分支一并退役；门户全量页脚、admin 零页脚不变。理由：工作台界面（console/admin 两侧）零页脚对称、最简；贴底改造要动 App 壳布局（flex column 化）却只为一个用户明说「不需要」的元素。合规备案信息仍在门户页 SiteFooter（登录前必经）。

### D4 档位下拉：活跃档 ∪ 当前档，禁用置灰；新建表单强制选择
客户行下拉选项 = `is_active` 档位 ∪ 该客户当前 plan_code（当前档若是禁用旧档仍显示并置灰选中，运营看得到全貌）；`disabled` 属性天然阻止选中禁用档提交，后端 409 保持为最后防线（与 portal-pricing 场景一致）。新建表单取消 `useState("starter")`：空默认 + 未选禁用提交按钮。备选「纯文本展示当前禁用档」放弃——运营改回活跃档需要看到旧档在选项里。服务端 `/internal/admin/plans` 返回面不动（它「含禁用档」是特性）。

### D5 本地数据 overview 并入 admin 双闸
`internal_overview` 的鉴权依赖从 `require_internal` 换为 `require_admin_access`（X-Admin-Token 闸 + Bearer wire-admin 角色闸，裁决矩阵照 admin/auth.py）；nginx 注入与前端零改动。理由：该端点唯一调用方是 admin UI，前端注释（admin/api.ts）当年就预期并入双闸；双闸严格强于单令牌闸（多角色门禁），且消除「运营数据面挂在内部爬虫令牌闸下」的错位。`X-Internal-Token` 语义保留给真正的引擎侧内部通道。

### D6 分流落地 + admin 独立成壳
- 落地：ConsoleView 会话建立（ready）且 hash 为裸 `#/console` 时，若 `hasAdminRole(session)` 则改跳 `#/admin`（在既有偏好落地序之前判定，suppress 不回写 landing_tab）；显式页签直达不劫持。
- 摘壳：App 壳对 `route.name === "admin"` 不再渲染站点 header/footer，AdminLayout 顶部自带极简条（品牌字 + 「返回门户」链接 + 身份 + 登出）；门户顶栏对管理员保留「内部后台」入口互跳（现状不变）。
理由：满足「同一登录入口、登录后按角色见不同界面」；摘壳成本仅 App 两个条件分支 + AdminLayout 一条顶栏。备选「仅分流不摘壳」保留门户导航混入后台的违和感，放弃。

### D7 分流判定读 ID token roles claim（前端半边）
与导航显隐同源（`hasAdminRole(loadSession())`），不改后端会话结构。风险（角色刚变更未反映在旧 token）由 admin 路由门禁兜底：无角色落 #/admin 只会看到角色门禁拦截卡，无数据面暴露。

## Risks / Trade-offs

- [改后端 /assets 形状期间双镜像滚动窗口内新旧前后端错配] → api 与 web 同批一次滚动（deploy 常规做法）；前端容错让旧前端对新后端只丢徽章行不白屏（新前端对旧后端反之降级），窗口内无硬故障。
- [overview 并闸后若存在脚本/定时任务用 X-Internal-Token 调它] → 已核查唯一调用方是 admin UI；迁移说明写入 deploy/README。
- [admin 摘壳后 #/admin 深链在未登录时呈现孤立后台门禁卡] → 门禁卡自带「返回门户」与登录引导链接（AdminLayout 顶栏在无会话时也渲染品牌与返回链接）。
- [分流让管理员 landing_tab 偏好失效] → 分流只作用于裸 #/console 且优先于偏好落地；管理员显式进控制台页签仍走偏好，偏好数据不丢。

## Migration Plan

api + web 双镜像同批滚动（无迁移、无 env 变更）；回滚 = 回滚双镜像。overview 旧闸并存无兼容负担（换闸即切换）。滚动后冒烟：①真客户账号进「我的数据」（有数据集的客户，白屏回归）；②admin 改档/建户（starter 不再默认）；③admin 本地数据页 200；④admin 账号裸登录落 #/admin；⑤普通账号登录仍落 #/console 且无后台入口。

## Open Questions

（无——拷问会话九问已全部裁决：嵌套回契约、双层边界、控制台去页脚、下拉并集置灰、新建强制选择、双闸并闸、分流+摘壳、角色未实测误发、单 change 打包。）
