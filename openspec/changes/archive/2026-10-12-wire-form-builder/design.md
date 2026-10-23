# Design: wire-form-builder

## Context

- 产物底座（artifacts：版本链/状态机/空白创建）与修正层（corrections：
  insert/update/delete 三操作、api 档本地层+cell 档引擎透传、审计可回滚）均已
  在役；分享令牌机器（HMAC 钉版本、TTL 7/30/90、secret 未配 503）只放行
  dashboard。本 change 在这三块既有面上「长出」form，不改它们的既有语义。
- 生产为双库（wire_platform / wire_datasets 分库）——**查 datasets 表必须走
  datasets 库会话**（wire-dashboard-query-contract 已踩过：子代理夹具合库会
  掩盖该路由需求）。

## Goals / Non-Goals

见 proposal。补充边界：公开提交是**写-only** 面——没有"读回数据"的公开路径；
防滥用 v0 = 令牌限流 + corrections 可整条回滚 + 发布态开关（archive 即断）。

## Decisions

### D1. form spec 契约与校验分层

- 信封 `{title, spec:{datasetId: string|null, fields: [...], submitLabel?,
  successMessage?}}`；`datasetId=null` 为构建中间态（可保存不可提交）。
- **保存时（PUT/空白创建后首存）校验三层**：①结构（fields 数组、input 枚举、
  column 唯一、select 非空 options、label/文案字符串长度上限）——artifacts
  库内零依赖可测；②datasetId 非空时查 datasets 库会话验存在+属主（零引擎
  调用）；③**列名是否仍在数据集中不校验**——数据集列会演进，钉死校验会让旧
  版本表单无法重存；列漂移的真相在提交时暴露（422 如实），构建器呈现实时列。
- 校验实现放 `artifacts/form_spec.py`（纯函数），`update_spec` 与
  `create_blank_artifact` 按 kind 分派：dashboard 走既有最小校验，form 走
  form_spec 校验。validate_spec 通用口径（title 非空+spec.spec 非空）保持，
  form 追加结构层。
- `BLANK_CREATE_KINDS = ("dashboard", "form")`；form 初始 spec =
  `{title, spec:{title, datasetId: None, fields: []}}`。

### D2. 提交路径复用：抽公共 insert 服务函数

corrections 路由里的 insert 分支（载荷校验→api 档
`api_store.apply_local_correction` / cell 档引擎 `correction_add`）下沉为
`datasets/service.py::submit_insert_correction(db, engine_factory, dataset,
values, actor)`——console 运行时与公开提交共用同一函数（同闸同审计，杜绝两
口径）。既有 update/delete 分支不动（仍在路由内）。
- 免费档行数闸：`apply_local_correction` 内既有 QuotaExceeded 语义原样传导
  （路由转 429）；公开提交路径同样转 429（带原因文案，提交者需可读反馈）。
- 引擎不可达：与 corrections 路由同款 502 诚实失败，零半写。

### D3. 公开提交端点与限流

- `POST /api/v1/share/form/{artifact_id}/submit?t&v`，body `{values: object}`。
- 校验序：secret 在位（503）→ 令牌验签+v 一致+未过期（统一 404）→ 产物
  published+kind=form（统一 404）→ 钉死版本行在场（统一 404）→ **按钉死版本
  spec** 校验 values（未知列/必填/类型——422 带字段级原因；提交者持有效令牌，
  422 detail 无泄露面）→ 限流 → dataset 取行（datasets 库会话；不存在/他企=
  404 统一口径）→ submit_insert_correction（actor=`share-form:{artifact_id}`）
  → 201 `{ok:true, message: successMessage}`。
- 类型校验：number 收 int/float 或可 parseFloat 的字符串（NaN/Inf 拒）并归一
  为数值；date 须 `YYYY-MM-DD`（regex，拒绝时间部分）；select 值须 ∈ options；
  text 收字符串（数字/布尔标量 stringify，对象/数组拒）；未勾字段不传即缺省
  （非必填跳过，不写 null 键）。
- **限流器**：进程内滑动窗（`share/rate_limit.py`）：键=令牌 mac 段（非完整
  token，避免明文 version/exp 进键），`collections.deque`+`threading.Lock`，
  惰性读 env `FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR`（缺省 60；测试逐用例注入，
  与 share secret 同范式）；超限 429 + `Retry-After`（窗首出队秒数）。单副本
  进程内限流= v0 明示边界（现产单副本 api；多副本时换共享存储，届时另立
  change）。
- 公开 GET `/api/v1/share/form/{id}?t&v` 返回 `{title, spec(钉死), version,
  expires_at}`——与 dashboard 公开端点同形（前端同款消费），spec 内只有字段
  定义与文案。

### D4. 分享链接生成端点扩展

`POST /api/v1/artifacts/{id}/share` 的 kind 门从 `!= "dashboard"` 403 改为
`not in ("dashboard", "form")`；url 按 kind 分派路径
（`/api/v1/share/dashboard/...` vs `/api/v1/share/form/...`）。TTL 选项、
published 门、secret 禁用语义零改动。

### D5. Web 形态

- `web/src/console/form/FormRuntime.tsx`（**console 与分享页共用**）：
  props `{spec, endpoint}`——console 传 corrections 端点（带会话），分享页传
  公开 submit 端点（t/v 进 query）；必填/类型前端先拦（与后端同规则），错误
  逐字段呈现，成功显示 successMessage；`datasetId==null` 渲染「未绑定数据集」
  诚实态。
- `FormEditor.tsx`：数据集下拉（复用既有列表端点）→ 列勾选（`columns` 端点）
  → 字段卡（label/input/required/options）→ submit 文案 → PUT 保存版本链；
  保存前结构自检（与后端规则同源的纯函数 `formSpec.ts`，供 editor 与 runtime
  共用）。
- ArtifactsPage：`ARTIFACT_KINDS` 加 `form`、kind 徽标/筛选自动跟上、
  「新建表单」入口（与新建看板并列）、form 详情展开=运行时+编辑器入口+分享
  按钮（published 态）。
- ShareView：mode 由路径分派（`/share/dashboard/:id` vs `/share/form/:id`）；
  form 态渲染 FormRuntime（公开端点）+ 品牌署名/noindex 沿用既有四态框架。

### D6. 审计与红线对照

- actor 命名空间：console 提交沿用 `console:<cid>`；公开提交
  `share-form:<artifact_id>`——修正历史/WAL 可归因到表单，回滚粒度=单条。
- 红线①永不写回客户源：提交=corrections 覆盖层 insert，天然满足。红线③
  公开渲染层零客户数据：公开面只出字段定义（所有者主动发布的表单文案），
  无行数据、无读路径。noindex 沿用。红线⑥文档同批：web 文档页「表单」双语
  章节随代码同批上线（i18nKeys 完整性测试会强制 zh/en 同步）。

## Risks / Trade-offs

- **公开写面滥用**：限流是每令牌的——一条链接多人共用共享额度（v0 接受，
  env 可调；验证码/邮件确认明确非目标）。极端垃圾提交的兜底=修正历史整批
  回滚 + archive 断链。
- **钉死版本 vs 数据集演进**：令牌钉 form spec 版本，但写入落在数据集当前
  有效表——列被删后旧表单提交 422 如实（不静默丢、不写错列）。
- **api 档 insert 的行数闸**对公开提交同样生效（免费档表单被填满=429），这是
  商业配额的自然延伸，文案如实呈现。

## Migration Plan

无迁移：form 复用 artifacts/artifact_versions（spec JSON 列）与
dataset_corrections（api 档）既有表；无新表无新 env 强依赖（限流 env 缺省
60、secret 沿用 FD_WIRE_SHARE_SECRET）。

## Open Questions

无——AI 生成 form、update/delete 型表单、字段联动均为明示非目标（v1+ 另立）。
