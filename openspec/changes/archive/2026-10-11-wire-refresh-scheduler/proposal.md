# wire-refresh-scheduler

## Why

「用户会持续优化和迭代这一套系统」的自动化半边缺失：派生数据集与看板刷新都
是手动按钮。本 change 交付**统一刷新调度器**——既有后台驱动范式（计量 driver
的 lifespan asyncio task）上加一个 tick 调度循环，统一执行两类目标：**派生
数据集刷新**（既有 dataset_refresh 服务，零 LLM 成本）与**看板 AI 刷新**
（既有无头刷新轮）；并补**级联刷新**（父刷新自动传导派生子，transitively、
防环）。cron 表达式、失败退避 = 后续。

## What Changes

- **表 `refresh_schedules`**（元数据库，alembic 0023）：id、customer_id、
  target_type（dataset|artifact）、target_id、interval_minutes、next_run_at、
  paused、last_status（pending|running|ok|failed）、last_run_at、last_error、
  created_at；每客户默认上限 10（env `FD_WIRE_SCHEDULE_MAX_PER_CUSTOMER`）。
- **最小间隔地板**：dataset ≥60 分钟、artifact ≥360 分钟（AI 刷新耗 LLM
  token，防无人值守烧钱；env 可调 `FD_WIRE_SCHEDULE_MIN_INTERVAL_*`）。
- **调度器**（lifespan asyncio task，`FD_WIRE_REFRESH_SCHEDULER_ENABLED` 默认
  true、tick 60s）：取 due 且未 paused 的调度（单飞全局串行——同一 tick 逐个
  执行，执行体经 asyncio.to_thread 包同步调用）；执行前置 running、后置
  ok/failed+last_error+next_run_at=now+interval；目标已删/归档→自动删除调度。
- **级联刷新**：数据集刷新完成后，其 derive 来源链上的子数据集（transitively）
  依次刷新（防环：路径上已访问集合）；手动刷新端点增加 `cascade` 查询参数
  （默认 true）。
- **CRUD API**：`GET /api/v1/refresh-schedules`（本人列表）、`POST`（建：目标
  属主校验+类型校验——dataset 须 derived 来源、artifact 须 kind=dashboard；
  interval 地板校验）、`DELETE /{id}`、`POST /{id}/pause|resume`；跨客户 404。
- **web**：DatasetDetail（derived）与 DashboardPane（查看态）各加「定时刷新」
  小节：创建（间隔选择=地板起步的预设档）、暂停/恢复、删除、显示下次运行/
  上次结果/上次错误。i18n 双语。

## Capabilities

### New Capabilities

- `refresh-scheduler`: 统一定时刷新——调度表与 CRUD、tick 执行循环（计量驱动
  范式）、间隔地板与客户配额、级联刷新防环、目标失存自动清理。

## Impact

- **fd-wire/api**：schedules 模块（表/service/CRUD/线程）、data_plane 刷新
  服务加级联、artifacts 复用 refresh.py、alembic 0023、pytest。
- **fd-wire/web**：两处管理小节、api.ts 追加、i18n、测试。
- **成本口径**：artifact 调度=每轮 LLM 探索轨道（平台凭据不计客户账单），
  地板 360min 封住无人值守成本面。
