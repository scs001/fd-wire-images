# Proposal: wire-pipeline-actions — 控制台重派生与定时管理（管线操作联动）

## Why

派生数据集的重派生（手动+级联）与定时登记的**端点早已接受控制台会话**
（data-plane 双凭据鉴权 console channel），但控制台没有入口——客户要刷新
派生链只能等 cron 或造 wdk key。管线视图 v0 定下的「操作入口在数据集详情页」
由本变更兑现。

## What Changes

- **重派生按钮（DatasetDetail）**：仅 derived + cell 档显示——「重派生」
  （级联默认开、可关）→ `POST /datasets/{id}/refresh`（console Bearer）→
  级联结果列表如实呈现（各级 dataset 名/深度/ok/failed+原因）；刷新后
  详情数据（行数/更新时间）同步刷新。
- **定时重派生管理（DatasetDetail）**：cron 五字段输入（登记/更新）+
  当前登记态（cron/enabled/last_run_at/last_status/last_error/last_rows
  如实呈现）+ 删除登记；naive UTC 口径诚实注记（客户时区换算属登记侧
  既有 v1 约定）。
- 管线视图保持只读（节点跳详情操作，v0 红线不动）。

## Impact

- **specs**：transform-pipeline ADDED「控制台重派生与定时管理」。
- **web**：DatasetDetail 两段 + api client 四函数（refresh/getSchedule/
  putSchedule/deleteSchedule）+ i18n。**零 api 改动**（端点已支持 console
  channel——本变更纯 UI 兑现）。
- **兼容**：纯新增入口；wdk 轨道行为不动。

## Non-Goals

- 管线视图节点上的直接操作按钮（保持只读，跳详情操作）。
- 客户时区 cron 换算 UI（v1 口径=登记侧换算，如实注记）。
- api 层数据集（免费档）重派生（source_kind 闸如实 400/403）。
