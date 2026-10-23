## ADDED Requirements

### Requirement: 萬星直连调度 SHALL 同映射同红线且留痕可审

操作员对 in_progress 需求单触发萬星直连调度时，系统 MUST 按与生成工单
完全相同的确定性映射与脱敏红线构造工单 YAML（含禁爬源复检与 slug 三态
探测：occupied 拒、free/unverified 放行），经 A2A `message/send` 直连
spider-heal（调用键经 secret 注入、新域 wanxing.finddatatech.cloud、
`Idempotency-Key` 头），成功后 MUST 把投递事实 append-only 留档
（`wanxing_dispatched` 事件：工单路径/全文/哈希、幂等键、message id、
HTTP 状态、agent 回执摘录）。直连单 MUST NOT 进入舰队拉取队列
（拉取队列只认 `workorder_exported` 事件——通道互斥由操作员选择、事件
类型天然区分）。同一工单内容重复直连 MUST 派生同一幂等键（wanxing 侧
去重口径）。

#### Scenario: 直连成功
- **WHEN** 操作员对带合法源地址的 in_progress 需求单点击「萬星直连」且 A2A 调用 2xx
- **THEN** 响应携带工单全文与 a2a 回执（http_status/message_id/idempotency_key/reply_excerpt），需求单时间线追加 `wanxing_dispatched`，状态不迁移，拉取队列不收录该单

#### Scenario: 幂等键稳定
- **WHEN** 同一需求单以同一工单内容重复直连
- **THEN** 两次派生的 Idempotency-Key 相同（`wire-<code>-<sha256 前 16 位>`）

#### Scenario: 出单闸复用
- **WHEN** 非 in_progress、或缺 source_urls、或命中禁爬源、或 slug 已占用
- **THEN** 与「转生成工单」同一拒绝口径（409/422），零事件留档

### Requirement: 直连失败 SHALL 如实降级绝不假成功

萬星直连凭据缺失（env 未配置）MUST 503 如实回绝（不静默回落舰队通道）；
A2A 调用失败（网络不可达、401/403 调用键或白名单问题、非 JSON 响应）
MUST 记 `wanxing_dispatch_failed` 事件（error/reason 留档）并以 502 回绝，
响应与失败事件 MUST 携带工单全文——人工推送降级通道始终可用，MUST NOT
在任何失败形态下呈现自动化投递成功假象。

#### Scenario: 未配置如实拒
- **WHEN** `WANXING_CALLER_KEY` 未配置时操作员触发直连
- **THEN** 503 明示「萬星直连未配置」，零事件留档

#### Scenario: 调用失败留档降级
- **WHEN** A2A 调用返回 403（调用者不在白名单）或网络不可达
- **THEN** 502 携带错误定位与工单全文，时间线追加 `wanxing_dispatch_failed`（含 error/reason），不追加 `wanxing_dispatched`

### Requirement: 直连端点 SHALL 内部令牌与控制台双闸

萬星直连调度 MUST 同时暴露内部端点（`X-Internal-Token` 与
`FD_WIRE_INTERNAL_TOKEN` 比对，401 口径与既有内部端点一致）与操作员
控制台动作（admin 鉴权），两闸共用同一 service——语义、响应形状、拒绝
口径完全一致。

#### Scenario: 内部端点鉴权
- **WHEN** 无内部令牌调用 `POST /api/v1/demands/{id}/wanxing-dispatch`
- **THEN** 401，与既有内部端点口径一致

#### Scenario: 双闸同口径
- **WHEN** 同一需求单分别经内部端点与控制台动作触发直连
- **THEN** 走同一 service（`dispatch_wanxing_service`），响应形状与拒绝口径逐字段一致
