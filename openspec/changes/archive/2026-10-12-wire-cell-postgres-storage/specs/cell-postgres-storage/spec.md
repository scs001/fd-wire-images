# cell-postgres-storage Specification（delta）

## ADDED Requirements

### Requirement: 可选 PostgreSQL 数据根

cell 引擎数据根MUST由 `DAAS_DATABASE_URL` 定义：指向
`postgresql+psycopg://…` 时以 PostgreSQL 为数据根（建表迁移与全部
customer-dataset 工具在 PG 上工作）；未配置或指向 sqlite 时MUST保持既有
sqlite 行为不变（默认路径、文件位置、产物字节均不变——存量 cell 升级镜像
后无感）。方言选择MUST由连接 URL 派生，不引入第二配置面。

#### Scenario: 指向 PG 即 PG 根
- **WHEN** cell 以 `DAAS_DATABASE_URL=postgresql+psycopg://…` 启动
- **THEN** 引擎建表与全部数据工具运行于该 PG 库，行为与 sqlite 后端一致
  （双方言矩阵为证）

#### Scenario: 不带 PG env 行为不变
- **WHEN** cell 不设该 env（或仍指 sqlite）启动并升级到新镜像
- **THEN** 数据根仍为卷内 sqlite 文件，既有数据与行为零变化

### Requirement: 双方言行为一致

customer-dataset 全行为面（建集/上传/修正回滚/派生/link/增量同步/删除/
事件 WAL/查询超时护栏）MUST在同一套测试矩阵下于 sqlite 与 PostgreSQL 两
后端执行并通过；两后端的外部可观察行为（工具返回、错误文案、行哈希口径、
血缘记录）MUST一致，差异即缺陷。SQLite 专属设施MUST有 PG 等价实现：
占位符适配、事务头、表内省、自增序、查询超时护栏（progress_handler ↔
statement_timeout，超时文案相同）。

#### Scenario: 矩阵双后端全绿
- **WHEN** 测试矩阵分别在 sqlite 与 PG（一次性测试库）后端运行
- **THEN** 全部用例两后端通过，且无需按后端分叉断言

### Requirement: 一 cell 一库与属主凭据纪律

cell 数据根迁 PG 时MUST一 cell 一库（`wire_cell_<cell-key>`），库属主为
该 cell 专属角色；属主凭据MUST只发给该 cell 引擎（容器 env 注入，宿主
0600 文件纪律，与 wgk- 客户凭据同款）。cell 库MUST拒绝平台角色访问：
`PUBLIC` 与平台 api/管理角色对该库无 CONNECT 权限（红线「平台侧永不直读
cell 数据」由数据库 ACL 落死）。凭据MUST不落镜像、不进 git。

#### Scenario: 平台角色连不上 cell 库
- **WHEN** 以平台侧凭据（api 库账号）尝试连接 cell 库
- **THEN** 连接被拒绝（无 CONNECT 权限）

#### Scenario: cell 属主只及本库
- **WHEN** 以 cell 属主凭据登录
- **THEN** 仅可访问本 cell 库，越库访问被拒

### Requirement: 试点迁移与回退保底

存量 cell 迁移MUST按低峰搬迁流程执行：原 sqlite 卷不清不删（回退保底），
迁移=重建容器换 `DAAS_DATABASE_URL` env；回退=移除该 env 重建即回 sqlite
数据根。首个试点MUST在生产 drill cell（drillcust001）完成并通过 MCP 实弹
验收（建集/上传/修正/派生/link/增量/删除全行为面在 PG 根上真跑）。

#### Scenario: 试点实弹
- **WHEN** drill cell 以 PG 数据根重建后经 MCP 通道执行全行为面操作
- **THEN** 各操作返回与 sqlite 后端口径一致，数据落在 `wire_cell_*` 库

#### Scenario: 回退保底
- **WHEN** 迁移后移除 PG env 并重建容器
- **THEN** cell 回到 sqlite 数据根，原卷数据完好可用
