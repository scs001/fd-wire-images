## ADDED Requirements

### Requirement: 带内容创建 SHALL 限平台租户且与空白创建同校验

`POST /api/v1/artifacts` 的入参 SHALL 支持可选 `spec` 字段：携带 spec 时一步创建
「带内容 draft 产物」（v1 版本行 = 给定 spec），走与 spec 更新相同的 kind 分派校验
（`validate_kind_spec`）；校验失败 422 如实拒绝且零半写（不落任何产物/版本行）。
带 spec 的创建 SHALL 仅限平台自营发布方（`finddata-official`）：其他租户携带 spec
创建一律 403。不带 spec 的请求 SHALL 保持既有空白创建行为完全不变（全租户可用）。

#### Scenario: 平台租户带内容建档

- **WHEN** 平台租户 POST `{kind:"dashboard", title:"…", spec:{title:"…", panels:[…]}}`
  且 spec 通过 kind 分派校验
- **THEN** 生成 draft 产物，v1 版本内容为给定 spec
- **AND** 版本行 operator 与初始状态留痕语义与空白创建一致

#### Scenario: 非平台租户带内容被拒

- **WHEN** 普通租户 POST 携带非空 `spec`
- **THEN** 403 如实拒绝，不产生任何产物或版本行

#### Scenario: spec 校验失败零半写

- **WHEN** 携带的 spec 未通过 `validate_kind_spec`（如 dashboard 缺 panels、字段类型错）
- **THEN** 422 带原因拒绝，库中无新增产物行与版本行

#### Scenario: 不带 spec 行为不变

- **WHEN** 任一租户 POST 仅带 `{kind,title}`（与既有空白创建入参一致）
- **THEN** 行为与现状逐字节一致（最小空 spec、全租户可用、kind 白名单 422 语义不变）

### Requirement: showcase_slug 幂等 SHALL 同租户查重追加版本

携带 spec 的创建 SHALL 支持 `spec.showcase_slug` 作为幂等键（沿用 import_showcase
既有约定）：同租户内已存在同 slug 产物时，重复 POST SHALL NOT 创建第二条产物，
而是向既有产物追加一个新版本（append-only，语义与 spec 更新一致）并返回既有
产物标识；跨租户的同 slug 互不可见、互不命中。

#### Scenario: 同 slug 重放追加版本

- **WHEN** 平台租户以同 slug 第二次 POST（spec 内容有更新）
- **THEN** 不新建产物，既有产物版本号递增且新版本内容为本次给定 spec
- **AND** 响应如实标识既有产物（非新建语义）

#### Scenario: 同 slug 重放内容相同

- **WHEN** 平台租户以同 slug 第二次 POST 且 spec 与当前版本内容一致
- **THEN** 不新建产物、不追加冗余版本，返回既有产物当前状态

#### Scenario: 跨租户 slug 隔离

- **WHEN** 普通租户（若有带内容权限的后续演进）或平台租户使用与其他租户
  相同的 slug 值
- **THEN** 幂等查重只在请求方租户域内进行，不因他租户同 slug 而被拒或被合并
