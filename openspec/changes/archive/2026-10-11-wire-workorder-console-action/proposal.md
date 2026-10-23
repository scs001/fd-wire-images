# Proposal: wire-workorder-console-action — 操作员控制台「转生成工单」动作

## Why

wire-demand-generation-orchestration（2026-10-11 归档）落地了生成工单的
确定性映射与舰队拉取投递，但出单入口只有内部令牌端点
（`POST /api/v1/demands/{id}/workorder`，X-Internal-Token）——操作员实际
工作面是 admin 派发台（`#/admin/demands`，双闸鉴权），上面没有任何出单
动作；出单要靠本机 curl，人机闭环缺一环。主 spec 场景「操作员对带合法
源地址的 in_progress 需求单点击『转生成工单』」至今没有对应的 UI 事实。

## What Changes

- **api**：把 `export_workorder` 路由体下沉为 `demands/orchestration.py`
  的 service 函数（现有 `/api/v1/demands/{id}/workorder` 行为零变化），
  admin 路由新增 `POST /internal/admin/demands/{id}/workorder`（挂既有
  `require_admin_access` 双闸），复用同一 service——语义、响应形状、
  409/422 口径与内部端点完全一致。
- **web**：admin 派发台对 `in_progress` 单在派发面板内提供「转生成工单」
  动作：成功后展示 ticket_path、slug_check 三态释义、以及**工单全文
  （折叠）+「未投递，等待舰队拉取器（约 30 分钟）或人工推送」**诚实文案
  （demand-orchestration「诚实降级」场景的控制台兑现）；失败（非
  in_progress 409 / 无源 422 / slug 占用 409）在既有琥珀提示框如实回显。
- **不改**：状态机、投递通道、状态回流、客户面时间线——全部沿用主 spec
  既有要求。

## Capabilities

### Modified

- `demand-orchestration`：新增「操作员控制台出单动作」要求（内部端点与
  控制台动作共用同一 service 语义与红线）。
