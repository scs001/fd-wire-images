# demand-orchestration Specification（delta）

## ADDED Requirements

### Requirement: 操作员控制台 SHALL 提供出单动作

admin 派发台（双闸鉴权面）MUST为需求单提供「转生成工单」动作，复用与
内部端点完全相同的出单 service 语义（确定性映射、脱敏红线、三态 slug
探测、append-only 留档、出单不迁移状态）；仅 `in_progress` 单可出（其余
409 如实回绝）。成功响应 MUST在控制台展示工单全文（折叠）与
ticket_path、slug_check 释义，并明示「已入队未投递——等待舰队拉取器
（约 30 分钟）或人工推送」；MUST NOT展示任何自动化投递成功假象。失败
（无源地址 422、slug 占用 409）MUST在控制台如实回显服务端文案。

#### Scenario: 控制台出单
- **WHEN** 操作员在 admin 派发台对带合法源地址的 in_progress 需求单点击「转生成工单」
- **THEN** 走与内部端点同一 service 完成出单留档入队，页面展示工单全文与未投递诚实文案，需求单状态不变

#### Scenario: 控制台失败如实回显
- **WHEN** 操作员对无 source_urls 的单、slug 已占用的单或非 in_progress 的单触发出单
- **THEN** 控制台按服务端口径（422/409/409）如实回显原因，不产生任何留档

#### Scenario: 双闸继承
- **WHEN** 未过 admin 双闸（Bearer ID token 缺失/无 wire-admin 角色，或 X-Admin-Token 不符）访问控制台出单端点
- **THEN** 401/403 回绝，与 admin 端点族既有口径一致
