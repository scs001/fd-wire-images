# Proposal: wire-demand-visibility — 需求单编排可见性（运营进度面+客户交付就绪面+管理台交付同语义修复）

## Why

方向 C 盘点的结论：**中间段其实已打通**（需求单→工单 YAML→舰队拉取→萬星
生成→PR→人工审核→状态回流→客户时间线，DE-20261011-99 生产实证过）。被用户
三件输入（合规规则集/萬星直连凭据/商务口径）挡住的是**策略面**（自动触发、
审查门、定价分叉），但盘点同时抓出**不需要外部输入、纯内部可做**的三件，
其中一件是真缺陷：

1. **管理台流转漏自动交付钩子（真缺陷）**：内部端点
   `/internal/demands/{id}/transition` 在 delivered 时调
   `delivery_service.maybe_auto_deliver`（routes.py:168-169），管理台
   `/internal/admin/demands/{id}/transition` 直调 service 层**没有这个
   钩子**——从管理台交付的需求单 `scope_status` 留 None，客户私有数据
   面永远不开通。运营实际用的就是管理台，等于该钩子在生产主路径上失效。
2. **运营侧进度不可见**：`GET /internal/admin/demands` 已返回
   `pipeline_events`/`delivery_events`/`gate` 全文，但管理台 TS 类型没接、
   页面不渲染——运营看工单走到哪一步只能去翻 API 或对方仓。
3. **客户侧交付就绪不可见**：`delivery` 视图（scope 就绪/公开授权）已在
   客户 API 返回（`DemandOut.delivery`），web 完全没渲染——客户交付后
   看不到「数据已就绪」。

## What Changes

1. **管理台交付同语义（api 修复）**：`admin/routes.py` 的 transition 在
   target=delivered 时补调 `maybe_auto_deliver`（注入 `scope_client_dep`），
   与内部端点逐字同语义——scope 失败只落 pending、绝不打断迁移。
2. **运营进度面（web admin）**：`AdminDemand` TS 类型补齐 API 已返回的
   `pipeline_events`/`delivery_events`/`gate`/`scope_status`/`scope_name`；
   派发台每单可展开进度面：事件时间线（已出单/已投递/已入生成/待审核/
   已合并/受阻/工单丢失，含时间、ticket_path、PR 链接）+ gate 摘要 +
   交付状态；未出单如实占位。
3. **客户交付就绪面（web console）**：`CustomerDemand` 类型补 `delivery`；
   「我的需求」列表在 delivered 单上渲染交付就绪信号（已就绪=私有数据
   面已开通 / 准备中=交付挂起重试中）+ 公开授权状态；非 delivered 不渲染；
   内部细节（delivery_events 全文、错误串）绝不出现。

### 边界（显式不做）

- **自动触发**（in_progress 即自动出单）——改变运营抽审语义，等合规门
  （用户件①）落定后再议；
- **萬星直连**（A2A 直发）——等凭据（用户件②）；
- **客户需求详情页**——列表页已有编排时间线+交付就绪，详情页无增量价值；
- **blocked/lost 运营告警**——SMTP 未配（既有挂账），邮件通道修好前不叠加；
- **「已入库」阶段**——注册发生在 fd-open-data-mcp，wire 无信号源，不虚构。

## Impact

api：admin/routes.py 一处钩子补齐+测试；web：admin 类型/进度面+console
交付就绪渲染+测试；零迁移、零引擎/cell 改动；生产 api+web 滚版。
