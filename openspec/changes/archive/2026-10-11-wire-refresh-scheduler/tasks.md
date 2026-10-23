# Tasks — wire-refresh-scheduler

## 1. 数据模型与服务（api）

- [ ] 1.1 alembic 0023 `refresh_schedules`（列全+customer_id 索引；幂等+drift check）；模型挂元数据库（照 billing engine 模式）
- [ ] 1.2 schedules/service.py：建（属主/类型/interval 地板/客户配额校验）、列表、删、pause/resume、due 查询（next_run_at<=now 且未 paused，按 next_run_at 排序）；单测全覆盖校验路径
- [ ] 1.3 执行器：execute_dataset（复用 data_plane 既有 dataset_refresh 服务逻辑，抽出可内部调用段）与 execute_artifact（复用 artifacts/refresh.py 无头轮）；执行前后置状态与 next_run_at；目标不存在/归档→删调度行
- [ ] 1.4 调度循环：lifespan task（照 metering/driver.py 范式：enabled env、tick 60s env、asyncio.to_thread 包执行、全局串行、异常记日志不断环）；可测性：tick 函数独立可注入时钟

## 2. 级联刷新（api）

- [ ] 2.1 derive 子链发现：按 lineage 反查直接子（derived 来源=本数据集），transitively 展开+防环（visited 集合）；深度上限 10
- [ ] 2.2 dataset_refresh 端点加 cascade 查询参数（默认 true）：刷新本体后依次级联子链（顺序执行，任一失败记录错误但继续其余，响应含各级结果摘要）；单测：三层链/环防护/失败隔离

## 3. CRUD API（api）

- [ ] 3.1 GET /api/v1/refresh-schedules、POST（校验齐全：dataset 须 derived、artifact 须 dashboard、属主、地板、配额）、DELETE /{id}、POST /{id}/pause|resume；跨客户 404；pytest 全路径
- [ ] 3.2 环境项落 config 注释（FD_WIRE_REFRESH_SCHEDULER_ENABLED/TICK/MIN_INTERVAL_DATASET/MIN_INTERVAL_ARTIFACT/MAX_PER_CUSTOMER）

## 4. web

- [ ] 4.1 api.ts 追加 schedules CRUD 函数；DatasetDetail（derived 数据集）与 DashboardPane 查看态各加「定时刷新」小节（预设间隔档=类型地板起步：dataset 1h/6h/24h、artifact 6h/24h/7d；创建/暂停恢复/删除；下次运行/上次结果/上次错误展示；i18n ZH/EN）
- [ ] 4.2 组件测试：两处小节创建流/暂停切换/错误展示

## 5. 验证

- [ ] 5.1 api 全量绿+web 全量绿+tsc 干净
- [ ] 5.2 生产冒烟（随发布）：schedules 端点未登录 401；建一个 dataset 调度→等一个 tick→last_status=ok 证据（或调 tick 间隔 env 短窗验证）存 reports/
