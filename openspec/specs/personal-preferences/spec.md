# personal-preferences Specification

## Purpose
TBD - created by archiving change wire-personal-preferences. Update Purpose after archive.

## Requirements

### Requirement: 偏好存储模型（per-sub）

个人偏好 MUST按 Logto sub 存储（一人一行，owner 与团队成员通用）：三类键
`ui_locale`（∈zh|en|null）、`landing_tab`（∈控制台九页签
chat|artifacts|datasets|entities|packs|demands|usage|keys|team|null）、
`pinned_dashboard_id`（整型|null，MUST为裸列+应用层校验，不跨 Base 建
外键）。偏好是个人 UI 指针而非客户业务数据，MUST不进溯源账本/team_events；
任何已映射身份（含 viewer）MUST可读写自己的偏好（不受 viewer 方法级只读
门禁约束）。

#### Scenario: viewer 写自己的偏好
- **WHEN** viewer 角色 Bearer PUT 自己的 ui_locale
- **THEN** 200（不因 viewer 只读门禁 403）；GET 回读为新值

#### Scenario: 一人一行按 sub 隔离
- **WHEN** 同一客户两个成员各自 PUT 不同 ui_locale
- **THEN** 各自 GET 回读各自值，互不可见

### Requirement: 读写契约

`GET /api/console/me/preferences` 对已映射身份 MUST返回三键全量（未设=null，
行不存在不 404）；未映射身份 401。`PUT` MUST部分更新：请求体三键任意子集，
缺键不动、显式 null 清空；成功响应=写后全量态（同 GET 形）。校验失败 MUST
422 如实定位：ui_locale 非法、landing_tab 不在九页签内、pinned 不是本客户
未归档 dashboard 产物（不存在/跨客户 MUST同文案，不泄露他户存在性）。

#### Scenario: 部分更新与清空
- **WHEN** 已有 {ui_locale:zh, landing_tab:artifacts}，PUT {"ui_locale": null}
- **THEN** 200 {ui_locale:null, landing_tab:"artifacts"}；未携键不受影响

#### Scenario: 非法值 422
- **WHEN** PUT ui_locale="ja" 或 landing_tab="admin" 或 pinned=他人客户看板 id
- **THEN** 422 定位对应键；跨客户与不存在同文案

### Requirement: 置顶指针自愈

GET MUST对 pinned_dashboard_id 重验：产物存在、kind=dashboard、属同一客户、
未 archived。任一失效 MUST返回 null 且清列（读时自愈），不留死链。

#### Scenario: 置顶看板被归档
- **WHEN** pinned 看板已 archived 后 GET
- **THEN** pinned_dashboard_id=null（且存储列被清）；再 PUT 重置合法看板成功

### Requirement: 控制台同步语义（web）

登录引导 MUST拉取偏好（失败 MUST不阻塞控制台启动）。locale 收敛：server
ui_locale 有值→MUST覆盖本地 i18n 并生效；首次 GET 全空→MUST领养（把当前
本地语言上传一次）；顶栏切换语言 MUST即写 server（fire-and-forget，未登录
仅本地）。落地：**裸 `#/console` 入口才走偏好**（显式页签直达不劫持），
序=pinned（跳 artifacts 页签并直接展开该看板）> landing_tab > datasets；置顶
跳转引起的页签变化 MUST不回写 landing_tab，用户切页签 MUST即写。看板产物行
MUST提供置顶/取消置顶操作与默认徽标。

#### Scenario: 跨设备语言同步
- **WHEN** 设备 A 切换语言为 en，设备 B 裸登录进控制台
- **THEN** 设备 B 界面以 en 呈现（server 值覆盖本地默认）

#### Scenario: 裸入口按偏好落地
- **WHEN** 已置顶看板 K 且 landing_tab=team，浏览器开 #/console
- **THEN** 落 artifacts 页签并直接展开 K 的查看区（不落 team；此次跳转不
  改写 landing_tab）；未置顶时落 landing_tab；两者皆空落 datasets

#### Scenario: 显式直达不劫持
- **WHEN** 浏览器开 #/console/usage
- **THEN** 直接落 usage 页签（偏好不劫持；切页签行为照常即写）
