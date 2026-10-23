# Design: wire-record-view

## Context

代码现状（2026-10-15 探锚）：

- **kind 白名单两处口径**：`artifacts/models.py:36` `ARTIFACT_KINDS`（五类
  校验面，form 不在其中）；form 走 `service.py:53` `FORM_KIND` 常量 +
  `BLANK_CREATE_KINDS`（:360）+ `_blank_spec`（:363）分派。record-view 同
  form 走 service 层常量路线，不动 ARTIFACT_KINDS（AI 门 `ai/tools.py:18`
  ARTIFACT_TOOL_KINDS 不收录，天然不进 AI 面）。
- **分享机器全套可复用**：`share/service.py` `_mac`/`build_token`/
  `verify_token`（:72-116）、TTL 白名单 `share/routes.py:60`
  `ALLOWED_TTL_DAYS=(7,30,90)`、503 闸 `require_share_secret`、路径分派
  `SHARE_KIND_PATHS`（:56）、限流 `share/rate_limit.py`（mac 段滑窗）。
- **公开页承载形态**：API 返回 JSON 快照，SPA hash 路由渲染
  （`web/src/App.tsx:90-108` + `views/ShareView.tsx`，ShareMode 联合类型
  :31）；noindex/品牌面/四态规范集中在 ShareView。
- **取数地基**：`artifacts/query_contract.py:183` `_query_dataset`（tier
  分流：免费档 `api_store.execute_query`、cell 档引擎 TOOL_QUERY）；过滤
  算子白名单 eq/in/gt/gte/lt/lte 既有（`query_contract.py:37`）。dashboard
  渲染器是纯快照零 fetch（`DashboardRenderer.tsx:30`）——record-view 需要
  首个公开实时取数端点。

## Goals / Non-Goals

**Goals:**

- record-view 产物从空白创建到扫码取数全链路，移动优先时间轴渲染。
- 首个公开实时读面的暴露边界成为代码即契约（声明查询+令牌+限流+缓存+
  上限）。
- 长效令牌（0 档）三类 kind 同享、缺省行为零漂移。

**Non-Goals:**

- 按记录隔离令牌、附件/证书图片、AI 生成、批量二维码打包、短链
  （proposal Non-Goals 已列）。
- 时间轴之外的自定义页面模板（v1 固定「头部字段+时间轴段」两区块形态；
  模板化后置）。

## Decisions

**D1 kind 注册走 service 层常量路线（同 form）。**
`RECORD_VIEW_KIND = "record-view"` + BLANK_CREATE_KINDS 追加 + `_blank_spec`
分支 + `validate_kind_spec` 分派到新 `validate_record_view_spec`。展示面
两处（`workbench.py:271` WORKBENCH_KINDS、web api.ts 展示序）同步追加——
workbench 注释明示两处需一致。AI 门不收录（Non-Goal）。

**D2 公开取数端点：验签后声明查询，组装在服务端。**
`GET /api/v1/share/record-view/{id}?t&k` 流程：`verify_token`（版本钉死）
→ 取钉死版本 spec → 主记录等值查（limit 1，有效视图）→ 零行返回 200+
`{found: false}` 态（页面呈现「无此记录」，与 404 令牌态区分）→ 命中则
逐 section 代入 `{fromField}` 查询（升序 limit，复用 `_query_dataset`）
→ 返回 `{found, header: {label: value}, sections: [{title, rows, error?}],
version}`。section 查询失败（列缺失）单段 error 标注、其余照常（spec
「引用失效如实」场景）。**不选**「公开端点透传任意 query」：声明边界是
红线本体；**不选**「快照内嵌 spec（dashboard 模式）」：溯源要实时反映
补录，快照会在印刷后过期。

**D3 缓存与限流。**
60 秒服务端短缓存，键=（artifact_id, 钉死版本, 令牌 mac 段, k）——印刷物
场景同一批次扫码高频，缓存挡查询；限流复用 `rate_limit.py` 滑窗机器、
独立阈值 env（缺省 240/h，比 form submit 宽：读面无写入滥用面）。

**D4 TTL 0 档实现=exp 上限语义。**
令牌格式 `{version}.{exp}.{mac}` 不动：选 0 档时 exp 置 0，`verify_token`
对 exp==0 跳过过期校验（其余校验不动）。ALLOWED_TTL_DAYS 追加 0；生成
响应 expires_at 为 null（如实）。撤销闭环靠既有「归档/取消发布即 404」
与 secret 轮换全失效——文档如实说明，不新造吊销列表（v1 不发明撤销
登记表）。

**D5 二维码纯前端。**
引入小型 QR 库（如 qrcode 系纯 JS 实现，无 wasm/网络依赖）仅在 record-view
详情面懒加载；PNG（高清倍率）与 SVG 两格式下载。URL 组装=分享 base +
`#/share/record-view/{id}?t=…&k=…`，键值选择器走 keyColumn 去重下拉
（复用数据面 distinct 查询，上限 500）或手输。

**D6 公开页渲染：ShareView 第三 mode。**
`ShareMode` 增加 `"record-view"`；新组件 RecordViewRuntime：取数端点裸
fetch（绝不带 Authorization，同 fetchSharedDashboard 机器）、四态
（加载/就绪/无此记录/无效链接）复用品牌面与 noindex 注入；时间轴 UI
移动优先（扫码场景），节点点击展开该行全部列值。

## Risks / Trade-offs

- **首个公开实时读面**：暴露=钉死版本声明查询的结果行。缓解齐上（令牌、
  限流、缓存、行上限、noindex、k 可枚举如实文档）；若未来需要强隔离，
  按记录令牌是 additive 扩展。
- **主记录键不唯一**：keyColumn 无 DB 唯一性保证，取首行（等值查询按
  主键序稳定）并如实——构建器文档建议键列唯一；不新造唯一约束面。
- **section 查询放大**：每扫码 ≤10 section 查询（cell 档引擎调用），
  缓存+限流兜底；上限与 form 提交闸同量级。
- **QR 库供应链**：锁定小版本、仅 console 懒加载、不进公开页 bundle。
