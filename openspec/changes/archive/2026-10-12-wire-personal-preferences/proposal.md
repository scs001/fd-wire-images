# Proposal: wire-personal-preferences — 个人偏好持久化（语言/落地页/默认看板跨端同步）

## Why

方向 D 最后一项「个人偏好持久化」。今天控制台全部个人状态都活在浏览器里：
语言 = `localStorage["wire-web-lang"]`（换台设备回到中文默认）、落地页 = 硬编码
datasets 兜底（每次登录都落「我的数据」）、看板没有"默认"概念（每次进控制台
都要重新点开）。团队空间（wire-team-spaces）上线后一个客户多人共用，语言与
落地习惯更是每人一份的——偏好必须是 **per-sub** 个人数据，不是客户数据。

## What Changes

1. **偏好存储**（迁移 0029，平台库）：`member_preferences`（sub 主键、一人
   一行）：`ui_locale`（zh/en|null）、`landing_tab`（控制台九页签|null）、
   `pinned_dashboard_id`（裸整型+应用层校验，跨 Base 不建 FK）。零客户业务
   数据——只有 UI 指针，不经溯源账本。
2. **读写端点**：`GET/PUT /api/console/me/preferences`。GET 三键、null=未设
   （已映射身份永不 404）；PUT 部分更新（缺键不动、null 清空）+校验 422
   （置顶必须是**本客户未归档 dashboard 产物**，跨客户与不存在同文案防探测）；
   响应=写后全量态。偏好是个人数据：**viewer 也可读写自己的偏好**（新依赖
   `require_console_member_identity`，任意已映射身份，不做方法级门禁）。
3. **置顶指针自愈**：GET 时重验 pinned（存在/kind=dashboard/同客户/未归档），
   失效→返回 null 且读时清列（产物被归档/删除后偏好不留死链）。
4. **web 同步语义**：
   - 登录引导拉取偏好（失败不阻塞控制台启动）；
   - locale 收敛：server 有值→覆盖本地 i18n；server 全空→**首次领养**（把
     本地语言上传）；顶栏切换语言即写（fire-and-forget）；
   - 落地序：**裸 `#/console` 才走偏好**（显式 tab 直达），pinned >
     landing_tab > datasets；置顶跳转 = artifacts 页签+直接展开该看板；
   - 切页签即写 landing_tab（程序自跳不写）；看板行「设为默认看板/取消」+
     默认徽标。

### 边界（显式不做）

- 偏好版本化/历史（ROADMAP 远期：随溯源账本演进的偏好演进史）；
- 偏好面扩容（主题、密度、每看板记忆等）——三类先行，键面冻结在契约里；
- 未登录（share 公开页）侧任何偏好读写；
- 管理员代管/代改成员偏好。

## Impact

api：console/auth+新 preferences 路由+迁移 0029（双 PG）；web：App/ConsoleView/
ArtifactsPage+api client；生产 api+web 滚版；引擎/cell/告警零改动；不动
`POST /api/console/session` 契约（偏好独立 GET，不塞会话响应）。
