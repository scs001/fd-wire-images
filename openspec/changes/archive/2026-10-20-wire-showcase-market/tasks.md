## 1. 平台租户与判定

- [x] 1.1 在 `api/fd_wire_api/billing/` 新增 `PLATFORM_PUBLISHER_NAME = "finddata-official"` 常量与 `is_platform_publisher(customer_id, db) -> bool` helper（按 billing_customers 行比对 name），验证：单测断言常量值、未知 id 返回 False、平台行返回 True
- [x] 1.2 提供幂等的平台租户引导（脚本或启动期 upsert）：name 冲突即复用既有行、plan 挂免费档，验证：连跑两次不产生第二行，`billing_customers` 中 name 唯一
- [x] 1.3 补测试锁死常量与判定口径（改 name 必须同时改测试），验证：`pytest -k platform_publisher` 全绿

## 2. 匿名橱窗端点

- [x] 2.1 抽出带 `publisher_filter` 的共享查询构造（列表/详情），两个路由各自传入自己的过滤条件，验证：既有市场测试全绿（口径零漂移）
- [x] 2.2 新增 `GET /api/v1/showcase/dashboards`（匿名，无鉴权依赖）：查询层强制 `customer_id = 平台租户` + `visibility='market'` + `published` + `kind='dashboard'`；返回条目元数据，验证：客户的 market 产物不在结果中（专门用例）
- [x] 2.3 新增 `GET /api/v1/showcase/dashboards/{id}`（匿名）：先断言 `is_platform_publisher`，断言失败一律 404（不是 403）；通过则返回钉死版本的完整 spec（含快照）+ `refreshed_at`，验证：客户产物 id 请求返回 404 且不泄露存在性
- [x] 2.4 平台租户缺失时匿名端点返回空列表而非 500（安全降级），验证：无平台租户的库上请求列表得 `{items: [], total: 0}`
- [x] 2.5 下架即时失效：产物改 private 或 archive 后匿名列表与详情立即不返回，验证：改档后同一请求得 404
- [x] 2.6 独立限流（`FD_WIRE_SHOWCASE_RATE_PER_HOUR`）+ 60 秒短缓存（含「缓存命中零查询」断言），验证：超限 429 + Retry-After；同参数二次请求不打库
- [x] 2.7 响应携带缓存有效期信息供 CDN/静态构建复用，验证：响应头含该信息

## 3. 行业看板批量导入器

- [x] 3.1 新增 `api/scripts/import_showcase_dashboards.py`：读 `finddata/dashboards/*.spec.json`（以及 `macro_cn_spec.json`），把 `charts[].option` 转为 `raw_option` 面板、`kpi[]` 转为 kpi 面板、标题/出处保留，验证：对两份真实源文件干跑输出正确面板数与标题
- [x] 3.2 幂等：以文件名归一化 slug 写入 `spec.spec.showcase_slug`，重复执行对同一看板产生新版本（非新条目），验证：连跑两次后看板条数不变、版本号递增
- [x] 3.3 上架走既有链路（建 draft → 写 spec → publish → 设 market），不直写表，验证：导入后产物 `status_history` 含 draft→published 迁移且可见性为 market
- [x] 3.4 逐条独立：单份源文件失败不中断整批，失败原因逐条报告，验证：混入一份坏源文件的批次里其余全部成功
- [x] 3.5 不碰客户产物：存在同 slug 的非平台产物时不修改它（另建平台条目或报冲突），验证：专门用例断言客户产物零变化
- [x] 3.6 `--dry-run` 只报告不写入，验证：干跑后库内产物数不变

## 4. 前端橱窗页与市场标识

- [x] 4.1 新增 `web/src/views/ShowcaseView.tsx`：匿名列表（标题/分类/面板数）+ 详情（复用 `DashboardRenderer`，不传 toolbar），验证：组件测试断言列表渲染、详情出图、`data-toolbar="off"`
- [x] 4.2 新增路由 `#/showcase`（内含详情 `#/showcase/{id}`），匿名可达且不在控制台壳内，验证：路由单测 + 页面无登录跳转
- [x] 4.3 橱窗页只读红线：无编辑/保存/复制/导出 CSV/导出图片入口，且不出现任何租户标识，验证：组件测试逐项断言不存在
- [x] 4.4 橱窗页可索引（不注入 noindex），与登录市场面的不索引纪律并存，验证：页面无 robots=noindex 注入，市场面仍注入
- [x] 4.5 市场面新增「平台自营」徽标（后端条目带该标识），验证：市场测试断言徽标按标识出现/缺席
- [x] 4.6 双语键值齐备，验证：`vitest run i18n` 全绿

## 5. 收口

- [x] 5.1 红线专项测试套：客户 market 产物在橱窗列表/详情/计数/任何字段均不可见，验证：该套件全绿
- [x] 5.2 全量前端与后端测试，验证：`vitest run` 与 `pytest` 无新增失败
- [x] 5.3 `tsc --noEmit` 无错误，验证：命令退出码 0
- [x] 5.4 实弹：导入两份真实成品看板 → 匿名端点返回完整 spec → 用 echarts 渲染其 raw_option 面板全部出图，验证：逐张图渲染成功且条数与源文件一致
- [x] 5.5 跑 `openspec validate wire-showcase-market --strict`，验证：输出 valid
