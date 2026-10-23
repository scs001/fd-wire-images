## Context

2026-10-22 省级面板看板导入（finddata 根 change `add-province-panel-dashboards`）
暴露的程序化发布缺口，用户定性「应有开放接口」。最初设想是「新增 POST 创建端点」，
实查代码后发现缺口比设想窄——**POST /api/v1/artifacts 已存在**（2026-10-11
wire-dashboard-blank-create 落地，routes.py `create_artifact` → service
`create_blank_artifact`），但入参只有 `{kind,title}`，spec 由服务端内造最小空板。
本 change 把创建面从「空白」扩到「带内容 + 幂等」，权限红线按用户裁决从紧。

## Goals / Non-Goals

- Goals：程序化发布全链 HTTP 可达（建档→publish→visibility）；重放安全（slug 幂等）；
  空白创建既有行为零变化。
- Non-Goals：不改空白创建权限（全租户现状保留，放开内容创建给全租户另议）；
  不改可见性/市场/橱窗模型；不给 slug 建列建索引（v0 遍历查重）。

## 侦察事实（设计依据）

- `POST /api/v1/artifacts` 现状：`ArtifactCreateIn{kind,title}` → 内造空 spec →
  `create_blank_artifact`（BLANK_CREATE_KINDS 白名单，v0 已扩到
  dashboard/form/alert_rule/record-view/doc）；建档手法 = version 1 版本行 +
  operator 审计 + 初始状态留痕 + title 投影，来源会话指针 None。
- spec 更新链路：`PUT /api/v1/artifacts/{id}` → `validate_kind_spec` 分派校验 →
  追加不可变版本；archived 拒改 409。带内容创建的校验必须复用同一函数（单一真源）。
- 可见性链路：`PUT /api/v1/market/artifacts/{id}/visibility` 过 `require_asset_editor`
  （owner/editor，viewer 403）；公开橱窗查询层硬编码
  `customer_id == PLATFORM_PUBLISHER_NAME("finddata-official")` +
  `visibility == VISIBILITY_MARKET` + `status == "published"` 三条硬条件
  （market/showcase.py `_base_query`）——普通租户无论怎么设可见性都进不了橱窗，
  平台红线在展示面已天然成立。
- `import_showcase.py` 现状：pod 内直调 `artifacts_service.create_blank_artifact`
  + 直连双库（artifacts 在 ai 库 SQLite PVC；billing_customers 在平台库），
  幂等 = 导入前遍历产物 spec 找 `inner.showcase_slug`，撞 slug 跳过。

## 权限裁决（记录依据）

用户 2026-10-22 原话定性：「market 可见性是对外展示面，创建权必须限平台租户
（或管理员角色），不能做成『登录即可建』——否则任何 console 账号都能往市场上架
东西。」实况里「创建」与「上架」已是两道门，但按裁决从紧：**新开的带内容创建
限平台租户**（`customer_id == finddata-official`，与橱窗发布方同一判定，
复用 `is_platform_publisher`）；空白创建是已上产既有行为，不动。将来若要放开，
在此 change 之上改一个判定即可。

## 幂等语义

- 键：`spec.showcase_slug`（spec JSON 自由扩展位，已是 import_showcase 约定，
  现存 14+ 张市场板全带 slug）。
- 查重域：请求方租户内。实现 = 取该租户全部产物的当前 spec，扫 `inner.showcase_slug`
  （import_showcase 同款逻辑下沉到 service 层）；平台租户产物量级几十条，遍历
  可接受；将来超规模再落列建索引（Non-Goal）。
- 重放行为：命中即走与 PUT 同一条追加版本路径（append-only），响应返回既有产物
  （新建 201 / 重放 200 如实区分）；给定 spec 与当前版本内容一致时不追加冗余版本
  ——「重跑导入脚本 = 确保最新内容在线」的单调语义，重放永远安全。
- 查重对全租户生效但**带 spec 创建限平台**，故 v0 实际只有平台租户能用幂等；
  语义上不写死平台（跨租户隔离场景保留，为将来放开留缝）。

## import_showcase HTTP 化

- 增 `--http-base-url` + 凭据（平台租户 console token）模式：POST（带 spec+slug，
  幂等）→ `POST /api/v1/artifacts/{id}/publish` → `PUT /api/v1/market/artifacts/{id}/visibility`。
- pod 直跑模式保留为回退（同一脚本两条通道，HTTP 模式优先）；生产验收用 HTTP 模式
  重放 5 张省级板，断言「零新建、版本号 +1、内容一致」。
- 本地跑 HTTP 模式的既有坑：macOS 系统代理会劫持回环（须 NO_PROXY）；对生产域名
  无此问题。

## Risks / Trade-offs

- slug 遍历查重在平台租户产物千级后变慢 → 届时落 `showcase_slug` 列（json_extract）
  + 索引，接口语义不变。
- 「重放不追加冗余版本」需要内容比对（当前版本 spec vs 给定 spec），比对口径 =
  规范化 JSON 语义相等（键序无关），避免键序差异制造假版本。
- 403（越权带内容）与 422（校验失败）必须可区分，测试各钉一条，防止权限回归
  被误报成校验问题。

## Migration Plan

纯增量：新字段可选、新行为限平台租户、既有入参路径零改动。无需数据迁移；
`import_showcase` 切 HTTP 模式后 pod 依赖解除，回退 = 沿用 pod 直跑模式。

## Open Questions

- 无（权限已按用户裁决从紧；放开给全租户为将来独立裁决）。
