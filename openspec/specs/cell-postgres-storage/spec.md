# cell-postgres-storage Specification

## Purpose
TBD - created by archiving change wire-cell-postgres-storage. Update Purpose after archive.

## Requirements

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

### Requirement: cell-manager 自动建库发凭据

`CELL_STORAGE=postgres` 时，cell-manager 开通（provision）MUST自动完成
一 cell 一库供给：建属主角色与 `wire_cell_<customer_id>` 库、`REVOKE CONNECT
… FROM PUBLIC`、隔离验证、生成口令、凭据入 OpenBAO（`secret/wire/cell-db/
<customer_id>`，KV v2）、容器 env 注入 `DAAS_DATABASE_URL`。全程MUST不把
凭据明文落入日志、审计、state 或镜像。`CELL_STORAGE` 未设或为 sqlite 时
行为MUST与现状字节一致。

#### Scenario: 全自动开通
- **WHEN** 对新客户以 postgres 画像执行 provision
- **THEN** 库/角色/ACL 就位、隔离验证通过、OpenBAO 条目写入、容器以 PG
  数据根就绪，返回信息含库名与注入的 env 名（不含明文凭据）

#### Scenario: 幂等重入
- **WHEN** 对已 PG 化客户重复 provision（或 ACTIVE 前失败重试）
- **THEN** 不重复建库；ACL 校验仍在；凭据从 OpenBAO 读取复用；OpenBAO
  条目缺失时轮换口令自愈（ALTER ROLE+重写条目）

#### Scenario: 失败零半写
- **WHEN** 供给链任一步失败（建库/验证/OpenBAO/容器）
- **THEN** 本次新建的库与角色被回滚清理，不留孤儿；既有资产不受损

### Requirement: 凭据入 OpenBAO 且令牌受限

cell 数据根凭据MUST以 OpenBAO 为唯一持久存放（容器 env 为运行期注入）；
cell-manager 使用的 tokenMUST受 ACL 策略限定仅可访问 `wire/cell-db/*`
路径族（越路径拒绝）。OpenBAO 处于封印态时开通MUST fail-closed 并如实
报错（提示先解封），不得静默降级为本地明文存放。

#### Scenario: 越路径拒绝
- **WHEN** 以 cell-manager token 尝试读写 `wire/cell-db/*` 以外的路径
- **THEN** 被拒绝（permission denied）

#### Scenario: 封印 fail-closed
- **WHEN** OpenBAO 封印或不可达时执行 postgres 画像 provision
- **THEN** 开通失败并明确报错（含解封指引），不产生任何本地凭据落盘

### Requirement: 生命周期语义与备份覆盖

purge（保留期满清退）MUST删除该 cell 的 PG 库与属主角色（与卷清除对齐）；
deprovision（退租保留期）MUST保留库（与卷保留对齐）。备份脚本MUST动态
发现 `wire_cell_%` 前缀的全部库并逐库备份——新增 cell 库无需人工改清单。

#### Scenario: purge 删库
- **WHEN** 对 postgres 画像 cell 执行 purge
- **THEN** `wire_cell_<id>` 库与角色被删除，卷清除照旧，审计留痕

#### Scenario: 备份自动覆盖新库
- **WHEN** 新建任意 `wire_cell_*` 库后运行备份脚本
- **THEN** 该库被自动纳入备份产物，无需修改脚本

### Requirement: 可选存储配额

引擎数据根为 PostgreSQL 时MUST支持可选存储配额：env
`FD_CELL_STORAGE_QUOTA_MB` 设为正整数即启用，未设或非正则完全不设防
（两后端行为与现状一致）。配额检查MUST在全部增量写入路径的事务头执行
（全量快照提交、增量合并、批量写、派生、指标连接、link 物化、刷新重物
化）——已超限即整批拒绝/整事务回滚，错误文案MUST含用量、上限与「超过
上限」字样（api 侧配额语义识别可直接命中）。配额与行数上限MUST叠加生效
而非替代。sqlite 数据根MUST不受该 env 影响（行数闸仍其唯一保护）。

#### Scenario: 超限整批拒绝
- **WHEN** PG 根 cell 设置了配额且 `pg_database_size` 已超限，任一增量
  写入路径被调用
- **THEN** 整批拒绝/整事务回滚，错误含「超过上限」与用量数值，旧数据
  完整保留

#### Scenario: 未设零变化
- **WHEN** 未设置该 env（或值非法回落为未设）
- **THEN** 两种后端行为与现状完全一致（sqlite 根下设置该 env 也不产生
  任何新行为）

### Requirement: 用量可观测

配额启用时 overview MUST回带 `storage_quota_bytes` 与
`storage_used_bytes`（`pg_database_size` 口径）；未启用时MUST不回带这
两字段（响应形状与现状一致）。

#### Scenario: 配额观测
- **WHEN** 对配额启用中的 PG 根数据集调用 overview
- **THEN** 返回含配额与当前用量字节数，且 used 与库真实尺寸一致

### Requirement: cell-manager 配额下发

cell-manager MUST支持 `CELL_PG_QUOTA_MB`（可选正整数，非法值
ConfigError）：provision 时注入 cell env `FD_CELL_STORAGE_QUOTA_MB`，
postgres 画像与 sqlite 画像的注入行为一致定义（sqlite 下注入无害，引擎
侧不生效）。未配置时不注入该 env。

#### Scenario: 下发
- **WHEN** 以 CELL_PG_QUOTA_MB=512 执行 provision
- **THEN** 容器 env 含 FD_CELL_STORAGE_QUOTA_MB=512；未配置的 provision
  不含该 env
