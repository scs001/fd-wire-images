## Context

见 `proposal.md` 的 Why。当前实现的两个事实起点：

- **分流是单向改写**：`ConsoleView` 在会话建立后对 `isBareConsoleHash() && hasAdminRole()` 直接 `window.location.hash = "#/admin"`；`App` 又以 `route.name !== "admin"` 为条件排除门户顶栏。管理员落后台后，`AdminLayout` 的三个出口（品牌链接 / exit / 登出）全部指向 `#/`，没有任何一个指向 `#/console`。代码注释表明这是有意决策（"design D6：管理员即便存过 landing_tab，裸登录也落 admin"），且显式子路径不受劫持——管理员手敲 `#/console/artifacts` 可正常进入。因此这不是鉴权缺陷，而是「管理员只当管理员」的假设与「管理员同时是客户」的现实冲突。
- **后台列表接口全量返回**：`GET /internal/admin/catalog/pending` 为 `select(...).where(status == ...)` 后直接 `.all()`，无 limit/offset。生产实测 verified 3,421 条（≈1.0 MB）、registered 54,132 条（≈16 MB），前端一次性 map 全部条目。

## Goals / Non-Goals

**Goals**

- 管理员身份不失去控制台可达性，且分流后的后台壳提供显式出口。
- 目录治理列表按页返回，呈现层只渲染当前页。

**Non-Goals**

- 不改权限模型：`wire-admin` 角色闸、后台 API 双闸、客户数据隔离边界一律不动。
- 不改分流本身的存在性：管理员裸登录仍落后台（既有 D6 决策保留），只补回可达性。
- 不引入分页组件库或虚拟滚动依赖：复用控制台「我的数据」页既有的上下页翻页模式。
- 不修其余后台列表端点（customers / ledger / demands / plans / usage-overview / notes 均无分页）。它们是同类隐患但当前规模未造成可观察故障，本次不扩面，见 Risks。

## Decisions

### D1：保留分流，补出口（而非取消分流）

分流本身节省管理员一次点击，且已作为 D6 决策沉淀在 `console-auth` spec 中；取消它会让管理员每次登录都落到客户视图，反而退化。因此保留「管理员裸登录落后台」，改为在该壳上补一条指向控制台的出口，并让文档页入口对管理员可达。

**备选**：取消分流，所有角色一律落控制台，后台改为门户导航上的一个入口。
**不选的理由**：这会让管理员每天登录多一次点击，且与既有 spec requirement「管理员裸登录落后台」直接冲突，需要删改既有场景。

### D2：控制台出口挂在后台顶栏，与「返回门户」并列

后台顶栏已有 exit（`返回门户`）。新增一条并列出口指向 `#/console`，语义为「以客户身份使用控制台」。二者目的不同（回门户浏览 vs 进控制台用资产），并列呈现而不合并。

### D3：分页沿用控制台既有翻页模式，单页上限对齐公开目录

后端新增 `limit`/`offset`（上限与公开目录 `catalog/service.py` 的 100 对齐），响应返回 `items` + `total`。前端改用「上一页/下一页 + 共 N 页」，复用 `DatasetsPage` 既有翻页实现风格，不引入新组件或依赖。总数经一次带 `total` 的响应获得，不额外加计数请求。

**备选**：虚拟滚动（只渲染视口内条目）。
**不选的理由**：引入新依赖；且分页信封同时压住了传输体积（16 MB → 单页 KB 级），虚拟滚动只治渲染不治传输。

### D4：响应信封为 BREAKING，一次性改到位

`CatalogPendingOut` 由裸列表语义改为 `{items, total, ...}`。调用方只有前端 `admin/api.ts` 一处（内部端点），同批次改完，不留兼容分支。

**备选**：新增 `?page=` 参数但保留全量默认行为以向后兼容。
**不选的理由**：默认全量意味着新接入方一踩即中；且该端点无外部消费者，兼容分支只是包袱。

## Risks / Trade-offs

- **[列表接口批量隐患未清]** 其余 6 个后台列表端点同为全量返回（customers / ledger / demands / plans / usage-overview / notes），本次不动 → 本次只给 `catalog/pending` 建立分页契约与前端范式；其余端点按同一范式补齐时可直接复用，无需再设计。若其中任一端点规模先失控，单开 change 补。
- **[白屏根因未完全钉死]** 已确认量级失控（3,421 / 54,132 条全量渲染），但「下滑变全白」究竟是渲染压力还是 `AdminLayout` 的 `flex min-h-0 flex-1` 在溢出时压掉内容区，需浏览器 DevTools 复核滚动时 DOM 中条目是否仍在 → 实施时先复现确认，分页落地后再验一次；若分页后仍白，则属布局裁剪问题，单开任务处理。
- **[BREAKING 契约]** `catalog/pending` 响应形状变更 → 仅前端一处消费者，同批改完并过既有测试；归档前跑 `openspec validate --strict` 与前端测试。
- **[分流改动触及登录主链路]** 文档页入口改动落在ConsoleView 分流分支附近 → 三个既有场景（管理员裸登录落后台 / 普通客户落地不变 / 显式直达不劫持）须逐条回归，不得为修新场景而破坏旧场景。

## Migration Plan

1. 后端：为 `catalog_pending` 加 `limit`/`offset` 与 `total`，上限 100。
2. 前端：`admin/api.ts` 适配新信封；`CatalogGovernancePage.tsx` 改为分页呈现。
3. 前端：`AdminLayout` 顶栏新增控制台出口；`ConsoleView` 分流分支调整；`DocsView` 入口核对。
4. 回归：三个既有分流场景 + 分页两端（verified/registered）+ 双语键值测试。
5. 回滚：全部为前端与单端点改动，无数据迁移，逐提交 revert 即可；分页信封变更随同一次发布，不存在半量状态。

## Open Questions

无。规格与任务分解所需的决策均已在此钉定。
